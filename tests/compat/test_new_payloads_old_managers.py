"""
B11, the R5 gate: what a NEW collector sends (spec Appendix A) against the
Managers already deployed, and the NEW Manager on what they stored.

  - The OLD Manager (f086e7e: the old_code fixture) and the CURRENT one
    (fb6498c: tests/manager/current_manager.py) store every message of a NEW
    collector's batch: OLD answers stored == len(batch) (otherwise the
    collector would re-send the whole batch on every run), CURRENT "stored"
    for each message.  The shapes obey R1 / R5 (key names, key count, size).
  - A NEW Manager then opening those data dirs takes one pre-upgrade backup
    per database it alters, migrates the wide storage_breakdown table (kept
    as storage_breakdown_legacy), keeps every stored row readable, backfills
    rollups equal to a brute-force GROUP BY, and serves the new endpoints once
    the next live message arrives (central snapshots are not backfilled).
  - Downgrade gate: a data_dir the NEW Manager wrote (rollup tables, narrow
    storage_breakdown, the new central tables and columns), opened by
    CURRENT, takes no backup, creates no file and lists none of those tables;
    what CURRENT stores meanwhile is picked up by NEW afterwards.

Each deployed Manager runs from its own extracted code in a subprocess
(the modules share names with the NEW ones) and is driven through its Flask
routes; the NEW Manager runs in this process.
"""
import copy
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (REPO / 'manager', REPO / 'tests' / 'manager'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import manager as mgr  # noqa: E402
import rollups  # noqa: E402
from compat_helpers import OLD_TOKEN  # noqa: E402
from current_manager import current_manager_dir  # noqa: E402
from manager_helpers import ADMIN, READ, auth, base_config, make_msg  # noqa: E402
from storage_util import format_timestamp  # noqa: E402
from test_manager_new_types import APPENDIX_A  # noqa: E402
from test_manager_rollups import assert_rollups_match  # noqa: E402

NEW_TYPES = ('system_metrics', 'user_metrics', 'sessions', 'storage_health', 'storage_breakdown')
KEY_RE = re.compile(r'_?[a-z][a-z0-9_]{0,62}')
INTERNAL_KEYS = {'id', 'timestamp', 'device_type', 'msg_id', 'data_type'}

# stdin: {"config", "token", "posts": [queue bodies], "gets": [paths]}; stdout: {"posts", "gets"}
_RUNNER = r'''
import json, logging, sys
sys.path.insert(0, sys.argv[1])
import manager as m
job = json.load(sys.stdin)
logging.basicConfig(level=logging.WARNING)
m.init_app(job["config"], logging.getLogger("deployed-manager"))
client = m.app.test_client()
headers = {"Authorization": "Bearer " + job["token"]}
out = {"posts": [], "gets": {}}
for body in job.get("posts", []):
    r = client.post("/api/data/queue", json=body, headers=headers)
    out["posts"].append({"status": r.status_code, "json": r.get_json(silent=True)})
for path in job.get("gets", []):
    r = client.get(path, headers=headers)
    out["gets"][path] = {"status": r.status_code, "json": r.get_json(silent=True)}
for store in (getattr(m, "_data_store", None), getattr(m, "_metrics_db", None)):
    if store is not None:
        store.close()
sys.stdout.write(json.dumps(out))
'''


def run_deployed(code_dir, config, token, posts=(), gets=()):
    proc = subprocess.run(
        [sys.executable, '-c', _RUNNER, str(code_dir)],
        input=json.dumps({'config': config, 'token': token, 'posts': list(posts), 'gets': list(gets)}),
        capture_output=True, text=True, timeout=300, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout)


@pytest.fixture(params=['old', 'current'])
def deployed(request, old_code):
    """The Manager a NEW collector may meet: its code, its config and token."""
    if request.param == 'old':
        return {'name': 'old', 'code': old_code / 'manager', 'token': OLD_TOKEN,
                'config': lambda d: {'data_dir': d, 'auth_tokens': [OLD_TOKEN]}}
    return {'name': 'current', 'code': current_manager_dir(), 'token': ADMIN, 'config': base_config}


# --------------------------------------------------------------------------- #
# A NEW collector's runs                                                      #
# --------------------------------------------------------------------------- #

T0 = (datetime.now(timezone.utc) - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)


def collector_run(hour, full_list=True, breakdown=True):
    """One metrics run (and the daily disk run) of a NEW collector, as queue messages with msg_ids."""
    ts = format_timestamp(T0 + timedelta(hours=hour, minutes=7))
    msgs = []
    for payload in APPENDIX_A:
        dt = payload['data_type']
        if dt == 'storage_breakdown' and not breakdown:
            continue
        if dt == 'sessions' and (payload['_list_state'] == 'full') != full_list:
            continue
        data = copy.deepcopy(payload)
        if dt == 'system_metrics':
            data['cpu_percent'] = 20.0 + hour
        elif dt == 'user_metrics' and data.get('cpu_seconds') is not None:
            data['cpu_seconds'] = 1000.0 + hour
        elif dt == 'sessions':
            data['smb_clients'] = 7 + hour
        msgs.append(make_msg(data.pop('data_type'), data, ts=ts))
    return msgs


def queue_body(msgs, n=1):
    return {'queue_id': f'Triton-b11run{n:05d}', 'name': 'Triton', 'id': 'id-triton', 'messages': msgs}


RUNS = [collector_run(0), collector_run(1, full_list=False, breakdown=False), collector_run(26, breakdown=True)]


def test_appendix_a_obeys_r1_and_r5():
    for payload in APPENDIX_A:
        keys = [k for k in payload if k != 'data_type']
        assert all(KEY_RE.fullmatch(k) for k in keys), payload['data_type']
        assert not set(keys) & INTERNAL_KEYS and not [k for k in keys if k.startswith('payload_')]
        assert len({k.lower() for k in keys}) == len(keys)                 # no two differ only in case
        assert len(keys) <= 1000
        assert len(json.dumps(payload).encode('utf-8')) <= 1024 * 1024


def _backups(path):
    return sorted(glob.glob(glob.escape(str(path)) + '.pre-upgrade-*.bak'))


def _tables(path):
    con = sqlite3.connect(str(path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _count(path, table):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def _decoded(row):
    """A wide row as sent: JSON text of lists / objects decoded (the old Managers stored them so)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and v[:1] in '[{':
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def _by_time(rows):
    """Rows in (timestamp, id) order: rows of one run share their timestamp (N10)."""
    return sorted(rows, key=lambda r: (str(r.get('timestamp')), r.get('id')))


def _new_client(data_dir, **overrides):
    mgr.init_app(base_config(str(data_dir), **overrides), logging.getLogger('test-b11'))
    return mgr.app.test_client()


def _get(client, path):
    r = client.get(path, headers=auth(READ))
    return r.status_code, r.get_json(silent=True)


def test_deployed_manager_stores_new_payloads_and_new_manager_takes_over(deployed, tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    reads = ['/api/data/Triton'] + [f'/api/data/Triton/{t}?limit=1000' for t in NEW_TYPES] + \
            ['/api/user_metrics/all', '/api/user_metrics/history/Triton/LAB%5Calice', '/api/metrics/all', '/api/totals']
    out = run_deployed(deployed['code'], deployed['config'](str(data_dir)), deployed['token'],
                       posts=[queue_body(m, i) for i, m in enumerate(RUNS)], gets=reads)
    for run, post in zip(RUNS, out['posts']):
        assert post['status'] == 200, post
        assert post['json']['stored'] == len(run), post          # an OLD collector-style ACK: nothing re-sent
        if deployed['name'] == 'current':
            assert post['json']['counts'] == {'stored': len(run), 'duplicate': 0, 'rejected': 0, 'error': 0}
    before = {p: r['json'] for p, r in out['gets'].items()}
    assert all(r['status'] == 200 for r in out['gets'].values()), out['gets']
    assert set(NEW_TYPES) <= set(before['/api/data/Triton']['data_types'])
    db, central = data_dir / 'triton' / 'data.db', data_dir / 'metrics.db'
    wide_rows = _count(db, 'storage_breakdown')
    assert wide_rows == 2 and 'storage_breakdown_msgs' not in _tables(db)
    assert not _backups(db) and not _backups(central)

    # ---- the NEW Manager on that data_dir ---------------------------------- #
    client = _new_client(data_dir)
    try:
        # one copy per database it altered, holding what the deployed Manager wrote
        (db_bak,) = _backups(db)
        (central_bak,) = _backups(central)
        assert 'storage_breakdown' in _tables(db_bak) and not [t for t in _tables(db_bak) if t.startswith('_')]
        assert not {'device_session_snapshot', 'device_storage_health'} & _tables(central_bak)
        # the wide storage_breakdown table is migrated, and kept
        tables = _tables(db)
        assert 'storage_breakdown' not in tables and {'storage_breakdown_legacy', 'storage_breakdown_msgs'} <= tables
        assert _count(db, 'storage_breakdown_legacy') == _count(db, 'storage_breakdown_msgs') == wide_rows

        # every stored row reads as before (storage_breakdown: its JSON text now nested)
        status, types = _get(client, '/api/data/Triton')
        assert sorted(types['data_types']) == sorted(before['/api/data/Triton']['data_types'])
        for t in NEW_TYPES:
            status, now = _get(client, f'/api/data/Triton/{t}?limit=1000')
            assert status == 200
            old_rows = before[f'/api/data/Triton/{t}?limit=1000']['rows']
            want = [_decoded(r) if t == 'storage_breakdown' else r for r in old_rows]
            got = now['rows']
            if deployed['name'] == 'old':          # the old Manager had no msg_id (N2)
                got = [{k: v for k, v in r.items() if k != 'msg_id'} for r in got]
            assert _by_time(got) == _by_time(want), t
        for path in ('/api/user_metrics/all', '/api/user_metrics/history/Triton/LAB%5Calice'):
            assert _get(client, path) == (200, before[path]), path
        totals = _get(client, '/api/totals')[1]
        for row in totals['devices'].values():
            row.pop('network_last_updated', None) if deployed['name'] == 'old' else None
        assert totals == before['/api/totals']
        sb = _get(client, '/api/data/Triton/storage_breakdown')[1]['rows']
        sent = {k: v for k, v in APPENDIX_A[-1].items() if k != 'data_type'}
        assert sb[0]['folders'] == sent['folders'] and sb[0]['device'] == sent['device']

        # rollups backfilled from what the deployed Manager stored
        status, st = _get(client, '/api/rollups/status')
        assert st['triton']['system_metrics']['complete'] is False
        assert mgr._data_store.run_rollup_backfill('triton')
        assert_rollups_match(str(db), ('system_metrics', 'user_metrics', 'sessions', 'storage_health'))
        assert all(v['complete'] for v in _get(client, '/api/rollups/status')[1]['triton'].values())
        h = _get(client, '/api/history/Triton/user_metrics/cpu_seconds?entity=LAB%5Calice'
                         f'&from={format_timestamp(T0)}&resolution=hour')[1]
        assert h['resolution'] == 'hour' and sum(p['count'] for p in h['data']) == 3

        # the new endpoints: per-system data at once, central snapshots from the next live message
        assert _get(client, '/api/sessions/Triton')[0] == 404
        assert _get(client, '/api/storage/health/Triton')[0] == 404
        assert _get(client, '/api/storage/breakdown/Triton')[1]['folders'] == sent['folders']
        usage = _get(client, '/api/user_metrics/usage?window=7d')[1]['devices']['Triton']
        assert usage['source'] == 'rollups' and {u['username'] for u in usage['users']} == {'LAB\\alice', 'LAB\\bob'}
        live = collector_run(30)
        r = client.post('/api/data/queue', json=queue_body(live, 9), headers=auth(ADMIN))
        assert r.get_json()['counts'] == {'stored': len(live), 'duplicate': 0, 'rejected': 0, 'error': 0}
        status, s = _get(client, '/api/sessions/Triton')
        assert status == 200 and s['sessions'] == APPENDIX_A[3]['sessions'] and s['summary']['smb_clients'] == 37
        status, health = _get(client, '/api/storage/health/Triton')
        assert status == 200 and health['overall_status'] == 'warning'
        assert _get(client, '/api/storage/breakdown/all')[1]['Triton']['folders'][0]['top_owner'] == 'jeff'
        m = _get(client, '/api/metrics/all')[1]['Triton']
        assert m['interval_s'] == 300.2 and m['disk_busy_max_name'] == 'sata2'
        alice = [u for u in _get(client, '/api/user_metrics/all')[1]['Triton'] if u['username'] == 'LAB\\alice'][0]
        assert alice['cpu_seconds'] == 1030.0 and alice['gpus_held'] == 1
        # a re-send of the runs the deployed Manager stored: duplicates (by msg_id, or by the old rows)
        r = client.post('/api/data/queue', json=queue_body(RUNS[0], 10), headers=auth(ADMIN))
        assert r.get_json()['counts']['duplicate'] == len(RUNS[0])
        # and a restart takes no further copy
        client = _new_client(data_dir)
        assert _backups(db) == [db_bak] and _backups(central) == [central_bak]
    finally:
        mgr._close_stores()


def _listing(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs
                  if not f.endswith(('-wal', '-shm')))


def test_downgrade_gate_new_data_dir_opened_by_current(tmp_path):
    data_dir = tmp_path / 'data'
    client = _new_client(data_dir)
    try:
        for i, run in enumerate(RUNS):
            r = client.post('/api/data/queue', json=queue_body(run, i), headers=auth(ADMIN))
            assert r.get_json()['counts']['stored'] == len(run)
    finally:
        mgr._close_stores()
    db, central = data_dir / 'triton' / 'data.db', data_dir / 'metrics.db'
    assert {'_rollup_hour', 'storage_breakdown_msgs', 'storage_breakdown_rows'} <= _tables(db)
    assert {'device_session_snapshot', 'device_storage_breakdown'} <= _tables(central)
    files = _listing(data_dir)

    # CURRENT on it: no backup, nothing created, none of those tables listed, ingest works
    later = collector_run(40)
    gets = ['/api/data/Triton', '/api/systems', '/api/metrics/all', '/api/user_metrics/all',
            '/api/history/Triton/system_metrics/cpu_percent', '/api/data/Triton/sessions']
    out = run_deployed(current_manager_dir(), base_config(str(data_dir)), ADMIN,
                       posts=[queue_body(later, 40)], gets=gets)
    assert out['posts'][0]['json']['counts'] == {'stored': len(later), 'duplicate': 0, 'rejected': 0, 'error': 0}
    assert all(r['status'] == 200 for r in out['gets'].values()), out['gets']
    listed = out['gets']['/api/data/Triton']['json']['data_types']
    assert not [t for t in listed if t.startswith(('_', 'storage_breakdown_'))]
    # (the storage_breakdown run of that batch went into a wide table, as CURRENT stores any new type)
    assert sorted(listed) == sorted(NEW_TYPES)
    assert not _backups(db) and not _backups(central)
    assert _listing(data_dir) == files
    assert out['gets']['/api/user_metrics/all']['json']['Triton'][0]['username'] in ('LAB\\alice', 'LAB\\bob')

    # back on NEW: CURRENT's rows are a rollup gap, its wide storage_breakdown table is migrated
    # (after a backup that holds it), and everything is readable
    client = _new_client(data_dir)
    try:
        (bak,) = _backups(db)
        assert 'storage_breakdown' in _tables(bak)
        assert not _backups(central)
        assert 'storage_breakdown' not in _tables(db) and 'storage_breakdown_legacy' in _tables(db)
        rows = _get(client, '/api/data/Triton/storage_breakdown')[1]['rows']
        assert len(rows) == 3 and rows[0]['timestamp'] == later[0]['header']['timestamp']
        assert rows[0]['folders'] == APPENDIX_A[-1]['folders']
        gaps = {p[0] for p in sqlite3.connect(str(db)).execute(
            "SELECT tbl FROM _rollup_pending WHERE reason = 'gap'")}
        assert {'system_metrics', 'user_metrics', 'sessions', 'storage_health'} <= gaps
        assert mgr._data_store.run_rollup_backfill('triton')
        assert_rollups_match(str(db), [t for t in rollups.TABLE_ORDER])
        client = _new_client(data_dir)
        assert _backups(db) == [bak]
    finally:
        mgr._close_stores()

    # the cycle once more: CURRENT makes a new wide storage_breakdown table (the
    # migrated one no longer holds its index names) and NEW migrates that too
    for n, cycle in enumerate((collector_run(50), collector_run(60))):
        out = run_deployed(current_manager_dir(), base_config(str(data_dir)), ADMIN,
                           posts=[queue_body(cycle, 50 + n)], gets=['/api/data/Triton'])
        assert out['posts'][0]['json']['counts'] == {'stored': len(cycle), 'duplicate': 0, 'rejected': 0, 'error': 0}
        assert sorted(out['gets']['/api/data/Triton']['json']['data_types']) == sorted(NEW_TYPES)
        client = _new_client(data_dir)
        try:
            assert len(_backups(db)) == 2 + n and not _backups(central)
            legacy = {'storage_breakdown_legacy', 'storage_breakdown_legacy_1'} | (
                {'storage_breakdown_legacy_2'} if n else set())
            assert 'storage_breakdown' not in _tables(db) and legacy <= _tables(db)
            rows = _get(client, '/api/data/Triton/storage_breakdown')[1]['rows']
            assert len(rows) == 4 + n and rows[0]['timestamp'] == cycle[0]['header']['timestamp']
            assert rows[0]['folders'] == APPENDIX_A[-1]['folders']
        finally:
            mgr._close_stores()
