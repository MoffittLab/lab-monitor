"""archive_metrics.py against a real Manager database; retention is opt-in."""
import json
import logging
import os
import subprocess
import sys

import archive_metrics
import manager as mgr
from manager_helpers import READ, auth, base_config, make_msg, metrics_payload, post_queue


def _seed(client):
    msgs = [
        make_msg('system_metrics', metrics_payload(1, 1), ts='2020-01-15T00:00:00Z'),
        make_msg('system_metrics', metrics_payload(2, 2), ts='2020-02-15T00:00:00Z'),
        make_msg('system_metrics', metrics_payload(3, 3), ts=mgr.utc_now()),
        make_msg('folder_usage', {'_unit': 'bytes', '/volume1': 1, '/volume1/A': 1}, ts='2020-01-16T00:00:00Z'),
        make_msg('folder_usage', {'_unit': 'bytes', '/volume1': 2, '/volume1/A': 2}, ts=mgr.utc_now()),
        make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 1.0, 'ram_bytes': 1}, ts='2020-01-15T00:00:01Z'),
        make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 2.0, 'ram_bytes': 2}, ts=mgr.utc_now()),
    ]
    assert post_queue(client, msgs).get_json()['counts']['stored'] == 7


def _lines(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def test_retention_archives_then_deletes(client, data_dir):
    _seed(client)
    result = archive_metrics.run_history_retention(
        mgr._data_store, mgr._metrics_db, data_dir, 365, logging.getLogger('t'))
    assert result['archived'] == {
        'triton/system_metrics': 2, 'triton/folder_usage': 1, 'triton/user_metrics': 1,
        'user_metrics_history': 1,
    }

    archive_dir = os.path.join(data_dir, 'triton', 'archive')
    jan = _lines(os.path.join(archive_dir, '2020-01-system_metrics.jsonl'))
    assert jan[0]['network_bytes_in'] == 1
    assert _lines(os.path.join(archive_dir, '2020-02-system_metrics.jsonl'))[0]['network_bytes_in'] == 2
    fu = _lines(os.path.join(archive_dir, '2020-01-folder_usage.jsonl'))
    assert fu[0]['/volume1/A'] == 1 and fu[0]['_unit'] == 'bytes'
    assert _lines(os.path.join(archive_dir, '2020-01-user_metrics_history.jsonl'))[0]['username'] == 'alice'

    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert [r['network_bytes_in'] for r in rows] == [3]
    rows = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows']
    assert [r['/volume1'] for r in rows] == [2]
    hist = client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()
    assert [h['cpu_percent'] for h in hist] == [2.0]

    # second run: nothing left to do
    again = archive_metrics.run_history_retention(
        mgr._data_store, mgr._metrics_db, data_dir, 365, logging.getLogger('t'))
    assert again['archived'] == {}


def test_dry_run_deletes_nothing(client, data_dir):
    _seed(client)
    result = archive_metrics.run_history_retention(
        mgr._data_store, mgr._metrics_db, data_dir, 365, logging.getLogger('t'), dry_run=True)
    assert result['archived']['triton/system_metrics'] == 2
    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 3
    assert not os.path.exists(os.path.join(data_dir, 'triton', 'archive'))


def _run_script(cfg_path, *extra):
    script = os.path.join(os.path.dirname(mgr.__file__), 'archive_metrics.py')
    return subprocess.run([sys.executable, script, '--config', cfg_path, *extra],
                          capture_output=True, text=True, timeout=120)


def test_script_default_is_off_and_legacy_retention_days_ignored(client, data_dir, scratch):
    _seed(client)
    mgr._close_stores()
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(data_dir, retention_days=90), f)
    r = _run_script(cfg_path)
    assert r.returncode == 0, r.stderr
    assert 'disabled' in r.stderr

    client = mgr.app.test_client()
    mgr.init_app(base_config(data_dir), logging.getLogger('t'))
    assert len(client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']) == 3


def test_script_runs_against_real_db(client, data_dir, scratch):
    _seed(client)
    mgr._close_stores()
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(data_dir, history_retention_days=365), f)
    r = _run_script(cfg_path)
    assert r.returncode == 0, r.stderr
    assert 'completed successfully' in r.stderr

    mgr.init_app(base_config(data_dir), logging.getLogger('t'))
    client = mgr.app.test_client()
    assert len(client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']) == 1
