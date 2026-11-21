"""
GET /api/user_metrics/usage (spec §16): per-user CPU and GPU totals over a
window, from the hourly rollups once they are complete, else from the raw
per-system user_metrics rows; cached for usage_cache_seconds.

The Manager's clock is frozen at hh:30 (a day ago), so the window starts half
way through an hour and the rollup path's partial first hour is known.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

import data_store
import manager as mgr
from manager_helpers import ADMIN, COLL_TRITON, READ, auth, make_msg, post_queue
from storage_util import format_timestamp

_real = datetime.now(timezone.utc) - timedelta(days=1)
NOW = _real.replace(minute=30, second=0, microsecond=0)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(mgr, 'datetime', _FrozenDatetime)


def _ago(hours):
    return format_timestamp(NOW - timedelta(hours=hours))


def _user(username, ts, **fields):
    data = {'username': username, 'cpu_percent': fields.pop('cpu_percent', 1.0),
            'ram_bytes': fields.pop('ram_bytes', 1024), 'ram_formatted': '1 KB'}
    data.update(fields)
    return make_msg('user_metrics', data, ts=ts)


def _gpu(seconds, busy, vram):
    return {'gpu_busy_seconds': busy, 'gpu_held_seconds': seconds, 'gpus_held': 1, 'gpu_vram_bytes': vram,
            'gpu_util_percent': 50.0, 'gpu_processes': 1}


# alice: CPU and GPU; bob: GPU only; carol: no usage fields at all (an old collector)
ROWS = [
    _user('alice', _ago(2), cpu_seconds=600.0, cpu_cores=2.0, interval_s=300.0, **_gpu(300.0, 150.0, 4 * 2 ** 30)),
    _user('alice', _ago(10), cpu_seconds=1200.0, cpu_cores=4.0, interval_s=300.0, **_gpu(300.0, 30.0, 8 * 2 ** 30)),
    _user('alice', _ago(23.9), cpu_seconds=300.0, cpu_cores=1.0, interval_s=300.0),
    _user('alice', _ago(26), cpu_seconds=9999.0, cpu_cores=32.0, interval_s=300.0),        # 24 h: outside
    _user('alice', _ago(24 * 6), cpu_seconds=3600.0, cpu_cores=12.0, interval_s=300.0),    # 7 d only
    _user('bob', _ago(3), cpu_percent=None, ram_bytes=None, **_gpu(300.0, 270.0, 2 ** 30)),
    _user('bob', _ago(5), cpu_percent=None, ram_bytes=None, **_gpu(300.0, None, 2 ** 30)),
    _user('carol', _ago(4)),
    _user('carol', _ago(6)),
]


def _post(client, msgs):
    r = post_queue(client, msgs, token=ADMIN)
    assert r.status_code == 200
    assert [x['status'] for x in r.get_json()['results']] == ['stored'] * len(msgs)


def _usage(client, query='', status=200):
    r = client.get('/api/user_metrics/usage' + query, headers=auth(READ))
    assert r.status_code == status, r.get_data(as_text=True)
    return r.get_json()


def _users(body, device='Triton'):
    return {u['username']: u for u in body['devices'][device]['users']}


def test_totals_window_and_nulls(client):
    _post(client, ROWS)
    body = _usage(client)
    assert body['window'] == '24h' and body['to'] == format_timestamp(NOW) and body['from'] == _ago(24)
    dev = body['devices']['Triton']
    assert dev['source'] == 'rollups' and dev['complete'] is True
    # sorted by gpu_busy_hours (bob 270 s, alice 180 s), then cpu_core_hours; carol (no GPU data) last
    assert [u['username'] for u in dev['users']] == ['bob', 'alice', 'carol']
    u = _users(body)
    alice, bob, carol = u['alice'], u['bob'], u['carol']
    assert alice['samples'] == 3
    assert alice['cpu_core_hours'] == pytest.approx(2100 / 3600)
    assert alice['avg_cores'] == pytest.approx(2100 / 86400)
    assert alice['peak_cores'] == 4.0
    assert alice['gpu_busy_hours'] == pytest.approx(180 / 3600)
    assert alice['gpu_held_hours'] == pytest.approx(600 / 3600)
    assert alice['peak_gpu_vram_bytes'] == 8 * 2 ** 30 and isinstance(alice['peak_gpu_vram_bytes'], int)
    # a GPU-only user: no CPU figures (null, not 0); one busy reading unmeasured
    assert bob['cpu_core_hours'] is None and bob['avg_cores'] is None and bob['peak_cores'] is None
    assert bob['gpu_busy_hours'] == pytest.approx(270 / 3600) and bob['gpu_held_hours'] == pytest.approx(600 / 3600)
    # an aggregate over no measured value is null
    assert {k: v for k, v in carol.items() if k not in ('username', 'samples')} == dict.fromkeys(
        ('cpu_core_hours', 'avg_cores', 'peak_cores', 'gpu_busy_hours', 'gpu_held_hours', 'peak_gpu_vram_bytes'))
    assert carol['samples'] == 2

    week = _users(_usage(client, '?window=7d'))['alice']
    assert week['samples'] == 5 and week['cpu_core_hours'] == pytest.approx((2100 + 9999 + 3600) / 3600)
    assert week['avg_cores'] == pytest.approx((2100 + 9999 + 3600) / (7 * 86400)) and week['peak_cores'] == 32.0
    assert _users(_usage(client, '?window=30d'))['alice']['samples'] == 5


def test_top_device_and_bad_arguments(client):
    _post(client, ROWS)
    assert [u['username'] for u in _usage(client, '?top=1')['devices']['Triton']['users']] == ['bob']
    assert _usage(client, '?top=1000')['devices']['Triton']['users'][2]['username'] == 'carol'   # capped at 200
    only = _usage(client, '?device=Triton&window=7d')
    assert list(only['devices']) == ['Triton'] and only['window'] == '7d'
    _usage(client, '?device=Nobody', status=404)
    _usage(client, '?device=bad%20name!', status=400)
    for bad in ('?window=1y', '?window=24', '?top=0', '?top=abc'):
        _usage(client, bad, status=400)
    assert client.get('/api/user_metrics/usage', headers=auth(COLL_TRITON)).status_code == 403
    assert client.get('/api/user_metrics/usage').status_code == 401
    # the static route wins over /api/user_metrics/<device_name>; that one is unchanged
    assert isinstance(client.get('/api/user_metrics/Triton', headers=auth(READ)).get_json(), list)


def test_a_system_without_user_metrics(client):
    _post(client, [make_msg('system_metrics', {'cpu_percent': 1.0}, ts=_ago(1))])
    assert _usage(client)['devices'] == {'Triton': {'source': 'raw', 'complete': True, 'users': []}}


def _store_without_rollups(data_dir, msgs):
    store = data_store.TypedDataStore(data_dir)
    for m in msgs:
        h, d = m['header'], m['data']
        store.store_message(h['device_name'], h['msg_id'], h['timestamp'], 'NAS', d['data_type'],
                            {k: v for k, v in d.items() if k != 'data_type'})
    store.close()


def test_raw_until_the_backfill_is_done_then_rollups(make_client, data_dir):
    edge = _user('alice', _ago(24 + 10 / 60), cpu_seconds=60.0, cpu_cores=8.0)   # 10 min before the window
    _store_without_rollups(data_dir, ROWS + [edge])
    client = make_client(usage_cache_seconds=0)
    _post(client, [make_msg('system_metrics', {'cpu_percent': 1.0}, ts=_ago(1))])     # registers "Triton"
    raw = _usage(client)
    assert raw['devices']['Triton']['source'] == 'raw' and raw['devices']['Triton']['complete'] is False
    for system in mgr._data_store.list_systems():
        assert mgr._data_store.run_rollup_backfill(system)
    rolled = _usage(client)
    assert rolled['devices']['Triton']['source'] == 'rollups' and rolled['devices']['Triton']['complete'] is True
    r, h = _users(raw), _users(rolled)
    assert set(r) == set(h) == {'alice', 'bob', 'carol'}
    # the same totals, except that the rollups include the hour the window starts in
    assert {k: v for k, v in r['bob'].items()} == {k: v for k, v in h['bob'].items()}
    assert r['carol'] == h['carol']
    assert h['alice']['samples'] == r['alice']['samples'] + 1
    assert h['alice']['cpu_core_hours'] == pytest.approx(r['alice']['cpu_core_hours'] + 60 / 3600)
    assert (r['alice']['peak_cores'], h['alice']['peak_cores']) == (4.0, 8.0)
    for k in ('gpu_busy_hours', 'gpu_held_hours', 'peak_gpu_vram_bytes'):
        assert r['alice'][k] == pytest.approx(h['alice'][k]), k


def test_rollups_disabled_reads_raw(make_client):
    client = make_client(rollups={'enabled': False})
    _post(client, ROWS)
    dev = _usage(client)['devices']['Triton']
    assert dev['source'] == 'raw' and dev['complete'] is True
    assert _users(_usage(client))['alice']['cpu_core_hours'] == pytest.approx(2100 / 3600)


def test_answers_are_cached_for_usage_cache_seconds(make_client):
    client = make_client()
    assert mgr._usage_cache_seconds == 300
    _post(client, ROWS)
    first = _usage(client)
    late = _user('alice', _ago(1), cpu_seconds=3600.0, cpu_cores=1.0)
    _post(client, [late])
    assert _usage(client) == first                                         # reused
    assert _users(_usage(client, '?top=2'))['alice']['samples'] == 4       # another key: computed now
    for key, (t, answer) in list(mgr._usage_cache.items()):
        mgr._usage_cache[key] = (t - 301, answer)                          # 301 s later
    again = _usage(client)
    assert _users(again)['alice']['samples'] == 4 and again['devices'] != first['devices']

    client = make_client(usage_cache_seconds=0)                            # 0: never reused
    a = _usage(client)
    _post(client, [_user('alice', _ago(0.5), cpu_seconds=1.0, cpu_cores=1.0)])
    assert _users(_usage(client))['alice']['samples'] == _users(a)['alice']['samples'] + 1
    assert mgr._usage_cache == {}


def test_two_systems_under_their_registered_names(make_client, data_dir):
    client = make_client()
    _post(client, ROWS)
    atlas = [_user('dave', _ago(1), cpu_seconds=36.0, cpu_cores=0.5)]
    for m in atlas:
        m['header'].update(device_name='Atlas', device_id='id-atlas')
    assert post_queue(client, atlas, token=ADMIN, name='Atlas').status_code == 200
    devices = _usage(client)['devices']
    assert set(devices) == {'Triton', 'Atlas'}
    assert [u['username'] for u in devices['Atlas']['users']] == ['dave']
    assert os.path.isdir(os.path.join(data_dir, 'atlas'))
