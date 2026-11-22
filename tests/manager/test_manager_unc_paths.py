"""
SQLite file: URIs for a UNC path (\\\\server\\share\\...): `snapshot.py verify` of a
snapshot on the NAS share (docs/UPGRADE.md Step 4 tells the operator to run it on
last_snapshot), a snapshot run and the pre-upgrade backup check with a data_dir on
a share, and rollups.py --status (it reads every data.db read-only) on such a
data_dir.  pathlib's as_uri() gives file://server/share/..., which SQLite refuses
("invalid uri authority: server") unless it was built with
SQLITE_ALLOW_URI_AUTHORITY; file:////server/share/... (empty authority) works.

On POSIX the test switches the modules to Windows' path rules (their _WINDOWS
setting, and pathlib's Windows flavour, which is what turns a UNC path into a URI
authority) and uses a path that starts with '//': a UNC path to that code, and
the same folder to Linux.  SQLite then parses the URI it gets exactly as it
would on Windows.  On Windows the folder is reached through \\\\localhost\\<drive>$
when that administrative share is reachable.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import PureWindowsPath

import pytest

import rollups
import snapshot
import storage_util
from manager_helpers import COLL_TRITON, make_msg, metrics_payload, post_queue


def _settings(dest, **over):
    s = dict(snapshot.DEFAULTS, enabled=True, dest_dir=dest)
    s.update(over)
    return s


@pytest.fixture
def unc(monkeypatch):
    """
    unc(path) -> the same folder or file as a UNC path, for the code under test.
    On POSIX the first call switches the modules to Windows' path rules, so call
    it only once the local work (the Manager, a local snapshot run) is done.
    """
    if os.name == 'nt':
        def to_unc(path):
            drive, rest = os.path.splitdrive(os.path.abspath(path))
            p = '\\\\localhost\\%s$%s' % (drive.rstrip(':'), rest)
            if not os.path.exists(p):
                pytest.skip('the administrative share \\\\localhost\\%s$ is not reachable' % drive)
            return p
        return to_unc

    def to_unc(path):
        for mod in (snapshot, storage_util, rollups):
            monkeypatch.setattr(mod, '_WINDOWS', True, raising=False)
            monkeypatch.setattr(mod, 'Path', PureWindowsPath, raising=False)
        return '/' + os.path.abspath(path)
    return to_unc


def _seed(client):
    msgs = [make_msg('system_metrics', metrics_payload(i, i), ts=f'2026-08-04T07:{i:02d}:00.000000Z')
            for i in range(5)]
    assert post_queue(client, msgs, token=COLL_TRITON).get_json()['counts']['stored'] == 5


def test_pathlib_gives_a_uri_authority_sqlite_refuses():
    """Why sqlite_uri exists: what pathlib makes of a UNC path is not a URI SQLite opens."""
    p = r'\\nas\share\lab-monitor-snapshots\snapshot-20261122T033000Z\metrics.db'
    assert PureWindowsPath(p).as_uri().startswith('file://nas/share/')
    with pytest.raises(sqlite3.OperationalError, match='invalid uri authority'):
        sqlite3.connect(PureWindowsPath(p).as_uri() + '?mode=ro&immutable=1', uri=True)


def test_snapshot_verify_of_a_snapshot_on_a_share(client, data_dir, scratch, unc):
    _seed(client)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    path = unc(res['path'])
    assert snapshot.verify_snapshot(path) == []
    assert snapshot.main(['verify', path]) == 0


def test_sqlite_still_checks_each_copy_on_a_share(client, data_dir, scratch, unc):
    """
    A copy whose size and SHA-256 match the manifest but that SQLite can't read is
    reported by SQLite's own check, not by a URI error.
    """
    _seed(client)
    res = snapshot.SnapshotRunner(data_dir, _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    db = os.path.join(res['path'], 'metrics.db')
    with open(db, 'r+b') as f:
        f.write(b'not a database!!')              # the 16-byte SQLite header
    mpath = os.path.join(res['path'], snapshot.MANIFEST)
    manifest = json.load(open(mpath, encoding='utf-8'))
    for entry in manifest['files']:
        if entry['path'] == 'metrics.db':
            entry['sha256'] = hashlib.sha256(open(db, 'rb').read()).hexdigest()
    with open(mpath, 'w', encoding='utf-8') as f:
        json.dump(manifest, f)
    assert snapshot.verify_snapshot(unc(res['path'])) == ['metrics.db: file is not a database']


def test_a_snapshot_run_with_data_dir_on_a_share(client, data_dir, scratch, unc):
    """Staging opens each live database by URI (mode=rw): a data_dir on a share must work too."""
    _seed(client)
    res = snapshot.SnapshotRunner(unc(data_dir), _settings(os.path.join(scratch, 'snaps'))).run()
    assert res['status'] == 'ok', res
    assert not res['failed'] and res['files'] == 2
    assert snapshot.verify_snapshot(unc(res['path'])) == []


def test_the_backup_check_reads_a_backup_on_a_share(scratch, unc):
    """storage_util opens a backup read-only by URI to see whether it can be reused."""
    path = os.path.join(scratch, 'sys', 'data.db')
    os.makedirs(os.path.dirname(path))
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE t (v)')
    conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(3)])
    conn.commit()
    conn.close()
    bconn = storage_util._open_backup_ro(unc(path))
    try:
        assert bconn.execute('SELECT COUNT(*) FROM t').fetchone()[0] == 3
    finally:
        bconn.close()
    assert sorted(os.listdir(os.path.dirname(path))) == ['data.db']     # read-only, no side files


def test_rollups_status_reads_a_data_dir_on_a_share(client, data_dir, unc):
    """rollups.py --status opens each data.db read-only by URI (storage_util.sqlite_uri)."""
    _seed(client)
    dbs = [os.path.join(data_dir, d, 'data.db') for d in sorted(os.listdir(data_dir))
           if os.path.isfile(os.path.join(data_dir, d, 'data.db'))]
    assert dbs
    want = {}
    for path in dbs:
        conn = rollups._open_read_only(path)
        try:
            want[path] = rollups.table_status(conn)
        finally:
            conn.close()
    for path in dbs:
        conn = rollups._open_read_only(unc(path))
        try:
            assert conn.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0] == 5
            assert rollups.table_status(conn) == want[path]
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                conn.execute('CREATE TABLE never (v)')
        finally:
            conn.close()
