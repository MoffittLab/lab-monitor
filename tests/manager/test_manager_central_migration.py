"""Upgrading an existing central metrics.db created by the previous Manager."""
import os
import sqlite3

from manager_helpers import READ, auth, make_msg, metrics_payload, post_queue

_OLD_SCHEMA = [
    '''CREATE TABLE devices (name TEXT PRIMARY KEY, system_id TEXT, device_type TEXT DEFAULT 'unknown',
                             first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL)''',
    '''CREATE TABLE device_snapshot (name TEXT PRIMARY KEY, metrics_timestamp DATETIME, cpu_percent REAL,
                                     ram_percent REAL, uptime_seconds INTEGER DEFAULT 0,
                                     uptime_formatted TEXT DEFAULT '0s', network_bandwidth_in_mbps REAL DEFAULT 0,
                                     network_bandwidth_out_mbps REAL DEFAULT 0, disk_timestamp DATETIME,
                                     total_disk_bytes INTEGER DEFAULT 0, folders_json TEXT DEFAULT '{}')''',
    '''CREATE TABLE device_totals (name TEXT PRIMARY KEY, total_bytes_in INTEGER DEFAULT 0,
                                   total_bytes_out INTEGER DEFAULT 0, last_bytes_in INTEGER DEFAULT 0,
                                   last_bytes_out INTEGER DEFAULT 0, total_disk_bytes INTEGER DEFAULT 0,
                                   last_updated DATETIME)''',
    '''CREATE TABLE device_user_snapshot (name TEXT NOT NULL, username TEXT NOT NULL, cpu_percent REAL,
                                          ram_bytes INTEGER, ram_formatted TEXT, timestamp TEXT,
                                          PRIMARY KEY (name, username))''',
    '''CREATE TABLE user_metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, device_name TEXT NOT NULL,
                                          timestamp TEXT NOT NULL, username TEXT NOT NULL, cpu_percent REAL,
                                          ram_bytes INTEGER, ram_formatted TEXT)''',
]


def _build_old_central(data_dir):
    os.makedirs(data_dir)
    db = sqlite3.connect(os.path.join(data_dir, 'metrics.db'))
    for stmt in _OLD_SCHEMA:
        db.execute(stmt)
    db.execute("INSERT INTO devices VALUES ('Triton', 'id-triton', 'synology', '2026-08-01T00:00:00.1Z', '2026-08-04T07:00:00.123456Z')")
    db.execute("INSERT INTO device_totals VALUES ('Triton', 5000, 2500, 1000, 500, 0, '2026-08-04T07:00:00.123456Z')")
    db.execute("INSERT INTO user_metrics_history (device_name, timestamp, username, cpu_percent) "
               "VALUES ('Triton', '2026-08-04T07:00:00Z', 'alice', 1.0)")
    db.commit()
    db.close()


def test_old_central_db_is_migrated_in_place(make_client, data_dir):
    _build_old_central(data_dir)
    client = make_client()

    con = sqlite3.connect(os.path.join(data_dir, 'metrics.db'))
    totals_cols = {r[1] for r in con.execute('PRAGMA table_info(device_totals)')}
    umh_cols = {r[1] for r in con.execute('PRAGMA table_info(user_metrics_history)')}
    snap_cols = {r[1] for r in con.execute('PRAGMA table_info(device_snapshot)')}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert 'network_last_updated' in totals_cols and 'msg_id' in umh_cols and 'metrics_json' in snap_cols
    assert 'dead_letter' in tables

    # registry and totals survive; replay guard was seeded from last_updated
    assert [d['name'] for d in client.get('/api/devices', headers=auth(READ)).get_json()['devices']] == ['Triton']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert t['total_bytes_in'] == 5000 and t['network_last_updated'] == '2026-08-04T07:00:00.123456Z'

    # an old-collector re-send of an already-counted reading is not counted again
    post_queue(client, [make_msg('system_metrics', metrics_payload(1000, 500), ts='2026-08-04T07:00:00.123456Z',
                                 msg_id=None)])
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']['total_bytes_in'] == 5000

    # a new reading accumulates normally; a lowercase spelling maps onto 'Triton'
    from manager_helpers import COLL_TRITON
    post_queue(client, [make_msg('system_metrics', metrics_payload(1600, 800), ts='2026-08-04T07:05:00Z',
                                 name='triton')], token=COLL_TRITON, name='triton')
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert list(t) == ['Triton']
    assert t['Triton']['total_bytes_in'] == 5600 and t['Triton']['total_bytes_out'] == 2800

    hist = client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()
    assert len(hist) == 1


# --------------------------------------------------------------------------- #
# The usage-metrics release: new per-user columns and snapshot tables         #
# --------------------------------------------------------------------------- #

import glob  # noqa: E402

from current_manager import run_current  # noqa: E402
from manager_helpers import ADMIN, base_config  # noqa: E402

USAGE_COLUMNS = {'cpu_seconds', 'cpu_cores', 'interval_s', 'process_count', 'gpu_vram_bytes', 'gpu_util_percent',
                 'gpu_busy_seconds', 'gpus_held', 'gpu_held_seconds', 'gpu_processes'}
NEW_TABLES = {'device_session_snapshot', 'device_storage_health', 'device_storage_breakdown'}


def _central(data_dir):
    return os.path.join(data_dir, 'metrics.db')


def _backups(path):
    return sorted(glob.glob(glob.escape(path) + '.pre-upgrade-*.bak'))


def _schema(path):
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {t: {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')} for t in tables}
        return tables, cols
    finally:
        con.close()


def test_old_central_db_gains_the_usage_columns_and_tables_once(make_client, data_dir):
    _build_old_central(data_dir)
    client = make_client()
    (bak,) = _backups(_central(data_dir))
    tables, cols = _schema(_central(data_dir))
    assert NEW_TABLES <= tables
    assert USAGE_COLUMNS <= cols['device_user_snapshot'] and USAGE_COLUMNS <= cols['user_metrics_history']
    # the copy holds the original layout
    btables, bcols = _schema(bak)
    assert not btables & NEW_TABLES and not bcols['user_metrics_history'] & USAGE_COLUMNS
    # old rows read NULL, and a NULL usage field is left out: the answer is what it was
    assert client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json() == [
        {'timestamp': '2026-08-04T07:00:00Z', 'cpu_percent': 1.0, 'ram_bytes': None, 'ram_formatted': None}]
    con = sqlite3.connect(_central(data_dir))
    assert con.execute('SELECT cpu_seconds, gpus_held FROM user_metrics_history').fetchall() == [(None, None)]
    con.close()
    for path in ('/api/sessions/all', '/api/storage/health/all', '/api/storage/breakdown/all'):
        assert client.get(path, headers=auth(READ)).get_json() == {}
    # a restart: nothing left to migrate, no second copy
    make_client()
    assert _backups(_central(data_dir)) == [bak]


def test_current_central_db_is_backed_up_before_the_usage_columns(make_client, data_dir):
    """A metrics.db the CURRENT Manager (160da09 / fb6498c) wrote has dead_letter and msg_id, but not the new layout."""
    user = make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 3.0, 'ram_bytes': 7, 'ram_formatted': '7 B'},
                    ts='2026-08-04T07:00:00.000000Z')
    gets = ['/api/user_metrics/all', '/api/user_metrics/Triton', '/api/user_metrics/history/Triton/alice',
            '/api/metrics/all', '/api/totals', '/api/devices']
    out = run_current(base_config(data_dir), ADMIN, gets=gets, posts=[
        {'queue_id': 'Triton-cur00001', 'name': 'Triton', 'id': 'id-triton',
         'messages': [user, make_msg('system_metrics', metrics_payload(), ts='2026-08-04T07:00:00.000000Z')]}])
    tables, cols = _schema(_central(data_dir))
    assert 'dead_letter' in tables and not tables & NEW_TABLES          # CURRENT layout
    assert not _backups(_central(data_dir))

    client = make_client()
    (bak,) = _backups(_central(data_dir))
    tables, cols = _schema(_central(data_dir))
    assert NEW_TABLES <= tables and USAGE_COLUMNS <= cols['device_user_snapshot']
    btables, _ = _schema(bak)
    assert 'dead_letter' in btables and not btables & NEW_TABLES        # the copy: CURRENT's layout
    # every answer about CURRENT's data is unchanged
    for path in gets:
        assert client.get(path, headers=auth(READ)).get_json() == out['gets'][path]['json'], path
    make_client()
    assert _backups(_central(data_dir)) == [bak]
