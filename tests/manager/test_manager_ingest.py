"""Ingest: msg_id dedup, per-message ACK, dead_letter, legacy formats, transient errors."""
import os
import sqlite3

import manager as mgr
from manager_helpers import (
    ADMIN, LEGACY, READ, auth, make_msg, metrics_payload, post_queue,
)


def _batch(msg_id='auto'):
    return [
        make_msg('system_metrics', metrics_payload(1000, 500), ts='2026-08-04T07:00:00Z', msg_id=msg_id),
        make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 5.0, 'ram_bytes': 10,
                                  'ram_formatted': '10 B'}, ts='2026-08-04T07:00:00.000100Z', msg_id=msg_id),
        make_msg('user_metrics', {'username': 'bob', 'cpu_percent': 1.0, 'ram_bytes': 20,
                                  'ram_formatted': '20 B'}, ts='2026-08-04T07:00:00.000200Z', msg_id=msg_id),
        make_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 10, '/volume1': 10, 'total_usage': 10},
                 ts='2026-08-04T07:00:01Z', msg_id=msg_id),
    ]


def _count(db_path, sql):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_ack_shape_and_dedup_same_batch_twice(client, data_dir):
    batch = _batch()
    r1 = post_queue(client, batch, queue_id='Triton-aaaaaaaaaaaa')
    assert r1.status_code == 200
    a1 = r1.get_json()
    assert a1['status'] == 'ok' and a1['queue_id'] == 'Triton-aaaaaaaaaaaa'
    assert a1['received'] == 4 and a1['stored'] == 4
    assert a1['counts'] == {'stored': 4, 'duplicate': 0, 'rejected': 0, 'error': 0}
    assert [r['msg_id'] for r in a1['results']] == [m['header']['msg_id'] for m in batch]
    assert all(r['status'] == 'stored' for r in a1['results'])

    totals_before = client.get('/api/totals', headers=auth(READ)).get_json()

    r2 = post_queue(client, batch)
    a2 = r2.get_json()
    assert a2['received'] == 4 and a2['stored'] == 4      # old collectors keep draining
    assert a2['counts'] == {'stored': 0, 'duplicate': 4, 'rejected': 0, 'error': 0}

    # no double accumulation / no duplicate history rows
    assert client.get('/api/totals', headers=auth(READ)).get_json() == totals_before
    assert totals_before['devices']['Triton']['total_bytes_in'] == 1000
    db = os.path.join(data_dir, 'triton', 'data.db')
    assert _count(db, 'SELECT COUNT(*) FROM system_metrics') == 1
    assert _count(db, 'SELECT COUNT(*) FROM user_metrics') == 2
    assert _count(db, 'SELECT COUNT(*) FROM folder_usage_msgs') == 1
    assert _count(os.path.join(data_dir, 'metrics.db'), 'SELECT COUNT(*) FROM user_metrics_history') == 2


def test_duplicate_within_one_batch(client):
    m = make_msg('system_metrics', metrics_payload())
    ack = post_queue(client, [m, m]).get_json()
    assert [r['status'] for r in ack['results']] == ['stored', 'duplicate']
    assert ack['stored'] == 2


def test_legacy_messages_without_msg_id_dedup(client):
    batch = _batch(msg_id=None)
    a1 = post_queue(client, batch).get_json()
    assert a1['counts']['stored'] == 4
    ids = [r['msg_id'] for r in a1['results']]
    assert all(i.startswith('legacy-') for i in ids)
    assert len(set(ids)) == 4          # username distinguishes the user_metrics rows

    a2 = post_queue(client, batch).get_json()
    assert a2['counts'] == {'stored': 0, 'duplicate': 4, 'rejected': 0, 'error': 0}
    assert [r['msg_id'] for r in a2['results']] == ids
    totals = client.get('/api/totals', headers=auth(READ)).get_json()
    assert totals['devices']['Triton']['total_bytes_in'] == 1000


def test_legacy_id_is_deterministic_contract():
    import hashlib
    header = {'device_name': 'Triton', 'timestamp': '2026-08-04T07:00:00Z'}
    payload = {'data_type': 'user_metrics', 'username': 'alice'}
    expected = 'legacy-' + hashlib.sha1(b'Triton|user_metrics|2026-08-04T07:00:00Z|alice').hexdigest()
    assert mgr._legacy_id('Triton', header, payload) == expected


def test_one_bad_message_does_not_fail_batch(client):
    good1 = make_msg('system_metrics', metrics_payload(), ts='2026-08-04T07:00:00Z')
    bad = make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 'abc', 'ram_bytes': 1},
                   ts='2026-08-04T07:00:01Z')
    no_ts = make_msg('system_metrics', metrics_payload())
    del no_ts['header']['timestamp']
    good2 = make_msg('user_metrics', {'username': 'bob', 'cpu_percent': 3.0, 'ram_bytes': 2},
                     ts='2026-08-04T07:00:02Z')

    r = post_queue(client, [good1, bad, no_ts, good2])
    assert r.status_code == 200
    ack = r.get_json()
    statuses = [x['status'] for x in ack['results']]
    assert statuses == ['stored', 'rejected', 'rejected', 'stored']
    assert ack['stored'] == ack['received'] == 4
    assert 'cpu_percent' in ack['results'][1]['error']
    assert ack['results'][2]['error'] == 'missing timestamp'

    dl = client.get('/api/dead_letter', headers=auth(ADMIN)).get_json()['dead_letters']
    by_id = {d['msg_id']: d for d in dl}
    assert bad['header']['msg_id'] in by_id and no_ts['header']['msg_id'] in by_id
    assert '"abc"' in by_id[bad['header']['msg_id']]['raw_json']

    # the good user row landed in the central snapshot; the bad one did not
    users = client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json()
    assert [u['username'] for u in users] == ['bob']


def test_rejected_resent_stays_rejected_and_counted(client):
    bad = make_msg('system_metrics', {'cpu_percent': 'x'})
    post_queue(client, [bad])
    ack = post_queue(client, [bad]).get_json()
    assert ack['results'][0]['status'] == 'rejected'
    assert ack['stored'] == 1
    assert len(client.get('/api/dead_letter', headers=auth(ADMIN)).get_json()['dead_letters']) == 1


def test_shape_name_and_data_type_rejections(client):
    wrong_name = make_msg('system_metrics', metrics_payload(), name='Atlas')
    bad_type = make_msg('sqlite_master', {'x': 1})
    bad_type2 = make_msg('folder_usage_items', {'x': 1})
    weird = make_msg('bad type!', {'x': 1})
    bad_ts = make_msg('system_metrics', metrics_payload(), ts='yesterday')
    bad_id = make_msg('system_metrics', metrics_payload(), msg_id='has spaces')
    ack = post_queue(client, [wrong_name, bad_type, bad_type2, weird, bad_ts, bad_id, 'garbage', {'header': 5}]).get_json()
    assert [x['status'] for x in ack['results']] == ['rejected'] * 8
    assert ack['stored'] == 8
    assert ack['results'][0]['error'] == "device_name does not match the queue's name"
    assert ack['results'][6]['msg_id'].startswith('invalid-')


def test_transient_error_is_not_persisted_and_retry_succeeds(client, data_dir, monkeypatch):
    m = make_msg('system_metrics', metrics_payload(4000, 4000))
    real = mgr._metrics_db.apply_side_effects

    def locked(*a, **k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(mgr._metrics_db, 'apply_side_effects', locked)

    ack = post_queue(client, [m]).get_json()
    assert ack['results'][0]['status'] == 'error'
    assert ack['stored'] == 0 and ack['counts']['error'] == 1
    # rolled back: no history row (the table creation itself was undone too)
    assert client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows'] == []

    monkeypatch.setattr(mgr._metrics_db, 'apply_side_effects', real)
    ack = post_queue(client, [m]).get_json()
    assert ack['results'][0]['status'] == 'stored'
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']['total_bytes_in'] == 4000


def test_unexpected_exception_is_rejected_not_500(client, monkeypatch):
    def boom(*a, **k):
        raise TypeError('secret internal detail')
    monkeypatch.setattr(mgr._metrics_db, 'apply_side_effects', boom)
    r = post_queue(client, [make_msg('system_metrics', metrics_payload())])
    assert r.status_code == 200
    res = r.get_json()['results'][0]
    assert res['status'] == 'rejected'
    assert 'secret internal detail' not in r.get_data(as_text=True)


def test_legacy_entries_format(make_client):
    client = make_client(auth_tokens=[LEGACY])   # old deployments: one shared token
    body = {
        'queue_id': 'Triton-2026', 'name': 'Triton', 'id': 'id-triton',
        'entries': [
            {'type': 'metrics', 'timestamp': '2026-08-04T07:00:00Z', 'cpu_percent': 3.0,
             'network_bytes_in': 10, 'network_bytes_out': 20},
            {'type': 'disk', 'cpu': 1},     # no timestamp -> rejected, still counted
        ],
    }
    ack = client.post('/api/data/queue', json=body, headers=auth(LEGACY)).get_json()
    assert [x['status'] for x in ack['results']] == ['stored', 'rejected']
    assert ack['stored'] == 2
    m = client.get('/api/metrics/nas/Triton', headers=auth(READ)).get_json()
    assert m['cpu_percent'] == 3.0


def test_timestamps_are_canonicalized(client):
    post_queue(client, [make_msg('system_metrics', metrics_payload(), ts='2026-08-04T09:00:00+02:00')])
    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert rows[0]['timestamp'] == '2026-08-04T07:00:00.000000Z'
    assert rows[0]['msg_id']


def test_malformed_bodies(client):
    from manager_helpers import COLL_TRITON
    h = auth(COLL_TRITON)
    assert client.post('/api/data/queue', data='not json', headers={**h, 'Content-Type': 'application/json'}).status_code == 400
    assert client.post('/api/data/queue', json=[1, 2], headers=h).status_code == 400
    assert client.post('/api/data/queue', json={'name': 'Triton'}, headers=h).status_code == 400
    r = client.post('/api/data/queue', json={'queue_id': 'q', 'name': '../etc', 'id': 'x', 'messages': []}, headers=h)
    assert r.status_code == 400
    assert r.get_json() == {'status': 'error', 'error': 'Invalid device name'}
    r = client.post('/api/data/queue', json={'queue_id': 'q', 'name': 'Triton', 'id': 'x', 'messages': {}}, headers=h)
    assert r.status_code == 400


def test_unmeasured_values_stay_null(client):
    payload = metrics_payload()
    payload.update(cpu_percent=None, uptime_formatted=None, total_ram_bytes=None)
    post_queue(client, [make_msg('system_metrics', payload)])
    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['cpu_percent'] is None
    assert m['total_ram_bytes'] is None
    assert m['ram_percent'] == 40.0


def test_invalid_data_shape_echoes_header_msg_id(client):
    # A new collector removes only msg_ids it sent: a rejected message must
    # come back under its own id, or it would be re-sent forever.
    no_data = {'header': {'msg_id': 'abc123', 'device_name': 'Triton',
                          'timestamp': '2026-08-04T07:00:00Z'}, 'data': None}
    list_data = {'header': {'msg_id': 'def456', 'device_name': 'Triton',
                            'timestamp': '2026-08-04T07:00:00Z'}, 'data': [1, 2]}
    bad_id = {'header': {'msg_id': 'not valid!', 'device_name': 'Triton'}, 'data': 5}
    ack = post_queue(client, [no_data, list_data, bad_id]).get_json()
    assert [r['status'] for r in ack['results']] == ['rejected'] * 3
    assert [r['msg_id'] for r in ack['results']][:2] == ['abc123', 'def456']
    assert ack['results'][2]['msg_id'].startswith('invalid-')
    dead = client.get('/api/dead_letter', headers=auth(ADMIN)).get_json()['dead_letters']
    assert {d['msg_id'] for d in dead} >= {'abc123', 'def456'}


def test_out_of_range_timestamp_is_rejected_as_invalid(client):
    m = make_msg('system_metrics', metrics_payload(), ts='0001-01-01T00:00:00+01:00')
    ack = post_queue(client, [m]).get_json()
    assert ack['results'][0]['status'] == 'rejected'
    assert ack['results'][0]['error'] == 'invalid timestamp'


def test_corrupt_system_db_is_error_not_rejected(client, data_dir):
    # A damaged data.db is a storage problem: collectors must KEEP the
    # messages (error), not drop them as rejected.
    post_queue(client, [make_msg('system_metrics', metrics_payload())])
    mgr._data_store.close()
    db_path = os.path.join(data_dir, 'triton', 'data.db')
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    with open(db_path, 'wb') as f:
        f.write(b'this is not a sqlite database' * 200)

    ack = post_queue(client, [make_msg('system_metrics', metrics_payload(), ts='2026-08-04T08:00:00Z')]).get_json()
    assert ack['results'][0]['status'] == 'error'
    assert ack['stored'] == 0 and ack['counts']['error'] == 1
    assert 'not a database' not in str(ack)    # no exception text in the ACK
    assert client.get('/api/dead_letter', headers=auth(ADMIN)).get_json()['dead_letters'] == []


def test_storage_error_classification():
    from storage_util import is_transient_db_error
    import data_store

    def op_error(msg, code=None):
        e = sqlite3.OperationalError(msg)
        if code is not None:
            e.sqlite_errorcode = code
        return e

    storage = [
        op_error('database is locked'), op_error('database is locked', 5),
        op_error('disk I/O error', 10 | (3 << 8)),          # extended SQLITE_IOERR_WRITE
        op_error('unable to open database file', 14),
        sqlite3.DatabaseError('database disk image is malformed'),
        sqlite3.DatabaseError('file is not a database'),
        sqlite3.InternalError('x'),
        sqlite3.ProgrammingError('Cannot operate on a closed database.'),
        PermissionError('denied'), MemoryError(),
    ]
    message = [
        op_error('table x has no column named locked_files', 1),   # SQLITE_ERROR
        sqlite3.IntegrityError('UNIQUE constraint failed'),
        sqlite3.DataError('string or blob too big'),
        sqlite3.InterfaceError('Error binding parameter'),
        sqlite3.ProgrammingError('Incorrect number of bindings'),
        data_store.RejectedMessage('bad'), TypeError('x'), OverflowError('x'),
    ]
    assert [is_transient_db_error(e) for e in storage] == [True] * len(storage)
    assert [is_transient_db_error(e) for e in message] == [False] * len(message)
