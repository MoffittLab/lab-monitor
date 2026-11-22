"""
tools/rollback_upgrade.py puts the Manager's pre-upgrade backups back
(<db>.pre-upgrade-<stamp>.bak -> <db>) without deleting anything: the
current files are renamed to <db>.post-upgrade-<stamp>*, the .bak stays.
(End to end with real backups and the OLD Manager: tests/compat, check
"rollback".)
"""
import hashlib
import http.server
import json
import os
import socket
import sqlite3
import threading

import pytest

import rollback_upgrade as rb


def _db(path, rows, wal=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS t (v TEXT)')
    con.execute('DELETE FROM t')
    con.executemany('INSERT INTO t VALUES (?)', [(r,) for r in rows])
    con.commit()
    con.close()


def _rows(path):
    con = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    try:
        return [r[0] for r in con.execute('SELECT v FROM t ORDER BY rowid')]
    finally:
        con.close()


def _tree(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, 'rb') as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


@pytest.fixture
def data_dir(tmp_path):
    """metrics.db and two systems after an upgrade, plus one system created since."""
    d = tmp_path / 'data'
    central = str(d / 'metrics.db')
    _db(central + '.pre-upgrade-20261101T100000Z.bak', ['central-original'])
    # metrics.db with part of its content only in a -wal file (a Manager killed by Task Scheduler)
    live = str(tmp_path / 'live' / 'metrics.db')
    _db(live, ['central-original', 'central-new'], wal=True)
    con = sqlite3.connect(live)
    con.execute('PRAGMA wal_autocheckpoint=0')
    con.execute("INSERT INTO t VALUES ('central-wal')")
    con.commit()
    for suffix in ('', '-wal'):
        with open(live + suffix, 'rb') as src, open(central + suffix, 'wb') as dst:
            dst.write(src.read())
    con.close()
    atl = str(d / 'atlantis' / 'data.db')
    _db(atl + '.pre-upgrade-20261101T100000Z.bak', ['atl-first-attempt'])
    _db(atl + '.pre-upgrade-20261102T080000Z.bak', ['atl-original'])
    _db(atl, ['atl-migrated'])
    vault = str(d / 'vault_2' / 'data.db')
    _db(vault + '.pre-upgrade-20261101T100000Z.bak', ['vault-original'])   # data.db deleted since
    _db(str(d / 'new_box' / 'data.db'), ['created-by-new-manager'])
    (d / 'Vault-2').mkdir()
    (d / 'Vault-2' / 'usage.jsonl').write_text('{"legacy": 1}\n')
    return d


def test_dry_run_changes_nothing(data_dir, capsys):
    before = _tree(data_dir)
    assert rb.main(['--data-dir', str(data_dir)]) == 0
    assert _tree(data_dir) == before
    out = capsys.readouterr().out
    assert 'Dry run' in out and 'new_box' in out and 'pre-upgrade-20261102T080000Z.bak' in out


def test_apply_restores_newest_backups_and_keeps_everything(data_dir):
    backups_before = {k: v for k, v in _tree(data_dir).items() if k.endswith('.bak')}
    code, items = rb.rollback(str(data_dir), apply=True, out=lambda *_: None)
    assert code == 0
    assert _rows(str(data_dir / 'metrics.db')) == ['central-original']
    assert _rows(str(data_dir / 'atlantis' / 'data.db')) == ['atl-original']        # the newest backup
    assert _rows(str(data_dir / 'vault_2' / 'data.db')) == ['vault-original']
    assert _rows(str(data_dir / 'new_box' / 'data.db')) == ['created-by-new-manager']  # no backup: untouched
    after = _tree(data_dir)
    # nothing deleted: the backups are unchanged, the post-upgrade files are kept aside
    assert {k: v for k, v in after.items() if k.endswith('.bak')} == backups_before
    moved = sorted(k for k in after if '.post-upgrade-' in k)
    assert any(k.startswith('metrics.db.post-upgrade-') and k.endswith('-wal') for k in moved), moved
    assert any(k.startswith(os.path.join('atlantis', 'data.db.post-upgrade-')) for k in moved), moved
    kept = [k for k in moved if k.startswith('metrics.db.post-upgrade-') and not k.endswith(('-wal', '-shm'))][0]
    stamp = kept[len('metrics.db.post-upgrade-'):]
    # the post-upgrade state, WAL content included, is still readable from the moved files
    scratch = data_dir.parent / 'reopen'
    scratch.mkdir()
    for suffix in ('', '-wal', '-shm'):
        src = data_dir / ('metrics.db.post-upgrade-' + stamp + suffix)
        if src.exists():
            (scratch / ('m.db' + suffix)).write_bytes(src.read_bytes())
    assert _rows(str(scratch / 'm.db')) == ['central-original', 'central-new', 'central-wal']
    assert (data_dir / 'Vault-2' / 'usage.jsonl').read_text() == '{"legacy": 1}\n'
    assert not [k for k in after if k.endswith('.rollback.tmp')]


def test_use_oldest(data_dir):
    code, _ = rb.rollback(str(data_dir), apply=True, use_oldest=True, out=lambda *_: None)
    assert code == 0
    assert _rows(str(data_dir / 'atlantis' / 'data.db')) == ['atl-first-attempt']


def test_a_bad_backup_changes_nothing(data_dir, capsys):
    bak = data_dir / 'vault_2' / 'data.db.pre-upgrade-20261101T100000Z.bak'
    bak.write_bytes(b'SQLite format 3\x00' + b'\x00' * 200)
    before = _tree(data_dir)
    assert rb.main(['--data-dir', str(data_dir), '--apply', '--force']) == 1
    assert _tree(data_dir) == before
    assert 'BACKUP NOT USABLE' in capsys.readouterr().out


def test_nothing_to_roll_back(tmp_path):
    d = tmp_path / 'data'
    _db(str(d / 'metrics.db'), ['x'])
    assert rb.main(['--data-dir', str(d), '--apply', '--force']) == 2


def test_a_locked_database_is_left_as_it_was(data_dir, monkeypatch):
    real = os.replace
    locked = str(data_dir / 'vault_2')

    def replace(src, dst):
        if str(dst).startswith(locked) and str(dst).endswith('data.db'):
            raise PermissionError(13, 'being used by another process')
        return real(src, dst)

    monkeypatch.setattr(rb.os, 'replace', replace)
    monkeypatch.setattr(rb.time, 'sleep', lambda s: None)
    lines = []
    code, _ = rb.rollback(str(data_dir), apply=True, out=lines.append)
    assert code == 1
    assert _rows(str(data_dir / 'atlantis' / 'data.db')) == ['atl-original']        # done before the failure
    assert not (data_dir / 'vault_2' / 'data.db').exists()                         # unchanged (was deleted)
    assert not list(data_dir.glob('vault_2/*.rollback.tmp'))
    assert any('Already restored' in ln for ln in lines)
    monkeypatch.setattr(rb.os, 'replace', real)
    assert rb.rollback(str(data_dir), apply=True, out=lambda *_: None)[0] == 0      # simply run it again
    assert _rows(str(data_dir / 'vault_2' / 'data.db')) == ['vault-original']
    assert _rows(str(data_dir / 'atlantis' / 'data.db')) == ['atl-original']


def test_refuses_while_a_manager_answers(data_dir, tmp_path):
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(('127.0.0.1', port), Health)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        cfg = tmp_path / 'config.json'
        cfg.write_text(json.dumps({'data_dir': str(data_dir), 'port': port}))
        before = _tree(data_dir)
        assert rb.main(['--manager-config', str(cfg), '--apply']) == 2
        assert _tree(data_dir) == before
    finally:
        srv.shutdown()
        srv.server_close()


def _new_layout(path, rows):
    """A copy of a partly migrated database (narrow folder tables, msg_id columns)."""
    _db(path, rows)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE folder_usage_msgs (id INTEGER PRIMARY KEY, msg_id TEXT, timestamp TEXT)')
    con.execute('CREATE TABLE folder_usage_legacy (id INTEGER PRIMARY KEY, timestamp TEXT)')
    con.commit()
    con.close()


def test_newer_backup_of_a_partly_migrated_database_is_skipped(data_dir, capsys):
    """An earlier Manager version could copy a partly migrated database as a second
    "pre-upgrade" backup; restoring that one would hide the folder history from the old
    Manager, so the newest backup that still has the OLD layout is restored."""
    atl = str(data_dir / 'atlantis' / 'data.db')
    _new_layout(atl + '.pre-upgrade-20261103T090000Z.bak', ['atl-partly-migrated'])
    central = str(data_dir / 'metrics.db')
    _db(central + '.pre-upgrade-20261103T090000Z.bak', ['central-partly-migrated'])
    con = sqlite3.connect(central + '.pre-upgrade-20261103T090000Z.bak')
    con.execute('CREATE TABLE dead_letter (id INTEGER PRIMARY KEY)')
    con.commit()
    con.close()

    code, items = rb.rollback(str(data_dir), apply=True, out=print)
    assert code == 0
    assert _rows(atl) == ['atl-original']
    assert _rows(central) == ['central-original']
    out = capsys.readouterr().out
    assert out.count('WARNING: skipped the newer') == 2 and '20261103T090000Z' in out


def test_only_new_layout_backups_restore_the_newest_with_a_warning(tmp_path, capsys):
    d = tmp_path / 'data'
    db = str(d / 'box' / 'data.db')
    _new_layout(db + '.pre-upgrade-20261101T100000Z.bak', ['only-copy'])
    _db(db, ['live'])
    code, _items = rb.rollback(str(d), apply=True, out=print)
    assert code == 0 and _rows(db) == ['only-copy']
    assert 'already has the new layout' in capsys.readouterr().out


def _review_fix_layout(path, rows, central=False, usage_metrics=False):
    """A database as the review-fix release leaves it (or, usage_metrics, as the usage-metrics release does)."""
    if central:
        _db(path, rows)
    else:
        _new_layout(path, rows)
    con = sqlite3.connect(path)
    if central:
        con.execute('CREATE TABLE dead_letter (id INTEGER PRIMARY KEY)')
    if usage_metrics:
        con.execute('CREATE TABLE device_session_snapshot (name TEXT)' if central else
                    'CREATE TABLE _rollup_series (id INTEGER PRIMARY KEY)')
    con.commit()
    con.close()


@pytest.fixture
def twice_upgraded(tmp_path):
    """f086e7e -> review-fix (first backups) -> usage-metrics (second backups, of the review-fix layout)."""
    d = tmp_path / 'data'
    central = str(d / 'metrics.db')
    _db(central + '.pre-upgrade-20261101T100000Z.bak', ['central-f086e7e'])
    _review_fix_layout(central + '.pre-upgrade-20261201T100000Z.bak', ['central-review-fix'], central=True)
    _review_fix_layout(central, ['central-usage-metrics'], central=True, usage_metrics=True)
    atl = str(d / 'atlantis' / 'data.db')
    _db(atl + '.pre-upgrade-20261101T100000Z.bak', ['atl-f086e7e'])
    _review_fix_layout(atl + '.pre-upgrade-20261201T100000Z.bak', ['atl-review-fix'])
    _review_fix_layout(atl, ['atl-usage-metrics'], usage_metrics=True)
    return d


def test_refuses_to_go_back_past_the_review_fix_release(twice_upgraded, capsys):
    """The way back to the review-fix release is starting its code (docs/UPGRADE.md), not this tool."""
    d = twice_upgraded
    before = _tree(d)
    assert rb.main(['--data-dir', str(d)]) == 0                       # the plan, with the warning
    out = capsys.readouterr().out
    assert 'past the review-fix release' in out and '--past-review-fix' in out and 'do NOT run this tool' in out
    assert 'the review-fix layout, copied by the usage-metrics release' in out
    for extra in ([], ['--use-oldest']):
        assert rb.main(['--data-dir', str(d), '--apply', '--force'] + extra) == 2
        assert 'Refused: nothing was changed' in capsys.readouterr().out
        assert _tree(d) == before
    assert rb.main(['--data-dir', str(d), '--apply', '--force', '--past-review-fix']) == 0
    assert _rows(str(d / 'atlantis' / 'data.db')) == ['atl-f086e7e']
    assert _rows(str(d / 'metrics.db')) == ['central-f086e7e']


def test_going_back_from_usage_metrics_straight_to_f086e7e_needs_no_flag(tmp_path, capsys):
    """f086e7e -> usage-metrics (no review-fix release in between): its only backups have the old layout."""
    d = tmp_path / 'data'
    atl = str(d / 'atlantis' / 'data.db')
    _db(atl + '.pre-upgrade-20261201T100000Z.bak', ['atl-f086e7e'])
    _review_fix_layout(atl, ['atl-usage-metrics'], usage_metrics=True)
    assert rb.main(['--data-dir', str(d), '--apply', '--force']) == 0
    assert _rows(atl) == ['atl-f086e7e']
    assert 'past the review-fix release' not in capsys.readouterr().out
