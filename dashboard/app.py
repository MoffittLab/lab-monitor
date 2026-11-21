#!/usr/bin/env python3
"""
Lab Monitor Dashboard

Web interface for viewing system usage statistics.
Polls Manager API and displays real-time data.

Usage:
    python3 app.py --config config.json
    python3 app.py --hash-password          # print a password hash for config "auth"
"""
import os
import re
import sys
import json
import math
import hmac
import time
import getpass
import hashlib
import logging
import argparse
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, Response, render_template, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash


def write_pid_file():
    """Write PID file to .pids/dashboard.pid in the repo root."""
    try:
        # Repo root is two levels up from this script (dashboard/app.py -> lab-monitor/)
        repo_root = Path(__file__).parent.parent
        pids_dir = repo_root / '.pids'
        pids_dir.mkdir(exist_ok=True)

        pid_file = pids_dir / 'dashboard.pid'
        pid_file.write_text(str(os.getpid()))
        return str(pid_file)
    except Exception as e:
        print(f"Warning: Failed to write PID file: {e}")
        return None


# ---------------------------------------------------------------------------
# Logging / config
# ---------------------------------------------------------------------------

def setup_logging(log_file: str = None, log_level: str = "INFO"):
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, log_level))
    logger.propagate = False   # waitress configures the root logger; avoid duplicate lines
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def load_config(config_path: str) -> dict:
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        raise


# Substrings that mark a token as an unedited example value
PLACEHOLDER_MARKERS = ('CHANGE-ME', 'YOUR-', 'GENERATE-RANDOM', 'SAME-AS-', 'PASTE-')


def validate_config(config: dict) -> list:
    """
    Check the config for problems.  Raises ValueError for settings the
    dashboard must not start with; returns a list of warning strings for
    settings that work but are probably wrong.
    """
    warnings = []

    auth = config.get('auth')
    if auth is not None:
        if not isinstance(auth, dict) or not auth.get('username') or not auth.get('password_hash'):
            raise ValueError(
                "config 'auth' must be {\"username\": ..., \"password_hash\": ...} "
                "(generate the hash with: python app.py --hash-password), or be removed")
        # werkzeug hashes look like "<method>$<salt>$<hash>"
        if str(auth['password_hash']).count('$') < 2:
            raise ValueError(
                "config 'auth.password_hash' is not a password hash; "
                "generate one with: python app.py --hash-password")

    token = str(config.get('manager_token') or '')
    if not token:
        warnings.append("manager_token is empty - Manager requests will be rejected (HTTP 401)")
    elif any(m in token.upper() for m in PLACEHOLDER_MARKERS):
        warnings.append("manager_token looks like a placeholder - set it to a Manager read token")

    host = str(config.get('host', '0.0.0.0'))
    if auth is None and host not in ('127.0.0.1', 'localhost', '::1'):
        warnings.append(
            f"Dashboard is bound to {host} without authentication; anyone who can reach "
            f"port {config.get('port', 5001)} can view it. Configure 'auth' "
            f"(python app.py --hash-password) or bind to 127.0.0.1 behind a proxy.")
    return warnings


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

_config = {}
_logger = logging.getLogger(__name__)

# Same pattern the Manager uses for device names (contract 7)
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
# ISO-8601 date or date-time, as sent by the chart range controls
ISO_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?)?(Z|[+-]\d{2}:?\d{2})?$')
MAX_FIELD_LEN = 512

# Manager endpoints polled for every /api/data refresh.  The device registry
# is the core call: without it the dashboard has no authoritative system list.
CORE_ENDPOINT  = '/api/devices'
POLL_ENDPOINTS = (CORE_ENDPOINT, '/api/metrics/all', '/api/usage/all',
                  '/api/totals', '/api/user_metrics/all')

ManagerResult = namedtuple('ManagerResult', 'data error status')

# requests.Session for connection reuse.  Shared by the poll threads: only
# get() is used and no cookies are set, which is safe in practice; the
# underlying urllib3 pool is thread-safe.
_session = None
_session_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=len(POLL_ENDPOINTS), thread_name_prefix='manager-poll')

# Monotonic clock used by the /api/data cache (patched in tests)
_monotonic = time.monotonic


class _TTLCache:
    """
    Single-value cache for the aggregated /api/data result.  The lock is held
    while computing, so concurrent requests (several browsers, waitress
    threads) share one Manager round-trip instead of each starting their own.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._value   = None
        self._expires = 0.0

    def clear(self):
        with self._lock:
            self._value, self._expires = None, 0.0

    def get_or_compute(self, compute, ttl_for):
        with self._lock:
            if self._value is not None and _monotonic() < self._expires:
                return self._value
            value = compute()
            self._value   = value
            self._expires = _monotonic() + ttl_for(value)
            return value


_data_cache = _TTLCache()

# Successful basic-auth credentials, stored as keyed digests so that each
# request doesn't re-run the (deliberately slow) password hash check.
_auth_lock     = threading.Lock()
_auth_verified = set()
_auth_cache_key = os.urandom(32)
_AUTH_CACHE_MAX = 32


def init_app(config: dict, logger: logging.Logger):
    global _config, _logger, _session
    for warning in validate_config(config):
        logger.warning(warning)
    _config = config
    _logger = logger
    with _session_lock:
        _session = None
    _data_cache.clear()
    with _auth_lock:
        _auth_verified.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _num_config(key: str, default, minimum=None, maximum=None):
    """Numeric config value with a fallback for missing/invalid values."""
    try:
        v = float(_config.get(key, default))
        if not math.isfinite(v):
            raise ValueError
    except (TypeError, ValueError):
        v = float(default)
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return v


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def format_bytes(bytes_value) -> str:
    """Convert bytes to a human-readable string (binary units, '—' if unknown)."""
    v = _to_float(bytes_value)
    if v is None:
        return '—'
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if abs(v) < 1024:
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} PiB"


def _to_float(val):
    """float(val), or None for null / NaN / inf / non-numeric / bool."""
    if val is None or isinstance(val, bool):
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _to_int(val):
    v = _to_float(val)
    return int(v) if v is not None else None


def _json_safe(obj):
    """
    Replace NaN/inf floats with None, recursively.  Python's json emits them as
    bare NaN/Infinity, which browsers' JSON.parse rejects - one bad value would
    otherwise break the whole dashboard (contract 4: unmeasured is null).
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _error(message: str, status: int):
    """Generic JSON error body - never echoes exception text."""
    return jsonify({"success": False, "error": message}), status


def _valid_name(name: str) -> bool:
    """System name / data type check: NAME_RE, and not just dots ('.', '..')."""
    return bool(NAME_RE.match(name)) and name.strip('.') != ''


def _manager_headers() -> dict:
    token = _config.get("manager_token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            adapter = HTTPAdapter(pool_connections=2, pool_maxsize=len(POLL_ENDPOINTS) * 2)
            s.mount('http://', adapter)
            s.mount('https://', adapter)
            _session = s
        return _session


def _manager_fetch(path: str, params: dict = None) -> ManagerResult:
    """
    GET from manager.  Returns ManagerResult(data, error, status): data is the
    parsed JSON on success; otherwise error is a short, client-safe reason.
    """
    url     = _config.get("manager_url", "http://localhost:5000").rstrip('/') + path
    timeout = _num_config("manager_timeout_seconds", 5, minimum=0.5)
    try:
        r = _get_session().get(url, headers=_manager_headers(), params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        _logger.warning(f"Timeout fetching {path}")
        return ManagerResult(None, 'timeout', None)
    except requests.exceptions.ConnectionError as e:
        _logger.warning(f"Connection error fetching {path}: {e}")
        return ManagerResult(None, 'connection failed', None)
    except requests.exceptions.RequestException as e:
        _logger.error(f"Error fetching {path}: {e}")
        return ManagerResult(None, 'request failed', None)

    if r.status_code != 200:
        _logger.warning(f"Manager {path} returned {r.status_code}")
        reason = {
            401: 'HTTP 401 (Manager rejected manager_token)',
            403: 'HTTP 403 (manager_token is not allowed to read)',
        }.get(r.status_code, f'HTTP {r.status_code}')
        return ManagerResult(None, reason, r.status_code)
    try:
        return ManagerResult(r.json(), None, 200)
    except ValueError:
        _logger.error(f"Manager {path} returned invalid JSON")
        return ManagerResult(None, 'invalid JSON', 200)


def parse_folder_usage(raw: dict) -> dict:
    """
    Convert the flat folder_usage summary dict (from system_latest) into a
    structured form the frontend can consume.

    Input keys fall into five categories:
      - Metadata: name, system_id, device_type, data_type, timestamp, and
        every key starting with "_" (_unit, _timed_out, _errors, ...)
      - Leaf folders:  /volume1/JeffMoffitt  (depth > 1 after splitting on /)
      - Volume roots:  /volume1              (depth == 1)
      - Capacity:      /volume1_total_bytes, /volume1_free_bytes
      - Grand total:   total_usage

    Null / NaN / non-numeric values mean "not measured" and are skipped
    (contract 4) - they are never reported as 0.

    Returns:
        {
            folders:              [{path, usage_bytes, usage_formatted}, ...]
            volumes:              [{path, usage_bytes, usage_formatted, total_*, free_*}, ...]
            total_usage_bytes:    int or None
            total_usage_formatted: str
            timed_out:            [folder paths that hit the scan timeout]
            errors:               [folder paths that failed to scan]
            scan_seconds:         float or None
        }
    """
    METADATA = {'name', 'system_id', 'device_type', 'data_type', 'timestamp', 'total_disk_bytes'}
    if not isinstance(raw, dict):
        raw = {}
    unit_default = raw.get('_unit')  # message-level unit default for all numeric fields

    folders  = {}
    volumes  = {}
    capacity = {}   # vol_path -> {'total_bytes': int, 'free_bytes': int}
    total    = None

    for key, val in raw.items():
        # "_"-prefixed keys are metadata, never folders (contract 5)
        if not isinstance(key, str) or key in METADATA or key.startswith('_'):
            continue
        if key == 'total_usage':
            total = _to_int(val)
            continue

        # Capacity keys: /volume1_total_bytes, /volume1_free_bytes
        # Must be checked before generic depth classification
        for suffix, cap_key in (('_total_bytes', 'total_bytes'), ('_free_bytes', 'free_bytes')):
            if key.endswith(suffix):
                v = _to_int(val)
                if v is not None:
                    capacity.setdefault(key[:-len(suffix)], {})[cap_key] = v
                break
        else:
            # Not a capacity key — must be numeric and not null/NaN
            v = _to_int(val)
            if v is None:
                continue

            # Classify: split on forward slash (normalise backslash too)
            parts = [p for p in key.replace('\\', '/').split('/') if p]
            if len(parts) <= 1:
                volumes[key] = v   # e.g. /volume1  or  E:
            else:
                folders[key] = v   # e.g. /volume1/JeffMoffitt

    # Build volume list, attaching capacity data where available
    vol_list = []
    for k, v in sorted(volumes.items()):
        cap   = capacity.get(k, {})
        entry = {
            'path':            k,
            'usage_bytes':     v,
            'usage_formatted': format_bytes(v),
        }
        if 'total_bytes' in cap:
            entry['total_bytes']     = cap['total_bytes']
            entry['total_formatted'] = format_bytes(cap['total_bytes'])
        if 'free_bytes' in cap:
            entry['free_bytes']     = cap['free_bytes']
            entry['free_formatted'] = format_bytes(cap['free_bytes'])
        vol_list.append(entry)

    def _path_list(val) -> list:
        if not isinstance(val, list):
            return []
        return [str(p) for p in val if p is not None]

    return {
        'folders': [
            {'path': k, 'usage_bytes': v, 'usage_formatted': format_bytes(v)}
            for k, v in sorted(folders.items())
        ],
        'volumes':               vol_list,
        'total_usage_bytes':     total,
        'total_usage_formatted': format_bytes(total),
        'units':                 {'_unit': unit_default} if unit_default else {},
        'timed_out':             _path_list(raw.get('_timed_out')),
        'errors':                _path_list(raw.get('_errors')),
        'scan_seconds':          _to_float(raw.get('_scan_seconds')),
    }


def fetch_manager_state() -> dict:
    """Fetch every POLL_ENDPOINTS path concurrently. Returns {path: ManagerResult}."""
    futures = {path: _executor.submit(_manager_fetch, path) for path in POLL_ENDPOINTS}
    results = {}
    for path, future in futures.items():
        try:
            results[path] = future.result()
        except Exception as e:   # _manager_fetch handles request errors; this is a bug guard
            _logger.error(f"Unexpected error fetching {path}: {e}", exc_info=True)
            results[path] = ManagerResult(None, 'internal error', None)
    return results


def _blank_system(name: str, device_type='unknown') -> dict:
    return {
        'name': name, 'system_id': None, 'device_type': device_type or 'unknown',
        'first_seen': None, 'last_seen': None,
        'metrics': None, 'disk': None, 'totals': None,
    }


def get_all_systems_data(results: dict = None) -> tuple:
    """
    Merge the Manager responses (system_metrics, folder_usage, totals, users)
    into a single dict keyed by system name.

    Returns:
        (systems, global_totals, errors) where systems is
        {
            "Triton": {
                "name":        "Triton",
                "device_type": "synology",
                "metrics":     { cpu_percent, ram_percent, ... } or None,
                "disk":        { folders, volumes, total_usage_bytes, ... } or None,
            },
            ...
        }
        and errors is [{"endpoint": path, "error": reason}, ...] for every
        Manager call that failed or returned an unexpected shape.
    """
    if results is None:
        results = fetch_manager_state()

    errors: list = []

    def _payload(path: str, expected_type=dict):
        res = results.get(path) or ManagerResult(None, 'not fetched', None)
        if res.error:
            errors.append({'endpoint': path, 'error': res.error})
            return expected_type()
        if not isinstance(res.data, expected_type):
            _logger.error(f"Manager {path} returned unexpected shape: {type(res.data).__name__}")
            errors.append({'endpoint': path, 'error': 'unexpected response'})
            return expected_type()
        return res.data

    systems: dict = {}

    # --- device registry (authoritative system list) ---
    devices_resp = _payload('/api/devices')
    devices = devices_resp.get('devices', [])
    if not isinstance(devices, list):
        errors.append({'endpoint': '/api/devices', 'error': 'unexpected response'})
        devices = []
    for dev in devices:
        if not isinstance(dev, dict) or not dev.get('name'):
            continue
        name = str(dev['name'])
        systems[name] = _blank_system(name, dev.get('device_type'))
        systems[name].update({
            'system_id':  dev.get('system_id'),
            'first_seen': dev.get('first_seen'),
            'last_seen':  dev.get('last_seen'),
        })

    # --- metrics snapshots ---
    # Unmeasured values stay None (contract 4); the frontend renders them as "—".
    for name, data in _payload('/api/metrics/all').items():
        if not isinstance(data, dict):
            continue
        systems.setdefault(name, _blank_system(name, data.get('device_type')))
        units = data.get('units')
        gpus  = data.get('gpus')
        systems[name]['metrics'] = {
            'timestamp':                    data.get('timestamp'),
            'cpu_percent':                  data.get('cpu_percent'),
            'ram_percent':                  data.get('ram_percent'),
            'total_ram_bytes':              data.get('total_ram_bytes'),
            'uptime_seconds':               data.get('uptime_seconds'),
            'uptime_formatted':             data.get('uptime_formatted'),
            'network_bytes_in':             data.get('network_bytes_in'),
            'network_bytes_out':            data.get('network_bytes_out'),
            'network_bandwidth_in_mbps':    data.get('network_bandwidth_in_mbps'),
            'network_bandwidth_out_mbps':   data.get('network_bandwidth_out_mbps'),
            'units':                        units if isinstance(units, dict) else {},
            'gpus':                         [g for g in gpus if isinstance(g, dict)] if isinstance(gpus, list) else [],
        }

    # --- disk snapshots ---
    for name, data in _payload('/api/usage/all').items():
        if not isinstance(data, dict):
            continue
        systems.setdefault(name, _blank_system(name, data.get('device_type')))
        parsed = parse_folder_usage(data)
        parsed['timestamp'] = data.get('timestamp')
        systems[name]['disk'] = parsed

    # --- per-device running totals ---
    totals_resp = _payload('/api/totals')
    device_totals = totals_resp.get('devices', {})
    for name, t in (device_totals.items() if isinstance(device_totals, dict) else ()):
        if name in systems and isinstance(t, dict):
            systems[name]['totals'] = {
                'total_bytes_in':   t.get('total_bytes_in'),
                'total_bytes_out':  t.get('total_bytes_out'),
                'total_disk_bytes': t.get('total_disk_bytes'),
            }

    # --- per-user metrics snapshot ---
    for name, users in _payload('/api/user_metrics/all').items():
        if name in systems and isinstance(users, list):
            systems[name]['users'] = [u for u in users if isinstance(u, dict)]

    # Surface most-recent timestamp per system
    for sys_ in systems.values():
        ts = (sys_.get('metrics') or {}).get('timestamp') \
          or (sys_.get('disk')    or {}).get('timestamp')
        sys_['timestamp'] = ts

    global_totals = totals_resp.get('global', {})
    if not isinstance(global_totals, dict):
        global_totals = {}
    return systems, global_totals, errors


def build_data_payload() -> tuple:
    """
    One full poll of the Manager.  Returns (payload, http_status):
      - 502 + success:false when the core call (device registry) failed
      - 200 + degraded:true when only some of the other calls failed
    """
    systems, global_totals, errors = get_all_systems_data(fetch_manager_state())
    now = _utc_now_iso()

    if any(e['endpoint'] == CORE_ENDPOINT for e in errors):
        return {
            "success":   False,
            "error":     "Manager unavailable",
            "errors":    errors,
            "timestamp": now,
        }, 502

    return _json_safe({
        "success":       True,
        "timestamp":     now,
        "degraded":      bool(errors),
        "errors":        errors,
        "systems":       systems,
        "global_totals": global_totals,
    }), 200


def downsample(points: list, max_points: int) -> list:
    """
    Bucket-average a time-ordered [{timestamp, value}] list down to at most
    max_points entries.  Fallback for Managers that ignore ?max_points.
    Each bucket keeps its middle timestamp and the mean of its non-null values.
    """
    n = len(points)
    if max_points <= 0 or n <= max_points:
        return points
    bucket = math.ceil(n / max_points)
    out = []
    for start in range(0, n, bucket):
        chunk  = points[start:start + bucket]
        values = [v for v in (_to_float(p.get('value')) for p in chunk) if v is not None]
        out.append({
            'timestamp': chunk[len(chunk) // 2].get('timestamp'),
            'value':     (sum(values) / len(values)) if values else None,
        })
    return out


# ---------------------------------------------------------------------------
# Auth (optional HTTP basic auth for the whole dashboard)
# ---------------------------------------------------------------------------

def _check_credentials(username: str, password: str) -> bool:
    auth_cfg = _config['auth']
    key = hmac.new(_auth_cache_key,
                   f"{username}\0{password}".encode('utf-8', 'replace'),
                   hashlib.sha256).digest()
    with _auth_lock:
        if key in _auth_verified:
            return True

    # Always run the hash check so a wrong username costs the same time
    user_ok = hmac.compare_digest(username.encode('utf-8', 'replace'),
                                  str(auth_cfg['username']).encode('utf-8', 'replace'))
    try:
        pass_ok = check_password_hash(str(auth_cfg['password_hash']), password)
    except (ValueError, TypeError):
        _logger.error("auth.password_hash in config is not a valid werkzeug hash")
        pass_ok = False

    if user_ok and pass_ok:
        with _auth_lock:
            if len(_auth_verified) >= _AUTH_CACHE_MAX:
                _auth_verified.clear()
            _auth_verified.add(key)
        return True
    return False


# Served without basic auth: /health carries no system data and lets
# tools/restart_manager_dashboard.py (and load balancers) see a 200.
AUTH_EXEMPT_PATHS = frozenset({'/health'})


@app.before_request
def _require_basic_auth():
    if not _config.get('auth'):
        return None   # auth not configured - open dashboard (legacy behaviour)
    if request.path in AUTH_EXEMPT_PATHS:
        return None
    creds = request.authorization
    if creds is not None and (creds.type or '').lower() == 'basic' \
            and _check_credentials(creds.username or '', creds.password or ''):
        return None
    return Response('Authentication required\n', 401,
                    {'WWW-Authenticate': 'Basic realm="Lab Monitor Dashboard", charset="UTF-8"'})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _refresh_seconds() -> float:
    return _num_config("refresh_interval_seconds", 30, minimum=5)


def _data_timeout_seconds() -> float:
    """
    How long the browser waits for /api/data before calling the dashboard
    server unresponsive.  Worst legitimate case: waiting behind another poll
    in progress (cache lock), then running our own; each poll can take a
    connect timeout plus a read timeout.
    """
    return max(15.0, 4 * _num_config("manager_timeout_seconds", 5, minimum=0.5) + 5)


def _client_config() -> dict:
    """Settings the frontend needs (rendered into index.html as JSON)."""
    return {
        'refreshInterval': int(_refresh_seconds() * 1000),
        'dataTimeoutMs':   int(_data_timeout_seconds() * 1000),
        'staleMinutes':    _num_config('stale_minutes', 15, minimum=1),
        'offlineMinutes':  _num_config('offline_minutes', 120, minimum=1),
        'diskStaleHours':  _num_config('disk_stale_hours', 36, minimum=1),
        'maxChartPoints':  int(_num_config('chart_max_points', 2000, minimum=10, maximum=10000)),
    }


@app.route('/')
def index():
    return render_template('index.html', client_config=_client_config())


@app.route('/health')
def health():
    """Liveness probe: no auth, no Manager call, no system data."""
    return jsonify({"status": "ok"})


@app.route('/api/data')
def get_data():
    """Main data endpoint polled by the frontend JavaScript (cached for one refresh interval)."""
    ttl = _num_config("cache_ttl_seconds", _refresh_seconds(), minimum=0)

    def _ttl_for(value):
        # Failures (and degraded results) are cached briefly so a down Manager
        # isn't hammered, but recovery shows up quickly.
        payload, status = value
        return ttl if status == 200 and not payload.get('degraded') else min(ttl, 5)

    try:
        payload, status = _data_cache.get_or_compute(build_data_payload, _ttl_for)
    except Exception as e:
        _logger.error(f"Error building /api/data: {e}", exc_info=True)
        return _error('Internal error', 500)
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/history/<system_name>/<data_type>/<path:field>')
def get_metric_history(system_name, data_type, field):
    """Proxy metric time-series data from Manager for frontend charting."""
    if not _valid_name(system_name):
        return _error('Invalid system name', 400)
    if not _valid_name(data_type):
        return _error('Invalid data type', 400)
    # "." / ".." segments would be collapsed by URL normalisation and could
    # reach a different Manager endpoint
    if not field or len(field) > MAX_FIELD_LEN \
            or any(seg in ('.', '..') for seg in field.replace('\\', '/').split('/')):
        return _error('Invalid field', 400)

    from_param = request.args.get('from') or None
    to_param   = request.args.get('to')   or None
    for ts in (from_param, to_param):
        if ts is not None and not ISO_TS_RE.match(ts):
            return _error('Invalid time range', 400)

    # Bounded response size: the Manager bucket-averages to max_points
    # ("downsampled") and caps raw rows at limit ("truncated").  Older
    # Managers ignore max_points (and limit when from/to is given) -
    # downsample() below covers that case.
    max_points = request.args.get('max_points', type=int) \
        or int(_num_config('chart_max_points', 2000))
    max_points = max(10, min(max_points, 10000))
    if from_param:
        limit = int(_num_config('history_max_rows', 100000, minimum=1))
    else:
        limit = request.args.get('limit', type=int) or 500
        limit = max(1, min(limit, 10000))

    params = {'max_points': max_points, 'limit': limit}
    if from_param:
        params['from'] = from_param
    if to_param:
        params['to'] = to_param

    try:
        # URL-encode every path segment so that names, data types and field
        # paths like /volume1 or E: survive the round-trip to the manager's
        # routes without breaking URL structure.
        res = _manager_fetch(
            f"/api/history/{quote(system_name, safe='')}/{quote(data_type, safe='')}/{quote(field, safe='')}",
            params=params,
        )
        if res.status == 404:
            return _error('Not found', 404)
        if res.status == 400:
            return _error('Bad request', 400)
        if res.error or not isinstance(res.data, dict):
            return _error('Manager unavailable', 502)

        data = res.data
        points = data.get('data')
        if isinstance(points, list) and len(points) > max_points:
            data['data'] = downsample(points, max_points)
            data['downsampled'] = True
            data['downsampled_from'] = len(points)
        return jsonify(_json_safe(data)), 200

    except Exception as e:
        _logger.error(f"Error fetching metric history: {e}", exc_info=True)
        return _error('Internal error', 500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def hash_password_cli() -> int:
    """Prompt for a password and print the config 'auth' block."""
    password = getpass.getpass("Dashboard password: ")
    if len(password) < 8:
        print("ERROR: use at least 8 characters", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != password:
        print("ERROR: passwords do not match", file=sys.stderr)
        return 1
    print("\nAdd this to dashboard config.json (choose your own username):\n")
    print(json.dumps({"auth": {"username": "labadmin",
                               "password_hash": generate_password_hash(password)}}, indent=2))
    return 0


def serve(host: str, port: int, threads: int, debug: bool, logger: logging.Logger):
    """Run under waitress when installed; fall back to the Flask dev server."""
    if not debug:
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            waitress_serve = None
        if waitress_serve is not None:
            logger.info(f"Serving with waitress ({threads} threads)")
            waitress_serve(app, host=host, port=port, threads=threads)
            return
    logger.warning("!" * 60)
    logger.warning("waitress is not installed (or debug=true): using the Flask DEVELOPMENT")
    logger.warning("server, which is not meant for production. pip install waitress")
    logger.warning("!" * 60)
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


def main():
    parser = argparse.ArgumentParser(description="Lab Monitor Dashboard")
    parser.add_argument("--config", default="config.json", help="Config file")
    parser.add_argument("--hash-password", action="store_true",
                        help="Prompt for a password and print a hash for config 'auth'")
    args = parser.parse_args()

    if args.hash_password:
        sys.exit(hash_password_cli())

    config = load_config(args.config)
    logger = setup_logging(config.get("log_file"), config.get("log_level", "INFO"))
    try:
        init_app(config, logger)
    except ValueError as e:
        logger.error(f"Invalid config: {e}")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Lab Monitor Dashboard starting")
    logger.info(f"Manager URL: {config.get('manager_url', 'http://localhost:5000')}")
    logger.info(f"Basic auth: {'enabled' if config.get('auth') else 'DISABLED'}")
    logger.info("=" * 60)

    # Write PID file for process management
    pid_file = write_pid_file()
    if pid_file:
        logger.info(f"PID: {os.getpid()} (written to {pid_file})")

    host    = config.get("host", "0.0.0.0")
    port    = config.get("port", 5001)
    debug   = config.get("debug", False)
    threads = int(config.get("threads", 8))
    logger.info(f"Listening on {host}:{port}")
    serve(host, port, threads, debug, logger)


if __name__ == "__main__":
    main()
