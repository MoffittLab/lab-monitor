"""
Dashboard server tests: Manager outage / partial failure handling, /api/data
cache, optional sign-in, folder_usage parsing, history proxy validation and
max_points pass-through; the usage-metrics sections (sessions, storage health,
storage owners, usage totals) against a NEW Manager and against CURRENT
(e5cd699) and OLD (f086e7e) Managers that don't have them.  The Manager is
mocked by patching requests.Session.get, so no network is used.
"""
import base64
import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
import requests
from werkzeug.security import generate_password_hash

REPO = Path(__file__).resolve().parents[2]


def _load_dashboard_module():
    name = 'lab_monitor_dashboard_app'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / 'dashboard' / 'app.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod     # Flask resolves templates/static via sys.modules
    spec.loader.exec_module(mod)
    return mod


dash = _load_dashboard_module()

TOKEN = 'read-token-0123456789abcdef'


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError('not json')
        return self._payload


def healthy_manager():
    """Path -> response for a Manager with one server and one NAS."""
    return {
        '/api/devices': FakeResponse(200, {'devices': [
            {'name': 'Atlas', 'system_id': 'win-atlas', 'device_type': 'server',
             'first_seen': '2026-01-01T00:00:00Z', 'last_seen': '2026-11-21T00:00:00Z'},
            {'name': 'Triton', 'system_id': 'syn-triton', 'device_type': 'nas',
             'first_seen': '2026-01-01T00:00:00Z', 'last_seen': '2026-11-21T00:00:00Z'},
        ]}),
        '/api/metrics/all': FakeResponse(200, {
            'Atlas': {'name': 'Atlas', 'device_type': 'server', 'timestamp': '2026-11-21T00:00:00Z',
                      'cpu_percent': 12.5, 'ram_percent': None, 'uptime_seconds': 100,
                      'uptime_formatted': '1m', 'network_bytes_in': None, 'network_bytes_out': None,
                      'network_bandwidth_in_mbps': 1.5, 'network_bandwidth_out_mbps': None,
                      'units': {'cpu_percent_unit': '%'}, 'gpus': [], 'total_ram_bytes': 1024},
        }),
        '/api/usage/all': FakeResponse(200, {
            'Triton': {'name': 'Triton', 'system_id': 'syn-triton', 'device_type': 'nas',
                       'timestamp': '2026-11-20T02:00:00Z', 'total_disk_bytes': 300,
                       '_unit': 'bytes', '/volume1': 300, '/volume1_total_bytes': 1000,
                       '/volume1_free_bytes': 700, '/volume1/Data': 200, 'total_usage': 300},
        }),
        '/api/totals': FakeResponse(200, {
            'global': {'total_bytes_in': 10, 'total_bytes_out': 20, 'total_disk_bytes': 300},
            'devices': {'Atlas': {'total_bytes_in': 10, 'total_bytes_out': 20, 'total_disk_bytes': 0}},
        }),
        '/api/user_metrics/all': FakeResponse(200, {
            'Atlas': [{'username': 'DOMAIN\\alice', 'cpu_percent': 5.0, 'ram_bytes': 10,
                       'ram_formatted': '10 B', 'timestamp': '2026-11-21T00:00:00Z'}],
        }),
    }


NOT_FOUND = FakeResponse(404, {'status': 'error', 'error': 'Not found'})


class FakeManager:
    """Callable used in place of requests.Session.get."""

    def __init__(self, routes=None):
        self.routes = routes if routes is not None else healthy_manager()
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, session, url, headers=None, params=None, timeout=None, **kw):
        path = urlsplit(url).path
        with self.lock:
            self.calls.append({'url': url, 'path': path, 'params': params, 'headers': headers})
        route = self.routes.get(path)
        if route is None:
            # history endpoint: match on prefix
            for key, val in self.routes.items():
                if key.endswith('*') and path.startswith(key[:-1]):
                    route = val
                    break
        if route is None:
            return FakeResponse(404, {'status': 'error', 'error': 'Not found'})
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(path, params)
        return route

    def count(self, path=None):
        return len([c for c in self.calls if path is None or c['path'] == path])


@pytest.fixture
def make_client(monkeypatch):
    """make_client(config_overrides, routes) -> (flask test client, FakeManager)."""
    def _make(overrides=None, routes=None):
        config = {
            'manager_url': 'http://manager.test:5000',
            'manager_token': TOKEN,
            'refresh_interval_seconds': 30,
            'host': '127.0.0.1',
            'client_names': False,      # no DNS lookups from the tests
        }
        config.update(overrides or {})
        dash.init_app(config, logging.getLogger('test-dashboard'))
        fake = FakeManager(routes)
        # a plain function so it binds as a method (receives the session)
        monkeypatch.setattr(requests.Session, 'get',
                            lambda session, url, **kw: fake(session, url, **kw))
        dash.app.config['TESTING'] = True
        return dash.app.test_client(), fake
    return _make


# ---------------------------------------------------------------------------
# /api/data: outage, partial failure, healthy
# ---------------------------------------------------------------------------

def test_manager_outage_returns_502(make_client):
    boom = requests.exceptions.ConnectionError('secret-host.internal refused connection')
    routes = {p: boom for p in dash.POLL_ENDPOINTS}
    client, fake = make_client(routes=routes)

    resp = client.get('/api/data')
    assert resp.status_code == 502
    body = resp.get_json()
    assert body['success'] is False
    assert body['error'] == 'Manager unavailable'
    assert {'endpoint': '/api/devices', 'error': 'connection failed'} in body['errors']
    # Exception text (hostnames etc.) is logged, never returned
    assert b'secret-host' not in resp.data


def test_core_call_unauthorized_returns_502(make_client):
    routes = healthy_manager()
    routes['/api/devices'] = FakeResponse(401, {'status': 'error', 'error': 'Unauthorized'})
    client, _ = make_client(routes=routes)

    resp = client.get('/api/data')
    assert resp.status_code == 502
    body = resp.get_json()
    assert body['success'] is False
    assert body['errors'][0]['endpoint'] == '/api/devices'
    assert body['errors'][0]['error'].startswith('HTTP 401')


def test_partial_failure_is_degraded(make_client):
    routes = healthy_manager()
    routes['/api/user_metrics/all'] = requests.exceptions.Timeout('slow')
    routes['/api/totals'] = FakeResponse(200, None, bad_json=True)
    client, _ = make_client(routes=routes)

    resp = client.get('/api/data')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['degraded'] is True
    assert {'endpoint': '/api/user_metrics/all', 'error': 'timeout'} in body['errors']
    assert {'endpoint': '/api/totals', 'error': 'invalid JSON'} in body['errors']
    # Data from the calls that worked is still there
    assert set(body['systems']) == {'Atlas', 'Triton'}
    assert body['systems']['Atlas']['metrics']['cpu_percent'] == 12.5
    assert body['global_totals'] == {}


def test_unexpected_shape_counts_as_failure(make_client):
    routes = healthy_manager()
    routes['/api/metrics/all'] = FakeResponse(200, ['not', 'a', 'dict'])
    client, _ = make_client(routes=routes)

    body = client.get('/api/data').get_json()
    assert body['degraded'] is True
    assert {'endpoint': '/api/metrics/all', 'error': 'unexpected response'} in body['errors']


def test_healthy_poll_keeps_nulls_and_parses_disk(make_client):
    client, fake = make_client()
    resp = client.get('/api/data')
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == 'no-store'
    body = resp.get_json()
    assert body['success'] is True and body['degraded'] is False and body['errors'] == []

    atlas = body['systems']['Atlas']
    # Unmeasured values stay null (contract 4) - never defaulted to 0
    assert atlas['metrics']['ram_percent'] is None
    assert atlas['metrics']['network_bytes_in'] is None
    assert atlas['metrics']['network_bandwidth_out_mbps'] is None
    assert atlas['users'][0]['username'] == 'DOMAIN\\alice'
    assert atlas['totals']['total_bytes_in'] == 10

    disk = body['systems']['Triton']['disk']
    assert disk['volumes'][0]['path'] == '/volume1'
    assert disk['volumes'][0]['total_bytes'] == 1000
    assert [f['path'] for f in disk['folders']] == ['/volume1/Data']
    assert disk['timestamp'] == '2026-11-20T02:00:00Z'

    # Every Manager call carries the read token
    assert all(c['headers'] == {'Authorization': f'Bearer {TOKEN}'} for c in fake.calls)


def test_nan_from_manager_becomes_null(make_client):
    routes = healthy_manager()
    routes['/api/metrics/all'] = FakeResponse(200, {
        'Atlas': {'timestamp': '2026-11-21T00:00:00Z', 'cpu_percent': float('nan'),
                  'ram_percent': float('inf')}})
    client, _ = make_client(routes=routes)
    resp = client.get('/api/data')
    assert b'NaN' not in resp.data and b'Infinity' not in resp.data
    m = json.loads(resp.data)['systems']['Atlas']['metrics']
    assert m['cpu_percent'] is None and m['ram_percent'] is None


def test_manager_calls_run_concurrently(make_client):
    # Every call waits for all five to be in flight at once; a sequential
    # implementation would time out at the barrier.
    barrier = threading.Barrier(len(dash.POLL_ENDPOINTS), timeout=5)
    base = healthy_manager()

    def waiting(path, params):
        barrier.wait()
        return base.get(path, NOT_FOUND)     # a CURRENT Manager: 404 for the optional endpoints

    client, fake = make_client(routes={p: waiting for p in dash.POLL_ENDPOINTS})
    body = client.get('/api/data').get_json()
    assert body['success'] is True and body['errors'] == []
    assert fake.count() == len(dash.POLL_ENDPOINTS)


# ---------------------------------------------------------------------------
# /api/data cache
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_cache_ttl_is_refresh_interval(make_client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dash, '_monotonic', clock)
    client, fake = make_client({'refresh_interval_seconds': 30})

    client.get('/api/data')
    assert fake.count('/api/devices') == 1
    clock.now += 29
    client.get('/api/data')
    client.get('/api/data')
    assert fake.count('/api/devices') == 1       # served from cache
    clock.now += 2                               # past the 30 s TTL
    client.get('/api/data')
    assert fake.count('/api/devices') == 2


def test_cache_ttl_override_and_failures_cached_briefly(make_client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dash, '_monotonic', clock)
    routes = {p: requests.exceptions.ConnectionError('down') for p in dash.POLL_ENDPOINTS}
    client, fake = make_client({'refresh_interval_seconds': 60}, routes=routes)

    assert client.get('/api/data').status_code == 502
    clock.now += 4
    assert client.get('/api/data').status_code == 502
    assert fake.count('/api/devices') == 1
    clock.now += 2                               # failures expire after 5 s
    fake.routes = healthy_manager()
    assert client.get('/api/data').status_code == 200
    assert fake.count('/api/devices') == 2


def test_degraded_result_cached_briefly(make_client, monkeypatch):
    # A degraded poll is cached like a failure (5 s), so recovery of the
    # failed Manager call shows up without waiting a full refresh interval.
    clock = FakeClock()
    monkeypatch.setattr(dash, '_monotonic', clock)
    routes = healthy_manager()
    routes['/api/metrics/all'] = requests.exceptions.Timeout('slow')
    client, fake = make_client({'refresh_interval_seconds': 60}, routes=routes)

    assert client.get('/api/data').get_json()['degraded'] is True
    clock.now += 4
    client.get('/api/data')
    assert fake.count('/api/devices') == 1
    clock.now += 2
    fake.routes = healthy_manager()
    assert client.get('/api/data').get_json()['degraded'] is False
    assert fake.count('/api/devices') == 2


def test_cache_shared_across_threads(make_client):
    client, fake = make_client()
    results = []

    def hit():
        results.append(dash.app.test_client().get('/api/data').status_code)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 8
    assert fake.count('/api/devices') == 1


# ---------------------------------------------------------------------------
# Sign-in: one password, then a cookie that keeps the browser signed in
# ---------------------------------------------------------------------------

PASSWORD = 'correct horse'
PASSWORD_HASH = generate_password_hash(PASSWORD)
DAY = 86400


@pytest.fixture
def auth_cfg(tmp_path):
    """An "auth" block whose key file lives in the test's own folder."""
    return {'password_hash': PASSWORD_HASH, 'session_key_file': str(tmp_path / 'session_key')}


@pytest.fixture
def clock(monkeypatch):
    """Controls the wall clock (cookie ages) and the monotonic clock (lockouts)."""
    class Clock:
        now = 1_800_000_000.0
        mono = 5000.0

        def advance(self, seconds):
            self.now += seconds
            self.mono += seconds
    c = Clock()
    monkeypatch.setattr(dash, '_now', lambda: c.now)
    monkeypatch.setattr(dash, '_monotonic', lambda: c.mono)
    return c


def _sign_in(client, password=PASSWORD, **kw):
    return client.post('/login', data={'password': password, **kw.pop('data', {})}, **kw)


def _session_cookies(resp):
    return [h for h in resp.headers.getlist('Set-Cookie') if h.startswith(dash.SESSION_COOKIE + '=')]


def test_no_auth_configured_is_open(make_client):
    client, _ = make_client()
    assert client.get('/').status_code == 200
    assert client.get('/api/data').status_code == 200
    assert b'Sign out' not in client.get('/').data
    assert client.get('/login').headers['Location'] == '/'      # nothing to sign in to


def test_not_signed_in_sees_only_the_sign_in_page(make_client, auth_cfg):
    client, fake = make_client({'auth': auth_cfg})

    resp = client.get('/')
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'
    resp = client.get('/anything/else?x=1&y=2')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/login?next=%2Fanything%2Felse%3Fx%3D1%26y%3D2'

    # The page's script gets JSON it can act on, not a redirect to HTML
    resp = client.get('/api/data')
    assert resp.status_code == 401
    assert resp.get_json() == {'success': False, 'error': 'Sign-in required', 'login': '/login'}
    assert client.get('/api/history/Atlas/system_metrics/cpu_percent').status_code == 401

    # No other credential is accepted
    basic = 'Basic ' + base64.b64encode(f'labadmin:{PASSWORD}'.encode()).decode()
    assert client.get('/', headers={'Authorization': basic}).status_code == 302
    assert client.get('/api/data', headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 401
    assert fake.count() == 0      # nothing reached the Manager

    # The sign-in page and what it needs are served; they hold no system data
    page = client.get('/login')
    assert page.status_code == 200 and page.headers['Cache-Control'] == 'no-store'
    assert b'type="password"' in page.data and b'180 days' in page.data
    assert client.get('/static/css/style.css').status_code == 200


def test_sign_in_sets_a_long_lived_cookie(make_client, auth_cfg, clock):
    client, fake = make_client({'auth': auth_cfg})

    resp = _sign_in(client, 'wrong password')
    assert resp.status_code == 401 and b'Wrong password.' in resp.data
    assert _session_cookies(resp) == [] and client.get('/').status_code == 302

    resp = _sign_in(client, data={'next': '/?range=7d'})
    assert resp.status_code == 302 and resp.headers['Location'] == '/?range=7d'
    (cookie,) = _session_cookies(resp)
    attrs = [a.strip() for a in cookie.split(';')[1:]]
    assert f'Max-Age={180 * DAY}' in attrs and 'HttpOnly' in attrs and 'SameSite=Lax' in attrs
    assert 'Path=/' in attrs and 'Secure' not in attrs          # plain http
    assert PASSWORD not in cookie and PASSWORD_HASH not in cookie

    page = client.get('/')
    assert page.status_code == 200 and b'Sign out' in page.data
    assert client.get('/api/data').status_code == 200 and fake.count('/api/devices') == 1
    assert client.get('/login').headers['Location'] == '/'      # already signed in


def test_cookie_is_marked_secure_behind_https(make_client, auth_cfg):
    client, _ = make_client({'auth': dict(auth_cfg, cookie_secure=True)})
    assert 'Secure' in [a.strip() for a in _session_cookies(_sign_in(client))[0].split(';')]
    client, _ = make_client({'auth': auth_cfg})
    resp = _sign_in(client, base_url='https://dashboard.test')
    assert 'Secure' in [a.strip() for a in _session_cookies(resp)[0].split(';')]


def test_browser_is_remembered_for_180_days_from_its_last_visit(make_client, auth_cfg, clock):
    client, _ = make_client({'auth': auth_cfg})
    _sign_in(client)

    # Used again the same day: still signed in, no new cookie
    clock.advance(3600)
    resp = client.get('/')
    assert resp.status_code == 200 and _session_cookies(resp) == []

    # Back after 100 days: still signed in, and the 180 days start again
    clock.advance(100 * DAY)
    resp = client.get('/api/data')
    assert resp.status_code == 200 and len(_session_cookies(resp)) == 1
    clock.advance(170 * DAY)
    assert client.get('/').status_code == 200

    # Not used for more than 180 days: asked again
    clock.advance(181 * DAY)
    assert client.get('/').status_code == 302
    assert client.get('/api/data').status_code == 401


def test_remember_days_is_configurable(make_client, auth_cfg, clock):
    client, _ = make_client({'auth': dict(auth_cfg, remember_days=7)})
    resp = _sign_in(client)
    assert f'Max-Age={7 * DAY}' in _session_cookies(resp)[0]
    assert b'7 days' in dash.app.test_client().get('/login').data
    clock.advance(8 * DAY)
    assert client.get('/').status_code == 302


def test_restart_keeps_browsers_signed_in_but_a_new_password_does_not(make_client, auth_cfg, tmp_path):
    client, _ = make_client({'auth': auth_cfg})
    _sign_in(client)
    token = client.get_cookie(dash.SESSION_COOKIE).value
    key_file = tmp_path / 'session_key'
    assert len(key_file.read_bytes().strip()) >= 32

    def status_after_restart(auth):
        restarted, _ = make_client({'auth': auth})
        restarted.set_cookie(dash.SESSION_COOKIE, token)
        return restarted.get('/').status_code

    assert status_after_restart(auth_cfg) == 200                                    # same key file, same password
    assert status_after_restart(dict(auth_cfg, password_hash=generate_password_hash('new password'))) == 302
    key_file.unlink()                                                              # "sign everyone out"
    assert status_after_restart(auth_cfg) == 302


def test_forged_and_damaged_cookies_are_refused(make_client, auth_cfg, clock):
    client, _ = make_client({'auth': auth_cfg})
    _sign_in(client)
    token = client.get_cookie(dash.SESSION_COOKIE).value
    version, issued, nonce, mac = token.split('.')

    def status_with(value):
        c = dash.app.test_client()
        c.set_cookie(dash.SESSION_COOKIE, value)
        return c.get('/').status_code

    assert status_with(token) == 200
    later = str(int(issued) + 1000 * DAY)
    for bad in ('', 'x', token[:-2], f'{version}.{later}.{nonce}.{mac}',       # issue time changed
                f'v2.{issued}.{nonce}.{mac}', f'{version}.{issued}.other.{mac}',
                f'{version}.{issued}.{nonce}.', token + '.extra', 'é' * 20, 'a' * 300):
        assert status_with(bad) == 302, bad
    # A correctly signed cookie from the future (clock set back) is refused too
    clock.advance(-3600)
    assert status_with(token) == 302


def test_wrong_passwords_lock_that_address_out_for_a_while(make_client, auth_cfg, clock):
    client, _ = make_client({'auth': auth_cfg})
    for _ in range(dash.LOGIN_MAX_FAILURES - 1):
        assert _sign_in(client, 'guess').status_code == 401
    resp = _sign_in(client, 'guess')
    assert resp.status_code == 429 and b'Too many wrong passwords' in resp.data

    # Locked: even the right password is refused, without being checked
    checked = []
    real = dash._check_password
    dash._check_password = lambda p: checked.append(p) or real(p)
    try:
        assert _sign_in(client).status_code == 429 and checked == []
        # Another address is not affected
        other = _sign_in(client, environ_base={'REMOTE_ADDR': '10.9.8.7'})
        assert other.status_code == 302 and checked == [PASSWORD]
    finally:
        dash._check_password = real

    clock.advance(dash.LOGIN_LOCK_SECONDS + 1)
    assert _sign_in(client).status_code == 302


def test_a_correct_password_clears_the_failure_count(make_client, auth_cfg, clock):
    client, _ = make_client({'auth': auth_cfg})
    for _ in range(dash.LOGIN_MAX_FAILURES - 1):
        _sign_in(client, 'guess')
    assert _sign_in(client).status_code == 302
    for _ in range(dash.LOGIN_MAX_FAILURES - 1):
        assert _sign_in(client, 'guess').status_code == 401     # counting starts again
    # Old failures are forgotten after the window
    clock.advance(dash.LOGIN_WINDOW_SECONDS + 1)
    assert _sign_in(client, 'guess').status_code == 401


def test_overlong_password_is_refused_without_hashing(make_client, auth_cfg):
    client, _ = make_client({'auth': auth_cfg})
    assert _sign_in(client, 'x' * (dash.MAX_PASSWORD_LEN + 1)).status_code == 401
    assert _sign_in(client, '').status_code == 401


@pytest.mark.parametrize('target', ['//evil.example/', 'https://evil.example/', '/\\evil.example',
                                    'evil', '/login', '/logout?x=1', '/a\nb', ''])
def test_sign_in_never_redirects_off_site(make_client, auth_cfg, target):
    client, _ = make_client({'auth': auth_cfg})
    resp = _sign_in(client, data={'next': target})
    assert resp.status_code == 302 and resp.headers['Location'] == '/'


def test_cross_site_form_posts_are_refused(make_client, auth_cfg):
    client, _ = make_client({'auth': auth_cfg})
    resp = _sign_in(client, headers={'Origin': 'http://evil.example'})
    assert resp.status_code == 400 and _session_cookies(resp) == []
    assert _sign_in(client, headers={'Origin': 'null'}).status_code == 400
    assert _sign_in(client, headers={'Origin': 'http://localhost'}).status_code == 302

    resp = client.post('/logout', headers={'Origin': 'http://evil.example'})
    assert resp.status_code == 400 and client.get('/').status_code == 200          # still signed in


def test_sign_out(make_client, auth_cfg):
    client, _ = make_client({'auth': auth_cfg})
    _sign_in(client)
    assert client.get('/logout').status_code == 405             # a link or image can't sign you out
    resp = client.post('/logout')
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'
    assert client.get('/').status_code == 302 and client.get('/api/data').status_code == 401


def test_health_is_open_even_with_auth(make_client, auth_cfg):
    client, fake = make_client({'auth': auth_cfg})
    resp = client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}     # no system data
    assert fake.count() == 0                       # liveness only: no Manager call
    # The exemption is exact: nothing else is opened up
    assert client.get('/health/x').status_code == 302
    assert client.get('/healthz').status_code == 302
    assert client.get('/api/data').status_code == 401

    client, _ = make_client()
    assert client.get('/health').status_code == 200


def test_unwritable_key_file_still_lets_people_sign_in(make_client, tmp_path, caplog):
    auth = {'password_hash': PASSWORD_HASH, 'session_key_file': str(tmp_path / 'no-such-folder' / 'key')}
    with caplog.at_level(logging.WARNING, logger='test-dashboard'):
        client, _ = make_client({'auth': auth})
    assert any('after every Dashboard restart' in r.getMessage() for r in caplog.records)
    assert _sign_in(client).status_code == 302 and client.get('/').status_code == 200


def test_responses_carry_basic_security_headers(make_client, auth_cfg):
    client, _ = make_client({'auth': auth_cfg})
    for path in ('/login', '/health'):
        headers = client.get(path).headers
        assert headers['X-Frame-Options'] == 'DENY' and headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('auth', [
    {},
    {'username': 'labadmin'},
    {'password_hash': 'plaintext-password'},
    {'password_hash': PASSWORD_HASH, 'remember_days': 0},
    {'password_hash': PASSWORD_HASH, 'remember_days': 'forever'},
    {'password_hash': PASSWORD_HASH, 'remember_days': True},
    {'password_hash': PASSWORD_HASH, 'remember_days': 100000},
])
def test_invalid_auth_config_refuses_to_start(auth):
    with pytest.raises(ValueError):
        dash.init_app({'manager_token': TOKEN, 'auth': auth}, logging.getLogger('test-dashboard'))


def test_old_username_setting_is_ignored_with_a_warning(make_client, auth_cfg):
    auth = dict(auth_cfg, username='labadmin')
    warnings = dash.validate_config({'manager_token': TOKEN, 'host': '127.0.0.1', 'auth': auth})
    assert len(warnings) == 1 and 'username is no longer used' in warnings[0]
    client, _ = make_client({'auth': auth})
    assert _sign_in(client).status_code == 302


def test_hash_password_prints_an_auth_block_without_a_user_name(monkeypatch, capsys):
    monkeypatch.setattr(dash.getpass, 'getpass', lambda prompt='': 'a long lab password')
    assert dash.hash_password_cli() == 0
    out = capsys.readouterr().out
    block = json.loads(out[out.index('{'):out.rindex('}') + 1])
    assert set(block['auth']) == {'password_hash', 'remember_days'} and block['auth']['remember_days'] == 180
    assert dash.check_password_hash(block['auth']['password_hash'], 'a long lab password')
    dash.validate_config({'manager_token': TOKEN, 'host': '127.0.0.1', 'auth': block['auth']})
    monkeypatch.setattr(dash.getpass, 'getpass', lambda prompt='': 'short')
    assert dash.hash_password_cli() == 1


def test_config_warnings():
    warnings = dash.validate_config({'manager_token': 'SAME-AS-ONE-OF-MANAGER-TOKENS', 'host': '0.0.0.0'})
    assert any('placeholder' in w for w in warnings)
    assert any('without authentication' in w for w in warnings)
    assert dash.validate_config({'manager_token': TOKEN, 'host': '127.0.0.1'}) == []


# ---------------------------------------------------------------------------
# parse_folder_usage
# ---------------------------------------------------------------------------

def test_parse_folder_usage_skips_metadata_and_nulls():
    raw = {
        'name': 'Triton', 'system_id': 'x', 'device_type': 'nas', 'timestamp': 't',
        'total_disk_bytes': 5, 'data_type': 'folder_usage',
        '_unit': 'bytes',
        '_timed_out': ['/volume1/Huge'],
        '_errors': ['/volume1/Broken', None],
        '_scan_seconds': 12.5,
        '_future_metadata': 123,           # numeric "_" key must not become a volume
        '/volume1': 600,
        '/volume1_total_bytes': 1000,
        '/volume1_free_bytes': 400,
        '/volume1/Data': 500,
        '/volume1/Unmeasured': None,       # null: omitted, not 0
        '/volume1/NaN': float('nan'),
        '/volume1/Flag': True,             # bools are not byte counts
        'E:': 10,
        'E:\\Users': 7,
        'total_usage': 610,
    }
    out = dash.parse_folder_usage(raw)
    assert [f['path'] for f in out['folders']] == ['/volume1/Data', 'E:\\Users']
    assert [v['path'] for v in out['volumes']] == ['/volume1', 'E:']
    vol1 = out['volumes'][0]
    assert vol1['total_bytes'] == 1000 and vol1['free_bytes'] == 400
    assert 'total_bytes' not in out['volumes'][1]
    assert out['total_usage_bytes'] == 610
    assert out['timed_out'] == ['/volume1/Huge']
    assert out['errors'] == ['/volume1/Broken']
    assert out['scan_seconds'] == 12.5
    assert out['units'] == {'_unit': 'bytes'}


def test_parse_folder_usage_linux_volumes_at_any_depth():
    # A Linux collector: volumes from the mount table (/, /mnt/data), folders
    # from scan_paths "/home/*" and "/*" (/data is a folder, not a volume)
    raw = {
        '_volumes': ['/', '/mnt/data'],
        '/': 10, '/_total_bytes': 100, '/_free_bytes': 90,
        '/mnt/data': 50 * 10**12, '/mnt/data_total_bytes': 100 * 10**12,
        '/mnt/data_free_bytes': 50 * 10**12,
        '/home/alice': 3000, '/data': 12345, '/scratch': 7,
        'total_usage': 10 + 50 * 10**12,
    }
    out = dash.parse_folder_usage(raw)
    assert [(v['path'], v.get('total_bytes')) for v in out['volumes']] == [
        ('/', 100), ('/mnt/data', 100 * 10**12)]
    assert [f['path'] for f in out['folders']] == ['/data', '/home/alice', '/scratch']


def test_parse_folder_usage_capacity_marks_a_volume_without_volume_list():
    # No "_volumes" (collector before it): a key with capacity is a volume at
    # any depth; otherwise depth decides, as it always did
    raw = {'/mnt/data': 5, '/mnt/data_total_bytes': 10, '/volume1': 3, '/volume1/Data': 2}
    out = dash.parse_folder_usage(raw)
    assert [v['path'] for v in out['volumes']] == ['/mnt/data', '/volume1']
    assert [f['path'] for f in out['folders']] == ['/volume1/Data']
    # A malformed "_volumes" is ignored, not trusted
    out = dash.parse_folder_usage({**raw, '_volumes': 'oops'})
    assert [v['path'] for v in out['volumes']] == ['/mnt/data', '/volume1']


def test_parse_folder_usage_null_total_and_garbage():
    out = dash.parse_folder_usage({'total_usage': None, '/v1/a': 'abc', '/v1_total_bytes': None})
    assert out['total_usage_bytes'] is None
    assert out['total_usage_formatted'] == '—'
    assert out['folders'] == [] and out['volumes'] == []
    assert out['timed_out'] == [] and out['errors'] == [] and out['scan_seconds'] is None
    assert dash.parse_folder_usage(None)['folders'] == []


def test_parse_folder_usage_internal_columns_are_metadata():
    # history rows (/api/data/<sys>/folder_usage) carry the Manager's own
    # columns; payload keys that collided with them arrive as payload_<key>
    raw = {'id': 7, 'msg_id': 'abc', 'timestamp': '2026-11-21T00:00:00Z', 'device_type': 'nas',
           'first_seen': 1, 'last_seen': 2, 'payload_id': 99, 'payload_timestamp': 5,
           '/volume1': 600, '/volume1/Data': 500}
    out = dash.parse_folder_usage(raw)
    assert [v['path'] for v in out['volumes']] == ['/volume1']
    assert [f['path'] for f in out['folders']] == ['/volume1/Data']


def test_format_bytes_binary_units():
    assert dash.format_bytes(1536) == '1.50 KiB'
    assert dash.format_bytes(3 * 1024 ** 4) == '3.00 TiB'
    assert dash.format_bytes(None) == '—'
    assert dash.format_bytes(float('nan')) == '—'


# ---------------------------------------------------------------------------
# History proxy: validation, quoting, max_points
# ---------------------------------------------------------------------------

HISTORY = '/api/history/*'


def _history_routes(points=None):
    payload = {'system_name': 'Triton', 'data_type': 'system_metrics', 'field': 'cpu_percent',
               'unit': '%', 'data': points if points is not None else
               [{'timestamp': '2026-11-21T00:00:00Z', 'value': 1.0}]}
    return {HISTORY: FakeResponse(200, payload)}


@pytest.mark.parametrize('url', [
    '/api/history/bad%20name!/system_metrics/cpu_percent',
    '/api/history/' + 'a' * 65 + '/system_metrics/cpu_percent',
    '/api/history/%2E%2E/system_metrics/cpu_percent',
    '/api/history/Triton/system_metrics%3Bdrop/cpu_percent',
    '/api/history/Triton/system_metrics/..',
    '/api/history/Triton/system_metrics/cpu_percent?from=yesterday',
    '/api/history/Triton/system_metrics/cpu_percent?to=2026-01-01%27%3B',
])
def test_history_rejects_invalid_input(make_client, url):
    client, fake = make_client(routes=_history_routes())
    resp = client.get(url)
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False
    assert fake.count() == 0


def test_history_quotes_path_segments(make_client):
    client, fake = make_client(routes=_history_routes())
    assert client.get('/api/history/Lab-A.1/folder_usage/E%3A').status_code == 200
    assert client.get('/api/history/Triton/folder_usage/volume1%2Fmy%20folder%3F').status_code == 200
    urls = [c['url'] for c in fake.calls]
    assert urls[0] == 'http://manager.test:5000/api/history/Lab-A.1/folder_usage/E%3A'
    assert urls[1] == 'http://manager.test:5000/api/history/Triton/folder_usage/volume1%2Fmy%20folder%3F'


def test_history_allows_grandfathered_registered_names(make_client):
    # An older Manager registered devices under any name; the new Manager still
    # serves those exact names, so their charts must work once the registry lists them
    routes = {**healthy_manager(), **_history_routes()}
    routes['/api/devices'] = FakeResponse(200, {'devices': [{'name': 'Lab PC', 'device_type': 'server'}]})
    client, fake = make_client(routes=routes)
    url = '/api/history/Lab%20PC/system_metrics/cpu_percent'
    assert client.get(url).status_code == 400                    # not listed by the Manager yet
    assert client.get('/api/data').status_code == 200
    assert client.get(url).status_code == 200
    assert fake.calls[-1]['url'] == 'http://manager.test:5000/api/history/Lab%20PC/system_metrics/cpu_percent'
    assert client.get('/api/history/Other%20PC/system_metrics/cpu_percent').status_code == 400


def test_history_passes_max_points_and_limits(make_client):
    client, fake = make_client({'history_max_rows': 50000}, routes=_history_routes())

    client.get('/api/history/Triton/system_metrics/cpu_percent'
               '?from=2000-01-01T00:00:00Z&to=2026-11-21T10:00:00.000Z&max_points=500')
    params = fake.calls[-1]['params']
    assert params['max_points'] == 500
    assert params['from'] == '2000-01-01T00:00:00Z'
    assert params['to'] == '2026-11-21T10:00:00.000Z'
    assert params['limit'] == 50000

    client.get('/api/history/Triton/system_metrics/cpu_percent?from=2026-11-01')
    assert fake.calls[-1]['params']['max_points'] == 2000     # default chart_max_points

    client.get('/api/history/Triton/system_metrics/cpu_percent?limit=99999999&max_points=99999999')
    params = fake.calls[-1]['params']
    assert params['limit'] == 10000 and params['max_points'] == 10000
    assert 'from' not in params

    client.get('/api/history/Triton/system_metrics/cpu_percent')
    assert fake.calls[-1]['params']['limit'] == 500


def test_history_downsamples_when_manager_ignores_max_points(make_client):
    points = [{'timestamp': f'2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z', 'value': float(i)}
              for i in range(5000)]
    client, _ = make_client(routes=_history_routes(points))
    body = client.get('/api/history/Triton/system_metrics/cpu_percent'
                      '?from=2026-01-01&max_points=1000').get_json()
    assert len(body['data']) <= 1000
    assert body['downsampled'] is True
    assert body['downsampled_from'] == 5000
    # bucket means: first bucket of 5 values 0..4 -> 2.0
    assert body['data'][0]['value'] == 2.0


def test_history_small_result_untouched(make_client):
    client, _ = make_client(routes=_history_routes())
    body = client.get('/api/history/Triton/system_metrics/cpu_percent').get_json()
    assert body['data'] == [{'timestamp': '2026-11-21T00:00:00Z', 'value': 1.0}]
    assert 'downsampled' not in body


def test_history_upstream_errors_are_generic(make_client):
    client, _ = make_client(routes={HISTORY: requests.exceptions.ConnectionError('db-host:5000 refused')})
    resp = client.get('/api/history/Triton/system_metrics/cpu_percent')
    assert resp.status_code == 502
    assert resp.get_json() == {'success': False, 'error': 'Manager unavailable'}

    client, _ = make_client(routes={})          # Manager 404 for unknown system
    resp = client.get('/api/history/Nobody/system_metrics/cpu_percent')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Page / routes
# ---------------------------------------------------------------------------

def test_index_renders_client_config_and_pinned_plotly(make_client):
    client, _ = make_client({'stale_minutes': 20, 'offline_minutes': 90, 'refresh_interval_seconds': 45})
    page = client.get('/').get_data(as_text=True)
    assert 'plotly-latest.min.js' not in page
    assert 'src="https://cdn.plot.ly/plotly-1.58.5.min.js"' in page
    assert 'integrity="sha384-' in page and 'crossorigin="anonymous"' in page
    assert '"staleMinutes": 20' in page and '"offlineMinutes": 90' in page
    assert '"refreshInterval": 45000' in page
    # Browser gives up on a hung /api/data after 4 x manager timeout + 5 s
    assert '"dataTimeoutMs": 25000' in page

    client, _ = make_client({'manager_timeout_seconds': 10})
    assert '"dataTimeoutMs": 45000' in client.get('/').get_data(as_text=True)


def test_removed_unused_routes(make_client):
    client, _ = make_client()
    assert client.get('/api/system/Triton/disk_history').status_code == 404
    assert client.get('/api/system/Triton/metrics_history').status_code == 404


def test_static_assets_present():
    static = REPO / 'dashboard' / 'static'
    assert not (static / 'js' / 'dashboard-old.js').exists()
    assert not (static / 'img' / 'backdrop.png').exists()
    backdrop = static / 'img' / 'backdrop.jpg'
    assert backdrop.exists() and backdrop.stat().st_size < 300 * 1024
    assert 'backdrop.jpg' in (static / 'css' / 'style.css').read_text()


# ---------------------------------------------------------------------------
# Usage-metrics Manager: optional endpoints (spec §16, R7)
# ---------------------------------------------------------------------------

# Appendix A payloads, as the NEW Manager serves them (§16 shapes; B4 passes
# the §11 system_metrics keys through /api/metrics/all)
A1_SYSTEM_METRICS = {
    "cpu_percent": 23.4, "cpu_percent_unit": "%", "interval_s": 300.2, "interval_s_unit": "s",
    "cpu_count": 32, "cpu_iowait_percent": 1.2, "cpu_iowait_percent_unit": "%", "cpu_steal_percent": 0.0,
    "cpu_steal_percent_unit": "%", "proc_cpu_coverage_percent": 96.5, "proc_cpu_coverage_percent_unit": "%",
    "disk_read_bytes_per_s": 1250000, "disk_read_bytes_per_s_unit": "B/s", "disk_write_bytes_per_s": 310000,
    "disk_write_bytes_per_s_unit": "B/s", "disk_read_iops": 41.2, "disk_read_iops_unit": "ops/s",
    "disk_write_iops": 12.0, "disk_write_iops_unit": "ops/s", "disk_await_ms": 6.3, "disk_await_ms_unit": "ms",
    "disk_busy_max_percent": 17.0, "disk_busy_max_percent_unit": "%", "disk_busy_max_name": "sata2",
    "gpu_sample_seconds": 300.2, "gpu_sample_seconds_unit": "s", "gpu_count": 1,
    "gpus": [{"index": 0, "name": "NVIDIA RTX A6000", "gpu_percent": 87, "vram_used_bytes": 22548578304,
              "vram_total_bytes": 51527024640, "vram_used_formatted": "21.0 GB", "vram_total_formatted": "48.0 GB",
              "temperature_c": 71, "power_watts": 250.1, "uuid": "GPU-8f2c0000-1111-2222-3333-444455556666",
              "process_source": "nvml", "util_split": "single_user",
              "users": [{"username": "LAB\\alice", "vram_bytes": 21474836480, "util_percent": 87.0, "processes": 2}],
              "users_truncated": 0, "unattributed_vram_bytes": 1073741824, "unattributed_processes": 1}],
}
A2_USERS = [
    {"username": "LAB\\alice", "cpu_percent": 12.5, "ram_bytes": 8589934592, "ram_formatted": "8.0 GB",
     "cpu_seconds": 1200.4, "cpu_cores": 4.0, "interval_s": 300.2, "process_count": 17,
     "gpu_vram_bytes": 21474836480, "gpu_util_percent": 87.0, "gpu_busy_seconds": 261.2, "gpus_held": 1,
     "gpu_held_seconds": 300.2, "gpu_processes": 2, "timestamp": "2026-11-21T00:00:01Z"},
    {"username": "LAB\\bob", "cpu_percent": None, "ram_bytes": None, "ram_formatted": None,
     "gpu_vram_bytes": 1073741824, "gpu_util_percent": 0.0, "gpu_busy_seconds": 0.0, "gpus_held": 1,
     "gpu_held_seconds": 300.2, "gpu_processes": 1, "timestamp": "2026-11-21T00:00:01Z"},
]
A3_SESSIONS = {
    "data_type": "sessions", "_schema": 1, "_unit": "count", "_detail": "full",
    "_sources": {"tcp": "ok", "utmp": "ok", "smb": "not_permitted"}, "_collect_seconds": 0.04,
    "_list_state": "full", "_list_hash": "3f9a1c2b7d4e5f60",
    "clients_total": 9, "users_total": 1, "smb_clients": 7, "nfs_clients": 1, "ssh_clients": 1, "ssh_users": 1,
    "dsm_clients": 2, "rsync_clients": 0, "sessions_truncated": False,
    "sessions": [{"service": "ssh", "username": "jeff", "client": "10.11.2.40", "connections": 1,
                  "since": "2026-11-21T12:35:00Z", "source": "utmp"},
                 {"service": "smb", "client": "10.11.2.17", "connections": 2, "source": "tcp"}],
}
A4_HEALTH = {
    "data_type": "storage_health", "_schema": 1, "trigger": "change", "overall_status": "warning",
    "overall_status_code": 1, "status_reasons": ["md2 rebuilding 37.5% (ETA 5h 12m)"],
    "sources": {"md": "ok", "sysfs": "ok", "mounts": "ok", "snmp": "off", "smart": "not_permitted"},
    "arrays": [{"id": "md2", "kind": "md", "name": None, "level": "raid5", "role": "data", "state": "active",
                "status": "warning", "members_total": 4, "members_active": 3, "degraded": 1,
                "sync_action": "recover", "sync_percent": 37.5, "sync_speed_kbps": 120000,
                "sync_eta_seconds": 18720, "mismatch_cnt": 0, "size_bytes": 11995903803392,
                "faulty_members": [], "members": ["sata1", "sata2", "sata4", "sata3"]}],
    "disks": [{"id": "sata3", "model": "WD80EFZX", "serial": None, "size_bytes": 8001563222016, "media": "hdd",
               "bus": "sata", "status": "ok", "health_source": "sysfs", "smart_passed": None,
               "temperature_c": None, "power_on_hours": None, "reallocated_sectors": None,
               "pending_sectors": None, "uncorrectable_sectors": None, "read_errors_uncorrected": None,
               "write_errors_uncorrected": None, "wear_used_percent": None, "remaining_life_percent": None,
               "bad_sectors": None, "io_errors": 0, "device_state": "running", "in_standby": None,
               "arrays": ["md2"]}],
    "volumes": [{"id": "/volume1", "fs": "btrfs", "read_only": False, "health": None, "status": "ok"}],
    "events": None, "_flat_truncated": False,
}
A5_BREAKDOWN = {
    "reference_time": "2026-11-21T02:00:03Z", "cold_thresholds_days": [365, 1095], "owner_source": "posix_uid",
    "folders": [{"path": "/volume1/Lab", "folder_owner": "jeff", "bytes": 5000, "files": 120,
                 "cold_1y_bytes": 3000, "cold_3y_bytes": 1000}],
    "device": {"bytes": 5000, "files": 120, "cold_1y_bytes": 3000, "cold_3y_bytes": 1000,
               "owners": [{"owner": "jeff", "bytes": 4000, "files": 100, "cold_1y_bytes": 2500, "cold_3y_bytes": 900}],
               "other": {"owner_count": 3, "bytes": 1000, "files": 20, "cold_1y_bytes": 500, "cold_3y_bytes": 100}},
    "volume_owners": None,
}
USAGE_ROW = {"username": "LAB\\alice", "samples": 288, "cpu_core_hours": 30.0, "avg_cores": 1.25, "peak_cores": 4.0,
             "gpu_busy_hours": 18.2, "gpu_held_hours": 23.9, "peak_gpu_vram_bytes": 21474836480}


def new_manager():
    """healthy_manager() plus what the usage-metrics Manager adds."""
    routes = healthy_manager()
    atlas = dict(routes['/api/metrics/all']._payload['Atlas'])
    atlas.update({k: v for k, v in A1_SYSTEM_METRICS.items() if not k.endswith('_unit')})
    atlas['units'] = {k: v for k, v in A1_SYSTEM_METRICS.items() if k.endswith('_unit')}
    routes['/api/metrics/all'] = FakeResponse(200, {'Atlas': atlas})
    routes['/api/user_metrics/all'] = FakeResponse(200, {'Atlas': [dict(u) for u in A2_USERS]})
    ts = '2026-11-21T00:00:00Z'
    routes[dash.SESSIONS_ENDPOINT] = FakeResponse(200, {'Triton': {
        'timestamp': ts, 'list_timestamp': ts, 'list_confirmed_at': ts,
        'summary': {k: v for k, v in A3_SESSIONS.items() if k not in ('data_type', 'sessions')},
        'sessions': A3_SESSIONS['sessions']}})
    routes[dash.HEALTH_ENDPOINT] = FakeResponse(200, {'Triton': dict(
        {k: A4_HEALTH[k] for k in ('overall_status', 'status_reasons', 'sources', 'arrays', 'disks', 'volumes', 'events')},
        timestamp=ts)})
    folders = [dict({k: f[k] for k in ('path', 'folder_owner', 'bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')},
                    top_owner='jeff', top_owner_bytes=4000) for f in A5_BREAKDOWN['folders']]
    routes[dash.BREAKDOWN_ENDPOINT] = FakeResponse(200, {'Triton': dict(
        {k: A5_BREAKDOWN[k] for k in ('owner_source', 'reference_time', 'cold_thresholds_days', 'device', 'volume_owners')},
        timestamp=ts, folders=folders, folders_truncated=False)})
    routes[dash.USAGE_ENDPOINT] = FakeResponse(200, {
        'window': '24h', 'from': '2026-11-20T00:00:00Z', 'to': ts,
        'devices': {'Atlas': {'source': 'rollups', 'complete': True, 'users': [USAGE_ROW]}}})
    return routes


def test_current_manager_404s_are_not_errors(make_client):
    # healthy_manager() has none of the optional endpoints: FakeManager answers 404
    client, fake = make_client()
    body = client.get('/api/data').get_json()
    assert body['success'] is True and body['degraded'] is False and body['errors'] == []
    assert body['features'] == {'sessions': False, 'storage_health': False, 'breakdown': False, 'usage': False}
    for sys_ in body['systems'].values():
        assert not {'sessions', 'storage_health', 'breakdown', 'usage'} & set(sys_)
    assert {fake.count(p) for p in dash.OPTIONAL_ENDPOINTS} == {1}


def test_old_manager_usage_route_answers_a_user_list(make_client):
    # OLD (f086e7e) routes /api/user_metrics/usage to /api/user_metrics/<device_name>: 200 []
    routes = healthy_manager()
    routes[dash.USAGE_ENDPOINT] = FakeResponse(200, [])
    client, _ = make_client(routes=routes)
    body = client.get('/api/data').get_json()
    assert body['degraded'] is False and body['errors'] == []
    assert body['features']['usage'] is False


@pytest.mark.parametrize('path', sorted(dash.OPTIONAL_ENDPOINTS))
def test_optional_endpoint_failure_is_degraded(make_client, path):
    routes = new_manager()
    routes[path] = FakeResponse(500, {'status': 'error', 'error': 'Internal error'})
    client, _ = make_client(routes=routes)
    body = client.get('/api/data').get_json()
    assert body['success'] is True and body['degraded'] is True
    assert body['errors'] == [{'endpoint': path, 'error': 'HTTP 500'}]
    key = dash.OPTIONAL_ENDPOINTS[path]
    assert body['features'][key] is None       # unknown, not "unsupported"
    assert all(v is True for k, v in body['features'].items() if k != key)


@pytest.mark.parametrize('path', sorted(dash.OPTIONAL_ENDPOINTS))
def test_optional_endpoint_wrong_top_level_shape_is_degraded(make_client, path):
    routes = new_manager()
    routes[path] = FakeResponse(200, 'not a dict')
    client, _ = make_client(routes=routes)
    body = client.get('/api/data').get_json()
    assert {'endpoint': path, 'error': 'unexpected response'} in body['errors']


def test_new_manager_sections_are_merged(make_client):
    client, _ = make_client(routes=new_manager())
    body = client.get('/api/data').get_json()
    assert body['degraded'] is False and body['errors'] == []
    assert body['features'] == {'sessions': True, 'storage_health': True, 'breakdown': True, 'usage': True}
    atlas, triton = body['systems']['Atlas'], body['systems']['Triton']

    m = atlas['metrics']
    assert m['interval_s'] == 300.2 and m['cpu_count'] == 32 and m['disk_read_bytes_per_s'] == 1250000
    assert m['disk_busy_max_name'] == 'sata2' and m['units']['disk_read_bytes_per_s_unit'] == 'B/s'
    assert m['gpus'][0]['users'] == [{'username': 'LAB\\alice', 'vram_bytes': 21474836480,
                                      'util_percent': 87.0, 'processes': 2}]
    assert m['gpus'][0]['unattributed_vram_bytes'] == 1073741824 and m['gpus'][0]['uuid'].startswith('GPU-')
    assert [u['username'] for u in atlas['users']] == ['LAB\\alice', 'LAB\\bob']
    assert atlas['users'][1]['cpu_percent'] is None and atlas['users'][1]['gpus_held'] == 1

    s = triton['sessions']
    assert s['list_confirmed_at'] == '2026-11-21T00:00:00Z'
    assert s['summary']['clients_total'] == 9 and s['summary']['rsync_clients'] == 0
    assert s['summary']['_sources'] == {'tcp': 'ok', 'utmp': 'ok', 'smb': 'not_permitted'}
    assert s['summary']['_detail'] == 'full' and s['summary']['sessions_truncated'] is False
    assert s['sessions'][1] == {'service': 'smb', 'client': '10.11.2.17', 'connections': 2, 'source': 'tcp'}

    h = triton['storage_health']
    assert h['overall_status'] == 'warning' and h['status_reasons'] == ['md2 rebuilding 37.5% (ETA 5h 12m)']
    assert h['arrays'][0]['members'] == ['sata1', 'sata2', 'sata4', 'sata3'] and h['arrays'][0]['sync_percent'] == 37.5
    assert h['disks'][0]['model'] == 'WD80EFZX' and h['disks'][0]['temperature_c'] is None
    assert h['volumes'][0] == {'id': '/volume1', 'fs': 'btrfs', 'health': None, 'status': 'ok', 'read_only': False}
    assert 'disk_sata3_status_code' not in h and h['events'] is None

    b = triton['breakdown']
    assert b['owner_source'] == 'posix_uid' and b['cold_thresholds_days'] == [365, 1095]
    assert b['folders'][0]['cold_1y_bytes'] == 3000 and b['folders'][0]['top_owner'] == 'jeff'
    assert b['device']['owners'][0] == {'owner': 'jeff', 'bytes': 4000, 'files': 100,
                                       'cold_1y_bytes': 2500, 'cold_3y_bytes': 900}
    assert b['device']['other']['owner_count'] == 3

    u = atlas['usage']
    assert u['window'] == '24h' and u['source'] == 'rollups' and u['complete'] is True
    assert u['users'] == [USAGE_ROW]
    # Sections only attach to systems the device registry lists
    assert 'usage' not in triton and 'sessions' not in atlas


def test_malformed_rows_are_ignored(make_client):
    routes = new_manager()
    long = 'x' * 257
    routes[dash.SESSIONS_ENDPOINT] = FakeResponse(200, {
        'Triton': {'summary': {'clients_total': 'many', 'smb_clients': '7', 'Bad_clients': 3, 'x_users': None,
                               '_sources': ['tcp'], '_detail': 5},
                   'sessions': [{'username': long, 'client': 7, 'connections': 'two'}, 'row', None,
                                {'service': 'ssh', 'username': 'jeff', 'pid': 123}]},
        'Atlas': 'not a dict',
        'Ghost': {'summary': {}, 'sessions': []},          # not in the device registry
    })
    routes[dash.HEALTH_ENDPOINT] = FakeResponse(200, {'Triton': {
        'overall_status': 'on fire', 'status_reasons': ['r' * 300] + [str(i) for i in range(20)] + [5],
        'sources': {'md': 'ok', 'smart': 3}, 'arrays': 'md2', 'disks': [{'id': 'sda', 'status': 'melted',
        'temperature_c': float('nan')}, 'sdb'], 'volumes': [{'id': '/v', 'read_only': 'yes'}], 'events': [1]}})
    routes[dash.BREAKDOWN_ENDPOINT] = FakeResponse(200, {'Triton': {
        'folders': [{'path': '/a', 'bytes': '12'}, {'bytes': 5}, 'x'], 'device': {'owners': None, 'other': 'x'},
        'cold_thresholds_days': 'soon'}})
    routes[dash.USAGE_ENDPOINT] = FakeResponse(200, {'window': '24h', 'devices': {
        'Atlas': {'users': [{'username': 5}, {'username': 'ok', 'cpu_core_hours': 'lots'}], 'complete': 'yes'},
        'Triton': []}})
    client, _ = make_client(routes=routes)
    body = client.get('/api/data').get_json()
    assert body['degraded'] is False and body['errors'] == []
    systems = body['systems']
    assert 'Ghost' not in systems and 'sessions' not in systems['Atlas']

    s = systems['Triton']['sessions']
    assert s['summary'] == {'clients_total': None, 'smb_clients': 7.0, 'x_users': None, '_detail': None, '_sources': {}}
    assert s['sessions'] == [{'username': None, 'client': None, 'connections': None},
                             {'service': 'ssh', 'username': 'jeff'}]
    assert s['timestamp'] is None

    h = systems['Triton']['storage_health']
    assert h['overall_status'] is None and h['arrays'] == [] and h['events'] is None
    assert len(h['status_reasons']) == 10 and h['status_reasons'][0] == 'r' * 200
    assert h['sources'] == {'md': 'ok'}
    assert h['disks'] == [{'id': 'sda', 'status': None, 'temperature_c': None}]
    assert h['volumes'] == [{'id': '/v', 'status': None, 'read_only': None}]

    b = systems['Triton']['breakdown']
    assert b['folders'] == [{'path': '/a', 'bytes': 12.0}]
    assert b['device'] == {'owners': None, 'other': None} and b['cold_thresholds_days'] is None

    u = systems['Atlas']['usage']
    assert u['users'] == [{'username': 'ok', 'cpu_core_hours': None}] and u['complete'] is None
    assert 'usage' not in systems['Triton']


@pytest.mark.parametrize('status', [[], ['warning'], {}, {'a': 1}, [None], [[]], 3, None])
def test_unhashable_overall_status_is_ignored(make_client, status):
    # A list or dict must not reach the frozenset lookup (TypeError -> HTTP 500 for every viewer)
    routes = new_manager()
    health = routes[dash.HEALTH_ENDPOINT]._payload['Triton']
    health['overall_status'] = status
    client, _ = make_client(routes=routes)
    r = client.get('/api/data')
    assert r.status_code == 200
    body = r.get_json()
    assert body['degraded'] is False and body['errors'] == []
    h = body['systems']['Triton']['storage_health']
    assert h['overall_status'] is None and h['disks']        # the rest of the section still merges


def test_stale_user_rows_are_dropped(make_client):
    routes = healthy_manager()      # Atlas system_metrics at 2026-11-21T00:00:00Z
    routes['/api/user_metrics/all'] = FakeResponse(200, {'Atlas': [
        {'username': 'now', 'timestamp': '2026-11-21T00:00:00Z'},
        {'username': 'newer', 'timestamp': '2026-11-21T00:00:09.123456Z'},
        {'username': 'two-min', 'timestamp': '2026-11-20T23:58:00Z'},              # exactly 120 s: kept
        {'username': 'offset', 'timestamp': '2026-11-21T01:00:00+01:00'},           # same instant: kept
        {'username': 'gone', 'timestamp': '2026-11-20T23:57:59Z'},                  # 121 s older: dropped
        {'username': 'naive-old', 'timestamp': '2026-11-20 20:00:00'},               # no zone = UTC: dropped
        {'username': 'no-ts'}, {'username': 'bad-ts', 'timestamp': 'yesterday'},   # age unknown: kept
    ]})
    client, _ = make_client(routes=routes)
    users = client.get('/api/data').get_json()['systems']['Atlas']['users']
    assert [u['username'] for u in users] == ['now', 'newer', 'two-min', 'offset', 'no-ts', 'bad-ts']

    # No system_metrics timestamp: every row is kept
    routes['/api/metrics/all'] = FakeResponse(200, {})
    client, _ = make_client(routes=routes)
    assert len(client.get('/api/data').get_json()['systems']['Atlas']['users']) == 8


def test_gpu_users_are_cleaned(make_client):
    routes = healthy_manager()
    gpus = [
        {'index': 0, 'name': 'A', 'users': [
            {'username': 'LAB\\alice', 'vram_bytes': '1024', 'util_percent': float('nan'), 'processes': True, 'pid': 7},
            {'username': None, 'vram_bytes': 1}, {'username': 'u' * 257}, 'bob', None,
            {'username': 'carol'}],
         'users_truncated': 'two', 'unattributed_vram_bytes': -1, 'process_source': 'nvml'},
        {'index': 1, 'name': 'B', 'users': 'not a list'},
        {'index': 2, 'name': 'C'},                      # gpu_per_user "off": no users key
        'not a gpu',
    ]
    routes['/api/metrics/all'] = FakeResponse(200, {'Atlas': {'timestamp': '2026-11-21T00:00:00Z', 'gpus': gpus}})
    client, _ = make_client(routes=routes)
    out = client.get('/api/data').get_json()['systems']['Atlas']['metrics']['gpus']
    assert len(out) == 3
    assert out[0]['users'] == [{'username': 'LAB\\alice', 'vram_bytes': 1024.0, 'util_percent': None, 'processes': None},
                               {'username': 'carol'}]
    assert out[0]['users_truncated'] is None and out[0]['unattributed_vram_bytes'] == -1
    assert out[0]['process_source'] == 'nvml' and out[0]['name'] == 'A'
    assert out[1]['users'] is None
    assert 'users' not in out[2]


# ---------------------------------------------------------------------------
# Per-endpoint TTL cache
# ---------------------------------------------------------------------------

def test_endpoint_ttls_and_failures(make_client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dash, '_monotonic', clock)
    client, fake = make_client({'refresh_interval_seconds': 30}, routes=new_manager())

    def polls(path):
        return fake.count(path)

    assert client.get('/api/data').get_json()['degraded'] is False
    assert all(polls(p) == 1 for p in dash.POLL_ENDPOINTS)

    # 31 s: the /api/data cache expired; only sessions (no TTL) and the core calls are fetched again,
    # so a failure of the cached endpoints isn't even seen
    fake.routes[dash.HEALTH_ENDPOINT] = FakeResponse(500, {'status': 'error', 'error': 'x'})
    clock.now += 31
    body = client.get('/api/data').get_json()
    assert body['degraded'] is False
    assert body['systems']['Triton']['storage_health']['overall_status'] == 'warning'   # cached good value
    assert polls(dash.SESSIONS_ENDPOINT) == 2 and polls('/api/devices') == 2
    assert polls(dash.HEALTH_ENDPOINT) == polls(dash.USAGE_ENDPOINT) == polls(dash.BREAKDOWN_ENDPOINT) == 1

    # 62 s: health's 60 s TTL is over - the failed fetch is reported, and not cached
    clock.now += 31
    body = client.get('/api/data').get_json()
    assert body['degraded'] is True and body['errors'] == [{'endpoint': dash.HEALTH_ENDPOINT, 'error': 'HTTP 500'}]
    assert polls(dash.HEALTH_ENDPOINT) == 2
    fake.routes[dash.HEALTH_ENDPOINT] = new_manager()[dash.HEALTH_ENDPOINT]
    clock.now += 6                                  # past the 5 s degraded /api/data TTL
    assert client.get('/api/data').get_json()['degraded'] is False
    assert polls(dash.HEALTH_ENDPOINT) == 3         # retried at once, not after another TTL

    # usage (300 s) and breakdown (600 s)
    clock.now += 300 - 68 + 1
    client.get('/api/data')
    assert polls(dash.USAGE_ENDPOINT) == 2 and polls(dash.BREAKDOWN_ENDPOINT) == 1
    clock.now += 300
    client.get('/api/data')
    assert polls(dash.BREAKDOWN_ENDPOINT) == 2


def test_endpoint_404_is_cached_for_its_ttl(make_client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dash, '_monotonic', clock)
    client, fake = make_client({'refresh_interval_seconds': 30})      # CURRENT Manager: 404s
    client.get('/api/data')
    clock.now += 31
    body = client.get('/api/data').get_json()
    assert body['features']['breakdown'] is False and body['degraded'] is False
    assert fake.count(dash.BREAKDOWN_ENDPOINT) == 1 and fake.count(dash.SESSIONS_ENDPOINT) == 2


# ---------------------------------------------------------------------------
# History proxy: entity, resolution, storage_breakdown fields, aggregates
# ---------------------------------------------------------------------------

def _entity_history(path, params):
    payload = {'system_name': 'Atlas', 'data_type': 'user_metrics', 'field': 'cpu_cores', 'unit': 'cores',
               'entity': (params or {}).get('entity'), 'resolution': 'hour', 'bucket_seconds': 3600,
               'data': [{'timestamp': '2026-11-21T00:30:00Z', 'value': 1.5, 'min': 1.0, 'max': 2.0, 'count': 12}]}
    return FakeResponse(200, payload)


def test_history_forwards_entity_and_resolution(make_client):
    client, fake = make_client(routes={HISTORY: _entity_history})
    resp = client.get('/api/history/Atlas/user_metrics/cpu_cores?from=2026-11-01&entity=LAB%5Calice&resolution=hour')
    assert resp.status_code == 200
    params = fake.calls[-1]['params']
    assert params['entity'] == 'LAB\\alice' and params['resolution'] == 'hour'
    assert resp.get_json()['data'][0] == {'timestamp': '2026-11-21T00:30:00Z', 'value': 1.5,
                                          'min': 1.0, 'max': 2.0, 'count': 12}
    # Neither is sent unless given
    client.get('/api/history/Atlas/system_metrics/cpu_percent')
    assert 'entity' not in fake.calls[-1]['params'] and 'resolution' not in fake.calls[-1]['params']


@pytest.mark.parametrize('query', [
    'entity=', 'entity=' + 'a' * 257, 'entity=al%01ice', 'entity=al%0Aice',
    'resolution=minute', 'resolution=', 'resolution=HOUR',
])
def test_history_rejects_bad_entity_or_resolution(make_client, query):
    client, fake = make_client(routes={HISTORY: _entity_history})
    resp = client.get('/api/history/Atlas/user_metrics/cpu_cores?' + query)
    assert resp.status_code == 400 and fake.count() == 0


def test_history_entity_ignored_by_older_manager_is_not_charted(make_client):
    # CURRENT / OLD Managers ignore ?entity and return every user's values mixed together
    client, _ = make_client(routes=_history_routes())
    resp = client.get('/api/history/Atlas/user_metrics/cpu_percent?entity=alice')
    assert resp.status_code == 404
    assert resp.get_json() == {'success': False, 'error': 'Per-user history needs a newer Manager'}


def test_history_storage_breakdown_field_length(make_client):
    client, fake = make_client(routes=_history_routes())
    # A folder path at the R10 limit (4096 characters) fits
    longest = 'folder/' + '/volume1/' + 'd' * (4096 - 9) + '/bytes'
    assert client.get('/api/history/Triton/storage_breakdown/' + quote(longest, safe='')).status_code == 200
    assert fake.calls[-1]['url'].endswith('/storage_breakdown/' + quote(longest, safe=''))
    assert client.get('/api/history/Triton/storage_breakdown/folder/' + 'd' * 4387 + '/bytes').status_code == 200  # 4400
    assert client.get('/api/history/Triton/storage_breakdown/folder/' + 'd' * 4388 + '/bytes').status_code == 400  # 4401
    # The exception is only for storage_breakdown
    assert client.get('/api/history/Triton/system_metrics/' + 'a' * 513).status_code == 400
    assert client.get('/api/history/Triton/folder_usage/' + 'a' * 600).status_code == 400
    # "." / ".." segments are still refused, with / or \ separators
    for bad in ('folder/volume1/../x/bytes', 'folder%2Fvolume1%2F..%2Fx%2Fbytes', 'owner/./bytes', 'folder/a\\..\\b/bytes'):
        assert client.get('/api/history/Triton/storage_breakdown/' + bad).status_code == 400, bad
    assert client.get('/api/history/Triton/storage_breakdown/owner%2FLAB%5Cjeff%2Fbytes').status_code == 200
    assert fake.calls[-1]['url'].endswith('/storage_breakdown/owner%2FLAB%5Cjeff%2Fbytes')


def test_downsample_keeps_min_max_count():
    points = [{'timestamp': f't{i}', 'value': float(i), 'min': float(i) - 1, 'max': float(i) + 1, 'count': i + 1}
              for i in range(10)]
    out = dash.downsample(points, 5)
    assert len(out) == 5
    # bucket 0: values 0,1 with counts 1,2 -> weighted mean 2/3
    assert out[0] == {'timestamp': 't1', 'value': pytest.approx(2 / 3), 'count': 3, 'min': -1.0, 'max': 2.0}
    # Plain points stay plain (older Managers): no aggregate keys appear
    plain = dash.downsample([{'timestamp': f't{i}', 'value': i} for i in range(4)], 2)
    assert plain == [{'timestamp': 't1', 'value': 0.5}, {'timestamp': 't3', 'value': 2.5}]
    # All-null buckets stay null, never 0
    assert dash.downsample([{'timestamp': 'a', 'value': None, 'count': 0}] * 4, 2)[0]['value'] is None


# ---------------------------------------------------------------------------
# /api/storage/breakdown/<system>?folder= proxy
# ---------------------------------------------------------------------------

def test_breakdown_proxy(make_client):
    answer = {'timestamp': 't', 'folders': [dict(A5_BREAKDOWN['folders'][0], owners=[{'owner': 'jeff', 'bytes': 4000}])]}
    client, fake = make_client(routes={'/api/storage/breakdown/*': FakeResponse(200, answer)})
    resp = client.get('/api/storage/breakdown/Triton?folder=/volume1/Lab')
    assert resp.status_code == 200 and resp.get_json() == answer
    assert fake.calls[-1]['url'] == 'http://manager.test:5000/api/storage/breakdown/Triton'
    assert fake.calls[-1]['params'] == {'folder': '/volume1/Lab'}
    client.get('/api/storage/breakdown/Triton')
    assert fake.calls[-1]['params'] is None
    calls = fake.count()
    for url in ('/api/storage/breakdown/Triton?folder=/volume1/../etc',
                '/api/storage/breakdown/Triton?folder=..',
                '/api/storage/breakdown/Triton?folder=E:%5C..%5Cx',
                '/api/storage/breakdown/Triton?folder=/' + 'a' * 4096,
                '/api/storage/breakdown/bad%20name!?folder=/x'):
        assert client.get(url).status_code == 400, url
    assert fake.count() == calls


def test_breakdown_proxy_passes_404_and_hides_errors(make_client):
    client, _ = make_client(routes={})      # older Manager, or no breakdown for this system
    assert client.get('/api/storage/breakdown/Triton?folder=/volume1/Lab').status_code == 404
    client, _ = make_client(routes={'/api/storage/breakdown/*': requests.exceptions.ConnectionError('db-host refused')})
    resp = client.get('/api/storage/breakdown/Triton?folder=/volume1/Lab')
    assert resp.status_code == 502 and resp.get_json() == {'success': False, 'error': 'Manager unavailable'}


# ---------------------------------------------------------------------------
# Unchanged against the CURRENT (e5cd699) and OLD (f086e7e) Managers
# ---------------------------------------------------------------------------

CURRENT_COMMIT = 'e5cd699'


def _load_current_dashboard(tmp_path):
    """dashboard/app.py as released in CURRENT, loaded from git as its own module."""
    git = shutil.which('git')
    if git is None:
        pytest.skip('git not installed')
    res = subprocess.run([git, '-C', str(REPO), 'show', f'{CURRENT_COMMIT}:dashboard/app.py'],
                         capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip(f'{CURRENT_COMMIT} not available in this checkout')
    path = tmp_path / 'current_dashboard_app.py'
    path.write_text(res.stdout, encoding='utf-8')
    spec = importlib.util.spec_from_file_location('current_dashboard_app', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def old_manager():
    """healthy_manager() as the OLD Manager shapes it: total_ram_bytes defaults to 0,
    /api/user_metrics/usage hits /api/user_metrics/<device_name>, history has no flags."""
    routes = healthy_manager()
    atlas = dict(routes['/api/metrics/all']._payload['Atlas'], total_ram_bytes=0)
    routes['/api/metrics/all'] = FakeResponse(200, {'Atlas': atlas})
    routes[dash.USAGE_ENDPOINT] = FakeResponse(200, [])
    routes[HISTORY] = FakeResponse(200, {'system_name': 'Atlas', 'data_type': 'system_metrics',
                                         'field': 'cpu_percent', 'unit': '%',
                                         'data': [{'timestamp': '2026-11-21T00:00:00Z', 'value': 1.0}]})
    return routes


@pytest.mark.parametrize('routes_for', [healthy_manager, old_manager], ids=['current', 'old'])
def test_older_manager_gives_the_same_data_as_the_current_dashboard(make_client, tmp_path, routes_for):
    current = _load_current_dashboard(tmp_path)
    routes = routes_for()
    routes.setdefault(HISTORY, _history_routes()[HISTORY])
    client, _ = make_client(routes=routes)
    new_body = client.get('/api/data').get_json()

    current.init_app(dict(dash._config), logging.getLogger('test-dashboard-current'))
    payload, status = current.build_data_payload()
    old_body = json.loads(json.dumps(payload))
    assert status == 200 and old_body['degraded'] is False

    assert new_body['degraded'] is False and new_body['errors'] == []
    assert set(new_body) == set(old_body) | {'features'}
    assert new_body['global_totals'] == old_body['global_totals']
    assert set(new_body['systems']) == set(old_body['systems'])
    # Every system exactly as the CURRENT Dashboard presented it: no new keys
    # appear for data an older Manager serves (tools/verify_upgrade.py compares this too)
    assert new_body['systems'] == old_body['systems']

    # Charts work as before (no entity or resolution sent by default)
    resp = client.get('/api/history/Atlas/system_metrics/cpu_percent?from=2026-11-01')
    assert resp.status_code == 200
    assert resp.get_json()['data'] == [{'timestamp': '2026-11-21T00:00:00Z', 'value': 1.0}]


# ---------------------------------------------------------------------------
# Static files: content types don't depend on the system (Windows registry)
# ---------------------------------------------------------------------------

def test_static_files_have_the_types_browsers_require_under_nosniff(make_client):
    import mimetypes
    # What a Windows registry often says; nosniff then makes browsers refuse the script
    mimetypes.add_type('text/plain', '.js')
    mimetypes.add_type('application/octet-stream', '.css')
    dash.register_static_types()

    client, _ = make_client()
    static = REPO / 'dashboard' / 'static'
    files = [p for p in static.rglob('*') if p.is_file()]
    assert any(p.suffix == '.js' for p in files) and any(p.suffix == '.css' for p in files)
    for path in files:
        assert path.suffix.lower() in dash.STATIC_TYPES, f'add {path.suffix} to STATIC_TYPES'
        resp = client.get('/static/' + path.relative_to(static).as_posix())
        assert resp.status_code == 200
        assert resp.headers['Content-Type'].split(';')[0] == dash.STATIC_TYPES[path.suffix.lower()], path.name
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'
        resp.close()


def test_page_loads_its_script_and_stylesheet_from_static(make_client):
    client, _ = make_client()
    page = client.get('/').get_data(as_text=True)
    assert '/static/js/dashboard.js?v=' in page and '/static/css/style.css?v=' in page
    assert client.get('/static/js/dashboard.js').headers['Content-Type'].startswith('text/javascript')


def test_pages_use_versioned_static_urls_so_cached_copies_are_not_reused(make_client, monkeypatch):
    import re
    client, _ = make_client()
    page = client.get('/').get_data(as_text=True)
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert len(urls) == 2
    for url in urls:
        # A URL no browser has cached: a copy saved with a wrong Content-Type
        # (kept through "304 Not Modified") is never used again
        assert re.search(rf'\?v={dash.STATIC_HEADERS_VERSION}-[0-9a-f]+-[0-9a-f]+$', url), url
        resp = client.get(url)
        assert resp.status_code == 200
        resp.close()

    with dash.app.test_request_context():
        before = dash.static_url('js/dashboard.js')
        monkeypatch.setattr(dash, 'STATIC_HEADERS_VERSION', dash.STATIC_HEADERS_VERSION + 1)
        assert dash.static_url('js/dashboard.js') != before
        assert dash.static_url('no/such/file.js').endswith(f'?v={dash.STATIC_HEADERS_VERSION}')


def test_client_addresses_get_names():
    lookups = []

    def by_addr(ip):
        lookups.append(ip)
        return {'10.0.0.7': 'scope3.lab.example', '10.0.0.8': '10.0.0.8',
                '10.0.0.9': 'host.docker.internal'}[ip]        # KeyError: no name

    names = dash._ClientNames(addr_lookup=by_addr, name_lookup={'triton5': '10.0.0.5'}.__getitem__)
    wanted = ['10.0.0.5', '10.0.0.7', '10.0.0.8', '10.0.0.9', '10.0.0.10']
    assert names.names(wanted, ['triton5', 'gone', 'not a host name']) == {}     # nothing known yet: no waiting
    names.wait()
    names.names(wanted, ['triton5', 'gone'])        # triton5's address is known now; the others get looked up
    names.wait()
    assert names.names(wanted, ['triton5', 'gone']) == {'10.0.0.5': 'triton5', '10.0.0.7': 'scope3'}
    count = len(lookups)
    names.names(wanted, ['triton5'])
    names.wait()
    assert len(lookups) == count                    # answers and failures are both remembered


def test_connected_rows_carry_the_client_name(monkeypatch):
    monkeypatch.setattr(dash, '_config', {})
    monkeypatch.setattr(dash, '_client_names', type('Fixed', (), {
        'names': staticmethod(lambda addresses, systems: {'10.0.0.5': 'triton5'})})())
    systems = {'kraken': {'sessions': {'sessions': [
        {'client': '10.0.0.5', 'service': 'smb'}, {'client': '10.0.0.6', 'service': 'smb'},
        {'client': '127.0.0.1', 'service': 'ssh'}, {'username': 'ann', 'service': 'console'}]}}}
    dash._name_clients(systems)
    rows = systems['kraken']['sessions']['sessions']
    assert [r.get('client_name') for r in rows] == ['triton5', None, None, None]

    monkeypatch.setattr(dash, '_config', {'client_names': False})
    rows[0].pop('client_name')
    dash._name_clients(systems)
    assert 'client_name' not in rows[0]


def test_page_has_the_snapshot_tiles_and_the_detail_area(make_client):
    client, _ = make_client()
    page = client.get('/').get_data(as_text=True)
    for element_id in ('systemsReporting', 'connectedNow', 'totalStorage', 'freeSpace',
                       'lifetimeTransferIn', 'lifetimeTransferOut',
                       'systemSummaryStrip', 'detailHint', 'nasGrid', 'chartCallout'):
        assert f'id="{element_id}"' in page, element_id
