"""Plain helpers and tokens shared by the Manager tests (imported by name)."""
import uuid

ADMIN = 'adm-' + 'a1' * 16
READ = 'rd-' + 'b2' * 16
COLL_TRITON = 'ct-' + 'c3' * 16
COLL_ATLAS = 'ca-' + 'd4' * 16
LEGACY = 'lg-' + 'e5' * 16


def base_config(data_dir, **overrides):
    cfg = {
        'data_dir': data_dir,
        'admin_tokens': [ADMIN],
        'read_tokens': [READ],
        'collector_tokens': {'Triton': COLL_TRITON, 'Atlas': COLL_ATLAS},
    }
    cfg.update(overrides)
    return cfg


def auth(token):
    return {'Authorization': f'Bearer {token}'}


def make_msg(data_type, data, ts='2026-08-04T07:00:00.000000Z', name='Triton',
             msg_id='auto', device_type='synology'):
    header = {
        'device_name': name,
        'device_id': f'id-{name.lower()}',
        'device_type': device_type,
        'timestamp': ts,
    }
    if msg_id == 'auto':
        header['msg_id'] = uuid.uuid4().hex
    elif msg_id is not None:
        header['msg_id'] = msg_id
    body = dict(data)
    if data_type is not None:
        body['data_type'] = data_type
    return {'header': header, 'data': body}


def post_queue(client, messages, token=COLL_TRITON, name='Triton', queue_id=None):
    body = {
        'queue_id': queue_id or f'{name}-{uuid.uuid4().hex[:12]}',
        'name': name,
        'id': f'id-{name.lower()}',
        'messages': messages,
    }
    return client.post('/api/data/queue', json=body, headers=auth(token))


def metrics_payload(bytes_in=1000, bytes_out=500, cpu=12.5):
    return {
        'cpu_percent': cpu, 'cpu_percent_unit': '%',
        'ram_percent': 40.0,
        'uptime_seconds': 100, 'uptime_formatted': '1m',
        'network_bytes_in': bytes_in, 'network_bytes_out': bytes_out,
        'network_bandwidth_in_mbps': 1.0, 'network_bandwidth_out_mbps': 0.5,
    }
