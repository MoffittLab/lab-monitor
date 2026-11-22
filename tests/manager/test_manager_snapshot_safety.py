"""
Nightly snapshots (manager/snapshot.py), checked by what a run does rather
than through the module's helpers: the staged copy is written without fsync,
and pruning never deletes a snapshot this Manager did not write.
(The rest is in test_manager_snapshot.py.)
"""
import json
import os
import socket
import sqlite3
import time
import types
from pathlib import Path

import snapshot
from manager_helpers import COLL_TRITON, make_msg, metrics_payload, post_queue

# snapshot.PARTIAL_OWNER: written into a .partial folder when it is created
OWNER_FILE = 'partial-owner.json'


def _settings(dest, **over):
    s = dict(snapshot.DEFAULTS, enabled=True, dest_dir=dest)
    s.update(over)
    return s


def _seed(client):
    msgs = [make_msg('system_metrics', metrics_payload(i, i), ts=f'2026-08-04T07:{i:02d}:00.000000Z')
            for i in range(5)]
    assert post_queue(client, msgs, token=COLL_TRITON).get_json()['counts']['stored'] == 5


def test_the_staged_copy_is_not_synced_while_the_backup_writes_it(client, data_dir, scratch, monkeypatch):
    """
    With SQLite's default synchronous=FULL the backup's commit fsyncs the whole staged
    copy, and every ingest request then waits behind that disk flush.  Seen from the
    backup call itself: its target connection must have synchronous=OFF (0).
    """
    _seed(client)
    staging = os.path.join(data_dir, snapshot.STAGING_DIR)
    seen = []

    class Conn:
        """A connection whose backup() records the target's synchronous setting."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def backup(self, target, *a, **kw):
            target = target._conn if isinstance(target, Conn) else target
            path = target.execute('PRAGMA database_list').fetchone()[2]
            seen.append((os.path.relpath(path, staging).split(os.sep)[1:],
                         target.execute('PRAGMA synchronous').fetchone()[0]))
            return self._conn.backup(target, *a, **kw)

    shim = types.SimpleNamespace(**{k: getattr(sqlite3, k) for k in dir(sqlite3) if not k.startswith('__')})
    shim.connect = lambda *a, **kw: Conn(sqlite3.connect(*a, **kw))
    monkeypatch.setattr(snapshot, 'sqlite3', shim)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    monkeypatch.undo()
    assert res['status'] == 'ok', res
    assert seen == [(['triton', 'data.db'], 0), (['metrics.db'], 0)]
    assert snapshot.verify_snapshot(res['path']) == []


def test_prune_leaves_every_snapshot_this_manager_did_not_write(scratch):
    """
    A second Manager (a test one given a copy of config.json), this one before its
    data_dir moved, or a folder copied in by hand can share dest_dir: only snapshots whose
    manifest names this host and data_dir, and .partial folders with this Manager's owner
    file, are ever deleted.
    """
    dest = os.path.join(scratch, 'snaps')
    data = os.path.join(scratch, 'data')
    os.makedirs(data)
    here = socket.gethostname()

    def mk(stamp, manifest=None, owner=None, partial=False):
        p = os.path.join(dest, 'snapshot-' + stamp + ('.partial' if partial else ''))
        os.makedirs(p)
        if manifest is not None:
            Path(p, 'manifest.json').write_text(json.dumps(manifest))
        if owner is not None:
            Path(p, OWNER_FILE).write_text(json.dumps(owner))
        return os.path.basename(p)
    ours = {'host': here, 'source_data_dir': os.path.abspath(data)}
    other_host = mk('20261101T033000Z', {'host': 'another-host', 'source_data_dir': os.path.abspath(data)})
    other_dir = mk('20261102T033000Z', {'host': here, 'source_data_dir': os.path.join(scratch, 'old-data')})
    no_owner = mk('20261103T033000Z', {})
    old_ours = [mk(f'202611{d:02d}T033000Z', ours) for d in (4, 5, 6)]
    stale_partial_ours = mk('20200101T000000Z', owner=ours, partial=True)
    stale_partial_theirs = mk('20200102T000000Z', owner={'host': 'another-host',
                                                         'source_data_dir': os.path.abspath(data)}, partial=True)
    stale_partial_unknown = mk('20200103T000000Z', partial=True)
    runner = snapshot.SnapshotRunner(data, _settings(dest, keep_daily=1, keep_monthly=0))
    deleted = sorted(os.path.basename(p) for p in runner.prune())
    assert deleted == sorted(old_ours[:2] + [stale_partial_ours])
    assert sorted(os.listdir(dest)) == sorted([other_host, other_dir, no_owner, old_ours[2],
                                               stale_partial_theirs, stale_partial_unknown])
    assert snapshot.PARTIAL_OWNER == OWNER_FILE


def test_two_managers_sharing_dest_dir_start_in_the_same_second(scratch, monkeypatch):
    """
    Two Managers with one dest_dir, both scheduled at snapshot.time, choose the folder
    name in the same UTC second.  Here A gets there first and is still copying when B
    goes on (forced: B waits for A's folder, A waits for B's run to end).  B must use
    another folder, and neither run may remove the other's: both snapshots complete.
    """
    import threading

    dest = os.path.join(scratch, 'shared-dest')

    def mk(name, rows):
        d = os.path.join(scratch, name, 'data')
        os.makedirs(os.path.join(d, 'dev1'))
        for p in ('metrics.db', os.path.join('dev1', 'data.db')):
            conn = sqlite3.connect(os.path.join(d, p))
            conn.execute('CREATE TABLE t (v TEXT)')
            conn.executemany('INSERT INTO t VALUES (?)', [('y' * 200,)] * rows)
            conn.commit()
            conn.close()
        return d
    a = snapshot.SnapshotRunner(mk('managerA', 2000), _settings(dest))
    b = snapshot.SnapshotRunner(mk('managerB', 10), _settings(dest))
    same_second = time.time()
    picked, b_done = {}, threading.Event()
    both_picked = threading.Barrier(2, timeout=30)

    def pick(runner, who):
        real = runner._new_stamp

        def patched(dest_dir, started):
            picked[who] = real(dest_dir, same_second)
            both_picked.wait()
            if who == 'B':           # A is in its folder by now
                deadline = time.time() + 10
                while not os.path.exists(os.path.join(picked['A'][2], OWNER_FILE)) and time.time() < deadline:
                    time.sleep(0.01)
            return picked[who]
        runner._new_stamp = patched
    pick(a, 'A')
    pick(b, 'B')
    real_stage = snapshot._stage_database

    def stage(src, staged):
        if threading.current_thread().name == 'A':
            b_done.wait(10)          # A is still copying while B runs
        return real_stage(src, staged)
    monkeypatch.setattr(snapshot, '_stage_database', stage)
    res = {}

    def run(who, runner):
        try:
            res[who] = runner.run('scheduled')
        finally:
            if who == 'B':
                b_done.set()
    threads = [threading.Thread(target=run, args=(w, r), name=w) for w, r in (('A', a), ('B', b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert res['A']['status'] == 'ok', res['A']
    assert res['B']['status'] == 'ok', res['B']
    assert res['A']['stamp'] != res['B']['stamp']
    assert sorted(os.listdir(dest)) == sorted(os.path.basename(res[w]['path']) for w in 'AB')
    for w in 'AB':
        assert snapshot.verify_snapshot(res[w]['path']) == []
