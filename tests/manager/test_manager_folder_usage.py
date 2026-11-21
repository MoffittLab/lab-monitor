"""folder_usage: narrow storage, flat read shape, wide->narrow migration."""
import json
import math
import os
import sqlite3

from manager_helpers import READ, auth, make_msg, post_queue


def _folder_payload(n, base=1000):
    p = {'_unit': 'bytes'}
    for i in range(n):
        p[f'/volume1/share/folder_{i:05d}'] = base + i
    p['/volume1'] = 10 ** 13
    p['/volume1_total_bytes'] = 2 * 10 ** 13
    p['/volume1_free_bytes'] = 10 ** 13
    p['total_usage'] = 10 ** 13
    return p


def test_3000_folders_round_trip(client, data_dir):
    payload = _folder_payload(3000)
    payload['_timed_out'] = ['/volume1/slow']
    payload['_errors'] = ['/volume1/broken']
    payload['_scan_seconds'] = 12.5
    ack = post_queue(client, [make_msg('folder_usage', payload)]).get_json()
    assert ack['counts']['stored'] == 1, ack

    rows = client.get('/api/data/Triton/folder_usage?limit=5', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 1
    row = rows[0]
    for key, value in payload.items():
        assert row[key] == value, key
    assert {'id', 'msg_id', 'timestamp', 'device_type'} <= set(row)
    assert isinstance(row['/volume1/share/folder_00007'], int)   # exact integer bytes

    # the narrow layout really is narrow
    con = sqlite3.connect(os.path.join(data_dir, 'triton', 'data.db'))
    cols = [r[1] for r in con.execute('PRAGMA table_info(folder_usage_msgs)')]
    assert len(cols) == 5
    assert con.execute('SELECT COUNT(*) FROM folder_usage_items').fetchone()[0] == 3004
    con.close()

    # snapshot + totals
    usage = client.get('/api/usage/all', headers=auth(READ)).get_json()['Triton']
    assert usage['/volume1/share/folder_02999'] == 3999
    assert usage['_timed_out'] == ['/volume1/slow']
    assert client.get('/api/totals', headers=auth(READ)).get_json()['global']['total_disk_bytes'] == 10 ** 13

    types = client.get('/api/data/Triton', headers=auth(READ)).get_json()['data_types']
    assert types == ['folder_usage']


def test_folder_history_and_leading_slash(client):
    msgs = []
    for day in range(1, 6):
        p = {'_unit': 'bytes', '/volume1': day * 100, '/volume1/A': day, 'total_usage': day * 100}
        msgs.append(make_msg('folder_usage', p, ts=f'2026-08-0{day}T02:00:00Z'))
    post_queue(client, msgs)

    r = client.get('/api/history/Triton/folder_usage/volume1', headers=auth(READ)).get_json()
    assert r['field'] == '/volume1'
    assert r['unit'] == 'bytes'
    assert [p['value'] for p in r['data']] == [100.0, 200.0, 300.0, 400.0, 500.0]
    assert r['truncated'] is False

    r = client.get('/api/history/Triton/folder_usage/volume1%2FA?from=2026-08-02T00:00:00Z&to=2026-08-04T00:00:00Z',
                   headers=auth(READ)).get_json()
    assert [p['value'] for p in r['data']] == [2.0, 3.0]

    rows = client.get('/api/data/Triton/folder_usage?start=2026-08-01T00:00:00Z&end=2026-08-31T00:00:00Z&limit=2',
                      headers=auth(READ)).get_json()
    assert [x['/volume1/A'] for x in rows['rows']] == [1, 2]
    assert rows['truncated'] is True


def _old_store(db, table, timestamp, device_type, payload):
    """The pre-upgrade TypedDataStore.store(): one column per key, NaN backfill."""
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            timestamp   DATETIME NOT NULL,
            device_type TEXT     DEFAULT 'unknown'
        )
    ''')
    db.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_ts" ON "{table}" (timestamp DESC)')
    existing = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
    for col in sorted(set(payload) - existing - {'id', 'timestamp', 'device_type', 'data_type'}):
        col_type = 'TEXT' if isinstance(payload[col], (list, dict, str)) else 'REAL'
        qcol = col.replace('"', '""')
        db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{qcol}" {col_type}')
        backfill = float('nan') if col_type == 'REAL' else ''
        db.execute(f'UPDATE "{table}" SET "{qcol}" = ? WHERE "{qcol}" IS NULL', (backfill,))
    cols = ['timestamp', 'device_type'] + list(payload)
    vals = [timestamp, device_type] + [json.dumps(v) if isinstance(v, (list, dict)) else v
                                       for v in payload.values()]
    cnames = ', '.join('"' + c.replace('"', '""') + '"' for c in cols)
    db.execute(f'INSERT INTO "{table}" ({cnames}) VALUES ({", ".join("?" * len(cols))})', vals)
    db.commit()


def _build_old_db(data_dir):
    os.makedirs(os.path.join(data_dir, 'triton'))
    db = sqlite3.connect(os.path.join(data_dir, 'triton', 'data.db'))
    _old_store(db, 'folder_usage', '2026-08-01T02:00:00.123456Z', 'synology',
               {'_unit': 'bytes', '/volume1/A': 100.0, '/volume1/B': 200.0, '/volume1': 300.0, 'total_usage': 300.0})
    _old_store(db, 'folder_usage', '2026-08-02T02:00:00Z', 'synology',
               {'_unit': 'bytes', '/volume1/A': 110.0, '/volume1/C': 50.0, '/volume1': 360.0, 'total_usage': 360.0})
    _old_store(db, 'system_metrics', '2026-08-02T02:05:00.5Z', 'synology',
               {'cpu_percent': 5.0, 'network_bytes_in': 1.0, 'network_bytes_out': 2.0})
    db.close()


def test_wide_to_narrow_migration(make_client, data_dir):
    _build_old_db(data_dir)
    client = make_client()

    rows = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 2
    newest, oldest = rows
    assert oldest['timestamp'] == '2026-08-01T02:00:00.123456Z'
    assert newest['timestamp'] == '2026-08-02T02:00:00.000000Z'
    assert oldest['/volume1/A'] == 100 and oldest['/volume1/B'] == 200 and oldest['_unit'] == 'bytes'
    assert '/volume1/C' not in oldest                       # NaN backfill was "not measured"
    assert newest['/volume1/C'] == 50 and '/volume1/B' not in newest
    assert oldest['id'] == 1 and oldest['msg_id'] is None

    con = sqlite3.connect(os.path.join(data_dir, 'triton', 'data.db'))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'folder_usage' not in tables
    assert 'folder_usage_legacy' in tables                  # kept, not dropped
    assert con.execute('SELECT COUNT(*) FROM folder_usage_legacy').fetchone()[0] == 2
    con.close()

    # new rows append after the migrated ones; history spans both
    post_queue(client, [make_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 120, '/volume1': 400},
                                 ts='2026-08-03T02:00:00Z')])
    r = client.get('/api/history/Triton/folder_usage/volume1%2FA', headers=auth(READ)).get_json()
    assert [p['value'] for p in r['data']] == [100.0, 110.0, 120.0]

    # old wide system_metrics table gains msg_id on first write; old row kept
    post_queue(client, [make_msg('system_metrics', {'cpu_percent': 7.0, 'network_bytes_in': 5, 'network_bytes_out': 5},
                                 ts='2026-08-03T02:05:00Z')])
    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert [x['cpu_percent'] for x in rows] == [7.0, 5.0]
    assert rows[1]['msg_id'] is None and rows[0]['msg_id']

    assert sorted(client.get('/api/data/Triton', headers=auth(READ)).get_json()['data_types']) == \
        ['folder_usage', 'system_metrics']


def test_migration_runs_once_and_reopen_is_stable(make_client, data_dir):
    _build_old_db(data_dir)
    make_client()
    client = make_client()      # second init: reopen the migrated DB
    rows = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 2


def test_folder_usage_null_and_nonfinite_values(client):
    p = {'_unit': 'bytes', '/volume1/A': None, '/volume1/B': float('nan'), '/volume1': 5}
    ack = post_queue(client, [make_msg('folder_usage', p)]).get_json()
    assert ack['counts']['stored'] == 1
    row = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows'][0]
    assert row['/volume1/A'] is None and row['/volume1/B'] is None and row['/volume1'] == 5


def test_folder_usage_internal_key_collision_renamed(client):
    p = {'_unit': 'bytes', 'id': 99, 'timestamp': 1, '/volume1': 5}
    post_queue(client, [make_msg('folder_usage', p)])
    row = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows'][0]
    assert row['id'] != 99 and row['payload_id'] == 99
    assert row['payload_timestamp'] == 1
    assert not math.isnan(row['/volume1'])


def test_long_folder_paths_never_reject_the_scan(client):
    # Synology allows 255-byte names per component: a 522-char path is legal
    deep = '/volume1/' + 'a' * 255 + '/' + 'b' * 255
    absurd = '/volume1/' + 'c' * 5000
    payload = _folder_payload(2000)
    payload[deep] = 7
    payload[absurd] = 8
    payload['_errors'] = ['/volume1/broken']
    ack = post_queue(client, [make_msg('folder_usage', payload)]).get_json()
    assert ack['counts']['stored'] == 1, ack

    row = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows'][0]
    assert row[deep] == 7                                  # stored as-is
    assert row['/volume1/share/folder_01999'] == 2999      # the rest of the scan too
    assert absurd not in row                               # unusable key dropped ...
    assert row['_errors'][0] == '/volume1/broken'          # ... and reported
    assert len(row['_errors']) == 2 and row['_errors'][1].startswith('/volume1/ccc')
    assert row['_errors'][1].endswith('...') and len(row['_errors'][1]) < 300

    usage = client.get('/api/usage/all', headers=auth(READ)).get_json()['Triton']
    assert usage['/volume1/share/folder_00000'] == 1000
