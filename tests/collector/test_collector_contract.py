"""
Every message the collector builds is storable by every Manager (contract
R1, R5, R10): key names, size, string limits, and a msg_id. Message types are
produced through the real collect paths from fixtures: system_metrics,
user_metrics, sessions and storage_health from metrics runs, folder_usage
and storage_breakdown from disk runs.
"""
import json
import logging
import os
import re
import sys
import types

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32',
                                reason='needs a POSIX host (os.getuid / os.sysconf / /proc / POSIX paths)')


KEY_RE = re.compile(r'^_?[a-z][a-z0-9_]{0,62}$')
SERVICE_RE = re.compile(r'^[a-z][a-z0-9]{0,15}$')
STATUSES = {'ok', 'warning', 'critical', 'unknown'}
SOURCE_STATUSES = {'ok', 'partial', 'not_permitted', 'unavailable', 'stale', 'unsafe', 'timeout', 'error', 'off'}
RESERVED = {'id', 'timestamp', 'device_type', 'msg_id', 'data_type'}
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_KEYS = 1000
LOG = logging.getLogger('test')


def check_message(entry):
    """The contract checks for one message; returns the list of problems."""
    problems = []
    header, data = entry['header'], entry['data']
    if not re.fullmatch(r'[0-9a-f]{32}', str(header.get('msg_id'))):
        problems.append(f"msg_id {header.get('msg_id')!r}")
    keys = [k for k in data if k != 'data_type']
    if data.get('data_type') == 'folder_usage':
        keys = [k for k in keys if k.startswith('_') or k in ('total_usage',)]   # paths are the folder keys
    for key in keys:
        if not KEY_RE.match(key) or key in RESERVED or key.startswith('payload_'):
            problems.append(f'key {key!r} (R1)')
    lowered = [k.lower() for k in data]
    if len(set(lowered)) != len(lowered):
        problems.append('keys differing only in case (R1)')
    if len(data) > MAX_KEYS:
        problems.append(f'{len(data)} keys (R5)')
    size = len(json.dumps(entry, separators=(',', ':')).encode('utf-8'))
    if size > MAX_MESSAGE_BYTES:
        problems.append(f'{size} bytes (R5)')
    usernames = [data.get('username')] + [u.get('username') for g in data.get('gpus') or [] for u in g.get('users') or []]
    for name in usernames:
        if name is not None and (not isinstance(name, str) or len(name) > 256):
            problems.append(f'username {str(name)[:40]!r}... (R10)')
    if data.get('disk_busy_max_name') is not None and len(data['disk_busy_max_name']) > 32:
        problems.append('disk_busy_max_name over 32 characters')
    for g in data.get('gpus') or []:
        if g.get('uuid') is not None and len(g['uuid']) > 96:
            problems.append('GPU uuid over 96 characters')
    problems += _type_problems(data)
    return problems


def _long(value, limit):
    return value is not None and (not isinstance(value, str) or len(value) > limit)


def _type_problems(data):
    """The R10 limits and enums of the new data types."""
    problems = []
    kind = data.get('data_type')
    if kind == 'sessions':
        for source, status in (data.get('_sources') or {}).items():
            if status not in SOURCE_STATUSES:
                problems.append(f'source status {source}={status!r} (R11)')
        for key in data:
            if key.endswith(('_clients', '_users')) and not SERVICE_RE.match(key.rsplit('_', 1)[0]):
                problems.append(f'service key {key!r}')
        for row in data.get('sessions') or []:
            if not SERVICE_RE.match(row.get('service', '')):
                problems.append(f"service {row.get('service')!r}")
            if _long(row.get('username'), 256) or _long(row.get('client'), 256) or _long(row.get('client_host'), 256):
                problems.append('session row string over its limit (R10)')
    elif kind == 'storage_health':
        if data.get('overall_status') not in STATUSES:
            problems.append('overall_status')
        reasons = data.get('status_reasons') or []
        if len(reasons) > 10 or any(_long(r, 200) for r in reasons):
            problems.append('status_reasons over 10 x 200 (R10)')
        for status in (data.get('sources') or {}).values():
            if status not in SOURCE_STATUSES:
                problems.append(f'source status {status!r} (R11)')
        for list_key in ('arrays', 'disks', 'volumes'):
            for row in data.get(list_key) or []:
                ids = [row.get('id')] + list(row.get('members') or []) + list(row.get('faulty_members') or []) + \
                    list(row.get('arrays') or [])
                if any(_long(i, 64) for i in ids) or row.get('status') not in STATUSES:
                    problems.append(f'{list_key} row {str(row.get("id"))[:20]!r} (R10 / status)')
        flat = [k for k in data if re.match(r'(disk|array|volume)_', k)]
        if any(isinstance(data[k], bool) or not isinstance(data[k], (int, float)) for k in flat):
            problems.append('a flattened storage_health key that is not a number')
    elif kind == 'storage_breakdown':
        for entry in (data.get('folders') or []) + [data.get('device') or {}]:
            if _long(entry.get('path'), 4096) or _long(entry.get('folder_owner'), 256):
                problems.append('folder path or owner over its limit (R10)')
            for owner in entry.get('owners') or []:
                if _long(owner.get('owner'), 256):
                    problems.append('owner name over 256 (R10)')
            values = [entry.get(k) for k in ('cold_3y_bytes', 'cold_1y_bytes', 'bytes')]
            if None not in values and not values[0] <= values[1] <= values[2]:
                problems.append('cold_3y <= cold_1y <= bytes')
    return problems


def _gpu_module(users):
    """A fake pynvml: one GPU with a compute process per user."""
    nvml = types.ModuleType('pynvml')
    ns = types.SimpleNamespace
    nvml.NVML_TEMPERATURE_GPU = 0
    nvml.nvmlInit = nvml.nvmlShutdown = lambda: None
    nvml.nvmlSystemGetDriverVersion = lambda: '550.54.15'
    nvml.nvmlDeviceGetCount = lambda: 1
    nvml.nvmlDeviceGetHandleByIndex = lambda i: i
    nvml.nvmlDeviceGetName = lambda h: 'GPU'
    nvml.nvmlDeviceGetUUID = lambda h: 'GPU-' + 'f' * 200
    nvml.nvmlDeviceGetUtilizationRates = lambda h: ns(gpu=50)
    nvml.nvmlDeviceGetMemoryInfo = lambda h: ns(used=2 ** 30, total=2 ** 34)
    nvml.nvmlDeviceGetTemperature = lambda h, s: 50
    nvml.nvmlDeviceGetPowerUsage = lambda h: 100_000
    nvml.nvmlDeviceGetComputeRunningProcesses = lambda h: [ns(pid=pid, usedGpuMemory=2 ** 20) for pid in users]
    nvml.nvmlDeviceGetGraphicsRunningProcesses = lambda h: []
    nvml.nvmlDeviceGetProcessUtilization = lambda h, t: [ns(pid=pid, smUtil=10) for pid in users]
    return nvml


@pytest.fixture
def produced(collector, config, procfs, monkeypatch, tmp_path):
    """
    Messages from a fake Linux GPU host (two metrics runs: no window, then a
    window) with oversized names everywhere: users, the session host,
    disks; sessions from logind, storage health from a fake sysfs.
    """
    from lib import sessions, storage_health
    logind = tmp_path / 'logind'
    logind.mkdir()
    (logind / '7').write_text(f"USER={'y' * 300}\nSERVICE=sshd\nREMOTE_HOST={'h' * 300}\n")
    monkeypatch.setattr(sessions, 'UTMP_PATHS', (str(tmp_path / 'no-utmp'),))
    monkeypatch.setattr(sessions, 'LOGIND_SESSIONS', str(logind))
    monkeypatch.setattr(sessions, 'PROC_NET', str(tmp_path / 'no-net'))
    monkeypatch.setattr(storage_health, 'SYS_ROOT', os.path.dirname(procfs.sys_block))
    monkeypatch.setattr(storage_health, 'PROC_ROOT', procfs.proc)
    monkeypatch.setattr(storage_health, 'FSTAB', str(tmp_path / 'no-fstab'))
    long_name = 'x' * 300
    procfs.passwd = {1000: 'alice', 1001: long_name}
    procfs.block = {'nvme0n1': {}, 'sd' + 'a' * 40: {}}
    procfs.disks = {'nvme0n1': [0] * 11, 'sd' + 'a' * 40: [0] * 11}
    for pid, uid in ((2001, 1000), (2002, 1001), (2003, 4242)):
        procfs.proc_entry(pid, uid, cpu=100, rss=10)
    procfs.cpu.update(user=100, idle=1000)
    monkeypatch.setitem(sys.modules, 'pynvml', _gpu_module({2001: 'alice', 2002: long_name, 2003: None}))
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 1.0)
    captured = []
    monkeypatch.setattr(collector, '_record', lambda c, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    config['platform'] = 'linux'
    procfs.write()
    assert collector.collect_metrics(config, LOG) is True
    procfs.uptime += 300
    procfs.cpu.update(user=5000, idle=20000)
    procfs.disks['sd' + 'a' * 40] = [10, 0, 80, 7, 20, 0, 160, 9, 0, 33, 40]
    for p in procfs.procs.values():
        p['cpu'] += 3000
    from lib import perf_counters
    monkeypatch.setattr(perf_counters, '_wall_time', lambda t=perf_counters._wall_time(): t + 300)
    procfs.write()
    assert collector.collect_metrics(config, LOG) is True
    return captured


def test_metrics_run_messages_meet_the_contract(produced):
    types_seen = {e['data']['data_type'] for e in produced}
    assert types_seen == {'system_metrics', 'user_metrics', 'sessions', 'storage_health'}
    for entry in produced:
        assert check_message(entry) == [], entry['data']
    names = {e['data']['username'] for e in produced if e['data']['data_type'] == 'user_metrics'}
    assert names == {'alice', 'x' * 256, 'uid:4242'}                 # cut to 256 characters
    second = [e for e in produced if e['data']['data_type'] == 'system_metrics'][1]['data']
    assert second['interval_s'] == 300.0 and len(second['disk_busy_max_name']) == 32
    assert len(second['gpus'][0]['uuid']) == 96
    session = next(e['data'] for e in produced if e['data']['data_type'] == 'sessions')
    assert session['sessions'][0]['username'] == 'y' * 256 and len(session['sessions'][0]['client_host']) == 256
    health = next(e['data'] for e in produced if e['data']['data_type'] == 'storage_health')
    assert [len(d['id']) for d in health['disks']] == [7, 42]       # nvme0n1 and sd + 40 a's (within 64)
    assert all(KEY_RE.match(k) for k in health)


@pytest.fixture
def disk_messages(collector, config, monkeypatch, tmp_path):
    """folder_usage and storage_breakdown from two disk runs (the second with the breakdown walker)."""
    deep = tmp_path / 'vol' / ('d' * 200) / ('e' * 200)
    deep.mkdir(parents=True)
    (deep / 'f').write_bytes(b'x' * 10)
    captured = []
    monkeypatch.setattr(collector, '_record_and_sync', lambda c, n, i, entries, log, sync=True: captured.extend(entries) or True)
    config.update({'platform': 'linux', 'volumes': [], 'scan_paths': [str(tmp_path / 'vol') + '/*']})
    for _ in range(2):
        assert collector.collect_disk_usage(config, LOG) is True
    return captured


@posix_only
def test_disk_run_messages_meet_the_contract(disk_messages):
    assert [e['data']['data_type'] for e in disk_messages] == ['folder_usage', 'storage_breakdown'] * 2
    for entry in disk_messages:
        assert check_message(entry) == [], entry['data']
    last = disk_messages[-1]['data']
    assert last['folders'][0]['owners'] is not None and last['_breakdown_unavailable'] == []


def test_check_message_catches_violations():
    base = {'header': {'msg_id': 'a' * 32}, 'data': {'data_type': 'x'}}
    bad = [{'Cpu': 1}, {'cpu': 1, 'CPU': 2}, {'timestamp': 1}, {'payload_x': 1}, {'msg_id': 1},
           {'username': 'u' * 257}, {'big': 'x' * MAX_MESSAGE_BYTES}]
    for extra in bad:
        entry = {'header': base['header'], 'data': {**base['data'], **extra}}
        assert check_message(entry), extra
    assert check_message({'header': {}, 'data': {'data_type': 'x'}}) == ["msg_id None"]
    assert check_message(base) == []


# Appendix A (the shared fixtures of every task): the collector's messages have the same shapes
APPENDIX_A = {
    'sessions': {'_schema', '_unit', '_detail', '_sources', '_collect_seconds', '_list_state', '_list_hash',
                 'clients_total', 'users_total', 'sessions_truncated'},
    'session_row': {'service', 'username', 'client', 'client_host', 'connections', 'since', 'state', 'source'},
    'storage_health': {'_schema', 'trigger', 'overall_status', 'overall_status_code', 'status_reasons', 'sources',
                       'arrays', 'disks', 'volumes', 'events', '_flat_truncated'},
    'disk': {'id', 'model', 'serial', 'size_bytes', 'media', 'bus', 'status', 'health_source', 'smart_passed',
             'temperature_c', 'power_on_hours', 'reallocated_sectors', 'pending_sectors', 'uncorrectable_sectors',
             'read_errors_uncorrected', 'write_errors_uncorrected', 'wear_used_percent', 'remaining_life_percent',
             'bad_sectors', 'io_errors', 'device_state', 'in_standby', 'arrays'},
    'array': {'id', 'kind', 'name', 'level', 'role', 'state', 'status', 'members_total', 'members_active', 'degraded',
              'sync_action', 'sync_percent', 'sync_speed_kbps', 'sync_eta_seconds', 'mismatch_cnt', 'size_bytes',
              'faulty_members', 'members'},
    'volume': {'id', 'fs', 'read_only', 'health', 'status'},
    'storage_breakdown': {'_schema', '_unit', 'reference_time', 'cold_thresholds_days', 'owner_source', 'age_source',
                          'top_owners', 'top_owners_effective', 'owners_truncated', 'folders', 'device',
                          'volume_owners', '_timed_out', '_errors', '_breakdown_unavailable'},
    'folder': {'path', 'folder_owner', 'bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes', 'owners', 'other'},
    'device': {'bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes', 'owners', 'other'},
    'owner': {'owner', 'bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes'},
    'other': {'owner_count', 'bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes'},
}


@posix_only
def test_shapes_match_appendix_a(produced, disk_messages, collector):
    from lib import storage_health
    by_type = {}
    for entry in produced + disk_messages:
        by_type.setdefault(entry['data']['data_type'], []).append(entry['data'])
    for data in by_type['sessions']:
        assert APPENDIX_A['sessions'] <= set(data)
        assert set(data) - APPENDIX_A['sessions'] - {'data_type', 'sessions'} <= {
            k for k in data if k.endswith(('_clients', '_users')) or k == 'rdp_disconnected'}
        for row in data.get('sessions') or []:
            assert set(row) <= APPENDIX_A['session_row'] and {'service', 'connections', 'source'} <= set(row)
    health = by_type['storage_health'][0]
    flat = {k for k in health if re.match(r'(disk|array|volume)_', k)}
    assert set(health) - flat - {'data_type'} == APPENDIX_A['storage_health']
    assert all(set(d) == APPENDIX_A['disk'] for d in health['disks'])
    assert set(storage_health.ARRAY_FIELDS) == APPENDIX_A['array']
    breakdown = by_type['storage_breakdown'][-1]
    assert set(breakdown) - {'data_type'} == APPENDIX_A['storage_breakdown']
    assert set(breakdown['device']) == APPENDIX_A['device']
    for folder in breakdown['folders']:
        assert set(folder) == APPENDIX_A['folder'] and set(folder['other']) == APPENDIX_A['other']
        assert all(set(o) == APPENDIX_A['owner'] for o in folder['owners'])
