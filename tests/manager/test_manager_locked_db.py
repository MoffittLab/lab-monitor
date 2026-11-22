"""
A database another process holds locked (an open write transaction in a DB
browser, a long archive_metrics.py run): each ingest request waits at most
one busy timeout, and the requests behind it give up quickly, so a stuck
database can't tie up the Manager's threads.  Nothing is lost: the messages
are answered 'error' and stay in the collector's queue.
"""
import os
import sqlite3
import time

import pytest

import manager as mgr
import storage_util
from manager_helpers import READ, auth, make_msg, metrics_payload, post_queue
from storage_util import LockedConnection

BUSY_MS = 1500


@pytest.fixture
def short_busy(monkeypatch):
    monkeypatch.setattr(storage_util, 'BUSY_TIMEOUT_MS', BUSY_MS)


def _hold_lock(path):
    con = sqlite3.connect(path, isolation_level=None)
    con.execute('BEGIN IMMEDIATE')
    return con


def _msgs(n, minute=8):
    return [make_msg('system_metrics', metrics_payload(1000 + i, 500), ts=f'2026-08-04T{minute:02d}:00:{i:02d}.000001Z')
            for i in range(n)]


def _count_calls(monkeypatch):
    calls = []
    real = mgr._data_store.store_message

    def spy(*a, **k):
        calls.append(a[1])
        return real(*a, **k)
    monkeypatch.setattr(mgr._data_store, 'store_message', spy)
    return calls


def _timed_post(client, msgs):
    t0 = time.monotonic()
    r = post_queue(client, msgs)
    return r, time.monotonic() - t0


def test_locked_system_db_costs_one_wait_per_request(short_busy, make_client, data_dir, monkeypatch):
    client = make_client()
    post_queue(client, _msgs(1, minute=7))
    db_path = os.path.join(data_dir, 'triton', 'data.db')
    lock = _hold_lock(db_path)
    try:
        calls = _count_calls(monkeypatch)
        batch = _msgs(10)
        legacy = make_msg('system_metrics', metrics_payload(), ts='2026-08-04T08:30:00Z', msg_id=None)
        r, elapsed = _timed_post(client, batch + [legacy])
        ack = r.get_json()
        assert r.status_code == 200
        assert [x['status'] for x in ack['results']] == ['error'] * 11
        # every result carries the id the collector knows the message by
        assert [x['msg_id'] for x in ack['results']] == \
            [m['header']['msg_id'] for m in batch] + [mgr._legacy_id('Triton', legacy['header'], legacy['data'])]
        assert ack['stored'] == 0 and ack['counts']['error'] == 11
        assert len(calls) == 1                               # the rest was not attempted
        assert BUSY_MS / 1000 * 0.8 < elapsed < BUSY_MS / 1000 * 2

        # the next request, still locked, gives up quickly instead of waiting again
        r, elapsed = _timed_post(client, batch)
        assert [x['status'] for x in r.get_json()['results']] == ['error'] * 10
        assert elapsed < BUSY_MS / 1000 / 2
    finally:
        lock.execute('ROLLBACK')
        lock.close()

    # lock released: stored at once, and full waits again from now on
    r, elapsed = _timed_post(client, batch)
    assert [x['status'] for x in r.get_json()['results']] == ['stored'] * 10
    assert mgr._data_store._get_db('Triton').locked_until == 0.0
    assert len(client.get('/api/data/Triton/system_metrics?limit=100', headers=auth(READ)).get_json()['rows']) == 11


def test_locked_central_db_costs_one_wait_per_request(short_busy, make_client, data_dir, monkeypatch):
    client = make_client()
    post_queue(client, _msgs(1, minute=7))
    lock = _hold_lock(os.path.join(data_dir, 'metrics.db'))
    try:
        calls = _count_calls(monkeypatch)
        r, elapsed = _timed_post(client, _msgs(5))
        assert [x['status'] for x in r.get_json()['results']] == ['error'] * 5
        assert len(calls) == 1 and elapsed < BUSY_MS / 1000 * 2
        # the per-system database is not locked: only the central one waits briefly now
        assert mgr._data_store._get_db('Triton').locked_until == 0.0
        assert mgr._metrics_db._conn.locked_until > time.monotonic()
        r, elapsed = _timed_post(client, _msgs(5))
        assert elapsed < BUSY_MS / 1000 / 2
    finally:
        lock.execute('ROLLBACK')
        lock.close()
    # nothing half-stored: the per-system rows were rolled back with the central update
    rows = client.get('/api/data/Triton/system_metrics?limit=100', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 1
    r = post_queue(client, _msgs(5))
    assert [x['status'] for x in r.get_json()['results']] == ['stored'] * 5
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']['total_bytes_in'] == 1004


def test_locked_connection_short_waits_then_full_wait_again(short_busy, scratch, monkeypatch):
    path = os.path.join(scratch, 'x.db')
    db = LockedConnection(path)
    with db.transaction() as conn:
        conn.execute('CREATE TABLE t (v)')
    lock = _hold_lock(path)

    def attempt():
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            with db.transaction() as conn:
                conn.execute('INSERT INTO t VALUES (1)')
        return time.monotonic() - t0

    try:
        assert attempt() >= BUSY_MS / 1000 * 0.8           # full wait
        assert attempt() < BUSY_MS / 1000 / 2              # short waits ...
        assert attempt() < BUSY_MS / 1000 / 2
        db.locked_until = time.monotonic() - 1             # ... until the interval ends
        assert attempt() >= BUSY_MS / 1000 * 0.8
        # reads are not affected by the shorter wait
        assert db.query_one('PRAGMA busy_timeout')[0] == BUSY_MS
    finally:
        lock.execute('ROLLBACK')
        lock.close()
    with db.transaction() as conn:                         # free again: works within the interval
        conn.execute('INSERT INTO t VALUES (2)')
    assert db.locked_until == 0.0

    # a lock error raised by the transaction body (another database) doesn't start short waits
    with pytest.raises(sqlite3.OperationalError):
        with db.transaction():
            raise sqlite3.OperationalError('database is locked')
    assert db.locked_until == 0.0
    db.close()
