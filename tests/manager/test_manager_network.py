"""Network running totals (contract 6): null, replay/out-of-order, reboot."""
import pytest

from manager_helpers import COLL_TRITON, READ, auth, make_msg, metrics_payload, post_queue


def _totals(client):
    return client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']


def _send(client, ts, bytes_in, bytes_out):
    payload = metrics_payload(bytes_in, bytes_out)
    ack = post_queue(client, [make_msg('system_metrics', payload, ts=ts)]).get_json()
    assert ack['counts']['stored'] == 1, ack


def test_running_totals_sequence(client):
    _send(client, '2026-08-04T07:00:00Z', 1000, 500)        # first contact seeds
    assert (_totals(client)['total_bytes_in'], _totals(client)['total_bytes_out']) == (1000, 500)

    _send(client, '2026-08-04T07:05:00Z', 1500, 700)        # normal delta
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1500, 700)

    _send(client, '2026-08-04T07:02:00Z', 1200, 600)        # out-of-order: skipped
    _send(client, '2026-08-04T07:05:00Z', 9999, 9999)       # same timestamp (replay): skipped
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1500, 700)
    assert (t['last_bytes_in'], t['last_bytes_out']) == (1500, 700)

    _send(client, '2026-08-04T07:10:00Z', None, 800)        # one counter null: untouched
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1500, 700)
    m = client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']
    assert m['network_bytes_in'] is None and m['network_bytes_out'] == 800

    _send(client, '2026-08-04T07:15:00Z', 100, 50)          # reboot: delta = current
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1600, 750)

    _send(client, '2026-08-04T07:20:00Z', 300, 80)
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1800, 780)


def test_missing_counters_never_count_as_zero(client):
    _send(client, '2026-08-04T07:00:00Z', 1000, 500)
    payload = metrics_payload()
    del payload['network_bytes_in'], payload['network_bytes_out']
    post_queue(client, [make_msg('system_metrics', payload, ts='2026-08-04T07:05:00Z')])
    _send(client, '2026-08-04T07:10:00Z', 1100, 600)
    t = _totals(client)
    # had the missing reading been treated as 0 the next delta would be "reboot" = 1100
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1100, 600)


def test_disk_update_does_not_block_metrics(client):
    post_queue(client, [make_msg('folder_usage', {'/volume1': 5, 'total_usage': 5}, ts='2026-08-04T08:00:00Z')])
    _send(client, '2026-08-04T07:55:00Z', 1000, 500)
    t = _totals(client)
    assert t['total_bytes_in'] == 1000 and t['total_disk_bytes'] == 5


def test_rejected_or_duplicate_messages_have_no_side_effects(client):
    m = make_msg('system_metrics', metrics_payload(1000, 500), ts='2026-08-04T07:00:00Z')
    post_queue(client, [m])
    dup = dict(m)
    post_queue(client, [dup])
    bad = make_msg('system_metrics', {**metrics_payload(5000, 5000), 'ram_percent': 'lots'},
                   ts='2026-08-04T07:30:00Z')
    assert post_queue(client, [bad]).get_json()['results'][0]['status'] == 'rejected'
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1000, 500)


def test_nonfinite_counters_are_unmeasured(client):
    # Flask's JSON parser accepts NaN / Infinity literals; they must count as
    # "not measured" (totals untouched) rather than reject the whole message.
    _send(client, '2026-08-04T07:00:00Z', 1000, 500)
    for i, (bin_, bout) in enumerate((('NaN', '1500'), ('Infinity', '-Infinity'), ('1e999', '2000'))):
        body = ('{"queue_id": "Triton-nan%d", "name": "Triton", "id": "id-triton", "messages": [{'
                '"header": {"msg_id": "nan%d", "device_name": "Triton", "timestamp": "2026-08-04T07:0%d:00Z"},'
                '"data": {"data_type": "system_metrics", "cpu_percent": NaN, "ram_percent": 41.0,'
                ' "network_bytes_in": %s, "network_bytes_out": %s}}]}') % (i, i, i + 5, bin_, bout)
        ack = client.post('/api/data/queue', data=body, content_type='application/json',
                          headers=auth(COLL_TRITON)).get_json()
        assert ack['results'][0]['status'] == 'stored', ack
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1000, 500)
    rows = client.get('/api/data/Triton/system_metrics?limit=10', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 4
    assert all(r['cpu_percent'] is None for r in rows if r['msg_id'].startswith('nan'))
    assert {r['ram_percent'] for r in rows if r['msg_id'].startswith('nan')} == {41.0}
    # a later real reading still accumulates from the last real counter
    _send(client, '2026-08-04T07:10:00Z', 1600, 700)
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (1600, 700)


def _send_up(client, ts, bytes_in, bytes_out, uptime='omit'):
    payload = metrics_payload(bytes_in, bytes_out)
    if uptime == 'omit':
        del payload['uptime_seconds']
    else:
        payload['uptime_seconds'] = uptime
    ack = post_queue(client, [make_msg('system_metrics', payload, ts=ts)]).get_json()
    assert ack['counts']['stored'] == 1, ack


def test_counter_decrease_without_reboot_is_not_counted(client, caplog):
    # Up for days, yet the counters dropped (e.g. a collector that stopped
    # counting loopback): nothing is added, and the counter is re-baselined.
    _send_up(client, '2026-08-04T07:00:00Z', 10_000, 5_000, uptime=900_000)
    _send_up(client, '2026-08-04T07:05:00Z', 12_000, 6_000, uptime=900_300)
    _send_up(client, '2026-08-04T07:10:00Z', 4_000, 6_500, uptime=900_600)
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (12_000, 6_500)
    assert (t['last_bytes_in'], t['last_bytes_out']) == (4_000, 6_500)
    assert any('not a reboot' in r.getMessage() for r in caplog.records)
    _send_up(client, '2026-08-04T07:15:00Z', 4_500, 6_600, uptime=900_900)   # counts from the new baseline
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (12_500, 6_600)


@pytest.mark.parametrize('uptime', [120, 299 + 300, None, 'omit'])
def test_counter_decrease_after_reboot_counts_traffic_since_boot(client, uptime):
    # uptime shorter than the 300 s since the previous reading (+ slack), or
    # unknown: a reboot, so the current counter is the traffic since boot
    _send_up(client, '2026-08-04T07:00:00Z', 10_000, 5_000, uptime=900_000)
    _send_up(client, '2026-08-04T07:05:00Z', 300, 200, uptime=uptime)
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (10_300, 5_200)


def test_reboot_check_uses_time_since_last_counter_reading(client):
    # A later folder_usage message moves last_updated but not the network
    # reading time; the check measures uptime against the network reading.
    _send_up(client, '2026-08-04T07:00:00Z', 10_000, 5_000, uptime=900_000)
    post_queue(client, [make_msg('folder_usage', {'/volume1': 5, 'total_usage': 5}, ts='2026-08-04T09:00:00Z')])
    _send_up(client, '2026-08-04T09:05:00Z', 300, 200, uptime=3_600)       # booted at 08:05: a reboot
    t = _totals(client)
    assert (t['total_bytes_in'], t['total_bytes_out']) == (10_300, 5_200)
