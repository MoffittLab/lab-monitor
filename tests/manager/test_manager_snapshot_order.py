"""Snapshots and the device registry follow the newest message, whatever the stored order."""
from manager_helpers import READ, auth, make_msg, metrics_payload, post_queue

NEW = '2026-11-21T10:00:00Z'
OLD = '2026-11-18T10:00:00Z'


def _ingest(client, ts, cpu, total, user_cpu):
    msgs = [
        make_msg('system_metrics', metrics_payload(cpu=cpu), ts=ts),
        make_msg('folder_usage', {'_unit': 'bytes', '/volume1': total, 'total_usage': total}, ts=ts),
        make_msg('user_metrics', {'username': 'alice', 'cpu_percent': user_cpu, 'ram_bytes': 1}, ts=ts),
    ]
    ack = post_queue(client, msgs).get_json()
    assert ack['counts']['stored'] == 3, ack


def test_older_messages_do_not_roll_back_snapshots(client):
    _ingest(client, NEW, cpu=50.0, total=500, user_cpu=9.0)
    _ingest(client, OLD, cpu=5.0, total=100, user_cpu=1.0)      # gap-fill / replay

    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['cpu_percent'] == 50.0 and m['timestamp'].startswith('2026-11-21')

    dev = client.get('/api/devices', headers=auth(READ)).get_json()['devices'][0]
    assert dev['last_seen'].startswith('2026-11-21')
    assert dev['first_seen'].startswith('2026-11-18')          # moved back to the oldest message

    usage = client.get('/api/usage/all', headers=auth(READ)).get_json()['Triton']
    assert usage['timestamp'].startswith('2026-11-21') and usage['total_usage'] == 500
    assert client.get('/api/totals', headers=auth(READ)).get_json()['global']['total_disk_bytes'] == 500

    users = client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json()
    assert users[0]['cpu_percent'] == 9.0 and users[0]['timestamp'].startswith('2026-11-21')

    # ... but the older messages are in the history
    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert [r['cpu_percent'] for r in rows] == [50.0, 5.0]
    hist = client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()
    assert len(hist) == 2


def test_newer_messages_still_advance_snapshots(client):
    _ingest(client, OLD, cpu=5.0, total=100, user_cpu=1.0)
    _ingest(client, NEW, cpu=50.0, total=500, user_cpu=9.0)
    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['cpu_percent'] == 50.0
    dev = client.get('/api/devices', headers=auth(READ)).get_json()['devices'][0]
    assert dev['first_seen'].startswith('2026-11-18') and dev['last_seen'].startswith('2026-11-21')
    assert client.get('/api/usage/all', headers=auth(READ)).get_json()['Triton']['total_usage'] == 500
    assert client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json()[0]['cpu_percent'] == 9.0
