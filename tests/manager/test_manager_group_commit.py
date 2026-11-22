"""
Group commit on ingest (manager._store_messages, storage_util.CommitGroup).

One transaction per message cost two fsyncs per message (the row in data.db,
the central side effects in metrics.db): a 100-message batch made about 200
synchronous commits and, on an SSD whose flushes slow down now and then,
took seconds.  Now the messages of a request share one transaction per
database, and the invariants are:

  - a batch costs one COMMIT per database and group (bounded by
    ingest_commit_messages and INGEST_COMMIT_SECONDS);
  - what is stored is exactly what one transaction per message stores (rows,
    ids, central side effects, rollups, dead letters);
  - nothing is acknowledged before its COMMIT returned: a failed COMMIT
    answers the whole group 'error', and the retry stores everything once;
  - a message that fails is rolled back alone, as before;
  - a crash in the middle of a request loses nothing that was acknowledged;
  - one-time steps (backups, migrations, rollup setup) never run inside a
    group, a group takes its locks in one order (data.db, then metrics.db),
    and it never deadlocks with DELETE or other requests, also for the same
    system;
  - every connection syncs each COMMIT (synchronous=FULL).
"""
import collections
import copy
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import data_store
import manager as mgr
import storage_util
from manager_helpers import ADMIN, READ, auth, base_config, make_msg, metrics_payload, post_queue
from storage_util import CommitGroup, GroupBarrier, GroupCommitError, LockedConnection, format_timestamp
from test_manager_new_types import A1_SYSTEM_METRICS, A2_USER, A3_SESSIONS_FULL, A4_STORAGE_HEALTH
from test_manager_rollups import _build_current, _layout, assert_rollups_match, backups, random_messages, seed_message

HERE = Path(__file__).resolve().parent
MANAGER_DIR = HERE.parents[1] / 'manager'
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _runs(first, count, name='Triton'):
    """count collector runs of 5 messages each (system, 2 users, sessions, storage_health), like a backlog."""
    msgs = []
    for k in range(first, first + count):
        ts = format_timestamp(T0 + timedelta(seconds=300 * k))
        system = dict(copy.deepcopy(A1_SYSTEM_METRICS), network_bytes_in=10 ** 6 + 1000 * k,
                      network_bytes_out=10 ** 5 + 100 * k, uptime_seconds=10 ** 6 + 300 * k)
        alice = dict(copy.deepcopy(A2_USER), cpu_seconds=float(k))
        bob = dict(copy.deepcopy(A2_USER), username='bob', cpu_percent=float(k % 7))
        for payload in (system, alice, bob, copy.deepcopy(A3_SESSIONS_FULL), copy.deepcopy(A4_STORAGE_HEALTH)):
            msgs.append(make_msg(payload.pop('data_type'), payload, ts=ts, name=name))
    return msgs


def _statuses(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return [r['status'] for r in resp.get_json()['results']]


def _client(data_dir, **cfg):
    mgr.init_app(base_config(data_dir, **cfg), logging.getLogger('test-group-commit'))
    return mgr.app.test_client()


def _post(client, msgs, name='Triton'):
    return _statuses(post_queue(client, msgs, token=ADMIN, name=name))


def _commits(monkeypatch):
    """Counter of COMMIT statements per database file name, from now on."""
    counts = collections.Counter()
    real = LockedConnection._lock_statement

    def counting(self, sql, short_wait=False):
        result = real(self, sql, short_wait)
        if sql == 'COMMIT':
            counts[os.path.basename(self.path)] += 1
        return result
    monkeypatch.setattr(LockedConnection, '_lock_statement', counting)
    return counts


def _dump(data_dir):
    """Every row of every table of every database under data_dir, in rowid (or key) order."""
    out = {}
    for dirpath, _, files in os.walk(data_dir):
        for f in files:
            if not f.endswith('.db'):
                continue
            path = os.path.join(dirpath, f)
            con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            try:
                for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                    if table == 'dead_letter':
                        cols = [c for c in cols if c != 'received_at']      # wall-clock time
                    sel = ', '.join(f'"{c}"' for c in cols)
                    try:
                        rows = con.execute(f'SELECT {sel} FROM "{table}" ORDER BY rowid').fetchall()
                    except sqlite3.OperationalError:                  # WITHOUT ROWID: key order
                        rows = sorted(con.execute(f'SELECT {sel} FROM "{table}"').fetchall(), key=repr)
                    out[(os.path.relpath(path, data_dir), table)] = (cols, rows)
            finally:
                con.close()
    return out


def _rows(data_dir, sql, db='triton/data.db'):
    con = sqlite3.connect(os.path.join(data_dir, db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _reference(scratch, msgs_batches, label='reference', **cfg):
    """The same requests stored with one transaction per message: (statuses, dump)."""
    ref = os.path.join(scratch, label)
    client = _client(ref, ingest_commit_messages=1, **cfg)
    statuses = [_post(client, copy.deepcopy(b)) for b in msgs_batches]
    mgr._close_stores()
    return statuses, _dump(ref)


# --------------------------------------------------------------------------- #
# Commits per batch                                                           #
# --------------------------------------------------------------------------- #

def test_a_batch_costs_one_commit_per_database(client, data_dir, monkeypatch):
    assert _post(client, _runs(0, 1)) == ['stored'] * 5          # opens Triton's data.db
    commits = _commits(monkeypatch)
    assert _post(client, _runs(1, 20)) == ['stored'] * 100
    assert commits == {'data.db': 1, 'metrics.db': 1}
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(21,)]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM user_metrics_history', 'metrics.db') == [(42,)]


@pytest.mark.parametrize('cfg, seconds, want', [
    ({'ingest_commit_messages': 10}, None, 10),        # groups of 10
    ({'ingest_commit_messages': 1}, None, 100),        # one transaction per message, as before
    ({}, 0.0, 100),                                     # the time bound closes every group after one message
])
def test_group_size_and_time_bound(make_client, data_dir, monkeypatch, cfg, seconds, want):
    client = make_client(**cfg)
    if seconds is not None:
        monkeypatch.setattr(mgr, 'INGEST_COMMIT_SECONDS', seconds)
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    commits = _commits(monkeypatch)
    assert _post(client, _runs(1, 20)) == ['stored'] * 100
    assert commits == {'data.db': want, 'metrics.db': want}


def test_first_batch_of_a_new_system_opens_its_database_then_groups(client, monkeypatch):
    commits = _commits(monkeypatch)
    assert _post(client, _runs(0, 20)) == ['stored'] * 100
    # the first message alone (it creates data.db: its setup commits too), then one group
    assert commits['metrics.db'] == 2 and commits['data.db'] <= 4


# --------------------------------------------------------------------------- #
# Same data as one transaction per message                                    #
# --------------------------------------------------------------------------- #

def _mixed_requests():
    msgs = [seed_message()] + random_messages(seed=3, n=60)
    msgs += _runs(0, 6)
    dup = copy.deepcopy(msgs[5])
    bad_ts = make_msg('system_metrics', metrics_payload(), ts='not a time')
    bad_shape = {'header': {'msg_id': 'bad-shape-1', 'device_name': 'Triton'}, 'data': 'nope'}
    legacy = make_msg('system_metrics', metrics_payload(7, 7), ts='2026-08-02T00:00:00Z', msg_id=None)
    msgs[30:30] = [dup, bad_ts, bad_shape, legacy, copy.deepcopy(legacy)]
    return [msgs[i:i + 100] for i in range(0, len(msgs), 100)] + [copy.deepcopy(msgs[:40])]   # + a replay


def test_grouped_ingest_stores_exactly_what_per_message_ingest_stores(scratch, data_dir):
    requests = _mixed_requests()
    want_statuses, want = _reference(scratch, requests)
    client = _client(data_dir)
    statuses = [_post(client, copy.deepcopy(b)) for b in requests]
    assert statuses == want_statuses
    flat = [s for b in statuses for s in b]
    assert {'stored', 'duplicate', 'rejected'} <= set(flat) and 'error' not in flat
    assert_rollups_match(os.path.join(data_dir, 'triton', 'data.db'))
    mgr._close_stores()
    got = _dump(data_dir)
    assert got.keys() == want.keys()
    for key in want:
        assert got[key] == want[key], key


# --------------------------------------------------------------------------- #
# ACK only after COMMIT                                                       #
# --------------------------------------------------------------------------- #

def _flaky_commit(monkeypatch, db, failures=1, error='disk I/O error'):
    real = db._lock_statement
    left = {'n': failures}

    def flaky(sql, short_wait=False):
        if sql == 'COMMIT' and left['n']:
            left['n'] -= 1
            raise sqlite3.OperationalError(error)
        return real(sql, short_wait)
    monkeypatch.setattr(db, '_lock_statement', flaky)


def test_a_failed_commit_answers_the_whole_group_error_and_the_retry_stores_it_once(
        make_client, scratch, data_dir, monkeypatch):
    first, batch = _runs(0, 1), _runs(1, 6)
    _, want = _reference(scratch, [first, batch])
    client = make_client(ingest_commit_messages=10)
    assert _post(client, copy.deepcopy(first)) == ['stored'] * 5
    # metrics.db commits, then data.db's COMMIT fails: the side effects of the
    # group are in, its rows are not
    _flaky_commit(monkeypatch, mgr._data_store.cached_connection('Triton'))
    resp = post_queue(client, copy.deepcopy(batch), token=ADMIN)
    ack = resp.get_json()
    assert [r['status'] for r in ack['results']] == ['error'] * 30
    assert ack['stored'] == 0 and [r['msg_id'] for r in ack['results']] == [m['header']['msg_id'] for m in batch]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(1,)]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM user_metrics_history', 'metrics.db') == [(2 + 4,)]
    # the collector sends it again: everything stored once, side effects not doubled
    assert _post(client, copy.deepcopy(batch)) == ['stored'] * 30
    mgr._close_stores()
    got = _dump(data_dir)
    # The retried per-user rows were already in metrics.db: INSERT OR IGNORE skips
    # them but uses up their AUTOINCREMENT ids, so later rows' ids have a gap (as
    # when a per-message data.db COMMIT failed before).  Same rows otherwise.
    for dump in (got, want):
        cols, rows = dump.pop(('metrics.db', 'user_metrics_history'))
        dump['history without ids'] = (cols[1:], [r[1:] for r in rows])
        dump.pop(('metrics.db', 'sqlite_sequence'))
    assert got == want


def test_a_failed_metrics_commit_stores_nothing_of_the_group(make_client, data_dir, monkeypatch):
    client = make_client(ingest_commit_messages=10)
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    _flaky_commit(monkeypatch, mgr._metrics_db.connection)
    assert _post(client, _runs(1, 4)) == ['error'] * 20
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(1,)]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM user_metrics_history', 'metrics.db') == [(2,)]
    assert _post(client, _runs(1, 4)) == ['stored'] * 20


def test_groups_committed_before_a_failure_stay_acknowledged(make_client, data_dir, monkeypatch):
    client = make_client(ingest_commit_messages=10)
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    db = mgr._data_store.cached_connection('Triton')
    real = db._lock_statement
    seen = {'commits': 0}

    def second_commit_fails(sql, short_wait=False):
        if sql == 'COMMIT':
            seen['commits'] += 1
            if seen['commits'] == 2:
                raise sqlite3.OperationalError('disk I/O error')
        return real(sql, short_wait)
    monkeypatch.setattr(db, '_lock_statement', second_commit_fails)
    batch = _runs(1, 6)
    assert _post(client, copy.deepcopy(batch)) == ['stored'] * 10 + ['error'] * 20
    stored = {r[0] for r in _rows(data_dir, 'SELECT msg_id FROM system_metrics UNION ALL SELECT msg_id FROM user_metrics '
                                            'UNION ALL SELECT msg_id FROM sessions UNION ALL SELECT msg_id FROM storage_health')}
    assert {m['header']['msg_id'] for m in batch[:10]} <= stored
    assert not {m['header']['msg_id'] for m in batch[10:]} & stored
    assert _post(client, copy.deepcopy(batch)) == ['duplicate'] * 10 + ['stored'] * 20


# --------------------------------------------------------------------------- #
# A failing message inside a group                                            #
# --------------------------------------------------------------------------- #

def _failing_side_effects(monkeypatch, msg_id, make_error, rollback_data_db=False):
    real = mgr._metrics_db.apply_side_effects

    def patched(name, system_id, device_type, timestamp, data_type, payload, msg_id_=None, raw_timestamp=None):
        if msg_id_ == msg_id:
            if rollback_data_db:
                # what SQLite does to the whole transaction after SQLITE_FULL / SQLITE_IOERR
                mgr._data_store.cached_connection('Triton').conn.execute('ROLLBACK')
            raise make_error()
        return real(name, system_id, device_type, timestamp, data_type, payload, msg_id_, raw_timestamp=raw_timestamp)
    monkeypatch.setattr(mgr._metrics_db, 'apply_side_effects', patched)


def test_a_message_that_fails_is_rolled_back_alone(make_client, scratch, data_dir, monkeypatch):
    first, batch = _runs(0, 1), _runs(1, 4)
    bad = batch[7]['header']['msg_id']
    ref = os.path.join(scratch, 'ref')
    for d in (ref, data_dir):
        client = _client(d, ingest_commit_messages=1 if d == ref else 100)
        assert _post(client, copy.deepcopy(first)) == ['stored'] * 5
        _failing_side_effects(monkeypatch, bad, lambda: ValueError('boom'))
        assert _post(client, copy.deepcopy(batch)) == ['stored'] * 7 + ['rejected'] + ['stored'] * 12
        monkeypatch.undo()
        mgr._close_stores()
    assert _dump(data_dir) == _dump(ref)
    assert _rows(data_dir, f"SELECT COUNT(*) FROM user_metrics WHERE msg_id = '{bad}'") == [(0,)]
    assert _rows(data_dir, f"SELECT reason FROM dead_letter WHERE msg_id = '{bad}'", 'metrics.db') == \
        [('storage failed (ValueError): boom',)]


def test_a_storage_failure_keeps_the_messages_before_it(make_client, data_dir, monkeypatch):
    client = make_client()
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    batch = _runs(1, 4)
    _failing_side_effects(monkeypatch, batch[5]['header']['msg_id'],
                          lambda: sqlite3.OperationalError('database is locked'))
    assert _post(client, copy.deepcopy(batch)) == ['stored'] * 5 + ['error'] * 15
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(2,)]
    monkeypatch.undo()
    assert _post(client, copy.deepcopy(batch)) == ['duplicate'] * 5 + ['stored'] * 15


def test_a_transaction_rolled_back_by_sqlite_stores_nothing_of_the_group(make_client, scratch, data_dir,
                                                                          monkeypatch):
    first, batch = _runs(0, 1), _runs(1, 4)
    _, want = _reference(scratch, [first, batch])
    client = make_client()
    assert _post(client, copy.deepcopy(first)) == ['stored'] * 5
    _failing_side_effects(monkeypatch, batch[6]['header']['msg_id'],
                          lambda: sqlite3.OperationalError('database or disk is full'), rollback_data_db=True)
    # the messages before it were in the rolled-back transaction: none of them is acknowledged
    assert _post(client, copy.deepcopy(batch)) == ['error'] * 20
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(1,)]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM user_metrics_history', 'metrics.db') == [(2,)]
    monkeypatch.undo()
    assert _post(client, copy.deepcopy(batch)) == ['stored'] * 20
    mgr._close_stores()
    assert _dump(data_dir) == want


# --------------------------------------------------------------------------- #
# Crash in the middle of a request                                            #
# --------------------------------------------------------------------------- #

_CRASH_SCRIPT = r'''
import json, logging, os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import manager as mgr
from manager_helpers import ADMIN, base_config, post_queue
data_dir, requests, acks, crash_at = sys.argv[3], json.load(open(sys.argv[4])), sys.argv[5], int(sys.argv[6])
mgr.init_app(base_config(data_dir, ingest_commit_messages=10), logging.getLogger('crash'))
client = mgr.app.test_client()
real, calls = mgr._process_message, [0]

def process(*a, **k):
    calls[0] += 1
    if calls[0] == crash_at:
        os._exit(17)                 # killed between two messages, mid-group
    return real(*a, **k)
mgr._process_message = process
with open(acks, 'w') as f:
    for body in requests:
        r = post_queue(client, body, token=ADMIN)
        f.write(json.dumps(r.get_json()) + '\n')
        f.flush()
        os.fsync(f.fileno())
'''


def test_a_crash_between_messages_loses_nothing_acknowledged(scratch, data_dir):
    requests = [_runs(0, 6), _runs(6, 6)]                   # 30 + 30 messages, groups of 10
    req_path, acks, script = (os.path.join(scratch, n) for n in ('requests.json', 'acks.jsonl', 'crash.py'))
    with open(req_path, 'w') as f:
        json.dump(requests, f)
    with open(script, 'w') as f:
        f.write(_CRASH_SCRIPT)
    crash_at = 30 + 25                                       # the 5th message of the second request's 3rd group
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    p = subprocess.run([sys.executable, script, str(MANAGER_DIR), str(HERE), data_dir, req_path, acks, str(crash_at)],
                       capture_output=True, timeout=120, env=env)
    assert p.returncode == 17, p.stderr.decode()[-2000:]
    answered = [json.loads(line) for line in open(acks)]
    assert len(answered) == 1 and answered[0]['stored'] == 30   # the second request was never answered

    client = _client(data_dir)
    assert _rows(data_dir, 'PRAGMA integrity_check') == [('ok',)]
    stored = {r[0] for t in ('system_metrics', 'user_metrics', 'sessions', 'storage_health')
              for r in _rows(data_dir, f'SELECT msg_id FROM {t}')}
    assert {r['msg_id'] for r in answered[0]['results']} <= stored
    second = [m['header']['msg_id'] for m in requests[1]]
    assert set(second[:20]) <= stored and not set(second[20:]) & stored    # committed groups only
    # the collector sends the unanswered request again
    assert _post(client, requests[1]) == ['duplicate'] * 20 + ['stored'] * 10
    assert _rows(data_dir, 'SELECT COUNT(*) FROM user_metrics_history', 'metrics.db') == [(24,)]


# --------------------------------------------------------------------------- #
# One-time steps, locks, concurrency                                          #
# --------------------------------------------------------------------------- #

def test_backups_and_migrations_run_outside_any_group(make_client, data_dir, monkeypatch):
    """A CURRENT database with folder_usage only gets its rollup-tables backup in the middle of a batch."""
    folders = [make_msg('folder_usage', {'/volume1/Lab': 100 + i, '_unit': 'bytes'},
                        ts=f'2026-08-0{i + 1}T02:00:00.000000Z') for i in range(3)]
    _build_current(data_dir, {'Triton': folders})
    path = os.path.join(data_dir, 'triton', 'data.db')
    before = _layout(path)
    client = make_client()
    assert not backups(path)
    seen = []
    real = data_store.backup_database

    def spy(conn, db_path, *a, **k):
        seen.append((storage_util.current_group(), conn.in_transaction))
        if seen[-1] != (None, False):
            # it would copy uncommitted rows (and the online backup of a connection
            # inside its own write transaction never finishes)
            raise OSError('backup inside an open transaction')
        return real(conn, db_path, *a, **k)
    monkeypatch.setattr(data_store, 'backup_database', spy)
    commits = _commits(monkeypatch)
    batch = [make_msg('folder_usage', {'/volume1/Lab': 200 + i, '_unit': 'bytes'},
                      ts=f'2026-08-1{i}T02:00:00.000000Z') for i in range(2)] + _runs(0, 10)
    assert _post(client, batch) == ['stored'] * 52
    assert seen == [(None, False)]                       # one backup, no group and no transaction open
    (bak,) = backups(path)
    assert _layout(bak) == before
    con = sqlite3.connect(bak)
    try:
        # the backup holds what was committed before it: the 3 old runs and the 2 of this batch
        assert con.execute('SELECT COUNT(*) FROM folder_usage_msgs').fetchone() == (5,)
    finally:
        con.close()
    # group, the system_metrics message alone (backup, rollup tables), group again
    assert commits['metrics.db'] == 3
    assert_rollups_match(path)


class _BoundedLock:
    """A Lock whose `with` gives up after 5 s (AssertionError) instead of deadlocking the test run."""

    def __init__(self):
        self._lock = threading.Lock()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        if not self._lock.acquire(timeout=5):
            raise AssertionError('waited for the cache lock: this would deadlock')
        return self

    def __exit__(self, *exc):
        self._lock.release()


def test_get_db_inside_a_group_never_waits_for_the_cache_lock(client, monkeypatch):
    """delete_system() holds the cache lock while it waits for the database's lock (held by the group)."""
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    store, central = mgr._data_store, mgr._metrics_db.connection
    monkeypatch.setattr(store, '_cache_lock', _BoundedLock())
    db = store.cached_connection('Triton')
    deleter = threading.Thread(target=store.delete_system, args=('Triton',), daemon=True)
    with CommitGroup([central, db], lock_order=[db, central]) as group:
        with db.transaction() as conn:                   # the group holds db's lock from here
            conn.execute("SELECT 1")
        deleter.start()
        deadline = time.monotonic() + 10
        while not store._cache_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert store._cache_lock.locked() and deleter.is_alive()
        assert store._get_db('Triton', create=True) is db        # would deadlock through the cache lock
        group.commit()
    deleter.join(10)
    assert not deleter.is_alive()


def test_concurrent_requests_and_a_delete_never_deadlock(client, monkeypatch):
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    assert _post(client, _runs(0, 1, name='Atlas'), name='Atlas') == ['stored'] * 5
    real = mgr._process_message

    def slow(*a, **k):
        time.sleep(0.0005)
        return real(*a, **k)
    monkeypatch.setattr(mgr, '_process_message', slow)
    errors, results = [], collections.Counter()

    def ingest(name, first):
        try:
            c = mgr.app.test_client()
            for k in range(first, first + 60, 20):
                for s in _statuses(post_queue(c, _runs(k, 20, name=name), token=ADMIN, name=name)):
                    results[name, s] += 1
        except Exception as e:      # pragma: no cover - reported below
            errors.append(e)

    def delete():
        time.sleep(0.05)
        r = mgr.app.test_client().delete('/api/system/Atlas', headers=auth(ADMIN))
        results['delete', r.status_code] += 1

    threads = [threading.Thread(target=ingest, args=('Triton', 1)), threading.Thread(target=ingest, args=('Atlas', 1)),
               threading.Thread(target=delete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads), 'deadlock'
    assert not errors and results['delete', 200] == 1
    assert results['Triton', 'stored'] == 300
    # a message whose connection DELETE closed under it is answered 'error' (kept and retried), as before
    assert results['Atlas', 'stored'] + results['Atlas', 'error'] == 300


class _RankedLock:
    """
    A connection's RLock that records every acquisition this thread makes
    while it holds a lock of a later rank (data.db = 0, metrics.db = 1): two
    threads taking the same two locks in opposite orders can deadlock.
    Taking a lock again that the thread already holds can't wait, so it is
    not recorded.
    """
    _held = threading.local()

    def __init__(self, lock, rank, name, violations):
        self._lock, self.rank, self.name, self._violations = lock, rank, name, violations

    def acquire(self, blocking=True, timeout=-1):
        held = self._held.__dict__.setdefault('locks', [])
        if self not in held:
            later = sorted({lk.name for lk in held if lk.rank > self.rank})
            if later:
                self._violations.append(f'{self.name} taken while holding {", ".join(later)}')
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            held.append(self)
        return ok

    def release(self):
        self._lock.release()
        held = self._held.locks
        del held[len(held) - 1 - held[::-1].index(self)]

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def test_a_group_takes_data_db_before_metrics_db(client, data_dir, monkeypatch):
    """
    The per-message path holds data.db while it writes metrics.db (side
    effects inside the row's transaction).  A group keeps the locks it took,
    so it must take them in the same order even when its first message only
    writes metrics.db: a rejected message (its dead letter).
    """
    seed = _runs(0, 2)
    assert _post(client, seed) == ['stored'] * 10
    violations = []
    central, db = mgr._metrics_db.connection, mgr._data_store.cached_connection('Triton')
    monkeypatch.setattr(central, 'lock', _RankedLock(central.lock, 1, 'metrics.db', violations))
    monkeypatch.setattr(db, 'lock', _RankedLock(db.lock, 0, 'data.db', violations))
    bad = make_msg('system_metrics', metrics_payload(), ts='not-a-time')
    future = make_msg('system_metrics', metrics_payload(), ts='2099-01-01T00:00:00.000000Z')
    batches = [
        [bad] + _runs(2, 2),                                    # rejected first, then new
        [copy.deepcopy(bad), 'not a message'] + copy.deepcopy(seed[:5]),  # rejected, then duplicates
        copy.deepcopy(seed[5:]) + [future] + _runs(4, 1),       # duplicates, a future reading, new
    ]
    assert _post(client, batches[0]) == ['rejected'] + ['stored'] * 10
    assert _post(client, batches[1]) == ['rejected'] * 2 + ['duplicate'] * 5
    assert _post(client, batches[2]) == ['duplicate'] * 5 + ['stored'] * 6
    assert violations == []
    assert len(mgr._metrics_db.get_dead_letters()) == 2      # the same bad message is recorded once


_SAME_SYSTEM_SCRIPT = r'''
import faulthandler, json, logging, os, sys, threading, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import manager as mgr
from manager_helpers import ADMIN, base_config, post_queue
data_dir, (seed, resent, rejected_first), out_path = sys.argv[3], json.load(open(sys.argv[4])), sys.argv[5]
mgr.init_app(base_config(data_dir), logging.getLogger('same-system'))
logging.disable(logging.CRITICAL)
assert post_queue(mgr.app.test_client(), seed, token=ADMIN).get_json()['counts']['stored'] == len(seed)
real, me = mgr._process_message, threading.local()
holds_data_db, rejected_done = threading.Event(), threading.Event()

def process(*a, **k):
    me.calls = getattr(me, 'calls', 0) + 1
    if me.role == 'rejected' and me.calls == 1:
        holds_data_db.wait(10)
    result = real(*a, **k)
    if me.role == 'resent' and me.calls == 1:
        # a duplicate: this request's group holds data.db now.  With the
        # bug the other request's dead letter takes metrics.db meanwhile;
        # with the fix it waits for data.db, and this wait times out.
        holds_data_db.set()
        rejected_done.wait(1)
    elif me.role == 'rejected' and me.calls == 1:
        rejected_done.set()
    return result
mgr._process_message = process
out = {}

def run(role, msgs):
    me.role = role
    out[role] = post_queue(mgr.app.test_client(), msgs, token=ADMIN).get_json()['counts']
threads = [threading.Thread(target=run, args=a, daemon=True)
           for a in (('resent', resent), ('rejected', rejected_first))]
for t in threads:
    t.start()
deadline = time.monotonic() + 30
for t in threads:
    t.join(max(0.0, deadline - time.monotonic()))
if any(t.is_alive() for t in threads):
    faulthandler.dump_traceback(all_threads=True)
    os._exit(3)
with open(out_path, 'w') as f:
    json.dump(out, f)
mgr._close_stores()
'''


def test_two_requests_for_the_same_system_never_deadlock(scratch, data_dir):
    """
    A collector's retry after its timeout (or a sensor script posting under
    its collector's name) can reach the Manager while the first request is
    still running: two groups for one data.db.  One starts with duplicates
    (its group holds data.db), the other with a rejected message (its dead
    letter writes metrics.db).  Taking the locks in the order the blocks use
    them hung both threads for good, and with them every later ingest and
    every read of metrics.db.  Run in a subprocess, so that a deadlock fails
    this test instead of hanging the test run.
    """
    seed = _runs(0, 4)
    requests = [seed, copy.deepcopy(seed) + _runs(4, 4),
                [make_msg('system_metrics', metrics_payload(), ts='not-a-time')] + _runs(8, 4)]
    req_path, out_path, script = (os.path.join(scratch, n) for n in ('requests.json', 'out.json', 'same.py'))
    with open(req_path, 'w') as f:
        json.dump(requests, f)
    with open(script, 'w') as f:
        f.write(_SAME_SYSTEM_SCRIPT)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    p = subprocess.run([sys.executable, script, str(MANAGER_DIR), str(HERE), data_dir, req_path, out_path],
                       capture_output=True, timeout=120, env=env)
    assert p.returncode == 0, 'deadlock:\n' + p.stderr.decode()[-4000:]
    with open(out_path) as f:
        out = json.load(f)
    assert out == {'resent': {'stored': 20, 'duplicate': 20, 'rejected': 0, 'error': 0},
                   'rejected': {'stored': 20, 'duplicate': 0, 'rejected': 1, 'error': 0}}
    assert _rows(data_dir, 'SELECT COUNT(*) FROM system_metrics') == [(12,)]
    assert _rows(data_dir, 'SELECT COUNT(*) FROM dead_letter', 'metrics.db') == [(1,)]


def test_readers_wait_for_the_group_and_never_see_part_of_it(client, data_dir, monkeypatch):
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    real = mgr._process_message
    calls, seen = [0], []
    reader = threading.Thread(target=lambda: seen.append(len(mgr.app.test_client().get(
        '/api/data/Triton/system_metrics?limit=1000', headers=auth(READ)).get_json()['rows'])))

    def process(*a, **k):
        calls[0] += 1
        if calls[0] == 50:
            reader.start()
            time.sleep(0.3)
            assert reader.is_alive()             # waiting for the group's lock
        return real(*a, **k)
    monkeypatch.setattr(mgr, '_process_message', process)
    assert _post(client, _runs(1, 20)) == ['stored'] * 100
    reader.join(10)
    assert seen == [21]


def test_every_connection_syncs_each_commit(client, scratch, monkeypatch):
    assert _post(client, _runs(0, 1)) == ['stored'] * 5
    for db in (mgr._metrics_db.connection, mgr._data_store.cached_connection('Triton')):
        assert db.query_one('PRAGMA synchronous')[0] == 2          # FULL
        assert db.query_one('PRAGMA journal_mode')[0] == 'wal'
    # also on a SQLite build whose WAL connections default to NORMAL
    real = sqlite3.connect

    def normal_by_default(*a, **k):
        conn = real(*a, **k)
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn
    monkeypatch.setattr(storage_util.sqlite3, 'connect', normal_by_default)
    db = LockedConnection(os.path.join(scratch, 'normal.db'))
    try:
        assert db.query_one('PRAGMA synchronous')[0] == 2
    finally:
        db.close()


def test_ingest_commit_messages_setting(data_dir, caplog):
    log = logging.getLogger('test-group-commit')
    for bad in (0, -5, 2.5, '100', True, [10]):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            mgr.validate_config(base_config(data_dir, ingest_commit_messages=bad), log)
        assert "'ingest_commit_messages'" in caplog.text
        assert mgr.ingest_commit_messages({'ingest_commit_messages': bad}) == 100
    assert mgr.ingest_commit_messages({}) == 100
    assert mgr.ingest_commit_messages({'ingest_commit_messages': 1}) == 1
    assert mgr.ingest_commit_messages({'ingest_commit_messages': 500}) == 500


# --------------------------------------------------------------------------- #
# CommitGroup itself                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def two_dbs(scratch):
    dbs = []
    for name in ('a.db', 'b.db'):
        db = LockedConnection(os.path.join(scratch, name))
        with db.transaction() as conn:
            conn.execute('CREATE TABLE t (v)')
        dbs.append(db)
    yield dbs
    for db in dbs:
        db.close()


def _values(db):
    return [r[0] for r in db.query('SELECT v FROM t ORDER BY rowid')]


def _writable_from_another_thread(db):
    out = []

    def write():
        with db.transaction() as conn:
            conn.execute("INSERT INTO t VALUES ('other')")
        out.append(True)
    t = threading.Thread(target=write)
    t.start()
    t.join(5)
    return out == [True]


def test_blocks_are_savepoints_of_one_transaction(two_dbs):
    a, b = two_dbs
    with CommitGroup([a, b]) as group:
        with a.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(ValueError):
            with a.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (2)")
                raise ValueError('only this block is undone')
        with a.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (3)")
            with a.transaction() as inner:               # nested use joins the block
                inner.execute("INSERT INTO t VALUES (4)")
        with b.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (5)")
        assert a.conn.in_transaction and b.conn.in_transaction
        # nothing is visible to another connection before commit()
        other = sqlite3.connect(a.path)
        assert other.execute('SELECT COUNT(*) FROM t').fetchone() == (0,)
        other.close()
        group.commit()
    assert _values(a) == [1, 3, 4] and _values(b) == [5]
    assert storage_util.current_group() is None
    assert _writable_from_another_thread(a) and _writable_from_another_thread(b)


def test_a_member_is_begun_after_the_members_before_it_in_lock_order(two_dbs, monkeypatch):
    a, b = two_dbs
    real, begins = LockedConnection._lock_statement, []

    def spy(self, sql, short_wait=False):
        if sql == 'BEGIN IMMEDIATE':
            begins.append(os.path.basename(self.path))
        return real(self, sql, short_wait)
    monkeypatch.setattr(LockedConnection, '_lock_statement', spy)

    def held_elsewhere(db):
        out = []
        t = threading.Thread(target=lambda: out.append(db.lock.acquire(timeout=0.05) and db.lock.release()))
        t.start()
        t.join(5)
        return out == [False]

    with CommitGroup([a, b], lock_order=[b, a]) as group:
        with a.transaction() as conn:                    # a's first block begins b first
            conn.execute("INSERT INTO t VALUES (1)")
        assert begins == ['b.db', 'a.db'] and held_elsewhere(b) and held_elsewhere(a)
        with b.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (2)")
        assert begins == ['b.db', 'a.db']
        group.commit()
    assert _values(a) == [1] and _values(b) == [2]
    assert not held_elsewhere(a) and not held_elsewhere(b)
    # without a lock_order, the commit order is the lock order
    with CommitGroup([a, b]) as group:
        with b.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (3)")
        group.commit()
    assert begins == ['b.db', 'a.db', 'a.db', 'b.db']
    with pytest.raises(ValueError):
        CommitGroup([a, b], lock_order=[a])


def test_leaving_a_group_without_commit_rolls_back_and_releases(two_dbs):
    a, b = two_dbs
    with pytest.raises(RuntimeError):
        with CommitGroup([a, b]):
            with a.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError('request failed')
    assert _values(a) == [] and not a.conn.in_transaction
    assert storage_util.current_group() is None and _writable_from_another_thread(a)


def test_commit_order_and_a_failing_second_commit(two_dbs, monkeypatch):
    a, b = two_dbs
    real, order = LockedConnection._lock_statement, []

    def spy(self, sql, short_wait=False):
        if sql == 'COMMIT':
            order.append(os.path.basename(self.path))
            if self is a:
                raise sqlite3.OperationalError('disk I/O error')
        return real(self, sql, short_wait)
    monkeypatch.setattr(LockedConnection, '_lock_statement', spy)
    with CommitGroup([b, a]) as group:
        for db in (a, b):
            with db.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.OperationalError):
            group.commit()
    monkeypatch.undo()
    assert order == ['b.db', 'a.db']                       # members commit in the order given
    assert _values(b) == [1] and _values(a) == []
    assert not a.conn.in_transaction and _writable_from_another_thread(a) and _writable_from_another_thread(b)


def test_a_transaction_rolled_back_under_the_group_cannot_commit(two_dbs):
    a, b = two_dbs
    with CommitGroup([a, b]) as group:
        with b.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.OperationalError):
            with a.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                conn.execute('ROLLBACK')                   # as SQLite does after SQLITE_FULL / IOERR
                raise sqlite3.OperationalError('database or disk is full')
        assert group.broken
        with pytest.raises(GroupCommitError):
            with a.transaction():                          # never runs outside the transaction
                pass
        with pytest.raises(GroupCommitError):
            group.commit()
    assert _values(a) == [] and _values(b) == []
    assert storage_util.is_transient_db_error(GroupCommitError('x'))


def test_barriers(two_dbs, scratch):
    a, b = two_dbs
    c = LockedConnection(os.path.join(scratch, 'c.db'))
    try:
        with c.transaction() as conn:
            conn.execute('CREATE TABLE t (v)')
        with CommitGroup([a]) as group:
            b.group_barrier()                               # not a member: no barrier
            with pytest.raises(GroupBarrier):
                a.group_barrier('a backup')
            # a member inside an ungrouped transaction of another database would
            # commit after it: refused before anything is written
            with pytest.raises(GroupBarrier):
                with c.transaction() as conn:
                    conn.execute("INSERT INTO t VALUES ('c')")
                    with a.transaction() as inner:
                        inner.execute("INSERT INTO t VALUES ('a')")
            with pytest.raises(RuntimeError):
                with CommitGroup([b]):
                    pass
            group.commit()
        assert _values(a) == [] and _values(c) == []
    finally:
        c.close()


def test_a_busy_begin_in_a_group_waits_like_any_begin(two_dbs, monkeypatch):
    monkeypatch.setattr(storage_util, 'BUSY_TIMEOUT_MS', 300)
    a, _ = two_dbs
    a.conn.execute('PRAGMA busy_timeout = 300')
    holder = sqlite3.connect(a.path, isolation_level=None)
    holder.execute('BEGIN IMMEDIATE')
    try:
        with CommitGroup([a]) as group:
            t0 = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with a.transaction():
                    pass
            assert time.monotonic() - t0 >= 0.2
            assert a.locked_until > time.monotonic()        # the next BEGIN waits briefly
            group.commit()
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    with CommitGroup([a]) as group:
        with a.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        group.commit()
    assert a.locked_until == 0.0 and _values(a) == [1]
