"""
Rollups (manager/rollups.py) and their hooks in data_store, manager and
archive_metrics.

The oracle everywhere is a brute-force GROUP BY over the raw table with the
same SQL bucketing (UTC hour / day of strftime('%s', timestamp)) and the same
"numeric values only" rule: the rollup tables must hold exactly that, however
the rows got there (live ingest, backfill of OLD-format data written by the
f086e7e code itself, CURRENT-format data written by the fb6498c code, a
restart or kill in the middle of a backfill, a downgrade gap).
"""
import glob
import io
import json
import logging
import math
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import archive_metrics
import data_store
import manager as mgr
import rollups
from current_manager import run_current
from manager_helpers import ADMIN, COLL_TRITON, READ, auth, base_config, make_msg, metrics_payload, post_queue
from storage_util import format_timestamp

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MANAGER_DIR = REPO / 'manager'
LEVEL_WIDTH = {'hour': 3600, 'day': 86400}
_DENY = {'system_metrics': {'cpu_count'}, 'user_metrics': {'interval_s', 'username'},
         'sessions': {'sessions_truncated'}, 'storage_health': set()}
_SCRATCH_BASE = '/tmp/claude-1000' if os.path.isdir('/tmp/claude-1000') else None
log = logging.getLogger('test-rollups')


# --------------------------------------------------------------------------- #
# Oracle                                                                      #
# --------------------------------------------------------------------------- #

def _connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def brute_force(path, table, level, frozen_before=None):
    """{(entity, field, bucket): (n, vsum, vmin, vmax)} straight from the raw rows."""
    w = LEVEL_WIDTH[level]
    con = _connect(path)
    try:
        cols = [(r[1], r[2]) for r in con.execute(f'PRAGMA table_info("{table}")')]
        names = {c.lower() for c, _ in cols}
        ent = '"username"' if table == 'user_metrics' and 'username' in names else "''"
        out = {}
        for col, decl in cols:
            low = col.lower()
            if (decl or '').upper() != 'REAL' or low in ('id', 'timestamp', 'device_type', 'msg_id') \
                    or low.endswith('_unit') or low.startswith(('payload_', '_')) or low in _DENY[table]:
                continue
            q = col.replace('"', '""')
            cond = f"typeof(\"{q}\") IN ('integer', 'real') AND abs(\"{q}\") < 1e308 AND b IS NOT NULL"
            params = []
            if frozen_before:
                cond += ' AND timestamp >= ?'
                params.append(frozen_before)
            for e, b, n, s, lo, hi in con.execute(
                    f"SELECT COALESCE({ent}, ''), (CAST(strftime('%s', timestamp) AS INTEGER) / {w}) * {w} AS b, "
                    f"COUNT(*), TOTAL(\"{q}\"), MIN(\"{q}\"), MAX(\"{q}\") FROM \"{table}\" WHERE {cond} "
                    f"GROUP BY 1, 2", params):
                out[(e, col, b)] = (n, s, lo, hi)
        return out
    finally:
        con.close()


def rolled(path, table, level):
    """{(entity, field, bucket): (n, vsum, vmin, vmax)} from the rollup tables."""
    con = _connect(path)
    try:
        return {(r['entity'], r['field'], r['bucket']): (r['n'], r['vsum'], r['vmin'], r['vmax'])
                for r in con.execute(
                    f'SELECT s.entity, s.field, x.bucket, x.n, x.vsum, x.vmin, x.vmax FROM _rollup_{level} x '
                    f'JOIN _rollup_series s ON s.id = x.series_id WHERE s.data_type = ?', (table,))}
    finally:
        con.close()


def assert_same(got, want, what=''):
    assert set(got) == set(want), (what, sorted(set(got) ^ set(want))[:10])
    for key, (n, s, lo, hi) in want.items():
        gn, gs, glo, ghi = got[key]
        assert gn == n and glo == lo and ghi == hi, (what, key, got[key], want[key])
        assert math.isclose(gs, s, rel_tol=1e-9, abs_tol=1e-6), (what, key, gs, s)


def assert_rollups_match(path, tables=('system_metrics', 'user_metrics'), frozen_before=None):
    for table in tables:
        for level in ('hour', 'day'):
            if level in rollups.REGISTRY[table]['levels']:
                want = brute_force(path, table, level, frozen_before)
                assert_same(rolled(path, table, level), want, f'{path} {table} {level}')
            else:
                assert rolled(path, table, level) == {}


def pending(path):
    con = _connect(path)
    try:
        return [tuple(r) for r in con.execute('SELECT tbl, lo, hi, cursor, reason FROM _rollup_pending ORDER BY tbl, lo')]
    finally:
        con.close()


def tables_of(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def backups(path):
    return sorted(glob.glob(glob.escape(path) + '.pre-upgrade-*.bak'))


def finish_backfill(store=None):
    """Run every pending range to completion (the background thread's work, synchronously)."""
    store = store or mgr._data_store
    for system in store.list_systems():
        assert store.run_rollup_backfill(system)


def _statuses(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return [r['status'] for r in resp.get_json()['results']]


# --------------------------------------------------------------------------- #
# Random but reproducible messages                                            #
# --------------------------------------------------------------------------- #

T0 = datetime(2026, 8, 3, 21, 0, 0, tzinfo=timezone.utc)


def _ts(rnd, i):
    """Timestamps across three days, some exactly on hour / day boundaries."""
    if i % 17 == 0:
        return format_timestamp(T0 + timedelta(hours=rnd.randint(0, 60)))
    if i % 23 == 0:
        return format_timestamp(T0 + timedelta(days=1, microseconds=-1))
    return format_timestamp(T0 + timedelta(seconds=rnd.randint(0, 3 * 86400), microseconds=rnd.randint(0, 999999)))


def random_messages(seed=1, n=150, name='Triton'):
    rnd = random.Random(seed)
    msgs = []
    for i in range(n):
        ts = _ts(rnd, i)
        cpu = rnd.choice([rnd.uniform(0, 100), None, float('nan'), 0, 100])
        data = metrics_payload(cpu=cpu, bytes_in=rnd.randint(0, 10 ** 12), bytes_out=rnd.randint(0, 10 ** 9))
        data['fan_ok'] = rnd.choice([True, False, None])
        data['extra'] = rnd.choice([rnd.uniform(-5, 5), '12.5', 'abc', None, 7])
        if i % 3:
            data.update({'interval_s': 300.0, 'interval_s_unit': 's', 'cpu_count': 8,
                         'disk_read_bytes_per_s': rnd.randint(0, 10 ** 8), 'disk_read_bytes_per_s_unit': 'B/s'})
        msgs.append(make_msg('system_metrics', data, ts=ts, name=name))
        for user in rnd.sample(['alice', 'bob', 'LAB\\carol', 'uid:1001'], rnd.randint(0, 3)):
            u = {'username': user, 'cpu_percent': rnd.choice([rnd.uniform(0, 50), None]),
                 'ram_bytes': rnd.choice([rnd.randint(0, 2 ** 34), None]), 'ram_formatted': 'x'}
            if rnd.random() < 0.6:
                u.update({'cpu_seconds': round(rnd.uniform(0, 3000), 2), 'cpu_cores': rnd.uniform(0, 8),
                          'interval_s': 300.2, 'process_count': rnd.randint(0, 40)})
            if rnd.random() < 0.3:
                u.update({'gpu_vram_bytes': rnd.randint(0, 2 ** 34), 'gpu_util_percent': rnd.uniform(0, 100),
                          'gpu_busy_seconds': rnd.uniform(0, 300), 'gpus_held': 1, 'gpu_held_seconds': 300.2,
                          'gpu_processes': 2})
            msgs.append(make_msg('user_metrics', u, ts=ts, name=name))
    return msgs


def seed_message(name='Triton'):
    """Posted first, so 'extra' and 'fan_ok' become REAL columns whatever order follows."""
    return make_msg('system_metrics', dict(metrics_payload(), extra=1.0, fan_ok=True),
                    ts='2026-08-03T20:30:00.000000Z', name=name)


def post_all(client, msgs, name='Triton', token=ADMIN, size=50):
    statuses = []
    for i in range(0, len(msgs), size):
        statuses += _statuses(post_queue(client, msgs[i:i + size], token=token, name=name))
    return statuses


# --------------------------------------------------------------------------- #
# OLD-format data dirs (make_old_fixture.py with the f086e7e code)            #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='module')
def old_fixture():
    """An OLD-format data_dir written by the OLD collector + Manager (2 days, 6 devices)."""
    sys.path.insert(0, str(REPO / 'tests' / 'compat'))
    try:
        from compat_helpers import OLD_REF, build_fixture
    finally:
        sys.path.pop(0)
    root = Path(tempfile.mkdtemp(prefix='lm-rollups-old-', dir=_SCRATCH_BASE))
    try:
        tar = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', OLD_REF],
                             capture_output=True, check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f'old code not available: {e}')
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        kw = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
        for m in tf.getmembers():
            if m.isfile() or m.isdir():
                tf.extract(m, str(root / 'old-code'), **kw)
    out = root / 'fixture' / 'data'
    build_fixture(root / 'old-code', out, '--days', '2')
    yield out
    shutil.rmtree(root, ignore_errors=True)


def _copy_fixture(src, data_dir):
    shutil.copytree(str(src), data_dir)
    return sorted(d for d in os.listdir(data_dir) if os.path.isfile(os.path.join(data_dir, d, 'data.db')))


# =========================================================================== #
# B1: data_store groundwork                                                   #
# =========================================================================== #

def test_internal_tables_are_reserved_and_hidden(client, data_dir):
    assert _statuses(post_queue(client, random_messages(n=10), token=ADMIN)) == ['stored'] * len(random_messages(n=10))
    path = os.path.join(data_dir, 'triton', 'data.db')
    internal = {t for t in tables_of(path) if t.startswith('_rollup_')}
    assert internal == {'_rollup_series', '_rollup_hour', '_rollup_day', '_rollup_state', '_rollup_pending'}
    types = client.get('/api/data/Triton', headers=auth(READ)).get_json()['data_types']
    assert types == ['system_metrics', 'user_metrics']
    for t in internal:
        assert client.get(f'/api/data/Triton/{t}', headers=auth(READ)).status_code == 400
        assert client.get(f'/api/history/Triton/{t}/n', headers=auth(READ)).status_code == 400
    r = post_queue(client, [make_msg('_rollup_hour', {'n': 1})], token=ADMIN)
    assert _statuses(r) == ['rejected']
    con = sqlite3.connect(path)
    try:
        assert data_store.TypedDataStore._upgrade_reasons(con) == []
    finally:
        con.close()
    # the temp table of a batch never lands in data.db
    assert '_rollup_batch' not in tables_of(path)


def test_insert_helpers_return_row_ids_and_storage_breakdown_is_listed(data_dir):
    store = data_store.TypedDataStore(data_dir)
    try:
        store.store_message('Triton', 'a' * 32, '2026-08-04T07:00:00.000000Z', 'NAS', 'system_metrics', {'x': 1.0})
        db = store._get_db('Triton')
        with db.transaction() as conn:
            rid = store._insert_wide(conn, 'system_metrics', 'b' * 32, '2026-08-04T07:05:00.000000Z', 'NAS', {'x': 2.0})
            store._ensure_folder_tables(conn)
            fid = store._insert_folder_usage(conn, 'c' * 32, '2026-08-04T02:00:00.000000Z', 'NAS', {'/v1': 5})
        assert (rid, fid) == (2, 1)
        assert store.list_data_types('Triton') == ['folder_usage', 'system_metrics']
        # the narrow storage_breakdown layout (B5) is listed under its data type, never by table name
        with db.transaction() as conn:
            conn.execute('CREATE TABLE storage_breakdown_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE, '
                         "timestamp TEXT NOT NULL, device_type TEXT DEFAULT 'unknown', meta_json TEXT NOT NULL DEFAULT '{}')")
            conn.execute('CREATE TABLE storage_breakdown_rows (msg_row_id INTEGER NOT NULL, scope TEXT NOT NULL, '
                         'path TEXT NOT NULL, kind TEXT NOT NULL, owner TEXT NOT NULL DEFAULT \'\', bytes INTEGER, '
                         'PRIMARY KEY (msg_row_id, scope, path, kind, owner)) WITHOUT ROWID')
        assert store.list_data_types('Triton') == ['folder_usage', 'storage_breakdown', 'system_metrics']
        assert data_store.TypedDataStore._upgrade_reasons(db.conn) == []
    finally:
        store.close()


# =========================================================================== #
# Live path                                                                   #
# =========================================================================== #

def test_live_rollups_equal_brute_force(client, data_dir):
    msgs = random_messages(seed=7)
    statuses = post_all(client, [seed_message()] + msgs)
    assert statuses.count('stored') == len(msgs) + 1
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert pending(path) == []
    assert_rollups_match(path)
    hour = rolled(path, 'system_metrics', 'hour')
    fields = {f for _, f, _ in hour}
    # numbers, booleans (0/1) and numeric text in REAL columns roll up; text, units,
    # the denylist and NULL / NaN never do (no bucket from a gap)
    assert {'cpu_percent', 'fan_ok', 'extra', 'interval_s', 'network_bytes_in'} <= fields
    assert not fields & {'cpu_count', 'cpu_percent_unit', 'uptime_formatted', 'msg_id'}
    assert all(b % 3600 == 0 for _, _, b in hour)
    assert all(b % 86400 == 0 for _, _, b in rolled(path, 'system_metrics', 'day'))
    users = {e for e, _, _ in rolled(path, 'user_metrics', 'hour')}
    assert users == {'alice', 'bob', 'LAB\\carol', 'uid:1001'}
    assert not {f for _, f, _ in rolled(path, 'user_metrics', 'hour')} & {'interval_s', 'username', 'ram_formatted'}
    con = _connect(path)
    units = {r['field']: r['unit'] for r in con.execute(
        "SELECT field, unit FROM _rollup_series WHERE data_type = 'user_metrics'")}
    sm_units = {r['field']: r['unit'] for r in con.execute(
        "SELECT field, unit FROM _rollup_series WHERE data_type = 'system_metrics'")}
    con.close()
    assert units['cpu_seconds'] == 's' and units['ram_bytes'] == 'bytes' and units['cpu_cores'] == 'cores'
    assert sm_units['cpu_percent'] == '%' and sm_units['disk_read_bytes_per_s'] == 'B/s' and sm_units['fan_ok'] is None


def test_all_null_buckets_give_no_rows(client, data_dir):
    msgs = [make_msg('system_metrics', {'cpu_percent': None, 'ram_percent': 5.0}, ts=f'2026-08-04T0{h}:10:00.000000Z')
            for h in range(3)]
    assert post_all(client, msgs) == ['stored'] * 3
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert {f for _, f, _ in rolled(path, 'system_metrics', 'hour')} == {'ram_percent'}
    assert_rollups_match(path, ('system_metrics',))


def test_order_duplicates_and_legacy_replays_do_not_change_rollups(make_client, data_dir):
    client = make_client(collector_tokens={'*': COLL_TRITON})
    msgs = random_messages(seed=11, n=80)
    atlas = [dict(m, header=dict(m['header'], device_name='Atlas', msg_id=uuid.uuid4().hex)) for m in msgs]
    random.Random(3).shuffle(atlas)
    post_all(client, [seed_message()] + msgs)
    post_all(client, [seed_message('Atlas')] + atlas, name='Atlas')
    tri, atl = (os.path.join(data_dir, d, 'data.db') for d in ('triton', 'atlas'))
    for table in ('system_metrics', 'user_metrics'):
        for level in ('hour', 'day'):
            assert_same(rolled(atl, table, level), rolled(tri, table, level), f'shuffled {table} {level}')
    before = {(t, lv): rolled(tri, t, lv) for t in ('system_metrics', 'user_metrics') for lv in ('hour', 'day')}
    # the same msg_ids again, and old-collector messages (no msg_id) sent twice
    again = post_all(client, random.Random(5).sample(msgs, 30))
    assert set(again) == {'duplicate'}
    legacy = [make_msg('system_metrics', metrics_payload(cpu=float(i)), ts=f'2026-08-05T01:0{i}:00Z', msg_id=None)
              for i in range(5)]
    assert post_all(client, legacy) == ['stored'] * 5
    mid = {(t, lv): rolled(tri, t, lv) for t in ('system_metrics', 'user_metrics') for lv in ('hour', 'day')}
    assert post_all(client, legacy) == ['duplicate'] * 5
    after = {(t, lv): rolled(tri, t, lv) for t in ('system_metrics', 'user_metrics') for lv in ('hour', 'day')}
    assert after == mid
    assert mid[('system_metrics', 'hour')] != before[('system_metrics', 'hour')]   # the first send counted
    assert mid[('user_metrics', 'hour')] == before[('user_metrics', 'hour')]
    assert_rollups_match(tri)


def test_rollup_bug_never_dead_letters_valid_data(client, data_dir, monkeypatch, caplog):
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert post_all(client, [seed_message()]) == ['stored']
    real = rollups.RollupEngine.roll_range

    def broken(self, *a, **kw):
        raise ValueError('bug in rollups')

    monkeypatch.setattr(rollups.RollupEngine, 'roll_range', broken)
    msgs = random_messages(seed=21, n=12)
    with caplog.at_level(logging.ERROR, logger='rollups'):
        assert set(post_all(client, msgs)) == {'stored'}
    assert sum(1 for r in caplog.records if 'rolling up' in r.getMessage()) <= 2   # rate-limited per table
    con = _connect(path)
    n_raw = con.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0]
    con.close()
    assert n_raw == 13
    assert {p[4] for p in pending(path)} <= {'repair', 'gap'} and pending(path)
    assert mgr._metrics_db.get_dead_letters(10) == []
    monkeypatch.setattr(rollups.RollupEngine, 'roll_range', real)
    finish_backfill()
    assert pending(path) == []
    assert_rollups_match(path)


def test_transient_rollup_error_is_a_retryable_error(client, data_dir, monkeypatch):
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert post_all(client, [seed_message()]) == ['stored']

    def locked(self, *a, **kw):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(rollups.RollupEngine, 'roll_range', locked)
    msg = make_msg('system_metrics', metrics_payload(cpu=50.0), ts='2026-08-04T09:00:00.000000Z')
    assert _statuses(post_queue(client, [msg], token=ADMIN)) == ['error']
    con = _connect(path)
    assert con.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0] == 1   # rolled back with it
    con.close()
    monkeypatch.undo()
    assert _statuses(post_queue(client, [msg], token=ADMIN)) == ['stored']        # the collector's retry
    assert pending(path) == []
    assert_rollups_match(path, ('system_metrics',))


def test_series_cap_skips_new_series_and_logs_once(make_client, data_dir, caplog):
    client = make_client(rollups={'max_series_per_table': 3})
    path = os.path.join(data_dir, 'triton', 'data.db')
    with caplog.at_level(logging.WARNING, logger='rollups'):
        for i in range(3):
            post_all(client, [make_msg('system_metrics', {'a': 1.0, 'b': 2.0, 'c': 3.0, 'd': 4.0, 'e': float(i)},
                                       ts=f'2026-08-04T0{i}:00:00.000000Z')])
    con = _connect(path)
    assert con.execute('SELECT COUNT(*) FROM _rollup_series').fetchone()[0] == 3
    con.close()
    assert sum(1 for r in caplog.records if 'max_series_per_table' in r.getMessage()) == 1
    # the series that exist keep rolling up
    hour = rolled(path, 'system_metrics', 'hour')
    assert len({f for _, f, _ in hour}) == 3 and len(hour) == 9


def test_retention_keeps_a_table_at_the_series_cap(make_client, data_dir, caplog):
    # field c is beyond max_series_per_table: its raw rows are all there is of it
    client = make_client(rollups={'max_series_per_table': 2})
    t0 = datetime.now(timezone.utc) - timedelta(days=60)
    post_all(client, [make_msg('system_metrics', {'a': 1.0, 'b': 2.0, 'c': float(i)},
                               ts=format_timestamp(t0 + timedelta(hours=i))) for i in range(48)])
    assert mgr._data_store.rollups_complete('Triton', 'system_metrics')
    path = os.path.join(data_dir, 'triton', 'data.db')
    with caplog.at_level(logging.WARNING):
        result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 30, log)
    assert result['archived'] == {}
    assert any('triton/system_metrics: skipped' in r.getMessage() and 'max_series_per_table' in r.getMessage()
               for r in caplog.records)
    con = _connect(path)
    assert con.execute('SELECT COUNT(c) FROM system_metrics').fetchone()[0] == 48
    con.close()
    # with room for every field and a rebuild, the rows are rolled up and pruned
    mgr._close_stores()
    store = data_store.TypedDataStore(data_dir, rollups=rollups.RollupEngine({'max_series_per_table': 10}))
    try:
        assert store.rebuild_rollups('Triton', 'system_metrics')['queued_ids'] == 48
        assert store.run_rollup_backfill('Triton')
        result = archive_metrics.run_history_retention(store, None, data_dir, 30, log)
        assert result['archived'] == {'triton/system_metrics': 48}
        assert {f for _, f, _ in rolled(path, 'system_metrics', 'hour')} == {'a', 'b', 'c'}
    finally:
        store.close()


def test_rollups_disabled_writes_nothing_and_the_gap_is_filled_later(make_client, data_dir):
    client = make_client(rollups={'enabled': False})
    msgs = random_messages(seed=31, n=40)
    post_all(client, [seed_message()] + msgs)
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert not [t for t in tables_of(path) if t.startswith('_')]
    assert client.get('/api/rollups/status', headers=auth(READ)).get_json() == {}
    client = make_client()                      # enabled again: everything stored meanwhile is queued
    assert {p[0] for p in pending(path)} == {'system_metrics', 'user_metrics'}
    post_all(client, random_messages(seed=32, n=10))
    finish_backfill()
    assert_rollups_match(path)


# =========================================================================== #
# Backfill                                                                    #
# =========================================================================== #

def test_backfill_of_old_format_data(old_fixture, make_client, data_dir):
    systems = _copy_fixture(old_fixture, data_dir)
    assert systems == ['atlantis', 'lab_nas', 'vault_2', 'win_srv_lab', 'ws_07']
    originals = {}
    for s in systems:
        p = os.path.join(data_dir, s, 'data.db')
        con = sqlite3.connect(p)
        originals[s] = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables_of(p)}
        assert 'msg_id' not in {r[1] for r in con.execute('PRAGMA table_info(system_metrics)')}   # really OLD
        con.close()
    client = make_client()
    status = client.get('/api/rollups/status', headers=auth(READ)).get_json()
    assert set(status) == set(systems)
    for s in systems:
        p = os.path.join(data_dir, s, 'data.db')
        (bak,) = backups(p)                                     # the usual one pre-upgrade copy ...
        assert not [t for t in tables_of(bak) if t.startswith('_')]   # ... taken before any rollup table
        sm = status[s]['system_metrics']
        assert sm['complete'] is False and sm['done_ids'] == 0 and sm['pending_ids'] == originals[s]['system_metrics']
        assert pending(p)[0][4] == 'initial'
    finish_backfill()
    status = client.get('/api/rollups/status', headers=auth(READ)).get_json()
    for s in systems:
        p = os.path.join(data_dir, s, 'data.db')
        assert all(t['complete'] and t['pending_ids'] == 0 for t in status[s].values())
        assert_rollups_match(p, [t for t in ('system_metrics', 'user_metrics') if t in tables_of(p)])
        # nothing stored was rewritten or deleted
        con = sqlite3.connect(p)
        for t, n in originals[s].items():
            if t not in ('folder_usage', 'sqlite_sequence'):
                assert con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == n, (s, t)
        con.close()
        assert len(backups(p)) == 1
    # old rows hold the 1-s cpu_percent sample (interval_s NULL): rolled up as stored, and the
    # counter fields they lack add nothing (no series, no zeros)
    p = os.path.join(data_dir, 'atlantis', 'data.db')
    hour = rolled(p, 'system_metrics', 'hour')
    con = sqlite3.connect(p)
    n_cpu = con.execute("SELECT COUNT(*) FROM system_metrics WHERE typeof(cpu_percent) = 'real'").fetchone()[0]
    con.close()
    assert sum(v[0] for (e, f, b), v in hour.items() if f == 'cpu_percent') == n_cpu
    assert not {f for _, f, _ in hour} & {'interval_s', 'cpu_seconds', 'disk_read_bytes_per_s'}
    # a NEW message (interval-averaged cpu) joins the same series, with its interval_s
    data = dict(metrics_payload(cpu=33.0), interval_s=300.0, interval_s_unit='s')
    r = post_queue(client, [make_msg('system_metrics', data, ts='2026-08-02T23:59:00.000000Z', name='Atlantis')],
                   token=ADMIN, name='Atlantis')
    assert _statuses(r) == ['stored']
    assert_rollups_match(p)
    assert 'interval_s' in {f for _, f, _ in rolled(p, 'system_metrics', 'hour')}
    # a restart takes no further backup and queues nothing
    client = make_client()
    assert all(len(backups(os.path.join(data_dir, s, 'data.db'))) == 1 for s in systems)
    assert all(pending(os.path.join(data_dir, s, 'data.db')) == [] for s in systems)


def _build_current(data_dir, msgs_by_name):
    """CURRENT-format databases written by the fb6498c Manager itself."""
    posts = []
    for name, msgs in msgs_by_name.items():
        for i in range(0, len(msgs), 100):
            posts.append({'queue_id': f'{name}-{uuid.uuid4().hex[:12]}', 'name': name, 'id': f'id-{name.lower()}',
                          'messages': msgs[i:i + 100]})
    out = run_current({'data_dir': data_dir, 'admin_tokens': [ADMIN]}, ADMIN, posts=posts)
    for p in out['posts']:
        assert p['status'] == 200 and p['json']['counts']['error'] == 0, p
    return out


def test_backfill_of_current_format_data(make_client, data_dir):
    _build_current(data_dir, {'Triton': [seed_message()] + random_messages(seed=41, n=120),
                              'Atlas': [seed_message('Atlas')] + random_messages(seed=42, n=60, name='Atlas')})
    paths = [os.path.join(data_dir, d, 'data.db') for d in ('triton', 'atlas')]
    for p in paths:
        assert not [t for t in tables_of(p) if t.startswith('_')]
    client = make_client()
    for p in paths:
        assert backups(p) == []                     # CURRENT format: nothing to migrate, no copy
        assert {x[4] for x in pending(p)} == {'initial'}
    finish_backfill()
    for p in paths:
        assert_rollups_match(p)
    post_all(client, random_messages(seed=43, n=20))
    assert_rollups_match(paths[0])


def _bulk_rows(path, n, start=datetime(2025, 1, 1, tzinfo=timezone.utc), step=300, fields=12, seed=5):
    """n CURRENT-layout system_metrics rows written directly (fast), no rollups."""
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS system_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp DATETIME NOT NULL, device_type TEXT DEFAULT 'unknown', msg_id TEXT)")
    have = {r[1] for r in con.execute('PRAGMA table_info(system_metrics)')}
    names = [f'f{i}' for i in range(fields)]
    for f in names:
        if f not in have:
            con.execute(f'ALTER TABLE system_metrics ADD COLUMN "{f}" REAL')
    con.execute('CREATE INDEX IF NOT EXISTS idx_system_metrics_ts ON system_metrics (timestamp DESC)')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_system_metrics_msg_id ON system_metrics (msg_id)')
    cols = ['timestamp', 'msg_id'] + names
    rows = [[format_timestamp(start + timedelta(seconds=step * i)), uuid.uuid4().hex]
            + [rnd.choice([rnd.random() * 100, None]) for _ in names] for i in range(n)]
    con.executemany(f'INSERT INTO system_metrics ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', rows)
    con.commit()
    con.close()


def test_restart_mid_backfill_resumes_without_double_counting(data_dir):
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_rows(path, 9000)
    engine = rollups.RollupEngine({'backfill_batch_ids': 500, 'backfill_max_duty': 1.0})
    store = data_store.TypedDataStore(data_dir, rollups=engine)
    db = store._get_db('triton')
    for _ in range(5):
        assert engine.backfill_step(db, 'system_metrics') == 500
    assert pending(path) == [('system_metrics', 0, 9000, 2500, 'initial')]
    store.close()                                   # restart
    store = data_store.TypedDataStore(data_dir, rollups=rollups.RollupEngine({'backfill_batch_ids': 700}))
    assert pending(path) == [('system_metrics', 0, 9000, 2500, 'initial')]
    assert store.run_rollup_backfill('triton')
    store.close()
    assert pending(path) == []
    assert_rollups_match(path, ('system_metrics',))


_KILL_RUNNER = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import rollups, data_store
store = data_store.TypedDataStore(sys.argv[2], rollups=rollups.RollupEngine(
    {"backfill_batch_ids": int(sys.argv[3]), "backfill_max_duty": 1.0}))
store.run_rollup_backfill("triton")
print("done", flush=True)
time.sleep(60)
'''


def _probe_cursor(path):
    con = sqlite3.connect(path, timeout=10)
    try:
        row = con.execute("SELECT cursor FROM _rollup_pending WHERE tbl = 'system_metrics'").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def test_killed_backfill_and_a_concurrent_process_never_double_count(data_dir):
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_rows(path, 30000, fields=20)
    # 1. kill -9 a process in the middle of its backfill (most of its time is inside a batch transaction)
    proc = subprocess.Popen([sys.executable, '-c', _KILL_RUNNER, str(MANAGER_DIR), data_dir, '100'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        cur = _probe_cursor(path)
        if cur and cur >= 3000:
            break
        time.sleep(0.02)
    proc.send_signal(signal.SIGKILL)
    proc.wait(10)
    cur = _probe_cursor(path)
    assert cur is not None and 0 < cur < 30000, cur
    # 2. finish it from two processes at once: this one and a subprocess (archive CLI style)
    other = subprocess.Popen([sys.executable, '-c', _KILL_RUNNER, str(MANAGER_DIR), data_dir, '400'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    store = data_store.TypedDataStore(data_dir, rollups=rollups.RollupEngine(
        {'backfill_batch_ids': 250, 'backfill_max_duty': 1.0}))
    try:
        assert store.run_rollup_backfill('triton')
    finally:
        store.close()
    line = other.stdout.readline()
    other.kill()
    other.wait(10)
    assert line.strip() == b'done', other.stderr.read()[-2000:]
    assert pending(path) == []
    assert_rollups_match(path, ('system_metrics',))


def test_ingest_is_not_blocked_by_the_backfill(make_client, data_dir):
    """Measured, logged and bounded: ingest waits at most for one batch transaction."""
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_rows(path, 40000, fields=22, start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    client = make_client()
    thread = rollups.start_backfill_thread(mgr._data_store)
    lat, i = [], 0
    try:
        t_end = time.monotonic() + 60
        while _probe_cursor(path) is not None and time.monotonic() < t_end:
            msg = make_msg('system_metrics', dict(metrics_payload(cpu=float(i % 100)), f0=1.0),
                           ts=format_timestamp(datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(seconds=i)))
            t0 = time.monotonic()
            assert _statuses(post_queue(client, [msg], token=ADMIN)) == ['stored']
            lat.append(time.monotonic() - t0)
            i += 1
            time.sleep(0.005)
    finally:
        thread.stop()
    assert _probe_cursor(path) is None, 'backfill did not finish within 60 s'
    lat.sort()
    log.warning(f'ingest during backfill: {len(lat)} messages, median {lat[len(lat) // 2] * 1000:.1f} ms, '
                f'p99 {lat[int(len(lat) * 0.99)] * 1000:.1f} ms, max {lat[-1] * 1000:.1f} ms')
    assert len(lat) >= 20
    assert lat[-1] < 2.0          # one batch of 2000 ids takes ~50 ms here; generous for slow CI
    assert_rollups_match(path, ('system_metrics',))


def test_gap_from_rows_stored_without_rollups(client, data_dir):
    """A downgraded (or rollups-disabled) Manager stored rows: the next open queues them."""
    post_all(client, [seed_message()] + random_messages(seed=51, n=30))
    path = os.path.join(data_dir, 'triton', 'data.db')
    mgr._close_stores()
    con = sqlite3.connect(path)
    with con:
        for i in range(25):
            con.execute('INSERT INTO system_metrics (timestamp, device_type, msg_id, cpu_percent) VALUES (?, ?, ?, ?)',
                        (f'2026-08-06T0{i % 10}:{i:02d}:00.000000Z', 'NAS', uuid.uuid4().hex, float(i)))
        con.execute("INSERT INTO system_metrics (timestamp, device_type, msg_id, cpu_percent) "
                    "VALUES ('not a timestamp', 'NAS', 'x1', 50.0)")
    con.close()
    mgr.init_app(base_config(data_dir), log)
    gaps = [p for p in pending(path) if p[4] == 'gap']
    assert len(gaps) == 1 and gaps[0][0] == 'system_metrics' and gaps[0][2] - gaps[0][1] == 26
    finish_backfill()
    assert pending(path) == []
    assert_rollups_match(path)                     # the unparseable row has no bucket: skipped


def test_spec_version_change_rolls_up_again(client, data_dir, monkeypatch):
    post_all(client, [seed_message()] + random_messages(seed=61, n=30))
    path = os.path.join(data_dir, 'triton', 'data.db')
    monkeypatch.setattr(rollups, 'SPEC_VERSION', rollups.SPEC_VERSION + 1)
    mgr.init_app(base_config(data_dir), log)
    assert {p[4] for p in pending(path)} == {'spec'}
    assert rolled(path, 'system_metrics', 'hour') == {}
    finish_backfill()
    assert_rollups_match(path)
    con = _connect(path)
    assert {r[0] for r in con.execute('SELECT spec_version FROM _rollup_state')} == {rollups.SPEC_VERSION}
    con.close()


# =========================================================================== #
# Downgrade NEW -> CURRENT (fb6498c) -> NEW                                   #
# =========================================================================== #

def test_downgrade_to_current_manager_and_back(make_client, data_dir):
    client = make_client()
    post_all(client, [seed_message()] + random_messages(seed=71, n=60))
    sessions = make_msg('sessions', {'_schema': 1, '_unit': 'count', '_list_state': 'none', '_list_hash': None,
                                     'clients_total': 3, 'smb_clients': 2, 'sessions_truncated': False},
                        ts='2026-08-04T10:00:00.000000Z')
    assert _statuses(post_queue(client, [sessions], token=ADMIN)) == ['stored']
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert '_rollup_hour' in tables_of(path)
    mgr._close_stores()
    files_before = sorted(os.listdir(os.path.join(data_dir, 'triton')))
    later = random_messages(seed=72, n=30)
    later = [dict(m, header=dict(m['header'], timestamp=m['header']['timestamp'].replace('2026-08-0', '2026-09-0')))
             for m in later]
    out = run_current(base_config(data_dir), ADMIN,
                      posts=[{'queue_id': 'Triton-downgrade1', 'name': 'Triton', 'id': 'id-triton', 'messages': later}],
                      gets=['/api/data/Triton', '/api/data/Triton/system_metrics', '/api/systems',
                            '/api/history/Triton/system_metrics/cpu_percent', '/api/metrics/all',
                            '/api/user_metrics/Triton', '/api/history/Triton/sessions/clients_total'])
    assert out['posts'][0]['json']['counts'] == {'stored': len(later), 'duplicate': 0, 'rejected': 0, 'error': 0}
    for p, r in out['gets'].items():
        assert r['status'] == 200, (p, r)
    assert out['gets']['/api/data/Triton']['json']['data_types'] == ['sessions', 'system_metrics', 'user_metrics']
    assert out['gets']['/api/history/Triton/sessions/clients_total']['json']['data'][0]['value'] == 3
    # CURRENT took no backup and created nothing next to the database
    assert sorted(os.listdir(os.path.join(data_dir, 'triton'))) == files_before
    assert backups(path) == [] and backups(os.path.join(data_dir, 'metrics.db')) == []
    # back on NEW: the rows CURRENT stored are a gap, rolled up by the backfill
    client = make_client()
    gaps = [p for p in pending(path) if p[4] == 'gap']
    assert {g[0] for g in gaps} == {'system_metrics', 'user_metrics'}
    finish_backfill()
    assert_rollups_match(path)
    assert_rollups_match(path, ('sessions',))
    assert backups(path) == []


# =========================================================================== #
# Retention (archive_metrics.run_history_retention)                           #
# =========================================================================== #

def _store_without_rollups(data_dir, msgs):
    """Rows stored by a Manager without rollups, so the next open queues a backfill."""
    store = data_store.TypedDataStore(data_dir)
    for m in msgs:
        h, d = m['header'], m['data']
        store.store_message(h['device_name'], h.get('msg_id'), h['timestamp'], 'NAS', d['data_type'],
                            {k: v for k, v in d.items() if k != 'data_type'})
    store.close()


def _history_msgs(days_ago_list, name='Triton'):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = []
    for i, days in enumerate(days_ago_list):
        ts = format_timestamp(now - timedelta(days=days, minutes=i))
        out.append(make_msg('system_metrics', dict(metrics_payload(cpu=float(i)), f_extra=float(i * 2)), ts=ts, name=name))
        out.append(make_msg('user_metrics', {'username': 'alice', 'cpu_percent': float(i), 'ram_bytes': i,
                                             'ram_formatted': 'x', 'cpu_seconds': float(i)}, ts=ts, name=name))
    return out


def test_retention_finishes_rollups_freezes_then_prunes(make_client, data_dir):
    msgs = _history_msgs([400, 399, 398, 10, 5, 1])
    _store_without_rollups(data_dir, msgs)
    client = make_client()
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert {p[0] for p in pending(path)} == {'system_metrics', 'user_metrics'}
    full = {(t, lv): brute_force(path, t, lv) for t in ('system_metrics', 'user_metrics') for lv in ('hour', 'day')}
    result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    cutoff = result['cutoff']
    assert result['archived'] == {'triton/system_metrics': 3, 'triton/user_metrics': 3}
    assert pending(path) == []
    con = _connect(path)
    assert {r[0]: r[1] for r in con.execute('SELECT tbl, frozen_before FROM _rollup_state')} == \
        {'system_metrics': cutoff, 'user_metrics': cutoff}
    con.close()
    # rollups still hold the pruned rows (kept forever)
    for key, want in full.items():
        assert_same(rolled(path, *key), want, key)
    archives = os.listdir(os.path.join(data_dir, 'triton', 'archive'))
    assert all(a.endswith(('-system_metrics.jsonl', '-user_metrics.jsonl')) for a in archives), archives
    # a pruned message replayed with its msg_id is stored again (its id is gone) but not rolled up twice
    replay = [m for m in msgs[:2]]
    assert post_all(client, replay) == ['stored', 'stored']
    for key, want in full.items():
        assert_same(rolled(path, *key), want, ('after replay',) + key)
    # the next run prunes them again
    archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    for key, want in full.items():
        assert_same(rolled(path, *key), want, ('after second run',) + key)


def test_retention_skips_a_table_whose_rollups_are_incomplete(make_client, data_dir, monkeypatch, caplog):
    _store_without_rollups(data_dir, _history_msgs([400, 399, 2]))
    make_client()
    path = os.path.join(data_dir, 'triton', 'data.db')
    monkeypatch.setattr(mgr._data_store, 'run_rollup_backfill', lambda *a, **kw: False)
    with caplog.at_level(logging.WARNING):
        result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    assert result['archived'] == {}
    assert sum(1 for r in caplog.records if 'rollups are not complete' in r.getMessage()) == 2
    con = _connect(path)
    assert con.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0] == 3
    assert {r[0] for r in con.execute('SELECT frozen_before FROM _rollup_state')} == {None}
    con.close()
    # the central user_metrics_history is pruned without this guard (it has no rollups)
    monkeypatch.undo()
    result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    assert result['archived']['triton/system_metrics'] == 2


# =========================================================================== #
# Rebuild CLI, read helper, status endpoint, config                           #
# =========================================================================== #

def test_rebuild_cli_and_frozen_buckets(client, data_dir, capsys):
    post_all(client, [seed_message()] + random_messages(seed=81, n=60))
    path = os.path.join(data_dir, 'triton', 'data.db')
    mgr._close_stores()
    cfg = os.path.join(os.path.dirname(data_dir), 'config.json')
    with open(cfg, 'w') as f:
        json.dump(base_config(data_dir), f)
    con = sqlite3.connect(path)
    with con:                                   # damage the rollups: a rebuild repairs them
        con.execute('UPDATE _rollup_hour SET n = n + 5')
    con.close()
    assert rollups.main(['--config', cfg, '--rebuild', '--system', 'Triton']) == 0
    assert 'system_metrics: re-queued ids' in capsys.readouterr().out
    assert_rollups_match(path)
    # with pruned history (frozen_before set) the buckets before it are kept, the rest rebuilt
    frozen = '2026-08-05T06:30:00.000000Z'
    keep_day = 1785888000          # 2026-08-06T00:00Z: frozen_before rounded up to a UTC day
    con = sqlite3.connect(path)
    with con:
        con.execute("UPDATE _rollup_state SET frozen_before = ? WHERE tbl = 'system_metrics'", (frozen,))
        con.execute('UPDATE _rollup_hour SET n = n + 1000 WHERE bucket < ? AND series_id IN '
                    "(SELECT id FROM _rollup_series WHERE data_type = 'system_metrics')", (keep_day,))
    con.close()
    before = rolled(path, 'system_metrics', 'hour')
    assert rollups.main(['--config', cfg, '--rebuild', '--data-type', 'system_metrics']) == 0
    assert 'buckets before 2026-08-06T00:00:00.000000Z kept' in capsys.readouterr().out
    after = rolled(path, 'system_metrics', 'hour')
    assert {k: v for k, v in after.items() if k[2] < keep_day} == {k: v for k, v in before.items() if k[2] < keep_day}
    want = {k: v for k, v in brute_force(path, 'system_metrics', 'hour').items() if k[2] >= keep_day}
    assert_same({k: v for k, v in after.items() if k[2] >= keep_day}, want)
    con = _connect(path)
    assert con.execute("SELECT frozen_before FROM _rollup_state WHERE tbl = 'system_metrics'").fetchone()[0] \
        == '2026-08-06T00:00:00.000000Z'
    con.close()
    assert rollups.main(['--config', cfg, '--status']) == 0
    assert json.loads(capsys.readouterr().out)['triton']['system_metrics']['complete'] is True


def test_read_series_merges_buckets(client, data_dir):
    msgs = [make_msg('system_metrics', {'cpu_percent': float(v)}, ts=f'2026-08-04T{h:02d}:{m:02d}:00.000000Z')
            for h, m, v in ((0, 10, 10), (0, 50, 20), (1, 5, 30), (2, 0, 40), (3, 30, 50), (5, 59, 60))]
    post_all(client, msgs)
    db = mgr._data_store._get_db('Triton')
    start = datetime(2026, 8, 4, 0, 30, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc).timestamp()
    with db.lock:
        s = rollups.find_series(db.conn, 'system_metrics', 'CPU_PERCENT')
        assert s['field'] == 'cpu_percent' and s['unit'] is None
        pts = rollups.read_series(db.conn, s['id'], 'hour', start, end, max_points=2)
        assert rollups.find_series(db.conn, 'system_metrics', 'nope') is None
    # 5 buckets (00:00 .. 04:00, the partial first included) merged 3 at a time
    assert [(p['value'], p['min'], p['max'], p['count']) for p in pts] == [(25.0, 10.0, 40.0, 4), (50.0, 50.0, 50.0, 1)]
    # n-weighted bucket centre: (2 x 00:30 + 01:30 + 02:30) / 4
    assert pts[0]['timestamp'] == '2026-08-04T01:15:00.000000Z'
    assert pts[1]['timestamp'] == '2026-08-04T03:30:00.000000Z'
    with db.lock:
        day = rollups.read_series(db.conn, s['id'], 'day', start, end, max_points=10)
    assert day == [{'timestamp': '2026-08-04T05:00:00.000000Z', 'value': 35.0, 'min': 10.0, 'max': 60.0, 'count': 6}]


def test_rollup_status_endpoint(client, data_dir):
    post_all(client, [seed_message()] + random_messages(seed=91, n=10))
    r = client.get('/api/rollups/status', headers=auth(READ))
    assert r.status_code == 200
    st = r.get_json()['triton']
    assert set(st) == {'system_metrics', 'user_metrics'}
    assert set(st['system_metrics']) == {'pending_ids', 'done_ids', 'frozen_before', 'series', 'complete',
                                         'last_live_id'}
    assert st['system_metrics']['complete'] is True and st['system_metrics']['done_ids'] == 11
    assert client.get('/api/rollups/status', headers=auth(COLL_TRITON)).status_code == 403
    assert client.get('/api/rollups/status').status_code == 401


def test_rollup_and_usage_cache_settings_never_refuse_to_start(make_client, data_dir, caplog):
    assert rollups.settings_from_config({}) == rollups.DEFAULTS
    good = {'enabled': True, 'backfill_batch_ids': 500, 'backfill_max_duty': 1, 'max_series_per_table': 10}
    assert rollups.settings_from_config({'rollups': good}) == dict(good, backfill_max_duty=1.0)
    bad = {'enabled': 'yes', 'backfill_batch_ids': 0, 'backfill_max_duty': 1.5, 'max_series_per_table': True,
           'colour': 'blue'}
    with caplog.at_level(logging.WARNING):
        mgr.validate_config(base_config(data_dir, rollups=bad, usage_cache_seconds=-1), log)
        mgr.validate_config(base_config(data_dir, rollups='on', usage_cache_seconds='300'), log)
    text = caplog.text
    for key in ('rollups.enabled', 'rollups.backfill_batch_ids', 'rollups.backfill_max_duty',
                'rollups.max_series_per_table', 'rollups.colour', "'rollups' must be an object", 'usage_cache_seconds'):
        assert key in text, key
    assert rollups.settings_from_config({'rollups': bad}) == rollups.DEFAULTS
    assert mgr.usage_cache_seconds({'usage_cache_seconds': -1}) == 300
    assert mgr.usage_cache_seconds({'usage_cache_seconds': 0}) == 0
    assert mgr.usage_cache_seconds({}) == 300
    client = make_client(rollups=bad, usage_cache_seconds=12.5)
    assert mgr._usage_cache_seconds == 12.5
    assert mgr._data_store.rollups is not None and mgr._data_store.rollups.batch_ids == 2000
    assert _statuses(post_queue(client, [seed_message()], token=ADMIN)) == ['stored']
    r = subprocess.run([sys.executable, str(MANAGER_DIR / 'manager.py'), '--config', _write_cfg(
        data_dir, rollups=bad), '--check-config'], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and 'Config OK' in r.stdout, r.stderr


def _write_cfg(data_dir, **overrides):
    path = os.path.join(os.path.dirname(data_dir), f'cfg-{uuid.uuid4().hex[:6]}.json')
    with open(path, 'w') as f:
        json.dump(base_config(data_dir, **overrides), f)
    return path


def test_status_cli_only_reads_and_rebuild_refuses_an_unmigrated_data_dir(old_fixture, make_client, data_dir,
                                                                          capsys):
    # e.g. run from the new checkout before the switch, while the OLD Manager still serves this data_dir
    systems = _copy_fixture(old_fixture, data_dir)
    cfg = _write_cfg(data_dir)

    def layout():
        return {s: (tables_of(os.path.join(data_dir, s, 'data.db')), backups(os.path.join(data_dir, s, 'data.db')))
                for s in systems}
    before = layout()
    assert rollups.main(['--config', cfg, '--status']) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert rollups.main(['--config', cfg, '--rebuild']) == 1
    assert 'not migrated by this Manager version yet' in capsys.readouterr().out
    assert rollups.main(['--config', cfg, '--rebuild', '--system', 'Atlantis']) == 1
    assert layout() == before and not glob.glob(os.path.join(data_dir, '*.bak'))
    # once this Manager has opened them: the same JSON as /api/rollups/status, and a rebuild works
    client = make_client()
    mgr._close_stores()
    capsys.readouterr()
    assert rollups.main(['--config', cfg, '--status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert set(status) == {s for s in systems if 'system_metrics' in tables_of(os.path.join(data_dir, s, 'data.db'))}
    assert rollups.main(['--config', cfg, '--rebuild', '--system', 'Atlantis', '--data-type', 'system_metrics']) == 0
    capsys.readouterr()
    assert rollups.main(['--config', cfg, '--status']) == 0
    status = json.loads(capsys.readouterr().out)
    client = make_client()
    assert client.get('/api/rollups/status', headers=auth(READ)).get_json() == status
    assert status['atlantis']['system_metrics']['complete'] is True
    assert_rollups_match(os.path.join(data_dir, 'atlantis', 'data.db'), ('system_metrics',))


def test_backfill_thread_starts_with_the_manager_and_stops(make_client, data_dir):
    _store_without_rollups(data_dir, random_messages(seed=95, n=30))
    make_client()
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert pending(path)
    thread = mgr.start_rollup_backfill_thread(base_config(data_dir), log)
    assert thread is mgr._rollup_backfill and thread.thread.is_alive()
    deadline = time.monotonic() + 30
    while pending(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pending(path) == []
    assert_rollups_match(path)
    mgr._close_stores()                         # init_app / shutdown stop it
    thread.thread.join(5)
    assert not thread.thread.is_alive() and mgr._rollup_backfill is None
    mgr.init_app(base_config(data_dir, rollups={'enabled': False}), log)
    assert mgr.start_rollup_backfill_thread(base_config(data_dir), log) is None


def test_deleting_a_system_with_a_pending_backfill(make_client, data_dir, caplog):
    _store_without_rollups(data_dir, random_messages(seed=97, n=20))
    client = make_client()
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert pending(path)
    thread = rollups.BackfillThread(mgr._data_store)
    db = mgr._data_store._get_db('Triton')
    real = mgr._data_store.run_rollup_backfill

    def delete_first(system, *a, **kw):
        # DELETE /api/system/<name> lands while this pass holds the connection
        assert client.delete('/api/system/Triton', headers=auth(ADMIN)).status_code == 200
        return mgr._data_store.rollups.run_backfill(db, None)

    mgr._data_store.run_rollup_backfill = delete_first
    try:
        with caplog.at_level(logging.ERROR, logger='rollups'):
            thread.run_once()
    finally:
        mgr._data_store.run_rollup_backfill = real
    assert not os.path.exists(path)
    assert not [r for r in caplog.records if r.name == 'rollups']
    assert client.get('/api/rollups/status', headers=auth(READ)).get_json() == {}


def test_backfill_runs_of_one_database_do_not_overlap(data_dir):
    """The background thread and a retention run share the duty cycle instead of doubling it."""
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_rows(path, 6000)
    engine = rollups.RollupEngine({'backfill_batch_ids': 300})
    store = data_store.TypedDataStore(data_dir, rollups=engine)
    active, overlaps, real = [0], [0], engine._run_backfill

    def watched(*a, **kw):
        active[0] += 1
        overlaps[0] = max(overlaps[0], active[0])
        try:
            return real(*a, **kw)
        finally:
            active[0] -= 1

    engine._run_backfill = watched
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(store.run_rollup_backfill('triton')))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert results == [True, True] and overlaps[0] == 1
        # a caller whose deadline passes while waiting gives up (retention then skips the table)
        lock = engine._runs[os.path.join(data_dir, 'triton', 'data.db')]
        with lock:
            assert engine.run_backfill(store._get_db('triton'), deadline=time.monotonic() + 0.5) is False
    finally:
        store.close()
    assert_rollups_match(path, ('system_metrics',))


def test_sessions_and_storage_health_roll_up_as_registered(client, data_dir):
    from test_manager_new_types import A3_SESSIONS_FULL, A3_SESSIONS_UNCHANGED, A4_STORAGE_HEALTH
    msgs = []
    for i in range(6):
        for payload in (A3_SESSIONS_FULL if i == 0 else A3_SESSIONS_UNCHANGED, A4_STORAGE_HEALTH):
            data = {k: v for k, v in payload.items() if k != 'data_type'}
            data.update({'smb_clients': i} if payload['data_type'] == 'sessions' else {'disk_sata3_io_errors': i * 2})
            msgs.append(make_msg(payload['data_type'], data, ts=f'2026-11-2{i % 2}T1{i}:00:00.000000Z'))
    assert set(post_all(client, msgs)) == {'stored'}
    path = os.path.join(data_dir, 'triton', 'data.db')
    assert_rollups_match(path, ('sessions', 'storage_health'))
    sessions = {f for _, f, _ in rolled(path, 'sessions', 'hour')}
    assert {'clients_total', 'smb_clients', 'ssh_users', 'rsync_clients'} <= sessions
    assert not sessions & {'sessions_truncated', '_collect_seconds', '_schema', 'sessions'}
    health = {f for _, f, _ in rolled(path, 'storage_health', 'day')}
    assert health == {'overall_status_code', 'disk_sata3_status_code', 'disk_sata3_io_errors', 'array_md2_status_code',
                      'array_md2_degraded', 'array_md2_sync_percent', 'volume_volume1_status_code'}
    assert rolled(path, 'storage_health', 'hour') == {}
    con = _connect(path)
    units = {r['field']: r['unit'] for r in con.execute("SELECT field, unit FROM _rollup_series WHERE data_type = 'sessions'")}
    con.close()
    assert units['smb_clients'] == 'count'            # the message-level _unit


# =========================================================================== #
# Read path: /api/history resolution planner (B7)                             #
# =========================================================================== #

P0 = datetime(2025, 1, 1, tzinfo=timezone.utc)          # 12 days of 5-minute rows from here
P_ROWS = 12 * 288
OLD_HISTORY_KEYS = {'system_name', 'data_type', 'field', 'unit', 'data', 'truncated', 'downsampled'}
NEW_HISTORY_KEYS = {'entity', 'resolution', 'bucket_seconds', 'downsampled_from'}


def _bulk_users(path, n, start=P0, step=300, seed=9):
    """CURRENT-layout user_metrics rows for alice (cpu 0-10) and bob (cpu 100-110), written directly."""
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS user_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp DATETIME NOT NULL, device_type TEXT DEFAULT 'unknown', msg_id TEXT, "
                "username TEXT, cpu_percent REAL)")
    con.execute('CREATE INDEX IF NOT EXISTS idx_user_metrics_ts ON user_metrics (timestamp DESC)')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_user_metrics_msg_id ON user_metrics (msg_id)')
    rows = []
    for i in range(n):
        ts = format_timestamp(start + timedelta(seconds=step * i))
        rows.append((ts, uuid.uuid4().hex, 'alice', rnd.choice([rnd.random() * 10, None])))
        rows.append((ts, uuid.uuid4().hex, 'bob', 100 + rnd.random() * 10))
    con.executemany('INSERT INTO user_metrics (timestamp, msg_id, username, cpu_percent) VALUES (?, ?, ?, ?)', rows)
    con.commit()
    con.close()


@pytest.fixture
def planner_client(make_client, data_dir):
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_rows(path, P_ROWS, start=P0)
    _bulk_users(path, P_ROWS)
    client = make_client()
    finish_backfill()
    # registered centrally, like any system that sent data
    assert _statuses(post_queue(client, [make_msg('folder_usage', {'/volume1': 1}, ts='2025-02-01T00:00:00Z')],
                                token=ADMIN)) == ['stored']
    return client


def _raw_values(path, table, field, start, end, entity=None):
    con = _connect(path)
    try:
        sql = (f'SELECT timestamp, "{field}" FROM {table} WHERE timestamp >= ? AND timestamp < ? '
               f"AND typeof(\"{field}\") IN ('integer', 'real')")
        params = [start, end]
        if entity is not None:
            sql += ' AND username = ?'
            params.append(entity)
        return [(r[0], r[1]) for r in con.execute(sql + ' ORDER BY timestamp', params)]
    finally:
        con.close()


def _history(client, table, field, **params):
    r = client.get(f'/api/history/Triton/{table}/{field}', query_string=params, headers=auth(READ))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _iso(days):
    return format_timestamp(P0 + timedelta(days=days))


PLANNER_CASES = [
    # (name, params, expected resolution, downsampled, bucket_seconds)
    ('tail', {'limit': 50}, 'raw', False, None),
    ('one day, 2000 points', {'from': _iso(1), 'to': _iso(2), 'max_points': 2000}, 'raw', False, None),
    # f0 is a number in about half the rows: 12 days hold ~1700 values, ~1000 s per point
    ('12 days, 1000 points', {'from': _iso(0), 'to': _iso(12), 'max_points': 1000}, 'raw', True, 1037),
    ('12 days, 100 points', {'from': _iso(0), 'to': _iso(12), 'max_points': 100}, 'hour', True, 3 * 3600),
    ('12 days, 10 points', {'from': _iso(0), 'to': _iso(12), 'max_points': 10}, 'day', True, 2 * 86400),
    ('"All Available", 100 points', {'from': '2000-01-01T00:00:00Z', 'to': '9999-12-31T23:59:59Z',
                                     'max_points': 100}, 'hour', True, 3 * 3600),
    ('no max_points, limit 100', {'from': _iso(0), 'to': _iso(12), 'limit': 100}, 'hour', True, 3 * 3600),
    ('raw asked for', {'from': _iso(0), 'to': _iso(12), 'max_points': 10, 'resolution': 'raw'}, 'raw', True, 103560),
    ('hour asked for', {'from': _iso(1), 'to': _iso(2), 'max_points': 2000, 'resolution': 'hour'}, 'hour', True, 3600),
    ('day asked for', {'from': _iso(0), 'to': _iso(12), 'max_points': 2000, 'resolution': 'day'}, 'day', True, 86400),
]


@pytest.mark.parametrize('name, params, resolution, downsampled, bucket', PLANNER_CASES,
                         ids=[c[0] for c in PLANNER_CASES])
def test_planner_chooses_the_level_and_points_add_up(planner_client, data_dir, name, params, resolution,
                                                      downsampled, bucket):
    path = os.path.join(data_dir, 'triton', 'data.db')
    h = _history(planner_client, 'system_metrics', 'f0', **params)
    assert OLD_HISTORY_KEYS | NEW_HISTORY_KEYS <= set(h), set(h)
    assert (h['resolution'], h['downsampled'], h['entity'], h['truncated']) == (resolution, downsampled, None,
                                                                              name == 'tail')
    if resolution == 'raw' and bucket:
        assert abs(h['bucket_seconds'] - bucket) <= 60      # the span of the values found / max_points
    else:
        assert h['bucket_seconds'] == bucket
    pts = h['data']
    stamps = [p['timestamp'] for p in pts]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert len(pts) <= params.get('max_points', params.get('limit'))
    if name == 'tail':
        raw = _raw_values(path, 'system_metrics', 'f0', _iso(0), _iso(99))[-50:]
        assert [(p['timestamp'], p['value']) for p in pts] == raw
        return
    raw = _raw_values(path, 'system_metrics', 'f0', format_timestamp(parse_iso(params['from'])), params['to'])
    if not downsampled:
        assert [(p['timestamp'], p['value']) for p in pts] == raw and h['downsampled_from'] is None
        assert not [p for p in pts if set(p) != {'timestamp', 'value'}]
        return
    values = [v for _, v in raw]
    assert all(p['min'] <= p['value'] <= p['max'] and p['count'] >= 1 for p in pts)
    assert sum(p['count'] for p in pts) == len(values) == h['downsampled_from']
    assert min(p['min'] for p in pts) == min(values) and max(p['max'] for p in pts) == max(values)
    assert sum(p['value'] * p['count'] for p in pts) / len(values) == pytest.approx(sum(values) / len(values))
    if resolution in ('hour', 'day'):
        # each point merges bucket_seconds of buckets from the first one with data:
        # value = sum / n, min, max and n of exactly those raw values
        assert h['unit'] is None and h['field'] == 'f0'
        origin = max(parse_iso(params['from']), P0).timestamp()
        groups = {}
        for ts, v in raw:
            groups.setdefault(int((parse_iso(ts).timestamp() - origin) // h['bucket_seconds']), []).append(v)
        want = [(sum(vs) / len(vs), min(vs), max(vs), len(vs)) for _, vs in sorted(groups.items())]
        got = [(p['value'], p['min'], p['max'], p['count']) for p in pts]
        assert len(got) == len(want)
        for g, w in zip(got, want):
            assert g[0] == pytest.approx(w[0]) and g[1:] == w[1:]
        for p, (i, _) in zip(pts, sorted(groups.items())):
            centre = parse_iso(p['timestamp']).timestamp()
            assert origin + i * h['bucket_seconds'] <= centre <= origin + (i + 1) * h['bucket_seconds']


def parse_iso(text):
    from storage_util import parse_timestamp
    return parse_timestamp(text)


def test_planner_entity_filters_raw_and_rollups(planner_client, data_dir):
    path = os.path.join(data_dir, 'triton', 'data.db')
    for params, resolution in (({'from': _iso(1), 'to': _iso(2), 'max_points': 2000}, 'raw'),
                               ({'from': _iso(0), 'to': _iso(12), 'max_points': 100}, 'hour'),
                               ({'from': _iso(0), 'to': _iso(12), 'max_points': 5}, 'day')):
        mixed = _history(planner_client, 'user_metrics', 'cpu_percent', **params)
        alice = _history(planner_client, 'user_metrics', 'cpu_percent', entity='alice', **params)
        # every user's values mixed (as before entity existed): no rollup series, always raw
        assert (mixed['resolution'], alice['resolution']) == ('raw', resolution)
        assert alice['entity'] == 'alice' and mixed['entity'] is None and alice['unit'] == '%'
        values = [v for _, v in _raw_values(path, 'user_metrics', 'cpu_percent', params['from'], params['to'], 'alice')]
        if resolution == 'raw':
            assert [p['value'] for p in alice['data']] == values
        else:
            assert sum(p['count'] for p in alice['data']) == len(values)
            assert max(p['max'] for p in alice['data']) == max(values) < 100
            assert max(p['max'] for p in mixed['data']) > 100          # bob's values too
    # no user, a user without rows, and the argument checks
    assert _history(planner_client, 'user_metrics', 'cpu_percent', entity='nobody', **{'from': _iso(0)})['data'] == []
    for bad in ({'entity': ''}, {'entity': 'x' * 257}, {'entity': 'a\x01b'}, {'resolution': 'minute'}):
        r = planner_client.get('/api/history/Triton/user_metrics/cpu_percent', query_string=bad, headers=auth(READ))
        assert r.status_code == 400, bad
    r = planner_client.get('/api/history/Triton/system_metrics/f0', query_string={'entity': 'alice'},
                           headers=auth(READ))
    assert r.status_code == 400 and 'user_metrics' in r.get_json()['error']


def test_planner_stays_raw_while_a_backfill_is_pending(planner_client, make_client, data_dir):
    path = os.path.join(data_dir, 'triton', 'data.db')
    params = {'from': _iso(0), 'to': _iso(13), 'max_points': 10}
    assert _history(planner_client, 'system_metrics', 'f0', **params)['resolution'] == 'day'
    mgr._close_stores()
    _bulk_rows(path, 10, start=P0 + timedelta(days=12))          # stored without rollups (a downgrade gap)
    client = make_client()
    assert [p[4] for p in pending(path) if p[0] == 'system_metrics'] == ['gap']
    h = _history(client, 'system_metrics', 'f0', **params)
    assert h['resolution'] == 'raw' and h['downsampled'] is True
    finish_backfill()
    assert _history(client, 'system_metrics', 'f0', **params)['resolution'] == 'day'


def test_planner_on_a_table_with_daily_rollups_only(make_client, data_dir):
    client = make_client()
    msgs = [make_msg('storage_health', {'overall_status': 'ok', 'overall_status_code': 0,
                                        'disk_sata1_temperature_c': 30 + (i % 10)},
                     ts=format_timestamp(P0 + timedelta(hours=i))) for i in range(30 * 24)]
    post_all(client, msgs, size=200)
    month = {'from': _iso(0), 'to': _iso(30)}
    h = _history(client, 'storage_health', 'disk_sata1_temperature_c', max_points=100, **month)
    assert (h['resolution'], h['downsampled'], h['unit']) == ('raw', True, '°C')      # no hourly level
    assert _history(client, 'storage_health', 'disk_sata1_temperature_c', max_points=100, resolution='hour',
                    **month)['resolution'] == 'raw'
    day = _history(client, 'storage_health', 'disk_sata1_temperature_c', max_points=100, resolution='day', **month)
    assert day['resolution'] == 'day' and len(day['data']) == 30 and day['bucket_seconds'] == 86400
    assert _history(client, 'storage_health', 'disk_sata1_temperature_c', max_points=10, **month)['resolution'] == 'day'


def test_history_over_pruned_raw_rows_answers_from_rollups(make_client, data_dir):
    msgs = _history_msgs([400, 399, 398, 10, 5, 1])
    _store_without_rollups(data_dir, msgs)
    client = make_client()
    archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    now = datetime.now(timezone.utc)
    full = {'from': format_timestamp(now - timedelta(days=401)), 'to': format_timestamp(now)}
    raw = _history(client, 'system_metrics', 'f_extra', resolution='raw', **full)
    assert len(raw['data']) == 3                                    # the pruned rows are gone from raw
    h = _history(client, 'system_metrics', 'f_extra', **full)
    assert h['resolution'] == 'hour' and sum(p['count'] for p in h['data']) == 6
    assert h['data'][0]['timestamp'] < format_timestamp(now - timedelta(days=397))
    # a window after the cutoff is answered exactly from raw rows
    recent = _history(client, 'system_metrics', 'f_extra', **{'from': format_timestamp(now - timedelta(days=30)),
                                                             'to': format_timestamp(now)})
    assert recent['resolution'] == 'raw' and recent['data'] == raw['data']
    # entity on the pruned range: from the user's rollup series
    u = _history(client, 'user_metrics', 'cpu_seconds', entity='alice', **full)
    assert u['resolution'] == 'hour' and sum(p['count'] for p in u['data']) == 6 and u['unit'] == 's'


def _hourly_rows(path, start, hours, field='load'):
    """One system_metrics row an hour (value = its hour number), CURRENT layout, written directly."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS system_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                f"timestamp DATETIME NOT NULL, device_type TEXT DEFAULT 'unknown', msg_id TEXT, \"{field}\" REAL)")
    con.execute('CREATE INDEX IF NOT EXISTS idx_system_metrics_ts ON system_metrics (timestamp DESC)')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_system_metrics_msg_id ON system_metrics (msg_id)')
    con.executemany(f'INSERT INTO system_metrics (timestamp, msg_id, "{field}") VALUES (?, ?, ?)',
                    [(format_timestamp(start + timedelta(hours=i)), uuid.uuid4().hex, float(i)) for i in range(hours)])
    con.commit()
    con.close()


def test_rollup_points_never_lie_outside_the_values_of_the_window(make_client, data_dir):
    # Triton: hourly values at hh:10, Atlas at hh:50.  A partly filled first or
    # last bucket has its centre (hh:30) after the newest / before the oldest
    # value; verify_upgrade's "All Available" check (N17) then reported part of
    # the range as missing
    for name, minute in (('triton', 10), ('atlas', 50)):
        os.makedirs(os.path.join(data_dir, name))
        _hourly_rows(os.path.join(data_dir, name, 'data.db'), P0 + timedelta(minutes=minute), 103)
    client = make_client()
    finish_backfill()
    window = {'from': '2000-01-01T00:00:00Z', 'to': '9999-12-31T23:59:59Z'}
    cases = (('Triton', {'max_points': 50}, 'hour', 3 * 3600),       # 103 buckets: the last point is one bucket
             ('Atlas', {'max_points': 50}, 'hour', 3 * 3600),
             ('Triton', {'max_points': 200, 'resolution': 'hour'}, 'hour', 3600),   # one bucket per point
             ('Atlas', {'max_points': 200, 'resolution': 'hour'}, 'hour', 3600),
             ('Atlas', {'max_points': 200, 'resolution': 'day'}, 'day', 86400))
    for name, params, resolution, bucket in cases:
        r = client.get(f'/api/history/{name}/system_metrics/load', query_string=dict(window, **params),
                       headers=auth(READ))
        h = r.get_json()
        assert (h['resolution'], h['bucket_seconds']) == (resolution, bucket), (name, params)
        raw = _raw_values(os.path.join(data_dir, name.lower(), 'data.db'), 'system_metrics', 'load',
                          window['from'], format_timestamp(P0 + timedelta(days=30)))
        stamps = [p['timestamp'] for p in h['data']]
        assert raw[0][0] <= stamps[0] and stamps[-1] <= raw[-1][0], (name, params, stamps[:1], stamps[-1:])
        assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
        assert sum(p['count'] for p in h['data']) == len(raw)


def test_all_users_history_ignores_rows_without_a_username(make_client, data_dir):
    # rows an older Manager stored without a username (a script or another
    # sender: the OLD collector never sends one) are rolled up as a '' series;
    # the all-users history must still mix every user's values, from raw rows
    os.makedirs(os.path.join(data_dir, 'triton'))
    path = os.path.join(data_dir, 'triton', 'data.db')
    _bulk_users(path, P_ROWS)
    con = sqlite3.connect(path)
    con.executemany('INSERT INTO user_metrics (timestamp, username, cpu_percent) VALUES (?, ?, 1.0)',
                    [(format_timestamp(P0 + timedelta(days=d, minutes=1)), u) for d in range(12) for u in (None, '')])
    con.commit()
    con.close()
    client = make_client()
    finish_backfill()
    con = _connect(path)
    assert con.execute("SELECT COUNT(*) FROM _rollup_series WHERE data_type = 'user_metrics' AND entity = ''"
                       ).fetchone()[0] > 0
    con.close()
    params = {'from': _iso(0), 'to': _iso(12), 'max_points': 10}
    values = [v for _, v in _raw_values(path, 'user_metrics', 'cpu_percent', params['from'], params['to'])]
    for extra in ({}, {'resolution': 'hour'}, {'resolution': 'day'}):
        h = _history(client, 'user_metrics', 'cpu_percent', **params, **extra)
        assert (h['resolution'], h['entity']) == ('raw', None), extra
        assert sum(p['count'] for p in h['data']) == len(values) == h['downsampled_from']
        assert max(p['max'] for p in h['data']) == max(values) > 100      # bob's values are in it
    # one user's history still comes from that user's rollups
    assert _history(client, 'user_metrics', 'cpu_percent', entity='bob', **params)['resolution'] == 'day'


def test_history_from_rollups_after_retention_pruned_every_raw_row(make_client, data_dir):
    # a system silent for longer than history_retention_days: every raw row
    # is pruned, its rollups are kept and still charted
    _store_without_rollups(data_dir, _history_msgs([400, 399, 398]))
    client = make_client()
    result = archive_metrics.run_history_retention(mgr._data_store, mgr._metrics_db, data_dir, 365, log)
    assert result['archived'] == {'triton/system_metrics': 3, 'triton/user_metrics': 3}
    path = os.path.join(data_dir, 'triton', 'data.db')
    con = _connect(path)
    assert con.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0] == 0
    con.close()
    now = datetime.now(timezone.utc)
    full = {'from': format_timestamp(now - timedelta(days=401)), 'to': format_timestamp(now)}
    for extra in ({}, {'resolution': 'hour'}):
        h = _history(client, 'system_metrics', 'cpu_percent', **full, **extra)
        assert (h['resolution'], h['unit']) == ('hour', '%') and sum(p['count'] for p in h['data']) == 3
    u = _history(client, 'user_metrics', 'cpu_seconds', entity='alice', **full)
    assert u['resolution'] == 'hour' and sum(p['count'] for p in u['data']) == 3 and u['unit'] == 's'
    # raw answers (asked for, a tail query) are empty, as before rollups existed
    for params in (dict(full, resolution='raw'), {}):
        h = _history(client, 'system_metrics', 'cpu_percent', **params)
        assert h['data'] == [] and h['resolution'] == 'raw'


def test_raw_history_unit_is_the_newest_rows_as_before(client, data_dir):
    # CURRENT answered unit null when the newest row has no <field>_unit (a run
    # without disk counters); the rollup series' unit is only for rollup answers
    a = dict(metrics_payload(), disk_read_bytes_per_s=100, disk_read_bytes_per_s_unit='B/s')
    b = dict(metrics_payload(), disk_read_bytes_per_s=None)
    post_all(client, [make_msg('system_metrics', a, ts='2026-11-20T01:00:00.000000Z'),
                      make_msg('system_metrics', b, ts='2026-11-20T01:05:00.000000Z')])
    assert _history(client, 'system_metrics', 'disk_read_bytes_per_s')['unit'] is None
    window = {'from': '2026-11-20T00:00:00Z', 'to': '2026-11-21T00:00:00Z'}
    assert _history(client, 'system_metrics', 'disk_read_bytes_per_s', **window)['unit'] is None
    hourly = _history(client, 'system_metrics', 'disk_read_bytes_per_s', resolution='hour', **window)
    assert (hourly['resolution'], hourly['unit']) == ('hour', 'B/s')
