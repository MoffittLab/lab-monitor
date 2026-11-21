"""Read endpoints, schema-evolution edge cases, limits, error bodies."""
import os

import manager as mgr
from manager_helpers import ADMIN, COLL_TRITON, READ, auth, make_msg, metrics_payload, post_queue


def test_unknown_system_is_404_and_creates_nothing(client, data_dir):
    before = set(os.listdir(data_dir))
    for path in ('/api/data/Ghost', '/api/data/Ghost/system_metrics',
                 '/api/history/Ghost/system_metrics/cpu_percent',
                 '/api/usage/history/Ghost', '/api/usage/nas/Ghost', '/api/metrics/nas/Ghost',
                 '/api/user_metrics/Ghost', '/api/user_metrics/history/Ghost/alice'):
        r = client.get(path, headers=auth(READ))
        assert r.status_code == 404, path
        assert r.get_json()['status'] == 'error'
    assert set(os.listdir(data_dir)) == before
    assert not os.path.exists(os.path.join(data_dir, 'ghost'))


def test_invalid_names_400(client):
    assert client.get('/api/data/bad%20name', headers=auth(READ)).status_code == 400
    assert client.get('/api/data/Triton/sqlite_master', headers=auth(READ)).status_code == 400
    assert client.delete('/api/system/a%2Fb', headers=auth(ADMIN)).status_code in (400, 404, 405)


def test_known_system_unknown_type_is_empty(client):
    post_queue(client, [make_msg('system_metrics', metrics_payload())])
    r = client.get('/api/data/Triton/gpu_stats', headers=auth(READ))
    assert r.status_code == 200 and r.get_json()['rows'] == []
    r = client.get('/api/history/Triton/system_metrics/no_such_field', headers=auth(READ))
    assert r.status_code == 200 and r.get_json()['data'] == []


def test_case_colliding_columns(client):
    post_queue(client, [make_msg('custom', {'Temp': 1.0}, ts='2026-08-04T07:00:00Z')])
    # same column, different case: stored in the existing column
    ack = post_queue(client, [make_msg('custom', {'TEMP': 2.0}, ts='2026-08-04T07:01:00Z')]).get_json()
    assert ack['counts']['stored'] == 1
    # two keys in one payload that collide case-insensitively: rejected, batch fine
    ack = post_queue(client, [make_msg('custom', {'a': 1, 'A': 2}, ts='2026-08-04T07:02:00Z'),
                              make_msg('custom', {'Temp': 3.0}, ts='2026-08-04T07:03:00Z')]).get_json()
    assert [r['status'] for r in ack['results']] == ['rejected', 'stored']
    rows = client.get('/api/data/Triton/custom', headers=auth(READ)).get_json()['rows']
    assert [r['Temp'] for r in rows] == [3.0, 2.0, 1.0]


def test_payload_id_cannot_set_primary_key(client):
    post_queue(client, [make_msg('custom', {'id': 777, 'timestamp': 'x', 'msg_id': 'y', 'v': 1})])
    post_queue(client, [make_msg('custom', {'id': 777, 'v': 2})])     # would collide as a PK
    rows = client.get('/api/data/Triton/custom', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 2
    assert {r['payload_id'] for r in rows} == {777}
    assert all(r['id'] != 777 for r in rows)
    assert rows[1]['payload_timestamp'] == 'x' and rows[1]['payload_msg_id'] == 'y'


def test_not_specified_data_type(client):
    ack = post_queue(client, [make_msg(None, {'x': 1})]).get_json()
    assert ack['counts']['stored'] == 1
    assert 'not_specified' in client.get('/api/data/Triton', headers=auth(READ)).get_json()['data_types']


def test_oversize_body_413(make_client):
    client = make_client(max_request_bytes=2048)
    big = make_msg('system_metrics', {**metrics_payload(), 'blob': 'x' * 5000})
    r = post_queue(client, [big])
    assert r.status_code == 413
    assert r.get_json() == {'status': 'error', 'error': 'Request body too large'}


def test_too_many_messages_413(make_client):
    client = make_client(max_messages_per_request=3)
    r = post_queue(client, [make_msg('custom', {'v': i}) for i in range(4)])
    assert r.status_code == 413
    assert r.get_json()['status'] == 'error'


def test_message_cap_exempts_old_collectors(make_client):
    # Old collectors (no msg_id) post their whole queue and can't split it:
    # a 413 would repeat forever, so the count cap doesn't apply to them.
    client = make_client(max_messages_per_request=3)
    msgs = [make_msg('custom', {'v': i}, ts=f'2026-08-04T07:00:0{i}Z', msg_id=None) for i in range(6)]
    r = post_queue(client, msgs)
    assert r.status_code == 200
    ack = r.get_json()
    assert ack['counts']['stored'] == 6 and ack['stored'] == 6

    # the legacy 'entries' format is an old collector too
    body = {'queue_id': 'Triton-legacy', 'name': 'Triton', 'id': 'id-triton',
            'entries': [{'type': 'metrics', 'timestamp': f'2026-08-04T08:00:0{i}Z', 'cpu_percent': 1.0}
                        for i in range(5)]}
    r = client.post('/api/data/queue', json=body, headers=auth(COLL_TRITON))
    assert r.status_code == 200 and r.get_json()['counts']['stored'] == 5

    # one msg_id in the batch makes it a new-collector batch: capped
    msgs[0] = make_msg('custom', {'v': 99})
    assert post_queue(client, msgs).status_code == 413


def test_oversize_body_logs_remedy(make_client, caplog):
    client = make_client(max_request_bytes=2048)
    big = make_msg('system_metrics', {**metrics_payload(), 'blob': 'x' * 5000})
    with caplog.at_level('WARNING'):
        assert post_queue(client, [big]).status_code == 413
    assert any('max_request_bytes' in r.getMessage() and 'old collector' in r.getMessage()
               for r in caplog.records)


def test_out_of_range_timestamp_param_is_400(client):
    post_queue(client, [make_msg('system_metrics', metrics_payload())])
    base = '/api/history/Triton/system_metrics/cpu_percent'
    for ts in ('0001-01-01T00:00:00%2B01:00', '9999-12-31T23:59:59-01:00'):
        r = client.get(f'{base}?from={ts}', headers=auth(READ))
        assert r.status_code == 400, ts
        assert r.get_json()['status'] == 'error'
    r = client.get('/api/data/Triton/system_metrics?start=0001-01-01T00:00:00%2B01:00'
                   '&end=2026-01-01T00:00:00Z', headers=auth(READ))
    assert r.status_code == 400


def test_window_limit_truncated_and_max_points(client):
    msgs = [make_msg('system_metrics', metrics_payload(cpu=float(i)), ts=f'2026-08-04T{i // 60:02d}:{i % 60:02d}:00Z')
            for i in range(120)]
    post_queue(client, msgs)
    base = '/api/history/Triton/system_metrics/cpu_percent'

    r = client.get(base + '?limit=10', headers=auth(READ)).get_json()   # tail
    assert [p['value'] for p in r['data']] == [float(i) for i in range(110, 120)]
    assert r['truncated'] is True and r['unit'] == '%'

    # window over the limit: the whole range is bucket-averaged, the newest
    # data is not cut off (older Dashboards send from/to without max_points)
    r = client.get(base + '?from=2026-08-04T00:00:00Z&to=2026-08-05T00:00:00Z&limit=50', headers=auth(READ)).get_json()
    assert 40 <= len(r['data']) <= 50
    assert r['truncated'] is False and r['downsampled'] is True
    assert r['data'][0]['value'] < 2 and r['data'][-1]['value'] > 117
    assert r['data'][-1]['timestamp'] >= '2026-08-04T01:57'

    # window within the limit: raw points
    r = client.get(base + '?from=2026-08-04T00:00:00Z&to=2026-08-05T00:00:00Z&limit=120', headers=auth(READ)).get_json()
    assert [p['value'] for p in r['data']] == [float(i) for i in range(120)]
    assert r['truncated'] is False and r['downsampled'] is False

    r = client.get(base + '?from=2026-08-04T00:00:00Z&to=2026-08-05T00:00:00Z&max_points=12',
                   headers=auth(READ)).get_json()
    assert r['downsampled'] is True and r['truncated'] is False
    assert len(r['data']) == 12
    assert abs(r['data'][0]['value'] - 4.5) < 1e-9          # mean of 0..9
    assert abs(r['data'][-1]['value'] - 114.5) < 1e-9
    assert r['data'][0]['timestamp'].endswith('Z')

    r = client.get(base + '?max_points=4&limit=40', headers=auth(READ)).get_json()   # tail + downsample
    assert r['downsampled'] is True and len(r['data']) == 4
    assert abs(r['data'][0]['value'] - 84.5) < 1e-9

    r = client.get('/api/data/Triton/system_metrics?start=2026-08-04T00:00:00Z&end=2026-08-05T00:00:00Z',
                   headers=auth(READ)).get_json()
    assert len(r['rows']) == 120 and r['truncated'] is False

    assert client.get(base + '?max_points=abc', headers=auth(READ)).status_code == 400
    assert client.get(base + '?from=notatime', headers=auth(READ)).status_code == 400


def test_errors_do_not_leak_exception_text(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('C:\\secret\\path exploded')
    monkeypatch.setattr(mgr._metrics_db, 'get_all_devices', boom)
    r = client.get('/api/devices', headers=auth(READ))
    assert r.status_code == 500
    assert r.get_json() == {'status': 'error', 'error': 'Internal server error'}
    assert client.get('/no/such/route', headers=auth(READ)).get_json()['status'] == 'error'


def test_cors_off_by_default(client):
    r = client.get('/api/devices', headers={**auth(READ), 'Origin': 'http://evil.example'})
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_names_unified_between_stores(client, data_dir):
    post_queue(client, [make_msg('system_metrics', metrics_payload(), name='Lab-A')],
               token=mgr_admin(), name='Lab-A')
    post_queue(client, [make_msg('system_metrics', metrics_payload(2000, 1000), name='lab-a',
                                 ts='2026-08-04T08:00:00Z')], token=mgr_admin(), name='lab-a')
    devices = client.get('/api/devices', headers=auth(READ)).get_json()['devices']
    assert [d['name'] for d in devices] == ['Lab-A']           # one device, first spelling
    assert os.listdir(data_dir).count('lab_a') == 1
    rows = client.get('/api/data/lab-a/system_metrics', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 2
    assert client.get('/api/metrics/nas/LAB-A', headers=auth(READ)).status_code == 200


def mgr_admin():
    return ADMIN


def test_usage_history_endpoint(client):
    post_queue(client, [make_msg('folder_usage', {'/volume1': 5, 'total_usage': 5}, ts=mgr.utc_now())],
               token=COLL_TRITON)
    r = client.get('/api/usage/history/Triton?days=7', headers=auth(READ)).get_json()
    assert len(r['records']) == 1 and r['records'][0]['/volume1'] == 5
