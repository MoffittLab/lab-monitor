"""
Dashboard server tests: Manager outage / partial failure handling, /api/data
cache, optional sign-in, folder_usage parsing, history proxy validation and
max_points pass-through.  The Manager is mocked by patching
requests.Session.get, so no network is used.
"""
import base64
import importlib.util
import json
import logging
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

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
        return base[path]

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
    assert '/static/js/dashboard.js' in page and '/static/css/style.css' in page
    assert client.get('/static/js/dashboard.js').headers['Content-Type'].startswith('text/javascript')
