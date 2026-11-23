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
import base64
import getpass
import hashlib
import logging
import secrets
import socket
import ipaddress
import mimetypes
import argparse
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, g, render_template, jsonify, redirect, request, url_for
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
        if not isinstance(auth, dict) or not auth.get('password_hash'):
            raise ValueError(
                "config 'auth' must be {\"password_hash\": ...} "
                "(generate the hash with: python app.py --hash-password), or be removed")
        # werkzeug hashes look like "<method>$<salt>$<hash>"
        if str(auth['password_hash']).count('$') < 2:
            raise ValueError(
                "config 'auth.password_hash' is not a password hash; "
                "generate one with: python app.py --hash-password")
        days = auth.get('remember_days', DEFAULT_REMEMBER_DAYS)
        if isinstance(days, bool) or not isinstance(days, (int, float)) \
                or not math.isfinite(days) or not (1 <= days <= MAX_REMEMBER_DAYS):
            raise ValueError(
                f"config 'auth.remember_days' must be a number from 1 to {MAX_REMEMBER_DAYS} "
                f"(default {DEFAULT_REMEMBER_DAYS})")
        if auth.get('username'):
            warnings.append("auth.username is no longer used: sign-in is by password only "
                            "(remove it from the config)")

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


# Sign-in (config "auth"): one shared password, then a signed cookie that keeps
# the browser signed in.  See the "Sign-in" section below.
DEFAULT_REMEMBER_DAYS = 180
MAX_REMEMBER_DAYS     = 3650
SESSION_COOKIE        = 'lab_monitor_session'
# Default place of the key that signs the cookies (config auth.session_key_file
# overrides it).  Created on first start; treat it like config.json.
SESSION_KEY_FILE      = Path(__file__).resolve().parent / '.session_key'


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

# Content types of the static files, set here instead of trusting the system:
# on Windows mimetypes reads the registry, where .js is often "text/plain".
# Every response carries X-Content-Type-Options: nosniff, so a browser refuses
# to run a script (or apply a stylesheet) served with the wrong type, and the
# page stays empty.
STATIC_TYPES = {
    '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
    '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
}


def register_static_types():
    for ext, content_type in STATIC_TYPES.items():
        mimetypes.add_type(content_type, ext)


register_static_types()

app = Flask(__name__)

# Part of every static URL the pages use (see static_url).  Bump it when a
# response HEADER of the static files changes: a browser that revalidates its
# cached copy gets "304 Not Modified" and keeps the headers it cached, so a
# corrected Content-Type would otherwise never reach it.
STATIC_HEADERS_VERSION = 2


@app.template_global()
def static_url(filename: str) -> str:
    """
    URL of a static file with a version that changes when the file (or
    STATIC_HEADERS_VERSION) does, so browsers fetch it afresh instead of
    reusing a cached copy.
    """
    try:
        st = os.stat(os.path.join(app.static_folder, filename))
        version = f"{STATIC_HEADERS_VERSION}-{st.st_mtime_ns:x}-{st.st_size:x}"
    except OSError:
        version = str(STATIC_HEADERS_VERSION)
    return url_for('static', filename=filename, v=version)

_config = {}
_logger = logging.getLogger(__name__)

# Same pattern the Manager uses for device names (contract 7)
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
# ISO-8601 date or date-time, as sent by the chart range controls
ISO_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?)?(Z|[+-]\d{2}:?\d{2})?$')
MAX_FIELD_LEN = 512
# storage_breakdown history fields embed a folder path ("folder/<path>/bytes"),
# and paths may be up to 4096 characters (R10)
MAX_BREAKDOWN_FIELD_LEN = 4400
MAX_FOLDER_LEN = 4096
MAX_ENTITY_LEN = 256
HISTORY_RESOLUTIONS = frozenset({'auto', 'raw', 'hour', 'day'})

# Manager endpoints polled for every /api/data refresh.  The device registry
# is the core call: without it the dashboard has no authoritative system list.
CORE_ENDPOINT      = '/api/devices'
SESSIONS_ENDPOINT  = '/api/sessions/all'
HEALTH_ENDPOINT    = '/api/storage/health/all'
BREAKDOWN_ENDPOINT = '/api/storage/breakdown/all'
USAGE_ENDPOINT     = '/api/user_metrics/usage'     # the Manager defaults to window=24h
POLL_ENDPOINTS = (CORE_ENDPOINT, '/api/metrics/all', '/api/usage/all',
                  '/api/totals', '/api/user_metrics/all',
                  SESSIONS_ENDPOINT, HEALTH_ENDPOINT, BREAKDOWN_ENDPOINT, USAGE_ENDPOINT)

# Endpoints an older Manager doesn't have (R7): its 404 means "feature not
# available", which is neither an error nor a degraded poll.  Value: the key
# in the /api/data "features" dict.
OPTIONAL_ENDPOINTS = {
    SESSIONS_ENDPOINT:  'sessions',
    HEALTH_ENDPOINT:    'storage_health',
    BREAKDOWN_ENDPOINT: 'breakdown',
    USAGE_ENDPOINT:     'usage',
}

# Slow-changing data is fetched at most once per TTL (seconds), whatever the
# refresh interval.  Sessions change every run and are fetched every poll.
ENDPOINT_TTL = {
    BREAKDOWN_ENDPOINT: 600,
    USAGE_ENDPOINT:     300,
    HEALTH_ENDPOINT:    60,
}

# A user row is hidden when it is this much older than the system's latest
# system_metrics: the Manager keeps the last row of users who logged off.
STALE_USER_SECONDS = 120

ManagerResult = namedtuple('ManagerResult', 'data error status')

# Device names from the latest successful /api/devices poll (see
# _valid_system_name); replaced as a whole, never mutated.
_registered_names = frozenset()

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


class _EndpointCache:
    """
    Last answer of each ENDPOINT_TTL path, kept apart from the /api/data
    cache.  Only answers are stored (parsed JSON, or a 404 = feature absent);
    a failed fetch is never stored, so it can't replace a good value - the
    failure is reported for that poll and the next poll fetches again.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._entries = {}    # path -> (ManagerResult, expires)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def get(self, path: str):
        with self._lock:
            entry = self._entries.get(path)
            return entry[0] if entry is not None and _monotonic() < entry[1] else None

    def put(self, path: str, result: ManagerResult, ttl: float):
        with self._lock:
            self._entries[path] = (result, _monotonic() + ttl)


_endpoint_cache = _EndpointCache()

# Sign-in state.  _signing_key is derived from the key file and the password
# hash (None while auth is off); _login_failures counts wrong passwords per
# client address.  Wall clock for cookie ages (patched in tests).
_signing_key    = None
_login_lock     = threading.Lock()
_login_failures = {}
_now = time.time


def init_app(config: dict, logger: logging.Logger):
    global _config, _logger, _session, _registered_names, _signing_key
    for warning in validate_config(config):
        logger.warning(warning)
    _config = config
    _logger = logger
    with _session_lock:
        _session = None
    _data_cache.clear()
    _endpoint_cache.clear()
    with _login_lock:
        _login_failures.clear()
    _signing_key = _make_signing_key(config['auth'], logger) if config.get('auth') else None
    _registered_names = frozenset()


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


_TS_PARTS_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?'
                          r'(Z|[+-]\d{2}:?\d{2})?$')


def _parse_ts(ts):
    """Epoch seconds of a Manager timestamp (no zone = UTC), or None if unparseable."""
    m = _TS_PARTS_RE.match(ts.strip()) if isinstance(ts, str) else None
    if not m:
        return None
    year, month, day, hour, minute, sec, frac, zone = m.groups()
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(sec or 0),
                      tzinfo=timezone.utc)
    except ValueError:
        return None
    secs = dt.timestamp() + (float('0.' + frac) if frac else 0.0)
    if zone and zone != 'Z':
        offset = int(zone[1:3]) * 3600 + int(zone[-2:]) * 60
        secs -= offset if zone[0] == '+' else -offset
    return secs


def _clean_str(val, max_len: int):
    """val if it is a str of at most max_len characters, else None."""
    return val if isinstance(val, str) and len(val) <= max_len else None


def _str_list(val, max_len: int, max_items: int) -> list:
    """The str entries (each at most max_len characters) of a list; [] otherwise."""
    if not isinstance(val, list):
        return []
    return [v for v in val[:max_items] if isinstance(v, str) and len(v) <= max_len]


def _str_map(val, max_len: int = 64) -> dict:
    """{str: str} entries of a dict, such as {source: status}; {} otherwise."""
    if not isinstance(val, dict):
        return {}
    return {k: v for k, v in val.items()
            if isinstance(k, str) and isinstance(v, str) and len(k) <= max_len and len(v) <= max_len}


def _pick(row: dict, numbers=(), strings=None, flags=(), lists=None) -> dict:
    """
    The known keys of one nested row from a Manager response: numbers via
    _to_float (unmeasured stays null), strings only as str within their
    length limit, flags only as bool, lists as lists of short strings.
    Keys the row doesn't have stay absent; anything else is dropped.
    """
    out = {}
    for key in numbers:
        if key in row:
            out[key] = _to_float(row[key])
    for key, max_len in (strings or {}).items():
        if key in row:
            out[key] = _clean_str(row[key], max_len)
    for key in flags:
        if key in row:
            out[key] = row[key] if isinstance(row[key], bool) else None
    for key, max_items in (lists or {}).items():
        if key in row:
            out[key] = _str_list(row[key], 64, max_items)
    return out


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


def _valid_system_name(name: str) -> bool:
    """
    NAME_RE, or exactly a name the Manager's device registry listed in a
    recent poll: an older Manager registered devices under any name (e.g.
    "Lab PC") and still serves them, so their charts must keep working.
    Every segment is URL-quoted before it is forwarded.
    """
    return _valid_name(name) or (name.strip('.') != '' and name in _registered_names)


def _has_dot_segment(path: str) -> bool:
    """True if a '/' or '\\' separated path has a '.' or '..' segment."""
    return any(seg in ('.', '..') for seg in path.replace('\\', '/').split('/'))


def _valid_entity(entity: str) -> bool:
    """History ?entity= (a username): 1-256 printable characters, as the Manager checks."""
    return 1 <= len(entity) <= MAX_ENTITY_LEN and entity.isprintable()


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
        # An older Manager answers 404 to the optional endpoints on every poll (R7)
        log = _logger.debug if r.status_code == 404 and path in OPTIONAL_ENDPOINTS else _logger.warning
        log(f"Manager {path} returned {r.status_code}")
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
      - Metadata: the snapshot / history row columns (name, system_id,
        device_type, data_type, timestamp, total_disk_bytes, id, msg_id,
        first_seen, last_seen), "payload_<key>" (a payload key the Manager
        renamed because it collided with one of its columns), and every key
        starting with "_" (_unit, _timed_out, _errors, _volumes, ...)
      - Volume roots:  /volume1, E:, /, /mnt/data
      - Leaf folders:  /volume1/JeffMoffitt, /home/alice, /data
      - Capacity:      /volume1_total_bytes, /volume1_free_bytes
      - Grand total:   total_usage

    A key is a volume when the payload lists it in "_volumes" (collectors
    since the Linux one) or has capacity keys for it. Only a payload with
    no "_volumes" falls back to path depth (one component = volume), as
    older collectors' /volumeN and E: always were. Depth alone can't tell a
    Linux volume /mnt/data (depth 2) from a folder /data (depth 1).

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
    METADATA = {'name', 'system_id', 'device_type', 'data_type', 'timestamp', 'total_disk_bytes',
                'id', 'msg_id', 'first_seen', 'last_seen'}
    if not isinstance(raw, dict):
        raw = {}
    unit_default = raw.get('_unit')  # message-level unit default for all numeric fields

    folders  = {}
    volumes  = {}
    capacity = {}   # vol_path -> {'total_bytes': int, 'free_bytes': int}
    total    = None
    sizes    = {}   # folder or volume key -> bytes, classified below
    declared = raw.get('_volumes')
    declared = ({str(v) for v in declared if v is not None}
                if isinstance(declared, list) else None)

    for key, val in raw.items():
        # "_"-prefixed keys are metadata, never folders (contract 5)
        if not isinstance(key, str) or key in METADATA or key.startswith(('_', 'payload_')):
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
            sizes[key] = v

    for key, v in sizes.items():
        if key in capacity or (declared is not None and key in declared):
            volumes[key] = v
        elif declared is not None:
            folders[key] = v       # e.g. /data from scan_paths "/*"
        else:
            # Older collector: split on forward slash (normalise backslash too)
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
    """
    Fetch every POLL_ENDPOINTS path concurrently. Returns {path: ManagerResult}.
    ENDPOINT_TTL paths are answered from _endpoint_cache while their last
    answer is younger than the TTL.
    """
    results = {}
    futures = {}
    for path in POLL_ENDPOINTS:
        cached = _endpoint_cache.get(path) if path in ENDPOINT_TTL else None
        if cached is not None:
            results[path] = cached
        else:
            futures[path] = _executor.submit(_manager_fetch, path)
    for path, future in futures.items():
        try:
            res = future.result()
        except Exception as e:   # _manager_fetch handles request errors; this is a bug guard
            _logger.error(f"Unexpected error fetching {path}: {e}", exc_info=True)
            res = ManagerResult(None, 'internal error', None)
        results[path] = res
        if path in ENDPOINT_TTL and (res.error is None or res.status == 404):
            _endpoint_cache.put(path, res, ENDPOINT_TTL[path])
    return results


# ---------------------------------------------------------------------------
# Optional sections (sessions, storage health, storage owners, usage totals).
# Each cleaner keeps only the keys the frontend knows, with numbers as
# numbers-or-null and strings within the R10 limits; non-dict rows are
# dropped.  Every string still goes through escapeHtml() in the browser.
# ---------------------------------------------------------------------------

# New system_metrics keys (spec §11), copied into systems[name]['metrics']
SYSTEM_METRIC_NUMBERS = (
    'interval_s', 'cpu_count', 'cpu_iowait_percent', 'cpu_steal_percent', 'proc_cpu_coverage_percent',
    'disk_read_bytes_per_s', 'disk_write_bytes_per_s', 'disk_read_iops', 'disk_write_iops',
    'disk_await_ms', 'disk_busy_max_percent', 'gpu_sample_seconds',
)
GPU_USER_NUMBERS = ('vram_bytes', 'util_percent', 'processes')
GPU_NUMBERS      = ('users_truncated', 'unattributed_vram_bytes', 'unattributed_processes')
MAX_GPU_USERS    = 64

SESSION_SUMMARY_NUMBERS = ('_schema', '_collect_seconds', 'clients_total', 'users_total', 'rdp_disconnected')
SESSION_SUMMARY_STRINGS = {'_unit': 16, '_detail': 16, '_list_state': 16, '_list_hash': 64}
SESSION_COUNT_RE        = re.compile(r'^[a-z][a-z0-9]{0,15}_(clients|users)$')
SESSION_ROW_STRINGS     = {'service': 16, 'username': 256, 'client': 64, 'client_host': 256,
                           'since': 40, 'state': 16, 'source': 16}
MAX_SESSION_ROWS        = 5000

HEALTH_STATUSES = frozenset({'ok', 'warning', 'critical', 'unknown'})
ARRAY_NUMBERS   = ('members_total', 'members_active', 'degraded', 'sync_percent', 'sync_speed_kbps',
                   'sync_eta_seconds', 'mismatch_cnt', 'size_bytes')
ARRAY_STRINGS   = {'id': 64, 'kind': 16, 'name': 64, 'level': 16, 'role': 16, 'state': 32,
                   'status': 16, 'sync_action': 16}
DISK_NUMBERS    = ('size_bytes', 'temperature_c', 'power_on_hours', 'reallocated_sectors', 'pending_sectors',
                   'uncorrectable_sectors', 'read_errors_uncorrected', 'write_errors_uncorrected',
                   'wear_used_percent', 'remaining_life_percent', 'bad_sectors', 'io_errors')
DISK_STRINGS    = {'id': 64, 'model': 128, 'serial': 64, 'media': 16, 'bus': 16, 'status': 16,
                   'health_source': 16, 'device_state': 32}
VOLUME_STRINGS  = {'id': 64, 'fs': 32, 'health': 32, 'status': 16}

BREAKDOWN_TOTALS = ('bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')
MAX_OWNER_ROWS   = 1000
MAX_FOLDER_ROWS  = 10000

USAGE_USER_NUMBERS = ('samples', 'cpu_core_hours', 'avg_cores', 'peak_cores',
                      'gpu_busy_hours', 'gpu_held_hours', 'peak_gpu_vram_bytes')


def _dict_rows(val, max_rows: int) -> list:
    return [r for r in val[:max_rows] if isinstance(r, dict)] if isinstance(val, list) else []


def _clean_gpu(g: dict) -> dict:
    """One gpus[i] entry: passed through, with the per-user detail cleaned."""
    out = dict(g)
    out.update(_pick(g, GPU_NUMBERS))
    if 'users' in g:
        users = g['users']
        # null: no per-process data for this GPU; anything else non-list is treated the same
        out['users'] = [
            {'username': u['username'], **_pick(u, GPU_USER_NUMBERS)}
            for u in _dict_rows(users, MAX_GPU_USERS)
            if _clean_str(u.get('username'), 256) is not None
        ] if isinstance(users, list) else None
    return out


def _drop_stale_users(users: list, metrics_ts) -> list:
    """
    Drop user rows more than STALE_USER_SECONDS older than the latest
    system_metrics (the Manager never deletes the snapshot rows of users who
    logged off).  Rows are kept when either timestamp is missing or unparseable.
    """
    ref = _parse_ts(metrics_ts)
    if ref is None:
        return users
    kept = []
    for u in users:
        ts = _parse_ts(u.get('timestamp'))
        if ts is None or ref - ts <= STALE_USER_SECONDS:
            kept.append(u)
    return kept


def _clean_sessions(entry: dict) -> dict:
    """One /api/sessions/all entry: {timestamp, list_timestamp, list_confirmed_at, summary, sessions}."""
    raw = entry.get('summary') if isinstance(entry.get('summary'), dict) else {}
    summary = _pick(raw, SESSION_SUMMARY_NUMBERS, SESSION_SUMMARY_STRINGS, flags=('sessions_truncated',))
    for key, val in raw.items():
        if isinstance(key, str) and SESSION_COUNT_RE.match(key):
            summary[key] = _to_float(val)
    summary['_sources'] = _str_map(raw.get('_sources'))
    return {
        'timestamp':         _clean_str(entry.get('timestamp'), 40),
        'list_timestamp':    _clean_str(entry.get('list_timestamp'), 40),
        'list_confirmed_at': _clean_str(entry.get('list_confirmed_at'), 40),
        'summary':           summary,
        'sessions':          [_pick(r, ('connections',), SESSION_ROW_STRINGS)
                              for r in _dict_rows(entry.get('sessions'), MAX_SESSION_ROWS)],
    }


# --- Names for the client addresses in the Connected lists ------------------
# A monitored system's own name when the address is one of theirs, otherwise
# the name DNS gives for the address.  A lookup can take seconds, so one
# background thread does them and a poll only uses what is already known: a
# new address gets its name a poll or two later.
CLIENT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$')


class _ClientNames:
    OK_TTL      = 6 * 3600     # seconds a found name (or address) is kept
    FAIL_TTL    = 3600         # seconds before a failed lookup is tried again
    MAX_ENTRIES = 5000

    def __init__(self, addr_lookup=None, name_lookup=None):
        self._addr_lookup = addr_lookup or (lambda ip: socket.gethostbyaddr(ip)[0])
        self._name_lookup = name_lookup or socket.gethostbyname
        self._lock    = threading.Lock()
        self._known   = {}     # ('addr', ip) -> (name or None, expires); ('name', system) -> (ip or None, expires)
        self._pending = []
        self._worker  = None

    def _get(self, key, now):
        """The cached value (None if unknown); a missing or expired one is queued for the worker."""
        with self._lock:
            hit = self._known.get(key)
            if (hit is None or hit[1] <= now) and key not in self._pending:
                if len(self._known) >= self.MAX_ENTRIES:
                    self._known = {k: v for k, v in self._known.items() if v[1] > now}
                if len(self._known) < self.MAX_ENTRIES:
                    self._pending.append(key)
                    if self._worker is None:
                        self._worker = threading.Thread(target=self._run, name='client-names', daemon=True)
                        self._worker.start()
            return hit[0] if hit else None

    def _run(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                key = self._pending[0]
            value = self._lookup(key)
            with self._lock:
                self._pending.remove(key)
                self._known[key] = (value, _monotonic() + (self.OK_TTL if value else self.FAIL_TTL))

    def _lookup(self, key):
        kind, value = key
        try:
            if kind == 'name':
                return str(ipaddress.ip_address(self._name_lookup(value)))
            full = str(self._addr_lookup(value))
            name = full.split('.')[0]
            # Not a name: the address echoed back, or a hosts-file alias such as host.docker.internal
            if not CLIENT_NAME_RE.match(name) or name.isdigit() or full.lower().endswith(('.internal', 'localhost')):
                return None
            return name
        except Exception:
            return None

    def wait(self, timeout: float = 5.0) -> None:
        """Block until the queued lookups are done (tests)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def names(self, addresses, system_names) -> dict:
        """{address: name} for the addresses whose name is known by now."""
        now = _monotonic()
        own = {}
        for system in system_names:
            if isinstance(system, str) and CLIENT_NAME_RE.match(system):
                ip = self._get(('name', system), now)
                if ip:
                    own[ip] = system
        found = {}
        for ip in addresses:
            name = own.get(ip) or self._get(('addr', ip), now)
            if name:
                found[ip] = name
        return found


_client_names = _ClientNames()


def _client_address(value):
    """value as a normalised IP address string, or None (not an address, or a local one)."""
    try:
        addr = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    return None if addr.is_loopback or addr.is_link_local or addr.is_unspecified else str(addr)


def _name_clients(systems: dict) -> None:
    """Add client_name to the Connected rows whose address has a known name ("client_names": false turns it off)."""
    if _config.get('client_names', True) is False:
        return
    rows = []
    for sys_ in systems.values():
        for row in (sys_.get('sessions') or {}).get('sessions') or []:
            ip = _client_address(row.get('client'))
            if ip:
                rows.append((ip, row))
    if not rows:
        return
    names = _client_names.names(sorted({ip for ip, _ in rows}), sorted(systems))
    for ip, row in rows:
        if ip in names:
            row['client_name'] = names[ip]


def _health_status(row: dict) -> dict:
    """Statuses outside the shared vocabulary read as null (shown as unknown)."""
    if row.get('status') not in HEALTH_STATUSES:
        row['status'] = None
    return row


def _clean_storage_health(entry: dict) -> dict:
    """One /api/storage/health/all entry (the flattened history keys are not sent)."""
    events = entry.get('events')
    reasons = entry.get('status_reasons')
    # A list or dict here is unhashable: test the type before the set lookup
    overall = entry.get('overall_status')
    return {
        'timestamp':      _clean_str(entry.get('timestamp'), 40),
        'overall_status': overall if isinstance(overall, str) and overall in HEALTH_STATUSES else None,
        'status_reasons': [r[:200] for r in reasons[:10] if isinstance(r, str)] if isinstance(reasons, list) else [],
        'sources':        _str_map(entry.get('sources')),
        'arrays':  [_health_status(_pick(a, ARRAY_NUMBERS, ARRAY_STRINGS,
                                         lists={'faulty_members': 512, 'members': 512}))
                    for a in _dict_rows(entry.get('arrays'), 128)],
        'disks':   [_health_status(_pick(d, DISK_NUMBERS, DISK_STRINGS, ('smart_passed', 'in_standby'),
                                         lists={'arrays': 128}))
                    for d in _dict_rows(entry.get('disks'), 512)],
        'volumes': [_health_status(_pick(v, (), VOLUME_STRINGS, ('read_only',)))
                    for v in _dict_rows(entry.get('volumes'), 64)],
        'events':  {k: _to_float(v) for k, v in events.items() if isinstance(k, str) and len(k) <= 64}
                   if isinstance(events, dict) else None,
    }


def _clean_owner_totals(val):
    """A device / other block: {bytes, files, cold_1y_bytes, cold_3y_bytes[, owner_count]}, or None."""
    return _pick(val, BREAKDOWN_TOTALS + ('owner_count',)) if isinstance(val, dict) else None


def _clean_owner_rows(val):
    """owners rows; None (owners not collected) stays None, unlike [] (collected, none)."""
    if not isinstance(val, list):
        return None
    return [{'owner': r['owner'], **_pick(r, BREAKDOWN_TOTALS)}
            for r in _dict_rows(val, MAX_OWNER_ROWS) if _clean_str(r.get('owner'), 256) is not None]


def _clean_breakdown(entry: dict) -> dict:
    """One /api/storage/breakdown/all entry (folder totals plus the device owner rollup)."""
    device = entry.get('device')
    if isinstance(device, dict):
        device = dict(_clean_owner_totals(device),
                      owners=_clean_owner_rows(device.get('owners')),
                      other=_clean_owner_totals(device.get('other')))
    else:
        device = None
    thresholds = entry.get('cold_thresholds_days')
    return {
        'timestamp':            _clean_str(entry.get('timestamp'), 40),
        'reference_time':       _clean_str(entry.get('reference_time'), 40),
        'owner_source':         _clean_str(entry.get('owner_source'), 32),
        'cold_thresholds_days': [_to_float(t) for t in thresholds[:2]] if isinstance(thresholds, list) else None,
        'device':               device,
        'folders':              [_pick(f, BREAKDOWN_TOTALS + ('top_owner_bytes',),
                                       {'path': MAX_FOLDER_LEN, 'folder_owner': 256, 'top_owner': 256})
                                 for f in _dict_rows(entry.get('folders'), MAX_FOLDER_ROWS)
                                 if _clean_str(f.get('path'), MAX_FOLDER_LEN) is not None],
        'folders_truncated':    entry.get('folders_truncated') is True,
    }


def _clean_usage(resp: dict, entry: dict) -> dict:
    """One device of /api/user_metrics/usage, with the window it covers."""
    return {
        'window':   _clean_str(resp.get('window'), 16),
        'from':     _clean_str(resp.get('from'), 40),
        'to':       _clean_str(resp.get('to'), 40),
        'source':   _clean_str(entry.get('source'), 16),
        'complete': entry.get('complete') if isinstance(entry.get('complete'), bool) else None,
        'users':    [{'username': u['username'], **_pick(u, USAGE_USER_NUMBERS)}
                     for u in _dict_rows(entry.get('users'), 1000)
                     if _clean_str(u.get('username'), 256) is not None],
    }


def _blank_system(name: str, device_type='unknown') -> dict:
    return {
        'name': name, 'system_id': None, 'device_type': device_type or 'unknown',
        'first_seen': None, 'last_seen': None,
        'metrics': None, 'disk': None, 'totals': None,
    }


def get_all_systems_data(results: dict = None) -> tuple:
    """
    Merge the Manager responses (system_metrics, folder_usage, totals, users,
    and on Managers that have them sessions, storage health, storage owners
    and usage totals) into a single dict keyed by system name.

    Returns:
        (systems, global_totals, errors, features) where systems is
        {
            "Triton": {
                "name":           "Triton",
                "device_type":    "synology",
                "metrics":        { cpu_percent, ram_percent, ... } or None,
                "disk":           { folders, volumes, total_usage_bytes, ... } or None,
                "sessions":       { timestamp, list_confirmed_at, summary, sessions }   (when reported)
                "storage_health": { overall_status, status_reasons, disks, ... }        (when reported)
                "breakdown":      { device, folders, ... }                              (when reported)
                "usage":          { window, users: [...] }                              (when reported)
            },
            ...
        }
        errors is [{"endpoint": path, "error": reason}, ...] for every
        Manager call that failed or returned an unexpected shape, and
        features is {sessions, storage_health, breakdown, usage}: true when
        the Manager answered, false when it doesn't have the endpoint (404,
        R7 - not an error), null when the call failed.
    """
    global _registered_names
    if results is None:
        results = fetch_manager_state()

    errors: list = []
    features: dict = {}

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

    def _optional(path: str) -> dict:
        """_payload for an OPTIONAL_ENDPOINTS path."""
        res = results.get(path)
        key = OPTIONAL_ENDPOINTS[path]
        absent = res is not None and (
            res.status == 404
            # An older Manager routes /api/user_metrics/usage to
            # /api/user_metrics/<device_name> and answers with a user list
            or (path == USAGE_ENDPOINT and res.error is None and isinstance(res.data, list)))
        if absent:
            features[key] = False
            return {}
        failures = len(errors)
        data = _payload(path)
        features[key] = True if len(errors) == failures else None
        return data

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
    if devices:
        _registered_names = frozenset(systems)

    # --- metrics snapshots ---
    # Unmeasured values stay None (contract 4); the frontend renders them as "—".
    for name, data in _payload('/api/metrics/all').items():
        if not isinstance(data, dict):
            continue
        systems.setdefault(name, _blank_system(name, data.get('device_type')))
        units = data.get('units')
        gpus  = data.get('gpus')
        metrics = {
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
            'gpus':                         [_clean_gpu(g) for g in gpus if isinstance(g, dict)] if isinstance(gpus, list) else [],
        }
        # Counter-based fields (spec §11), only when the Manager sends them, so
        # an older Manager's answer comes out exactly as before
        for key in SYSTEM_METRIC_NUMBERS:
            if key in data:
                metrics[key] = _to_float(data[key])
        if 'disk_busy_max_name' in data:
            metrics['disk_busy_max_name'] = _clean_str(data['disk_busy_max_name'], 32)
        systems[name]['metrics'] = metrics

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
            metrics_ts = (systems[name].get('metrics') or {}).get('timestamp')
            systems[name]['users'] = _drop_stale_users([u for u in users if isinstance(u, dict)], metrics_ts)

    # --- optional sections: an older Manager answers 404 and they stay absent ---
    for path, key, clean in ((SESSIONS_ENDPOINT,  'sessions',       _clean_sessions),
                             (HEALTH_ENDPOINT,    'storage_health', _clean_storage_health),
                             (BREAKDOWN_ENDPOINT, 'breakdown',      _clean_breakdown)):
        for name, entry in _optional(path).items():
            if name in systems and isinstance(entry, dict):
                systems[name][key] = clean(entry)

    _name_clients(systems)

    usage_resp = _optional(USAGE_ENDPOINT)
    usage_devices = usage_resp.get('devices')
    for name, entry in (usage_devices.items() if isinstance(usage_devices, dict) else ()):
        if name in systems and isinstance(entry, dict):
            systems[name]['usage'] = _clean_usage(usage_resp, entry)

    # Surface most-recent timestamp per system
    for sys_ in systems.values():
        ts = (sys_.get('metrics') or {}).get('timestamp') \
          or (sys_.get('disk')    or {}).get('timestamp')
        sys_['timestamp'] = ts

    global_totals = totals_resp.get('global', {})
    if not isinstance(global_totals, dict):
        global_totals = {}
    return systems, global_totals, errors, features


def build_data_payload() -> tuple:
    """
    One full poll of the Manager.  Returns (payload, http_status):
      - 502 + success:false when the core call (device registry) failed
      - 200 + degraded:true when only some of the other calls failed
    """
    systems, global_totals, errors, features = get_all_systems_data(fetch_manager_state())
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
        "features":      features,
    }), 200


def downsample(points: list, max_points: int) -> list:
    """
    Bucket-average a time-ordered [{timestamp, value}] list down to at most
    max_points entries.  Fallback for Managers that ignore ?max_points.
    Each bucket keeps its middle timestamp and the mean of its non-null values.
    Points that are aggregates themselves ({min, max, count}, from rollups)
    merge into min of mins, max of maxes and summed counts, and the mean is
    weighted by count.
    """
    n = len(points)
    if max_points <= 0 or n <= max_points:
        return points
    bucket = math.ceil(n / max_points)
    out = []
    for start in range(0, n, bucket):
        chunk  = points[start:start + bucket]
        valued = [(v, p) for v, p in ((_to_float(p.get('value')), p) for p in chunk) if v is not None]
        counts = [_to_float(p.get('count')) for _, p in valued]
        point  = {'timestamp': chunk[len(chunk) // 2].get('timestamp'), 'value': None}
        if valued and all(c is not None and c > 0 for c in counts):
            point['value'] = sum(v * c for (v, _), c in zip(valued, counts)) / sum(counts)
            point['count'] = int(sum(counts))
        elif valued:
            point['value'] = sum(v for v, _ in valued) / len(valued)
        for key, pick in (('min', min), ('max', max)):
            vals = [x for x in (_to_float(p.get(key)) for p in chunk) if x is not None]
            if vals:
                point[key] = pick(vals)
        out.append(point)
    return out


# ---------------------------------------------------------------------------
# Sign-in (optional; config "auth": {"password_hash": ...})
#
# One shared password.  A correct password sets a cookie that keeps that
# browser signed in for auth.remember_days (default 180), renewed as it is
# used, so a computer in regular use is not asked again.  The cookie is
#     v1.<issued, unix seconds>.<random>.<HMAC-SHA256 of the first three parts>
# and holds nothing secret.  Its signing key is derived from the key file
# (.session_key, created on first start) and the password hash, so:
#   - changing the password signs every browser out;
#   - deleting the key file and restarting does the same;
#   - a restart alone keeps everyone signed in.
# The Dashboard itself speaks plain http: the password crosses the network
# once per sign-in, the cookie with every request.  Put it behind an HTTPS
# proxy to protect both (the cookie is then marked Secure).
# ---------------------------------------------------------------------------

# Cookies older than this are reissued, so the 180 days count from the last
# visit, without setting a cookie on every poll
SESSION_RENEW_SECONDS = 24 * 3600
# Wrong passwords from one client address: after LOGIN_MAX_FAILURES within
# LOGIN_WINDOW_SECONDS, sign-in from that address is refused for
# LOGIN_LOCK_SECONDS (before the password is even checked).  Browsers that are
# already signed in are not affected.
LOGIN_MAX_FAILURES   = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS   = 15 * 60
_LOGIN_TABLE_MAX     = 2000
MAX_PASSWORD_LEN     = 1024

# Served without sign-in: /health carries no system data and lets
# tools/restart_manager_dashboard.py (and load balancers) see a 200; the
# sign-in page and its form target; static files (stylesheet, script, images:
# no system data, and the sign-in page needs the stylesheet).
AUTH_EXEMPT_PATHS = frozenset({'/health', '/login', '/logout'})


def _load_session_key(path: Path, logger: logging.Logger) -> bytes:
    """The key file's content, creating the file (mode 0600) when it doesn't exist."""
    try:
        key = path.read_bytes().strip()
        if len(key) >= 32:
            return key
        logger.warning(f"{path} is too short to be a sign-in key; writing a new one "
                       f"(every browser has to sign in again)")
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Cannot read the sign-in key {path} ({e}); using a temporary key: "
                       f"browsers have to sign in again after every Dashboard restart")
        return secrets.token_hex(32).encode('ascii')
    key = secrets.token_hex(32).encode('ascii')
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(key + b'\n')
        logger.info(f"Created the sign-in key {path}")
    except OSError as e:
        logger.warning(f"Cannot save the sign-in key to {path} ({e}): browsers have to sign in "
                       f"again after every Dashboard restart")
    return key


def _make_signing_key(auth_cfg: dict, logger: logging.Logger) -> bytes:
    path = Path(auth_cfg.get('session_key_file') or SESSION_KEY_FILE)
    file_key = _load_session_key(path, logger)
    return hmac.new(file_key, str(auth_cfg['password_hash']).encode('utf-8'), hashlib.sha256).digest()


def _remember_seconds() -> int:
    days = (_config.get('auth') or {}).get('remember_days', DEFAULT_REMEMBER_DAYS)
    return int(float(days) * 86400)


def _sign(payload: str) -> str:
    mac = hmac.new(_signing_key, payload.encode('ascii'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')


def _new_session_token() -> str:
    payload = f"v1.{int(_now())}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_sign(payload)}"


def _session_issued(token):
    """When a valid, unexpired session cookie was issued (unix seconds), else None."""
    if not token or _signing_key is None or len(token) > 256:
        return None
    try:
        payload, mac = token.rsplit('.', 1)
        version, issued, _nonce = payload.split('.')
        if version != 'v1' or not issued.isdigit():
            return None
        if not hmac.compare_digest(mac.encode('ascii', 'replace'), _sign(payload).encode('ascii')):
            return None
    except (ValueError, UnicodeError):
        return None
    age = _now() - int(issued)
    # A little clock skew is fine; a cookie from the future is not
    if age < -300 or age > _remember_seconds():
        return None
    return int(issued)


def _set_session_cookie(resp):
    resp.set_cookie(SESSION_COOKIE, _new_session_token(), max_age=_remember_seconds(),
                    httponly=True, samesite='Lax', path='/',
                    secure=request.is_secure or bool((_config.get('auth') or {}).get('cookie_secure')))
    return resp


def _check_password(password: str) -> bool:
    if not password or len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        return check_password_hash(str(_config['auth']['password_hash']), password)
    except (ValueError, TypeError):
        _logger.error("auth.password_hash in config is not a valid werkzeug hash")
        return False


def _login_locked_seconds(client: str) -> int:
    """Seconds this client still has to wait before it may try a password again (0 = may try)."""
    now = _monotonic()
    with _login_lock:
        entry = _login_failures.get(client)
        if not entry:
            return 0
        if entry['locked_until'] > now:
            return int(math.ceil(entry['locked_until'] - now))
        if now - entry['first'] > LOGIN_WINDOW_SECONDS:
            del _login_failures[client]
        return 0


def _login_failed(client: str):
    now = _monotonic()
    with _login_lock:
        if len(_login_failures) >= _LOGIN_TABLE_MAX and client not in _login_failures:
            # Forget finished entries first; then, rather than grow without
            # bound, the oldest ones
            for key in [k for k, e in _login_failures.items()
                        if e['locked_until'] <= now and now - e['first'] > LOGIN_WINDOW_SECONDS]:
                del _login_failures[key]
            while len(_login_failures) >= _LOGIN_TABLE_MAX:
                del _login_failures[min(_login_failures, key=lambda k: _login_failures[k]['first'])]
        entry = _login_failures.get(client)
        if not entry or now - entry['first'] > LOGIN_WINDOW_SECONDS:
            entry = _login_failures[client] = {'count': 0, 'first': now, 'locked_until': 0.0}
        entry['count'] += 1
        if entry['count'] >= LOGIN_MAX_FAILURES:
            entry['locked_until'] = now + LOGIN_LOCK_SECONDS
            entry['count'] = 0
            entry['first'] = now
            return True
    return False


def _login_succeeded(client: str):
    with _login_lock:
        _login_failures.pop(client, None)


def _safe_next(target) -> str:
    """A path on this site to go to after sign-in ('/' for anything else: no open redirect)."""
    target = str(target or '')
    if not target.startswith('/') or target.startswith('//') or '\\' in target \
            or any(ord(c) < 32 for c in target) or len(target) > 2000:
        return '/'
    if urlsplit(target).netloc or target.split('?', 1)[0] in ('/login', '/logout'):
        return '/'
    return target


def _same_origin() -> bool:
    """False when a form post names another site as its origin (cross-site request)."""
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if not source or source == 'null':
        return source != 'null'
    return urlsplit(source).netloc == request.host


def _login_page(next_url: str, error: str = None, status: int = 200):
    resp = app.make_response((render_template(
        'login.html', next_url=next_url, error=error,
        remember_days=int(round(_remember_seconds() / 86400))), status))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.before_request
def _require_sign_in():
    if not _config.get('auth'):
        return None   # auth not configured - open dashboard (legacy behaviour)
    if request.endpoint == 'static' or request.path in AUTH_EXEMPT_PATHS:
        return None
    issued = _session_issued(request.cookies.get(SESSION_COOKIE))
    if issued is not None:
        g.renew_session = _now() - issued > SESSION_RENEW_SECONDS
        return None
    if request.path.startswith('/api/'):
        # The page's script sends the browser to the sign-in page on this answer
        resp = jsonify({"success": False, "error": "Sign-in required", "login": "/login"})
        resp.status_code = 401
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    target = request.full_path if request.query_string else request.path
    return redirect('/login' + ('' if target == '/' else '?next=' + quote(target, safe='')))


@app.after_request
def _finish_response(resp):
    if g.get('renew_session') and resp.status_code < 400:
        _set_session_cookie(resp)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    return resp


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _config.get('auth'):
        return redirect('/')
    next_url = _safe_next(request.values.get('next'))
    if request.method == 'GET':
        if _session_issued(request.cookies.get(SESSION_COOKIE)) is not None:
            return redirect(next_url)
        return _login_page(next_url)

    client = request.remote_addr or 'unknown'
    if not _same_origin():
        return _login_page(next_url, 'That request came from another page. Please try again.', 400)
    wait = _login_locked_seconds(client)
    if wait:
        return _login_page(next_url, 'Too many wrong passwords. Try again in '
                           f'{int(math.ceil(wait / 60))} minute(s).', 429)
    if _check_password(request.form.get('password', '')):
        _login_succeeded(client)
        _logger.info(f"Sign-in from {client}")
        return _set_session_cookie(redirect(next_url))
    locked = _login_failed(client)
    _logger.warning(f"Wrong Dashboard password from {client}"
                    + (f"; sign-in from that address is refused for {LOGIN_LOCK_SECONDS // 60} min"
                       if locked else ''))
    if locked:
        return _login_page(next_url, 'Too many wrong passwords. Try again in '
                           f'{LOGIN_LOCK_SECONDS // 60} minutes.', 429)
    return _login_page(next_url, 'Wrong password.', 401)


@app.route('/logout', methods=['POST'])
def logout():
    if not _same_origin():
        return _error('Bad request', 400)
    resp = redirect('/login' if _config.get('auth') else '/')
    resp.delete_cookie(SESSION_COOKIE, path='/')
    g.renew_session = False
    return resp


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
    return render_template('index.html', client_config=_client_config(),
                           auth_enabled=bool(_config.get('auth')))


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
    if not _valid_system_name(system_name):
        return _error('Invalid system name', 400)
    if not _valid_name(data_type):
        return _error('Invalid data type', 400)
    # "." / ".." segments would be collapsed by URL normalisation and could
    # reach a different Manager endpoint
    max_len = MAX_BREAKDOWN_FIELD_LEN if data_type == 'storage_breakdown' else MAX_FIELD_LEN
    if not field or len(field) > max_len or _has_dot_segment(field):
        return _error('Invalid field', 400)

    from_param = request.args.get('from') or None
    to_param   = request.args.get('to')   or None
    for ts in (from_param, to_param):
        if ts is not None and not ISO_TS_RE.match(ts):
            return _error('Invalid time range', 400)

    # entity: one user of user_metrics; resolution: raw / hourly / daily data.
    # Managers before the usage-metrics release ignore both.
    entity     = request.args.get('entity')
    resolution = request.args.get('resolution')
    if entity is not None and not _valid_entity(entity):
        return _error('Invalid entity', 400)
    if resolution is not None and resolution not in HISTORY_RESOLUTIONS:
        return _error('Invalid resolution', 400)

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
    if entity is not None:
        params['entity'] = entity
    if resolution is not None:
        params['resolution'] = resolution

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
        if entity is not None and data.get('entity') is None:
            # An older Manager ignored ?entity and answered with every user's
            # values mixed into one series - not a chart of this user
            return _error('Per-user history needs a newer Manager', 404)
        points = data.get('data')
        if isinstance(points, list) and len(points) > max_points:
            data['data'] = downsample(points, max_points)
            data['downsampled'] = True
            data['downsampled_from'] = len(points)
        return jsonify(_json_safe(data)), 200

    except Exception as e:
        _logger.error(f"Error fetching metric history: {e}", exc_info=True)
        return _error('Internal error', 500)


@app.route('/api/storage/breakdown/<system_name>')
def get_storage_breakdown(system_name):
    """
    Proxy the Manager's latest storage breakdown of one system, with the full
    owner rows of ?folder=<path> (every folder when omitted).  The card loads
    it when a folder's cold-data bar is clicked.  A Manager without storage
    breakdown, or without data for this system, answers 404.
    """
    if not _valid_system_name(system_name):
        return _error('Invalid system name', 400)
    folder = request.args.get('folder') or None
    if folder is not None and (len(folder) > MAX_FOLDER_LEN or _has_dot_segment(folder)):
        return _error('Invalid folder', 400)

    try:
        res = _manager_fetch(f"/api/storage/breakdown/{quote(system_name, safe='')}",
                             params={'folder': folder} if folder is not None else None)
        if res.status == 404:
            return _error('Not found', 404)
        if res.status == 400:
            return _error('Bad request', 400)
        if res.error or not isinstance(res.data, dict):
            return _error('Manager unavailable', 502)
        return jsonify(_json_safe(res.data)), 200
    except Exception as e:
        _logger.error(f"Error fetching storage breakdown: {e}", exc_info=True)
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
    print("\nAdd this to dashboard config.json, then restart the Dashboard:\n")
    print(json.dumps({"auth": {"password_hash": generate_password_hash(password),
                               "remember_days": DEFAULT_REMEMBER_DAYS}}, indent=2))
    print("\nA browser that signs in stays signed in for remember_days. Replacing the hash "
          "later signs every browser out.")
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
    if config.get('auth'):
        logger.info(f"Sign-in: enabled (password; a browser stays signed in for "
                    f"{_remember_seconds() // 86400} days)")
    else:
        logger.info("Sign-in: DISABLED")
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
