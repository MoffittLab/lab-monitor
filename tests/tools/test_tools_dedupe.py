"""tools/dedupe_history.py: removes only exact duplicates of pre-upgrade rows, with a backup, atomically."""
import os
import sqlite3

import pytest

import dedupe_history as dd
import storage_util


def _data_dir(tmp_path):
    """
    A migrated data_dir as the new Manager leaves it: old rows have msg_id NULL,
    folder scans are in folder_usage_msgs/_items, the wide table kept as
    folder_usage_legacy.  Duplicates of the kind the old Manager stored.
    """
    data = tmp_path / 'data'
    (data / 'box').mkdir(parents=True)
    db = sqlite3.connect(str(data / 'box' / 'data.db'))
    db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE system_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            device_type TEXT DEFAULT 'unknown', cpu_percent REAL, ram_percent REAL, gpu_0_temp REAL, msg_id TEXT);
        CREATE UNIQUE INDEX idx_system_metrics_msg_id ON system_metrics (msg_id);
        CREATE TABLE user_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            device_type TEXT DEFAULT 'unknown', username TEXT, cpu_percent REAL, ram_bytes INTEGER, msg_id TEXT);
        CREATE TABLE folder_usage_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE,
            timestamp TEXT NOT NULL, device_type TEXT DEFAULT 'unknown', meta_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE folder_usage_items (msg_row_id INTEGER NOT NULL, key TEXT NOT NULL, value NUMERIC,
            PRIMARY KEY (msg_row_id, key)) WITHOUT ROWID;
        CREATE TABLE folder_usage_legacy (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
            device_type TEXT, "/volume1/a" INTEGER);
    ''')
    sm = [
        ('2026-08-01T00:00:00Z', 'NAS', 10.0, 50.0, None, None),     # id 1
        ('2026-08-01T00:00:00Z', 'NAS', 10.0, 50.0, None, None),     # 2: copy of 1 (NULLs alike)
        ('2026-08-01T00:00:00Z', 'NAS', 10.0, 50.0, None, None),     # 3: copy of 1
        ('2026-08-01T00:05:00Z', 'NAS', 12.0, 51.0, 40.0, None),     # 4
        ('2026-08-01T00:05:00Z', 'NAS', 12.0, 51.0, 41.0, None),     # 5: same time, other value: kept
        ('2026-08-01T00:05:00Z', 'NAS', 12.0, 51.0, 40.0, None),     # 6: copy of 4
        ('2026-11-22T13:30:00Z', 'NAS', 1.0, 9.0, None, 'm-1'),      # 7: new Manager
        ('2026-11-22T13:30:00Z', 'NAS', 1.0, 9.0, None, 'm-2'),      # 8: new, same values: never touched
    ]
    db.executemany('INSERT INTO system_metrics (timestamp, device_type, cpu_percent, ram_percent, gpu_0_temp, msg_id) '
                   'VALUES (?, ?, ?, ?, ?, ?)', sm)
    um = [('2026-08-01T00:00:00Z', 'NAS', u, 1.5, 100, None) for u in ('alice', 'bob', 'alice', 'bob', 'alice')]
    db.executemany('INSERT INTO user_metrics (timestamp, device_type, username, cpu_percent, ram_bytes, msg_id) '
                   'VALUES (?, ?, ?, ?, ?, ?)', um)
    scans = [  # (id, msg_id, timestamp, items)
        (1, None, '2026-08-01T02:00:00.000000Z', {'/volume1/a': 100, '/volume1/b': 7}),
        (2, None, '2026-08-01T02:00:00.000000Z', {'/volume1/a': 100, '/volume1/b': 7}),   # copy of 1
        (3, None, '2026-08-01T02:00:00.000000Z', {'/volume1/a': 100, '/volume1/b': 8}),   # one value differs
        (4, 'm-9', '2026-11-22T07:00:00.000000Z', {'/volume1/a': 100, '/volume1/b': 7}),
    ]
    for sid, mid, ts, items in scans:
        db.execute('INSERT INTO folder_usage_msgs (id, msg_id, timestamp, device_type) VALUES (?, ?, ?, ?)',
                   (sid, mid, ts, 'NAS'))
        db.executemany('INSERT INTO folder_usage_items VALUES (?, ?, ?)', [(sid, k, v) for k, v in items.items()])
    db.executemany('INSERT INTO folder_usage_legacy (timestamp, device_type, "/volume1/a") VALUES (?, ?, ?)',
                   [('2026-08-01T02:00:00Z', 'NAS', 100)] * 2)       # duplicates there stay
    db.commit()
    db.close()

    m = sqlite3.connect(str(data / 'metrics.db'))
    m.executescript('''
        CREATE TABLE devices (name TEXT PRIMARY KEY, system_id TEXT, device_type TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE user_metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, device_name TEXT NOT NULL,
            timestamp TEXT NOT NULL, username TEXT NOT NULL, cpu_percent REAL, ram_bytes INTEGER,
            ram_formatted TEXT, msg_id TEXT);
    ''')
    m.execute("INSERT INTO devices VALUES ('box', 'id', 'NAS', 'a', 'b')")
    rows = [('box', '2026-08-01T00:00:00Z', 'alice', 1.0, 5, '5 B', None)] * 3 + \
           [('other', '2026-08-01T00:00:00Z', 'alice', 1.0, 5, '5 B', None)]           # other device: kept
    m.executemany('INSERT INTO user_metrics_history (device_name, timestamp, username, cpu_percent, ram_bytes, '
                  'ram_formatted, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    m.commit()
    m.close()
    return data


def _rows(path, sql):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_report_counts_and_changes_nothing(tmp_path, capsys):
    data = _data_dir(tmp_path)
    before = {p: p.read_bytes() for p in data.rglob('*.db')}
    assert dd.main(['--data-dir', str(data)]) == 0
    out = capsys.readouterr().out
    assert 'box              system_metrics 3, user_metrics 3, folder_usage_msgs 1' in out
    assert 'metrics.db       user_metrics_history 2' in out
    assert 'Nothing was changed' in out
    assert {p: p.read_bytes() for p in data.rglob('*.db')} == before
    assert not list(data.rglob('*.bak'))


def test_apply_removes_exact_old_copies_only(tmp_path, capsys):
    data = _data_dir(tmp_path)
    box = data / 'box' / 'data.db'
    assert dd.main(['--data-dir', str(data), '--apply']) == 0
    # earliest copy kept; a row differing in one value kept; new rows (msg_id) untouched
    assert [r[0] for r in _rows(box, 'SELECT id FROM system_metrics ORDER BY id')] == [1, 4, 5, 7, 8]
    assert _rows(box, 'SELECT id, username FROM user_metrics ORDER BY id') == [(1, 'alice'), (2, 'bob')]
    assert [r[0] for r in _rows(box, 'SELECT id FROM folder_usage_msgs ORDER BY id')] == [1, 3, 4]
    assert _rows(box, 'SELECT DISTINCT msg_row_id FROM folder_usage_items ORDER BY 1') == [(1,), (3,), (4,)]
    assert _rows(box, 'SELECT COUNT(*) FROM folder_usage_legacy') == [(2,)]
    central = data / 'metrics.db'
    assert _rows(central, 'SELECT id, device_name FROM user_metrics_history ORDER BY id') == [(1, 'box'), (4, 'other')]
    assert _rows(central, 'SELECT COUNT(*) FROM devices') == [(1,)]


def test_apply_backs_up_each_changed_database_first(tmp_path):
    data = _data_dir(tmp_path)
    box = data / 'box' / 'data.db'
    assert dd.main(['--data-dir', str(data), '--apply']) == 0
    backups = sorted(data.rglob('*.pre-dedupe-*.bak'))
    assert [b.parent.name for b in backups] == ['box', 'data']           # box/data.db and metrics.db
    bak = next(b for b in backups if b.parent.name == 'box')
    assert _rows(bak, 'SELECT COUNT(*) FROM system_metrics') == [(8,)]  # the data before
    assert not list(data.rglob('*.bak.tmp'))
    assert dd.main(['--data-dir', str(data), '--apply']) == 0            # second run: nothing to do
    assert len(list(data.rglob('*.pre-dedupe-*.bak'))) == 2              # and no new backups
    assert _rows(box, 'PRAGMA quick_check') == [('ok',)]


def test_vacuum_shrinks_and_keeps_data(tmp_path, capsys):
    data = _data_dir(tmp_path)
    c = sqlite3.connect(str(data / 'metrics.db'))
    c.executemany('INSERT INTO user_metrics_history (device_name, timestamp, username, cpu_percent, ram_bytes, '
                  'ram_formatted) VALUES (?, ?, ?, ?, ?, ?)', [('box', '2026-08-02', 'x' * 200, 1, 1, '1')] * 3000)
    c.commit()
    c.close()
    size = os.path.getsize(data / 'metrics.db')
    assert dd.main(['--data-dir', str(data), '--apply', '--vacuum']) == 0
    assert os.path.getsize(data / 'metrics.db') < size / 4
    assert _rows(data / 'metrics.db', "SELECT COUNT(*) FROM user_metrics_history WHERE timestamp = '2026-08-02'") == [(1,)]


def test_refuses_while_a_manager_holds_the_lock(tmp_path, capsys):
    data = _data_dir(tmp_path)
    lock = storage_util.InstanceLock(str(data)).acquire()
    try:
        assert dd.main(['--data-dir', str(data), '--apply']) == 1
    finally:
        lock.release()
    assert 'a Manager is running' in capsys.readouterr().out
    assert _rows(data / 'box' / 'data.db', 'SELECT COUNT(*) FROM system_metrics') == [(8,)]
    assert not list(data.rglob('*.bak'))


def test_a_failed_check_rolls_the_database_back(tmp_path, monkeypatch, capsys):
    data = _data_dir(tmp_path)
    box = data / 'box' / 'data.db'
    real = dd._dedupe_folder_scans

    def broken(conn):
        real(conn)
        raise dd.CheckFailed('simulated')                 # after system_metrics/user_metrics were deleted
    monkeypatch.setattr(dd, '_dedupe_folder_scans', broken)
    assert dd.main(['--data-dir', str(data), '--apply']) == 2
    out = capsys.readouterr().out
    assert '[FAILED] box: simulated (rolled back, unchanged)' in out
    assert '[OK]     metrics.db' in out                   # the other database was still done
    assert _rows(box, 'SELECT COUNT(*) FROM system_metrics') == [(8,)]
    assert _rows(box, 'SELECT COUNT(*) FROM folder_usage_msgs') == [(4,)]


def test_only_limits_the_databases(tmp_path, capsys):
    data = _data_dir(tmp_path)
    assert dd.main(['--data-dir', str(data), '--apply', '--only', 'box']) == 0
    assert _rows(data / 'box' / 'data.db', 'SELECT COUNT(*) FROM system_metrics') == [(5,)]
    assert _rows(data / 'metrics.db', 'SELECT COUNT(*) FROM user_metrics_history') == [(4,)]
    assert dd.main(['--data-dir', str(data), '--only', 'nosuch']) == 1
    assert '--only matched no database' in capsys.readouterr().out


def test_manager_config_gives_the_data_dir(tmp_path, capsys):
    data = _data_dir(tmp_path)
    cfg = tmp_path / 'config.json'
    cfg.write_text('{"data_dir": %s, "auth_tokens": ["secret-token-not-printed-0123"]}'
                   % ('"' + str(data).replace('\\', '\\\\') + '"'), encoding='utf-8')
    assert dd.main(['--manager-config', str(cfg)]) == 0
    out = capsys.readouterr().out
    assert str(data) in out and 'secret-token' not in out
