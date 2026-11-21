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


# --------------------------------------------------------------------------- #
# A message from a collector whose clock is wrong (ahead)                     #
# --------------------------------------------------------------------------- #

from datetime import datetime, timedelta, timezone  # noqa: E402

from old_manager import old_ingest, old_msg  # noqa: E402


def _ts(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _hours_ago(h):
    return _ts(datetime.now(timezone.utc) - timedelta(hours=h))


def test_future_dated_message_does_not_freeze_the_device(client, caplog):
    future = _ts(datetime.now(timezone.utc) + timedelta(days=365))   # bad RTC / before NTP sync
    msgs = [make_msg('system_metrics', metrics_payload(1000, 100, cpu=1.0), ts=_hours_ago(4)),
            make_msg('system_metrics', metrics_payload(5000, 500, cpu=2.0), ts=future),
            make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 99.0, 'ram_bytes': 1}, ts=future),
            make_msg('folder_usage', {'_unit': 'bytes', '/volume1': 999, 'total_usage': 999}, ts=future)]
    caplog.set_level('WARNING')
    ack = post_queue(client, msgs).get_json()
    assert ack['counts']['stored'] == 4                       # all stored in the history ...
    assert any('ahead of this server' in r.getMessage() for r in caplog.records)
    for i, h in enumerate((3, 2, 1)):                         # ... then the clock is right again
        ts = _hours_ago(h)
        post_queue(client, [
            make_msg('system_metrics', metrics_payload(2000 + 1000 * i, 200, cpu=10.0 * (i + 1)), ts=ts),
            make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 3.0 + i, 'ram_bytes': 1}, ts=ts),
            make_msg('folder_usage', {'_unit': 'bytes', '/volume1': 100 + i, 'total_usage': 100 + i}, ts=ts)])

    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['cpu_percent'] == 30.0 and m['timestamp'] == ts
    dev = client.get('/api/devices', headers=auth(READ)).get_json()['devices'][0]
    assert dev['last_seen'] == ts
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert t['total_bytes_in'] == 4000                        # 1000 seeded + 1000 + 1000 + 1000
    assert t['network_last_updated'] == ts
    assert client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json()[0]['cpu_percent'] == 5.0
    assert client.get('/api/usage/all', headers=auth(READ)).get_json()['Triton']['total_usage'] == 102
    # the future-dated readings are in the history
    rows = client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']
    assert rows[0]['timestamp'] == future and len(rows) == 5
    assert len(client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()) == 4


def test_first_contact_from_the_future_registers_the_device(client):
    future = _ts(datetime.now(timezone.utc) + timedelta(days=30))
    assert post_queue(client, [make_msg('system_metrics', metrics_payload(), ts=future)]
                      ).get_json()['counts']['stored'] == 1
    dev = client.get('/api/devices', headers=auth(READ)).get_json()['devices'][0]
    assert dev['name'] == 'Triton' and dev['last_seen'] < future
    post_queue(client, [make_msg('system_metrics', metrics_payload(1500, 600, cpu=7.0), ts=_hours_ago(0.1))])
    assert client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']['cpu_percent'] == 7.0


def test_future_dated_state_stored_by_the_old_manager_does_not_block(make_client, data_dir):
    """The old Manager stored whatever arrived last: a future-dated last reading must not freeze
    the device card, last_seen or the lifetime totals after the upgrade."""
    future = _ts(datetime.now(timezone.utc) + timedelta(days=200)).replace('.000000Z', 'Z')
    old_ingest(data_dir, [{'name': 'Triton', 'id': 'id-triton', 'messages': [
        old_msg('system_metrics', metrics_payload(1000, 100, cpu=1.0), _hours_ago(5)),
        old_msg('system_metrics', metrics_payload(2000, 200, cpu=2.0), future),
        old_msg('user_metrics', {'username': 'alice', 'cpu_percent': 50.0, 'ram_bytes': 1}, future),
    ]}])
    client = make_client()
    before = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert before['total_bytes_in'] == 2000

    now = _hours_ago(0.1)
    post_queue(client, [make_msg('system_metrics', metrics_payload(3500, 300, cpu=9.0), ts=now),
                        make_msg('user_metrics', {'username': 'alice', 'cpu_percent': 4.0, 'ram_bytes': 1}, ts=now)])
    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['cpu_percent'] == 9.0 and m['timestamp'] == now
    assert client.get('/api/devices', headers=auth(READ)).get_json()['devices'][0]['last_seen'] == now
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert t['total_bytes_in'] == 3500                        # the 1500 bytes since are counted
    assert client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json()[0]['cpu_percent'] == 4.0
