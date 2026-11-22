"""
Data an older Manager stored that must stay readable after the upgrade,
beyond the plain history rows (the old databases are written by the old code
itself, old_manager.old_ingest):

  - numeric folder_usage metadata columns of the old wide table, e.g.
    "_scan_seconds" from a newer collector that reported to the old Manager:
    the old /api/history charted them, so the new one does (migrated rows and
    new messages alike);
  - per-user rows the old DELETE /api/system/<name> left behind after it
    removed the device: the old per-user endpoints kept serving them;
  - an unmigrated (held-back) wide folder table reads exactly like the
    migrated one: values as stored, JSON lists as their text.
"""
import os
import sqlite3

import data_store
from manager_helpers import ADMIN, READ, auth, make_msg, post_queue
from old_manager import old_ingest, old_msg

OLD_SCANS = [
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 100, '/volume1': 300, 'total_usage': 300},
            '2026-08-02T02:00:00Z'),
    # a NEW collector reporting to the OLD Manager (mixed-version rollout)
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 110, '/volume1': 310, 'total_usage': 310,
                             '_timed_out': ['/volume1/B'], '_errors': [], '_scan_seconds': 812.5},
            '2026-08-03T02:00:00.250000Z'),
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 120, '/volume1': 320, 'total_usage': 320},
            '2026-08-04T02:00:00.5Z'),
]


def _history(client, field, **params):
    r = client.get(f'/api/history/Triton/folder_usage/{field}', query_string=params, headers=auth(READ))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_numeric_folder_metadata_stays_chartable(make_client, data_dir):
    old_ingest(data_dir, [{'name': 'Triton', 'id': 'id-triton', 'messages': OLD_SCANS}])
    cols = {r[1]: r[2] for r in sqlite3.connect(os.path.join(data_dir, 'triton', 'data.db'))
            .execute('PRAGMA table_info(folder_usage)')}
    assert cols['_scan_seconds'] == 'REAL' and cols['_timed_out'] == 'TEXT'   # old wide layout
    client = make_client()

    tail = _history(client, '_scan_seconds')
    assert tail['field'] == '_scan_seconds'
    assert [(p['timestamp'], p['value']) for p in tail['data']] == [('2026-08-03T02:00:00.250000Z', 812.5)]
    window = _history(client, '_scan_seconds', **{'from': '2026-08-01T00:00:00Z', 'to': '2026-08-05T00:00:00Z'})
    assert [p['value'] for p in window['data']] == [812.5]
    # a text metadata column is not a series; folders are unaffected
    assert _history(client, '_timed_out')['data'] == []
    assert [p['value'] for p in _history(client, 'volume1/A')['data']] == [100, 110, 120]

    # new messages carry it too (stored as metadata of the narrow layout)
    for i, secs in enumerate((30.0, 45.5, 60.0)):
        r = post_queue(client, [make_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 130 + i,
                                                          '_scan_seconds': secs, '_timed_out': []},
                                         ts=f'2026-08-0{5 + i}T02:00:00.000000Z')], token=ADMIN)
        assert r.get_json()['counts']['stored'] == 1
    assert [p['value'] for p in _history(client, '_scan_seconds')['data']] == [812.5, 30.0, 45.5, 60.0]
    assert [p['value'] for p in _history(client, '_scan_seconds', limit=2)['data']] == [45.5, 60.0]
    dz = _history(client, '_scan_seconds', **{'from': '2000-01-01T00:00:00Z', 'max_points': 2})
    assert dz['downsampled'] is True and len(dz['data']) <= 2
    assert all(30.0 <= p['value'] <= 812.5 for p in dz['data'])


def test_per_user_rows_of_a_deleted_device_stay_readable(make_client, data_dir):
    users = [old_msg('user_metrics', {'username': u, 'cpu_percent': c, 'ram_bytes': 1000,
                                      'ram_formatted': '1000 B'}, ts, name='gone-ws')
             for ts in ('2026-08-01T04:00:07Z', '2026-08-01T04:05:07.5Z') for u, c in (('carol', 4.5), ('dave', 1.0))]
    old_ingest(data_dir, [{'name': 'gone-ws', 'id': 'id-gone-ws', 'messages': users}])
    # what the old DELETE /api/system/gone-ws did: data.db and three central tables, NOT the per-user rows
    os.remove(os.path.join(data_dir, 'gone_ws', 'data.db'))
    con = sqlite3.connect(os.path.join(data_dir, 'metrics.db'))
    with con:
        for table in ('devices', 'device_snapshot', 'device_totals'):
            con.execute(f'DELETE FROM {table} WHERE name = ?', ('gone-ws',))
    con.close()

    client = make_client()
    r = client.get('/api/user_metrics/gone-ws', headers=auth(READ))
    assert r.status_code == 200
    assert [(u['username'], u['cpu_percent']) for u in r.get_json()] == [('carol', 4.5), ('dave', 1.0)]
    r = client.get('/api/user_metrics/history/gone-ws/carol', headers=auth(READ))
    assert r.status_code == 200
    assert [h['timestamp'] for h in r.get_json()] == ['2026-08-01T04:05:07.5Z', '2026-08-01T04:00:07Z']
    assert 'gone-ws' in client.get('/api/user_metrics/all', headers=auth(READ)).get_json()
    # still 404 for names nothing is known about, and nothing is created
    assert client.get('/api/user_metrics/never-seen', headers=auth(READ)).status_code == 404
    assert client.get('/api/user_metrics/history/never-seen/carol', headers=auth(READ)).status_code == 404
    assert not os.path.exists(os.path.join(data_dir, 'never_seen'))
    assert not os.path.exists(os.path.join(data_dir, 'gone_ws', 'data.db'))


def test_held_back_wide_folder_rows_read_like_migrated_ones(make_client, data_dir, monkeypatch):
    old_ingest(data_dir, [{'name': 'Triton', 'id': 'id-triton', 'messages': OLD_SCANS}])

    def failing_backup(*a, **kw):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(data_store, 'backup_database', failing_backup)
    client = make_client()

    def rows():
        r = client.get('/api/data/Triton/folder_usage', query_string={'limit': 10}, headers=auth(READ))
        assert r.status_code == 200
        return [{k: v for k, v in row.items() if k not in ('id', 'msg_id', 'timestamp')}
                for row in r.get_json()['rows']]

    held_back = rows()
    assert data_store.FOLDER_USAGE in {r[0] for r in sqlite3.connect(
        os.path.join(data_dir, 'triton', 'data.db')).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert held_back[1]['_timed_out'] == '["/volume1/B"]'       # as the old Manager returned it
    monkeypatch.undo()
    client = make_client()                                       # restart: backup works, table migrated
    assert rows() == held_back


def test_old_table_with_a_now_invalid_data_type_stays_readable(make_client, data_dir):
    sensors = [old_msg('1wire_sensors', {'temp_c': t, 'temp_c_unit': 'C'}, f'2026-08-02T12:0{i}:05Z')
               for i, t in enumerate((21.5, 22.0, 22.5))]
    old_ingest(data_dir, [{'name': 'Triton', 'id': 'id-triton', 'messages': sensors + OLD_SCANS}])
    client = make_client()
    types = client.get('/api/data/Triton', headers=auth(READ)).get_json()['data_types']
    assert '1wire_sensors' in types and 'folder_usage' in types
    assert not [t for t in types if t.startswith(('folder_usage_', 'sqlite_'))]
    r = client.get('/api/data/Triton/1wire_sensors', headers=auth(READ))
    assert r.status_code == 200 and [row['temp_c'] for row in r.get_json()['rows']] == [22.5, 22.0, 21.5]
    r = client.get('/api/history/Triton/1wire_sensors/temp_c', headers=auth(READ))
    assert r.status_code == 200 and [p['value'] for p in r.get_json()['data']] == [21.5, 22.0, 22.5]
    assert r.get_json()['unit'] == 'C'
    # the storage internals are not data types
    for internal in ('folder_usage_items', 'folder_usage_msgs', 'folder_usage_legacy', 'sqlite_sequence'):
        assert client.get(f'/api/data/Triton/{internal}', headers=auth(READ)).status_code == 400, internal
    # unknown odd labels are still refused, also for new messages ...
    assert client.get('/api/data/Triton/2nd_sensor', headers=auth(READ)).status_code == 400
    r = post_queue(client, [make_msg('2nd_sensor', {'temp_c': 23.0})], token=ADMIN)
    assert r.get_json()['counts']['rejected'] == 1
    r = post_queue(client, [make_msg('folder_usage_items', {'x': 1})], token=ADMIN)
    assert r.get_json()['counts']['rejected'] == 1
    # ... but the stream the old Manager accepted keeps going into its table
    r = post_queue(client, [make_msg('1wire_sensors', {'temp_c': 23.0, 'temp_c_unit': 'C'},
                                     ts='2026-08-02T12:15:05.000000Z')], token=ADMIN)
    assert r.get_json()['counts']['stored'] == 1
    r = client.get('/api/history/Triton/1wire_sensors/temp_c', headers=auth(READ))
    assert [p['value'] for p in r.get_json()['data']] == [21.5, 22.0, 22.5, 23.0]
    # a system without that old table can't start one
    r = post_queue(client, [make_msg('1wire_sensors', {'temp_c': 1.0}, name='Atlas')], token=ADMIN, name='Atlas')
    assert r.get_json()['counts']['rejected'] == 1
    assert not os.path.exists(os.path.join(data_dir, 'atlas'))
