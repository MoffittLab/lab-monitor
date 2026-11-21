"""
The new fields and data types (spec §11-§15) on ingest: validation and
sanitising (R8, manager._validate_known_fields / _sanitize_known).

  - a field of a known type with the wrong JSON type, a nested list that is
    not a list or exceeds its hard cap, a bad enum value: rejected
    (dead_letter);
  - a key that is numeric only by its name pattern (<svc>_clients,
    <svc>_users, disk_* / array_* / volume_*) holding a non-number: dropped
    and listed in "_dropped_fields", the message is stored;
  - a bad row inside a nested list: dropped and counted in "_dropped_rows".

Then what the Manager does with them (B4-B6): the central snapshots and the
read endpoints (/api/sessions, /api/storage/health, /api/storage/breakdown),
the per-user usage fields, history units, and the narrow storage_breakdown
layout (exact round trip, history field grammar, retention, migration of a
wide table an older Manager wrote).

The Appendix A payloads are the shared fixtures (verbatim).
"""
import copy
import glob
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import archive_metrics
import data_store
import manager as mgr
from current_manager import run_current
from manager_helpers import ADMIN, COLL_TRITON, READ, auth, base_config, make_msg, metrics_payload, post_queue
from storage_util import format_timestamp

A1_SYSTEM_METRICS = json.loads(r'''
{"data_type":"system_metrics","cpu_percent":23.4,"cpu_percent_unit":"%","interval_s":300.2,"interval_s_unit":"s",
 "cpu_count":32,"cpu_iowait_percent":1.2,"cpu_iowait_percent_unit":"%","cpu_steal_percent":0.0,"cpu_steal_percent_unit":"%",
 "proc_cpu_coverage_percent":96.5,"proc_cpu_coverage_percent_unit":"%",
 "disk_read_bytes_per_s":1250000,"disk_read_bytes_per_s_unit":"B/s","disk_write_bytes_per_s":310000,"disk_write_bytes_per_s_unit":"B/s",
 "disk_read_iops":41.2,"disk_read_iops_unit":"ops/s","disk_write_iops":12.0,"disk_write_iops_unit":"ops/s",
 "disk_await_ms":6.3,"disk_await_ms_unit":"ms","disk_busy_max_percent":17.0,"disk_busy_max_percent_unit":"%","disk_busy_max_name":"sata2",
 "gpu_sample_seconds":300.2,"gpu_sample_seconds_unit":"s","gpu_count":1,
 "gpus":[{"index":0,"name":"NVIDIA RTX A6000","gpu_percent":87,"vram_used_bytes":22548578304,"vram_total_bytes":51527024640,
          "vram_used_formatted":"21.0 GB","vram_total_formatted":"48.0 GB","temperature_c":71,"power_watts":250.1,
          "uuid":"GPU-8f2c0000-1111-2222-3333-444455556666","process_source":"nvml","util_split":"single_user",
          "users":[{"username":"LAB\\alice","vram_bytes":21474836480,"util_percent":87.0,"processes":2}],
          "users_truncated":0,"unattributed_vram_bytes":1073741824,"unattributed_processes":1}]}
''')
A2_USER = json.loads(r'''
{"data_type":"user_metrics","username":"LAB\\alice","cpu_percent":12.5,"ram_bytes":8589934592,"ram_formatted":"8.0 GB",
 "cpu_seconds":1200.4,"cpu_cores":4.0,"interval_s":300.2,"process_count":17,
 "gpu_vram_bytes":21474836480,"gpu_util_percent":87.0,"gpu_busy_seconds":261.2,"gpus_held":1,"gpu_held_seconds":300.2,"gpu_processes":2}
''')
A2_GPU_ONLY_USER = json.loads(r'''
{"data_type":"user_metrics","username":"LAB\\bob","cpu_percent":null,"ram_bytes":null,"ram_formatted":null,
 "gpu_vram_bytes":1073741824,"gpu_util_percent":0.0,"gpu_busy_seconds":0.0,"gpus_held":1,"gpu_held_seconds":300.2,"gpu_processes":1}
''')
A3_SESSIONS_FULL = json.loads(r'''
{"data_type":"sessions","_schema":1,"_unit":"count","_detail":"full",
 "_sources":{"tcp":"ok","utmp":"ok","smb":"not_permitted"},"_collect_seconds":0.04,
 "_list_state":"full","_list_hash":"3f9a1c2b7d4e5f60",
 "clients_total":9,"users_total":1,"smb_clients":7,"nfs_clients":1,"ssh_clients":1,"ssh_users":1,"dsm_clients":2,"rsync_clients":0,
 "sessions_truncated":false,
 "sessions":[{"service":"ssh","username":"jeff","client":"10.11.2.40","connections":1,"since":"2026-11-21T12:35:00Z","source":"utmp"},
             {"service":"smb","client":"10.11.2.17","connections":2,"source":"tcp"}]}
''')
A3_SESSIONS_UNCHANGED = json.loads(r'''
{"data_type":"sessions","_schema":1,"_unit":"count","_detail":"full",
 "_sources":{"tcp":"ok","utmp":"ok","smb":"not_permitted"},"_collect_seconds":0.03,
 "_list_state":"unchanged","_list_hash":"3f9a1c2b7d4e5f60",
 "clients_total":9,"users_total":1,"smb_clients":7,"nfs_clients":1,"ssh_clients":1,"ssh_users":1,"dsm_clients":2,"rsync_clients":0,
 "sessions_truncated":false}
''')
A4_STORAGE_HEALTH = json.loads(r'''
{"data_type":"storage_health","_schema":1,"trigger":"change","overall_status":"warning","overall_status_code":1,
 "status_reasons":["md2 rebuilding 37.5% (ETA 5h 12m)"],
 "sources":{"md":"ok","sysfs":"ok","mounts":"ok","snmp":"off","smart":"not_permitted"},
 "arrays":[{"id":"md2","kind":"md","name":null,"level":"raid5","role":"data","state":"active","status":"warning",
            "members_total":4,"members_active":3,"degraded":1,"sync_action":"recover","sync_percent":37.5,
            "sync_speed_kbps":120000,"sync_eta_seconds":18720,"mismatch_cnt":0,"size_bytes":11995903803392,
            "faulty_members":[],"members":["sata1","sata2","sata4","sata3"]}],
 "disks":[{"id":"sata3","model":"WD80EFZX","serial":null,"size_bytes":8001563222016,"media":"hdd","bus":"sata",
           "status":"ok","health_source":"sysfs","smart_passed":null,"temperature_c":null,"power_on_hours":null,
           "reallocated_sectors":null,"pending_sectors":null,"uncorrectable_sectors":null,
           "read_errors_uncorrected":null,"write_errors_uncorrected":null,"wear_used_percent":null,
           "remaining_life_percent":null,"bad_sectors":null,"io_errors":0,"device_state":"running",
           "in_standby":null,"arrays":["md2"]}],
 "volumes":[{"id":"/volume1","fs":"btrfs","read_only":false,"health":null,"status":"ok"}],
 "events":null,"_flat_truncated":false,
 "disk_sata3_status_code":0,"disk_sata3_io_errors":0,
 "array_md2_status_code":1,"array_md2_degraded":1,"array_md2_sync_percent":37.5,
 "volume_volume1_status_code":0}
''')
A5_STORAGE_BREAKDOWN = json.loads(r'''
{"data_type":"storage_breakdown","_schema":1,"_unit":"bytes","reference_time":"2026-11-21T02:00:03Z",
 "cold_thresholds_days":[365,1095],"owner_source":"posix_uid","age_source":"mtime",
 "top_owners":10,"top_owners_effective":10,"owners_truncated":false,
 "folders":[{"path":"/volume1/Lab","folder_owner":"jeff","bytes":5000,"files":120,"cold_1y_bytes":3000,"cold_3y_bytes":1000,
             "owners":[{"owner":"jeff","bytes":4000,"files":100,"cold_1y_bytes":2500,"cold_3y_bytes":900}],
             "other":{"owner_count":3,"bytes":1000,"files":20,"cold_1y_bytes":500,"cold_3y_bytes":100}}],
 "device":{"bytes":5000,"files":120,"cold_1y_bytes":3000,"cold_3y_bytes":1000,
           "owners":[{"owner":"jeff","bytes":4000,"files":100,"cold_1y_bytes":2500,"cold_3y_bytes":900}],
           "other":{"owner_count":3,"bytes":1000,"files":20,"cold_1y_bytes":500,"cold_3y_bytes":100}},
 "volume_owners":null,"_timed_out":[],"_errors":[],"_breakdown_unavailable":[]}
''')
APPENDIX_A = [A1_SYSTEM_METRICS, A2_USER, A2_GPU_ONLY_USER, A3_SESSIONS_FULL, A3_SESSIONS_UNCHANGED,
              A4_STORAGE_HEALTH, A5_STORAGE_BREAKDOWN]


def _msg(payload, minute=0):
    data = copy.deepcopy(payload)
    return make_msg(data.pop('data_type'), data, ts=f'2026-11-21T12:{minute:02d}:00.000000Z')


def _post(client, payloads):
    r = post_queue(client, [_msg(p, i) for i, p in enumerate(payloads)], token=ADMIN)
    assert r.status_code == 200
    return r.get_json()['results']


def _rows(client, data_type):
    r = client.get(f'/api/data/Triton/{data_type}', headers=auth(READ))
    assert r.status_code == 200
    return r.get_json()['rows']


def test_appendix_a_payloads_are_stored(client):
    assert [r['status'] for r in _post(client, APPENDIX_A)] == ['stored'] * len(APPENDIX_A)
    assert mgr._metrics_db.get_dead_letters(10) == []
    sm = _rows(client, 'system_metrics')[0]
    assert json.loads(sm['gpus']) == A1_SYSTEM_METRICS['gpus']          # nothing dropped
    assert '_dropped_rows' not in sm and '_dropped_fields' not in sm
    health = _rows(client, 'storage_health')[0]
    assert health['array_md2_sync_percent'] == 37.5 and json.loads(health['arrays']) == A4_STORAGE_HEALTH['arrays']
    assert len(_rows(client, 'sessions')) == 2 and len(_rows(client, 'user_metrics')) == 2


def test_sanitize_leaves_old_and_valid_payloads_unchanged():
    old = {'cpu_percent': 1.0, 'gpus': [{'index': 0, 'name': 'x'}], 'gpu_count': 1}
    for table, payload in [('system_metrics', old), ('folder_usage', {'/v': 1, '_unit': 'bytes'})] + \
            [(p['data_type'], p) for p in APPENDIX_A]:
        assert mgr._sanitize_known(table, payload) == payload, table


@pytest.mark.parametrize('payload, why', [
    (dict(A3_SESSIONS_FULL, sessions='ssh jeff'), "'sessions' must be a list"),
    (dict(A3_SESSIONS_FULL, sessions=[{'service': 'ssh'}] * 5001), "'sessions' must be a list of at most 5000"),
    (dict(A3_SESSIONS_FULL, clients_total='9'), "'clients_total' must be a number"),
    (dict(A3_SESSIONS_FULL, users_total=True), "'users_total' must be a number"),
    (dict(A3_SESSIONS_FULL, _list_state='partial'), "'_list_state'"),
    (dict(A3_SESSIONS_FULL, _list_hash='x' * 65), "'_list_hash'"),
    (dict(A3_SESSIONS_FULL, _sources=['tcp']), "'_sources'"),
    (dict(A4_STORAGE_HEALTH, overall_status='degraded'), "'overall_status' must be one of"),
    (dict(A4_STORAGE_HEALTH, overall_status_code='1'), "'overall_status_code' must be a number"),
    (dict(A4_STORAGE_HEALTH, disks={'sata3': {}}), "'disks' must be a list"),
    (dict(A4_STORAGE_HEALTH, volumes=[{'id': 'v', 'status': 'ok'}] * 65), "'volumes' must be a list of at most 64"),
    (dict(A4_STORAGE_HEALTH, status_reasons='md2 rebuilding'), "'status_reasons' must be a list"),
    (dict(A1_SYSTEM_METRICS, interval_s='300'), "'interval_s' must be a number"),
    (dict(A1_SYSTEM_METRICS, disk_busy_max_name=3), "'disk_busy_max_name' must be a string"),
    (dict(A1_SYSTEM_METRICS, gpus={'0': {}}), "'gpus' must be a list"),
    (dict(A1_SYSTEM_METRICS, gpus=[dict(A1_SYSTEM_METRICS['gpus'][0], users='alice')]), "'gpus[].users'"),
    (dict(A1_SYSTEM_METRICS, gpus=[dict(A1_SYSTEM_METRICS['gpus'][0], users=[{'username': 'u'}] * 65)]),
     "'gpus[].users'"),
    (dict(A2_USER, gpu_vram_bytes='20 GB'), "'gpu_vram_bytes' must be a number"),
    (dict(A2_USER, cpu_seconds=[1]), "'cpu_seconds' must be a number"),
    (dict(A5_STORAGE_BREAKDOWN, folders={'/volume1/Lab': {}}), "'folders' must be a list"),
    (dict(A5_STORAGE_BREAKDOWN, folders=[{'path': f'/v/{i}', 'bytes': 1} for i in range(10001)]),
     "'folders' must be a list of at most 10000"),
    (dict(A5_STORAGE_BREAKDOWN, folders=[{'path': f'/v/{i}', 'bytes': 1, 'owners': [{'owner': 'a', 'bytes': 1}] * 9}
                                         for i in range(5001)]), 'storage_breakdown has 50013 rows'),   # + device: total, owner, other
    (dict(A5_STORAGE_BREAKDOWN, device=['jeff']), "'device' must be an object"),
    (dict(A5_STORAGE_BREAKDOWN, folders=[dict(A5_STORAGE_BREAKDOWN['folders'][0], other=[1])]),
     "'folders[].other' must be an object"),
])
def test_wrong_shapes_are_rejected(client, payload, why):
    (res,) = _post(client, [payload])
    assert res['status'] == 'rejected' and why in res['error'], res
    (dl,) = mgr._metrics_db.get_dead_letters(10)
    assert why in dl['reason']


def test_pattern_keys_with_non_numbers_are_dropped_not_rejected(client):
    """A later collector adding e.g. "disk_sata1_model" must not dead-letter every health message (R8, D27)."""
    sess = dict(A3_SESSIONS_UNCHANGED, afp_users='3', nfs_users=None, smb_clients=7, vpn_clients=[1])
    health = dict(A4_STORAGE_HEALTH, disk_sata1_model='WD80EFZX', array_md2_degraded=True,
                  volume_volume1_status_code=0)
    assert [r['status'] for r in _post(client, [sess, health])] == ['stored', 'stored']
    s = _rows(client, 'sessions')[0]
    assert json.loads(s['_dropped_fields']) == ['afp_users', 'vpn_clients']
    assert s['smb_clients'] == 7 and 'afp_users' not in s and 'vpn_clients' not in s
    assert s.get('nfs_users') is None                          # null is a valid "number or null"
    h = _rows(client, 'storage_health')[0]
    assert json.loads(h['_dropped_fields']) == ['array_md2_degraded', 'disk_sata1_model']   # payload order
    assert 'disk_sata1_model' not in h and h['volume_volume1_status_code'] == 0
    # the same names in a type without patterns are ordinary fields
    (res,) = _post(client, [dict(A1_SYSTEM_METRICS, disk_sata1_model='WD')])
    assert res['status'] == 'stored'
    assert _rows(client, 'system_metrics')[0]['disk_sata1_model'] == 'WD'


def test_bad_nested_rows_are_dropped_and_counted(client):
    sessions = dict(A3_SESSIONS_FULL, sessions=A3_SESSIONS_FULL['sessions'] + [
        'not a row', {'service': 'SSH!', 'client': '10.0.0.1'}, {'service': 'ssh', 'username': 'u' * 257},
        {'service': 'smb', 'client': '10.0.0.2', 'connections': 'two'}, {'service': 'ssh', 'connections': float('inf')}])
    health = dict(A4_STORAGE_HEALTH,
                  arrays=A4_STORAGE_HEALTH['arrays'] + [dict(A4_STORAGE_HEALTH['arrays'][0], id='md3', status='rebuilding')],
                  disks=A4_STORAGE_HEALTH['disks'] + [dict(A4_STORAGE_HEALTH['disks'][0], id='x' * 65),
                                                      dict(A4_STORAGE_HEALTH['disks'][0], id='sata4',
                                                           temperature_c=float('nan')), 7],
                  volumes=A4_STORAGE_HEALTH['volumes'] + [{'id': '/volume2', 'status': None}],
                  status_reasons=['r%d ' % i + 'x' * 300 for i in range(12)] + [5])
    gpu = dict(A1_SYSTEM_METRICS['gpus'][0], users=A1_SYSTEM_METRICS['gpus'][0]['users'] + [
        {'username': '', 'vram_bytes': 1}, {'username': 'bob', 'vram_bytes': '1 GB'}, 'carol'])
    system = dict(A1_SYSTEM_METRICS, gpus=[gpu, 'not a gpu'])
    folder = A5_STORAGE_BREAKDOWN['folders'][0]
    breakdown = dict(A5_STORAGE_BREAKDOWN, folders=[
        folder, dict(folder, path='/volume1/Neg', bytes=-1), dict(folder, path=''), dict(folder, path='/v/o', owners='jeff'),
        dict(folder, path='/volume1/Mixed', owners=folder['owners'] + [{'owner': 'x', 'bytes': -5}, {'bytes': 1}],
             other=dict(folder['other'], files=-1))])
    results = _post(client, [sessions, health, system, breakdown])
    assert [r['status'] for r in results] == ['stored'] * 4
    s = _rows(client, 'sessions')[0]
    assert s['_dropped_rows'] == 5 and json.loads(s['sessions']) == A3_SESSIONS_FULL['sessions']
    h = _rows(client, 'storage_health')[0]
    assert h['_dropped_rows'] == 1 + 3 + 1 + 1          # array, disks, volume, non-str reason
    assert [a['id'] for a in json.loads(h['arrays'])] == ['md2']
    assert [d['id'] for d in json.loads(h['disks'])] == ['sata3']
    reasons = json.loads(h['status_reasons'])
    assert len(reasons) == 10 and all(len(r) == 200 for r in reasons) and reasons[0].startswith('r0 ')
    sm = _rows(client, 'system_metrics')[0]
    gpus = json.loads(sm['gpus'])
    assert sm['_dropped_rows'] == 3 and gpus[0]['users'] == A1_SYSTEM_METRICS['gpus'][0]['users']
    assert gpus[1] == 'not a gpu'                       # GPU entries themselves are passed on as sent
    b = _rows(client, 'storage_breakdown')[0]
    folders = b['folders']                              # narrow storage: rebuilt as sent (B5)
    assert [f['path'] for f in folders] == ['/volume1/Lab', '/volume1/Mixed']
    assert folders[1]['owners'] == folder['owners'] and folders[1]['other'] is None
    assert b['_dropped_rows'] == 3 + 2 + 1


def test_gpus_as_stored_json_text_is_accepted(client):
    """Rows read back from a database hold gpus as JSON text; re-sending one must stay a duplicate, not a dead letter."""
    old = {'cpu_percent': 5.0, 'gpus': '[]', 'gpu_count': 0}
    m = make_msg('system_metrics', old, ts='2026-11-21T12:00:00.000000Z')
    r = post_queue(client, [m, m], token=ADMIN).get_json()['results']
    assert [x['status'] for x in r] == ['stored', 'duplicate']


def test_sanitized_payload_is_what_is_stored_and_applied(client):
    """One copy for the history row and the central side effects (here: the user snapshot)."""
    bad_user = dict(A2_USER, username='LAB\\alice')
    (res,) = _post(client, [bad_user])
    assert res['status'] == 'stored'
    path = os.path.join(mgr._config['data_dir'], 'triton', 'data.db')
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute('PRAGMA table_info(user_metrics)')}
    con.close()
    assert {'cpu_seconds', 'gpu_busy_seconds', 'gpus_held'} <= cols


# =========================================================================== #
# Central snapshots and the read endpoints (B4, B6)                           #
# =========================================================================== #

def _at(minute, day=1):
    """A fixed time well in the past (a message from the future does not move a snapshot)."""
    return f'2026-11-{day:02d}T12:{minute:02d}:00.000000Z'


def _send(client, *items, name='Triton', token=ADMIN):
    """items: (payload, timestamp[, msg_id]); returns the statuses."""
    msgs = []
    for item in items:
        data = copy.deepcopy(item[0])
        msgs.append(make_msg(data.pop('data_type'), data, ts=item[1], name=name,
                             msg_id=item[2] if len(item) > 2 else 'auto'))
    r = post_queue(client, msgs, token=token, name=name)
    assert r.status_code == 200, r.get_data(as_text=True)
    return [x['status'] for x in r.get_json()['results']]


def _get(client, path, token=READ):
    return client.get(path, headers=auth(token))


NEW_ENDPOINTS = ('/api/sessions/all', '/api/sessions/Triton', '/api/storage/health/all',
                 '/api/storage/health/Triton', '/api/storage/breakdown/all', '/api/storage/breakdown/Triton',
                 '/api/user_metrics/usage')


def test_snapshots_and_read_endpoints(client):
    payloads = [A1_SYSTEM_METRICS, A2_USER, A2_GPU_ONLY_USER, A3_SESSIONS_FULL, A3_SESSIONS_UNCHANGED,
                A4_STORAGE_HEALTH, A5_STORAGE_BREAKDOWN]
    assert _send(client, *[(p, _at(i)) for i, p in enumerate(payloads)]) == ['stored'] * 7

    # sessions: counts of the latest message, list of the full one, confirmed by the unchanged one
    every = _get(client, '/api/sessions/all').get_json()
    assert list(every) == ['Triton']
    s = every['Triton']
    assert s == _get(client, '/api/sessions/Triton').get_json()
    assert set(s) == {'timestamp', 'list_timestamp', 'list_confirmed_at', 'summary', 'sessions'}
    assert (s['timestamp'], s['list_timestamp'], s['list_confirmed_at']) == (_at(4), _at(3), _at(4))
    assert s['sessions'] == A3_SESSIONS_FULL['sessions']
    want = {k: v for k, v in A3_SESSIONS_UNCHANGED.items() if k != 'data_type'}
    assert s['summary'] == want

    # storage health: the message without its flattened history keys
    h = _get(client, '/api/storage/health/Triton').get_json()
    assert h == _get(client, '/api/storage/health/all').get_json()['Triton']
    assert set(h) == {'timestamp', 'overall_status', 'status_reasons', 'sources', 'arrays', 'disks', 'volumes', 'events'}
    assert h['overall_status'] == 'warning' and h['arrays'] == A4_STORAGE_HEALTH['arrays']
    assert h['disks'] == A4_STORAGE_HEALTH['disks'] and h['events'] is None
    con = sqlite3.connect(os.path.join(mgr._config['data_dir'], 'metrics.db'))
    summary = json.loads(con.execute('SELECT summary_json FROM device_storage_health').fetchone()[0])
    con.close()
    assert not [k for k in summary if k.startswith(('disk_', 'array_', 'volume_'))]
    assert summary['overall_status_code'] == 1 and summary['trigger'] == 'change'

    # storage breakdown: the summary, and the latest run with every owner row
    b = _get(client, '/api/storage/breakdown/all').get_json()['Triton']
    assert set(b) == {'timestamp', 'owner_source', 'reference_time', 'cold_thresholds_days', 'device', 'folders',
                      'folders_truncated', 'volume_owners'}
    assert b['folders'] == [{'path': '/volume1/Lab', 'folder_owner': 'jeff', 'bytes': 5000, 'files': 120,
                             'cold_1y_bytes': 3000, 'cold_3y_bytes': 1000, 'top_owner': 'jeff', 'top_owner_bytes': 4000}]
    assert b['device'] == A5_STORAGE_BREAKDOWN['device'] and b['folders_truncated'] is False
    assert b['cold_thresholds_days'] == [365, 1095] and b['owner_source'] == 'posix_uid'
    run = _get(client, '/api/storage/breakdown/Triton').get_json()
    assert {k: v for k, v in run.items() if k not in ('system', 'id', 'msg_id', 'timestamp', 'device_type')} == \
        {k: v for k, v in A5_STORAGE_BREAKDOWN.items() if k != 'data_type'}
    assert run['system'] == 'Triton' and run['timestamp'] == _at(6)
    one = _get(client, '/api/storage/breakdown/Triton?folder=/volume1/Lab').get_json()
    assert one['folders'] == A5_STORAGE_BREAKDOWN['folders'] and one['device'] == A5_STORAGE_BREAKDOWN['device']
    assert _get(client, '/api/storage/breakdown/Triton?folder=/volume1/Other').status_code == 404
    for bad in ('', 'x' * 4097):
        assert _get(client, '/api/storage/breakdown/Triton?folder=' + bad).status_code == 400

    # per-user usage fields: present when measured, left out when not
    users = {u['username']: u for u in _get(client, '/api/user_metrics/all').get_json()['Triton']}
    alice, bob = users['LAB\\alice'], users['LAB\\bob']
    for k in ('cpu_seconds', 'cpu_cores', 'interval_s', 'process_count', 'gpu_vram_bytes', 'gpu_util_percent',
              'gpu_busy_seconds', 'gpus_held', 'gpu_held_seconds', 'gpu_processes'):
        assert alice[k] == A2_USER[k], k
    assert bob['gpus_held'] == 1 and bob['cpu_percent'] is None and 'cpu_seconds' not in bob
    assert _get(client, '/api/user_metrics/Triton').get_json() == _get(client, '/api/user_metrics/all').get_json()['Triton']
    (hist,) = _get(client, '/api/user_metrics/history/Triton/LAB%5Calice').get_json()
    assert hist['cpu_seconds'] == 1200.4 and hist['gpu_busy_seconds'] == 261.2

    # system_metrics: the counter-based fields of the latest message
    m = _get(client, '/api/metrics/all').get_json()['Triton']
    for k in ('interval_s', 'cpu_count', 'cpu_iowait_percent', 'disk_read_bytes_per_s', 'disk_await_ms',
              'disk_busy_max_percent', 'gpu_sample_seconds', 'disk_busy_max_name'):
        assert m[k] == A1_SYSTEM_METRICS[k], k
    assert m['units']['disk_read_bytes_per_s_unit'] == 'B/s'
    assert m['gpus'][0]['users'] == A1_SYSTEM_METRICS['gpus'][0]['users']


def test_read_endpoints_status_codes_and_scopes(client):
    assert _send(client, (dict(metrics_payload(), data_type='system_metrics'), _at(0))) == ['stored']
    for path in ('/api/sessions/Triton', '/api/storage/health/Triton', '/api/storage/breakdown/Triton'):
        assert _get(client, path).status_code == 404                     # known system, nothing reported yet
        assert _get(client, path.replace('Triton', 'Nobody')).status_code == 404
        assert _get(client, path.replace('Triton', 'bad%20name!')).status_code == 400
    for path in ('/api/sessions/all', '/api/storage/health/all', '/api/storage/breakdown/all'):
        assert _get(client, path).get_json() == {}
    for path in NEW_ENDPOINTS:
        assert _get(client, path, token=COLL_TRITON).status_code == 403, path
        assert client.get(path).status_code == 401, path
    # read endpoints never create anything for an unknown system
    assert not os.path.exists(os.path.join(mgr._config['data_dir'], 'nobody'))
    assert _get(client, '/api/user_metrics/usage?device=Nobody').status_code == 404
    assert not os.path.exists(os.path.join(mgr._config['data_dir'], 'nobody'))


def test_session_list_full_unchanged_and_none(client):
    full, unchanged = A3_SESSIONS_FULL, A3_SESSIONS_UNCHANGED

    def snap():
        return _get(client, '/api/sessions/Triton').get_json()

    assert _send(client, (full, _at(0))) == ['stored']
    s = snap()
    assert (s['list_timestamp'], s['list_confirmed_at'], len(s['sessions'])) == (_at(0), _at(0), 2)
    # unchanged with the stored list's hash: the list is confirmed
    assert _send(client, (dict(unchanged, clients_total=8), _at(5))) == ['stored']
    s = snap()
    assert (s['timestamp'], s['list_timestamp'], s['list_confirmed_at']) == (_at(5), _at(0), _at(5))
    assert s['summary']['clients_total'] == 8 and len(s['sessions']) == 2
    # unchanged with another hash (a list this Manager never got): kept, not confirmed
    assert _send(client, (dict(unchanged, _list_hash='0000000000000000'), _at(10))) == ['stored']
    s = snap()
    assert (s['timestamp'], s['list_timestamp'], s['list_confirmed_at']) == (_at(10), _at(0), _at(5))
    assert len(s['sessions']) == 2
    # a new full list replaces it
    rows = [{'service': 'smb', 'client': '10.11.2.99', 'connections': 1, 'source': 'tcp'}]
    assert _send(client, (dict(full, sessions=rows, _list_hash='1111111111111111'), _at(15))) == ['stored']
    s = snap()
    assert s['sessions'] == rows and (s['list_timestamp'], s['list_confirmed_at']) == (_at(15), _at(15))
    # counts only ("none"): the list is cleared
    none = {k: v for k, v in unchanged.items() if k != '_list_hash'}
    assert _send(client, (dict(none, _list_state='none', _detail='counts', _list_hash=None), _at(20))) == ['stored']
    s = snap()
    assert s['sessions'] == [] and s['list_timestamp'] is None and s['list_confirmed_at'] is None
    assert s['summary']['_detail'] == 'counts' and s['summary']['smb_clients'] == 7


def test_older_and_duplicate_messages_never_roll_snapshots_back(client):
    newer = [(A3_SESSIONS_FULL, _at(30), 'sess-30'), (A4_STORAGE_HEALTH, _at(30), 'hlth-30'),
             (A5_STORAGE_BREAKDOWN, _at(30), 'brk-30'), (A2_USER, _at(30), 'user-30')]
    assert _send(client, *newer) == ['stored'] * 4
    before = {p: _get(client, p).get_json() for p in ('/api/sessions/all', '/api/storage/health/all',
                                                       '/api/storage/breakdown/all', '/api/user_metrics/all')}
    folder = dict(A5_STORAGE_BREAKDOWN['folders'][0], path='/volume1/Old', bytes=1)
    older = [(dict(A3_SESSIONS_FULL, sessions=[], clients_total=0), _at(10)),
             (dict(A4_STORAGE_HEALTH, overall_status='ok', overall_status_code=0), _at(10)),
             (dict(A5_STORAGE_BREAKDOWN, folders=[folder]), _at(10)),
             (dict(A2_USER, cpu_seconds=1.0, gpus_held=0), _at(10))]
    assert _send(client, *older) == ['stored'] * 4                     # history rows, no snapshot change
    # the same msg_ids again with other content: duplicates, nothing applied
    changed = [(dict(p, clients_total=1) if p is A3_SESSIONS_FULL else p, _at(40), mid) for p, _, mid in newer]
    assert _send(client, *changed) == ['duplicate'] * 4
    for path, want in before.items():
        assert _get(client, path).get_json() == want, path
    # a newer message without the GPU fields clears them in the user snapshot (not measured now)
    cpu_only = {k: v for k, v in A2_USER.items() if not k.startswith('gpu')}
    assert _send(client, (cpu_only, _at(50))) == ['stored']
    alice = _get(client, '/api/user_metrics/all').get_json()['Triton'][0]
    assert alice['cpu_seconds'] == 1200.4 and not [k for k in alice if k.startswith('gpu')]


def test_delete_system_clears_the_new_snapshots(client):
    assert _send(client, (A3_SESSIONS_FULL, _at(0)), (A4_STORAGE_HEALTH, _at(1)), (A5_STORAGE_BREAKDOWN, _at(2)),
                 (A2_USER, _at(3))) == ['stored'] * 4
    assert _get(client, '/api/user_metrics/usage?window=30d').get_json()['devices']   # cached ...
    r = client.delete('/api/system/Triton', headers=auth(ADMIN))
    assert r.status_code == 200 and r.get_json()['central_db_rows_cleared'] is True
    for path in ('/api/sessions/all', '/api/storage/health/all', '/api/storage/breakdown/all'):
        assert _get(client, path).get_json() == {}, path
    assert _get(client, '/api/user_metrics/usage?window=30d').get_json()['devices'] == {}   # ... and dropped
    con = sqlite3.connect(os.path.join(mgr._config['data_dir'], 'metrics.db'))
    try:
        for t in ('device_session_snapshot', 'device_storage_health', 'device_storage_breakdown'):
            assert con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] == 0, t
    finally:
        con.close()


def test_snapshots_left_by_a_downgraded_managers_delete_are_not_served_again(make_client, data_dir):
    client = make_client()
    assert _send(client, (A3_SESSIONS_FULL, _at(0)), (A4_STORAGE_HEALTH, _at(0)),
                 (A5_STORAGE_BREAKDOWN, _at(0))) == ['stored'] * 3
    mgr._close_stores()
    # CURRENT (after a downgrade) deletes the system: it does not know the three snapshot tables
    out = run_current(base_config(data_dir), ADMIN, deletes=['/api/system/Triton'])
    assert out['deletes']['/api/system/Triton']['status'] == 200
    con = sqlite3.connect(os.path.join(data_dir, 'metrics.db'))
    left = [con.execute(f"SELECT COUNT(*) FROM {t} WHERE name = 'Triton'").fetchone()[0]
            for t in ('device_session_snapshot', 'device_storage_health', 'device_storage_breakdown')]
    con.close()
    assert left == [1, 1, 1]
    # back on NEW, the device registers anew: the deleted device's snapshots are gone
    client = make_client()
    assert _send(client, ({'data_type': 'system_metrics', **metrics_payload()}, _at(5))) == ['stored']
    assert _get(client, '/api/sessions/Triton').status_code == 404
    assert _get(client, '/api/storage/health/Triton').status_code == 404
    assert 'Triton' not in _get(client, '/api/storage/breakdown/all').get_json()
    assert _send(client, (A3_SESSIONS_FULL, _at(6))) == ['stored']
    assert _get(client, '/api/sessions/Triton').get_json()['timestamp'] == _at(6)


def test_history_units_of_the_new_types(client):
    health = dict(A4_STORAGE_HEALTH, disk_sata1_temperature_c=41, disk_sata1_power_on_hours=2100,
                  disk_sata1_reallocated_sectors=0)
    assert _send(client, (A3_SESSIONS_FULL, _at(0)), (health, _at(1)), (A2_USER, _at(2))) == ['stored'] * 3
    want = {'sessions/smb_clients': 'count', 'sessions/clients_total': 'count',
            'storage_health/disk_sata1_temperature_c': '\u00b0C', 'storage_health/array_md2_sync_percent': '%',
            'storage_health/disk_sata1_power_on_hours': 'h', 'storage_health/disk_sata1_reallocated_sectors': 'count',
            'storage_health/array_md2_degraded': 'count', 'storage_health/volume_volume1_status_code': 'count',
            'user_metrics/cpu_seconds': 's', 'user_metrics/cpu_percent': '%', 'user_metrics/cpu_cores': 'cores',
            'user_metrics/gpu_vram_bytes': 'bytes', 'user_metrics/process_count': None}
    for path, unit in want.items():
        h = _get(client, f'/api/history/Triton/{path}').get_json()
        assert h['unit'] == unit and h['data'], (path, h)


# =========================================================================== #
# storage_breakdown: narrow storage (B5)                                      #
# =========================================================================== #

def _sb_db(client):
    return os.path.join(mgr._config['data_dir'], 'triton', 'data.db')


def _sb_rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT scope, kind, COUNT(*) FROM storage_breakdown_rows GROUP BY 1, 2').fetchall()
    finally:
        con.close()


def _sent(payload):
    return {k: v for k, v in payload.items() if k != 'data_type'}


def _stored(row):
    return {k: v for k, v in row.items() if k not in ('id', 'msg_id', 'timestamp', 'device_type')}


ODD_BREAKDOWN = dict(A5_STORAGE_BREAKDOWN, owner_source='none', volume_owners={'E:': {'quota': None}}, folders=[
    # owners not collected / collected and none / absent; other null / absent; a key a later collector adds
    dict(A5_STORAGE_BREAKDOWN['folders'][0], path='/volume1/Lab/owner/x', owners=None, other=None),
    {'path': 'E:\\Users\\jdoe', 'folder_owner': None, 'bytes': 10, 'files': 2, 'cold_1y_bytes': None,
     'cold_3y_bytes': None, 'owners': [], 'dirs': 4},
    {'path': '/volume1/Lab/owner', 'bytes': 7, 'files': 1,
     'owners': [{'owner': 'uid:1001', 'bytes': 7, 'files': 1, 'newest': '2026-01-01'},
                {'owner': 'LAB\\alice', 'bytes': 0, 'files': 0, 'cold_1y_bytes': 0}],
     'other': {'owner_count': 0, 'bytes': 0, 'files': 0, 'note': 'x'}}],
    device=None)


def test_storage_breakdown_round_trip_is_exact(client):
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(0)), (ODD_BREAKDOWN, _at(1))) == ['stored', 'stored']
    rows = _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows']
    assert [_stored(r) for r in rows] == [_sent(ODD_BREAKDOWN), _sent(A5_STORAGE_BREAKDOWN)]
    # stored narrow: no wide table, one row per total / owner / other
    path = _sb_db(client)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert 'storage_breakdown' not in tables and {'storage_breakdown_msgs', 'storage_breakdown_rows'} <= tables
    assert sorted(_sb_rows(path)) == [('device', 'other', 1), ('device', 'owner', 1), ('device', 'total', 1),
                                      ('folder', 'other', 2), ('folder', 'owner', 3), ('folder', 'total', 4)]
    assert _get(client, '/api/data/Triton').get_json()['data_types'] == ['storage_breakdown']
    # a window query and the summary of a run without device / owners
    win = _get(client, f'/api/data/Triton/storage_breakdown?start={_at(1)}&end={_at(2)}').get_json()['rows']
    assert [_stored(r) for r in win] == [_sent(ODD_BREAKDOWN)]
    b = _get(client, '/api/storage/breakdown/all').get_json()['Triton']
    assert b['device'] is None and b['volume_owners'] == {'E:': {'quota': None}}
    assert [f['top_owner'] for f in b['folders']] == [None, None, 'uid:1001']    # by bytes: 5000, 10, 7


def test_storage_breakdown_history_field_grammar(client):
    folder = A5_STORAGE_BREAKDOWN['folders'][0]
    runs = []
    for i in range(3):
        f = [dict(folder, bytes=5000 + i),
             dict(folder, path='/volume1/Lab/owner/jeff', bytes=10 + i),     # "/owner/" inside a path
             dict(folder, path='E:\\Users\\jdoe', bytes=20 + i, owners=[{'owner': 'LAB\\alice', 'bytes': 3, 'files': i}])]
        device = dict(A5_STORAGE_BREAKDOWN['device'], owners=[{'owner': 'jeff', 'bytes': 4000 + i, 'files': 100},
                                                              {'owner': 'LAB\\alice', 'bytes': 1, 'files': i}])
        runs.append((dict(A5_STORAGE_BREAKDOWN, folders=f, device=device, top_owners_effective=10 - i), _at(i)))
    assert _send(client, *runs) == ['stored'] * 3

    def series(field):
        h = _get(client, f'/api/history/Triton/storage_breakdown/{field}').get_json()
        return h['field'], h['unit'], [p['value'] for p in h['data']]

    assert series('owner/jeff/bytes') == ('owner/jeff/bytes', 'bytes', [4000.0, 4001.0, 4002.0])
    assert series('owner/LAB%5Calice/files') == ('owner/LAB\\alice/files', 'count', [0.0, 1.0, 2.0])
    assert series('folder//volume1/Lab/bytes')[2] == [5000.0, 5001.0, 5002.0]
    # the leading '/' a path loses in URL routing is restored
    assert series('folder/volume1/Lab/cold_1y_bytes') == ('folder//volume1/Lab/cold_1y_bytes', 'bytes',
                                                         [3000.0] * 3)
    assert series('folder//volume1/Lab/owner/jeff/bytes')[2] == [10.0, 11.0, 12.0]
    assert series('folder%2F%2Fvolume1%2FLab%2Fowner%2Fjeff%2Fbytes')[2] == [10.0, 11.0, 12.0]
    assert series('folder/E:%5CUsers%5Cjdoe/bytes') == ('folder/E:\\Users\\jdoe/bytes', 'bytes', [20.0, 21.0, 22.0])
    # a numeric top-level key of the runs; anything else has no points
    assert series('top_owners_effective')[2] == [10.0, 9.0, 8.0]
    assert series('owner/nobody/bytes')[2] == [] and series('folders')[2] == [] and series('nope/x')[2] == []


def test_storage_breakdown_retention_archives_the_nested_runs(make_client, data_dir):
    client = make_client()
    now = datetime.now(timezone.utc)
    old = [(A5_STORAGE_BREAKDOWN, format_timestamp(now.replace(hour=12) - timedelta(days=400, minutes=i)))
           for i in range(archive_metrics.BATCH_BY_TYPE['storage_breakdown'] + 5)]       # two batches
    new_ts = format_timestamp(now - timedelta(days=1))
    assert _send(client, *old, (ODD_BREAKDOWN, new_ts)) == ['stored'] * (len(old) + 1)
    result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365,
                                                   logging.getLogger('test-retention'))
    assert result['archived'] == {'triton/storage_breakdown': len(old)}
    (archive,) = glob.glob(os.path.join(data_dir, 'triton', 'archive', '*-storage_breakdown.jsonl'))
    with open(archive, encoding='utf-8') as f:
        lines = [json.loads(x) for x in f]
    assert len(lines) == len(old) and all(_stored(line) == _sent(A5_STORAGE_BREAKDOWN) for line in lines)
    rows = _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows']
    assert [_stored(r) for r in rows] == [_sent(ODD_BREAKDOWN)]
    assert sorted(_sb_rows(_sb_db(client))) == [('folder', 'other', 1), ('folder', 'owner', 2), ('folder', 'total', 3)]
    # the summary of the latest run is unaffected
    assert _get(client, '/api/storage/breakdown/all').get_json()['Triton']['timestamp'] == new_ts


def _wide_breakdown_by_current(data_dir, runs):
    """A wide storage_breakdown table as the CURRENT (fb6498c) Manager stores a newer collector's runs."""
    msgs = []
    for payload, ts, mid in runs:
        data = copy.deepcopy(payload)
        msgs.append(make_msg(data.pop('data_type'), data, ts=ts, msg_id=mid))
    out = run_current(base_config(data_dir), ADMIN,
                      posts=[{'queue_id': 'Triton-current01', 'name': 'Triton', 'id': 'id-triton', 'messages': msgs}],
                      gets=['/api/data/Triton/storage_breakdown'])
    assert out['posts'][0]['json']['counts']['stored'] == len(runs)
    return out['gets']['/api/data/Triton/storage_breakdown']['json']['rows']


def _backups(path):
    return sorted(glob.glob(glob.escape(path) + '.pre-upgrade-*.bak'))


def _tables(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_failed_storage_breakdown_migration_keeps_the_wide_rows_served(make_client, data_dir, monkeypatch):
    old_rows = _wide_breakdown_by_current(data_dir, [(A5_STORAGE_BREAKDOWN, _at(0), 'b' * 32)])
    assert isinstance(old_rows[0]['folders'], str)                      # CURRENT: JSON text in a wide column
    db = os.path.join(data_dir, 'triton', 'data.db')

    def fail(*a, **kw):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(data_store.TypedDataStore, '_migrate_wide_storage_breakdown', fail)
    client = make_client()
    (bak,) = _backups(db)                                               # taken before the attempt
    assert 'storage_breakdown' in _tables(db) and 'storage_breakdown_msgs' not in _tables(db)
    # the old rows are served as before; a new run waits on its collector, other data does not
    assert _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows'] == old_rows
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(5))) == ['error']
    assert _send(client, (A3_SESSIONS_FULL, _at(5))) == ['stored']
    assert 'storage_breakdown_msgs' not in _tables(db)
    # a restart while it still fails takes no second copy
    client = make_client()
    assert _backups(db) == [bak]

    monkeypatch.undo()
    mgr._data_store._get_db('Triton').breakdown_attempted -= data_store.UPGRADE_RETRY_SECONDS
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(5))) == ['stored']
    assert {'storage_breakdown_legacy', 'storage_breakdown_msgs'} <= _tables(db) and 'storage_breakdown' not in _tables(db)
    rows = _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows']
    assert [_stored(r) for r in rows] == [_sent(A5_STORAGE_BREAKDOWN)] * 2
    assert [r['msg_id'] for r in rows][1] == 'b' * 32 and rows[1]['id'] == old_rows[0]['id']
    # the old run's msg_id is a duplicate now, as before the migration
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(0), 'b' * 32)) == ['duplicate']
    assert _backups(db) == [bak]


def test_per_user_usage_fields_a_downgraded_manager_left_behind_are_not_served(make_client, data_dir):
    """NEW -> CURRENT -> NEW: CURRENT updates a user's CPU / RAM row but not the usage columns."""
    client = make_client()
    assert _send(client, (A2_USER, _at(0)), (A1_SYSTEM_METRICS, _at(0))) == ['stored'] * 2
    mgr._close_stores()
    idle = {'username': 'LAB\\alice', 'cpu_percent': 0.5, 'ram_bytes': 500, 'ram_formatted': '500 B'}
    out = run_current(base_config(data_dir), ADMIN, posts=[
        {'queue_id': 'Triton-current03', 'name': 'Triton', 'id': 'id-triton',
         'messages': [make_msg('user_metrics', idle, ts=_at(5)), make_msg('system_metrics', metrics_payload(), ts=_at(5))]}])
    assert out['posts'][0]['json']['counts']['stored'] == 2

    client = make_client()
    expected = [dict(idle, timestamp=_at(5))]
    assert _get(client, '/api/user_metrics/Triton').get_json() == expected
    assert _get(client, '/api/user_metrics/all').get_json()['Triton'] == expected
    # the reading NEW stored keeps its usage fields in the history
    hist = _get(client, '/api/user_metrics/history/Triton/LAB%5Calice').get_json()
    assert [(h['timestamp'], h.get('cpu_cores')) for h in hist] == [(_at(5), None), (_at(0), 4.0)]
    # the next reading NEW stores brings them back
    assert _send(client, (dict(A2_USER, cpu_cores=2.0), _at(6))) == ['stored']
    (alice,) = _get(client, '/api/user_metrics/Triton').get_json()
    assert alice['cpu_cores'] == 2.0 and alice['gpus_held'] == 1 and alice['timestamp'] == _at(6)
    assert 'usage_timestamp' not in alice


def test_a_second_breakdown_migration_skips_runs_already_stored(make_client, data_dir, caplog):
    """NEW -> CURRENT -> NEW: CURRENT stored again a run NEW had (lost ACK, archive replay); no UNIQUE failure."""
    client = make_client()
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(0), 'a' * 32), (A5_STORAGE_BREAKDOWN, _at(1), 'b' * 32),
                 (A5_STORAGE_BREAKDOWN, _at(2), 'c' * 32)) == ['stored'] * 3
    mgr._close_stores()
    db = os.path.join(data_dir, 'triton', 'data.db')
    con = sqlite3.connect(db)
    with con:   # run b as if migrated from an OLD Manager's wide table (it dropped msg_ids)
        con.execute("UPDATE storage_breakdown_msgs SET msg_id = NULL WHERE msg_id = ?", ('b' * 32,))
    con.close()
    # CURRENT stores a and b again (with their msg_ids), and a new run d
    _wide_breakdown_by_current(data_dir, [(A5_STORAGE_BREAKDOWN, _at(0), 'a' * 32),
                                          (A5_STORAGE_BREAKDOWN, _at(1), 'b' * 32),
                                          (A5_STORAGE_BREAKDOWN, _at(3), 'd' * 32)])
    with caplog.at_level(logging.WARNING):
        client = make_client()
    assert len(_backups(db)) == 1
    assert 'storage_breakdown' not in _tables(db) and 'storage_breakdown_legacy' in _tables(db)
    assert '2 already stored here' in caplog.text and 'IntegrityError' not in caplog.text
    rows = _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows']
    assert [(r['timestamp'], r['msg_id']) for r in rows] == [
        (_at(3), 'd' * 32), (_at(2), 'c' * 32), (_at(1), None), (_at(0), 'a' * 32)]
    con = sqlite3.connect(db)
    try:   # every row CURRENT stored stays in the renamed table
        assert con.execute('SELECT COUNT(*) FROM storage_breakdown_legacy').fetchone()[0] == 3
    finally:
        con.close()
    # new runs are stored, re-sends are duplicates
    assert _send(client, (A5_STORAGE_BREAKDOWN, _at(4), 'e' * 32), (A5_STORAGE_BREAKDOWN, _at(3), 'd' * 32)) == [
        'stored', 'duplicate']
    assert _get(client, '/api/storage/breakdown/Triton').get_json()['timestamp'] == _at(4)


def test_breakdown_run_stored_rules(tmp_path):
    """_breakdown_run_stored: the same msg_id, or a run stored before the copy at the same timestamp."""
    con = sqlite3.connect(str(tmp_path / 'x.db'))
    con.row_factory = sqlite3.Row
    data_store.TypedDataStore._ensure_breakdown_tables(con)
    for mid, ts in (('a' * 32, _at(0)), (None, _at(1)), ('c' * 32, _at(2))):
        con.execute('INSERT INTO storage_breakdown_msgs (msg_id, timestamp, device_type, meta_json) '
                    "VALUES (?, ?, 'synology', '{}')", (mid, ts))
    stored = data_store.TypedDataStore._breakdown_run_stored

    def is_stored(mid, ts, has_msg_id=True, before=3):
        return stored(con, {'msg_id': mid, 'timestamp': ts}, has_msg_id, before)
    assert is_stored('a' * 32, _at(9))                       # its msg_id
    assert is_stored('x' * 32, _at(1))                       # a pre-upgrade run (msg_id NULL) at its time
    assert is_stored('x' * 32, '2026-11-01T12:01:00Z')       # in another form of that time
    assert not is_stored('x' * 32, _at(2))                   # another message at the time of one with an id
    assert is_stored(None, _at(2)) and is_stored(None, _at(1))      # no msg_id: any run at its time
    assert is_stored('x' * 32, _at(1), has_msg_id=False)     # a table without msg_id: the row has none
    assert not is_stored(None, _at(1), before=1)             # only runs stored before this copy
    assert not is_stored(None, None) and not is_stored(None, 'garbage')
    con.close()


def test_retention_skips_storage_breakdown_until_it_is_migrated(make_client, data_dir, monkeypatch, caplog):
    # CURRENT stored a newer collector's runs (wide storage_breakdown) and metrics, all old enough to prune
    sb = copy.deepcopy(A5_STORAGE_BREAKDOWN)
    sb.pop('data_type')
    triton = [make_msg('storage_breakdown', sb, ts=_at(0)), make_msg('system_metrics', metrics_payload(), ts=_at(1))]
    zeta = [make_msg('system_metrics', metrics_payload(), ts=_at(1), name='Zeta')]
    out = run_current(base_config(data_dir), ADMIN, posts=[
        {'queue_id': 'Triton-current02', 'name': 'Triton', 'id': 'id-triton', 'messages': triton},
        {'queue_id': 'Zeta-current0002', 'name': 'Zeta', 'id': 'id-zeta', 'messages': zeta}])
    assert [p['json']['stored'] for p in out['posts']] == [2, 1]

    def fail(*a, **kw):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(data_store.TypedDataStore, '_migrate_wide_storage_breakdown', fail)
    client = make_client()
    log = logging.getLogger('test-retention')
    with caplog.at_level(logging.WARNING):
        first = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 10, log)
        again = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 10, log)
    # the other tables and systems are pruned; the wide rows stay, are served, and are not archived
    assert first['archived'] == {'triton/system_metrics': 1, 'zeta/system_metrics': 1} and again['archived'] == {}
    assert sum('triton/storage_breakdown: skipped' in r.getMessage() for r in caplog.records) == 2
    assert os.listdir(os.path.join(data_dir, 'triton', 'archive')) == ['2026-11-system_metrics.jsonl']
    assert len(_get(client, '/api/data/Triton/storage_breakdown').get_json()['rows']) == 1
    # once the migration succeeds (retried here when due), the run is pruned like any other
    monkeypatch.undo()
    mgr._data_store._get_db('Triton').breakdown_attempted -= data_store.UPGRADE_RETRY_SECONDS
    result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 10, log)
    assert result['archived'] == {'triton/storage_breakdown': 1}
    assert _get(client, '/api/data/Triton/storage_breakdown').get_json()['rows'] == []
