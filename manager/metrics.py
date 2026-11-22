"""
Metrics Storage Module — central summary database (dashboard layer).

Three tables, each with a clear purpose:

  devices          — device registry; one row per system, never deleted.
                     Records identity (name, id, device_type) and first/last seen.

  device_snapshot  — latest known state per device; one row per system.
                     Holds the most recent metrics (cpu, ram, uptime,
                     bandwidth) and disk state (total_disk_bytes, raw
                     folders_json).  Only a message at least as new as the
                     current snapshot overwrites it, so a gap-fill of older
                     messages (tools/replay_archives.py, retried 'error'
                     results) adds history without rolling the snapshot back.

  device_totals    — running cumulative counters per device.
                     Accumulates network bytes across reboots by tracking
                     the last-seen OS counter and adding deltas.
                     Also mirrors the current total_disk_bytes for
                     quick global-sum queries.

Plus dead_letter (messages the Manager rejected, with the raw message and
the reason) and the per-user tables managed by UserMetricsStore.

Latest state of the newer data types, one row per device each (only a
message at least as new as the stored one replaces it):

  device_session_snapshot   sessions: the counts of the latest message
                            (summary_json) and the latest connection list
                            (sessions_json), with when that list was sent
                            (list_timestamp) and last confirmed unchanged
                            (list_confirmed_at)
  device_storage_health     storage_health without the flattened history keys
  device_storage_breakdown  storage_breakdown summary: device owners (top 25
                            plus other) and per-folder totals with their top
                            owner, at most 50 KB

The per-user rows carry the usage fields of spec §12 (cpu_seconds,
cpu_cores, ..., gpu_processes); rows stored before them read NULL, and the
read API leaves a NULL usage field out of a row, so data an older Manager
stored reads back exactly as before.

All writes for one ingested message happen in one transaction
(apply_side_effects).  Device names are matched through
storage_util.normalize_device_name, so "Lab-A" and "lab-a" resolve to the
same registered device.  Rows are never renamed: an older Manager keyed
these tables by the raw name, so a database can hold two spellings that
normalize alike (a collector renamed from "Triton" to "triton", or two
machines "lab-a" and "lab_a").  Both rows are kept as they are and stay
readable under their own names; data sent under a registered spelling goes
to that row (as the older Manager did), data under a new spelling to the
alike row with the same system_id, else the spelling seen most recently
(see resolve_name).

A message timestamped more than FUTURE_SLACK_SECONDS ahead of this server's
clock (a collector with a bad clock) is stored in the history, but does not
move the device's last_seen, snapshots or network totals, and a stored
timestamp that far ahead (an older Manager stored any) never holds back a
current message.

Opening a metrics.db written by an older Manager adds the new tables and
columns once, after a consistent copy ("metrics.db.pre-upgrade-<stamp>.bak",
storage_util.backup_database).  If that copy fails nothing is migrated:
reads keep working and every write raises MigrationBlockedError (ingest
answers 'error', collectors keep their messages) until a retry succeeds.

Full historical data lives in TypedDataStore (data_store.py).
"""

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from storage_util import (
    UPGRADE_RETRY_SECONDS, LockedConnection, MigrationBlockedError, backup_database,
    normalize_device_name, parse_timestamp, timestamp_le, utc_now,
)

logger = logging.getLogger(__name__)


_INT64_MAX = 2 ** 63 - 1

# Network counters that went DOWN are a reboot / counter reset only if the
# reported uptime started after the previous reading (allowing this much
# clock and collection-time slack); a machine that has been up all along
# had its counters drop for another reason (an interface removed, a
# collector that stopped counting loopback), and that must not add all the
# traffic since boot to the lifetime totals again.
REBOOT_UPTIME_SLACK_SECONDS = 300

# Per-user usage fields (spec §12): columns of device_user_snapshot and
# user_metrics_history, NULL in rows stored before them
USER_EXTRA_COLUMNS = (
    ('cpu_seconds', 'REAL'), ('cpu_cores', 'REAL'), ('interval_s', 'REAL'), ('process_count', 'INTEGER'),
    ('gpu_vram_bytes', 'INTEGER'), ('gpu_util_percent', 'REAL'), ('gpu_busy_seconds', 'REAL'),
    ('gpus_held', 'INTEGER'), ('gpu_held_seconds', 'REAL'), ('gpu_processes', 'INTEGER'),
)
USER_EXTRA_FIELDS = tuple(c for c, _ in USER_EXTRA_COLUMNS)
# device_user_snapshot also keeps the timestamp its usage fields were written
# at: an older Manager that runs on this database for a while (a downgrade)
# updates cpu_percent, RAM and timestamp but not the usage columns, which
# are then shown only while usage_timestamp equals timestamp
USER_SNAPSHOT_COLUMNS = USER_EXTRA_COLUMNS + (('usage_timestamp', 'TEXT'),)

# system_metrics keys (spec §11) that /api/metrics/all passes through from
# the latest message, when that message has them
SYSTEM_METRICS_EXTRA = (
    'interval_s', 'cpu_count', 'cpu_iowait_percent', 'cpu_steal_percent', 'proc_cpu_coverage_percent',
    'disk_read_bytes_per_s', 'disk_write_bytes_per_s', 'disk_read_iops', 'disk_write_iops',
    'disk_await_ms', 'disk_busy_max_percent', 'gpu_sample_seconds',
)

# Snapshot tables of the newer data types
_NEW_TABLES = ('device_session_snapshot', 'device_storage_health', 'device_storage_breakdown')

# Tables and columns of the current central schema (an existing database
# missing any of them is upgraded, after a pre-upgrade backup)
_CURRENT_SCHEMA = {
    'devices':              (),
    'device_snapshot':      ('metrics_json',),
    'device_totals':        ('network_last_updated',),
    'dead_letter':          (),
    'device_user_snapshot': tuple(c for c, _ in USER_SNAPSHOT_COLUMNS),
    'user_metrics_history': ('msg_id',) + USER_EXTRA_FIELDS,
    'device_session_snapshot':  (),
    'device_storage_health':    (),
    'device_storage_breakdown': (),
}

# storage_health keys that only exist for history and rollups (spec §14):
# not part of the latest-state snapshot
_FLAT_HEALTH_KEY = re.compile(r'(?:disk|array|volume)_[a-z0-9_]+')
_HEALTH_FIELDS = ('overall_status', 'status_reasons', 'sources', 'arrays', 'disks', 'volumes', 'events')

# storage_breakdown snapshot: device owners kept (the rest folded into
# "other") and the size cap of one device's summary
BREAKDOWN_DEVICE_OWNERS = 25
BREAKDOWN_SNAPSHOT_BYTES = 50 * 1024
_BREAKDOWN_NUMBERS = ('bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')

# A message whose timestamp is further ahead of this server's clock than
# this (a collector with a wrong clock: dead RTC battery, before NTP sync)
# is stored in the history but does not move the device's "latest" state;
# a stored state timestamp that far ahead does not block current messages.
FUTURE_SLACK_SECONDS = 3600
FUTURE_WARN_INTERVAL_SECONDS = 3600

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _has_original_central_layout(conn) -> bool:
    """
    True while a metrics.db still has the layout an older Manager wrote
    (the original f086e7e one, or that of the release before usage metrics,
    with dead_letter and msg_id): none of the tables / columns only this
    Manager adds (the snapshot tables of the newer data types, the per-user
    usage columns).  _init_schema creates those tables in its first
    transaction, so a database this Manager started to migrate never looks
    original again and gets no second "pre-upgrade" copy.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables & set(_NEW_TABLES):
        return False
    for table in ('device_user_snapshot', 'user_metrics_history'):
        if table in tables and set(USER_EXTRA_FIELDS) & {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}:
            return False
    return True


def _number_or_none(value):
    """Finite numbers pass through; anything else (None, str, bool, NaN, ±inf) → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _scrub(value):
    """NaN / ±inf (not valid JSON) → None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _json_text(value) -> str:
    return json.dumps(_scrub(value), default=str, allow_nan=False)


def _json_value(text, default):
    """A stored JSON column (default when missing or unreadable)."""
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _user_extra(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The per-user usage fields of a user_metrics payload (unmeasured / not a number → None)."""
    return {k: _number_or_none(payload.get(k)) for k in USER_EXTRA_FIELDS}


def _seconds_ahead(timestamp) -> Optional[float]:
    """How many seconds `timestamp` lies ahead of this server's clock (negative: past); None if unparseable."""
    try:
        return (parse_timestamp(timestamp) - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError):
        return None


def _in_future(timestamp) -> bool:
    """True if a timestamp is more than FUTURE_SLACK_SECONDS ahead of this server's clock."""
    ahead = _seconds_ahead(timestamp)
    return ahead is not None and ahead > FUTURE_SLACK_SECONDS


def _not_older(timestamp: str, current: Optional[str]) -> bool:
    """
    True if a message at `timestamp` may replace state recorded at `current`.
    A recorded timestamp far in the future (a message from a collector with
    a wrong clock, stored by an older Manager) never blocks a current one.
    """
    return not current or timestamp_le(current, timestamp) or _in_future(current)


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The later of two stored timestamps (either may be missing; one far in the future is ignored)."""
    if not a or _in_future(a):
        return b
    if not b:
        return a
    return b if timestamp_le(a, b) else a


def _ts_key(value) -> datetime:
    """Sort key for a stored timestamp (unparseable / missing sorts first)."""
    try:
        return parse_timestamp(value)
    except ValueError:
        return _EPOCH


def _seconds_between(earlier: Optional[str], later: str) -> Optional[float]:
    try:
        return (parse_timestamp(later) - parse_timestamp(earlier)).total_seconds()
    except (ValueError, TypeError):
        return None


def _by_bytes(entry: dict):
    """Sort key: bytes descending, unmeasured last."""
    b = _number_or_none(entry.get('bytes'))
    return (b is None, -(b or 0))


def _totals(entry: dict, keys=_BREAKDOWN_NUMBERS) -> dict:
    return {k: _number_or_none(entry.get(k)) for k in keys}


def _breakdown_device(device) -> Optional[dict]:
    """
    The device rollup with its BREAKDOWN_DEVICE_OWNERS largest owners; the
    others are folded into "other" (owner_count and sizes added up; a size
    any of them did not measure is null).  owners null (not collected)
    stays null.
    """
    if not isinstance(device, dict):
        return None
    out = _totals(device)
    other = device.get('other')
    other = _totals(other, ('owner_count',) + _BREAKDOWN_NUMBERS) if isinstance(other, dict) else None
    owners = device.get('owners')
    if isinstance(owners, list):
        rows = sorted((o for o in owners if isinstance(o, dict)), key=_by_bytes)
        keep, rest = rows[:BREAKDOWN_DEVICE_OWNERS], rows[BREAKDOWN_DEVICE_OWNERS:]
        if rest:
            other = other or {'owner_count': 0, 'bytes': 0, 'files': 0, 'cold_1y_bytes': 0, 'cold_3y_bytes': 0}
            other['owner_count'] = (other.get('owner_count') or 0) + len(rest)
            for k in _BREAKDOWN_NUMBERS:
                values = [other.get(k)] + [_number_or_none(o.get(k)) for o in rest]
                other[k] = None if any(v is None for v in values) else sum(values)
        out['owners'] = [dict(owner=o.get('owner'), **_totals(o)) for o in keep]
    else:
        out['owners'] = None
    out['other'] = other
    return out


def _breakdown_summary(data: Dict[str, Any]) -> dict:
    """
    What /api/storage/breakdown/all shows of one storage_breakdown message:
    the device rollup (_breakdown_device) and every folder's totals with its
    top owner, folders sorted by bytes descending and cut so the summary
    stays within BREAKDOWN_SNAPSHOT_BYTES ("folders_truncated").
    """
    thresholds = data.get('cold_thresholds_days')
    owner_source = data.get('owner_source')
    summary = {
        'owner_source': owner_source if isinstance(owner_source, str) else None,
        'reference_time': data.get('reference_time') if isinstance(data.get('reference_time'), str) else None,
        'cold_thresholds_days': [_number_or_none(t) for t in thresholds] if isinstance(thresholds, list) else None,
        'device': _breakdown_device(data.get('device')),
        'volume_owners': _scrub(data.get('volume_owners')),
        'folders': [],
        'folders_truncated': False,
    }
    folders = []
    for f in data.get('folders') if isinstance(data.get('folders'), list) else ():
        if not isinstance(f, dict):
            continue
        owners = sorted((o for o in f.get('owners') or () if isinstance(o, dict)
                         and _number_or_none(o.get('bytes')) is not None), key=_by_bytes) \
            if isinstance(f.get('owners'), list) else []
        folder_owner = f.get('folder_owner')
        folders.append(dict(path=f.get('path'), folder_owner=folder_owner if isinstance(folder_owner, str) else None,
                            **_totals(f), top_owner=owners[0].get('owner') if owners else None,
                            top_owner_bytes=_number_or_none(owners[0].get('bytes')) if owners else None))
    folders.sort(key=_by_bytes)
    size = len(_json_text(summary))
    for f in folders:
        size += len(_json_text(f)) + 2
        if size > BREAKDOWN_SNAPSHOT_BYTES:
            summary['folders_truncated'] = True
            break
        summary['folders'].append(f)
    return summary


class MetricsDB:

    def __init__(self, db_path: str, data_dir: str = None, backup_before_migration: bool = True):
        self.db_path  = db_path
        self.data_dir = data_dir   # kept for signature compat; unused here
        self.backup_before_migration = backup_before_migration
        self.upgrade_blocked: Optional[str] = None
        self._upgrade_attempted = 0.0
        self._names: Dict[str, str] = {}
        self._registered: set = set()
        self._system_ids: Dict[str, Optional[str]] = {}
        self._spellings: Dict[str, set] = {}
        self._future_warned: Dict[str, float] = {}
        self._connect()
        self.users = UserMetricsStore(self, init=False)
        self._upgrade()
        self._load_name_index()

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def _connect(self):
        self._conn = LockedConnection(self.db_path)
        self._conn.conn.execute('PRAGMA foreign_keys=ON')
        # Raw connection kept for compatibility (autocommit mode)
        self.db = self._conn.conn

    def transaction(self):
        """Context manager: one BEGIN IMMEDIATE ... COMMIT on the central DB."""
        return self._write()

    @property
    def connection(self) -> LockedConnection:
        """The central DB's LockedConnection (a member of ingest's group commits)."""
        return self._conn

    def _write(self):
        """A write transaction; MigrationBlockedError while a migration is held back."""
        self._check_writable()
        return self._conn.transaction()

    # ------------------------------------------------------------------ #
    # One-time upgrade of a metrics.db written by an older Manager         #
    # ------------------------------------------------------------------ #

    def _upgrade_reasons(self) -> List[str]:
        """What an existing database still needs migrated ([] = current or new)."""
        tables = {r[0] for r in self._conn.query("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables & set(_CURRENT_SCHEMA):
            return []   # a new (empty) database: nothing to back up
        reasons = []
        for table, columns in _CURRENT_SCHEMA.items():
            if table not in tables:
                reasons.append(f'no {table} table')
                continue
            have = self._columns(table)
            missing = [c for c in columns if c not in have]
            if missing:
                reasons.append(f"{table} has no {', '.join(missing)}")
        return reasons

    def _upgrade(self) -> bool:
        """
        Create / migrate the schema.  An existing database that needs changes
        is first copied with backup_database() (unless backup_before_migration
        is off); if that fails, nothing is changed and upgrade_blocked is set.
        Returns True if the schema is current.
        """
        with self._conn.lock:
            self._upgrade_attempted = time.monotonic()
            reasons = self._upgrade_reasons()
            if reasons:
                reason = '; '.join(reasons)
                if self.backup_before_migration:
                    try:
                        backup_database(self._conn.conn, self.db_path, reason,
                                        original_layout=_has_original_central_layout)
                    except Exception as e:
                        self.upgrade_blocked = reason
                        logger.error(
                            f"NOT migrating {self.db_path}: the pre-upgrade backup failed "
                            f"({type(e).__name__}: {e}). Reads keep working on the old schema; "
                            f"every new message gets 'error' and stays queued on its collector. "
                            f"Free disk space / fix permissions (retried every "
                            f"{UPGRADE_RETRY_SECONDS}s on the next write), or set "
                            f"\"backup_before_migration\": false to migrate without a backup."
                        )
                        return False
                else:
                    logger.warning(f"Migrating {self.db_path} WITHOUT a backup "
                                   f"(backup_before_migration is false): {reason}")
            self._init_schema()
            self.users._init_tables()
            self.upgrade_blocked = None
            return True

    def _check_writable(self):
        if not self.upgrade_blocked:
            return
        with self._conn.lock:
            if self.upgrade_blocked and \
                    time.monotonic() - self._upgrade_attempted >= UPGRADE_RETRY_SECONDS:
                self._conn.group_barrier('the held-back metrics.db migration')
                if self._upgrade():
                    self._load_name_index()
            if self.upgrade_blocked:
                raise MigrationBlockedError(
                    f"{self.db_path} needs a migration that is held back until its "
                    f"pre-upgrade backup succeeds")

    # ------------------------------------------------------------------ #
    # Schema                                                               #
    # ------------------------------------------------------------------ #

    def _columns(self, table: str) -> set:
        return {row[1] for row in self._conn.query(f'PRAGMA table_info("{table}")')}

    def _has_table(self, table: str) -> bool:
        """False while a held-back migration has not created it yet (reads then return nothing)."""
        return self._conn.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                                    (table,)) is not None

    def _init_schema(self):
        with self._conn.transaction() as db:
            # ---- device registry ----------------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS devices (
                    name        TEXT PRIMARY KEY,
                    system_id   TEXT,
                    device_type TEXT DEFAULT 'unknown',
                    first_seen  DATETIME NOT NULL,
                    last_seen   DATETIME NOT NULL
                )
            ''')

            # ---- latest snapshot per device ------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_snapshot (
                    name                        TEXT PRIMARY KEY,
                    metrics_timestamp           DATETIME,
                    cpu_percent                 REAL,
                    ram_percent                 REAL,
                    uptime_seconds              INTEGER DEFAULT 0,
                    uptime_formatted            TEXT    DEFAULT '0s',
                    network_bandwidth_in_mbps   REAL    DEFAULT 0,
                    network_bandwidth_out_mbps  REAL    DEFAULT 0,
                    disk_timestamp              DATETIME,
                    total_disk_bytes            INTEGER DEFAULT 0,
                    folders_json                TEXT    DEFAULT '{}',
                    metrics_json                TEXT    DEFAULT '{}'
                )
            ''')

            # ---- running cumulative counters ------------------------------ #
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_totals (
                    name                 TEXT PRIMARY KEY,
                    total_bytes_in       INTEGER DEFAULT 0,
                    total_bytes_out      INTEGER DEFAULT 0,
                    last_bytes_in        INTEGER DEFAULT 0,
                    last_bytes_out       INTEGER DEFAULT 0,
                    total_disk_bytes     INTEGER DEFAULT 0,
                    last_updated         DATETIME,
                    network_last_updated DATETIME
                )
            ''')

            # ---- rejected messages ---------------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS dead_letter (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    device_name TEXT,
                    msg_id      TEXT,
                    data_type   TEXT,
                    reason      TEXT NOT NULL,
                    raw_json    TEXT NOT NULL
                )
            ''')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_dead_letter_msg_id ON dead_letter (msg_id)')

            # ---- latest state of the newer data types ---------------------- #
            # (created in this first transaction: see _has_original_central_layout)
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_session_snapshot (
                    name              TEXT PRIMARY KEY,
                    timestamp         TEXT NOT NULL,
                    summary_json      TEXT NOT NULL DEFAULT '{}',
                    sessions_json     TEXT NOT NULL DEFAULT '[]',
                    list_hash         TEXT,
                    list_timestamp    TEXT,
                    list_confirmed_at TEXT
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_storage_health (
                    name           TEXT PRIMARY KEY,
                    timestamp      TEXT NOT NULL,
                    overall_status TEXT,
                    summary_json   TEXT NOT NULL DEFAULT '{}'
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_storage_breakdown (
                    name         TEXT PRIMARY KEY,
                    timestamp    TEXT NOT NULL,
                    owner_source TEXT,
                    summary_json TEXT NOT NULL DEFAULT '{}'
                )
            ''')

        # Migration: add metrics_json to existing databases that pre-date it
        if 'metrics_json' not in self._columns('device_snapshot'):
            self._conn.execute("ALTER TABLE device_snapshot ADD COLUMN metrics_json TEXT DEFAULT '{}'")
            logger.info("Migrated device_snapshot: added metrics_json column")

        # Migration: network replay guard gets its own timestamp, so a
        # folder_usage update (which also sets last_updated) can't make a
        # later-arriving metrics message look like a replay.  The old Manager
        # set last_bytes_* and device_snapshot.metrics_timestamp from the same
        # (last processed) system_metrics message, so that is when the stored
        # counters were read; last_updated, which folder_usage messages also
        # moved, is the fallback.  Only the new column is written.
        if 'network_last_updated' not in self._columns('device_totals'):
            with self._conn.transaction() as db:
                db.execute('ALTER TABLE device_totals ADD COLUMN network_last_updated DATETIME')
                db.execute('''
                    UPDATE device_totals SET network_last_updated = COALESCE(
                        (SELECT s.metrics_timestamp FROM device_snapshot s
                          WHERE s.name = device_totals.name),
                        last_updated)
                ''')
            logger.info("Migrated device_totals: added network_last_updated column")

    # ------------------------------------------------------------------ #
    # Device names                                                         #
    # ------------------------------------------------------------------ #

    def _load_name_index(self):
        """
        Map normalized name → the registered spelling data under a NEW
        (unregistered) spelling is filed under.

        An older Manager keyed the central tables by the raw device name, so
        two registered spellings can share a normalized name: a collector
        renamed from 'Triton' to 'triton', or two machines 'lab-a' and
        'lab_a'.  Neither row is changed or merged: each keeps its own
        snapshot and lifetime totals and stays readable under its own name,
        and data sent under a registered spelling goes to that row, exactly
        as the older Manager filed it (resolve_name).  A spelling not
        registered yet goes to the spelling seen MOST RECENTLY, whose last
        network counters are current; adding a new reading to a stale row
        would count the traffic since that row's last reading again.
        """
        try:
            rows = self._conn.query('SELECT name, system_id, first_seen, last_seen FROM devices')
        except Exception as e:   # only possible while a migration is held back
            logger.error(f"Could not read the device registry: {e}")
            rows = []
        groups: Dict[str, list] = {}
        for row in rows:
            groups.setdefault(normalize_device_name(row['name']), []).append(row)
        names: Dict[str, str] = {}
        for key, group in groups.items():
            group.sort(key=lambda r: (_ts_key(r['last_seen']), _ts_key(r['first_seen']), r['name']),
                       reverse=True)
            names[key] = group[0]['name']
            if len(group) > 1:
                logger.warning(
                    f"Central DB has devices {', '.join(repr(r['name']) for r in group)} that "
                    f"normalize to the same name (registered by an older Manager: a renamed "
                    f"collector, or different machines). All rows are kept unchanged and each "
                    f"stays readable under its own name; data sent under one of these names goes "
                    f"to that row, data under another spelling to the most recently seen, "
                    f"{names[key]!r}. They share one history directory."
                )
        self._names = names
        self._registered = {row['name'] for row in rows}
        self._system_ids = {row['name']: row['system_id'] for row in rows}
        self._spellings = {key: {row['name'] for row in group} for key, group in groups.items()}

    def resolve_name(self, name: str, system_id=None) -> str:
        """
        The registered row new data for this device is filed under: the row
        of exactly this spelling if one is registered (the older Manager
        keyed rows by the raw name, so 'lab-a' and 'lab_a' keep their own
        rows), else a registered spelling that normalizes alike (a renamed
        collector continues its row): the one with the same system_id, else
        the most recently seen.  A new spelling whose system_id differs from
        that of every alike registered row is another machine: it gets its
        own row (the name itself), as the older Manager gave it.
        """
        with self._conn.lock:
            if name in self._registered:
                return name
            key = normalize_device_name(name)
            default = self._names.get(key)
            if default is None:
                return name
            if system_id is not None and str(system_id):
                ids = {n: self._system_ids.get(n) for n in self._spellings.get(key, {default})}
                same = sorted(n for n, i in ids.items() if i is not None and str(i) == str(system_id))
                if same:
                    return default if default in same else same[0]
                if all(i is not None and str(i) for i in ids.values()):
                    return name
            return default

    def resolve_read_name(self, name: str) -> str:
        """
        The registered row a read for `name` should return: the row of that
        exact spelling if one is registered (so every row an older Manager
        stored stays reachable), else the spelling resolve_name() picks.
        """
        with self._conn.lock:
            if name in self._registered:
                return name
            return self._names.get(normalize_device_name(name), name)

    def is_registered(self, name: str) -> bool:
        """True if exactly this spelling is in the device registry."""
        with self._conn.lock:
            return name in self._registered

    def is_known(self, name: str) -> bool:
        with self._conn.lock:
            return normalize_device_name(name) in self._names

    def spellings(self, name: str) -> List[str]:
        """Every registered spelling that normalizes like `name` (sorted)."""
        key = normalize_device_name(name)
        with self._conn.lock:
            return sorted(n for n in self._registered if normalize_device_name(n) == key)

    # ------------------------------------------------------------------ #
    # Write — one message                                                  #
    # ------------------------------------------------------------------ #

    def apply_side_effects(self, name: str, system_id: str, device_type: str,
                           timestamp: str, data_type: str, payload: Dict[str, Any],
                           msg_id: Optional[str] = None, raw_timestamp: Optional[str] = None):
        """
        Apply every central-DB update for one newly stored message in a
        single transaction: device registry, then the data-type specific
        snapshot / running totals / per-user rows.  Raises on failure (the
        transaction is rolled back).  raw_timestamp (the header string) lets
        a message be matched against per-user history rows an older Manager
        stored (see UserMetricsStore._ingest).
        """
        if _in_future(timestamp):
            self._warn_future(name, timestamp)
            with self._write() as db:
                # History only: a device seen for the first time is
                # registered at this server's time, nothing else moves.
                if db.execute('SELECT 1 FROM devices WHERE name = ?', (name,)).fetchone() is None:
                    self._upsert_device(db, name, system_id, device_type, utc_now())
                self._remember_name(name, system_id)
                if data_type == 'user_metrics':
                    self.users._ingest(
                        db, name, timestamp,
                        payload.get('username'),
                        _number_or_none(payload.get('cpu_percent')),
                        _number_or_none(payload.get('ram_bytes')),
                        payload.get('ram_formatted'),
                        msg_id,
                        raw_timestamp=raw_timestamp,
                        update_snapshot=False,
                        extra=_user_extra(payload),
                    )
            return

        with self._write() as db:
            self._upsert_device(db, name, system_id, device_type, timestamp)
            self._remember_name(name, system_id)
            if data_type == 'system_metrics':
                self._update_snapshot_metrics(db, name, timestamp, payload)
                self._accumulate_network(
                    db, name,
                    payload.get('network_bytes_in'),
                    payload.get('network_bytes_out'),
                    timestamp,
                    uptime_seconds=payload.get('uptime_seconds'),
                )
            elif data_type == 'folder_usage':
                if self._update_snapshot_disk(db, name, timestamp, payload):
                    self._update_disk_total(db, name, payload.get('total_usage'), timestamp)
            elif data_type == 'user_metrics':
                self.users._ingest(
                    db, name, timestamp,
                    payload.get('username'),
                    _number_or_none(payload.get('cpu_percent')),
                    _number_or_none(payload.get('ram_bytes')),
                    payload.get('ram_formatted'),
                    msg_id,
                    raw_timestamp=raw_timestamp,
                    extra=_user_extra(payload),
                )
            elif data_type == 'sessions':
                self._update_session_snapshot(db, name, timestamp, payload)
            elif data_type == 'storage_health':
                self._update_health_snapshot(db, name, timestamp, payload)
            elif data_type == 'storage_breakdown':
                self._update_breakdown_snapshot(db, name, timestamp, payload)
            # Unknown / not_specified: nothing else to do in central DB

    def _warn_future(self, name: str, timestamp: str):
        """Log a message from the future (at most once per device per FUTURE_WARN_INTERVAL_SECONDS)."""
        now = time.monotonic()
        last = self._future_warned.get(name)
        if last is not None and now - last < FUTURE_WARN_INTERVAL_SECONDS:
            return
        self._future_warned[name] = now
        ahead = _seconds_ahead(timestamp) or 0.0
        logger.warning(
            f"{name}: message timestamp {timestamp} is {ahead / 3600:.1f} h ahead of this server's "
            f"clock ({utc_now()}). It is stored in the history, but it does not move the device's "
            f"last-seen time, snapshot, per-user snapshot or network totals. Check the clock of "
            f"{name} (or of this server). Logged at most once an hour per device."
        )

    def add_dead_letter(self, device_name: Optional[str], msg_id: Optional[str],
                        data_type: Optional[str], reason: str, raw: Any):
        """Record a rejected message (first rejection of a msg_id wins)."""
        try:
            raw_json = json.dumps(raw, default=str)
        except (TypeError, ValueError):
            raw_json = json.dumps(str(raw))
        with self._write() as db:
            db.execute('''
                INSERT OR IGNORE INTO dead_letter
                    (received_at, device_name, msg_id, data_type, reason, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (utc_now(), device_name, msg_id,
                  data_type if isinstance(data_type, str) else None, reason, raw_json))

    def get_dead_letters(self, limit: int = 100) -> List[Dict]:
        rows = self._conn.query(
            'SELECT * FROM dead_letter ORDER BY id DESC LIMIT ?', (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Write — device registry                                              #
    # ------------------------------------------------------------------ #

    def _upsert_device(self, db, name, system_id, device_type, timestamp):
        """
        Register on first contact.  Later messages move last_seen (and the
        identity fields) forward only when they are newer, and first_seen
        back when they are older, so stored order doesn't matter.
        """
        row = db.execute('SELECT first_seen, last_seen FROM devices WHERE name = ?', (name,)).fetchone()
        if row is None:
            db.execute('''
                INSERT INTO devices (name, system_id, device_type, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, system_id, device_type, timestamp, timestamp))
            # Snapshots left by an older Manager's DELETE /api/system/<name>
            # (it does not know these tables, so a downgrade could leave them):
            # they belong to the deleted device, not to this new registration
            ph = ', '.join('?' * len(_NEW_TABLES))
            for (table,) in db.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({ph})",
                                       _NEW_TABLES).fetchall():
                db.execute(f'DELETE FROM {table} WHERE name = ?', (name,))
            return
        if _not_older(timestamp, row['last_seen']):
            db.execute('''
                UPDATE devices SET system_id = ?, device_type = ?, last_seen = ?
                WHERE name = ?
            ''', (system_id, device_type, timestamp, name))
        if row['first_seen'] and not timestamp_le(row['first_seen'], timestamp):
            db.execute('UPDATE devices SET first_seen = ? WHERE name = ?', (timestamp, name))

    def _remember_name(self, name: str, system_id=None):
        # Called inside the write transaction (lock held); a rolled-back
        # registration only leaves a harmless alias for the same name.
        key = normalize_device_name(name)
        self._names.setdefault(key, name)
        self._spellings.setdefault(key, set()).add(name)
        self._registered.add(name)
        if system_id is not None or name not in self._system_ids:
            self._system_ids[name] = system_id if system_id is None else str(system_id)

    def upsert_device(self, name: str, system_id: str,
                      device_type: str, timestamp: str) -> bool:
        """Register a device on first contact; update last_seen on every ingest."""
        try:
            with self._write() as db:
                self._upsert_device(db, name, system_id, device_type, timestamp)
                self._remember_name(name, system_id)
            return True
        except Exception as e:
            logger.error(f"upsert_device failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — snapshot                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_units(data: Dict[str, Any]) -> Dict[str, str]:
        """
        Pull all unit fields out of a raw payload dict.
        Captures both per-field siblings ({field}_unit) and the
        message-level default (_unit).
        """
        return {k: v for k, v in data.items()
                if k == '_unit' or k.endswith('_unit')}

    @staticmethod
    def _update_snapshot_metrics(db, name, timestamp, data) -> bool:
        """
        Overwrite the metrics columns unless the snapshot already holds a
        newer system_metrics message.  Returns True if it was updated.
        """
        # Full raw payload kept as metrics_json so unit fields and any future
        # collector additions are preserved alongside the fast-access columns.
        # Unmeasured values are stored as NULL, never 0.
        db.execute('INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,))
        current = db.execute(
            'SELECT metrics_timestamp FROM device_snapshot WHERE name = ?', (name,)).fetchone()[0]
        if not _not_older(timestamp, current):
            return False
        metrics_json = json.dumps(
            {k: v for k, v in data.items() if k != 'data_type'}, default=str
        )
        db.execute('''
            UPDATE device_snapshot SET
                metrics_timestamp          = ?,
                cpu_percent                = ?,
                ram_percent                = ?,
                uptime_seconds             = ?,
                uptime_formatted           = ?,
                network_bandwidth_in_mbps  = ?,
                network_bandwidth_out_mbps = ?,
                metrics_json               = ?
            WHERE name = ?
        ''', (
            timestamp,
            _number_or_none(data.get('cpu_percent')),
            _number_or_none(data.get('ram_percent')),
            _number_or_none(data.get('uptime_seconds')),
            data.get('uptime_formatted') if isinstance(data.get('uptime_formatted'), str) else None,
            _number_or_none(data.get('network_bandwidth_in_mbps')),
            _number_or_none(data.get('network_bandwidth_out_mbps')),
            metrics_json,
            name,
        ))
        return True

    def update_snapshot_metrics(self, name: str, timestamp: str,
                                 data: Dict[str, Any]) -> bool:
        """
        Update the metrics columns of device_snapshot from a system_metrics payload.
        Creates the row if it doesn't exist yet (disk columns default to empty).
        """
        try:
            with self._write() as db:
                self._update_snapshot_metrics(db, name, timestamp, data)
            return True
        except Exception as e:
            logger.error(f"update_snapshot_metrics failed for {name}: {e}")
            return False

    @staticmethod
    def _update_snapshot_disk(db, name, timestamp, data) -> bool:
        """
        Overwrite the disk columns unless the snapshot already holds a newer
        folder_usage message.  Returns True if it was updated.
        """
        # Full payload (minus data_type) kept as folders_json so the dashboard
        # can reconstruct the folder breakdown without hitting the history DB.
        db.execute('INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,))
        current = db.execute(
            'SELECT disk_timestamp FROM device_snapshot WHERE name = ?', (name,)).fetchone()[0]
        if not _not_older(timestamp, current):
            return False
        folders_json = json.dumps(
            {k: v for k, v in data.items() if k != 'data_type'}, default=str
        )
        db.execute('''
            UPDATE device_snapshot SET
                disk_timestamp   = ?,
                total_disk_bytes = ?,
                folders_json     = ?
            WHERE name = ?
        ''', (timestamp, _number_or_none(data.get('total_usage')), folders_json, name))
        return True

    def update_snapshot_disk(self, name: str, timestamp: str,
                              data: Dict[str, Any]) -> bool:
        """Update the disk columns of device_snapshot from a folder_usage payload."""
        try:
            with self._write() as db:
                self._update_snapshot_disk(db, name, timestamp, data)
            return True
        except Exception as e:
            logger.error(f"update_snapshot_disk failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — snapshots of the newer data types                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _update_session_snapshot(db, name, timestamp, data) -> bool:
        """
        Latest sessions state, unless a newer sessions message is stored.
          summary   every non-list key of the message (counts, _sources, ...)
          the list  "_list_state" full: replaced, list_timestamp and
                    list_confirmed_at = this message; "unchanged" with the
                    stored list's hash: kept, list_confirmed_at = this
                    message; "unchanged" with another hash: kept as it is
                    (its real age stays visible); "none": cleared.
        Returns True if it was updated.
        """
        row = db.execute('SELECT * FROM device_session_snapshot WHERE name = ?', (name,)).fetchone()
        if row is not None and not _not_older(timestamp, row['timestamp']):
            return False
        summary = {k: v for k, v in data.items()
                   if k not in ('data_type', 'sessions') and not isinstance(v, list)}
        if row is not None:
            sessions_json, list_hash = row['sessions_json'], row['list_hash']
            list_ts, confirmed = row['list_timestamp'], row['list_confirmed_at']
        else:
            sessions_json, list_hash, list_ts, confirmed = '[]', None, None, None
        state, rows = data.get('_list_state'), data.get('sessions')
        sent_hash = data.get('_list_hash') if isinstance(data.get('_list_hash'), str) else None
        if state == 'full' or (state is None and isinstance(rows, list)):
            sessions_json = _json_text(rows if isinstance(rows, list) else [])
            list_hash, list_ts, confirmed = sent_hash, timestamp, timestamp
        elif state == 'unchanged':
            if list_ts is not None and sent_hash is not None and sent_hash == list_hash:
                confirmed = timestamp
        elif state == 'none':
            sessions_json, list_hash, list_ts, confirmed = '[]', None, None, None
        db.execute('''
            INSERT INTO device_session_snapshot
                (name, timestamp, summary_json, sessions_json, list_hash, list_timestamp, list_confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                timestamp         = excluded.timestamp,
                summary_json      = excluded.summary_json,
                sessions_json     = excluded.sessions_json,
                list_hash         = excluded.list_hash,
                list_timestamp    = excluded.list_timestamp,
                list_confirmed_at = excluded.list_confirmed_at
        ''', (name, timestamp, _json_text(summary), sessions_json, list_hash, list_ts, confirmed))
        return True

    @staticmethod
    def _update_health_snapshot(db, name, timestamp, data) -> bool:
        """Latest storage_health message without its flattened history keys, unless a newer one is stored."""
        row = db.execute('SELECT timestamp FROM device_storage_health WHERE name = ?', (name,)).fetchone()
        if row is not None and not _not_older(timestamp, row['timestamp']):
            return False
        summary = {k: v for k, v in data.items()
                   if k != 'data_type' and not (isinstance(k, str) and _FLAT_HEALTH_KEY.fullmatch(k))}
        status = data.get('overall_status')
        db.execute('''
            INSERT INTO device_storage_health (name, timestamp, overall_status, summary_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                timestamp      = excluded.timestamp,
                overall_status = excluded.overall_status,
                summary_json   = excluded.summary_json
        ''', (name, timestamp, status if isinstance(status, str) else None, _json_text(summary)))
        return True

    @staticmethod
    def _update_breakdown_snapshot(db, name, timestamp, data) -> bool:
        """Summary of the latest storage_breakdown message (_breakdown_summary), unless a newer one is stored."""
        row = db.execute('SELECT timestamp FROM device_storage_breakdown WHERE name = ?', (name,)).fetchone()
        if row is not None and not _not_older(timestamp, row['timestamp']):
            return False
        summary = _breakdown_summary(data)
        db.execute('''
            INSERT INTO device_storage_breakdown (name, timestamp, owner_source, summary_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                timestamp    = excluded.timestamp,
                owner_source = excluded.owner_source,
                summary_json = excluded.summary_json
        ''', (name, timestamp, summary['owner_source'], _json_text(summary)))
        return True

    # ------------------------------------------------------------------ #
    # Write — running totals                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _accumulate_network(db, name, current_bytes_in, current_bytes_out, timestamp,
                            uptime_seconds=None) -> bool:
        """
        Add the traffic since the last reading to the lifetime totals.

          - either counter null/missing  → skip (unmeasured, never treated as 0)
          - timestamp <= last network update → skip (replay / out-of-order)
          - current < last:
              * uptime_seconds unknown (null/missing), or shorter than the time
                since the last reading (+ REBOOT_UPTIME_SLACK_SECONDS): a reboot
                or counter reset → delta = current (traffic since boot)
              * uptime shows the machine has been up since the last reading:
                not a reboot (interface removed, loopback no longer counted,
                ...) → delta = 0 and the counter is re-baselined, so the
                traffic since boot is not counted a second time
          - otherwise delta = current - last

        On first contact the current counter value seeds the totals.
        Returns True if the totals were updated.
        """
        cur_in, cur_out = _number_or_none(current_bytes_in), _number_or_none(current_bytes_out)
        if cur_in is None or cur_out is None:
            return False
        cur_in, cur_out = int(cur_in), int(cur_out)
        if cur_in < 0 or cur_out < 0 or cur_in > _INT64_MAX or cur_out > _INT64_MAX:
            logger.warning(f"Ignoring out-of-range network counters from {name}")
            return False

        row = db.execute('SELECT * FROM device_totals WHERE name = ?', (name,)).fetchone()
        if row:
            last_ts = row['network_last_updated']
            # A last reading stamped far in the future (stored from a collector
            # with a wrong clock, e.g. by an older Manager) blocks nothing.
            future_last = _in_future(last_ts or row['last_updated'])
            if last_ts and timestamp_le(timestamp, last_ts) and not future_last:
                logger.info(
                    f"Skipping network accumulation for {name}: message {timestamp} "
                    f"is not newer than last update {last_ts}"
                )
                return False
            last_in, last_out = row['last_bytes_in'] or 0, row['last_bytes_out'] or 0
            reset_delta = MetricsDB._reset_delta_factor(
                name, uptime_seconds, None if future_last else (last_ts or row['last_updated']),
                timestamp, cur_in < last_in or cur_out < last_out)
            delta_in  = cur_in * reset_delta  if cur_in  < last_in  else cur_in  - last_in
            delta_out = cur_out * reset_delta if cur_out < last_out else cur_out - last_out
            new_total_in  = (row['total_bytes_in']  or 0) + delta_in
            new_total_out = (row['total_bytes_out'] or 0) + delta_out
            last_updated  = _later(row['last_updated'], timestamp)
        else:
            # First time we see this device — seed with current OS counter
            new_total_in, new_total_out = cur_in, cur_out
            last_updated = timestamp

        db.execute('''
            INSERT INTO device_totals
                (name, total_bytes_in, total_bytes_out,
                 last_bytes_in, last_bytes_out, last_updated, network_last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                total_bytes_in       = excluded.total_bytes_in,
                total_bytes_out      = excluded.total_bytes_out,
                last_bytes_in        = excluded.last_bytes_in,
                last_bytes_out       = excluded.last_bytes_out,
                last_updated         = excluded.last_updated,
                network_last_updated = excluded.network_last_updated
        ''', (name, new_total_in, new_total_out,
              cur_in, cur_out, last_updated, timestamp))
        return True

    @staticmethod
    def _reset_delta_factor(name, uptime_seconds, last_reading: Optional[str], timestamp: str,
                            decreased: bool) -> int:
        """
        1 if a counter decrease is a reboot / reset (count the current value as
        traffic since boot), 0 if the reported uptime shows the machine has
        been up since the previous reading (count nothing, re-baseline).
        """
        if not decreased:
            return 1
        uptime = _number_or_none(uptime_seconds)
        elapsed = _seconds_between(last_reading, timestamp)
        if uptime is None or uptime < 0 or elapsed is None:
            return 1      # contract 6: no uptime to go by → reboot
        if uptime < elapsed + REBOOT_UPTIME_SLACK_SECONDS:
            return 1      # booted after (or about when) the previous reading
        logger.warning(
            f"Network counters of {name} went down at {timestamp} although it reports "
            f"{uptime:.0f}s uptime ({elapsed:.0f}s since the previous reading at "
            f"{last_reading}): not a reboot, so nothing is added for the lower counter "
            f"and it is re-baselined"
        )
        return 0

    def accumulate_network(self, name: str, current_bytes_in: Optional[int],
                           current_bytes_out: Optional[int], timestamp: str,
                           uptime_seconds=None) -> bool:
        """Atomic (read + write in one transaction) wrapper; see _accumulate_network."""
        try:
            with self._write() as db:
                return self._accumulate_network(db, name, current_bytes_in, current_bytes_out,
                                                timestamp, uptime_seconds=uptime_seconds)
        except Exception as e:
            logger.error(f"accumulate_network failed for {name}: {e}")
            return False

    @staticmethod
    def _update_disk_total(db, name, total_disk_bytes, timestamp):
        row = db.execute('SELECT last_updated FROM device_totals WHERE name = ?', (name,)).fetchone()
        last_updated = _later(row['last_updated'], timestamp) if row else timestamp
        db.execute('''
            INSERT INTO device_totals (name, total_disk_bytes, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                total_disk_bytes = excluded.total_disk_bytes,
                last_updated     = excluded.last_updated
        ''', (name, _number_or_none(total_disk_bytes), last_updated))

    def update_disk_total(self, name: str, total_disk_bytes: Optional[int],
                          timestamp: str) -> bool:
        """Mirror the latest total_disk_bytes into device_totals for global sums."""
        try:
            with self._write() as db:
                self._update_disk_total(db, name, total_disk_bytes, timestamp)
            return True
        except Exception as e:
            logger.error(f"update_disk_total failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Read — device registry                                               #
    # ------------------------------------------------------------------ #

    def get_all_devices(self) -> List[Dict]:
        return [dict(row) for row in self._conn.query('SELECT * FROM devices ORDER BY name')]

    def get_device(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one('SELECT * FROM devices WHERE name = ?', (self.resolve_read_name(name),))
        return dict(row) if row else None

    def get_all_systems(self) -> List[str]:
        return [row[0] for row in self._conn.query('SELECT name FROM devices ORDER BY name')]

    # ------------------------------------------------------------------ #
    # Read — snapshots                                                     #
    # ------------------------------------------------------------------ #

    def get_snapshot(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one(
            'SELECT * FROM device_snapshot WHERE name = ?', (self.resolve_read_name(name),)
        )
        return self._unpack_snapshot(row) if row else None

    def get_all_snapshots(self) -> Dict[str, Dict]:
        rows = self._conn.query('SELECT * FROM device_snapshot ORDER BY name')
        return {row['name']: self._unpack_snapshot(row) for row in rows}

    def _unpack_snapshot(self, row) -> Dict:
        d = dict(row)
        try:
            d['folders'] = json.loads(d.pop('folders_json') or '{}')
        except Exception:
            d['folders'] = {}
        return d

    # ------------------------------------------------------------------ #
    # Read — totals                                                        #
    # ------------------------------------------------------------------ #

    def get_totals(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one(
            'SELECT * FROM device_totals WHERE name = ?', (self.resolve_read_name(name),)
        )
        return dict(row) if row else None

    def get_all_totals(self) -> Dict[str, Dict]:
        rows = self._conn.query('SELECT * FROM device_totals ORDER BY name')
        return {row['name']: dict(row) for row in rows}

    def get_global_totals(self) -> Dict:
        """Sum network and disk totals across all devices."""
        row = self._conn.query_one('''
            SELECT
                SUM(total_bytes_in)   AS total_bytes_in,
                SUM(total_bytes_out)  AS total_bytes_out,
                SUM(total_disk_bytes) AS total_disk_bytes
            FROM device_totals
        ''')
        if row:
            return {
                'total_bytes_in':   row['total_bytes_in']   or 0,
                'total_bytes_out':  row['total_bytes_out']  or 0,
                'total_disk_bytes': row['total_disk_bytes'] or 0,
            }
        return {'total_bytes_in': 0, 'total_bytes_out': 0, 'total_disk_bytes': 0}

    # ------------------------------------------------------------------ #
    # Read — dashboard compatibility shims                                 #
    # ------------------------------------------------------------------ #

    def get_latest_for_all(self) -> Dict[str, Dict]:
        """
        Latest system_metrics snapshot for every system.
        Shape compatible with /api/metrics/all dashboard calls.
        Includes a 'units' dict extracted from metrics_json.
        """
        result = {}
        rows = self._conn.query('''
            SELECT d.name, d.system_id, d.device_type,
                   s.metrics_timestamp AS timestamp,
                   s.cpu_percent, s.ram_percent,
                   s.uptime_seconds, s.uptime_formatted,
                   s.network_bandwidth_in_mbps,
                   s.network_bandwidth_out_mbps,
                   s.metrics_json,
                   t.last_bytes_in  AS network_bytes_in,
                   t.last_bytes_out AS network_bytes_out
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            LEFT JOIN device_totals   t ON t.name = d.name
            WHERE s.metrics_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in rows:
            d = dict(row)
            try:
                raw = json.loads(d.pop('metrics_json') or '{}')
            except Exception:
                raw = {}
            # Current counters come from the latest message itself, so a
            # message that couldn't measure them shows null rather than the
            # previous reading.
            for key in ('network_bytes_in', 'network_bytes_out'):
                if key in raw:
                    d[key] = _number_or_none(raw[key])
            d['units']           = self._extract_units(raw)
            d['gpus']            = raw.get('gpus', [])
            d['total_ram_bytes'] = _number_or_none(raw.get('total_ram_bytes'))
            # Counter-based fields (spec §11), only when the latest message
            # has them: a snapshot an older collector sent reads as before
            for key in SYSTEM_METRICS_EXTRA:
                if key in raw:
                    d[key] = _number_or_none(raw[key])
            if 'disk_busy_max_name' in raw:
                d['disk_busy_max_name'] = raw['disk_busy_max_name'] \
                    if isinstance(raw['disk_busy_max_name'], str) else None
            result[d['name']] = d
        return result

    def get_latest(self, name: str) -> Optional[Dict]:
        """Latest system_metrics snapshot for one system."""
        return self.get_latest_for_all().get(self.resolve_read_name(name))

    def get_latest_disk_usage_for_all(self) -> Dict[str, Dict]:
        """
        Latest folder_usage snapshot for every system.
        Shape compatible with /api/usage/all dashboard calls.
        """
        result = {}
        rows = self._conn.query('''
            SELECT d.name, d.system_id, d.device_type,
                   s.disk_timestamp   AS timestamp,
                   s.total_disk_bytes,
                   s.folders_json
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            WHERE s.disk_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in rows:
            d = dict(row)
            try:
                folders_raw = json.loads(d.pop('folders_json') or '{}')
            except Exception:
                folders_raw = {}
            d.update(folders_raw)   # inline flat folder keys for parse_folder_usage()
            result[d['name']] = d
        return result

    def get_latest_disk_usage(self, name: str) -> Optional[Dict]:
        return self.get_latest_disk_usage_for_all().get(self.resolve_read_name(name))

    # ------------------------------------------------------------------ #
    # Read — snapshots of the newer data types                             #
    # ------------------------------------------------------------------ #

    def _snapshot_rows(self, table: str, name: Optional[str]) -> list:
        """Rows of a snapshot table for registered devices (one device, or all); [] before the migration."""
        if not self._has_table(table):
            return []
        where = 'WHERE s.name = ?' if name is not None else ''
        return self._conn.query(
            f'SELECT s.* FROM {table} s JOIN devices d ON d.name = s.name {where} ORDER BY s.name',
            (self.resolve_read_name(name),) if name is not None else ())

    @staticmethod
    def _session_entry(row) -> dict:
        return {
            'timestamp': row['timestamp'],
            'list_timestamp': row['list_timestamp'],
            'list_confirmed_at': row['list_confirmed_at'],
            'summary': _json_value(row['summary_json'], {}),
            'sessions': _json_value(row['sessions_json'], []),
        }

    def get_all_sessions(self) -> Dict[str, Dict]:
        """{name: {timestamp, list_timestamp, list_confirmed_at, summary, sessions}} (GET /api/sessions/all)."""
        return {row['name']: self._session_entry(row) for row in self._snapshot_rows('device_session_snapshot', None)}

    def get_sessions(self, name: str) -> Optional[Dict]:
        rows = self._snapshot_rows('device_session_snapshot', name)
        return self._session_entry(rows[0]) if rows else None

    @staticmethod
    def _health_entry(row) -> dict:
        summary = _json_value(row['summary_json'], {})
        out = {'timestamp': row['timestamp']}
        out.update({k: summary.get(k) for k in _HEALTH_FIELDS})
        return out

    def get_all_storage_health(self) -> Dict[str, Dict]:
        """{name: {timestamp, overall_status, status_reasons, sources, arrays, disks, volumes, events}}."""
        return {row['name']: self._health_entry(row) for row in self._snapshot_rows('device_storage_health', None)}

    def get_storage_health(self, name: str) -> Optional[Dict]:
        rows = self._snapshot_rows('device_storage_health', name)
        return self._health_entry(rows[0]) if rows else None

    def get_all_breakdown(self) -> Dict[str, Dict]:
        """
        {name: {timestamp, owner_source, reference_time, cold_thresholds_days,
        device, folders, folders_truncated, volume_owners}} (the summary of
        each device's latest storage_breakdown, at most 50 KB each).
        """
        out = {}
        for row in self._snapshot_rows('device_storage_breakdown', None):
            entry = {'timestamp': row['timestamp']}
            entry.update(_json_value(row['summary_json'], {}))
            out[row['name']] = entry
        return out

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def delete_system(self, name: str) -> bool:
        """
        Remove ONE device's records from the central summary DB: devices,
        device_snapshot, device_totals, the sessions / storage health /
        storage breakdown snapshots and the per-user tables of the row
        reads for `name` return (that exact spelling if registered, else the
        spelling it resolves to), plus any per-user rows stored under exactly
        `name`.  Other registered spellings that normalize alike (two
        machines 'lab-a' / 'lab_a', or a stale row from before a rename)
        keep their rows; delete each by its own name.
        Returns True if any rows were deleted.
        """
        try:
            with self._conn.transaction() as db:
                names = {name, self.resolve_read_name(name)}
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                deleted = 0
                for n in names:
                    for table, col in (('devices', 'name'), ('device_snapshot', 'name'),
                                       ('device_totals', 'name'), ('device_user_snapshot', 'name'),
                                       ('user_metrics_history', 'device_name'),
                                       ('device_session_snapshot', 'name'), ('device_storage_health', 'name'),
                                       ('device_storage_breakdown', 'name')):
                        if table in tables:
                            deleted += db.execute(f'DELETE FROM {table} WHERE {col} = ?', (n,)).rowcount
                self._registered.difference_update(names)   # a deleted name is no longer grandfathered
            logger.info(f"Deleted system '{name}' ({', '.join(sorted(names))}) from central metrics DB "
                        f"({deleted} rows)")
            self._load_name_index()     # other spellings of the name keep being resolved
            return deleted > 0
        except Exception as e:
            logger.error(f"Failed to delete system '{name}' from metrics DB: {e}")
            return False

    def close(self):
        self._conn.close()


class UserMetricsStore:
    """
    Manages per-user CPU/RAM snapshot and normalized history.

    Snapshot table  (device_user_snapshot): latest row per (device, username).
    History table   (user_metrics_history):  one row per (device, timestamp, username),
                                             msg_id UNIQUE for dedup.
    Both carry the usage fields (USER_EXTRA_COLUMNS); the read methods leave
    a NULL one out of a row (not measured, or stored before they existed).
    A snapshot row's usage fields are served only when its usage_timestamp
    is its timestamp: after a downgrade the older Manager updated the row
    without them, so they belong to an earlier reading.
    """

    def __init__(self, metrics_db: MetricsDB, init: bool = True):
        self._metrics_db = metrics_db
        self._conn = metrics_db._conn
        self.db = metrics_db.db   # raw connection, kept for compatibility
        if init:
            self._init_tables()

    def _init_tables(self):
        extra = ''.join(f',\n                    {c} {t}' for c, t in USER_EXTRA_COLUMNS)
        snapshot_extra = ''.join(f',\n                    {c} {t}' for c, t in USER_SNAPSHOT_COLUMNS)
        with self._conn.transaction() as db:
            db.execute(f'''
                CREATE TABLE IF NOT EXISTS device_user_snapshot (
                    name          TEXT NOT NULL,
                    username      TEXT NOT NULL,
                    cpu_percent   REAL,
                    ram_bytes     INTEGER,
                    ram_formatted TEXT,
                    timestamp     TEXT{snapshot_extra},
                    PRIMARY KEY (name, username)
                )
            ''')
            db.execute(f'''
                CREATE TABLE IF NOT EXISTS user_metrics_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_name TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    username    TEXT NOT NULL,
                    cpu_percent REAL,
                    ram_bytes   INTEGER,
                    ram_formatted TEXT,
                    msg_id      TEXT{extra}
                )
            ''')
            cols = {row[1] for row in db.execute('PRAGMA table_info(user_metrics_history)').fetchall()}
            if 'msg_id' not in cols:
                # Migration: existing rows get NULL (UNIQUE allows many NULLs)
                db.execute('ALTER TABLE user_metrics_history ADD COLUMN msg_id TEXT')
                logger.info("Migrated user_metrics_history: added msg_id column")
            for table, columns in (('device_user_snapshot', USER_SNAPSHOT_COLUMNS),
                                   ('user_metrics_history', USER_EXTRA_COLUMNS)):
                have = {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}
                added = [c for c, _ in columns if c not in have]
                for c, t in columns:
                    if c in added:
                        # Migration: existing rows read NULL ("not measured")
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {c} {t}')
                if added:
                    logger.info(f"Migrated {table}: added {', '.join(added)} columns")
            db.execute('''
                CREATE INDEX IF NOT EXISTS idx_umh_device_ts
                    ON user_metrics_history (device_name, timestamp)
            ''')
            db.execute('''
                CREATE INDEX IF NOT EXISTS idx_umh_device_user_ts
                    ON user_metrics_history (device_name, username, timestamp)
            ''')
            db.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_umh_msg_id
                    ON user_metrics_history (msg_id)
            ''')

    @staticmethod
    def _ingest(db, device_name, timestamp, username, cpu_percent, ram_bytes,
                ram_formatted, msg_id=None, raw_timestamp=None, update_snapshot=True,
                extra: Optional[Dict[str, Any]] = None):
        """
        History row, unless it is there already: by msg_id (UNIQUE index),
        or because an older Manager stored this reading (a msg_id NULL row
        with the same device, user and timestamp, raw or canonical - whatever
        the message's id: none, "legacy-...", or a collector's random id the
        older Manager dropped); the snapshot row only if this message is not
        older (and update_snapshot: False for a message from the future).
        extra: the usage fields (USER_EXTRA_FIELDS -> number or None); the
        snapshot takes them as sent, NULL included (not measured now).
        """
        extra = extra or {}
        values = [extra.get(c) for c in USER_EXTRA_FIELDS]
        row = db.execute(
            'SELECT timestamp FROM device_user_snapshot WHERE name = ? AND username = ?',
            (device_name, username)).fetchone()
        if update_snapshot and (row is None or _not_older(timestamp, row['timestamp'])):
            UserMetricsStore._upsert_snapshot(
                db, device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted, extra)
        stamps = list(dict.fromkeys(t for t in (raw_timestamp, timestamp) if isinstance(t, str)))
        ph = ', '.join(['?'] * len(stamps))
        if stamps and db.execute(
                f'SELECT 1 FROM user_metrics_history INDEXED BY idx_umh_device_user_ts '
                f'WHERE device_name = ? AND username = ? AND timestamp IN ({ph}) '
                f'AND msg_id IS NULL LIMIT 1',
                [device_name, username] + stamps).fetchone():
            return
        db.execute(f'''
            INSERT OR IGNORE INTO user_metrics_history
                (device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted, msg_id,
                 {', '.join(USER_EXTRA_FIELDS)})
            VALUES ({', '.join(['?'] * (7 + len(USER_EXTRA_FIELDS)))})
        ''', [device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted, msg_id] + values)

    @staticmethod
    def _upsert_snapshot(db, device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted,
                         extra: Optional[Dict[str, Any]] = None):
        extra = extra or {}
        updates = ''.join(f',\n                {c} = excluded.{c}' for c in USER_EXTRA_FIELDS)
        db.execute(f'''
            INSERT INTO device_user_snapshot
                (name, username, cpu_percent, ram_bytes, ram_formatted, timestamp, {', '.join(USER_EXTRA_FIELDS)},
                 usage_timestamp)
            VALUES ({', '.join(['?'] * (7 + len(USER_EXTRA_FIELDS)))})
            ON CONFLICT(name, username) DO UPDATE SET
                cpu_percent   = excluded.cpu_percent,
                ram_bytes     = excluded.ram_bytes,
                ram_formatted = excluded.ram_formatted,
                timestamp     = excluded.timestamp{updates},
                usage_timestamp = excluded.usage_timestamp
        ''', [device_name, username, cpu_percent, ram_bytes, ram_formatted, timestamp]
                   + [extra.get(c) for c in USER_EXTRA_FIELDS] + [timestamp])

    def ingest(self, device_name: str, timestamp: str, username: str,
               cpu_percent: Optional[float], ram_bytes: Optional[int],
               ram_formatted: Optional[str], msg_id: Optional[str] = None,
               extra: Optional[Dict[str, Any]] = None):
        """Store one user-metric observation: updates snapshot + appends history."""
        with self._metrics_db._write() as db:
            self._ingest(db, device_name, timestamp, username, cpu_percent,
                         ram_bytes, ram_formatted, msg_id, extra=extra)

    def _extra_select(self, table: str) -> str:
        """', <usage columns>' that `table` has (none while a held-back migration has not added them)."""
        have = {row[1] for row in self._conn.query(f'PRAGMA table_info({table})')}
        fields = USER_EXTRA_FIELDS + (('usage_timestamp',) if table == 'device_user_snapshot' else ())
        return ''.join(f', {c}' for c in fields if c in have)

    @staticmethod
    def _user_row(row) -> dict:
        """
        A per-user row; a usage field that is NULL (unmeasured) is left out,
        and so is every usage field of a snapshot row whose usage_timestamp
        is not its timestamp (an older Manager updated it without them).
        """
        d = dict(row)
        current = 'usage_timestamp' not in d or d.pop('usage_timestamp') == d.get('timestamp')
        return {k: v for k, v in d.items() if k not in USER_EXTRA_FIELDS or (v is not None and current)}

    def get_snapshot(self, device_name: str) -> list:
        """Current users for one device, sorted by cpu_percent desc."""
        rows = self._conn.query(f'''
            SELECT username, cpu_percent, ram_bytes, ram_formatted, timestamp{self._extra_select('device_user_snapshot')}
            FROM device_user_snapshot
            WHERE name = ?
            ORDER BY cpu_percent DESC
        ''', (device_name,))
        return [self._user_row(r) for r in rows]

    def get_all_snapshots(self) -> dict:
        """Current users for all devices. Returns {device_name: [user_rows]}."""
        rows = self._conn.query(f'''
            SELECT name, username, cpu_percent, ram_bytes, ram_formatted, timestamp{self._extra_select('device_user_snapshot')}
            FROM device_user_snapshot
            ORDER BY name, cpu_percent DESC
        ''')
        result = {}
        for row in rows:
            d = self._user_row(row)
            result.setdefault(d.pop('name'), []).append(d)
        return result

    def has_device(self, device_name: str) -> bool:
        """
        True if per-user rows exist under exactly this device name.  The old
        DELETE /api/system/<name> left them behind, so they can outlive the
        device registry row; they stay readable.
        """
        return self._conn.query_one('''
            SELECT 1 WHERE EXISTS (SELECT 1 FROM device_user_snapshot WHERE name = ?)
                        OR EXISTS (SELECT 1 FROM user_metrics_history WHERE device_name = ?)
        ''', (device_name, device_name)) is not None

    def get_history(self, device_name: str, username: str, limit: int = 200) -> list:
        """Time-series rows for one user on one device."""
        rows = self._conn.query(f'''
            SELECT timestamp, cpu_percent, ram_bytes, ram_formatted{self._extra_select('user_metrics_history')}
            FROM user_metrics_history
            WHERE device_name = ? AND username = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (device_name, username, limit))
        return [self._user_row(r) for r in rows]

    # ---- retention support (archive_metrics.py) ---- #

    def history_older_than(self, cutoff: str, limit: int = 5000) -> list:
        """Oldest-first user_metrics_history rows with timestamp < cutoff."""
        rows = self._conn.query('''
            SELECT * FROM user_metrics_history
            WHERE timestamp < ?
            ORDER BY timestamp, id
            LIMIT ?
        ''', (cutoff, limit))
        return [dict(r) for r in rows]

    def delete_history_ids(self, ids: list) -> int:
        deleted = 0
        with self._metrics_db._write() as db:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ', '.join(['?'] * len(chunk))
                deleted += db.execute(
                    f'DELETE FROM user_metrics_history WHERE id IN ({ph})', chunk).rowcount
        return deleted
