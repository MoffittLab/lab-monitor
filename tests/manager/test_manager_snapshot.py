"""Nightly snapshots (manager/snapshot.py): config, a real run, pruning, API, verify."""
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import manager as mgr
import snapshot
from manager_helpers import ADMIN, READ, auth, base_config, make_msg, metrics_payload, post_queue


def _settings(dest, **over):
    s = dict(snapshot.DEFAULTS, enabled=True, dest_dir=dest)
    s.update(over)
    return s


def _seed(client, n=5, name='Triton', token=None):
    from manager_helpers import COLL_TRITON
    msgs = [make_msg('system_metrics', metrics_payload(i, i), name=name,
                     ts=f'2026-08-04T07:{i:02d}:00.000000Z') for i in range(n)]
    msgs.append(make_msg('folder_usage', {'_unit': 'bytes', '/volume1': 5, '/volume1/A': 3},
                         name=name))
    r = post_queue(client, msgs, token=token or COLL_TRITON, name=name)
    assert r.get_json()['counts']['stored'] == n + 1


def _ro(path):
    return sqlite3.connect(Path(os.path.abspath(path)).as_uri() + '?mode=ro&immutable=1', uri=True)


def _counts(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

def test_disabled_by_default(data_dir):
    s = snapshot.settings_from_config({'data_dir': data_dir})
    assert s['enabled'] is False and s['dest_dir'] is None


@pytest.mark.parametrize('section, needle', [
    ({'enabled': True}, 'dest_dir is required'),
    ({'enabled': 'yes', 'dest_dir': 'x'}, 'enabled must be true or false'),
    ({'enabled': True, 'dest_dir': 'D', 'time': '25:00'}, 'HH:MM'),
    ({'enabled': True, 'dest_dir': 'D', 'time': '3am'}, 'HH:MM'),
    ({'enabled': True, 'dest_dir': 'D', 'keep_daily': 0}, 'keep_daily'),
    ({'enabled': True, 'dest_dir': 'D', 'keep_monthly': -1}, 'keep_monthly'),
    ({'enabled': True, 'dest_dir': 'D', 'kep_daily': 3}, 'unknown setting'),
])
def test_bad_settings_refused(scratch, section, needle):
    cfg = base_config(os.path.join(scratch, 'data'), snapshot=section)
    if section.get('dest_dir') == 'D':
        section['dest_dir'] = os.path.join(scratch, 'dest')
    with pytest.raises(mgr.ConfigError, match=needle):
        mgr.validate_config(cfg, logging.getLogger('t'))


def test_dest_inside_data_dir_refused(scratch):
    data = os.path.join(scratch, 'data')
    for dest in (os.path.join(data, 'snaps'), data, scratch):
        cfg = base_config(data, snapshot={'enabled': True, 'dest_dir': dest})
        with pytest.raises(mgr.ConfigError, match='outside data_dir'):
            mgr.validate_config(cfg, logging.getLogger('t'))


# --------------------------------------------------------------------------- #
# A real run                                                                  #
# --------------------------------------------------------------------------- #

def test_snapshot_copies_every_database(client, data_dir, scratch):
    _seed(client)
    dest = os.path.join(scratch, 'nas', 'snaps')
    runner = snapshot.SnapshotRunner(data_dir, _settings(dest))
    result = runner.run('test')
    assert result['status'] == 'ok', result

    snaps = snapshot.list_snapshots(dest)
    assert len(snaps) == 1
    path = snaps[0][1]
    assert not any(n.endswith('.partial') for n in os.listdir(dest))
    manifest = json.load(open(os.path.join(path, 'manifest.json')))
    rels = {f['path'] for f in manifest['files']}
    assert rels == {'metrics.db', 'triton/data.db'}

    for entry in manifest['files']:
        copy = os.path.join(path, *entry['path'].split('/'))
        live = os.path.join(data_dir, *entry['path'].split('/'))
        c = _ro(copy)
        try:
            # self-contained: not left in WAL mode, so opening it creates no side files
            assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
            got = _counts(c)
        finally:
            c.close()
        assert got == entry['tables']
        lc = sqlite3.connect(live)
        try:
            assert _counts(lc) == got
        finally:
            lc.close()
    assert not any(f.endswith(('-wal', '-shm')) for _, _, fs in os.walk(path) for f in fs)

    assert snapshot.verify_snapshot(path) == []
    # staging cleaned up
    staging = os.path.join(data_dir, snapshot.STAGING_DIR)
    assert not os.path.isdir(staging) or os.listdir(staging) == []
    # status persisted
    st = json.load(open(os.path.join(data_dir, snapshot.STATUS_FILE)))
    assert st['last_snapshot'] == path and st['last_files'] == 2


def test_snapshot_includes_retention_archives_but_not_collector_files(client, data_dir, scratch):
    _seed(client)
    arch = os.path.join(data_dir, 'triton', 'archive')
    os.makedirs(arch)
    Path(arch, 'system_metrics-2020-01.jsonl').write_text('{"a": 1}\n')
    # a collector sharing data_dir (dual role) keeps its own files there: not ours
    Path(data_dir, 'triton', '2026-11.jsonl').write_text('{"b": 2}\n')
    Path(data_dir, 'triton', 'queue.json').write_text('[]')
    dest = os.path.join(scratch, 'snaps')
    assert snapshot.SnapshotRunner(data_dir, _settings(dest)).run()['status'] == 'ok'
    path = snapshot.list_snapshots(dest)[0][1]
    rels = {f['path'] for f in json.load(open(os.path.join(path, 'manifest.json')))['files']}
    assert rels == {'metrics.db', 'triton/data.db', 'triton/archive/system_metrics-2020-01.jsonl'}


def test_snapshot_while_ingesting_is_consistent(client, data_dir, scratch):
    _seed(client)
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set() and i < 200:
            r = post_queue(client, [make_msg('system_metrics', metrics_payload(i, i),
                                             ts=f'2026-09-01T00:00:{i % 60:02d}.{i:06d}Z')])
            if r.status_code != 200:
                errors.append(r.status_code)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    finally:
        stop.set()
        t.join()
    assert res['status'] == 'ok', res
    assert errors == []
    assert snapshot.verify_snapshot(res['path']) == []


def test_failure_is_recorded_and_leaves_nothing_behind(client, data_dir, scratch):
    _seed(client)
    blocker = os.path.join(scratch, 'not-a-dir')
    Path(blocker).write_text('x')
    dest = os.path.join(blocker, 'snaps')      # can't be created: parent is a file
    runner = snapshot.SnapshotRunner(data_dir, _settings(dest))
    res = runner.run()
    assert res['status'] == 'error'
    d = runner.describe()
    assert d['last_error'] and d['last_failure'] and not d['last_success']
    staging = os.path.join(data_dir, snapshot.STAGING_DIR)
    assert not os.path.isdir(staging) or os.listdir(staging) == []


def test_verify_failure_removes_partial(client, data_dir, scratch, monkeypatch):
    _seed(client)
    dest = os.path.join(scratch, 'snaps')
    real = snapshot._sha256
    monkeypatch.setattr(snapshot, '_sha256', lambda p: '0' * 64 if p.endswith('data.db') else real(p))
    res = snapshot.SnapshotRunner(data_dir, _settings(dest)).run()
    assert res['status'] == 'error' and 'Verification failed' in res['error']
    assert os.listdir(dest) == []


def test_concurrent_run_is_refused(client, data_dir, scratch):
    _seed(client)
    runner = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps')))
    runner._lock.acquire()
    try:
        assert runner.run() == {'status': 'busy'}
        assert runner.trigger() == 'running'
    finally:
        runner._lock.release()


def test_deleted_system_is_skipped(client, data_dir, scratch, monkeypatch):
    _seed(client)
    real = snapshot.find_databases

    def with_ghost(d):
        return [('ghost/data.db', os.path.join(d, 'ghost', 'data.db'))] + real(d)
    monkeypatch.setattr(snapshot, 'find_databases', with_ghost)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok' and res['files'] == 2
    assert not os.path.exists(os.path.join(data_dir, 'ghost', 'data.db'))   # never created


def test_verify_detects_tampering(client, data_dir, scratch):
    _seed(client)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    db = os.path.join(res['path'], 'triton', 'data.db')
    with open(db, 'r+b') as f:
        f.seek(5000)
        f.write(b'\xff' * 16)
    problems = snapshot.verify_snapshot(res['path'])
    assert problems and 'triton/data.db' in problems[0]
    assert snapshot.main(['verify', res['path']]) == 1


# --------------------------------------------------------------------------- #
# Pruning and schedule                                                        #
# --------------------------------------------------------------------------- #

def test_keep_policy():
    stamps = ['20260101T033000Z', '20260115T033000Z', '20260201T033000Z', '20260220T033000Z',
              '20260301T033000Z', '20260302T033000Z', '20260303T033000Z']
    keep = snapshot.snapshots_to_keep(stamps, keep_daily=2, keep_monthly=3)
    # newest two, plus newest of Mar (already), Feb and Jan
    assert keep == {'20260303T033000Z', '20260302T033000Z', '20260220T033000Z', '20260115T033000Z'}
    assert snapshot.snapshots_to_keep(stamps, 1, 0) == {'20260303T033000Z'}


def _ours(data_dir, **extra):
    """Manifest fields that make a snapshot this Manager's own (SnapshotRunner.owns)."""
    return dict({'host': socket.gethostname(), 'source_data_dir': os.path.abspath(data_dir)}, **extra)


def test_prune_only_touches_our_folders(scratch):
    dest = os.path.join(scratch, 'snaps')
    os.makedirs(dest)

    def mk(name, manifest=True, owner=False):
        p = os.path.join(dest, name)
        os.makedirs(p)
        if manifest:
            Path(p, 'manifest.json').write_text(json.dumps(_ours(scratch)))
        if owner:
            Path(p, snapshot.PARTIAL_OWNER).write_text(json.dumps(_ours(scratch)))
        return p
    for day in range(1, 6):
        mk(f'snapshot-202611{day:02d}T033000Z')
    mk('snapshot-20261101T040000Z', manifest=False)   # incomplete-looking, no manifest: kept
    mk('snapshot-notes')                               # not our pattern: kept
    mk('other-folder')
    mk('snapshot-20200101T000000Z.partial', manifest=False, owner=True)   # our stale partial: removed
    runner = snapshot.SnapshotRunner(scratch, _settings(dest, keep_daily=2, keep_monthly=0))
    deleted = {os.path.basename(p) for p in runner.prune()}
    assert deleted == {'snapshot-20261101T033000Z', 'snapshot-20261102T033000Z',
                       'snapshot-20261103T033000Z', 'snapshot-20200101T000000Z.partial'}
    assert sorted(os.listdir(dest)) == sorted([
        'snapshot-20261104T033000Z', 'snapshot-20261105T033000Z',
        'snapshot-20261101T040000Z', 'snapshot-notes', 'other-folder'])


def test_schedule_math():
    now = datetime(2026, 11, 21, 3, 0)
    assert snapshot.next_run_after(now, '03:30') == datetime(2026, 11, 21, 3, 30)
    assert snapshot.next_run_after(datetime(2026, 11, 21, 3, 30), '03:30') == datetime(2026, 11, 22, 3, 30)
    assert snapshot.last_slot_before(now, '03:30') == datetime(2026, 11, 20, 3, 30)


def test_missed_slot(scratch):
    r = snapshot.SnapshotRunner(scratch, _settings(os.path.join(scratch, 'x')))
    assert r.missed_last_slot() is True                       # never ran
    r.status['last_success'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert r.missed_last_slot() is False
    r.status['last_success'] = '2020-01-01T00:00:00Z'
    assert r.missed_last_slot() is True


def test_scheduler_thread_runs_on_trigger(client, data_dir, scratch):
    _seed(client)
    dest = os.path.join(scratch, 'snaps')
    r = snapshot.SnapshotRunner(data_dir, _settings(dest, catch_up=False, time='00:00'))
    done = threading.Event()
    real = r.run
    r.run = lambda reason='scheduled': (real(reason), done.set())[0]
    r.start()
    try:
        assert r.trigger()
        assert done.wait(30)
    finally:
        r.stop()
    assert len(snapshot.list_snapshots(dest)) == 1


# --------------------------------------------------------------------------- #
# API                                                                         #
# --------------------------------------------------------------------------- #

def test_api_disabled(client):
    r = client.get('/api/snapshot', headers=auth(READ))
    assert r.status_code == 200 and r.get_json()['enabled'] is False
    assert client.post('/api/snapshot', headers=auth(ADMIN)).status_code == 409


def test_api_run_and_status(make_client, data_dir, scratch):
    dest = os.path.join(scratch, 'snaps')
    client = make_client(snapshot={'enabled': True, 'dest_dir': dest})
    _seed(client)
    assert client.post('/api/snapshot', headers=auth(READ)).status_code == 403
    r = client.post('/api/snapshot', headers=auth(ADMIN))
    assert r.status_code == 202
    for _ in range(300):
        st = client.get('/api/snapshot', headers=auth(READ)).get_json()
        if st['last_success'] and not st['running']:
            break
        threading.Event().wait(0.1)
    assert st['last_success'] and st['last_files'] == 2 and st['hours_since_success'] == 0.0
    assert len(snapshot.list_snapshots(dest)) == 1


# --------------------------------------------------------------------------- #
# Consistency across databases, retention, damaged files, other stored files  #
# --------------------------------------------------------------------------- #

def _user_msg(i, name='Triton'):
    return make_msg('user_metrics', {'username': 'alice', 'cpu_percent': float(i), 'ram_bytes': i},
                    name=name, ts=f'2026-11-20T10:{i:02d}:00.000000Z')


def test_no_message_is_in_a_data_db_without_its_central_rows(client, data_dir, scratch, monkeypatch):
    """
    Ingest commits metrics.db first, then data.db.  After a restore a replay stores
    again what a data.db lacks, but a message already in data.db is a duplicate and
    its central rows are not re-applied.  So every data.db is staged first and
    metrics.db last, and nothing is copied to dest_dir before all are staged: a
    message stored during the run is then in neither copy, or only in metrics.db.
    """
    _seed(client)
    assert post_queue(client, [_user_msg(0)]).get_json()['counts']['stored'] == 1
    events = []
    real_stage, real_copy = snapshot._stage_database, snapshot._copy_hash

    def stage_then_ingest(src, staged):
        info = real_stage(src, staged)
        events.append(('stage', os.path.relpath(src, data_dir).replace(os.sep, '/')))
        # a collector posts while the snapshot is between two databases
        assert post_queue(client, [_user_msg(len(events))]).get_json()['counts']['stored'] == 1
        return info

    def copy(src, dst, *args):
        events.append(('copy', src))
        return real_copy(src, dst, *args)
    monkeypatch.setattr(snapshot, '_stage_database', stage_then_ingest)
    monkeypatch.setattr(snapshot, '_copy_hash', copy)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    d, c = _ro(os.path.join(res['path'], 'triton', 'data.db')), _ro(os.path.join(res['path'], 'metrics.db'))
    try:
        in_data = {r[0] for r in d.execute('SELECT msg_id FROM user_metrics')}
        central = {r[0] for r in c.execute('SELECT msg_id FROM user_metrics_history')}
    finally:
        d.close()
        c.close()
    assert in_data <= central, 'data.db holds messages whose central rows the snapshot lacks'
    # message 0 is in both, message 1 (posted between the two) in metrics.db only, and
    # message 2 (posted after the last one) in neither
    assert len(in_data) == 1 and len(central) == 2
    staged = [e[1] for e in events if e[0] == 'stage']
    assert staged == ['triton/data.db', 'metrics.db']
    first_copy = next(i for i, e in enumerate(events) if e[0] == 'copy')
    assert all(e[0] == 'copy' for e in events[first_copy:])


def _archived_ids(snap, manifest, table):
    out = set()
    for f in manifest['files']:
        if '/archive/' in f['path'] and f['path'].endswith(f'-{table}.jsonl'):
            with open(os.path.join(snap, *f['path'].split('/')), encoding='utf-8') as fh:
                out |= {json.loads(line)['id'] for line in fh}
    return out


@pytest.mark.parametrize('when', ['first', 'last'])
def test_rows_retention_moves_during_a_snapshot_are_in_it(client, data_dir, scratch, monkeypatch, when):
    """
    History retention appends rows to <system>/archive/*.jsonl, then deletes them.
    Running while the snapshot stages its first (a data.db) or last (metrics.db)
    database, every row must still be in the snapshot: in its staged database or
    in an archive file, which is listed only after every database is staged.
    """
    import archive_metrics
    old = [make_msg('system_metrics', metrics_payload(i, i), ts=f'2020-01-{i + 1:02d}T00:00:00Z')
           for i in range(5)]
    old += [make_msg('user_metrics', {'username': 'bob', 'cpu_percent': 1.0, 'ram_bytes': i},
                     ts=f'2020-01-{i + 1:02d}T00:00:01Z') for i in range(4)]
    assert post_queue(client, old).get_json()['counts']['stored'] == 9
    _seed(client)
    live = sqlite3.connect(os.path.join(data_dir, 'triton', 'data.db'))
    want = {t: {r[0] for r in live.execute(f'SELECT id FROM {t}')} for t in ('system_metrics', 'user_metrics')}
    live.close()
    live = sqlite3.connect(os.path.join(data_dir, 'metrics.db'))
    want_central = {r[0] for r in live.execute('SELECT id FROM user_metrics_history')}
    live.close()
    assert len(want_central) == 4

    real = snapshot._stage_database
    calls, results = [], []

    def stage(src, staged):
        calls.append(src)
        if (when == 'first' and len(calls) == 1) or (when == 'last' and src.endswith('metrics.db')):
            results.append(archive_metrics.run_history_retention(
                mgr._data_store, mgr._metrics_db, data_dir, 365, logging.getLogger('t')))
        return real(src, staged)
    monkeypatch.setattr(snapshot, '_stage_database', stage)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    assert results and results[0]['archived'].get('triton/system_metrics') == 5
    assert results[0]['archived'].get('user_metrics_history') == 4
    snap = res['path']
    manifest = json.load(open(os.path.join(snap, 'manifest.json')))
    d = _ro(os.path.join(snap, 'triton', 'data.db'))
    try:
        for table, ids in want.items():
            have = {r[0] for r in d.execute(f'SELECT id FROM {table}')} | _archived_ids(snap, manifest, table)
            assert ids <= have, (table, sorted(ids - have))
    finally:
        d.close()
    c = _ro(os.path.join(snap, 'metrics.db'))
    try:
        have = {r[0] for r in c.execute('SELECT id FROM user_metrics_history')}
    finally:
        c.close()
    assert want_central <= have | _archived_ids(snap, manifest, 'user_metrics_history')
    assert snapshot.verify_snapshot(snap) == []


def test_an_archive_line_being_written_is_left_out(client, data_dir, scratch, monkeypatch):
    """Retention may be appending to an archive file while it is copied: only whole lines are copied."""
    _seed(client)
    arch = os.path.join(data_dir, 'triton', 'archive')
    os.makedirs(arch)
    Path(arch, '2020-01-system_metrics.jsonl').write_bytes(b'{"id": 1}\n{"id": 2, "v": "' + b'x' * 50 +
                                                         b'"}\n{"id": 3, "cpu')
    Path(arch, '2020-02-system_metrics.jsonl').write_bytes(b'{"id": 4, "cpu')     # no complete line yet
    monkeypatch.setattr(snapshot, 'CHUNK', 8)       # lines longer than one read block
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    got = Path(res['path'], 'triton', 'archive', '2020-01-system_metrics.jsonl').read_bytes()
    assert got == b'{"id": 1}\n{"id": 2, "v": "' + b'x' * 50 + b'"}\n'
    assert Path(res['path'], 'triton', 'archive', '2020-02-system_metrics.jsonl').read_bytes() == b''
    assert snapshot.verify_snapshot(res['path']) == []


def _damaged_db(path):
    """A database whose table pages were overwritten (a failing disk)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, timestamp TEXT, v TEXT)')
    c.executemany('INSERT INTO system_metrics (timestamp, v) VALUES (?, ?)',
                  [(f'2026-01-01T00:00:{i % 60:02d}Z', 'x' * 200) for i in range(2000)])
    c.commit()
    c.close()
    with open(path, 'r+b') as f:
        f.seek(4096 * 2)
        f.write(b'\xff' * 4096 * 4)


def test_a_damaged_database_does_not_stop_the_others(client, data_dir, scratch):
    _seed(client)
    _damaged_db(os.path.join(data_dir, 'broken', 'data.db'))
    runner = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps')))
    res = runner.run()
    assert res['status'] == 'partial', res
    assert [f['path'] for f in res['failed']] == ['broken/data.db']
    path = res['path']
    assert os.path.basename(path).startswith('snapshot-') and not path.endswith('.partial')
    manifest = json.load(open(os.path.join(path, 'manifest.json')))
    assert {f['path'] for f in manifest['files']} == {'metrics.db', 'triton/data.db'}
    assert manifest['complete'] is False and manifest['failed'][0]['path'] == 'broken/data.db'
    assert not os.path.exists(os.path.join(path, snapshot.PARTIAL_OWNER))
    problems = snapshot.verify_snapshot(path)
    assert len(problems) == 1 and problems[0].startswith('broken/data.db: not in this snapshot')
    d = runner.describe()
    assert d['last_result'] == 'partial' and d['last_failed'] == ['broken/data.db']
    assert d['last_success'] is None and d['last_partial'] and 'broken/data.db' in d['last_error']
    assert d['last_failure'] == d['last_partial']
    assert runner.missed_last_slot() is False          # it ran: no catch-up at every restart
    # once the file is repaired, the next run is complete again
    os.remove(os.path.join(data_dir, 'broken', 'data.db'))
    time.sleep(1.0)
    res2 = runner.run()
    assert res2['status'] == 'ok' and runner.describe()['last_result'] == 'ok'
    assert runner.describe()['last_failed'] == []


def test_no_database_copied_is_a_failed_run(scratch):
    data = os.path.join(scratch, 'data')
    _damaged_db(os.path.join(data, 'broken', 'data.db'))
    dest = os.path.join(scratch, 'snaps')
    runner = snapshot.SnapshotRunner(data, _settings(dest))
    res = runner.run()
    assert res['status'] == 'error' and 'no database could be copied' in res['error']
    assert os.listdir(dest) == []
    assert runner.describe()['last_result'] == 'error'


def _fake_snapshot(dest, stamp, data_dir, dbs, failed=()):
    p = os.path.join(dest, f'snapshot-{stamp}')
    os.makedirs(p)
    Path(p, 'manifest.json').write_text(json.dumps(_ours(
        data_dir, files=[{'path': f, 'kind': 'db'} for f in dbs],
        failed=[{'path': f, 'kind': 'db', 'error': 'DatabaseError: malformed'} for f in failed])))
    return p


def test_prune_keeps_the_newest_good_copy_of_a_failed_database(scratch):
    dest = os.path.join(scratch, 'snaps')
    all_dbs, healthy = ['metrics.db', 'a/data.db', 'b/data.db'], ['metrics.db', 'a/data.db']
    for day in (1, 2):
        _fake_snapshot(dest, f'202611{day:02d}T033000Z', scratch, all_dbs)
    for day in range(3, 8):                       # b/data.db damaged since day 3
        _fake_snapshot(dest, f'202611{day:02d}T033000Z', scratch, healthy, failed=['b/data.db'])
    runner = snapshot.SnapshotRunner(scratch, _settings(dest, keep_daily=3, keep_monthly=0))
    deleted = sorted(os.path.basename(p) for p in runner.prune())
    assert deleted == ['snapshot-20261101T033000Z', 'snapshot-20261103T033000Z', 'snapshot-20261104T033000Z']
    assert 'snapshot-20261102T033000Z' in os.listdir(dest)      # the last good copy of b/data.db
    # repaired: once the kept snapshots hold a good copy again, day 2 is pruned as usual
    for day in (8, 9, 10):
        _fake_snapshot(dest, f'202611{day:02d}T033000Z', scratch, all_dbs)
    runner.prune()
    assert sorted(os.listdir(dest)) == ['snapshot-20261108T033000Z', 'snapshot-20261109T033000Z',
                                        'snapshot-20261110T033000Z']


def test_prune_never_deletes_another_managers_snapshots(client, data_dir, scratch):
    """A second (e.g. test) Manager given a copy of config.json shares dest_dir."""
    _seed(client)
    dest = os.path.join(scratch, 'shared')
    other = os.path.join(scratch, 'other-data')
    os.makedirs(other)
    c = sqlite3.connect(os.path.join(other, 'metrics.db'))
    c.execute('CREATE TABLE t (x)')
    c.commit()
    c.close()
    a = snapshot.SnapshotRunner(other, _settings(dest, keep_daily=14))
    a_paths = [a.run('manual')['path'] for _ in range(3)]
    # someone else's: another host with the same data_dir path, and leftovers of theirs
    foreign = os.path.join(dest, 'snapshot-20200101T000000Z')
    os.makedirs(foreign)
    Path(foreign, 'manifest.json').write_text(json.dumps(
        {'host': 'another-host', 'source_data_dir': os.path.abspath(data_dir), 'files': []}))
    os.makedirs(os.path.join(dest, 'snapshot-20200102T000000Z.partial'))           # no owner file
    theirs = os.path.join(dest, 'snapshot-20200103T000000Z.partial')
    os.makedirs(theirs)
    Path(theirs, snapshot.PARTIAL_OWNER).write_text(json.dumps({'host': 'another-host',
                                                                 'source_data_dir': os.path.abspath(data_dir)}))
    b = snapshot.SnapshotRunner(data_dir, _settings(dest, keep_daily=1, keep_monthly=0))
    first = b.run('manual')
    second = b.run('manual')
    assert first['status'] == second['status'] == 'ok'
    left = set(os.listdir(dest))
    assert {os.path.basename(p) for p in a_paths} <= left
    assert {'snapshot-20200101T000000Z', 'snapshot-20200102T000000Z.partial',
            'snapshot-20200103T000000Z.partial'} <= left
    assert os.path.basename(second['path']) in left and os.path.basename(first['path']) not in left


def test_a_run_in_the_same_second_as_the_last_one_waits_for_the_next(client, data_dir, scratch):
    _seed(client)
    dest = os.path.join(scratch, 'snaps')
    now = time.time()
    taken = {snapshot.utc_stamp(now + k) for k in range(3)}
    for stamp in taken:
        os.makedirs(os.path.join(dest, 'snapshot-' + stamp))
    runner = snapshot.SnapshotRunner(data_dir, _settings(dest))
    res = runner.run('manual')
    assert res['status'] == 'ok', res
    assert res['stamp'] not in taken
    assert runner.describe()['last_error'] is None


def test_a_request_while_one_is_pending_is_not_counted_twice(scratch):
    r = snapshot.SnapshotRunner(scratch, _settings(os.path.join(scratch, 'x')))
    assert r.trigger() == 'started'
    assert r.trigger() == 'pending'
    r._lock.acquire()
    try:
        assert r.trigger() == 'running'
    finally:
        r._lock.release()


def test_api_answers_409_while_a_request_is_pending(make_client, data_dir, scratch, monkeypatch):
    dest = os.path.join(scratch, 'snaps')
    far = datetime.fromtimestamp(time.time() + 12 * 3600).strftime('%H:%M')
    client = make_client(snapshot={'enabled': True, 'dest_dir': dest, 'catch_up': False, 'time': far})
    _seed(client)
    runner = mgr._snapshots
    gate, runs = threading.Event(), []
    real = runner._run_locked

    def slow(reason, started):
        runs.append(reason)
        assert gate.wait(30)
        return real(reason, started)
    monkeypatch.setattr(runner, '_run_locked', slow)
    runner.start()
    try:
        codes = [client.post('/api/snapshot', headers=auth(ADMIN)).status_code for _ in range(5)]
        assert codes == [202, 409, 409, 409, 409]
        gate.set()
        for _ in range(300):
            st = client.get('/api/snapshot', headers=auth(READ)).get_json()
            if st['last_success'] and not st['running']:
                break
            time.sleep(0.1)
        assert st['last_result'] == 'ok' and runs == ['manual']
    finally:
        gate.set()
        runner.stop()


def test_staged_copy_is_written_without_fsync(client, data_dir, scratch, monkeypatch):
    """synchronous=OFF on the temporary staged copy: its fsync made every ingest request wait."""
    _seed(client)
    seen = []
    real = snapshot._open_staged

    def spy(path):
        conn = real(path)
        seen.append(conn.execute('PRAGMA synchronous').fetchone()[0])
        return conn
    monkeypatch.setattr(snapshot, '_open_staged', spy)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok' and seen == [0, 0]
    assert snapshot.verify_snapshot(res['path']) == []


def test_start_removes_a_killed_runs_staging_files(scratch):
    data = os.path.join(scratch, 'data')
    root = os.path.join(data, snapshot.STAGING_DIR)
    left = os.path.join(root, '20261122T033000Z', 'atlantis')
    os.makedirs(left)
    Path(left, 'data.db').write_bytes(b'x' * 1000)
    Path(left, 'data.db-journal').write_bytes(b'j')          # hot journal of a killed backup
    os.makedirs(os.path.join(root, 'not-ours'))
    r = snapshot.SnapshotRunner(data, dict(snapshot.DEFAULTS))     # snapshots off
    assert r.start() is None
    assert os.listdir(root) == ['not-ours']
    os.rmdir(os.path.join(root, 'not-ours'))
    r.start()
    assert not os.path.exists(root)


def test_snapshot_includes_old_usage_reports_and_rolled_back_databases(client, data_dir, scratch):
    """The previous Manager's usage.jsonl and the files rollback_upgrade.py moved aside are stored data."""
    _seed(client)
    Path(data_dir, 'Vault-2').mkdir()
    Path(data_dir, 'Vault-2', 'usage.jsonl').write_text('{"legacy": 1}\n')
    moved = 'data.db.post-upgrade-20261120T100000Z'
    for name in (moved, moved + '-wal', moved + '-shm', 'data.db.pre-upgrade-20261101T100000Z.bak'):
        Path(data_dir, 'triton', name).write_bytes(b'x')
    Path(data_dir, 'metrics.db.post-upgrade-20261120T100000Z').write_bytes(b'y')
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    rels = {f['path'] for f in json.load(open(os.path.join(res['path'], 'manifest.json')))['files']}
    assert rels == {'metrics.db', 'triton/data.db', 'Vault-2/usage.jsonl', f'triton/{moved}',
                    f'triton/{moved}-wal', 'metrics.db.post-upgrade-20261120T100000Z'}
    assert snapshot.verify_snapshot(res['path']) == []
