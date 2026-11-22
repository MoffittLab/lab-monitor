"""Nightly snapshots (manager/snapshot.py): config, a real run, pruning, API, verify."""
import json
import logging
import os
import sqlite3
import threading
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
        assert runner.trigger() is False
    finally:
        runner._lock.release()


def test_deleted_system_is_skipped(client, data_dir, scratch, monkeypatch):
    _seed(client)
    real = snapshot.find_sources

    def with_ghost(d):
        return real(d) + [('ghost/data.db', os.path.join(d, 'ghost', 'data.db'), 'db')]
    monkeypatch.setattr(snapshot, 'find_sources', with_ghost)
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


def test_prune_only_touches_our_folders(scratch):
    dest = os.path.join(scratch, 'snaps')
    os.makedirs(dest)

    def mk(name, manifest=True):
        p = os.path.join(dest, name)
        os.makedirs(p)
        if manifest:
            Path(p, 'manifest.json').write_text('{}')
        return p
    for day in range(1, 6):
        mk(f'snapshot-202611{day:02d}T033000Z')
    mk('snapshot-20261101T040000Z', manifest=False)   # incomplete-looking, no manifest: kept
    mk('snapshot-notes')                               # not our pattern: kept
    mk('other-folder')
    mk('snapshot-20200101T000000Z.partial', manifest=False)   # stale partial: removed
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
