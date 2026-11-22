#!/usr/bin/env python3
r"""
Lab Monitor Collector

Runs on each NAS/Windows system with two modes:

DISK MODE (daily):
  1. Measures disk usage of configured folders (auto-discovers if not specified),
     and in the same walk the storage breakdown: file counts, cold data and
     usage per file owner (a second message, storage_breakdown)
  2. Appends to local archive (monthly JSONL)
  3. Appends entry to queue.json
  4. POSTs the queue to the Manager in batches
  5. Removes each message from queue.json once the Manager acknowledges it

METRICS MODE (every 5 minutes):
  1. Measures CPU (averaged since the previous run, from OS counters), RAM,
     network traffic, disk I/O, GPUs, and CPU-seconds / RAM / GPU use per user
  2. Appends system and user metrics to the local archive (monthly JSONL) and
     queue.json: from here on they are safe whatever else happens
  3. Runs the optional collectors (who is connected, storage health), each in
     a thread with its own time budget, and records their messages too
  4. POSTs the queue to the Manager in batches
  5. Removes each message from queue.json once the Manager acknowledges it

HEALTH MODE (by hand): a full storage-health check, recorded and sent at once.

Usage:
    python collector.py --config config.json --mode disk     # Daily
    python collector.py --config config.json --mode metrics  # Every 5 min
    python collector.py --config config.json --mode health   # Storage health now
    Add --dry-run (any mode) to print the messages as JSON on stdout instead:
    nothing is archived, queued, sent or saved.

Configuration:
    If 'volumes' is omitted, collector auto-discovers volume* directories
    (Synology: /volume1, /volume2, etc.)
    (Windows: E:, F:, etc.)
    (Linux: real filesystems from the mount table: /, /home, /data, ...)
    The platform is detected (config "platform" overrides it).
"""
import sys
import os
import json
import logging
import logging.handlers
import argparse
import importlib
import re
import socket
import threading
import time
import uuid
from collections import Counter, deque, namedtuple
from datetime import datetime, timezone
from typing import Optional

# Import the collector's own lib/ as a package. When collector.py is run as a
# script Python already puts its directory first on sys.path; it is only
# appended here (never inserted ahead of the stdlib/site-packages, so nothing
# dropped into lib/ can shadow them) for when collector.py is loaded from
# elsewhere, e.g. by the tests.
_COLLECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
if _COLLECTOR_DIR not in sys.path:
    sys.path.append(_COLLECTOR_DIR)

from lib import local_queue
from lib import disk_usage
from lib import gpu
from lib import perf_counters
from lib import platform_info
from lib import proc_accounting
from lib import settings
from lib import storage_breakdown
from lib.disk_usage import (discover_folders, discover_volumes, format_bytes,
                            measure_volume_capacity, scan_folders)

try:
    import requests
except ImportError:   # reported with a clear message by sync_queue_to_manager()
    requests = None

# Module-level logger for standalone helpers (e.g. fallback_* functions) that
# are not passed a logger. Child of the root logger configured by
# setup_logging(), so it inherits the same console/file handlers.
logger = logging.getLogger("collector")

DEFAULT_MAX_BATCH_MESSAGES = 100    # config "max_batch_messages"
DEFAULT_REQUEST_TIMEOUT    = 30     # config "request_timeout_seconds"
DEFAULT_TIMEOUT_SECONDS    = 3600   # config "timeout_seconds" (per folder)
# Minimum default "total_timeout_seconds", in multiples of timeout_seconds
# (see _default_total_timeout)
TOTAL_TIMEOUT_MULTIPLIER   = 3
LOG_MAX_BYTES              = 5 * 1024 * 1024
LOG_BACKUP_COUNT           = 5

# Same pattern the Manager validates device names against
DEVICE_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')

# ACK statuses meaning "durably handled by the Manager" → drop from queue.json.
# 'error' (transient, not persisted) stays queued for retry.
_HANDLED_STATUSES = frozenset({'stored', 'duplicate', 'rejected'})

# Apply ACKed removals to queue.json every N batches (and at the end), so a
# large backlog isn't re-read and rewritten after every single batch. Anything
# re-sent after a crash is deduplicated by the Manager via msg_id.
_REMOVE_FLUSH_EVERY = 10

# Template values that mean the token was never filled in
_PLACEHOLDER_MARKERS = ('CHANGE-ME', 'YOUR-', 'GENERATE-RANDOM', 'SAME-TOKEN')

# Linux defaults (install-collector-linux.sh sets both explicitly).
# 'Server' is the device type the Dashboard groups under "Servers".
DEFAULT_LINUX_DATA_DIR = '/var/lib/lab-monitor'
_DEFAULT_DEVICE_TYPES = {'linux': 'Server'}


class _MultiProcessRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler that tolerates other collector processes holding the
    log open (a multi-hour disk run overlaps the 5-minute metrics runs).

    The stock rollover shifts collector.log.1..N first and renames the live
    file last; on Windows that last rename fails while another process has the
    file open, so every attempt would shift (and eventually delete) the
    backups and drop the record being logged. Here the live file is renamed
    first; if that fails we keep appending and stop trying for this process.
    """
    _rollover_failed = False

    def shouldRollover(self, record):
        if self._rollover_failed:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        staging = self.baseFilename + '.rotating'
        try:
            os.replace(self.baseFilename, staging)
        except OSError:
            self._rollover_failed = True
        else:
            for i in range(self.backupCount - 1, 0, -1):
                src = f"{self.baseFilename}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{self.baseFilename}.{i + 1}")
            os.replace(staging, f"{self.baseFilename}.1")
        self.stream = self._open()


def setup_logging(log_file: str = None, log_level: str = "INFO"):
    """Configure logging to console and a size-rotated file (5 MB x 5 backups)."""
    logger = logging.getLogger()
    level = getattr(logging, str(log_level).upper(), None) if log_level else None
    bad_level = not isinstance(level, int) or isinstance(level, bool)
    logger.setLevel(logging.INFO if bad_level else level)
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
            file_handler = _MultiProcessRotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning(f"Cannot open log file {log_file} ({e}); logging to console only")

    if bad_level:
        logger.warning(f"Invalid log_level {log_level!r} in config; using INFO")
    
    return logger


def get_local_ip() -> str:
    """Detect the primary outbound IP address (cross-platform).

    Opens a UDP socket toward an external address to identify which
    local interface would be used. No data is actually sent.
    Falls back to hostname resolution, then 'unknown' on failure.
    """
    try:
        # Context manager guarantees the socket fd is closed even if
        # getsockname() raises, preventing fd leaks on repeated failures.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return 'unknown'


def _utcnow() -> datetime:
    """Current UTC time as a naive datetime (same value as the deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_header(name: str, system_id: str, device_type: str) -> dict:
    """
    Build standard message header: a unique msg_id (assigned once, at
    creation, and kept through every retry so the Manager can deduplicate),
    timestamp and IP address.
    """
    return {
        'msg_id':      local_queue.new_msg_id(),
        'device_name': name,
        'device_id':   system_id,
        'device_type': device_type,
        'ip_address':  get_local_ip(),
        'timestamp':   _utcnow().isoformat() + 'Z',
    }


def get_platform(config: dict) -> str:
    """
    'synology', 'linux' or 'windows': config "platform" when set, otherwise
    detected (Windows; Synology when a DSM marker file such as
    /etc/synoinfo.conf exists; any other host is 'linux'). The one answer
    every module uses: lib/platform_info.platform_kind.
    """
    return platform_info.platform_kind(config)


def get_data_dir(config: dict, create: bool = True) -> str:
    """Get (and unless create=False, create) the data directory from config"""
    if sys.platform == 'win32':
        # Windows
        data_dir = config.get('data_dir', 'data')
    elif 'data_dir' in config:
        data_dir = config['data_dir']
    else:
        # Synology/Linux
        data_dir = (DEFAULT_LINUX_DATA_DIR if get_platform(config) == 'linux'
                    else '/volume1/lab-monitor/data')
    
    if create:
        os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _device_dir(config: dict, nas_name: str, create: bool = True) -> str:
    """data/<name>/, where this device's queue, archive and state files live."""
    nas_dir = os.path.join(get_data_dir(config, create), nas_name)
    if create:
        os.makedirs(nas_dir, exist_ok=True)
    return nas_dir


def get_archive_path(config: dict, nas_name: str) -> str:
    """Get path to monthly archive JSONL for this NAS"""
    data_dir = get_data_dir(config)
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    
    year_month = _utcnow().strftime("%Y-%m")
    return os.path.join(nas_dir, f"{year_month}.jsonl")


def get_queue_path(config: dict, nas_name: str) -> str:
    """Get path to queue.json for this NAS"""
    data_dir = get_data_dir(config)
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    return os.path.join(nas_dir, "queue.json")


def append_to_archive(archive_path: str, entry: dict, logger: logging.Logger) -> bool:
    """Append entry to monthly JSONL archive"""
    try:
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with local_queue.open_for_append(archive_path) as f:   # never through a symlink
            f.write(json.dumps(entry) + '\n')
        return True
    except Exception as e:
        logger.error(f"Failed to append to archive {archive_path}: {e}")
        return False


def _cfg_number(config: dict, key: str, default, minimum=None, integer: bool = False):
    """Read a numeric config value; fall back to default (with a warning) if it's invalid."""
    raw = config.get(key)
    if raw is None:
        return default
    try:
        if isinstance(raw, bool):
            raise ValueError(key)
        value = int(raw) if integer else float(raw)
        if minimum is not None and value < minimum:
            raise ValueError(key)
        return value
    except (TypeError, ValueError):
        logger.warning(f"Invalid {key}={raw!r} in config; using {default!r}")
        return default


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    """Read a true/false config value; fall back to default (with a warning) if it's not a boolean."""
    raw = config.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    logger.warning(f"Invalid {key}={raw!r} in config (expected true or false); using {default!r}")
    return default


def _identity(config: dict, logger: logging.Logger):
    """Return (name, system_id, device_type) from config, or None if 'name' is missing."""
    # Support both 'name' and old 'nas_name' for backward compatibility
    name = config.get('name') or config.get('nas_name')
    system_id = config.get('id') or config.get('nas_id')
    # Default: 'Server' on Linux, else 'unknown'
    device_type = config.get('device_type') or _DEFAULT_DEVICE_TYPES.get(get_platform(config), 'unknown')

    if not name:
        logger.error("Config missing 'name' field (or old 'nas_name'). Update config.json.")
        return None
    if not DEVICE_NAME_RE.fullmatch(str(name)):
        token_key = re.sub(r'[^A-Za-z0-9_.-]', '_', str(name))[:64]
        logger.warning(
            f"Device name {name!r} is outside the allowed pattern (1-64 characters from "
            f"A-Z a-z 0-9 _ . -). An upgraded Manager accepts it only if a previous Manager "
            f"already registered exactly this spelling, so an existing device keeps working; "
            f"for a new device it refuses the messages (HTTP 400). Renaming ('name' in "
            f"config.json) starts a new device with its own, empty history. A collector_tokens "
            f"entry for this device must be keyed {token_key!r}."
        )
    return name, system_id, device_type


def _fmt(value, suffix: str = '') -> str:
    """Format a possibly-unmeasured value for log lines."""
    return 'n/a' if value is None else f"{value}{suffix}"


def _record(config: dict, name: str, entries: list, logger: logging.Logger) -> bool:
    """
    Write new messages to the permanent archive and the queue (no sync).
    
    Both appends happen under the queue lock so concurrent collector runs
    can't interleave archive lines or lose each other's queue entries. The
    archive is written even if the queue can't be updated. True once the
    messages are queued.
    """
    try:
        archive_path = get_archive_path(config, name)
        queue_path = get_queue_path(config, name)
    except OSError as e:
        logger.error(
            f"Cannot create the data directory for {name} (data_dir={config.get('data_dir')!r}): "
            f"{e}. {len(entries)} message(s) were neither archived nor queued."
        )
        return False
    max_queue = _cfg_number(config, 'max_queue_messages',
                            local_queue.DEFAULT_MAX_QUEUE_MESSAGES, minimum=0, integer=True)

    archived = False
    try:
        with local_queue.QueueLock(queue_path):
            for entry in entries:
                append_to_archive(archive_path, entry, logger)
            archived = True
            queued = local_queue.enqueue_locked(queue_path, entries, max_queue, logger)
    except Exception as e:
        if not archived:
            for entry in entries:   # the permanent record must not depend on the queue
                append_to_archive(archive_path, entry, logger)
        logger.error(
            f"Could not add {len(entries)} message(s) to {queue_path}: {e}. They are in "
            f"{archive_path} but will not be sent to the Manager automatically.",
            exc_info=not isinstance(e, local_queue.QueueLockTimeout),
        )
        return False

    logger.info(f"Archived {len(entries)} message(s) to {archive_path}; queue now holds {queued}")
    return True


def _sync(config: dict, name: str, system_id: str, logger: logging.Logger) -> bool:
    """Post the queue to the Manager (sync_queue_to_manager)."""
    try:
        queue_path = get_queue_path(config, name)
    except OSError as e:
        logger.error(f"Cannot create the data directory for {name} (data_dir={config.get('data_dir')!r}): "
                     f"{e}. Nothing was sent to the Manager.")
        return False
    return sync_queue_to_manager(config, name, system_id, queue_path, logger)


def _record_and_sync(config: dict, name: str, system_id: str, entries: list,
                     logger: logging.Logger, sync: bool = True) -> bool:
    """
    _record() the messages, then _sync() the queue (disk mode). sync=False
    (--no-sync) only records them: the next metrics run sends the queue.
    """
    if not _record(config, name, entries, logger):
        return False
    if not sync:
        logger.info("Not sending now (--no-sync): the next metrics run sends the queue to the Manager")
        return True
    return _sync(config, name, system_id, logger)


def _print_messages(entries: list) -> None:
    """--dry-run: the messages as one JSON list on stdout (the log goes to stderr)."""
    print(json.dumps(entries, indent=2), flush=True)


def _path_key(path: str) -> str:
    """Normalised path for equality checks ('E:\\' ~ 'e:', '/volume1/' ~ '/volume1')."""
    key = os.path.normcase(os.path.normpath(path))
    return key.rstrip('\\/') or key


def _measure_volumes(volumes: list, logger: logging.Logger) -> tuple:
    """
    Volume capacity (used / free / total) via shutil for each volume.

    Returns (volume_used, volume_capacity):
      volume_used:     {vol: used_bytes}
      volume_capacity: {"{vol}_total_bytes": ..., "{vol}_free_bytes": ...}
    Volumes whose capacity can't be read are left out (not reported as 0).
    """
    volume_capacity = {}
    volume_used     = {}
    for vol in volumes:
        cap = measure_volume_capacity(vol)
        if cap:
            volume_capacity[f"{vol}_total_bytes"] = cap['total_bytes']
            volume_capacity[f"{vol}_free_bytes"]  = cap['free_bytes']
            volume_used[vol] = cap['total_bytes'] - cap['free_bytes']
            logger.info(
                f"Volume {vol}: used={volume_used[vol]:,}  "
                f"total={cap['total_bytes']:,}  free={cap['free_bytes']:,}"
            )
    return volume_used, volume_capacity


def _measure_scan_paths(scan_paths: list, volumes: list, volume_used: dict, nas_type: str,
                        timeout: float, total_timeout: float, logger: logging.Logger,
                        breakdown=None) -> dict:
    """
    Folder usage, driven by 'scan_paths'.

    Each entry is either:
      "/volume1/Data1"    → measure that folder as a single total
      "/volume1/Data1/*"  → measure each immediate subfolder separately

    Backward-compat: if scan_paths is absent, fall back to scanning one level
    deep in each volume (old behaviour).

    Budgets: each folder gets at most `timeout` seconds and the whole scan
    `total_timeout` seconds. A folder that times out, or isn't reached before
    the overall budget runs out, goes in 'timed_out'; one that can't be read
    (or a configured path that doesn't exist) goes in 'errors', as does a glob
    root or volume that can't be listed. Neither is reported as 0 bytes, and
    later entries are still measured.

    A folder that is also a volume root with measured capacity (e.g.
    scan_paths ["/volume1"] with volumes ["/volume1"]) is not scanned: both
    would be reported under the same key, and the volume's used bytes from the
    filesystem are authoritative (they include snapshots, metadata and the
    system folders the scanner skips). Use "/volume1/*" for a breakdown.

    breakdown: disk_usage.BreakdownOptions for the storage breakdown, or None.

    Returns: {'sizes': {path: bytes}, 'timed_out': [...], 'errors': [...], 'seconds': float}
    With breakdown also 'files', 'folder_seconds', 'breakdown' and
    'breakdown_unavailable' (see disk_usage.scan_folders).
    """
    start = time.monotonic()
    deadline = start + total_timeout
    sizes, timed_out, errors = {}, [], []
    extra = {'files': {}, 'folder_seconds': {}, 'breakdown': {}, 'breakdown_unavailable': []}
    volume_roots = {_path_key(v) for v in volume_used}
    seen = set()

    def measure(folders):
        todo = []
        for folder in folders:
            key = _path_key(folder)
            if key in volume_roots:
                logger.warning(
                    f"Not scanning {folder}: it is a volume root and its used bytes are "
                    f"already reported from the volume's capacity. Use "
                    f"'{os.path.join(folder, '*')}' in scan_paths for a per-folder breakdown."
                )
                continue
            if key in seen:
                logger.debug(f"{folder} already measured; skipping duplicate")
                continue
            seen.add(key)
            todo.append(folder)
        if breakdown is None:
            result = scan_folders(todo, timeout=timeout, deadline=deadline)
        else:
            result = scan_folders(todo, timeout=timeout, deadline=deadline, breakdown=breakdown)
            extra['files'].update(result.get('files') or {})
            extra['folder_seconds'].update(result.get('seconds') or {})
            extra['breakdown'].update(result.get('breakdown') or {})
            extra['breakdown_unavailable'].extend(result.get('breakdown_unavailable') or [])
        sizes.update(result['sizes'])
        timed_out.extend(result['timed_out'])
        errors.extend(result['errors'])

    if not scan_paths and nas_type == 'linux':
        # One level deep in "/" would walk /usr, /var, /home ... as one folder each
        logger.warning(
            "No scan_paths configured: reporting volume capacity only. To measure folders, "
            "set scan_paths in the config, e.g. [\"/home/*\", \"/data/*\"]."
        )
    elif not scan_paths:
        # Legacy fallback: one level deep in each volume
        logger.info("No scan_paths configured — falling back to one level deep in each volume.")
        for vol in volumes:
            vol_normalized = os.path.normpath(vol)
            if not os.path.isdir(vol_normalized):
                logger.warning(f"Volume does not exist: {vol_normalized}")
                continue
            try:
                folders = discover_folders(vol_normalized, nas_type, raise_errors=True)
            except OSError as e:
                logger.error(f"Cannot list volume {vol_normalized}: {e} (reported in _errors)")
                errors.append(vol_normalized)
                continue
            measure(folders)
    else:
        for spec in scan_paths:
            try:
                spec = spec.strip()
                # Normalize path separators for cross-platform compatibility
                spec_normalized = os.path.normpath(spec)
                
                # Detect glob pattern: may end with /* (forward slash) or \* (backslash)
                # Check BEFORE normalization to handle both Windows and Unix styles
                is_glob = spec.endswith('/*') or spec.endswith('\\*')
                
                if is_glob:
                    # Glob mode: measure each immediate subfolder
                    # Remove glob pattern (/* or \*) to get root directory
                    root = spec_normalized
                    if root.endswith(os.sep + '*'):
                        root = root[:-2]  # Remove the separator + asterisk
                    elif root.endswith('/*'):
                        root = root[:-2]  # Remove the / + asterisk (in case normpath didn't convert)
                    root = os.path.normpath(root)
                    logger.info(f"Scanning subfolders of: {root}")
                    # Checked before any stat(): e.g. an NFS /home (dead server = hang)
                    refusal = disk_usage.scan_root_refusal(root)
                    if refusal:
                        logger.error(f"Not scanning {spec}: {refusal} (reported in _errors)")
                        errors.append(root)
                        continue
                    if not os.path.isdir(root):
                        logger.warning(f"Glob root does not exist or is not a directory: {root} (reported in _errors)")
                        errors.append(root)
                        continue
                    # Explicit glob roots may be anywhere (Linux, UNC), not only volume roots
                    try:
                        subfolders = discover_folders(root, nas_type, validate_volume=False,
                                                      raise_errors=True)
                    except OSError as e:
                        # e.g. a UNC share the SYSTEM account can't read
                        logger.error(f"Cannot list glob root {root}: {e} (reported in _errors)")
                        errors.append(root)
                        continue
                    if not subfolders:
                        logger.warning(f"No subfolders found under {root}")
                    measure(subfolders)
                else:
                    # Single-folder mode: measure this path as one total
                    logger.info(f"Measuring folder: {spec_normalized}")
                    refusal = disk_usage.scan_root_refusal(spec_normalized)
                    if refusal:
                        logger.error(f"Not scanning {spec}: {refusal} (reported in _errors)")
                        errors.append(spec_normalized)
                        continue
                    if not os.path.isdir(spec_normalized):
                        logger.warning(f"Scan path does not exist or is not a directory: {spec_normalized} (reported in _errors)")
                        errors.append(spec_normalized)
                        continue
                    measure([spec_normalized])
            except Exception as e:
                logger.error(f"Failed to scan scan_paths entry {spec!r}: {e}", exc_info=True)
                errors.append(str(spec))

    result = {
        'sizes':     sizes,
        'timed_out': timed_out,
        'errors':    errors,
        'seconds':   round(time.monotonic() - start, 1),
    }
    if breakdown is not None:
        result.update(extra)
    return result


def _default_total_timeout(timeout: float, entries: int, windows: bool) -> float:
    """
    Overall disk-scan budget when "total_timeout_seconds" isn't configured.

    Windows: 3 x timeout_seconds (3 h). With ~1 h for volume capacity and the
    upload a run fits the 4-hour Disk Collection task limit existing installs
    have (the installer sizes that limit as this budget + 1 h).

    Elsewhere there is no task time limit, so it is max(3, entries) x
    timeout_seconds, where entries is the number of scan_paths entries (or of
    volumes in the legacy no-scan_paths mode). Collectors before the overall
    budget gave every entry its own timeout_seconds, so an existing config with
    many long entries keeps the scan time it had.
    """
    if windows:
        return TOTAL_TIMEOUT_MULTIPLIER * timeout
    return max(TOTAL_TIMEOUT_MULTIPLIER, entries) * timeout


def _scan_policy(config: dict, nas_type: str):
    """
    Folder-scan rules for this platform (see disk_usage.ScanPolicy).

    "cross_filesystems" (default false on Linux, true elsewhere, which keeps
    the Synology/Windows behaviour): when false, measuring a folder never
    descends into another filesystem or mount point below it (du -x).
    "include_network_mounts" (default false): auto-discover nfs/cifs mounts
    as volumes and let scans enter them.
    """
    cross = _cfg_bool(config, 'cross_filesystems', nas_type != 'linux')
    include_network = _cfg_bool(config, 'include_network_mounts', False)
    return disk_usage.ScanPolicy(nas_type, one_filesystem=not cross,
                                 include_network=include_network)


def collect_disk_usage(config: dict, logger: logging.Logger, dry_run: bool = False,
                       sync: bool = True) -> bool:
    """
    Collect disk usage (daily mode). sync=False only archives and queues the
    result (the Linux disk unit, which has no network, runs it that way).

    Legacy scan_depth parameter in config is silently ignored; use scan_paths instead.
    dry_run: print the message instead of recording and sending it.

    With storage_breakdown.enabled (default) the same walk also measures
    file counts, cold data and (Linux, DSM) per-owner usage, sent as a
    second message, 'storage_breakdown' (lib/storage_breakdown.py);
    folder_usage is built exactly as without it. The time each folder took
    is kept in storage_breakdown_state.json (never by dry_run): only a
    folder whose previous measurement took at most
    storage_breakdown.max_timeout_fraction of its timeout gets the slower
    breakdown walker (disk_usage.BreakdownOptions).
    """
    logger.info("=== DISK USAGE COLLECTION ===")

    if not config.get('active', True):
        logger.info("Collector is inactive (active=false in config) - skipping collection and sync.")
        return True

    ident = _identity(config, logger)
    if ident is None:
        return False
    name, system_id, device_type = ident

    volumes    = config.get('volumes', [])
    scan_paths = config.get('scan_paths', [])
    timeout    = _cfg_number(config, 'timeout_seconds', DEFAULT_TIMEOUT_SECONDS, minimum=1)
    # Note: legacy 'scan_depth' parameter is ignored; use 'scan_paths' instead
    nas_type   = get_platform(config)

    try:
        # If legacy scan_depth is present, it's ignored (scan_paths takes precedence)
        if config.get('scan_depth'):
            logger.debug("Legacy 'scan_depth' parameter found in config; ignoring (use 'scan_paths' instead)")

        # ------------------------------------------------------------------
        # 1. Volume capacity (used / free / total) — always via shutil.
        #    'volumes' exists purely for this purpose.
        #    Auto-discover if omitted. Measured first so it is always sent,
        #    however the folder scan goes.
        # ------------------------------------------------------------------
        policy = _scan_policy(config, nas_type)
        if not volumes:
            logger.info("No volumes specified — auto-discovering...")
            volumes = discover_volumes(nas_type, mounts=policy.mounts,
                                       include_network=policy.include_network)
            if volumes:
                logger.info(f"Auto-discovered volumes: {volumes}")
            else:
                logger.warning("No volumes found. Add 'volumes' to config.json.")

        volume_used, volume_capacity = _measure_volumes(volumes, logger)

        total_timeout = _cfg_number(
            config, 'total_timeout_seconds',
            _default_total_timeout(timeout, len(scan_paths or volumes), nas_type == 'windows'),
            minimum=1)
        logger.info(
            f"Measuring disk usage for {name} (device_type={device_type}); time budget "
            f"{timeout:.0f}s per folder, {total_timeout:.0f}s overall"
        )

        # ------------------------------------------------------------------
        # 2. Folder usage — driven by 'scan_paths' (see _measure_scan_paths).
        # ------------------------------------------------------------------
        breakdown = _breakdown_options(config, name, nas_type, logger)
        with disk_usage.scan_policy(policy):
            scan = _measure_scan_paths(scan_paths, volumes, volume_used, nas_type,
                                       timeout, total_timeout, logger,
                                       breakdown=breakdown.scan_options(breakdown.previous)
                                       if breakdown is not None else None)
        if scan['timed_out'] or scan['errors']:
            logger.warning(
                f"Disk scan incomplete: {len(scan['timed_out'])} folder(s) timed out, "
                f"{len(scan['errors'])} failed — listed in _timed_out / _errors, not reported as 0"
            )

        entry = {
            'header': build_header(name, system_id, device_type),
            'data': {
                'data_type':     'folder_usage',
                '_unit':         'bytes',
                **scan['sizes'],    # per-folder usage from scan_paths
                **volume_used,      # per-volume used bytes (shutil, authoritative)
                **volume_capacity,  # per-volume total/free bytes
                # Sum of the measured volumes; null if no volume could be measured
                'total_usage':   sum(volume_used.values()) if volume_used else None,
                # Metadata ('_' prefix = never a folder)
                # Which keys are volumes: on Linux a volume can be /mnt/data
                # and a folder /data, so path depth can't tell them apart
                '_volumes':      list(volume_used),
                '_timed_out':    scan['timed_out'],
                '_errors':       scan['errors'],
                '_scan_seconds': scan['seconds'],
            }
        }
        entries = [entry]
        if breakdown is not None:
            payload = storage_breakdown.build_payload(scan, list(scan['sizes']), breakdown)
            if payload is not None:
                entries.append({'header': build_header(name, system_id, device_type),
                                'data': {'data_type': 'storage_breakdown', **payload}})
                logger.info(f"Storage breakdown: {len(payload['folders'])} folder(s), "
                            f"{len(payload['_breakdown_unavailable'])} measured without it "
                            f"(first run, or too close to their timeout)")
    except Exception as e:
        logger.error(f"Disk collection failed: {e}", exc_info=True)
        return False

    if dry_run:
        _print_messages(entries)
        return True
    sent = _record_and_sync(config, name, system_id, entries, logger, sync=sync)
    if breakdown is not None:
        _save_breakdown_state(breakdown.state_path, scan.get('folder_seconds') or {}, logger)
    return sent


def _breakdown_options(config: dict, name: str, nas_type: str, logger: logging.Logger):
    """storage_breakdown.Options for this disk run (with .previous and .state_path), or None when disabled."""
    try:
        options = storage_breakdown.Options(config, nas_type == 'windows', logger)
        if not options.enabled:
            return None
        options.state_path = os.path.join(_device_dir(config, name, create=False), storage_breakdown.STATE_FILE)
        options.previous = storage_breakdown.load_state(options.state_path)
        return options
    except Exception as e:
        logger.warning(f"Storage breakdown unavailable this run ({e}); folder_usage is measured as usual")
        return None


def _save_breakdown_state(path: str, seconds: dict, logger: logging.Logger) -> None:
    """storage_breakdown_state.json: {folder: seconds its measurement took} for the next run's time guard."""
    try:
        local_queue.atomic_write_json(path, seconds)
    except Exception as e:
        logger.warning(f"Could not write {path} ({e}); the next disk run measures every folder without "
                       f"the storage breakdown")


COUNTER_LOCK_TIMEOUT = 20    # seconds to wait for another run's perf_snapshot.json


def get_perf_snapshot_path(config: dict, nas_name: str, create: bool = True) -> str:
    """Path to perf_snapshot.json, the interval counters' baseline (lib/perf_counters.py)."""
    return os.path.join(_device_dir(config, nas_name, create), perf_counters.SNAPSHOT_FILE)


def measure_counters(config: dict, name: str, logger: logging.Logger, dry_run: bool = False) -> dict:
    """
    Interval counters for this run: CPU, disk I/O and per-user CPU-seconds
    averaged over the time since the previous run (lib/perf_counters.py,
    lib/proc_accounting.py), against the baseline in perf_snapshot.json.

    Under perf_snapshot.json's lock (waits up to 20 s) it reads the clock,
    CPU, disk and process counters, loads the previous snapshot, decides the
    window (perf_counters.window), saves the new snapshot and reports counter
    values only if that save worked, so the windows of two runs never
    overlap. A window under 5 s keeps the previous snapshot and reports no
    counters. On a lock timeout, or when the counters fail, the snapshot is
    left alone and there are no counters, but the processes are still read
    for the users (RAM, process counts; CPU None) and the GPU owners.
    dry_run (--dry-run) reads the snapshot but never writes it or its lock.

    Returns {} when nothing could be measured, else a dict with
      status       'ok' | 'reboot' | 'short' | 'stale' | 'none' | 'unavailable'
                   | 'locked' (lock timeout) | 'error' (the counters failed)
      interval_s   (1 dp) and, when there is a window: cpu_percent,
                   cpu_iowait_percent, cpu_steal_percent,
                   proc_cpu_coverage_percent and perf_counters.DISK_FIELDS
      users        {username: {cpu_seconds, cpu_cores, cpu_percent, ram_bytes,
                   process_count}} (CPU values None without a window) over the
                   processes _user_filter() keeps, or None when per-process data
                   is unavailable
      owner_of     {pid: username} of every process (excluded users too), or None
      uid_of       {pid: uid} (Linux/DSM)
    """
    try:
        path = get_perf_snapshot_path(config, name, create=not dry_run)
    except OSError as e:
        logger.warning(f"Interval counters unavailable: cannot create the data directory ({e})")
        return {}
    try:
        if dry_run:
            return _measure_counters_locked(config, path, logger, persist=False)
        with local_queue.QueueLock(path, timeout=COUNTER_LOCK_TIMEOUT):
            return _measure_counters_locked(config, path, logger, persist=True)
    except local_queue.QueueLockTimeout:
        logger.warning(f"Another collector run holds {path}.lock: interval counters are sent as null "
                       f"this run and the snapshot is left as it is")
        status = 'locked'
    except Exception as e:
        logger.warning(f"Interval counters failed ({e}); they are sent as null this run", exc_info=True)
        status = 'error'
    # No window, but the users are still reported and the GPU part gets the owners
    try:
        table = proc_accounting.read_processes(config, log=logger)
        uid_of = table.uid_of() if table is not None else {}
        users, _ = proc_accounting.user_cpu(None, table, None, None, os.cpu_count(),
                                            keep=_user_filter(config, uid_of))
    except Exception as e:
        logger.warning(f"Per-user metrics unavailable this run ({e})")
        return {}
    return {'status': status, 'users': users, 'uid_of': uid_of,
            'owner_of': table.owner_of if table is not None else None}


def _measure_counters_locked(config: dict, path: str, logger: logging.Logger, persist: bool) -> dict:
    max_window = settings.value(config, 'counter_max_window_seconds')
    clock = perf_counters.clock_now()
    cpu = perf_counters.read_cpu_counters()
    disks = perf_counters.read_disk_counters(config) if settings.value(config, 'disk_io') else None
    table = proc_accounting.read_processes(config, log=logger)
    uid_of = table.uid_of() if table is not None else {}
    keep = _user_filter(config, uid_of)
    ncpu = os.cpu_count()
    result = {'owner_of': table.owner_of if table is not None else None, 'uid_of': uid_of}

    def without_window(status):
        result['status'] = status
        result['users'], _ = proc_accounting.user_cpu(None, table, None, None, ncpu, keep=keep)
        return result

    if clock is None or cpu is None:
        return without_window('unavailable')
    prev = perf_counters.load_snapshot(path)
    status, interval = perf_counters.window(perf_counters.snapshot_clock(prev) if prev else None,
                                            clock, max_window)
    if status == 'short':
        logger.info(f"Only {interval:.1f}s since the previous run: no interval counters this run "
                    f"(the previous baseline is kept)")
        return without_window(status)
    if status == 'stale':
        logger.info(f"More than counter_max_window_seconds ({max_window:.0f}s) since the previous run: "
                    f"interval counters start from a new baseline")
    elif status == 'reboot':
        logger.info("The host rebooted since the previous run: interval counters are averages since boot")

    base_cpu = base_disks = base_procs = None
    if status == 'ok':
        base_cpu = perf_counters.snapshot_cpu(prev)
        base_disks = perf_counters.snapshot_disks(prev)
        if table is not None and prev['source'].get('proc') == table.source:
            base_procs = proc_accounting.prev_table(prev)
    elif status == 'reboot':
        base_cpu = perf_counters.zero_cpu(cpu)
        base_disks = perf_counters.zero_disks(disks)
        if table is not None:
            base_procs = proc_accounting.zero_baseline(clock, table.hz)
    cpu_values = perf_counters.cpu_metrics(base_cpu, cpu)
    if status in ('ok', 'reboot') and cpu_values is None:
        logger.info("CPU counters went backwards or changed source since the previous run: "
                    "interval counters start from a new baseline")
        status = 'none'

    if persist and not perf_counters.save_snapshot(
            path, perf_counters.build_snapshot(clock, cpu, disks, table)):
        return without_window(status)
    if cpu_values is None:
        return without_window(status)

    users, coverage = proc_accounting.user_cpu(
        base_procs, table, interval, cpu_values['attributable_s'], ncpu,
        settings.value(config, 'user_cpu_count_children'), keep)
    result.update({
        'status':                    status,
        'interval_s':                round(interval, 1),
        'cpu_percent':               cpu_values['cpu_percent'],
        'cpu_iowait_percent':        cpu_values['cpu_iowait_percent'],
        'cpu_steal_percent':         cpu_values['cpu_steal_percent'],
        'proc_cpu_coverage_percent': coverage,
        'users':                     users,
    })
    result.update(perf_counters.disk_metrics(base_disks, disks, interval, platform_info.is_32bit()))
    return result


USERNAME_MAX_CHARS = 256     # contract R10: longer names are cut

# The per-user GPU fields of a user who holds no GPU on a host where they were measured
_NO_GPU_USE = {'gpu_vram_bytes': 0, 'gpu_util_percent': 0.0, 'gpu_busy_seconds': 0.0,
               'gpus_held': 0, 'gpu_held_seconds': 0.0, 'gpu_processes': 0}


def _user_metrics_data(config: dict, cpu_users: Optional[dict], gpu_users: Optional[dict],
                       interval_s: Optional[float], legacy_rows: Optional[list] = None) -> list:
    """
    The data of the user_metrics messages: one per user in the union of the
    per-user CPU/RAM rows and the per-user GPU data. A user seen only on a GPU
    gets null cpu_percent / ram_bytes / ram_formatted. The GPU fields are on
    every row where per-user GPU data exists (0 for users without GPU use);
    the CPU-seconds fields wherever per-process accounting works.

    user_metrics: "all"; "off" (no messages); "active" (only users with
    cpu_cores >= user_metrics_min_cores, RSS >= user_metrics_min_ram_bytes,
    or a GPU held; without a window only the RAM and GPU tests apply);
    "auto" = "active" on Synology, "all" elsewhere.
    legacy_rows: get_per_user_stats() rows (perf_counters false) instead of
    cpu_users.
    """
    mode = settings.value(config, 'user_metrics')
    if mode == 'off':
        return []
    if mode == 'auto':
        mode = 'active' if get_platform(config) == 'synology' else 'all'
    min_cores = settings.value(config, 'user_metrics_min_cores')
    min_ram = settings.value(config, 'user_metrics_min_ram_bytes')

    rows = {}
    for row in legacy_rows or []:
        rows[row['username'][:USERNAME_MAX_CHARS]] = {
            'cpu_percent': row['cpu_percent'], 'ram_bytes': row['ram_bytes'],
            'ram_formatted': row['ram_formatted']}
    for username, c in (cpu_users or {}).items():
        rows[username[:USERNAME_MAX_CHARS]] = {
            'cpu_percent':   c['cpu_percent'],
            'ram_bytes':     c['ram_bytes'],
            'ram_formatted': format_bytes(c['ram_bytes']),
            'cpu_seconds':   c['cpu_seconds'],
            'cpu_cores':     c['cpu_cores'],
            'interval_s':    interval_s if c['cpu_seconds'] is not None else None,
            'process_count': c['process_count'],
        }
    if gpu_users is not None:
        for username in gpu_users:
            rows.setdefault(username, {'cpu_percent': None, 'ram_bytes': None, 'ram_formatted': None})
        for username, row in rows.items():
            row.update(gpu_users.get(username) or _NO_GPU_USE)

    def active(row):
        cores, ram = row.get('cpu_cores'), row.get('ram_bytes')
        return ((cores is not None and cores >= min_cores) or (ram is not None and ram >= min_ram)
                or bool(row.get('gpus_held')))

    data = [{'data_type': 'user_metrics', 'username': username, **row}
            for username, row in rows.items() if mode == 'all' or active(row)]
    data.sort(key=lambda d: (d.get('cpu_seconds') is None and d.get('cpu_percent') is None,
                             -(d.get('cpu_seconds') or d.get('cpu_percent') or 0),
                             -(d.get('gpu_busy_seconds') or 0), d['username']))
    return data


def _metrics_summary(cpu_percent, counters: dict, ram_percent, uptime_formatted, network_stats: dict,
                     user_count: int, gpu_stats: list) -> str:
    """The run's one INFO line. 'GPUs: ...' stays last: install-collector-linux.sh reads it from the journal."""
    interval = counters.get('interval_s')
    window = f" (avg {interval:.0f}s)" if interval is not None else " (1-s sample)"
    parts = [f"CPU: {_fmt(cpu_percent, '%')}{window if cpu_percent is not None else ''}"]
    if counters.get('cpu_iowait_percent') is not None:
        parts.append(f"iowait: {counters['cpu_iowait_percent']}%")
    parts += [f"RAM: {_fmt(ram_percent, '%')}", f"Uptime: {_fmt(uptime_formatted)}",
              f"Network: {network_stats}"]
    if counters.get('disk_read_bytes_per_s') is not None:
        disk = (f"Disk: r {format_bytes(counters['disk_read_bytes_per_s'])}/s "
                f"w {format_bytes(counters['disk_write_bytes_per_s'])}/s")
        if counters.get('disk_busy_max_percent') is not None:
            disk += f" busy {counters['disk_busy_max_percent']:.0f}% {counters['disk_busy_max_name']}"
        parts.append(disk)
    coverage = counters.get('proc_cpu_coverage_percent')
    parts.append(f"Users: {user_count}" + (f" (coverage {coverage:.0f}%)" if coverage is not None else ''))
    if gpu_stats:
        gpu_list = [f"{g.get('name') or 'Unknown'} {_fmt(g.get('gpu_percent'), '%')}" for g in gpu_stats]
        parts.append(f"GPUs: {len(gpu_stats)} ({', '.join(gpu_list)})")
    else:
        parts.append("GPUs: none/unavailable")
    return ' | '.join(parts)


def collect_metrics(config: dict, logger: logging.Logger, dry_run: bool = False) -> bool:
    """
    Collect system metrics (5-minute mode):

      1. interval counters (measure_counters), RAM, network, uptime and the
         GPUs (lib/gpu.collect, with the counters' process owners);
      2. the system_metrics message and 3. one user_metrics message per user;
      4. _record() them: the system metrics are safe before anything optional
         runs;
      5. the optional collectors (_optional_collectors: who is connected,
         storage health), each in a bounded thread; 6. _record() their
         messages, then one _sync(); 7. their state files, written last and
         only once their messages are recorded.

    "perf_counters": false restores the old path: per-user seeding, the 1-s
    CPU sample and psutil per-process cpu_percent. dry_run (--dry-run):
    print the messages instead of 4-7; no state file is written.
    """
    logger.info("=== METRICS COLLECTION ===")

    if not config.get('active', True):
        logger.info("Collector is inactive (active=false in config) — skipping collection and sync.")
        return True

    ident = _identity(config, logger)
    if ident is None:
        return False
    name, system_id, device_type = ident
    
    try:
        counters, seeded = {}, None
        if settings.value(config, 'perf_counters'):
            counters = measure_counters(config, name, logger, dry_run=dry_run)
        else:
            # Old path: seed per-user CPU tracking BEFORE the system-level
            # sleep. psutil.cpu_percent(interval=None) always returns 0.0 on
            # the first call for a given process object, so the 1-second
            # blocking call inside get_cpu_percent() is the measurement window.
            seeded = _seed_user_processes(config)

        # Every getter returns None for a value it can't measure; None is sent
        # as JSON null (never 0) so the Manager/Dashboard show "unmeasured".
        cpu_percent = counters.get('cpu_percent')
        if cpu_percent is None:
            cpu_percent = get_cpu_percent()   # the 1-s sample: no interval average this run
        ram_percent = get_ram_percent()
        total_ram_bytes = get_total_ram_bytes()
        network_stats = (get_network_stats(config, name, persist=False) if dry_run
                         else get_network_stats(config, name))
        uptime_seconds = get_uptime_seconds()
        uptime_formatted = format_uptime(uptime_seconds)

        interval_s = counters.get('interval_s')
        owner_of = counters.get('owner_of')
        if seeded is not None:
            # The seeded processes' psutil names, so a GPU user's row is the same user as their CPU row
            owner_of = {item['proc'].pid: item['username'] for item in seeded}
        sample = gpu.collect(config, owner_of, interval_s, logger,
                             user_filter=_user_filter(config, counters.get('uid_of')))
        gpu_stats = sample.devices

        if seeded is not None:
            legacy_rows, cpu_users = get_per_user_stats(config, logger, seeded_procs=seeded), None
        else:
            legacy_rows, cpu_users = None, counters.get('users')
        user_data = _user_metrics_data(config, cpu_users, sample.per_user, interval_s, legacy_rows)
        logger.info(_metrics_summary(cpu_percent, counters, ram_percent, uptime_formatted,
                                     network_stats, len(user_data), gpu_stats))

        # Create message (header + data).
        # Each numeric field is accompanied by a sibling *_unit field so any
        # consumer (dashboard, downstream scripts) can interpret values
        # correctly without hardcoding field-name→unit mappings.
        # Flat per-GPU columns for time-series chart history.
        # gpu_N_percent and gpu_N_vram_used match the data-metric attribute
        # names used by the dashboard JS. Keeping 'gpus' as a JSON blob
        # preserves the rich struct needed for the current-metrics card display.
        gpu_flat = {}
        for i, g in enumerate(gpu_stats):
            p = f'gpu_{i}'
            gpu_flat[f'{p}_percent']          = g.get('gpu_percent')
            gpu_flat[f'{p}_percent_unit']     = '%'
            gpu_flat[f'{p}_vram_used']        = g.get('vram_used_bytes')
            gpu_flat[f'{p}_vram_used_unit']   = 'bytes'
            if g.get('temperature_c') is not None:
                gpu_flat[f'{p}_temp_c']       = g['temperature_c']
                gpu_flat[f'{p}_temp_c_unit']  = '°C'
            if g.get('power_watts') is not None:
                gpu_flat[f'{p}_power_w']      = g['power_watts']
                gpu_flat[f'{p}_power_w_unit'] = 'W'
        if gpu_stats:
            # The dt behind the per-user GPU-seconds
            gpu_flat['gpu_sample_seconds']      = sample.sample_seconds
            gpu_flat['gpu_sample_seconds_unit'] = 's'

        entry = {
            'header': build_header(name, system_id, device_type),
            'data': {
                'data_type':                        'system_metrics',
                # The average since the previous run when interval_s is set,
                # else a 1-s sample (first run, no valid baseline)
                'cpu_percent':                      cpu_percent,
                'cpu_percent_unit':                 '%',
                'interval_s':                       interval_s,
                'interval_s_unit':                  's',
                'cpu_count':                        os.cpu_count(),
                'cpu_iowait_percent':               counters.get('cpu_iowait_percent'),
                'cpu_iowait_percent_unit':          '%',
                'cpu_steal_percent':                counters.get('cpu_steal_percent'),
                'cpu_steal_percent_unit':           '%',
                'proc_cpu_coverage_percent':        counters.get('proc_cpu_coverage_percent'),
                'proc_cpu_coverage_percent_unit':   '%',
                'ram_percent':                      ram_percent,
                'ram_percent_unit':                 '%',
                'total_ram_bytes':                  total_ram_bytes,
                'total_ram_bytes_unit':             'bytes',
                'uptime_seconds':                   uptime_seconds,
                'uptime_seconds_unit':              's',
                'uptime_formatted':                 uptime_formatted,
                'network_bytes_in':                 network_stats.get('bytes_in'),
                'network_bytes_in_unit':            'bytes',
                'network_bytes_out':                network_stats.get('bytes_out'),
                'network_bytes_out_unit':           'bytes',
                'network_bandwidth_in_mbps':        network_stats.get('bandwidth_in_mbps'),
                'network_bandwidth_in_mbps_unit':   'Mbps',
                'network_bandwidth_out_mbps':       network_stats.get('bandwidth_out_mbps'),
                'network_bandwidth_out_mbps_unit':  'Mbps',
                # Sums over the physical disks (md arrays and virtual devices left out)
                'disk_read_bytes_per_s':            counters.get('disk_read_bytes_per_s'),
                'disk_read_bytes_per_s_unit':       'B/s',
                'disk_write_bytes_per_s':           counters.get('disk_write_bytes_per_s'),
                'disk_write_bytes_per_s_unit':      'B/s',
                'disk_read_iops':                   counters.get('disk_read_iops'),
                'disk_read_iops_unit':              'ops/s',
                'disk_write_iops':                  counters.get('disk_write_iops'),
                'disk_write_iops_unit':             'ops/s',
                'disk_await_ms':                    counters.get('disk_await_ms'),
                'disk_await_ms_unit':               'ms',
                'disk_busy_max_percent':            counters.get('disk_busy_max_percent'),
                'disk_busy_max_percent_unit':       '%',
                'disk_busy_max_name':               counters.get('disk_busy_max_name'),
                'gpus':                             gpu_stats,
                'gpu_count':                        len(gpu_stats),
                **gpu_flat,
            }
        }
        entries = [entry]
        
        # Per-user metrics (one message per user for normalized history storage)
        for data in user_data:
            entries.append({'header': build_header(name, system_id, device_type), 'data': data})
        logger.info(f"Collected {len(entries)} message(s) (including {len(user_data)} user metrics)")
        
    except Exception as e:
        logger.error(f"Metrics collection failed: {e}", exc_info=True)
        return False

    if dry_run:
        optional = _optional_collectors(config, name, logger)
        _print_messages(entries + [{'header': build_header(name, system_id, device_type), 'data': r.payload}
                                   for r in optional if r.payload is not None])
        return True

    if not _record(config, name, entries, logger):
        return False
    optional = _optional_collectors(config, name, logger)
    extra = [{'header': build_header(name, system_id, device_type), 'data': r.payload}
             for r in optional if r.payload is not None]
    recorded = _record(config, name, extra, logger) if extra else True
    synced = _sync(config, name, system_id, logger)
    if recorded:
        _commit_state(optional, logger)
    return recorded and synced


# ---------------------------------------------------------------------------
# Optional collectors (sessions, storage health), run after the metrics are
# recorded
# ---------------------------------------------------------------------------
#
# Interface of lib/sessions.py and lib/storage_health.py. A module that isn't
# there is skipped (logged at DEBUG). Both functions run in a daemon thread
# within a time budget and must not write any file: they return their new
# state, which the main thread writes (local_queue.atomic_write_json to
# data/<name>/<module>.STATE_FILE) only after their message was recorded, so a
# failed record or an abandoned thread can never leave state saying "sent".
# Reading their own state file is fine (also under --dry-run). Every
# subprocess they start needs its own timeout.
#
#   sessions.STATE_FILE = 'sessions_state.json'
#   sessions.collect(config, data_dir, name, logger=None) -> (payload, state)
#       payload: the 'sessions' message data (contract item 13, with
#                'data_type': 'sessions'), or None for no message;
#       state:   the new sessions_state.json ({last_full_utc, hash}), or None
#                to leave it as it is.
#       Called only when sessions.enabled; budget sessions.timeout_seconds.
#   sessions.failure_payload(config, status) -> payload
#       The message sent when collect() raised (status 'error') or overran
#       its budget ('timeout'): null counts, every source in _sources set to
#       that status, _list_state 'none'. A Manager clears its stored list on
#       'none', so once that message is recorded the collector writes the
#       state _SESSIONS_FAILED_STATE: the next list is sent in full.
#   storage_health.STATE_FILE = 'storage_health_state.json'
#   storage_health.maybe_collect(config, data_dir, name, force=False, logger=None)
#           -> (payload, state)
#       payload: the 'storage_health' message data (contract item 14), or
#                None when nothing needs to be sent this run; force=True
#                (--mode health) always gives one, with trigger 'manual';
#       state:   the new storage_health_state.json, or None.
#       Called when storage_health.enabled (always for --mode health);
#       budget storage_health.timeout_seconds + 10.
#
#   data_dir is get_data_dir(config); a module's state file is
#   os.path.join(data_dir, name, STATE_FILE).

OPTIONAL_COLLECTORS = ('sessions', 'storage_health')
# storage_health's budget is its own timeout_seconds plus this margin, so its
# PowerShell / subprocess timeouts fire before the thread is abandoned
HEALTH_BUDGET_MARGIN_SECONDS = 10

_OptionalResult = namedtuple('_OptionalResult', 'kind payload state_path state status')

# sessions_state.json after a failed sessions run ({last_full_utc, hash}, as sessions.py writes it)
_SESSIONS_FAILED_STATE = {'last_full_utc': None, 'hash': None}


class _Bounded:
    """fn(*args, **kwargs) started in a daemon thread; wait() collects the outcome."""

    def __init__(self, fn, *args, **kwargs):
        self._value = self._error = None
        self._thread = threading.Thread(target=self._run, args=(fn, args, kwargs), daemon=True,
                                        name=f"lab-monitor-{getattr(fn, '__module__', 'task')}")
        self._thread.start()

    def _run(self, fn, args, kwargs):
        try:
            self._value = fn(*args, **kwargs)
        except BaseException as e:      # a ctypes fault surfaces as OSError on Windows
            self._error = e

    def wait(self, timeout: float):
        """('ok', value) | ('timeout', None) | ('error', exception). A thread still running is abandoned."""
        self._thread.join(max(0.0, timeout))
        if self._thread.is_alive():
            return 'timeout', None
        if self._error is not None:
            return 'error', self._error
        return 'ok', self._value


def _run_bounded(fn, budget_s: float, *args, **kwargs):
    """
    fn(*args, **kwargs) in a daemon thread, waited for at most budget_s
    seconds: ('ok', value), ('timeout', None) or ('error', exception). A
    hang or an exception costs only that one feature; a thread that overruns
    is abandoned (daemon threads end with the process).
    """
    return _Bounded(fn, *args, **kwargs).wait(budget_s)


def _optional_module(kind: str, logger: logging.Logger):
    """lib.<kind>, or None when this collector doesn't have it (or it can't be loaded)."""
    try:
        return importlib.import_module(f'lib.{kind}')
    except ModuleNotFoundError as e:
        if e.name == f'lib.{kind}':
            logger.debug(f"lib/{kind}.py is not part of this collector; skipping {kind}")
            return None
        logger.warning(f"Cannot load lib/{kind}.py ({e}); skipping {kind} this run")
    except Exception as e:
        logger.warning(f"Cannot load lib/{kind}.py ({e}); skipping {kind} this run", exc_info=True)
    return None


def _failed_sessions_payload(module, config: dict, status: str) -> dict:
    try:
        return module.failure_payload(config, status)
    except Exception:
        return {'data_type': 'sessions', '_schema': 1, '_unit': 'count', '_sources': {'tcp': status},
                '_list_state': 'none', '_list_hash': None, 'clients_total': None, 'users_total': None}


def _optional_collectors(config: dict, name: str, logger: logging.Logger,
                         kinds=OPTIONAL_COLLECTORS, force_health: bool = False) -> list:
    """
    Run the optional collectors (see the interface above) in parallel daemon
    threads, each within its budget, and return their _OptionalResult's: the
    payload to record (or None) and the state to commit afterwards (only
    what a finished thread returned, never an abandoned one's). A sessions
    run that fails or overruns still gives a 'sessions' message with null
    counts, so the Dashboard shows the list as unavailable instead of a stale
    one, and the state _SESSIONS_FAILED_STATE.
    """
    try:
        data_dir = get_data_dir(config, create=False)
    except OSError:
        return []
    started = []
    for kind in kinds:
        enabled = settings.value(config, f'{kind}.enabled', logger)
        if not enabled and not (kind == 'storage_health' and force_health):
            continue
        module = _optional_module(kind, logger)
        if module is None:
            continue
        if kind == 'sessions':
            budget = settings.value(config, 'sessions.timeout_seconds', logger)
            task = _Bounded(module.collect, config, data_dir, name, logger=logger)
        else:
            budget = settings.value(config, 'storage_health.timeout_seconds', logger) + HEALTH_BUDGET_MARGIN_SECONDS
            task = _Bounded(module.maybe_collect, config, data_dir, name, force=force_health, logger=logger)
        started.append((kind, module, task, time.monotonic() + budget, budget))

    results = []
    for kind, module, task, deadline, budget in started:
        status, value = task.wait(deadline - time.monotonic())
        payload = state = None
        if status == 'ok':
            if isinstance(value, tuple) and len(value) == 2 and all(
                    v is None or isinstance(v, dict) for v in value):
                payload, state = value
            else:
                logger.warning(f"{kind}: unexpected result {type(value).__name__}; nothing sent")
                status = 'error'
        elif status == 'timeout':
            logger.warning(f"{kind} took longer than its {budget:.0f}s budget; left running in the background "
                           f"and its result discarded")
        else:
            logger.warning(f"{kind} failed: {value}", exc_info=(type(value), value, value.__traceback__))
        if status != 'ok' and kind == 'sessions':
            payload = _failed_sessions_payload(module, config, status)
            state = dict(_SESSIONS_FAILED_STATE)
        if payload is not None:
            payload = {**payload, 'data_type': kind}
        state_file = getattr(module, 'STATE_FILE', f'{kind}_state.json')
        results.append(_OptionalResult(kind, payload, os.path.join(data_dir, name, state_file), state, status))
    return results


def _commit_state(results: list, logger: logging.Logger) -> None:
    """Write the optional collectors' new state files (only after their messages were recorded)."""
    for r in results:
        if r.state is None:
            continue
        try:
            local_queue.atomic_write_json(r.state_path, r.state)
        except Exception as e:
            logger.warning(f"Could not write {r.state_path} ({e}); {r.kind} starts afresh next run")


def collect_health(config: dict, logger: logging.Logger, dry_run: bool = False) -> bool:
    """--mode health: a full storage-health collection (trigger 'manual'), recorded and sent now."""
    logger.info("=== STORAGE HEALTH CHECK ===")
    ident = _identity(config, logger)
    if ident is None:
        return False
    name, system_id, device_type = ident
    results = _optional_collectors(config, name, logger, kinds=('storage_health',), force_health=True)
    if not results:
        logger.error("Storage health is not available in this collector (lib/storage_health.py is missing "
                     "or could not be loaded)")
        return False
    result = results[0]
    if result.status != 'ok' or result.payload is None:
        logger.error("Storage health check produced no message")
        return False
    entries = [{'header': build_header(name, system_id, device_type), 'data': result.payload}]
    if dry_run:
        _print_messages(entries)
        return True
    if not _record(config, name, entries, logger):
        return False
    _commit_state(results, logger)
    return _sync(config, name, system_id, logger)


class _BatchOutcome:
    """Result of POSTing one batch: msg_ids to drop from the queue, and what to do next."""
    __slots__ = ('acked', 'complete', 'stop', 'split')

    def __init__(self, acked=(), complete=False, stop=False, split=False):
        self.acked = set(acked)    # durably handled by the Manager → remove from queue
        self.complete = complete   # every message in the batch was handled
        self.stop = stop           # don't send further batches this run
        self.split = split         # too large (HTTP 413): retry as two halves


def _short(text: str, limit: int = 300) -> str:
    text = (text or '').strip().replace('\n', ' ')
    return text if len(text) <= limit else text[:limit] + '...'


def _post_batch(manager_url: str, token: str, timeout: float, name: str, system_id: str,
                batch: list, logger: logging.Logger) -> _BatchOutcome:
    """POST one batch to /api/data/queue and interpret the Manager's ACK."""
    queue_id = f"{name}-{uuid.uuid4().hex[:12]}"
    payload = {
        'queue_id': queue_id,
        'name':     name,
        'id':       system_id,
        'messages': batch,
    }
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(f"{manager_url}/api/data/queue", data=body,
                                 headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        logger.warning(f"Network error posting batch {queue_id}: {e}")
        return _BatchOutcome(stop=True)

    status = response.status_code
    if status == 413:
        if len(batch) > 1:
            logger.warning(
                f"Manager refused batch {queue_id} ({len(batch)} messages, {len(body):,} bytes) "
                f"as too large (HTTP 413); retrying it in two halves"
            )
            return _BatchOutcome(split=True)
        logger.error(
            f"Message {local_queue.msg_id_of(batch[0])} ({len(body):,} bytes) is larger than the "
            f"Manager accepts (HTTP 413, max_request_bytes); it stays queued"
        )
        return _BatchOutcome()
    if status == 401:
        logger.error(
            "Manager rejected manager_token (HTTP 401). It must match the Manager's "
            "collector_tokens entry for this device (or a legacy auth_tokens entry)."
        )
        return _BatchOutcome(stop=True)
    if status == 403:
        logger.error(
            f"Manager accepted the token but it may not send data for device {name!r} "
            f"(HTTP 403). The Manager's collector_tokens entry for this token must be keyed "
            f"by this device name."
        )
        return _BatchOutcome(stop=True)
    if status != 200:
        logger.warning(f"Manager returned HTTP {status} for batch {queue_id}: {_short(response.text)}")
        return _BatchOutcome(stop=True)

    try:
        result = response.json()
    except ValueError:
        logger.warning(f"Manager sent a non-JSON ACK for batch {queue_id}: {_short(response.text)}")
        return _BatchOutcome(stop=True)
    if not isinstance(result, dict) or result.get('status') != 'ok':
        logger.warning(f"Manager did not ACK batch {queue_id}: {_short(response.text)}")
        return _BatchOutcome(stop=True)
    if result.get('queue_id') != queue_id:
        logger.warning(
            f"Manager ACK queue_id mismatch: sent {queue_id!r}, "
            f"got {result.get('queue_id')!r} — keeping batch queued"
        )
        return _BatchOutcome(stop=True)

    batch_ids = [local_queue.msg_id_of(e) for e in batch]
    results = result.get('results')

    if not isinstance(results, list):
        # Older Manager: no per-message results, only a stored count. Only
        # drop the batch once it confirms it persisted every message we sent.
        stored = result.get('stored')
        if stored == len(batch):
            logger.info(f"Batch {queue_id}: Manager stored all {stored} message(s)")
            return _BatchOutcome(acked=batch_ids, complete=True)
        logger.warning(
            f"Manager ACK stored count mismatch for batch {queue_id}: sent {len(batch)}, "
            f"stored {stored!r} \u2014 keeping batch queued for retry"
        )
        return _BatchOutcome(stop=True)

    in_batch = set(batch_ids)
    acked = set()
    counts = Counter()
    errors = []
    for r in results:
        if not isinstance(r, dict) or r.get('msg_id') not in in_batch:
            continue
        msg_id, outcome = r['msg_id'], r.get('status')
        counts[outcome] += 1
        if outcome in _HANDLED_STATUSES:
            acked.add(msg_id)
            if outcome == 'rejected':
                logger.warning(
                    f"Manager permanently rejected message {msg_id} ({r.get('error') or 'no reason given'}); "
                    f"it is in the Manager's dead_letter table and the local archive, and is "
                    f"removed from the queue"
                )
        elif outcome == 'error':
            errors.append((msg_id, r.get('error') or 'transient error'))
    if errors:
        # One line, not one per message: a locked Manager database fails the whole batch
        first_id, reason = errors[0]
        logger.warning(
            f"Manager could not store {len(errors)} message(s) ({reason}; first: {first_id}); "
            f"keeping them queued for retry"
        )

    kept = len(batch) - len(acked)
    logger.info(
        f"Batch {queue_id}: sent {len(batch)} — stored {counts['stored']}, duplicate "
        f"{counts['duplicate']}, rejected {counts['rejected']}, error {counts['error']}"
        + (f"; {kept} kept for retry" if kept else "")
    )
    # No progress at all (e.g. Manager DB locked): don't hammer it with the rest
    return _BatchOutcome(acked=acked, complete=(kept == 0), stop=not acked)


def sync_queue_to_manager(config: dict, name: str, system_id: str, queue_path: str, logger: logging.Logger) -> bool:
    """
    Post the queue to the Manager in batches; drop each message once ACKed.

    - At most max_batch_messages (default 100) messages per POST, oldest first.
    - The queue lock is held only to snapshot and to remove ACKed msg_ids,
      never during the HTTP request.
    - Messages the Manager reports stored / duplicate / rejected are removed;
      'error' ones (and anything unacknowledged) stay queued for the next run.

    Returns True if every queued message was handled by the Manager.
    """
    if requests is None:
        logger.error(
            "The Python package 'requests' is not installed, so nothing can be sent to the "
            "Manager. Install it with: python -m pip install -r requirements.txt "
            "(run in the collector directory)"
        )
        return False

    manager_url = str(config.get('manager_url') or '').rstrip('/')
    manager_token = config.get('manager_token')
    if not manager_url or not manager_token:
        logger.error("Config needs both 'manager_url' and 'manager_token' to send data to the Manager")
        return False
    if any(marker in str(manager_token).upper() for marker in _PLACEHOLDER_MARKERS):
        logger.warning("manager_token in config.json looks like a template placeholder; the Manager will reject it")
    if manager_url.startswith('http://'):
        logger.debug("manager_url uses plain http:// — the bearer token is sent unencrypted")

    timeout = _cfg_number(config, 'request_timeout_seconds', DEFAULT_REQUEST_TIMEOUT, minimum=1)
    max_batch = _cfg_number(config, 'max_batch_messages', DEFAULT_MAX_BATCH_MESSAGES,
                            minimum=1, integer=True)

    try:
        entries = local_queue.snapshot(queue_path, logger)
    except Exception as e:
        logger.error(f"Could not read queue {queue_path}: {e}")
        return False
    if not entries:
        logger.info("Queue is empty, nothing to sync")
        return True
    
    logger.info(f"Posting {len(entries)} queued message(s) to Manager in batches of up to {max_batch}")
    pending = deque(entries[i:i + max_batch] for i in range(0, len(entries), max_batch))
    to_remove = set()
    removed = 0
    all_handled = True
    batches_since_flush = 0

    def flush():
        nonlocal removed, batches_since_flush
        if to_remove:
            removed += local_queue.remove_acked(queue_path, to_remove, logger)
            to_remove.clear()
        batches_since_flush = 0

    try:
        while pending:
            batch = pending.popleft()
            outcome = _post_batch(manager_url, manager_token, timeout, name, system_id, batch, logger)
            if outcome.split:
                mid = len(batch) // 2
                pending.appendleft(batch[mid:])
                pending.appendleft(batch[:mid])
                continue
            to_remove.update(outcome.acked)
            if not outcome.complete:
                all_handled = False
            if outcome.stop:
                break
            batches_since_flush += 1
            if batches_since_flush >= _REMOVE_FLUSH_EVERY:
                flush()
    except Exception as e:
        logger.error(f"Queue sync failed: {e}", exc_info=True)
        all_handled = False
    finally:
        try:
            flush()
        except Exception as e:
            logger.error(f"Could not remove acknowledged messages from {queue_path}: {e} "
                         f"(they will be re-sent and deduplicated by the Manager)")
            all_handled = False

    left = sum(len(b) for b in pending)
    if all_handled and not left:
        logger.info(f"Queue synced successfully ({removed} message(s) acknowledged)")
        return True
    logger.info(
        f"{removed} message(s) acknowledged; the rest stay queued for retry on the next run"
    )
    return False


def get_cpu_percent() -> Optional[float]:
    """System CPU usage (%) over a ~1 s window, or None if it can't be measured."""
    try:
        import psutil
    except ImportError:
        return fallback_cpu_percent()
    try:
        return psutil.cpu_percent(interval=1)
    except Exception as e:
        logger.warning(f"psutil.cpu_percent failed: {e}")
        return None


def _read_proc_stat_cpu() -> tuple:
    """(total, idle) jiffies from the aggregate 'cpu' line of /proc/stat."""
    with open('/proc/stat', 'r') as f:
        for line in f:
            if line.startswith('cpu '):
                fields = [int(x) for x in line.split()[1:]]
                # user nice system idle iowait irq softirq steal [guest guest_nice];
                # guest time is already counted in user/nice, so stop at steal.
                total = sum(fields[:8])
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
                return total, idle
    raise ValueError("no aggregate 'cpu' line in /proc/stat")


def fallback_cpu_percent(interval: float = 1.0) -> Optional[float]:
    """
    CPU usage without psutil: two samples `interval` seconds apart (the same
    window psutil.cpu_percent(interval=1) uses) of /proc/stat, or on Windows
    of GetSystemTimes. Returns None where neither is available.
    """
    if sys.platform == 'win32':
        first = perf_counters.read_cpu_counters()
        if first is None:
            return None
        time.sleep(interval)
        values = perf_counters.cpu_metrics(first, perf_counters.read_cpu_counters())
        return values['cpu_percent'] if values else None
    try:
        total1, idle1 = _read_proc_stat_cpu()
        time.sleep(interval)
        total2, idle2 = _read_proc_stat_cpu()
        d_total = total2 - total1
        if d_total <= 0:
            return None
        busy = d_total - (idle2 - idle1)
        return round(min(100.0, max(0.0, busy * 100.0 / d_total)), 1)
    except Exception as e:
        logger.warning(f"CPU fallback measurement failed: {e}")
        return None


def get_ram_percent() -> Optional[float]:
    """RAM usage (%), or None if it can't be measured."""
    try:
        import psutil
    except ImportError:
        return fallback_ram_percent()
    try:
        return psutil.virtual_memory().percent
    except Exception as e:
        logger.warning(f"psutil.virtual_memory failed: {e}")
        return None


def get_total_ram_bytes() -> Optional[int]:
    """Total system RAM in bytes, or None if it can't be measured."""
    try:
        import psutil
    except ImportError:
        return fallback_total_ram_bytes()
    try:
        return psutil.virtual_memory().total
    except Exception as e:
        logger.warning(f"psutil.virtual_memory failed: {e}")
        return None


def _read_meminfo() -> dict:
    """Parse /proc/meminfo into {key: value in kB}, by key name (line order varies)."""
    info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            key, _, rest = line.partition(':')
            parts = rest.split()
            if parts:
                try:
                    info[key.strip()] = int(parts[0])
                except ValueError:
                    pass
    return info


def _meminfo_total_available_kb() -> tuple:
    """(MemTotal, available) in kB; available falls back to MemFree+Buffers+Cached."""
    info = _read_meminfo()
    total = info.get('MemTotal')
    available = info.get('MemAvailable')
    if available is None and 'MemFree' in info:
        # Kernels older than 3.14 (some DSM releases) don't report MemAvailable
        available = info['MemFree'] + info.get('Buffers', 0) + info.get('Cached', 0)
    return total, available


def fallback_total_ram_bytes() -> Optional[int]:
    """Total RAM without psutil, or None if it can't be read."""
    try:
        if sys.platform == 'win32':
            # Windows: using wmic
            import subprocess
            # timeout guards against wmic hanging, which would otherwise block
            # the entire metrics collection run indefinitely.
            output = subprocess.check_output(
                'wmic OS get TotalVisibleMemorySize /value', timeout=10
            ).decode()
            for line in output.split('\n'):
                if 'TotalVisibleMemorySize' in line:
                    # Value is in KB
                    total_kb = int(line.split('=')[1].strip())
                    return total_kb * 1024
            return None
        else:
            # Linux: from /proc/meminfo
            total_kb, _ = _meminfo_total_available_kb()
            return total_kb * 1024 if total_kb else None
    except Exception as e:
        logger.warning(f"Total-RAM fallback measurement failed: {e}")
        return None


def _build_exclude_patterns(config: dict) -> list:
    """Build the combined list of username exclusion glob patterns (lib/settings.py, shared with sessions)."""
    return settings.exclude_user_patterns(config)


# Linux accounts. UID_MIN (first UID of a regular login account) comes from
# /etc/login.defs; useradd --system allocates daemon accounts (labmon,
# systemd-resolve, nvidia-persistenced, ...) below it.
LOGIN_DEFS = '/etc/login.defs'
DEFAULT_UID_MIN = 1000
OVERFLOW_UID = 65534     # 'nobody' (also every unmapped UID seen from inside a user namespace)


def _uid_min() -> int:
    """UID_MIN from /etc/login.defs, or 1000 (the shadow-utils default) if it isn't set or readable."""
    try:
        with open(LOGIN_DEFS) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'UID_MIN':
                    value = int(parts[1])
                    if value > 0:
                        return value
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return DEFAULT_UID_MIN


def _is_system_uid(uid: int, uid_min: int) -> bool:
    """A daemon account's UID: 1..UID_MIN-1, or nobody. root (0) is not one (see _seed_user_processes)."""
    return 0 < uid < uid_min or uid == OVERFLOW_UID


def _real_uid(uids) -> Optional[int]:
    """The real UID from psutil's uids (real, effective, saved) - the one psutil's username names."""
    real = getattr(uids, 'real', None)
    return real if isinstance(real, int) and not isinstance(real, bool) else None


def _user_filter(config: dict, uid_of: Optional[dict] = None):
    """
    keep(pid, username, uid=None) -> bool: whether a process is counted for
    its user in per-user metrics (CPU and RAM on every path, and per-user
    GPU), so all user_metrics rows follow the same rules. uid: the real UID
    (POSIX), looked up in uid_of ({pid: uid}) when not given.

    Not counted, besides exclude_users and the Windows defaults:
      - the collector itself: its own process, and on Linux every process of
        its service account when that is a system account (UID below
        UID_MIN). That is also the daily disk scan, which runs as the same
        'labmon' for up to the total_timeout_seconds budget. On Windows both
        run as SYSTEM, which the default excludes hide.
      - on Linux ("exclude_system_accounts", default true there; false on
        Synology, which keeps its old behaviour): processes of daemon
        accounts, i.e. UID 1..UID_MIN-1 or nobody (65534) with an account
        name on this host - systemd-resolve, messagebus, nvidia-persistenced,
        ... - the counterpart of the NT AUTHORITY\\* / NT SERVICE\\* defaults.
        root stays visible: docker containers and sudo jobs, real workloads on
        a GPU server, run as root. A UID without an account here (psutil
        reports it as the number, e.g. a container's own user, or the procfs
        path as 'uid:<n>') is shown too.
    """
    import fnmatch

    exclude_patterns = [p.lower() for p in _build_exclude_patterns(config)]
    own_pid = os.getpid()
    posix = not sys.platform.startswith('win')
    hide_system = skip_uid = uid_min = None
    if posix:
        uid_min = _uid_min()
        hide_system = _cfg_bool(config, 'exclude_system_accounts', get_platform(config) == 'linux')
        own_uid = os.getuid() if hasattr(os, 'getuid') else None
        if own_uid is not None and _is_system_uid(own_uid, uid_min):
            skip_uid = own_uid
    uid_of = uid_of or {}
    excluded_names = {}     # username -> matches exclude_users (cached for the run)

    def keep(pid, username, uid=None) -> bool:
        if pid == own_pid or not username:
            return False
        if posix:
            if uid is None:
                uid = uid_of.get(pid)
            if uid is not None and (
                    uid == skip_uid
                    or (hide_system and _is_system_uid(uid, uid_min)
                        and username not in (str(uid), f'uid:{uid}'))):
                return False
        excluded = excluded_names.get(username)
        if excluded is None:
            lower = username.lower()
            excluded = excluded_names[username] = any(fnmatch.fnmatch(lower, p) for p in exclude_patterns)
        return not excluded
    return keep


def _seed_user_processes(config: dict) -> list:
    """
    The "perf_counters": false path. First pass: iterate all user processes,
    call cpu_percent() once (returns 0 — that is expected and intentional),
    and return the list of process dicts.

    The caller must wait at least ~1 second before calling get_per_user_stats()
    so that psutil can accumulate a meaningful CPU delta between the seed call
    and the real measurement call.

    Which processes count: see _user_filter.
    """
    try:
        import psutil

        keep = _user_filter(config)
        posix = not sys.platform.startswith('win')
        attrs = ['pid', 'username', 'memory_info']
        if posix:
            attrs.append('uids')        # not a valid attribute name on Windows
        seeded = []
        for proc in psutil.process_iter(attrs):
            try:
                username = proc.info.get('username') or ''
                uid = _real_uid(proc.info.get('uids')) if posix else None
                if not keep(proc.info.get('pid', getattr(proc, 'pid', None)), username, uid):
                    continue
                proc.cpu_percent(interval=None)   # seed — always 0, intentionally discarded
                seeded.append({
                    'proc':        proc,
                    'username':    username,
                    'memory_info': proc.info.get('memory_info'),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return seeded
    except Exception:
        return []


def get_per_user_stats(config: dict, logger: logging.Logger,
                       seeded_procs: list = None) -> list:
    """
    Aggregate CPU% and RAM (bytes) by user account across all processes.
    Returns a list sorted by cpu_percent descending. This is the
    "perf_counters": false path; by default per-user CPU comes from
    measure_counters() (CPU-seconds over the whole interval).

    seeded_procs: list returned by _seed_user_processes(), called at least
    ~1 second earlier.  If None, falls back to a fresh (unseeded) pass which
    will produce 0% CPU — callers should always pass seeded_procs.

    Each entry: {username, cpu_percent, ram_bytes, ram_formatted}
    Without psutil there are no per-user metrics: [] (logged, not an error).
    """
    try:
        import psutil
    except ImportError:
        logger.info("psutil is not installed: per-user CPU/RAM metrics are not collected")
        return []
    try:
        from collections import defaultdict
        import fnmatch

        user_cpu = defaultdict(float)
        user_ram = defaultdict(int)

        if seeded_procs is None:
            # Fallback: fresh unseeded pass (CPU will be 0 — caller should avoid this)
            logger.warning("get_per_user_stats called without seeded_procs — CPU will read 0")
            seeded_procs = _seed_user_processes(config)

        logger.debug(f"get_per_user_stats: second pass over {len(seeded_procs)} seeded processes")

        for item in seeded_procs:
            try:
                proc     = item['proc']
                username = item['username']
                # Second call — psutil now has a real delta since the seed call
                cpu_pct = proc.cpu_percent(interval=None) or 0.0
                user_cpu[username] += cpu_pct
                mem = item['memory_info']
                user_ram[username] += mem.rss if mem else 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Normalize per-user CPU to the same 0-100% scale as the system-level
        # cpu_percent metric.  psutil per-process cpu_percent() returns usage
        # relative to ONE logical core (can exceed 100% for multi-threaded
        # processes), while the system-level psutil.cpu_percent(interval=1)
        # always reports 0-100% across all cores.  Dividing by cpu_count
        # converts the per-core scale to the whole-system scale so the two
        # numbers are directly comparable in the dashboard.
        cpu_count = psutil.cpu_count(logical=True) or 1

        result = []
        for username, cpu_total in user_cpu.items():
            ram_b = user_ram[username]
            result.append({
                'username':      username,
                'cpu_percent':   round(cpu_total / cpu_count, 2),
                'ram_bytes':     ram_b,
                'ram_formatted': format_bytes(ram_b),
            })
        final_result = sorted(result, key=lambda x: x['cpu_percent'], reverse=True)
        logger.debug(f"get_per_user_stats: {len(final_result)} users")
        for user in final_result[:5]:
            logger.debug(f"  {user['username']}: {user['cpu_percent']}% | {user['ram_formatted']}")
        return final_result

    except Exception as e:
        logger.error(f"get_per_user_stats: Exception occurred: {e}", exc_info=True)
        return []


def get_gpu_stats() -> list:
    """
    Per-GPU stats via nvidia-ml-py (NVIDIA only): the 'gpus' list of
    system_metrics (see lib/gpu.collect, which collect_metrics calls with the
    process owners for the per-user part). An empty list means "GPU data
    unavailable" (no nvidia-ml-py, no driver, no GPUs), not an error.
    """
    return gpu.collect({}, None, None, logger).devices


def fallback_ram_percent() -> Optional[float]:
    """RAM usage (%) without psutil, or None if it can't be read."""
    try:
        if sys.platform == 'win32':
            # Windows: using wmic
            import subprocess
            # timeout guards against wmic hanging (see fallback_total_ram_bytes).
            output = subprocess.check_output(
                'wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value',
                timeout=10,
            ).decode()
            total = 0
            free = None
            for line in output.split('\n'):
                if 'TotalVisibleMemorySize' in line:
                    total = int(line.split('=')[1].strip())
                elif 'FreePhysicalMemory' in line:
                    free = int(line.split('=')[1].strip())
            if total > 0 and free is not None:
                return round((total - free) / total * 100, 1)
            return None
        else:
            # Linux: from /proc/meminfo
            total, available = _meminfo_total_available_kb()
            if not total or available is None:
                return None
            return round((total - available) / total * 100, 1)
    except Exception as e:
        logger.warning(f"RAM-percent fallback measurement failed: {e}")
        return None


def get_uptime_seconds() -> Optional[int]:
    """System uptime in seconds, or None if it can't be measured."""
    try:
        import psutil
        boot_time = psutil.boot_time()
        current_time = time.time()
        uptime_seconds = int(current_time - boot_time)
        return max(0, uptime_seconds)  # Ensure non-negative
    except ImportError:
        # Pure-Python fallback: /proc/uptime first field is seconds since boot.
        try:
            with open('/proc/uptime', 'r') as f:
                return max(0, int(float(f.read().split()[0])))
        except Exception:
            return None
    except Exception:
        return None


def format_uptime(seconds: Optional[int]) -> Optional[str]:
    """Format uptime in seconds to human-readable format (None stays None)"""
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"


def get_network_snapshot_path(config: dict, nas_name: str, create: bool = True) -> str:
    """Get path to network snapshot file"""
    return os.path.join(_device_dir(config, nas_name, create), "network_snapshot.json")


def read_network_snapshot(snapshot_path: str) -> dict:
    """Read previous network snapshot"""
    if not os.path.exists(snapshot_path):
        return None
    
    try:
        with open(snapshot_path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def write_network_snapshot(snapshot_path: str, data: dict) -> bool:
    """Write current network snapshot for next run (atomically)"""
    try:
        local_queue.atomic_write_json(snapshot_path, data)
        return True
    except Exception:
        return False


def _is_loopback(iface: str) -> bool:
    """
    True for loopback interfaces: 'lo' (Linux/Synology), 'lo0' (BSD/macOS),
    'Loopback Pseudo-Interface 1' (Windows). Loopback traffic never leaves the
    machine, so both the psutil path and the /proc/net/dev fallback exclude it
    (apart from the upgrade transition in _counts_loopback); so are Linux
    virtual interfaces (_is_virtual_nic).
    """
    n = iface.strip().lower()
    return n == 'lo' or (n.startswith('lo') and n[2:].isdigit()) or 'loopback' in n


SYS_CLASS_NET = '/sys/class/net'


def _is_virtual_nic(iface: str) -> bool:
    """
    True for a Linux interface with no device behind it: the kernel lists it
    under /sys/devices/virtual/net, so /sys/class/net/<iface> has no 'device'
    link. That is docker0, every container's vethXXXX, br-*, virbr*, bond0,
    VLANs, tun/tap, wireguard, tailscale0, ... Their traffic also crosses a
    physical NIC (counted there), stays inside the host (container to
    container), or both - and veths come and go with containers, so summing
    them makes the totals drop whenever one stops, which the Manager takes as
    a reboot. Physical, virtio, Xen and USB NICs have a device. Not Linux, or
    no sysfs entry for the name: False (counted, as before).
    """
    if not sys.platform.startswith('linux'):
        return False
    entry = os.path.join(SYS_CLASS_NET, iface.strip())
    try:
        return os.path.isdir(entry) and not os.path.exists(os.path.join(entry, 'device'))
    except (OSError, ValueError):
        return False


def _counted_interfaces(names, include_loopback: bool = False, include_virtual: bool = False) -> list:
    """
    The interfaces whose counters are summed: every one except loopback
    (unless include_loopback) and, on Linux, the virtual ones (unless
    include_virtual; see _is_virtual_nic). Where every non-loopback interface
    is virtual - a collector inside a container, whose eth0 is a veth - they
    are all counted rather than none.
    """
    counted = [n for n in names if include_loopback or not _is_loopback(n)]
    if include_virtual:
        return counted
    with_device = [n for n in counted if _is_loopback(n) or not _is_virtual_nic(n)]
    if any(not _is_loopback(n) for n in with_device) or all(_is_loopback(n) for n in counted):
        return with_device
    return counted


def fallback_network_counters(include_loopback: bool = False, include_virtual: bool = False) -> tuple:
    """
    Read cumulative network byte counters without psutil.

    Sums Receive/Transmit bytes from /proc/net/dev (Linux/Synology) over the
    interfaces _counted_interfaces() selects (not loopback, not virtual,
    unless asked to). Used when psutil is unavailable, which
    is the norm on Synology armv7l where psutil has no prebuilt wheel and
    cannot be compiled (no gcc).

    Returns:
        (bytes_in, bytes_out), or (None, None) if /proc/net/dev is unreadable
        (e.g. Windows) or has no parseable interface rows.
    """
    try:
        rows = {}
        with open('/proc/net/dev', 'r') as f:
            lines = f.readlines()[2:]  # skip the two header rows
        for line in lines:
            iface, _, data = line.partition(':')
            cols = data.split()
            if len(cols) < 16:
                continue
            rows[iface.strip()] = (int(cols[0]), int(cols[8]))   # Receive / Transmit bytes
        if not rows:
            return None, None
        counted = _counted_interfaces(rows, include_loopback, include_virtual)
        return sum(rows[n][0] for n in counted), sum(rows[n][1] for n in counted)
    except Exception as e:
        logger.warning(f"Network fallback (/proc/net/dev) failed: {e}")
        return None, None


def get_network_counters(include_loopback: bool = False, include_virtual: bool = False) -> tuple:
    """
    Cumulative (bytes_in, bytes_out) over the interfaces _counted_interfaces()
    selects: not loopback, not Linux virtual ones (unless include_loopback /
    include_virtual). (None, None) if unreadable.
    """
    try:
        import psutil
    except ImportError:
        return fallback_network_counters(include_loopback, include_virtual)
    try:
        nics = psutil.net_io_counters(pernic=True)
        counted = _counted_interfaces(nics, include_loopback, include_virtual)
        return sum(nics[n].bytes_recv for n in counted), sum(nics[n].bytes_sent for n in counted)
    except Exception as e:
        logger.warning(f"psutil network counters unavailable: {e}")
        return None, None


def _psutil_available() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


def _counts_loopback(previous: Optional[dict]) -> bool:
    """
    Whether this run still counts loopback traffic (upgrade transition).

    Collectors before this version summed every interface, loopback included,
    on the psutil path (the /proc/net/dev fallback already skipped 'lo'). If an
    upgraded psutil host dropped loopback at once, its counters would fall,
    which the Manager treats as a reboot: it would add all traffic since boot
    to the device's lifetime totals a second time. So a host whose network
    snapshot was written by an older collector keeps counting loopback until
    its counters next reset (a reboot), when the Manager starts from a new
    baseline anyway (see get_network_stats). The snapshot's "loopback" field
    records the choice.
    """
    if not previous:
        return False   # first run on this host: exclude loopback from the start
    scope = previous.get('loopback')
    if scope is not None:
        return scope == 'included'
    return _psutil_available()   # snapshot from an older collector


def _counts_virtual(previous: Optional[dict]) -> bool:
    """
    Whether this run still counts Linux virtual interfaces (upgrade
    transition, as for loopback in _counts_loopback): collectors before this
    version summed docker0, veths, bridges, bonds, tunnels... too. A host whose
    network snapshot doesn't record the choice ("virtual") keeps counting them
    until its counters next go backwards - a reboot, or a container's veth
    disappearing, which the Manager already takes as a reset - and then stops.
    New installs leave them out from the first run.
    """
    if not previous:
        return False
    scope = previous.get('virtual')
    if scope is not None:
        return scope == 'included'
    return True   # snapshot from an older collector


def _counters_went_backwards(previous: Optional[dict], bytes_in: int, bytes_out: int) -> bool:
    """True if either counter is below the previous snapshot's (reboot / counter reset)."""
    try:
        return bytes_in < int(previous['bytes_in']) or bytes_out < int(previous['bytes_out'])
    except (KeyError, TypeError, ValueError):
        return False


def get_network_stats(config: dict, nas_name: str, persist: bool = True) -> dict:
    """
    Network traffic: cumulative OS counters plus average bandwidth since the
    previous run (persist=False, for --dry-run: the snapshot is read but not
    written). Anything that can't be measured is None (never 0):
      - counters unreadable → all four values None, and the snapshot file is
        left untouched so the next good run still has a valid baseline;
      - first run, unreadable previous snapshot, or a counter that went
        backwards (reboot/reset) → bandwidth None for this run.
    Loopback and Linux virtual interfaces are not counted (except during the
    upgrade transitions described in _counts_loopback / _counts_virtual).
    """
    unmeasured = {
        'bytes_in': None,
        'bytes_out': None,
        'bandwidth_in_mbps': None,
        'bandwidth_out_mbps': None
    }
    try:
        # Read previous snapshot
        snapshot_path = get_network_snapshot_path(config, nas_name, create=persist)
        previous = read_network_snapshot(snapshot_path)
        if not isinstance(previous, dict):
            previous = None

        # Prefer psutil; fall back to /proc/net/dev when it isn't installed
        # (common on Synology armv7l where psutil can't be built).
        include_loopback = _counts_loopback(previous)
        include_virtual = _counts_virtual(previous)
        current_bytes_in, current_bytes_out = get_network_counters(include_loopback, include_virtual)
        if current_bytes_in is None or current_bytes_out is None:
            logger.warning("Network counters unavailable; sending network fields as null")
            return unmeasured
        if ((include_loopback or include_virtual)
                and _counters_went_backwards(previous, current_bytes_in, current_bytes_out)):
            # Reboot/reset: the Manager takes these counters as a new baseline,
            # so this is the moment to stop counting loopback and virtual
            # interfaces (_counts_loopback, _counts_virtual).
            dropped = [what for what, on in (('loopback', include_loopback),
                                             ('virtual-interface', include_virtual)) if on]
            include_loopback = include_virtual = False
            current_bytes_in, current_bytes_out = get_network_counters()
            if current_bytes_in is None or current_bytes_out is None:
                logger.warning("Network counters unavailable; sending network fields as null")
                return unmeasured
            logger.info(f"Network counters were reset; {' and '.join(dropped)} traffic is no longer "
                        f"counted from now on")

        current_timestamp = _utcnow().isoformat() + 'Z'
        
        # Calculate bandwidth if we have a usable previous snapshot
        bandwidth_in_mbps = None
        bandwidth_out_mbps = None
        
        if previous:
            try:
                prev_bytes_in = int(previous['bytes_in'])
                prev_bytes_out = int(previous['bytes_out'])
                prev_dt = datetime.fromisoformat(previous['timestamp'].replace('Z', '+00:00'))
                curr_dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                time_delta = (curr_dt - prev_dt).total_seconds()
                
                if (time_delta > 0 and current_bytes_in >= prev_bytes_in
                        and current_bytes_out >= prev_bytes_out):
                    # bytes/sec * 8 bits/byte / 1,000,000 bits/Mbps = Mbps
                    bandwidth_in_mbps = ((current_bytes_in - prev_bytes_in) / time_delta) * 8 / 1_000_000
                    bandwidth_out_mbps = ((current_bytes_out - prev_bytes_out) / time_delta) * 8 / 1_000_000
            except (KeyError, TypeError, ValueError, AttributeError):
                pass  # unusable previous snapshot → bandwidth unmeasured this run
        
        # Save current snapshot for next run
        if persist:
            write_network_snapshot(snapshot_path, {
                'timestamp': current_timestamp,
                'bytes_in': current_bytes_in,
                'bytes_out': current_bytes_out,
                'loopback': 'included' if include_loopback else 'excluded',
                'virtual': 'included' if include_virtual else 'excluded',
            })
        
        return {
            'bytes_in': current_bytes_in,
            'bytes_out': current_bytes_out,
            'bandwidth_in_mbps': bandwidth_in_mbps,
            'bandwidth_out_mbps': bandwidth_out_mbps
        }
    except Exception as e:
        logger.warning(f"Network stats failed: {e}")
        return unmeasured


def main():
    parser = argparse.ArgumentParser(description='Lab Monitor Collector')
    parser.add_argument('--config', required=True, help='Config file')
    parser.add_argument('--mode', choices=['disk', 'metrics', 'health'], default='disk',
                        help='Collection mode (health: a full storage-health check, sent now)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the messages as JSON on stdout; archive, queue, send and save nothing')
    parser.add_argument('--no-sync', action='store_true',
                        help='disk mode: archive and queue the result without sending it; the next '
                             'metrics run sends it (the Linux disk unit runs without network access)')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.config):
        print(f"ERROR: Config not found: {args.config}")
        sys.exit(1)
    
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        print(f"ERROR: Could not read config {args.config}: {e}")
        sys.exit(1)
    if not isinstance(config, dict):
        print(f"ERROR: Config {args.config} must be a JSON object")
        sys.exit(1)
    
    logger = setup_logging(config.get('log_file'), config.get('log_level', 'INFO'))
    logger.info(f"Starting collector ({args.mode} mode{', dry run' if args.dry_run else ''})")
    
    if args.mode == 'disk':
        success = collect_disk_usage(config, logger, dry_run=args.dry_run, sync=not args.no_sync)
    elif args.mode == 'health':
        success = collect_health(config, logger, dry_run=args.dry_run)
    else:
        success = collect_metrics(config, logger, dry_run=args.dry_run)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
