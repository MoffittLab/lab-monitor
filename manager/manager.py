#!/usr/bin/env python3
"""
Lab Monitor Manager

Central service that receives queued messages from collectors and stores them.
Provides REST API for Dashboard queries.

Usage:
    python3 manager.py --config config.json
    python3 manager.py --config config.json --check-config   # validate and exit
"""
import os
import sys
import json
import hmac
import time
import hashlib
import logging
import logging.handlers
import math
import argparse
import sqlite3
import threading
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, request, jsonify, g
from werkzeug.exceptions import HTTPException
from functools import wraps
import re

import rollups
import snapshot
from metrics import MetricsDB
from data_store import (
    TypedDataStore, RejectedMessage, DEFAULT_ROW_LIMIT, FOLDER_USAGE, table_for_data_type,
)
from storage_util import (
    CommitGroup, GroupBarrier, InstanceLock, InstanceLockError, canonical_timestamp, format_timestamp,
    is_transient_db_error, is_valid_device_name, normalize_device_name, utc_now,
)


DEFAULT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
# Cap on messages per request for collectors that send header.msg_id (new
# collectors send <= max_batch_messages, default 100).  Requests whose
# messages carry no msg_id come from OLD collectors, which post their whole
# queue and can't split it: they are exempt (max_request_bytes still applies),
# otherwise a backlog over the cap would get 413 on every run forever.
DEFAULT_MAX_MESSAGES = 10000
# waitress's own body limit is a backstop above max_request_bytes, so that
# Flask (not waitress, whose 413 is plain text) answers an oversized body
# with the JSON error body.
WAITRESS_BODY_MARGIN = 1024 * 1024
MAX_QUERY_LIMIT = 200000
MIN_TOKEN_LENGTH = 16
_PLACEHOLDER_MARKERS = (
    'CHANGE-ME', 'CHANGE_ME', 'CHANGEME', 'YOUR-', 'YOUR_',
    'GENERATE-RANDOM', 'GENERATE_RANDOM', 'PLACEHOLDER', 'REPLACE-ME', 'REPLACE_ME',
)
_MSG_ID_RE = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
# /api/user_metrics/usage result cache (manager config "usage_cache_seconds")
DEFAULT_USAGE_CACHE_SECONDS = 300
# Group commit on ingest (manager config "ingest_commit_messages"; _store_messages):
# up to this many messages of one request share one transaction per database,
# and a group is committed once it has worked INGEST_COMMIT_SECONDS, so the
# locks it holds (other requests and reads wait for them) stay short.
DEFAULT_INGEST_COMMIT_MESSAGES = 100
INGEST_COMMIT_SECONDS = 0.5
USAGE_WINDOWS = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400}
# /api/history ?resolution= and ?entity= (one user of user_metrics)
HISTORY_RESOLUTIONS = ('auto', 'raw', 'hour', 'day')
MAX_ENTITY_LEN = 256
# /api/storage/breakdown/<name>?folder= (a folder path)
MAX_FOLDER_LEN = 4096


def write_pid_file():
    """Write PID file to .pids/manager.pid in the repo root."""
    try:
        # Repo root is two levels up from this script (manager/manager.py -> lab-monitor/)
        repo_root = Path(__file__).parent.parent
        pids_dir = repo_root / '.pids'
        pids_dir.mkdir(exist_ok=True)

        pid_file = pids_dir / 'manager.pid'
        pid_file.write_text(str(os.getpid()))
        return str(pid_file)
    except Exception as e:
        print(f"Warning: Failed to write PID file: {e}")
        return None


# Setup logging
def setup_logging(log_file: str = None, log_level: str = "INFO",
                  console_level: str = None,
                  max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
    """
    Configure the ROOT logger so every module (data_store, metrics, waitress)
    reaches the same handlers: a console handler plus, if log_file is set, a
    size-rotated file.  When a log file is configured the console defaults to
    WARNING so a redirected stdout (Task Scheduler launcher) stays small.
    Safe to call more than once.
    """
    level = getattr(logging, str(log_level).upper(), logging.INFO)
    if console_level is None:
        console_level = 'WARNING' if log_file else log_level
    console_lvl = getattr(logging, str(console_level).upper(), level)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, '_lab_monitor', False):
            root.removeHandler(h)
            h.close()

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(console_lvl)
    console._lab_monitor = True
    root.addHandler(console)

    notes = []
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        try:
            kept = _set_aside_unrotated_log(log_file, max_bytes)
            if kept:
                notes.append((logging.WARNING,
                              f"The previous {log_file} ({os.path.getsize(kept) / 1048576:.1f} MiB, "
                              f"written without rotation by an older Manager) was moved to {kept} "
                              f"and is kept there: rotation never deletes it"))
        except OSError as e:
            # e.g. still open in another process on Windows: don't rotate it
            # away (rotation would delete it after backup_count more files)
            max_bytes = 0
            notes.append((logging.WARNING,
                          f"{log_file} is larger than log_max_bytes but could not be moved aside "
                          f"({e}); it is appended to WITHOUT rotation until the next start"))
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler._lab_monitor = True
        root.addHandler(file_handler)

    logger = logging.getLogger('manager')
    for lvl, note in notes:
        logger.log(lvl, note)
    return logger


def _set_aside_unrotated_log(log_file: str, max_bytes: int):
    """
    A log file larger than max_bytes was not written by the rotating handler
    (an older Manager logged to one ever-growing file).  Rotating it would
    rename it to <log>.1 and delete it backup_count rotations later, so it
    is renamed to <log>.pre-rotation-<UTC stamp> instead, outside the
    rotation set.  Returns the new name, None if nothing was moved; raises
    OSError if the rename fails.
    """
    try:
        size = os.path.getsize(log_file)
    except OSError:
        return None
    if max_bytes <= 0 or size <= max_bytes:
        return None
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    dest = f"{log_file}.pre-rotation-{stamp}"
    n = 1
    while os.path.exists(dest):
        dest = f"{log_file}.pre-rotation-{stamp}-{n}"
        n += 1
    os.rename(log_file, dest)
    return dest


# Load config
def load_config(config_path: str) -> dict:
    """Load configuration from JSON"""
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        raise


class ConfigError(Exception):
    """Configuration the Manager refuses to start with."""


# Initialize Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = DEFAULT_MAX_REQUEST_BYTES

# Global config (set during startup)
_config = {}
_logger = logging.getLogger('manager')
_metrics_db = None
_data_store = None
_user_metrics = None   # initialised in init_app()
_snapshots = None      # snapshot.SnapshotRunner, initialised in init_app()
_auth = None
_cors_enabled = False
_rollup_backfill = None   # rollups.BackfillThread, started by main()
_usage_cache_seconds = DEFAULT_USAGE_CACHE_SECONDS
_ingest_commit_messages = DEFAULT_INGEST_COMMIT_MESSAGES
# (window, device, top) -> (time.monotonic() computed, answer)
_usage_cache = {}
_usage_cache_lock = threading.Lock()
# Grandfathered device names already logged once by this process
_grandfather_warned = set()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class _AuthConfig:
    """Token lists by scope (tokens held as UTF-8 bytes for compare_digest)."""
    def __init__(self):
        self.admin = []        # admin_tokens + legacy auth_tokens
        self.read = []         # read_tokens
        self.collectors = []   # [(normalized device name or '*', token)]


class _Rights:
    def __init__(self):
        self.admin = False
        self.read = False
        self.any_device = False
        self.devices = set()   # normalized device names this token may write

    def may_write(self, device_name: str) -> bool:
        return self.admin or self.any_device or normalize_device_name(device_name) in self.devices


def _check_token(token, where: str, problems: list):
    if not isinstance(token, str) or not token.strip():
        problems.append(f"{where}: token must be a non-empty string")
        return None
    t = token.strip()
    upper = t.upper()
    if any(m in upper for m in _PLACEHOLDER_MARKERS):
        problems.append(f"{where}: placeholder token — replace it with a random secret")
        return None
    if len(t) < MIN_TOKEN_LENGTH:
        problems.append(f"{where}: token is shorter than {MIN_TOKEN_LENGTH} characters")
        return None
    return t.encode('utf-8')


def build_auth(config: dict, logger: logging.Logger) -> _AuthConfig:
    """
    Validate and load the token scopes from config:
        collector_tokens  {"<device>": "<token>"}  POST /api/data/queue for that device
                          ("*" = any device, explicit opt-in)
        read_tokens       ["..."]                  GET endpoints (Dashboard)
        admin_tokens      ["..."]                  everything, incl. DELETE
        auth_tokens       ["..."]                  legacy: admin + any-device write
    Raises ConfigError for placeholder/short tokens or malformed sections.
    """
    auth, problems = _AuthConfig(), []

    for key, target in (('admin_tokens', auth.admin), ('read_tokens', auth.read),
                        ('auth_tokens', auth.admin)):
        tokens = config.get(key) or []
        if not isinstance(tokens, list):
            problems.append(f"{key} must be a list of strings")
            continue
        for i, token in enumerate(tokens):
            tb = _check_token(token, f"{key}[{i}]", problems)
            if tb:
                target.append(tb)

    collector_tokens = config.get('collector_tokens') or {}
    if not isinstance(collector_tokens, dict):
        problems.append('collector_tokens must be an object {"<device_name>": "<token>"}')
        collector_tokens = {}
    for device, token in collector_tokens.items():
        if device != '*' and not is_valid_device_name(device):
            problems.append(
                f"collector_tokens: invalid device name {device!r} (allowed: letters, digits, "
                f"'_', '.', '-'; for a collector registered under an older name such as "
                f"'Lab PC', use the same name with '_' for the other characters, e.g. "
                f"'Lab_PC': names are matched after normalization)")
            continue
        tb = _check_token(token, f"collector_tokens[{device!r}]", problems)
        if tb:
            auth.collectors.append(('*' if device == '*' else normalize_device_name(device), tb))

    if problems:
        raise ConfigError("Refusing to start — insecure or invalid tokens: " + "; ".join(problems))
    if not (auth.admin or auth.read or auth.collectors):
        raise ConfigError(
            "Refusing to start — no API tokens configured "
            "(set admin_tokens, read_tokens and collector_tokens)"
        )

    if config.get('auth_tokens'):
        logger.warning(
            "Config key 'auth_tokens' is deprecated: these tokens have full admin + "
            "any-device write rights. Move collectors to 'collector_tokens', the "
            "Dashboard to 'read_tokens' and tools to 'admin_tokens'."
        )
    if '*' in collector_tokens:
        logger.warning(
            "collector_tokens has a '*' entry: that token may write data for ANY device"
        )
    return auth


def _token_rights(token: str):
    """Rights for a bearer token, or None if it matches no configured token."""
    tb = token.encode('utf-8')
    rights, matched = _Rights(), False
    # Compare against every token (no early exit) with constant-time compares
    for t in _auth.admin:
        if hmac.compare_digest(tb, t):
            rights.admin = matched = True
    for t in _auth.read:
        if hmac.compare_digest(tb, t):
            rights.read = matched = True
    for device, t in _auth.collectors:
        if hmac.compare_digest(tb, t):
            matched = True
            if device == '*':
                rights.any_device = True
            else:
                rights.devices.add(device)
    return rights if matched else None


def _error(status: int, message: str):
    return jsonify({"status": "error", "error": message}), status


def require_scope(scope: str):
    """
    Decorator: require a Bearer token with the given scope.
        'read'  — read_tokens / admin
        'write' — collector_tokens / admin (device checked by the endpoint)
        'admin' — admin_tokens / legacy auth_tokens
    401 = missing or unknown token; 403 = valid token without this scope.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            auth_header = request.headers.get('Authorization', '')
            if not auth_header.startswith('Bearer ') or not auth_header[7:].strip():
                _logger.warning(f"Request missing Bearer token: {request.method} {request.path} from {request.remote_addr}")
                return _error(401, "Missing or invalid authorization")

            rights = _token_rights(auth_header[7:].strip()) if _auth else None
            if rights is None:
                _logger.warning(f"Invalid token: {request.method} {request.path} from {request.remote_addr}")
                return _error(401, "Invalid token")

            allowed = (
                rights.admin or
                (scope == 'read' and rights.read) or
                (scope == 'write' and (rights.any_device or rights.devices))
            )
            if not allowed:
                _logger.warning(f"Token not allowed for {request.method} {request.path} from {request.remote_addr}")
                return _error(403, "Token not allowed for this endpoint")

            g.rights = rights
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def _close_stores():
    global _metrics_db, _data_store, _user_metrics, _rollup_backfill
    if _rollup_backfill is not None:
        _rollup_backfill.stop()
        _rollup_backfill = None
    for store in (_data_store, _metrics_db):
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    _metrics_db = _data_store = _user_metrics = None


def validate_config(config: dict, logger: logging.Logger) -> _AuthConfig:
    """Check the config without touching any database. Raises ConfigError."""
    auth = build_auth(config, logger)
    retention = config.get('history_retention_days')
    if retention is not None and (isinstance(retention, bool) or not isinstance(retention, int) or retention < 1):
        raise ConfigError("history_retention_days must be null (keep forever) or a positive integer")
    for key in ('max_request_bytes', 'max_messages_per_request'):
        value = config.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ConfigError(f"{key} must be a positive integer")
    if not isinstance(config.get('backup_before_migration', True), bool):
        raise ConfigError("backup_before_migration must be true or false")
    try:
        snapshot.settings_from_config(config)
    except ValueError as e:
        raise ConfigError(str(e))
    # Optional feature settings: a bad value is logged and replaced by its
    # default, never a reason to refuse to start
    rollups.settings_from_config(config, logger)
    usage_cache_seconds(config, logger)
    ingest_commit_messages(config, logger)
    return auth


def usage_cache_seconds(config: dict, logger: logging.Logger = None) -> float:
    """
    Config "usage_cache_seconds" (default 300): how long a
    /api/user_metrics/usage answer is reused; 0 = not cached.  A bad value
    is logged (when logger is given) and replaced by the default.
    """
    value = config.get('usage_cache_seconds')
    if value is None:
        return DEFAULT_USAGE_CACHE_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(value) or value < 0:
        if logger:
            logger.warning(f"Config 'usage_cache_seconds' = {value!r} is invalid (a number of seconds "
                           f">= 0); using the default {DEFAULT_USAGE_CACHE_SECONDS}")
        return DEFAULT_USAGE_CACHE_SECONDS
    return value


def ingest_commit_messages(config: dict, logger: logging.Logger = None) -> int:
    """
    Config "ingest_commit_messages" (default 100): how many messages of one
    request are stored in one transaction per database (_store_messages);
    1 = one transaction per message.  A bad value is logged (when logger is
    given) and replaced by the default.
    """
    value = config.get('ingest_commit_messages')
    if value is None:
        return DEFAULT_INGEST_COMMIT_MESSAGES
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        if logger:
            logger.warning(f"Config 'ingest_commit_messages' = {value!r} is invalid (a whole number "
                           f">= 1); using the default {DEFAULT_INGEST_COMMIT_MESSAGES}")
        return DEFAULT_INGEST_COMMIT_MESSAGES
    return value


# --------------------------------------------------------------------------- #
# Recommended data layout                                                     #
#   <root>\manager-data   this Manager's databases: a local disk, used by     #
#                         nothing else                                        #
#   <root>\data           a collector on the same machine (archive, queue)    #
#   snapshot.dest_dir     nightly copy on a NAS share                         #
# --------------------------------------------------------------------------- #

# Files only a collector writes into its data_dir/<name>/ folder
_COLLECTOR_FILES = ('queue.json', 'queue.json.lock', 'network_snapshot.json',
                    'perf_snapshot.json', 'perf_snapshot.json.lock', 'sessions_state.json',
                    'storage_health_state.json', 'storage_breakdown_state.json')
_COLLECTOR_ARCHIVE_RE = re.compile(r'^\d{4}-\d{2}\.jsonl$')


def is_network_path(path: str) -> bool:
    """
    True for a UNC path (\\\\server\\share, \\\\?\\UNC\\...), a mapped network drive
    (Windows), or a path on an NFS / SMB / sshfs mount (Linux).  False when
    it can't tell.
    """
    p = str(path).strip()
    if re.match(r'^[\\/]{2}[?.][\\/]UNC[\\/]', p, re.I):
        return True
    if re.match(r'^[\\/]{2}(?![?.][\\/])', p):
        return True
    if os.name == 'nt':
        m = re.match(r'^(?:[\\/]{2}[?.][\\/])?([A-Za-z]):', p) or \
            re.match(r'^([A-Za-z]):', os.path.abspath(p))
        if m:
            try:
                import ctypes
                return ctypes.windll.kernel32.GetDriveTypeW(f"{m.group(1)}:\\") == 4  # DRIVE_REMOTE
            except Exception:
                return False
        return False
    try:
        target = os.path.realpath(p)
        best, fstype = '', ''
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mnt = parts[1].replace('\\040', ' ')
                    if (target == mnt or target.startswith(mnt.rstrip('/') + '/')) and len(mnt) > len(best):
                        best, fstype = mnt, parts[2]
        return fstype.startswith(('nfs', 'cifs', 'smb', 'fuse.sshfs', '9p'))
    except OSError:
        return False


def collector_folders_in(data_dir: str) -> list:
    """Sub-folders of data_dir holding a collector's own files (queue, monthly JSONL archive)."""
    found = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return found
    for name in names:
        sub = os.path.join(data_dir, name)
        if not os.path.isdir(sub):
            continue
        try:
            files = os.listdir(sub)
        except OSError:
            continue
        if any(f in _COLLECTOR_FILES or _COLLECTOR_ARCHIVE_RE.match(f) for f in files):
            found.append(name)
    return found


def data_layout_warnings(config: dict) -> list:
    """What differs from the recommended layout (see above); [] when it matches."""
    data_dir = config.get("data_dir", "data")
    out = []
    if is_network_path(data_dir):
        out.append(
            f"data_dir {data_dir!r} is on a network share. SQLite is not reliable there "
            f"(failed transactions, 'disk I/O error'). Keep data_dir on a local disk and copy it "
            f"to the share with snapshot.dest_dir (manager/README.md, 'Moving data_dir off the NAS').")
    shared = collector_folders_in(data_dir)
    if shared:
        out.append(
            f"data_dir {data_dir!r} is also a collector's data_dir (collector files in: "
            f"{', '.join(shared[:5])}{', ...' if len(shared) > 5 else ''}). It works, but keep the "
            f"Manager's databases in a folder of their own (e.g. ...\\lab-monitor\\manager-data).")
    snap = snapshot.settings_from_config(config)
    if not snap['enabled']:
        out.append(
            "Snapshots are disabled: the databases have no copy on another machine. Set "
            "snapshot.enabled and snapshot.dest_dir (manager/README.md, 'Snapshots to a NAS').")
    elif not is_network_path(snap['dest_dir']):
        out.append(
            f"snapshot.dest_dir {snap['dest_dir']!r} is not on a network share: a disk failure "
            f"on this machine could take the snapshots with it.")
    return out


def init_app(config: dict, logger: logging.Logger = None):
    """Initialize app with config. Raises ConfigError for unsafe config."""
    global _config, _logger, _metrics_db, _data_store, _user_metrics, _auth, _cors_enabled, _snapshots
    global _usage_cache_seconds, _ingest_commit_messages
    logger = logger or logging.getLogger('manager')

    auth = validate_config(config, logger)

    _close_stores()
    _config = config
    _logger = logger
    _auth = auth
    _grandfather_warned.clear()
    _usage_cache_seconds = usage_cache_seconds(config)
    _ingest_commit_messages = ingest_commit_messages(config)
    with _usage_cache_lock:
        _usage_cache.clear()

    app.config['MAX_CONTENT_LENGTH'] = int(config.get('max_request_bytes', DEFAULT_MAX_REQUEST_BYTES))

    data_dir = config.get("data_dir", "data")
    os.makedirs(data_dir, exist_ok=True)
    for warning in data_layout_warnings(config):
        logger.warning(warning)

    # Databases written by an older Manager are migrated once when opened;
    # by default each is first copied to <db>.pre-upgrade-<stamp>.bak.
    backup = config.get("backup_before_migration", True)
    if not backup:
        logger.warning("backup_before_migration is false: databases written by an older "
                       "Manager are migrated WITHOUT a backup copy")

    # Central summary DB (latest state per system — dashboard layer)
    db_path = os.path.join(data_dir, "metrics.db")
    _metrics_db = MetricsDB(db_path, data_dir=data_dir, backup_before_migration=backup)
    logger.info(f"Central metrics DB: {db_path}")

    # Per-system typed data store (full history); existing databases are
    # opened (and upgraded where needed) now rather than on first request.
    # Rollups are written with every new row; the rows already stored are
    # rolled up by the background backfill (start_rollup_backfill_thread).
    rollup_settings = rollups.settings_from_config(config)
    engine = rollups.RollupEngine(rollup_settings) if rollup_settings['enabled'] else None
    if engine is None:
        logger.warning("Rollups disabled (rollups.enabled is false): no hourly/daily rollups are "
                       "written; rows stored meanwhile are rolled up when they are enabled again")
    _data_store = TypedDataStore(data_dir=data_dir, backup_before_migration=backup, rollups=engine)
    blocked = _data_store.prepare_existing()
    logger.info(f"TypedDataStore root: {data_dir}")
    if _metrics_db.upgrade_blocked or blocked:
        held = (['metrics.db (so every system)'] if _metrics_db.upgrade_blocked else []) + \
               ([f'{blocked} per-system database(s)'] if blocked else [])
        logger.error(
            f"Database migration held back for {' and '.join(held)} because the pre-upgrade "
            f"backup failed (see the errors above). The data stays readable; new messages get "
            f"'error' and stay queued on the collectors until the backup succeeds."
        )

    # User metrics store (per-user CPU/RAM snapshots and history)
    _user_metrics = _metrics_db.users
    logger.info("User metrics store initialized")

    # Nightly snapshots to a second location (scheduled by main(), not here)
    if _snapshots is not None:
        _snapshots.stop()
    _snapshots = snapshot.SnapshotRunner(data_dir, snapshot.settings_from_config(config),
                                         logging.getLogger('snapshot'))

    for name in _metrics_db.get_all_systems():
        if not is_valid_device_name(name):
            logger.warning(
                f"Registered device '{name}' has a name outside the allowed pattern "
                f"(letters, digits, '_', '.', '-', max 64). It was registered by an older "
                f"Manager, so it is still accepted (grandfathered); new devices with such "
                f"names are refused. Renaming that collector would start a new device with "
                f"an empty history."
            )

    # CORS is off unless origins are configured: the Dashboard calls the
    # Manager server-side, so browsers never need cross-origin access.
    cors_origins = config.get("cors_origins") or []
    if cors_origins and not _cors_enabled:
        from flask_cors import CORS
        CORS(app, origins=cors_origins)
        _cors_enabled = True
        logger.info(f"CORS enabled for origins: {cors_origins}")
    elif not cors_origins:
        logger.info("CORS disabled (no cors_origins configured)")


def _name_accepted(name) -> bool:
    """
    A device name ingest and the read endpoints accept: ^[A-Za-z0-9_.-]{1,64}$,
    or exactly a name an older Manager already registered (it accepted any
    name; refusing it now would lock that collector out).  Grandfathered
    names are logged once per process.  Storage paths are always derived
    through normalize_device_name, so no name can escape data_dir.
    """
    if is_valid_device_name(name):
        return True
    if not isinstance(name, str) or _metrics_db is None or not _metrics_db.is_registered(name):
        return False
    if name not in _grandfather_warned:
        _grandfather_warned.add(name)
        _logger.warning(
            f"Accepting device name {name!r}: it is outside the allowed pattern but was "
            f"registered by an older Manager (grandfathered). Renaming that collector would "
            f"start a new device with an empty history."
        )
    return True


def validate_nas_name(nas_name: str) -> bool:
    """Validate a system name: ^[A-Za-z0-9_.-]{1,64}$ (same rule as ingest), or grandfathered."""
    return _name_accepted(nas_name)


def _system_known(name: str) -> bool:
    """A system is known if it has a history DB or a central registry row."""
    return _data_store.has_system(name) or _metrics_db.is_known(name)


def _user_device_known(name: str) -> bool:
    """
    For the per-user endpoints: a known system, or a name that still has
    per-user rows (the old DELETE /api/system/<name> left those behind, and
    the old Manager kept serving them).
    """
    return _system_known(name) or _user_metrics.has_device(name)


class _BadArg(Exception):
    pass


def _int_arg(name: str, default, minimum: int = 1, maximum: int = MAX_QUERY_LIMIT):
    raw = request.args.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadArg(f"'{name}' must be an integer")
    if value < minimum:
        raise _BadArg(f"'{name}' must be >= {minimum}")
    return min(value, maximum)


def _ts_arg(name: str):
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return canonical_timestamp(raw)
    except ValueError:
        raise _BadArg(f"'{name}' must be an ISO 8601 timestamp")


# ---------------------------------------------------------------------------
# Error handlers — JSON bodies, never exception text
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def _too_large(e):
    _logger.warning(
        f"Request body too large: {request.method} {request.path} from {request.remote_addr} "
        f"({request.content_length} bytes, max_request_bytes {app.config.get('MAX_CONTENT_LENGTH')}). "
        f"An old collector posting its whole queue gets this on every run until "
        f"max_request_bytes is raised or the collector is upgraded (it then sends small batches)."
    )
    return _error(413, "Request body too large")


@app.errorhandler(HTTPException)
def _http_error(e):
    return _error(e.code or 500, e.name)


@app.errorhandler(Exception)
def _unhandled(e):
    _logger.error(f"Unhandled error on {request.method} {request.path}", exc_info=e)
    return _error(500, "Internal server error")


# Routes

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

@app.route('/api/data/queue', methods=['POST'])
@require_scope('write')
def ingest_queue():
    """
    Receive and store a queue batch from a collector.

    New format (collectors v2+):
    {
        "queue_id": "Triton-3f9a1c2b7d4e",
        "name":     "Triton",
        "id":       "synology-triton",
        "messages": [
            {
                "header": {
                    "msg_id":      "<uuid4 hex>",
                    "device_name": "Triton",
                    "device_id":   "synology-triton",
                    "device_type": "synology",
                    "timestamp":   "2026-08-04T07:00:00Z"
                },
                "data": {
                    "data_type": "folder_usage",
                    ...
                }
            },
            ...
        ]
    }

    Legacy format (collectors v1 — backward compat):
    {
        "queue_id": "...",
        "name":     "Triton",
        "id":       "synology-triton",
        "entries":  [ { "type": "disk|metrics", "timestamp": "...", ... } ]
    }

    Returns 200 with a per-message ACK:
    {"status": "ok", "queue_id": "...", "received": N, "stored": H,
     "counts": {"stored": a, "duplicate": b, "rejected": c, "error": d},
     "results": [{"msg_id": "...", "status": "stored|duplicate|rejected|error", "error": "..."}]}
    where H = a + b + c (durably handled), so old collectors that delete their
    queue only when stored == sent keep draining.  After the first 'error'
    (a storage failure) the remaining messages are not attempted and are
    answered 'error' as well, so a locked database costs one busy timeout
    per request instead of one per message.  The answer is sent only after
    everything it reports stored, duplicate or rejected is committed
    (group commit, _store_messages).
    """
    data = request.get_json(silent=True)   # 413 propagates to the handler
    if not isinstance(data, dict):
        return _error(400, "Malformed request body (expected a JSON object)")

    queue_id  = data.get("queue_id")
    name      = data.get("name")
    system_id = data.get("id")

    if not all([queue_id, name, system_id]):
        return _error(400, "Missing required fields: queue_id, name, id")
    if not isinstance(queue_id, str) or len(queue_id) > 256 or isinstance(system_id, (dict, list)):
        return _error(400, "Invalid queue_id or id")
    if not _name_accepted(name):
        return _error(400, "Invalid device name")
    if not g.rights.may_write(name):
        _logger.warning(f"Token not allowed to write for device '{name}' (queue {queue_id})")
        return _error(403, "Token not allowed for this device")

    # ---------------------------------------------------------------- #
    # Route to new or legacy handler based on payload shape             #
    # ---------------------------------------------------------------- #
    if "messages" in data:
        messages = data["messages"]
    elif "entries" in data:
        _logger.info(f"Legacy queue format from {name} — consider upgrading collector")
        messages = data["entries"]
        if isinstance(messages, list):
            messages = _legacy_entries_to_messages(name, system_id, messages)
    else:
        return _error(400, "Payload must contain 'messages' or 'entries'")
    if not isinstance(messages, list):
        return _error(400, "'messages' must be a list")

    max_messages = int(_config.get("max_messages_per_request", DEFAULT_MAX_MESSAGES))
    if len(messages) > max_messages:
        if any(_header_msg_id(m) is not None for m in messages):
            _logger.warning(
                f"Queue {queue_id} from {name} has {len(messages)} messages (max {max_messages}): "
                f"refused with 413 - lower max_batch_messages on the collector or raise "
                f"max_messages_per_request"
            )
            return _error(413, f"Too many messages in one request (max {max_messages})")
        # Old collector (no msg_ids): it can't split its queue, so a 413 would
        # repeat forever.  Accept it; max_request_bytes still bounds the body.
        _logger.info(
            f"Queue {queue_id} from {name} has {len(messages)} messages without msg_id "
            f"(over max_messages_per_request {max_messages}); accepted from an old collector"
        )

    results = _store_messages(name, system_id, messages, queue_id)
    counts = {"stored": 0, "duplicate": 0, "rejected": 0, "error": 0}
    for r in results:
        counts[r["status"]] += 1
    handled = counts["stored"] + counts["duplicate"] + counts["rejected"]

    _logger.info(
        f"Processed queue {queue_id} from {name}: received {len(messages)}, "
        f"stored {counts['stored']}, duplicate {counts['duplicate']}, "
        f"rejected {counts['rejected']}, error {counts['error']}"
    )

    return jsonify({
        "status":    "ok",
        "queue_id":  queue_id,
        "received":  len(messages),
        "stored":    handled,
        "counts":    counts,
        "results":   results,
        "timestamp": utc_now(),
    }), 200


def _store_messages(name: str, system_id, messages: list, queue_id: str) -> list:
    """
    Store a request's messages in order; returns one result per message.

    Group commit: consecutive messages share one transaction per database
    (storage_util.CommitGroup: the system's data.db and the central
    metrics.db, each message in its own savepoint, so a message is still
    stored or rolled back on its own), committed after
    ingest_commit_messages messages or INGEST_COMMIT_SECONDS, and at the
    end.  One transaction per message costs two fsyncs per message (its row,
    its central side effects); a 100-message batch now costs a few.  The
    results of a group count only once its COMMIT returned: if it fails,
    every message of the group is answered 'error' (kept by the collector
    and retried; re-applying central side effects is idempotent), whatever
    it would have been.  metrics.db commits first, as in the per-message
    path, so a row is never committed without its side effects.  Locks are
    taken data.db first, as in the per-message path (side effects inside
    the data.db transaction), even when a group's first message only writes
    metrics.db (a rejected message's dead letter): a group holding
    metrics.db while it waits for data.db would deadlock with another
    request for the same system holding data.db (CommitGroup lock_order).
    A message that needs a one-time step first (the system's database is
    not open yet, a migration retry, the rollup setup) is stored on its own,
    outside any group.  After the first 'error' (a storage failure) the rest would
    fail the same way, each after its own busy timeout: they are answered
    'error' without being attempted, and the collector keeps them.
    """
    results = []
    i, n = 0, len(messages)
    failed = False
    while i < n and not failed:
        db = _data_store.cached_connection(name) \
            if _ingest_commit_messages > 1 and n - i > 1 else None
        alone = db is None
        if not alone:
            start = i
            deadline = time.monotonic() + INGEST_COMMIT_SECONDS
            try:
                central = _metrics_db.connection
                with CommitGroup([central, db], lock_order=[db, central]) as group:
                    while i < n and i - start < _ingest_commit_messages and \
                            (i == start or time.monotonic() < deadline):
                        try:
                            result = _process_message(name, system_id, messages[i])
                        except GroupBarrier:
                            alone = True        # stored on its own once the group is committed
                            break
                        results.append(result)
                        i += 1
                        if result["status"] == "error":
                            failed = True
                            break
                    group.commit()
            except Exception as e:
                level = logging.WARNING if is_transient_db_error(e) else logging.ERROR
                _logger.log(level,
                            f"Queue {queue_id} from {name}: committing {i - start} message(s) failed "
                            f"({type(e).__name__}: {e}); they are answered 'error' (kept by the "
                            f"collector for retry)", exc_info=level == logging.ERROR)
                for r in results[start:]:
                    r["status"] = "error"
                    r["error"] = "storage failure; retry later"
                failed = True
                break
        if alone and not failed and i < n:
            results.append(_process_message(name, system_id, messages[i]))
            i += 1
            failed = results[-1]["status"] == "error"
    if i < n:
        rest = messages[i:]
        _logger.warning(
            f"Queue {queue_id} from {name}: storage failure; the remaining {len(rest)} "
            f"message(s) were not attempted (kept by the collector for retry)"
        )
        results.extend(_not_attempted(name, m) for m in rest)
    return results


def _header_msg_id(msg):
    """header.msg_id of a message, or None (no header / old collector)."""
    if isinstance(msg, dict) and isinstance(msg.get("header"), dict):
        return msg["header"].get("msg_id")
    return None


def _not_attempted(batch_name: str, msg) -> dict:
    """'error' result for a message skipped after a storage failure, under the id it would get."""
    msg_id = _header_msg_id(msg)
    if msg_id is None and isinstance(msg, dict):
        header, payload = msg.get("header") or {}, msg.get("data") or {}
        if isinstance(header, dict) and isinstance(payload, dict):
            msg_id = _legacy_id(batch_name, header, payload)
    elif not (isinstance(msg_id, str) and _MSG_ID_RE.fullmatch(msg_id)):
        msg_id = None
    return {"msg_id": msg_id, "status": "error", "error": "storage failure; retry later"}


def _legacy_id(batch_name: str, header: dict, payload: dict) -> str:
    """Deterministic id for messages from collectors that don't send msg_id."""
    parts = (
        header.get("device_name") or batch_name or "",
        payload.get("data_type") or "",
        header.get("timestamp") or "",
        payload.get("username") or "",
    )
    key = "|".join(str(p) for p in parts)
    return "legacy-" + hashlib.sha1(key.encode("utf-8")).hexdigest()


# Fields whose value must be a number or null when present (otherwise the
# message is rejected)
_NUMERIC_FIELDS = {
    'system_metrics': ('cpu_percent', 'ram_percent', 'total_ram_bytes', 'uptime_seconds',
                       'network_bytes_in', 'network_bytes_out',
                       'network_bandwidth_in_mbps', 'network_bandwidth_out_mbps', 'gpu_count',
                       'interval_s', 'cpu_count', 'cpu_iowait_percent', 'cpu_steal_percent',
                       'proc_cpu_coverage_percent', 'disk_read_bytes_per_s', 'disk_write_bytes_per_s',
                       'disk_read_iops', 'disk_write_iops', 'disk_await_ms', 'disk_busy_max_percent',
                       'gpu_sample_seconds'),
    'user_metrics':   ('cpu_percent', 'ram_bytes', 'cpu_seconds', 'cpu_cores', 'interval_s',
                       'process_count', 'gpu_vram_bytes', 'gpu_util_percent', 'gpu_busy_seconds',
                       'gpus_held', 'gpu_held_seconds', 'gpu_processes'),
    'folder_usage':   ('total_usage',),
    'sessions':       ('clients_total', 'users_total', 'rdp_disconnected'),
    'storage_health': ('overall_status_code',),
}
# Keys that are numeric only because their name matches a pattern: a value
# that is not a number (or null) is DROPPED and listed in "_dropped_fields",
# the rest of the message is stored (R8: a later collector may add a string
# key that happens to match, e.g. "disk_sata1_model").
_NUMERIC_PATTERNS = {
    'sessions':       re.compile(r'[a-z][a-z0-9]{0,15}_(?:clients|users)'),
    'storage_health': re.compile(r'(?:disk|array|volume)_[a-z0-9_]+'),
}

_HEALTH_STATUS = ('ok', 'warning', 'critical', 'unknown')
_LIST_STATES = ('full', 'unchanged', 'none')
_SERVICE_RE = re.compile(r'[a-z][a-z0-9]{0,15}')
# Hard caps on nested lists (larger = rejected) and R10 string limits
_MAX_GPU_USERS = 64
_MAX_SESSIONS = 5000
_HEALTH_LIST_CAPS = {'arrays': 128, 'disks': 512, 'volumes': 64}
_MAX_BREAKDOWN_FOLDERS = 10000
_MAX_BREAKDOWN_ROWS = 50000
_MAX_USERNAME = 256
_MAX_PATH = 4096
_MAX_COMPONENT_ID = 64
_MAX_CLIENT = 256
_MAX_REASONS, _MAX_REASON_LEN = 10, 200
_BREAKDOWN_NUMBERS = ('bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_known_fields(table: str, payload: dict):
    """Reject messages whose well-known fields have unusable values."""
    for field in _NUMERIC_FIELDS.get(table, ()):
        value = payload.get(field)
        if value is not None and not _is_number(value):
            raise RejectedMessage(f"field '{field}' must be a number or null")
    if table == 'user_metrics':
        username = payload.get('username')
        if not isinstance(username, str) or not username:
            raise RejectedMessage("user_metrics requires a non-empty 'username'")
        rf = payload.get('ram_formatted')
        if rf is not None and not isinstance(rf, str):
            raise RejectedMessage("field 'ram_formatted' must be a string or null")
    elif table == 'system_metrics':
        name = payload.get('disk_busy_max_name')
        if name is not None and not isinstance(name, str):
            raise RejectedMessage("field 'disk_busy_max_name' must be a string or null")
        gpus = payload.get('gpus')
        # JSON text is what stored rows hold (an older Manager kept any form;
        # tools that rebuild messages from rows re-send it): accepted as is
        if gpus is not None and not isinstance(gpus, (list, str)):
            raise RejectedMessage("field 'gpus' must be a list or null")
        for gpu in gpus if isinstance(gpus, list) else ():
            users = gpu.get('users') if isinstance(gpu, dict) else None
            if users is not None and (not isinstance(users, list) or len(users) > _MAX_GPU_USERS):
                raise RejectedMessage(f"field 'gpus[].users' must be a list of at most "
                                      f"{_MAX_GPU_USERS} entries or null")
    elif table == 'sessions':
        _check_list(payload, 'sessions', _MAX_SESSIONS)
        state = payload.get('_list_state')
        if state is not None and state not in _LIST_STATES:
            raise RejectedMessage("field '_list_state' must be one of " + ', '.join(_LIST_STATES))
        list_hash = payload.get('_list_hash')
        if list_hash is not None and (not isinstance(list_hash, str) or len(list_hash) > 64):
            raise RejectedMessage("field '_list_hash' must be a string of at most 64 characters or null")
        sources = payload.get('_sources')
        if sources is not None and not isinstance(sources, dict):
            raise RejectedMessage("field '_sources' must be an object or null")
    elif table == 'storage_health':
        status = payload.get('overall_status')
        if status is not None and status not in _HEALTH_STATUS:
            raise RejectedMessage("field 'overall_status' must be one of " + ', '.join(_HEALTH_STATUS))
        for key, cap in _HEALTH_LIST_CAPS.items():
            _check_list(payload, key, cap)
        reasons = payload.get('status_reasons')
        if reasons is not None and not isinstance(reasons, list):
            raise RejectedMessage("field 'status_reasons' must be a list or null")
    elif table == 'storage_breakdown':
        folders = _check_list(payload, 'folders', _MAX_BREAKDOWN_FOLDERS)
        device = payload.get('device')
        if device is not None and not isinstance(device, dict):
            raise RejectedMessage("field 'device' must be an object or null")
        rows = _breakdown_rows(device) if device else 0
        for folder in folders or ():
            if isinstance(folder, dict):
                other = folder.get('other')
                if other is not None and not isinstance(other, dict):
                    raise RejectedMessage("field 'folders[].other' must be an object or null")
                rows += _breakdown_rows(folder)
        if rows > _MAX_BREAKDOWN_ROWS:
            raise RejectedMessage(f"storage_breakdown has {rows} rows (max {_MAX_BREAKDOWN_ROWS})")


def _check_list(payload: dict, key: str, cap: int):
    value = payload.get(key)
    if value is not None and (not isinstance(value, list) or len(value) > cap):
        raise RejectedMessage(f"field '{key}' must be a list of at most {cap} entries or null")
    return value


def _breakdown_rows(entry: dict) -> int:
    """Narrow rows one storage_breakdown folder (or device) entry costs: total + owners + other."""
    owners = entry.get('owners')
    other = entry.get('other')
    return 1 + (len(owners) if isinstance(owners, list) else 0) + (1 if isinstance(other, dict) else 0)


def _finite_numbers(row: dict, keys=None) -> bool:
    """No non-finite number among row's values (or the given keys)."""
    values = row.values() if keys is None else (row.get(k) for k in keys)
    return not any(isinstance(v, float) and not math.isfinite(v) for v in values)


def _short_str(value, limit: int, required: bool = False) -> bool:
    if value is None:
        return not required
    return isinstance(value, str) and (value != '' or not required) and len(value) <= limit


def _ids_ok(value) -> bool:
    return value is None or (isinstance(value, list) and
                             all(_short_str(v, _MAX_COMPONENT_ID, required=True) for v in value))


def _gpu_user_ok(row) -> bool:
    return (isinstance(row, dict) and _short_str(row.get('username'), _MAX_USERNAME, required=True)
            and all(row.get(k) is None or _is_number(row.get(k))
                    for k in ('vram_bytes', 'util_percent', 'processes'))
            and _finite_numbers(row))


def _session_ok(row) -> bool:
    if not isinstance(row, dict) or not _finite_numbers(row):
        return False
    service = row.get('service')
    return (isinstance(service, str) and _SERVICE_RE.fullmatch(service) is not None
            and _short_str(row.get('username'), _MAX_USERNAME)
            and _short_str(row.get('client'), _MAX_CLIENT)
            and _short_str(row.get('client_host'), _MAX_CLIENT)
            and (row.get('connections') is None or _is_number(row.get('connections'))))


def _component_ok(row) -> bool:
    """A storage_health arrays / disks / volumes row (status in the enum, ids within R10)."""
    return (isinstance(row, dict) and row.get('status') in _HEALTH_STATUS
            and _short_str(row.get('id'), _MAX_COMPONENT_ID, required=True)
            and all(_ids_ok(row.get(k)) for k in ('members', 'faulty_members', 'arrays'))
            and _finite_numbers(row))


def _breakdown_numbers_ok(row: dict) -> bool:
    for key in _BREAKDOWN_NUMBERS:
        value = row.get(key)
        if value is None:
            continue
        if not _is_number(value) or not math.isfinite(value) or value < 0:
            return False
    return _finite_numbers(row)


def _owner_ok(row) -> bool:
    return (isinstance(row, dict) and _short_str(row.get('owner'), _MAX_USERNAME, required=True)
            and _breakdown_numbers_ok(row))


def _sanitize_breakdown_entry(entry: dict, is_folder: bool):
    """(sanitized copy, rows dropped) of a folder or device entry, or (None, 1) if unusable."""
    if not isinstance(entry, dict) or not _breakdown_numbers_ok(entry):
        return None, 1
    if is_folder and not (_short_str(entry.get('path'), _MAX_PATH, required=True)
                          and _short_str(entry.get('folder_owner'), _MAX_USERNAME)):
        return None, 1
    out, dropped = dict(entry), 0
    owners = entry.get('owners')
    if owners is not None:
        if not isinstance(owners, list):
            return None, 1
        kept, seen = [], set()
        for o in owners:
            # a second row for the same owner is dropped too (one narrow row per owner)
            if _owner_ok(o) and o['owner'] not in seen:
                seen.add(o['owner'])
                kept.append(o)
        out['owners'] = kept
        dropped += len(owners) - len(kept)
    other = entry.get('other')
    if isinstance(other, dict) and not _breakdown_numbers_ok(other):
        out['other'] = None
        dropped += 1
    return out, dropped


def _sanitize_known(table: str, payload: dict) -> dict:
    """
    A copy of a validated payload with the bad parts of the new data types
    removed (R8), so the same payload is stored and used for side effects:
      - pattern-matched numeric keys (sessions <svc>_clients / <svc>_users,
        storage_health disk_* / array_* / volume_*) holding anything but a
        number or null are dropped and listed in "_dropped_fields";
      - nested rows that are not objects, whose status is outside its enum,
        with strings over the R10 limits, non-finite numbers, or (in
        storage_breakdown) negative byte counts, a folder path or an owner
        already listed are dropped and counted in "_dropped_rows";
      - storage_health status_reasons keeps at most 10 strings of at most
        200 characters.
    Messages without such keys (every old collector's) come back unchanged.
    """
    out = dict(payload)
    pattern = _NUMERIC_PATTERNS.get(table)
    dropped_fields = []
    if pattern is not None:
        for key, value in payload.items():
            if isinstance(key, str) and pattern.fullmatch(key) and value is not None and not _is_number(value):
                del out[key]
                dropped_fields.append(key)
    dropped_rows = 0

    def keep(key, ok):
        nonlocal dropped_rows
        rows = out.get(key)
        if isinstance(rows, list):
            kept = [r for r in rows if ok(r)]
            dropped_rows += len(rows) - len(kept)
            out[key] = kept

    if table == 'system_metrics' and isinstance(out.get('gpus'), list):
        gpus = []
        for gpu in out['gpus']:
            if isinstance(gpu, dict) and isinstance(gpu.get('users'), list):
                users = [u for u in gpu['users'] if _gpu_user_ok(u)]
                dropped_rows += len(gpu['users']) - len(users)
                gpu = dict(gpu, users=users)
            gpus.append(gpu)
        out['gpus'] = gpus
    elif table == 'sessions':
        keep('sessions', _session_ok)
    elif table == 'storage_health':
        for key in _HEALTH_LIST_CAPS:
            keep(key, _component_ok)
        reasons = out.get('status_reasons')
        if isinstance(reasons, list):
            kept = [r[:_MAX_REASON_LEN] for r in reasons if isinstance(r, str)][:_MAX_REASONS]
            dropped_rows += sum(1 for r in reasons if not isinstance(r, str))
            out['status_reasons'] = kept
    elif table == 'storage_breakdown':
        if isinstance(out.get('folders'), list):
            folders, paths = [], set()
            for folder in out['folders']:
                clean, n = _sanitize_breakdown_entry(folder, is_folder=True)
                if clean is not None and clean['path'] in paths:
                    clean, n = None, 1          # the same folder twice: the first entry is kept
                dropped_rows += n
                if clean is not None:
                    paths.add(clean['path'])
                    folders.append(clean)
            out['folders'] = folders
        if isinstance(out.get('device'), dict):
            clean, n = _sanitize_breakdown_entry(out['device'], is_folder=False)
            dropped_rows += n
            out['device'] = clean

    if dropped_fields:
        earlier = payload.get('_dropped_fields')
        out['_dropped_fields'] = (earlier if isinstance(earlier, list) else []) + dropped_fields
    if dropped_rows:
        earlier = payload.get('_dropped_rows')
        out['_dropped_rows'] = (earlier if _is_number(earlier) else 0) + dropped_rows
    return out


def _process_message(batch_name: str, system_id, msg) -> dict:
    """
    Store one message and apply its central side effects.  Never raises:
    returns {"msg_id", "status"[, "error"]} where status is
      stored    — newly persisted (side effects applied)
      duplicate — msg_id already present (nothing re-applied)
      rejected  — permanently unstorable; saved to dead_letter
      error     — transient failure; not persisted, collector should retry
    """
    msg_id = None
    data_type = None
    try:
        if not isinstance(msg, dict) or not isinstance(msg.get("header", {}), dict) \
                or not isinstance(msg.get("data", {}), dict):
            header_id = _header_msg_id(msg)
            if isinstance(header_id, str) and _MSG_ID_RE.fullmatch(header_id):
                # Echo the collector's id so it can drop the message from its queue
                msg_id = header_id
            else:
                raw = json.dumps(msg, sort_keys=True, default=str)
                msg_id = "invalid-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()
            raise RejectedMessage("message must be an object with 'header' and 'data' objects")

        header  = msg.get("header") or {}
        payload = msg.get("data") or {}
        data_type = payload.get("data_type")  # may be None → not_specified table

        if header.get("msg_id") is not None:
            raw_id = header["msg_id"]
            msg_id = raw_id if isinstance(raw_id, (str, int)) else None
            if not isinstance(raw_id, str) or not _MSG_ID_RE.fullmatch(raw_id):
                raise RejectedMessage("invalid msg_id")
        else:
            msg_id = _legacy_id(batch_name, header, payload)

        dev_name = header.get("device_name", batch_name)
        if not _name_accepted(dev_name):
            raise RejectedMessage("invalid device_name")
        if normalize_device_name(dev_name) != normalize_device_name(batch_name):
            raise RejectedMessage("device_name does not match the queue's name")

        raw_ts = header.get("timestamp")
        if not raw_ts:
            raise RejectedMessage("missing timestamp")
        try:
            timestamp = canonical_timestamp(raw_ts)
        except ValueError:
            raise RejectedMessage("invalid timestamp")

        dev_id   = header.get("device_id", system_id)
        dev_type = header.get("device_type", "unknown")
        if not isinstance(dev_type, str) or isinstance(dev_id, (dict, list)):
            raise RejectedMessage("invalid device_type or device_id")

        table = _data_store.ingest_table(dev_name, data_type)
        _validate_known_fields(table, payload)
        payload = _sanitize_known(table, payload)

        central_name = _metrics_db.resolve_name(dev_name, dev_id)

        def side_effects():
            _metrics_db.apply_side_effects(
                central_name, dev_id, dev_type, timestamp, table, payload, msg_id,
                raw_timestamp=raw_ts)

        # Full history in the per-system typed table; central updates run
        # inside the same per-system transaction so a failure rolls back both.
        # raw_ts: an old Manager stored the header timestamp as sent; messages
        # it may already hold are matched against its rows in both forms.
        status = _data_store.store_message(
            dev_name, msg_id, timestamp, dev_type, data_type, payload, on_new=side_effects,
            raw_timestamp=raw_ts)
        if status == "stored":
            _logger.debug(f"Stored {table} message {msg_id} from {dev_name}")
        return {"msg_id": msg_id, "status": status}

    except GroupBarrier:
        raise           # nothing written: stored again outside the group commit (_store_messages)
    except RejectedMessage as e:
        return _reject(batch_name, msg_id, data_type, msg, str(e))
    except Exception as e:
        if is_transient_db_error(e):
            # Lock contention, I/O, permissions or a damaged database file:
            # not persisted, the collector keeps the message and retries.
            _logger.warning(
                f"Storage failure for message {msg_id} from {batch_name} "
                f"(kept for retry): {type(e).__name__}: {e}"
            )
            return {"msg_id": msg_id, "status": "error", "error": "storage failure; retry later"}
        _logger.error(f"Failed to store message {msg_id} from {batch_name}", exc_info=True)
        return _reject(batch_name, msg_id, data_type, msg,
                       f"storage failed ({type(e).__name__})", detail=str(e))


def _reject(batch_name, msg_id, data_type, msg, reason, detail=None) -> dict:
    """Save a rejected message to dead_letter; 'error' if even that fails."""
    _logger.warning(f"Rejected message {msg_id} from {batch_name}: {reason}")
    try:
        _metrics_db.add_dead_letter(
            batch_name, msg_id if isinstance(msg_id, str) else None, data_type,
            f"{reason}: {detail}" if detail else reason, msg)
    except GroupBarrier:
        raise
    except Exception as e:
        _logger.error(f"Could not record dead letter for {msg_id} from {batch_name}: {e}")
        return {"msg_id": msg_id, "status": "error", "error": "storage failure; retry later"}
    return {"msg_id": msg_id, "status": "rejected", "error": reason}


def _legacy_entries_to_messages(name: str, system_id: str, entries: list) -> list:
    """
    Backward-compat translation for v1 collector entries (flat dicts with a
    'type' field) into the {header, data} message shape.  Entries without a
    timestamp are passed through so they are counted as rejected.
    """
    # Map legacy type names to new data_type labels
    data_type_map = {
        "disk":    "folder_usage",
        "metrics": "system_metrics",
    }
    envelope_keys = {"type", "timestamp", "device_type"}
    messages = []
    for entry in entries:
        if not isinstance(entry, dict):
            messages.append(entry)
            continue
        entry_type = entry.get("type", "unknown")
        data_type = data_type_map.get(entry_type, entry_type)

        # Build payload: everything except the legacy envelope fields
        payload = {k: v for k, v in entry.items() if k not in envelope_keys}
        payload["data_type"] = data_type

        messages.append({
            "header": {
                "device_name": name,
                "device_id":   system_id,
                "device_type": entry.get("device_type", "unknown"),
                "timestamp":   entry.get("timestamp"),
            },
            "data": payload,
        })
    return messages


# ---------------------------------------------------------------------------
# Latest snapshots (central DB)
# ---------------------------------------------------------------------------

@app.route('/api/usage/all', methods=['GET'])
@require_scope('read')
def get_all_usage():
    """
    Get latest disk usage for all systems (requires Bearer token).

    Returns: {system_name: latest_disk_usage}
    """
    return jsonify(_metrics_db.get_latest_disk_usage_for_all()), 200


@app.route('/api/usage/nas/<nas_name>', methods=['GET'])
@require_scope('read')
def get_nas_latest(nas_name):
    """
    Get latest disk usage for a specific system (requires Bearer token).

    Returns: Latest report or 404 if not found
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return _error(400, "Invalid NAS name")
    latest = _metrics_db.get_latest_disk_usage(nas_name)
    if not latest:
        return _error(404, "System not found")
    return jsonify(latest), 200


@app.route('/api/usage/history/<nas_name>', methods=['GET'])
@require_scope('read')
def get_nas_history(nas_name):
    """
    Get folder_usage history for a system (requires Bearer token).

    Query parameters:
        - days: Number of days to return (default: 30)
        - limit: Max number of records (default: 20000)

    Returns: {"system_name", "days", "records": [flat folder_usage rows, oldest first], "truncated"}
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return _error(400, "Invalid NAS name")
    try:
        days = _int_arg('days', 30, maximum=36500)
        limit = _int_arg('limit', DEFAULT_ROW_LIMIT)
    except _BadArg as e:
        return _error(400, str(e))
    if not _system_known(nas_name):
        return _error(404, "System not found")

    start = format_timestamp(datetime.now(timezone.utc) - timedelta(days=days))
    records, truncated = _data_store.fetch_rows(
        nas_name, FOLDER_USAGE, start=start, limit=limit, newest_first=False)
    return jsonify({
        "system_name": nas_name,
        "days": days,
        "records": records,
        "truncated": truncated,
    }), 200


@app.route('/api/metrics/all', methods=['GET'])
@require_scope('read')
def get_all_metrics():
    """
    Get latest metrics for all systems (requires Bearer token).
    Returns the most recent metric entry for each system.

    Returns: {name: {cpu_percent, ram_percent, network_bytes_in, network_bytes_out, timestamp, ...}}
    Unmeasured values are null.
    """
    return jsonify(_metrics_db.get_latest_for_all()), 200


@app.route('/api/metrics/nas/<nas_name>', methods=['GET'])
@require_scope('read')
def get_nas_metrics(nas_name):
    """
    Get latest metrics for a specific system (requires Bearer token).

    Returns: {cpu_percent, ram_percent, network_bytes_in, network_bytes_out, timestamp}
    """
    if not validate_nas_name(nas_name):
        return _error(400, "Invalid NAS name")
    metric = _metrics_db.get_latest(nas_name)
    if metric:
        return jsonify(metric), 200
    return _error(404, "No metrics found for NAS")


@app.route('/api/user_metrics/all', methods=['GET'])
@require_scope('read')
def get_all_user_metrics():
    """Latest per-user CPU/RAM snapshot for all devices."""
    return jsonify(_user_metrics.get_all_snapshots()), 200


@app.route('/api/user_metrics/usage', methods=['GET'])
@require_scope('read')
def get_user_usage():
    """
    Per-user CPU and GPU totals over a window (requires Bearer token).

    Query parameters:
        - window: 24h (default), 7d or 30d (ending now)
        - device: only this system (404 if unknown)
        - top:    users per device, 1-200 (default 50)

    Returns: {"window", "from", "to", "devices": {"<name>": {"source": "rollups"|"raw",
              "complete": bool, "users": [{"username", "samples", "cpu_core_hours", "avg_cores",
              "peak_cores", "gpu_busy_hours", "gpu_held_hours", "peak_gpu_vram_bytes"}]}}}
    Users are sorted by gpu_busy_hours, then cpu_core_hours (descending, null
    last); a value with no measurement in the window is null.  From the
    hourly rollups once they are complete ("rollups": they include the hour
    the window starts in, so a total can hold up to one hour more), else
    from the raw rows ("raw", complete false while the rollup backfill is
    pending).  Answers are reused for usage_cache_seconds (default 300).
    This static route shadows a device named "usage" in
    /api/user_metrics/<device_name>.
    """
    window = request.args.get('window') or '24h'
    if window not in USAGE_WINDOWS:
        return _error(400, "'window' must be one of " + ', '.join(USAGE_WINDOWS))
    try:
        top = _int_arg('top', 50, minimum=1, maximum=200)
    except _BadArg as e:
        return _error(400, str(e))
    device = request.args.get('device')
    if device is not None:
        if not validate_nas_name(device):
            return _error(400, "Invalid device name")
        if not _system_known(device):
            return _error(404, "System not found")

    key = (window, device, top)
    now = time.monotonic()
    with _usage_cache_lock:
        hit = _usage_cache.get(key)
    if hit is not None and now - hit[0] < _usage_cache_seconds:
        return jsonify(hit[1]), 200
    answer = _compute_usage(window, device, top)
    if _usage_cache_seconds > 0:
        with _usage_cache_lock:
            for k in [k for k, (t, _) in _usage_cache.items() if now - t >= _usage_cache_seconds]:
                del _usage_cache[k]
            _usage_cache[key] = (now, answer)
    return jsonify(answer), 200


def _compute_usage(window: str, device, top: int) -> dict:
    seconds = USAGE_WINDOWS[window]
    now = datetime.now(timezone.utc)
    start, end = format_timestamp(now - timedelta(seconds=seconds)), format_timestamp(now)
    # every system with a history database, under the name the Dashboard knows it by
    targets = [(device, device)] if device is not None else \
        [(_metrics_db.resolve_read_name(d), d) for d in _data_store.list_systems()]
    devices = {}
    for name, system in targets:
        try:
            usage = _data_store.user_usage(system, start, end)
        except Exception:
            _logger.error(f"Usage totals of {system} failed", exc_info=True)
            continue
        if usage is not None:
            devices[name] = {'source': usage['source'], 'complete': usage['complete'],
                             'users': _usage_users(usage['users'], seconds, top)}
    return {'window': window, 'from': start, 'to': end, 'devices': devices}


def _usage_users(users: dict, seconds: int, top: int) -> list:
    def hours(v):
        return v / 3600.0 if v is not None else None

    out = []
    for username, u in users.items():
        cpu = u['sums']['cpu_seconds']
        vram = u['peaks']['gpu_vram_bytes']
        out.append({
            'username':            username,
            'samples':             u['samples'],
            'cpu_core_hours':      hours(cpu),
            'avg_cores':           cpu / seconds if cpu is not None else None,
            'peak_cores':          u['peaks']['cpu_cores'],
            'gpu_busy_hours':      hours(u['sums']['gpu_busy_seconds']),
            'gpu_held_hours':      hours(u['sums']['gpu_held_seconds']),
            'peak_gpu_vram_bytes': int(vram) if isinstance(vram, float) and vram.is_integer() else vram,
        })
    out.sort(key=lambda r: (r['gpu_busy_hours'] is None, -(r['gpu_busy_hours'] or 0),
                            r['cpu_core_hours'] is None, -(r['cpu_core_hours'] or 0), r['username']))
    return out[:top]


@app.route('/api/user_metrics/<device_name>', methods=['GET'])
@require_scope('read')
def get_device_user_metrics(device_name: str):
    """Latest per-user CPU/RAM snapshot for one device."""
    if not (validate_nas_name(device_name) or _user_metrics.has_device(device_name)):
        return _error(400, "Invalid device name")
    if not _user_device_known(device_name):
        return _error(404, "System not found")
    return jsonify(_user_metrics.get_snapshot(_metrics_db.resolve_read_name(device_name))), 200


@app.route('/api/user_metrics/history/<device_name>/<path:username>', methods=['GET'])
@require_scope('read')
def get_user_metrics_history(device_name: str, username: str):
    """Time-series CPU/RAM for one user on one device."""
    if not (validate_nas_name(device_name) or _user_metrics.has_device(device_name)):
        return _error(400, "Invalid device name")
    try:
        limit = _int_arg('limit', 200)
    except _BadArg as e:
        return _error(400, str(e))
    if not _user_device_known(device_name):
        return _error(404, "System not found")
    return jsonify(_user_metrics.get_history(_metrics_db.resolve_read_name(device_name), username, limit)), 200


# ---------------------------------------------------------------------------
# Typed data query endpoints (full history via TypedDataStore)
# ---------------------------------------------------------------------------

@app.route('/api/data/<system_name>', methods=['GET'])
@require_scope('read')
def get_system_data_types(system_name):
    """
    List the data_types that have been recorded for a system.
    Returns: {"system": "Triton", "data_types": ["folder_usage", "system_metrics"]}
    404 if the system is unknown.
    """
    if not validate_nas_name(system_name):
        return _error(400, "Invalid system name")
    if not _system_known(system_name):
        return _error(404, "System not found")
    return jsonify({"system": system_name, "data_types": _data_store.list_data_types(system_name)}), 200


@app.route('/api/data/<system_name>/<data_type>', methods=['GET'])
@require_scope('read')
def get_typed_data(system_name, data_type):
    """
    Get historical rows for a (system, data_type) pair.

    Query params:
        limit  int  Max rows to return (default 100 for a recent-rows query,
                    20000 when start/end are given)
        start  str  ISO 8601 start timestamp (inclusive)
        end    str  ISO 8601 end timestamp (exclusive)

    Without start/end: newest rows first.  With both: oldest first.
    folder_usage rows keep the flat shape {"id", "msg_id", "timestamp",
    "device_type", "_unit", "<folder path>": bytes, ...}.

    Returns: {"system": ..., "data_type": ..., "rows": [...], "truncated": bool}
    """
    if not validate_nas_name(system_name):
        return _error(400, "Invalid system name")
    if not _data_store.is_readable_data_type(system_name, data_type):
        return _error(400, "Invalid data type")
    try:
        start = _ts_arg('start')
        end   = _ts_arg('end')
        if start and end:
            limit = _int_arg('limit', DEFAULT_ROW_LIMIT)
        else:
            limit = _int_arg('limit', 100)
    except _BadArg as e:
        return _error(400, str(e))
    if not _system_known(system_name):
        return _error(404, "System not found")

    if start and end:
        rows, truncated = _data_store.fetch_rows(
            system_name, data_type, start, end, limit=limit, newest_first=False)
    else:
        rows, truncated = _data_store.fetch_rows(system_name, data_type, limit=limit)

    return jsonify({
        "system":    system_name,
        "data_type": data_type,
        "rows":      rows,
        "truncated": truncated,
    }), 200


@app.route('/api/systems', methods=['GET'])
@require_scope('read')
def list_systems():
    """
    List all registered systems.
    Returns: {"systems": ["Triton", "Atlas", ...]}
    """
    return jsonify({"systems": _metrics_db.get_all_systems()}), 200


@app.route('/api/devices', methods=['GET'])
@require_scope('read')
def get_devices():
    """
    Full device registry — name, system_id, device_type, first_seen, last_seen.
    Returns: {"devices": [{...}, ...]}
    """
    return jsonify({"devices": _metrics_db.get_all_devices()}), 200


@app.route('/api/totals', methods=['GET'])
@require_scope('read')
def get_totals():
    """
    Running cumulative totals — per-device and global.
    Returns:
        {
            "global": {total_bytes_in, total_bytes_out, total_disk_bytes},
            "devices": {"Triton": {...}, ...}
        }
    """
    return jsonify({
        "global":  _metrics_db.get_global_totals(),
        "devices": _metrics_db.get_all_totals(),
    }), 200


@app.route('/api/history/<system_name>/<data_type>/<path:field>', methods=['GET'])
@require_scope('read')
def get_metric_history(system_name: str, data_type: str, field: str):
    """
    Fetch time-series data for one field of a system's data_type.

    Args:
        system_name: System name (e.g., "triton5")
        data_type:   e.g. "system_metrics", "folder_usage"
        field:       e.g. "cpu_percent", "/volume1" (a leading '/' lost in
                     URL routing is restored automatically)

    Optional query params:
        ?from=<ISO8601>   Start of time range (inclusive). Omit for a tail query.
        ?to=<ISO8601>     End of time range (exclusive). Defaults to now when
                          from is provided but to is omitted.
        ?limit=N          Tail query (no from): the last N points (default 500);
                          "truncated" is true when older points exist.
                          Window query: at most N points (default 20000); if
                          more matched, the whole window is bucket-averaged
                          down to N points ("downsampled") rather than cut off.
        ?max_points=N     Bucket-average the selected range server-side into at
                          most N points (equal-width time buckets).
        ?resolution=R     auto (default), raw, hour or day.  With auto a window
                          query on a rolled-up field is answered from its
                          hourly / daily rollups when the points would be at
                          least an hour / a day wide; raw while the rollup
                          backfill is pending, for a tail query, for
                          user_metrics without entity and when the window
                          holds at most max_points values.  A rollup point
                          lies at the count-weighted centre of its hours /
                          days, never outside the window's first and last
                          value.
        ?entity=USER      Only this user's values (user_metrics; 1-256
                          printable characters).  Without it every user's
                          values are mixed into one series, as before.

    Returns:
        {
            "system_name": "triton5",
            "data_type": "system_metrics",
            "field": "cpu_percent",
            "unit": "%",
            "data": [
                {"timestamp": "2026-08-04T18:30:00.000000Z", "value": 45.2},
                ...
            ],
            "truncated": false,
            "downsampled": false,
            "entity": null, "resolution": "raw", "bucket_seconds": null, "downsampled_from": null
        }
    A point that averages several values (downsampled raw data, rollups)
    also has "min", "max" and "count"; bucket_seconds is the width of such a
    point and downsampled_from the number of values averaged.
    404 if the system is unknown.
    """
    if not validate_nas_name(system_name):
        return _error(400, "Invalid system name")
    if not _data_store.is_readable_data_type(system_name, data_type):
        return _error(400, "Invalid data type")
    try:
        from_param = _ts_arg('from')
        to_param   = _ts_arg('to')
        use_window = bool(from_param)
        limit      = _int_arg('limit', DEFAULT_ROW_LIMIT if use_window else 500)
        max_points = _int_arg('max_points', None, minimum=1, maximum=100000)
        resolution = request.args.get('resolution') or 'auto'
        if resolution not in HISTORY_RESOLUTIONS:
            raise _BadArg("'resolution' must be one of " + ', '.join(HISTORY_RESOLUTIONS))
        entity = request.args.get('entity')
        if entity is not None:
            if not 1 <= len(entity) <= MAX_ENTITY_LEN or not entity.isprintable():
                raise _BadArg(f"'entity' must be 1-{MAX_ENTITY_LEN} printable characters")
            if not (rollups.REGISTRY.get(_table_of(data_type)) or {}).get('entity'):
                raise _BadArg("'entity' is only supported for user_metrics")
    except _BadArg as e:
        return _error(400, str(e))
    if not _system_known(system_name):
        return _error(404, "System not found")

    if use_window and not to_param:
        to_param = utc_now()

    field_norm = unquote(field)  # decode any residual %XX encoding
    _logger.debug(
        f"Fetching history: {system_name}/{data_type}/{field_norm} "
        f"(from={from_param} to={to_param} limit={limit} max_points={max_points})"
    )

    series = _data_store.get_field_series(
        system_name, data_type, field_norm,
        start=from_param, end=to_param if use_window else None,
        limit=limit, max_points=max_points, entity=entity, resolution=resolution,
    )
    plain = {'entity': entity, 'resolution': 'raw', 'bucket_seconds': None, 'downsampled_from': None}

    if series is None:
        if data_type == FOLDER_USAGE:
            # TypedDataStore has no folder_usage rows for this system (possible
            # if the data_dir was inaccessible during earlier ingest).  Fall
            # back to the current snapshot in the metrics DB so the user sees
            # at least one data point instead of an empty chart.
            _logger.warning(
                f"No TypedDataStore records for {system_name}/folder_usage — "
                f"falling back to metrics-DB snapshot"
            )
            snapshot = _metrics_db.get_latest_disk_usage(system_name)
            if snapshot:
                ts = snapshot.get('timestamp')
                fn = field_norm if field_norm in snapshot else (
                    '/' + field_norm if '/' + field_norm in snapshot else field_norm)
                val_raw = snapshot.get(fn)
                if ts and val_raw is not None:
                    try:
                        return jsonify(dict({
                            'system_name': system_name,
                            'data_type':   data_type,
                            'field':       fn,
                            'unit':        'bytes',
                            'data':        [{'timestamp': ts, 'value': float(val_raw)}],
                            'truncated':   False,
                            'downsampled': False,
                        }, **plain)), 200
                    except (ValueError, TypeError):
                        pass
        return jsonify(dict({
            'system_name': system_name,
            'data_type':   data_type,
            'field':       field_norm,
            'data':        [],
            'truncated':   False,
            'downsampled': False,
        }, **plain)), 200

    return jsonify({
        'system_name': system_name,
        'data_type':   data_type,
        'field':       series['field'],
        'unit':        series['unit'],
        'data':        [_history_point(p) for p in series['points']],
        'truncated':   series['truncated'],
        'downsampled': series['downsampled'],
        'entity':      series['entity'],
        'resolution':  series['resolution'],
        'bucket_seconds':   series['bucket_seconds'],
        'downsampled_from': series['downsampled_from'],
    }), 200


def _history_point(p) -> dict:
    """(timestamp, value) or (timestamp, average, min, max, count) as a history point."""
    if len(p) == 5:
        return {'timestamp': p[0], 'value': p[1], 'min': p[2], 'max': p[3], 'count': p[4]}
    return {'timestamp': p[0], 'value': p[1]}


def _table_of(data_type) -> str:
    """The table a history data_type label reads (rollups.REGISTRY keys)."""
    try:
        return table_for_data_type(data_type)
    except RejectedMessage:
        return ''


@app.route('/api/rollups/status', methods=['GET'])
@require_scope('read')
def get_rollup_status():
    """
    Rollup backfill progress per system and table (requires Bearer token).

    Returns: {"<system directory>": {"<table>": {
                 "pending_ids":   ids still to roll up (id span, not a row count),
                 "done_ids":      ids rolled up so far (id span),
                 "frozen_before": retention cutoff (rows older are never rolled up again) or null,
                 "series":        number of rollup series,
                 "complete":      no backfill range pending,
                 "last_live_id":  newest row id rolled up live or queued}}}
    {} when no database has rollups yet (or rollups have never been enabled).
    """
    return jsonify(_data_store.rollup_status()), 200


# ---------------------------------------------------------------------------
# Latest state of the newer data types (sessions, storage health and
# storage breakdown).  A Manager before them answers 404, which the
# Dashboard reads as "feature not available".  The static .../all routes
# shadow a device named "all".
# ---------------------------------------------------------------------------

def _one_system(name: str, read):
    """The answer of read(name) for one system: 400 bad name, 404 unknown system or nothing reported."""
    if not validate_nas_name(name):
        return _error(400, "Invalid system name")
    if not _system_known(name):
        return _error(404, "System not found")
    entry = read(name)
    if entry is None:
        return _error(404, "Nothing reported for this system yet")
    return jsonify(entry), 200


@app.route('/api/sessions/all', methods=['GET'])
@require_scope('read')
def get_all_sessions():
    """
    Who is connected, per system (requires Bearer token).
    Returns: {name: {"timestamp", "list_timestamp", "list_confirmed_at",
              "summary": {counts, _sources, ...}, "sessions": [rows]}}
    list_timestamp: when the list was last sent in full; list_confirmed_at:
    when a later message last confirmed it unchanged (the list's real age).
    """
    return jsonify(_metrics_db.get_all_sessions()), 200


@app.route('/api/sessions/<name>', methods=['GET'])
@require_scope('read')
def get_system_sessions(name: str):
    """One entry of /api/sessions/all; 404 if the system is unknown or sent no sessions yet."""
    return _one_system(name, _metrics_db.get_sessions)


@app.route('/api/storage/health/all', methods=['GET'])
@require_scope('read')
def get_all_storage_health():
    """
    Latest storage health per system (requires Bearer token).
    Returns: {name: {"timestamp", "overall_status", "status_reasons", "sources",
              "arrays", "disks", "volumes", "events"}} (the flattened history keys are left out)
    """
    return jsonify(_metrics_db.get_all_storage_health()), 200


@app.route('/api/storage/health/<name>', methods=['GET'])
@require_scope('read')
def get_system_storage_health(name: str):
    """One entry of /api/storage/health/all; 404 if the system is unknown or sent none yet."""
    return _one_system(name, _metrics_db.get_storage_health)


@app.route('/api/storage/breakdown/all', methods=['GET'])
@require_scope('read')
def get_all_storage_breakdown():
    """
    Storage attribution summary per system (requires Bearer token).
    Returns: {name: {"timestamp", "owner_source", "reference_time", "cold_thresholds_days",
              "device": {bytes, files, cold_1y_bytes, cold_3y_bytes, owners (top 25), other},
              "folders": [{path, folder_owner, bytes, files, cold_1y_bytes, cold_3y_bytes,
                           top_owner, top_owner_bytes}],
              "folders_truncated", "volume_owners"}}
    At most 50 KB per system: folders sorted by bytes (descending) and cut.
    """
    return jsonify(_metrics_db.get_all_breakdown()), 200


@app.route('/api/storage/breakdown/<name>', methods=['GET'])
@require_scope('read')
def get_system_storage_breakdown(name: str):
    """
    The latest storage_breakdown run of one system from its history database,
    with every owner row (requires Bearer token).

    Query parameters:
        - folder: only this folder (a path of at most 4096 characters; 404 if
                  the latest run does not list it)

    Returns: {"system", "id", "msg_id", "timestamp", "device_type", ...the run as sent:
              "reference_time", "owner_source", "cold_thresholds_days", "device",
              "folders": [{path, folder_owner, bytes, ..., "owners": [...], "other"}], ...}
    """
    folder = request.args.get('folder')
    if folder is not None and (not folder or len(folder) > MAX_FOLDER_LEN or '\x00' in folder):
        return _error(400, f"'folder' must be a path of 1-{MAX_FOLDER_LEN} characters")

    def read(n):
        run = _data_store.latest_storage_breakdown(n, folder)
        if run is None or (folder is not None and not run.get('folders')):
            return None
        return dict(run, system=n)
    return _one_system(name, read)


@app.route('/api/dead_letter', methods=['GET'])
@require_scope('admin')
def get_dead_letters():
    """Most recent rejected messages with their reason (admin only)."""
    try:
        limit = _int_arg('limit', 100, maximum=10000)
    except _BadArg as e:
        return _error(400, str(e))
    return jsonify({"dead_letters": _metrics_db.get_dead_letters(limit)}), 200


@app.route('/api/system/<name>', methods=['DELETE'])
@require_scope('admin')
def delete_system(name: str):
    """
    Permanently delete all data for a system (admin token required).

    Removes:
      - Per-system history DB:  data/<normalized name>/data.db
      - Central summary rows:   devices, device_snapshot, device_totals,
                                device_user_snapshot, user_metrics_history and
                                the sessions / storage health / storage
                                breakdown snapshots of this one device (see
                                MetricsDB.delete_system)

    An older Manager could register two spellings that normalize alike
    ('lab-a' and 'lab_a', or a name from before a rename).  They share one
    history directory, so while another such spelling stays registered its
    data.db is kept ("per_system_db_kept_for"); only the central rows of the
    named device go.  Deleting the last spelling removes the data.db too.

    This is irreversible. The collector's local JSONL archives are the
    source of truth and can be used to rebuild the manager if needed.

    Returns:
        {"status": "ok", "deleted": "<name>", "per_system_db_deleted": bool,
         "central_db_rows_cleared": bool[, "per_system_db_kept_for": [names]]}
    """
    if not validate_nas_name(name):
        return _error(400, "Invalid system name")

    _logger.warning(f"DELETE /api/system/{name} — permanently deleting all data for '{name}'")

    target = _metrics_db.resolve_read_name(name)
    sharing = [n for n in _metrics_db.spellings(name) if n not in (name, target)]
    rows_deleted = _metrics_db.delete_system(name)
    if sharing:
        db_deleted = False
        _logger.warning(
            f"Kept the history database of '{name}': it is shared with the other registered "
            f"device name(s) {', '.join(sharing)} (same normalized name); delete those too to remove it")
    else:
        db_deleted = _data_store.delete_system(name)

    with _usage_cache_lock:
        _usage_cache.clear()
    _logger.info(f"System '{name}' wiped: db_deleted={db_deleted}, central_rows_cleared={rows_deleted}")

    body = {
        "status":  "ok",
        "deleted": name,
        "per_system_db_deleted": db_deleted,
        "central_db_rows_cleared": rows_deleted,
    }
    if sharing:
        body["per_system_db_kept_for"] = sharing
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# Snapshots to a second location (off unless snapshot.enabled)
# ---------------------------------------------------------------------------

@app.route('/api/snapshot', methods=['GET'])
@require_scope('read')
def get_snapshot_status():
    """
    Snapshot settings and the outcome of the last runs:
        {"enabled", "dest_dir", "time", "keep_daily", "keep_monthly",
         "running", "running_since", "next_run", "last_attempt",
         "last_success", "last_snapshot", "last_bytes", "last_files",
         "last_seconds", "last_failure", "last_error", "hours_since_success"}
    Times are UTC ISO-8601.  A last_failure newer than last_success means the
    most recent run failed (last_error says why).
    """
    return jsonify(_snapshots.describe()), 200


@app.route('/api/snapshot', methods=['POST'])
@require_scope('admin')
def run_snapshot():
    """Start a snapshot now, in the background (admin token).  202, or 409."""
    if not _snapshots.settings['enabled']:
        return _error(409, "Snapshots are disabled (set snapshot.enabled and snapshot.dest_dir)")
    if _snapshots._thread is None or not _snapshots._thread.is_alive():
        # Not started by main() (e.g. under a test client): run on a one-off thread
        if _snapshots.running_since is not None:
            return _error(409, "A snapshot is already running")
        threading.Thread(target=_snapshots.run, args=('manual',),
                         name='nas-snapshot-manual', daemon=True).start()
    elif not _snapshots.trigger():
        return _error(409, "A snapshot is already running")
    _logger.info(f"Snapshot requested via POST /api/snapshot from {request.remote_addr}")
    return jsonify({"status": "started", "dest_dir": _snapshots.settings['dest_dir']}), 202


# ---------------------------------------------------------------------------
# History retention (off unless history_retention_days is set)
# ---------------------------------------------------------------------------

def start_retention_thread(config: dict, logger: logging.Logger):
    """Run archive_metrics.run_history_retention once a day if configured."""
    days = config.get('history_retention_days')
    if not days:
        logger.info("History retention disabled (history_retention_days not set): history is kept forever")
        return None

    from archive_metrics import run_history_retention
    data_dir = config.get("data_dir", "data")

    def loop():
        time.sleep(60)   # let startup and the first collector posts settle
        while True:
            try:
                run_history_retention(_data_store, _metrics_db, data_dir, days,
                                      logging.getLogger('archive_metrics'))
            except Exception:
                logger.error("History retention run failed", exc_info=True)
            time.sleep(24 * 3600)

    logger.warning(
        f"History retention ENABLED: rows older than {days} days are archived to "
        f"JSONL under <data_dir>/<system>/archive/ and then deleted (daily)"
    )
    t = threading.Thread(target=loop, name='history-retention', daemon=True)
    t.start()
    return t


def start_rollup_backfill_thread(config: dict, logger: logging.Logger):
    """
    Roll up, in the background, the history stored before rollups existed
    (or while they were off): bounded batches of rollups.backfill_batch_ids
    ids, each one short write transaction, at most rollups.backfill_max_duty
    of the time.  Progress: GET /api/rollups/status.
    """
    global _rollup_backfill
    if _data_store is None or _data_store.rollups is None:
        return None
    _rollup_backfill = rollups.start_backfill_thread(_data_store, logging.getLogger('rollups'))
    settings = rollups.settings_from_config(config)
    logger.info(f"Rollup backfill running in the background (batches of {settings['backfill_batch_ids']} "
                f"ids, max duty {settings['backfill_max_duty']}); progress: GET /api/rollups/status")
    return _rollup_backfill


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="Lab Monitor Manager")
    parser.add_argument("--config", default="config.json", help="Config file")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate the config (tokens, data_dir) and exit")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Setup logging.  --check-config logs to the console only: it never
    # touches the log file (the installer runs it while the old Manager may
    # still have that file open) or any database.
    logger = setup_logging(
        None if args.check_config else config.get("log_file"),
        config.get("log_level", "INFO"),
        console_level=config.get("console_log_level"),
        max_bytes=int(config.get("log_max_bytes", 10 * 1024 * 1024)),
        backup_count=int(config.get("log_backup_count", 5)),
    )

    # Initialize app (refuses unsafe tokens). Exit code 2 = config refused;
    # the Task Scheduler launcher does not restart on 2.  Exit code 3 =
    # another Manager holds this data_dir (the launcher retries later).
    instance_lock = None
    try:
        if args.check_config:
            validate_config(config, logger)
            print("Config OK")
            return
        # One Manager per data_dir, decided BEFORE any database is opened
        # (the first start after an upgrade backs up and migrates them); the
        # PID file then names this process for the whole startup.  The lock
        # stays referenced (held) until main() returns or the process ends.
        instance_lock = InstanceLock(config.get("data_dir", "data")).acquire()
        pid_file = write_pid_file()
        if pid_file:
            logger.info(f"PID: {os.getpid()} (written to {pid_file})")
        init_app(config, logger)
    except ConfigError as e:
        logger.critical(str(e))
        sys.exit(2)
    except InstanceLockError as e:
        logger.critical(
            f"Another Manager is already running on data_dir {config.get('data_dir', 'data')!r} "
            f"({e.holder or 'holder unknown'}; lock file {e.path}). Two Managers must never use "
            f"one data_dir: not starting. If no Manager should be running, end that process.")
        sys.exit(3)
    except (OSError, sqlite3.Error) as e:
        # Exit 1: the launcher retries (e.g. a share that isn't reachable yet at boot)
        logger.critical(
            f"Cannot open data_dir {config.get('data_dir', 'data')!r}: {type(e).__name__}: {e}. "
            f"If it is on a network share, the account running the Manager needs access to it "
            f"(a Task Scheduler task without a stored password has none; see manager/README.md, "
            f"'Running from Task Scheduler')."
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Lab Monitor Manager starting")
    logger.info(f"Data directory: {config.get('data_dir')}")
    logger.info(f"SQLite {sqlite3.sqlite_version} (Python sqlite3 module)")
    logger.info("=" * 60)

    start_retention_thread(config, logger)
    start_rollup_backfill_thread(config, logger)
    _snapshots.start()

    host = config.get("host", "0.0.0.0")
    port = config.get("port", 5000)
    debug = config.get("debug", False)

    logger.info(f"Listening on {host}:{port}")
    try:
        if debug:
            raise ImportError("debug mode requested")
        from waitress import serve
    except ImportError as e:
        logger.warning("!" * 60)
        logger.warning(f"Running on the Flask DEVELOPMENT server ({e}). "
                       f"Install waitress (pip install -r requirements.txt) for production.")
        logger.warning("!" * 60)
        app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
        return

    # Flask enforces max_request_bytes (JSON 413); waitress stops reading
    # bodies far beyond it.
    max_body = app.config['MAX_CONTENT_LENGTH']
    serve(
        app, host=host, port=port,
        threads=int(config.get("threads", 8)),
        max_request_body_size=max(2 * max_body, max_body + WAITRESS_BODY_MARGIN),
        ident="lab-monitor-manager",
    )


if __name__ == "__main__":
    main()
