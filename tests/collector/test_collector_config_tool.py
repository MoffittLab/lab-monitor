"""config_tool.py: nested blocks, JSON values, and validation against the collector's checks."""
import json
import os
import subprocess
import sys

import pytest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
TOOL = os.path.join(REPO_DIR, 'collector', 'config_tool.py')
EXAMPLES = ['config.example.json', 'config.synology.example.json', 'config.windows.example.json',
            'config.linux.example.json']


def tool(config_path, *args):
    proc = subprocess.run([sys.executable, TOOL, '--config', str(config_path), *args],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'name': 'box', 'manager_token': 'secret-0123456789abcdef',
                                'sessions': {'enabled': True, 'detail': 'full'}}))
    return path


def _read(path):
    with open(path) as f:
        return json.load(f)


def test_nested_block_round_trip(cfg):
    assert tool(cfg, 'set', 'sessions.timeout_seconds', '30')[0] == 0
    assert _read(cfg)['sessions'] == {'enabled': True, 'detail': 'full', 'timeout_seconds': 30}
    assert tool(cfg, 'get', 'sessions.detail') == (0, 'full', '')
    assert tool(cfg, 'get', 'sessions') == (0, json.dumps({'enabled': True, 'detail': 'full', 'timeout_seconds': 30}), '')
    assert tool(cfg, 'set', 'storage_health.enabled', 'false')[0] == 0
    assert _read(cfg)['storage_health'] == {'enabled': False}
    assert tool(cfg, 'get', 'sessions.nothing')[0] == 2
    assert tool(cfg, 'set', 'name.sub', '1')[0] == 1               # 'name' is not a block


def test_set_into_a_null_block(cfg):
    config = _read(cfg)
    config['sessions'] = None                # the collector reads a null block as a missing one
    cfg.write_text(json.dumps(config))
    assert tool(cfg, 'set', 'sessions.timeout_seconds', '30')[0] == 0
    assert _read(cfg)['sessions'] == {'timeout_seconds': 30}


def test_json_values(cfg):
    assert tool(cfg, 'set', 'disk_io_exclude', '["sdz", "usb*"]')[0] == 0
    assert _read(cfg)['disk_io_exclude'] == ['sdz', 'usb*']
    assert tool(cfg, 'set', 'counter_max_window_seconds', 'null')[0] == 0
    assert _read(cfg)['counter_max_window_seconds'] is None


@pytest.mark.parametrize('field,value,stored', [
    ('perf_counters', 'maybe', True),
    ('user_metrics', 'busy', 'auto'),
    ('counter_max_window_seconds', '1', 86400),
    ('disk_io_exclude', 'sdz', []),
    ('sessions.timeout_seconds', '-1', 20),
])
def test_invalid_values_are_replaced_by_the_default_with_a_warning(cfg, field, value, stored):
    code, out, err = tool(cfg, 'set', field, value)
    assert code == 0 and 'WARNING: Invalid' in err and 'default' in err
    node = _read(cfg)
    for part in field.split('.'):
        node = node[part]
    assert node == stored
    code, out, _ = tool(cfg, 'set', field, value, '--json')
    assert json.loads(out)['warning'].startswith(f'Invalid {field}=')


def test_valid_values_are_stored_as_given(cfg):
    assert tool(cfg, 'set', 'user_metrics', 'active') == (0, "Added 'user_metrics': \"active\"", '')
    assert tool(cfg, 'set', 'user_metrics_min_cores', '0.5')[0] == 0
    assert _read(cfg)['user_metrics_min_cores'] == 0.5


def test_check_reports_what_the_collector_would_ignore(cfg):
    assert tool(cfg, 'check') == (0, 'All validated settings are valid', '')
    data = _read(cfg)
    data.update(perf_counters='yes', sessions={'timeout_seconds': 'soon'})
    cfg.write_text(json.dumps(data))
    code, out, _ = tool(cfg, 'check')
    assert code == 3 and "Invalid perf_counters='yes'" in out and "Invalid sessions.timeout_seconds='soon'" in out
    code, out, _ = tool(cfg, 'check', '--json')
    assert code == 3 and {p['field'] for p in json.loads(out)['invalid']} == {'perf_counters',
                                                                              'sessions.timeout_seconds'}


@pytest.mark.parametrize('name', EXAMPLES)
def test_shipped_examples_pass_the_check(tmp_path, name):
    path = os.path.join(REPO_DIR, 'collector', name)
    assert tool(path, 'check')[0] == 0
    data = _read(path)
    # The examples spell out the counter defaults
    from_schema = {k: v for k, v in data.items() if k in ('perf_counters', 'counter_max_window_seconds',
                                                         'user_metrics', 'disk_io', 'disk_io_exclude')}
    assert from_schema == {'perf_counters': True, 'counter_max_window_seconds': 86400, 'user_metrics': 'auto',
                           'disk_io': True, 'disk_io_exclude': []}
    if 'windows' not in name:
        assert data['user_cpu_count_children'] is True
    if 'windows' in name or 'linux' in name:
        assert data['gpu_per_user'] == 'auto' and data['metrics_interval_seconds'] == 300
    # The nested blocks, with every key spelled out (Windows leaves out the POSIX-only owner keys)
    from lib import settings
    for block in ('sessions', 'storage_health', 'storage_breakdown'):
        keys = {k.split('.', 1)[1] for k in settings.SCHEMA if k.startswith(block + '.')}
        if 'windows' in name and block == 'storage_breakdown':
            keys -= {'owners', 'name_lookup_budget_seconds'}
        assert set(data[block]) == keys, (name, block)


def test_collector_reads_the_same_defaults(collector):
    from lib import settings
    for name in EXAMPLES:
        with open(os.path.join(REPO_DIR, 'collector', name)) as f:
            data = json.load(f)
        for key in settings.SCHEMA:
            if settings.raw_value(data, key) is not None:
                assert settings.value(data, key) == settings.SCHEMA[key][1], (name, key)


def test_whole_block_round_trip_with_checks(cfg):
    block = {'detail': 'bogus', 'timeout_seconds': 30, 'detial': 'full',
             'ports': {'smb': [445, 139], 'Bad Name': [1], 'ssh': [0, 22]}, 'exclude_clients': ['10.0.0.0/8', 'x']}
    code, out, err = tool(cfg, 'set', 'sessions', json.dumps(block))
    assert code == 0
    assert "Invalid sessions.detail='bogus'" in err and 'Unknown key sessions.detial' in err
    assert 'drop the invalid entries' in err
    assert _read(cfg)['sessions'] == {'detail': 'full', 'timeout_seconds': 30, 'detial': 'full',
                                      'ports': {'smb': [139, 445], 'ssh': [22]}, 'exclude_clients': ['10.0.0.0/8']}
    assert json.loads(tool(cfg, 'get', 'sessions', '--json')[1])['value']['ports'] == {'smb': [139, 445], 'ssh': [22]}
    code, out, _ = tool(cfg, 'check')
    assert code == 3 and "Unknown sessions.detial='full'" in out
    # A block that is not an object counts as missing: stored as {} with a warning
    code, _, err = tool(cfg, 'set', 'storage_breakdown', '5')
    assert code == 0 and 'must be a JSON object' in err and _read(cfg)['storage_breakdown'] == {}
    assert 'Unknown key storage_health.intervall' in tool(cfg, 'set', 'storage_health.intervall', '60')[2]


def test_check_flags_a_block_that_is_not_an_object(cfg):
    data = _read(cfg)
    data['storage_health'] = ['not', 'a', 'block']
    cfg.write_text(json.dumps(data))
    code, out, _ = tool(cfg, 'check', '--json')
    assert code == 3 and [p['field'] for p in json.loads(out)['invalid']] == ['storage_health']


@pytest.mark.parametrize('field,value,stored', [
    ('storage_health.temperature_c', '{"hdd": [45, 55]}', {'hdd': [45, 55], 'ssd': [70, 80]}),
    ('storage_breakdown.cold_days', '[180, 720]', [180, 720]),
    ('storage_breakdown.owners', 'none', 'none'),
    ('sessions.detail', 'counts', 'counts'),
])
def test_new_block_keys_accept_valid_values(cfg, field, value, stored):
    code, _, err = tool(cfg, 'set', field, value)
    assert code == 0 and err == ''
    from lib import settings
    assert settings.value(_read(cfg), field) == stored


@pytest.mark.parametrize('field,value', [
    ('storage_breakdown.cold_days', '[720, 180]'), ('storage_breakdown.max_timeout_fraction', '2'),
    ('storage_health.temperature_c', '{"nvme": [1, 2]}'), ('sessions.max_entries', '0'),
    ('storage_health.ignore', '"sda"'),
])
def test_new_block_keys_reject_invalid_values(cfg, field, value):
    code, _, err = tool(cfg, 'set', field, value)
    assert code == 0 and 'WARNING: Invalid' in err and 'storing the default' in err
