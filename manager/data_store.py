"""
TypedDataStore — per-system SQLite history, one table (or table pair) per data_type.

Storage layout:
    data_dir/
    └── <normalized_system_name>/          (storage_util.normalize_device_name)
        └── data.db
            ├── folder_usage_msgs     one row per disk collection run
            ├── folder_usage_items    one row per (run, folder/volume key) -> value
            ├── folder_usage_legacy   pre-upgrade wide table, kept after migration
            ├── system_metrics        one row per metrics collection run
            ├── user_metrics          one row per user per metrics run
            ├── not_specified         fallback for messages with no data_type
            └── <any_future_type>

folder_usage is stored NARROW.  A folder_usage payload has one key per
scanned folder, and the old layout added one column per folder path, which
runs into SQLite's 2000-column limit on big shares.  The read API reassembles
each run into the same flat dict as before:
    {"id", "msg_id", "timestamp", "device_type", "_unit", "/volume1/Data": 123, ...}
An existing wide "folder_usage" table is migrated on first open: rows are
copied into the narrow tables in one transaction and the old table is
renamed to folder_usage_legacy (never dropped).

Every other data_type uses a WIDE table: one column per payload key.
Schema evolution: when a message carries a key the table hasn't seen, the
column is added with ALTER TABLE.  Earlier rows read back as NULL for that
column, meaning "not measured" (distinct from a measured zero).
  - SQLite column names are case-insensitive: a key that differs only in
    case from an existing column is stored in that column; two keys in one
    payload that differ only in case are rejected.
  - Payload keys that collide with internal columns (id, timestamp,
    device_type, msg_id) are stored as "payload_<key>"; they can never set
    the primary key.

data_type labels: letter first, then letters/digits/_/-; "sqlite_*" and
"folder_usage_*" are refused.  The old Manager took any label (it only
sanitized it into a table name), so a table it made from a label refused
now ("1wire_sensors") stays readable, and keeps receiving that label's
messages, in the database that has it; no new table is ever made from such
a label.  The storage internals (folder_usage_msgs / _items / _legacy,
sqlite_*) are never listed or read as a data_type.

Dedup: every history table has a msg_id column with a UNIQUE index.  Rows
stored before dedup existed have msg_id NULL (UNIQUE allows many NULLs).
store_message() returns 'duplicate' without side effects if the msg_id is
already present.  A message whose msg_id is not stored yet is ALSO a
duplicate when the table already holds a pre-upgrade row (msg_id NULL) with
the same natural key: its timestamp (raw header string or canonical form),
plus the username for user_metrics.  That holds for every form of id: none
(an old collector, an archive replay of old messages), "legacy-..." (an old
queue entry re-sent by an upgraded collector) and a collector's random id
(a collector upgraded before the Manager: the old Manager stored its
messages but dropped the id, and the same message can come again from the
queue after a lost ACK or from an archive replay).  This Manager always
writes a msg_id, so a msg_id NULL row is always from before the upgrade;
collector timestamps carry microseconds, so a different reading never has
the same natural key.  The lookups use the timestamp indexes.

Upgrading a database written by an older Manager (a wide folder_usage
table, history tables without msg_id) happens once, when it is first
opened: by default a consistent copy is taken first
(storage_util.backup_database, "data.db.pre-upgrade-<stamp>.bak").  If that
copy fails, the database is not migrated: reads keep working on the old
layout and writes fail as transient 'error' (the collector keeps the
message) until a retry of the backup succeeds.

Reads never create directories, databases or tables.
"""

import json
import math
import os
import re
import sqlite3
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from storage_util import (
    UPGRADE_RETRY_SECONDS, LockedConnection, MigrationBlockedError, backup_database,
    canonical_timestamp, format_timestamp, normalize_device_name,
)

logger = logging.getLogger(__name__)

# Columns managed by the store itself in wide tables
_INTERNAL_COLS = ('id', 'timestamp', 'device_type', 'msg_id')
_INTERNAL_LOWER = frozenset(_INTERNAL_COLS)

# Table used when a message carries no data_type label
DEFAULT_TABLE = 'not_specified'

FOLDER_USAGE = 'folder_usage'
_FU_MSGS = 'folder_usage_msgs'
_FU_ITEMS = 'folder_usage_items'
_FU_LEGACY = 'folder_usage_legacy'

# data_type labels: letter first, then letters/digits/_/-, max 64 chars.
_DATA_TYPE_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
# Table-name prefixes reserved for SQLite and for the folder_usage layout
_RESERVED_PREFIXES = ('sqlite_', 'folder_usage_')
# The tables that really are internal (never listed or read as a data_type)
_INTERNAL_TABLE_RE = re.compile(r'sqlite_.*|folder_usage_(?:msgs|items|legacy(?:_\d+)?)')

# SQLite's default SQLITE_MAX_COLUMN is 2000; leave headroom.
_MAX_COLUMNS = 1900
# Longest field name in a WIDE table (it becomes a column name)
_MAX_KEY_LEN = 512
# Longest folder_usage key (a folder path, stored as a row value).  Linux
# PATH_MAX is 4096; longer or otherwise unusable keys are dropped from the
# message and listed in its "_errors", never reject the whole scan.
_MAX_FOLDER_KEY_LEN = 4096
_ERROR_PATH_PREVIEW = 256

_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1

# Row cap for windowed queries when the caller doesn't pass a limit
DEFAULT_ROW_LIMIT = 20000

_JD_UNIX_EPOCH = 2440587.5   # julianday('1970-01-01T00:00:00Z')


class RejectedMessage(ValueError):
    """A message that can never be stored as sent (bad shape or value)."""


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _sanitize_table(name: str) -> str:
    """
    Convert a data_type string into a valid unquoted SQLite table name.
    Lowercases, replaces anything that isn't a-z0-9_ with underscore,
    and truncates to 64 characters.
    """
    return re.sub(r'[^a-z0-9_]', '_', name.lower())[:64]


def _is_internal_table(name: str) -> bool:
    return _INTERNAL_TABLE_RE.fullmatch(name) is not None


def _readable_table(data_type, tables) -> Optional[str]:
    """
    Table a read for data_type uses, or None.  A label the current rules
    accept maps as on ingest; otherwise an EXISTING table an older Manager
    created from a label the current rules refuse (it accepted any text:
    "1wire_sensors", "folder_usage_v2") stays readable under the same
    sanitized name the old Manager used.  Internal tables never are.
    """
    try:
        return table_for_data_type(data_type)
    except RejectedMessage:
        pass
    if not isinstance(data_type, str) or not data_type:
        return None
    table = _sanitize_table(data_type)
    if table in tables and not _is_internal_table(table):
        return table
    return None


def is_valid_data_type(data_type) -> bool:
    try:
        table_for_data_type(data_type)
        return True
    except RejectedMessage:
        return False


def table_for_data_type(data_type) -> str:
    """Validated table name for a data_type label; raises RejectedMessage."""
    if data_type is None or data_type == '':
        return DEFAULT_TABLE
    if not isinstance(data_type, str) or not _DATA_TYPE_RE.fullmatch(data_type):
        raise RejectedMessage('invalid data_type')
    table = _sanitize_table(data_type)
    if table.startswith(_RESERVED_PREFIXES):
        raise RejectedMessage('reserved data_type')
    return table


def _quote_col(name: str) -> str:
    """
    Escape a column name for use inside a double-quoted SQLite identifier.
    SQLite only needs double-quotes within the identifier to be doubled.
    e.g.  /volume1/JeffMoffitt  →  /volume1/JeffMoffitt   (no change needed)
          col"name              →  col""name
    """
    return name.replace('"', '""')


def _col_type(value: Any) -> str:
    """
    SQLite column type for a (cleaned) value: TEXT for strings (including
    JSON-encoded lists/dicts), REAL for numbers, booleans and None.
    """
    return 'TEXT' if isinstance(value, str) else 'REAL'


def _scrub_json(value: Any) -> Any:
    """Replace NaN/±inf (not valid JSON) with None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_json(v) for k, v in value.items()}
    return value


def _clean_value(value: Any) -> Any:
    """
    Convert a JSON value to a SQLite-bindable one.
    NaN/±inf → None (unmeasured); lists/dicts → JSON text; bool → 0/1.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise RejectedMessage('integer value out of range')
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(_scrub_json(value), allow_nan=False)
    raise RejectedMessage(f'unsupported value type {type(value).__name__}')


def _check_key(key) -> None:
    if not isinstance(key, str) or key == '' or '\x00' in key or len(key) > _MAX_KEY_LEN:
        raise RejectedMessage('invalid field name')


def _prepare_wide_payload(payload: Dict[str, Any], existing_cols: List[str]) -> Dict[str, Any]:
    """
    Map payload keys onto column names for a wide table:
      - keys colliding with internal columns become payload_<key>
      - two keys that differ only in case are rejected
      - a key matching an existing column case-insensitively uses that
        column's spelling (SQLite treats them as the same column anyway)
    Values are cleaned for binding.
    """
    seen_lower: Dict[str, str] = {}
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        _check_key(key)
        col = 'payload_' + key if key.lower() in _INTERNAL_LOWER else key
        low = col.lower()
        if low in seen_lower:
            raise RejectedMessage(
                f"field names collide (SQLite column names are case-insensitive): "
                f"{seen_lower[low]!r} / {key!r}"
            )
        seen_lower[low] = key
        out[col] = _clean_value(value)

    by_lower = {c.lower(): c for c in existing_cols}
    return {by_lower.get(c.lower(), c): v for c, v in out.items()}


def _split_folder_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Tuple[str, Any]]]:
    """
    Split a flat folder_usage payload into (meta, items).
      meta  — "_"-prefixed metadata keys (_unit, _timed_out, _errors, ...)
              plus any non-numeric value; stored as JSON
      items — folder / volume / capacity / total keys with numeric or null
              values; one row each
    A key that can't be stored (empty, NUL, longer than _MAX_FOLDER_KEY_LEN)
    is dropped and appended (shortened) to meta["_errors"], so one odd path
    doesn't cost the whole day's scan.
    """
    meta: Dict[str, Any] = {}
    items: List[Tuple[str, Any]] = []
    seen = set()
    dropped: List[str] = []
    for key, value in payload.items():
        if not isinstance(key, str) or key == '' or '\x00' in key or len(key) > _MAX_FOLDER_KEY_LEN:
            text = str(key).replace('\x00', '')
            if len(text) > _ERROR_PATH_PREVIEW:
                text = text[:_ERROR_PATH_PREVIEW] + '...'
            dropped.append(text)
            continue
        if key in _INTERNAL_COLS:
            key = 'payload_' + key
        # e.g. payload has both "id" (renamed to payload_id) and "payload_id"
        if key in seen:
            raise RejectedMessage(f'field name collision: {key!r}')
        seen.add(key)

        if key.startswith('_') or isinstance(value, (str, list, dict, bool)):
            meta[key] = _scrub_json(value)
        else:
            items.append((key, _clean_value(value)))

    if dropped:
        logger.warning(
            f"[data_store] folder_usage: dropped {len(dropped)} unusable folder key(s) "
            f"(empty, NUL or longer than {_MAX_FOLDER_KEY_LEN} chars); listed in _errors"
        )
        errors = meta.get('_errors')
        if errors is None:
            errors = []
        elif not isinstance(errors, list):
            errors = [errors]
        meta['_errors'] = errors + dropped
    return meta, items


def _numeric_filter(expr: str) -> str:
    """SQL condition: expr holds a finite number (or numeric-looking text)."""
    return (
        f"{expr} IS NOT NULL AND ("
        f"typeof({expr}) IN ('integer', 'real') OR "
        f"(typeof({expr}) = 'text' AND trim({expr}) <> '' "
        f"AND trim({expr}) NOT GLOB '*[^0-9.eE+-]*')"
        f") AND abs(CAST({expr} AS REAL)) < 1e308"
    )


def _meta_number(meta_json, key):
    """
    SQL function lm_meta_number(meta_json, key): the numeric value of one
    folder_usage metadata key (e.g. "_scan_seconds"), else NULL.  Follows the
    old /api/history value rules: numbers and numeric text count, NaN/±inf
    and anything else don't.
    """
    try:
        meta = json.loads(meta_json) if meta_json else None
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    value = meta.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else None
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _register_functions(conn: sqlite3.Connection):
    try:
        conn.create_function('lm_meta_number', 2, _meta_number, deterministic=True)
    except (TypeError, sqlite3.NotSupportedError):     # Python < 3.8 / SQLite < 3.8.3
        conn.create_function('lm_meta_number', 2, _meta_number)


def _sql_literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _jd_to_timestamp(jd: float) -> str:
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=jd - _JD_UNIX_EPOCH)
    return format_timestamp(dt)


class _Duplicate(Exception):
    pass


# --------------------------------------------------------------------------- #
# Store                                                                       #
# --------------------------------------------------------------------------- #

class TypedDataStore:
    """
    Manages one SQLite database per system.
    Each data_type gets its own table; unknown types go to 'not_specified'.
    Tables are created on first write; columns are added as new keys arrive.
    """

    def __init__(self, data_dir: str, backup_before_migration: bool = True):
        self.data_dir = data_dir
        self.backup_before_migration = backup_before_migration
        self._dbs: Dict[str, LockedConnection] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Connection management                                                #
    # ------------------------------------------------------------------ #

    def _db_path(self, system_name: str) -> str:
        return os.path.join(self.data_dir, normalize_device_name(system_name), 'data.db')

    def has_system(self, system_name: str) -> bool:
        """True if a history database exists for this system (no side effects)."""
        key = normalize_device_name(system_name)
        with self._cache_lock:
            if key in self._dbs:
                return True
        return os.path.isfile(self._db_path(system_name))

    def _get_db(self, system_name: str, create: bool = False) -> Optional[LockedConnection]:
        """
        Cached connection for a system, keyed by the normalized name.
        With create=False (all read paths) returns None instead of creating
        the directory or database for an unknown system.
        """
        key = normalize_device_name(system_name)
        with self._cache_lock:
            db = self._dbs.get(key)
            if db is not None:
                return db
            path = self._db_path(system_name)
            if not os.path.isfile(path):
                if not create:
                    return None
                os.makedirs(os.path.dirname(path), exist_ok=True)
            db = LockedConnection(path)
            _register_functions(db.conn)
            db.folder_migrated = False
            db.folder_error = None        # why the wide -> narrow folder_usage copy failed
            db.folder_attempted = 0.0
            db.upgrade_blocked = None     # reason while a needed migration is held back
            db.upgrade_attempted = 0.0
            try:
                self._upgrade_db(db)
            except BaseException:
                db.close()                # e.g. a damaged file: retried on the next access
                raise
            self._dbs[key] = db
            return db

    def prepare_existing(self) -> int:
        """
        Open (and, where needed, back up and migrate) every existing per-system
        database, so the one-time upgrade happens at startup rather than
        inside the first request for each system.  Returns how many are
        held back because their backup failed.
        """
        blocked = 0
        for key in self.list_systems():
            try:
                db = self._get_db(key)
            except Exception as e:
                logger.error(f"[data_store] could not open {self._db_path(key)}: {e}")
                continue
            if db is not None and db.upgrade_blocked:
                blocked += 1
        return blocked

    # ------------------------------------------------------------------ #
    # One-time upgrade of databases written by an older Manager            #
    # ------------------------------------------------------------------ #

    @classmethod
    def _upgrade_reasons(cls, conn) -> List[str]:
        """What an existing database still needs migrated ([] = current schema)."""
        reasons = []
        tables = cls._list_tables(conn)
        if FOLDER_USAGE in tables:
            reasons.append('wide folder_usage table')
        old = sorted(
            t for t in tables
            if not _is_internal_table(t) and t != FOLDER_USAGE
            and 'msg_id' not in {c.lower() for c in cls._get_columns(conn, t)}
        )
        if old:
            reasons.append('no msg_id column in ' + ', '.join(old))
        return reasons

    @classmethod
    def _has_original_layout(cls, conn) -> bool:
        """
        True while a database still has the layout an older Manager wrote:
        no folder_usage_msgs / _items / _legacy table and no msg_id column.
        False once any migration step of this Manager has been applied.
        """
        for table in cls._list_tables(conn):
            if table.startswith('sqlite_'):
                continue
            if _is_internal_table(table):
                return False
            if 'msg_id' in {c.lower() for c in cls._get_columns(conn, table)}:
                return False
        return True

    def _upgrade_db(self, db: LockedConnection) -> bool:
        """
        Bring a database written by an older Manager to the current schema,
        after taking a pre-upgrade backup (unless backup_before_migration is
        off).  If the backup fails nothing is changed: db.upgrade_blocked is
        set, reads keep working on the old layout and writes raise
        MigrationBlockedError.  Returns True if the schema is current.
        """
        with db.lock:
            db.upgrade_attempted = time.monotonic()
            reasons = self._upgrade_reasons(db.conn)
            if not reasons:
                db.folder_migrated = True
                db.upgrade_blocked = None
                return True
            reason = '; '.join(reasons)
            if self.backup_before_migration:
                try:
                    # An older Manager only appended rows / added columns here
                    backup_database(db.conn, db.path, reason,
                                    original_layout=self._has_original_layout, append_only=True)
                except Exception as e:
                    db.upgrade_blocked = reason
                    logger.error(
                        f"[data_store] NOT migrating {db.path}: the pre-upgrade backup failed "
                        f"({type(e).__name__}: {e}). It is still readable in its old layout; new "
                        f"messages for it get 'error' and stay queued on the collector. Free disk "
                        f"space / fix permissions (retried every {UPGRADE_RETRY_SECONDS}s on the "
                        f"next write), or set \"backup_before_migration\": false to migrate without "
                        f"a backup."
                    )
                    return False
            else:
                logger.warning(
                    f"[data_store] migrating {db.path} WITHOUT a backup "
                    f"(backup_before_migration is false): {reason}"
                )
            db.upgrade_blocked = None

            self._try_folder_migration(db)
            try:
                with db.transaction() as conn:
                    for table in sorted(self._list_tables(conn)):
                        if not _is_internal_table(table) and table != FOLDER_USAGE:
                            self._ensure_table(conn, table)
            except Exception as e:
                # Retried table by table on the next write (store_message)
                logger.error(f"[data_store] adding msg_id columns failed for {db.path}: {e}",
                             exc_info=True)
            return True

    def _try_folder_migration(self, db: LockedConnection) -> bool:
        """
        Run the wide -> narrow folder_usage copy; True when done (or not needed).
        On failure (database locked by another program, disk full, ...) the
        wide table stays in place and keeps serving the old folder history;
        db.folder_error is set and new folder_usage messages for this system
        get 'error' (the collectors keep them) until a retry succeeds, so the
        narrow tables are never started next to an unmigrated wide table.
        """
        with db.lock:
            db.folder_attempted = time.monotonic()
            try:
                self._migrate_wide_folder_usage(db)
            except Exception as e:
                db.folder_error = f'{type(e).__name__}: {e}'
                logger.error(
                    f"[data_store] folder_usage migration failed for {db.path}: {e}. The old "
                    f"folder history stays in the wide table and is still served; new folder_usage "
                    f"messages for this system get 'error' (kept queued by the collector) until the "
                    f"migration succeeds. Retried on the next folder_usage message after "
                    f"{UPGRADE_RETRY_SECONDS}s and at every start.",
                    exc_info=True,
                )
                return False
            if db.folder_error:
                logger.warning(f"[data_store] folder_usage migration of {db.path} succeeded on retry")
            db.folder_error = None
            return True

    def _check_folder_writable(self, db: LockedConnection):
        """Raise MigrationBlockedError while the wide folder_usage table is not migrated yet."""
        if db.folder_migrated:
            return
        with db.lock:
            if not db.folder_migrated and \
                    time.monotonic() - db.folder_attempted >= UPGRADE_RETRY_SECONDS:
                self._try_folder_migration(db)
            if not db.folder_migrated:
                raise MigrationBlockedError(
                    f"{db.path}: the folder_usage migration is not done yet "
                    f"({db.folder_error or 'pending'}); retried every {UPGRADE_RETRY_SECONDS}s")

    def upgrade_pending(self, system_name: str) -> Optional[str]:
        """Why this system's database migration is held back, or None."""
        db = self._get_db(system_name)
        return db.upgrade_blocked if db is not None else None

    def _check_writable(self, db: LockedConnection):
        """Raise MigrationBlockedError while a held-back migration is pending."""
        if not db.upgrade_blocked:
            return
        with db.lock:
            if db.upgrade_blocked and \
                    time.monotonic() - db.upgrade_attempted >= UPGRADE_RETRY_SECONDS:
                self._upgrade_db(db)
            if db.upgrade_blocked:
                raise MigrationBlockedError(
                    f"{db.path} needs a migration that is held back until its "
                    f"pre-upgrade backup succeeds")

    # ------------------------------------------------------------------ #
    # Schema helpers                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_columns(conn, table: str) -> List[str]:
        """Return the column names currently in a table."""
        cursor = conn.execute(f'PRAGMA table_info("{table}")')
        return [row[1] for row in cursor.fetchall()]

    @staticmethod
    def _list_tables(conn) -> set:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in cursor.fetchall()}

    @staticmethod
    def _ensure_table(conn, table: str):
        """Create a wide table with the base schema; add msg_id to old tables."""
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS "{table}" (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME NOT NULL,
                device_type TEXT     DEFAULT 'unknown',
                msg_id      TEXT
            )
        ''')
        cols = {c.lower() for c in TypedDataStore._get_columns(conn, table)}
        if 'msg_id' not in cols:
            # Migration: tables created before dedup. Existing rows get NULL.
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN msg_id TEXT')
            logger.info(f"[data_store] migrated '{table}': added msg_id column")
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_ts" ON "{table}" (timestamp DESC)')
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{table}_msg_id" ON "{table}" (msg_id)')

    @staticmethod
    def _ensure_folder_tables(conn):
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {_FU_MSGS} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id      TEXT UNIQUE,
                timestamp   TEXT NOT NULL,
                device_type TEXT DEFAULT 'unknown',
                meta_json   TEXT NOT NULL DEFAULT '{{}}'
            )
        ''')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{_FU_MSGS}_ts ON {_FU_MSGS} (timestamp)')
        # value is NUMERIC so integer byte counts stay exact integers
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {_FU_ITEMS} (
                msg_row_id  INTEGER NOT NULL,
                key         TEXT    NOT NULL,
                value       NUMERIC,
                PRIMARY KEY (msg_row_id, key)
            ) WITHOUT ROWID
        ''')
        # History queries look up one key across runs; msg_row_id grows with
        # arrival time, so (key, msg_row_id) is the "timestamp-ish" index.
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{_FU_ITEMS}_key ON {_FU_ITEMS} (key, msg_row_id)')

    def _evolve_schema(self, conn, table: str, existing: List[str], mapped: Dict[str, Any]):
        """
        Add a column for every payload key the table hasn't seen before.
        Rows stored earlier read back NULL for it ("not measured").
        Logs every migration for auditability.
        """
        existing_lower = {c.lower() for c in existing}
        new_cols = [c for c in mapped if c.lower() not in existing_lower]
        if len(existing) + len(new_cols) > _MAX_COLUMNS:
            raise RejectedMessage(f'too many distinct fields for table {table!r}')

        for col in sorted(new_cols):
            col_type = _col_type(mapped[col])
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{_quote_col(col)}" {col_type}')
            logger.info(
                f"[data_store] schema evolved: '{table}'.\"{col}\" ({col_type}) added; "
                f"earlier rows read as NULL (not measured)"
            )

    # ------------------------------------------------------------------ #
    # Wide → narrow folder_usage migration                                 #
    # ------------------------------------------------------------------ #

    def _migrate_wide_folder_usage(self, db: LockedConnection):
        """
        Copy rows of a pre-upgrade wide "folder_usage" table into the narrow
        tables in one transaction, then rename the old table to
        folder_usage_legacy (kept, never dropped).
        """
        with db.lock:
            tables = self._list_tables(db.conn)
            if FOLDER_USAGE not in tables:
                db.folder_migrated = True
                return

            cols = self._get_columns(db.conn, FOLDER_USAGE)
            data_cols = [c for c in cols if c.lower() not in _INTERNAL_LOWER]
            has_msg_id = any(c.lower() == 'msg_id' for c in cols)
            legacy_name, n = _FU_LEGACY, 1
            while legacy_name in tables:
                legacy_name = f'{_FU_LEGACY}_{n}'
                n += 1

            copied = 0
            with db.transaction() as conn:
                # Another process (e.g. archive_metrics.py) may have migrated
                # it while we waited for the write lock.
                if FOLDER_USAGE not in self._list_tables(conn):
                    db.folder_migrated = True
                    return
                self._ensure_folder_tables(conn)
                # Keep original ids when the narrow table is still empty
                keep_ids = conn.execute(f'SELECT COUNT(*) FROM {_FU_MSGS}').fetchone()[0] == 0
                cursor = conn.execute(f'SELECT * FROM "{FOLDER_USAGE}" ORDER BY id')
                while True:
                    batch = cursor.fetchmany(500)
                    if not batch:
                        break
                    for row in batch:
                        self._copy_wide_folder_row(conn, dict(row), data_cols, has_msg_id, keep_ids)
                        copied += 1
                conn.execute(f'ALTER TABLE "{FOLDER_USAGE}" RENAME TO "{legacy_name}"')

            db.folder_migrated = True
            logger.warning(
                f"[data_store] migrated {copied} folder_usage rows in {db.path} "
                f"from the wide layout to folder_usage_msgs/items; "
                f"old table kept as '{legacy_name}'"
            )

    @staticmethod
    def _copy_wide_folder_row(conn, row: dict, data_cols: List[str], has_msg_id: bool, keep_ids: bool):
        raw_ts = row.get('timestamp')
        try:
            ts = canonical_timestamp(raw_ts)
        except ValueError:
            ts = str(raw_ts)
        meta, items = {}, []
        for col in data_cols:
            value = row.get(col)
            # NULL = never measured in this row (old NaN backfill reads as NULL);
            # '' was the old backfill for TEXT columns.
            if value is None or value == '':
                continue
            if isinstance(value, bytes):
                continue
            if col.startswith('_') or isinstance(value, str):
                meta[col] = value
            else:
                items.append((col, value))
        msg_id = row.get('msg_id') if has_msg_id else None
        if keep_ids:
            cur = conn.execute(
                f'INSERT INTO {_FU_MSGS} (id, msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?, ?)',
                (row['id'], msg_id, ts, row.get('device_type') or 'unknown', json.dumps(meta)),
            )
        else:
            cur = conn.execute(
                f'INSERT INTO {_FU_MSGS} (msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?)',
                (msg_id, ts, row.get('device_type') or 'unknown', json.dumps(meta)),
            )
        row_id = cur.lastrowid
        conn.executemany(
            f'INSERT INTO {_FU_ITEMS} (msg_row_id, key, value) VALUES (?, ?, ?)',
            [(row_id, k, v) for k, v in items],
        )

    # ------------------------------------------------------------------ #
    # Public write API                                                     #
    # ------------------------------------------------------------------ #

    def store_message(self,
                      system_name: str,
                      msg_id:      Optional[str],
                      timestamp:   str,
                      device_type: str,
                      data_type:   Optional[str],
                      data:        Dict[str, Any],
                      on_new:      Optional[Callable[[], None]] = None,
                      raw_timestamp: Optional[str] = None) -> str:
        """
        Store one message's payload in its typed table, in one transaction.

        Returns 'stored' or 'duplicate' (msg_id already present — nothing is
        written and on_new is not called).  on_new runs inside the transaction
        after the row is inserted; if it raises, the insert is rolled back so
        a retry re-applies both.  Raises RejectedMessage for permanently bad
        messages and sqlite3/OS errors for storage failures
        (MigrationBlockedError while a held-back migration is pending).

        A message whose msg_id is not stored yet (whatever its form: none,
        derived "legacy-...", or a collector's random id that an older
        Manager dropped) is also a duplicate if a pre-upgrade row (msg_id
        NULL) has the same timestamp (raw_timestamp, the header string as the
        old Manager stored it, or the canonical timestamp) and, for
        user_metrics, the same username.

        Args:
            system_name:  e.g. "Triton"
            msg_id:       header.msg_id (or the legacy derived id)
            timestamp:    canonical ISO-8601 UTC (storage_util.canonical_timestamp)
            device_type:  e.g. "synology", "windows"
            data_type:    e.g. "folder_usage", "system_metrics", or None
            data:         The full data dict from the message (the 'data_type'
                          key is stripped so it doesn't become a column)
        """
        table = self.ingest_table(system_name, data_type)
        payload = {k: v for k, v in data.items() if k != 'data_type'}
        db = self._get_db(system_name, create=True)
        self._check_writable(db)
        if table == FOLDER_USAGE:
            self._check_folder_writable(db)

        natural_ts = tuple(dict.fromkeys(t for t in (raw_timestamp, timestamp) if isinstance(t, str)))

        try:
            with db.transaction() as conn:
                if table == FOLDER_USAGE:
                    if FOLDER_USAGE in self._list_tables(conn):
                        # Never start the narrow tables next to an unmigrated
                        # wide table (reads would stop serving the wide one)
                        db.folder_migrated = False
                        raise MigrationBlockedError(
                            f"{db.path}: the wide folder_usage table is not migrated yet")
                    self._ensure_folder_tables(conn)
                    if msg_id and conn.execute(
                            f'SELECT 1 FROM {_FU_MSGS} WHERE msg_id = ?', (msg_id,)).fetchone():
                        raise _Duplicate()
                    if self._has_pre_upgrade_row(conn, table, natural_ts, payload):
                        raise _Duplicate()
                    self._insert_folder_usage(conn, msg_id, timestamp, device_type, payload)
                else:
                    self._ensure_table(conn, table)
                    if msg_id and conn.execute(
                            f'SELECT 1 FROM "{table}" WHERE msg_id = ?', (msg_id,)).fetchone():
                        raise _Duplicate()
                    if self._has_pre_upgrade_row(conn, table, natural_ts, payload):
                        raise _Duplicate()
                    self._insert_wide(conn, table, msg_id, timestamp, device_type, payload)
                if on_new is not None:
                    on_new()
        except _Duplicate:
            return 'duplicate'
        except sqlite3.IntegrityError as e:
            # UNIQUE(msg_id) backstop (e.g. another process inserted it)
            if 'msg_id' in str(e):
                return 'duplicate'
            raise RejectedMessage('constraint violation')
        return 'stored'

    def store(self,
              system_name: str,
              timestamp:   str,
              device_type: str,
              data_type:   Optional[str],
              data:        Dict[str, Any]) -> bool:
        """Compatibility wrapper (no msg_id, no dedup). Returns True on success."""
        try:
            try:
                timestamp = canonical_timestamp(timestamp)
            except ValueError:
                pass
            self.store_message(system_name, None, timestamp, device_type, data_type, data)
            return True
        except Exception as e:
            logger.error(f"[data_store] store failed for {system_name}/{data_type}: {e}", exc_info=True)
            return False

    @classmethod
    def _has_pre_upgrade_row(cls, conn, table: str, timestamps: Sequence[str],
                             payload: Dict[str, Any]) -> bool:
        """
        True if `table` holds a row stored before dedup existed (msg_id NULL)
        with this natural key: one of `timestamps` (the raw header string the
        old Manager stored, and the canonical form migrated rows have), plus
        the username for user_metrics.  Index-backed (timestamp indexes).
        """
        if not timestamps:
            return False
        ph = ', '.join(['?'] * len(timestamps))
        params = list(timestamps)
        if table == FOLDER_USAGE:
            if conn.execute(f'SELECT 1 FROM {_FU_MSGS} INDEXED BY idx_{_FU_MSGS}_ts '
                            f'WHERE timestamp IN ({ph}) AND msg_id IS NULL LIMIT 1',
                            params).fetchone():
                return True
            # A wide table whose migration failed (still being retried) holds
            # pre-upgrade rows too; it never has msg_ids.
            if FOLDER_USAGE in cls._list_tables(conn):
                return conn.execute(f'SELECT 1 FROM "{FOLDER_USAGE}" WHERE timestamp IN ({ph}) LIMIT 1',
                                    params).fetchone() is not None
            return False

        cond = f'timestamp IN ({ph}) AND msg_id IS NULL'
        if table == 'user_metrics':
            by_lower = {c.lower(): c for c in cls._get_columns(conn, table)}
            username = payload.get('username')
            if 'username' not in by_lower or not isinstance(username, str):
                return False
            cond += f' AND "{_quote_col(by_lower["username"])}" = ?'
            params.append(username)
        return conn.execute(f'SELECT 1 FROM "{table}" INDEXED BY "idx_{table}_ts" WHERE {cond} LIMIT 1',
                            params).fetchone() is not None

    def _insert_wide(self, conn, table, msg_id, timestamp, device_type, payload):
        existing = self._get_columns(conn, table)
        mapped = _prepare_wide_payload(payload, existing)
        self._evolve_schema(conn, table, existing, mapped)

        # Column names stored verbatim (quoted); values cleaned for SQLite
        cols   = ['msg_id', 'timestamp', 'device_type'] + list(mapped.keys())
        vals   = [msg_id,   timestamp,   device_type]   + list(mapped.values())
        ph     = ', '.join(['?'] * len(cols))
        cnames = ', '.join(f'"{_quote_col(c)}"' for c in cols)
        conn.execute(f'INSERT INTO "{table}" ({cnames}) VALUES ({ph})', vals)

    @staticmethod
    def _insert_folder_usage(conn, msg_id, timestamp, device_type, payload):
        meta, items = _split_folder_payload(payload)
        cur = conn.execute(
            f'INSERT INTO {_FU_MSGS} (msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?)',
            (msg_id, timestamp, device_type, json.dumps(meta, allow_nan=False)),
        )
        row_id = cur.lastrowid
        conn.executemany(
            f'INSERT INTO {_FU_ITEMS} (msg_row_id, key, value) VALUES (?, ?, ?)',
            [(row_id, k, v) for k, v in items],
        )

    # ------------------------------------------------------------------ #
    # Public read API                                                      #
    # ------------------------------------------------------------------ #

    def fetch_rows(self,
                   system_name: str,
                   data_type:   str,
                   start:       Optional[str] = None,
                   end:         Optional[str] = None,
                   limit:       int = 100,
                   newest_first: bool = True) -> Tuple[List[dict], bool]:
        """
        Rows of a typed table as flat dicts, plus a 'truncated' flag that is
        True when more than `limit` rows matched.  start/end bound the
        timestamp as [start, end).  Returns ([], False) if the system or
        table doesn't exist.
        """
        db = self._get_db(system_name)
        if db is None:
            return [], False
        with db.lock:
            table = _readable_table(data_type, self._list_tables(db.conn))
        if table is None:
            return [], False

        conds, params = [], []
        if start:
            conds.append('timestamp >= ?')
            params.append(start)
        if end:
            conds.append('timestamp < ?')
            params.append(end)
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        order = 'DESC' if newest_first else 'ASC'
        limit = max(1, int(limit))

        with db.lock:
            tables = self._list_tables(db.conn)
            if table == FOLDER_USAGE and _FU_MSGS not in tables and FOLDER_USAGE in tables:
                # Not migrated yet (held back): serve the old wide table
                cursor = db.conn.execute(
                    f'SELECT * FROM "{FOLDER_USAGE}" {where} ORDER BY timestamp {order}, id {order} LIMIT ?',
                    params + [limit + 1],
                )
                cols = [d[0] for d in cursor.description]
                rows = [self._wide_folder_row(dict(zip(cols, row))) for row in cursor.fetchall()]
            elif table == FOLDER_USAGE:
                if _FU_MSGS not in tables:
                    return [], False
                rows = self._folder_rows(db, where, params, order, limit + 1)
            else:
                if table not in tables:
                    return [], False
                cursor = db.conn.execute(
                    f'SELECT * FROM "{table}" {where} ORDER BY timestamp {order}, id {order} LIMIT ?',
                    params + [limit + 1],
                )
                cols = [d[0] for d in cursor.description]
                rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

        truncated = len(rows) > limit
        return rows[:limit], truncated

    @staticmethod
    def _wide_folder_row(row: dict) -> dict:
        """A row of an unmigrated wide folder_usage table in the narrow read shape."""
        out = {'id': row.get('id'), 'msg_id': None,
               'timestamp': row.get('timestamp'), 'device_type': row.get('device_type')}
        for col, value in row.items():
            if col.lower() in _INTERNAL_LOWER or value is None or value == '' \
                    or isinstance(value, bytes):
                continue   # NULL / '' = the old backfill for "not measured"
            # Values as stored (lists/dicts stay JSON text), exactly what the
            # old Manager returned and what the migration copies into meta_json.
            out[col] = value
        return out

    def _folder_rows(self, db: LockedConnection, where: str, params: list,
                     order: str, limit: int) -> List[dict]:
        """Reassemble narrow folder_usage runs into the legacy flat dict shape."""
        msgs = db.conn.execute(
            f'SELECT id, msg_id, timestamp, device_type, meta_json FROM {_FU_MSGS} '
            f'{where} ORDER BY timestamp {order}, id {order} LIMIT ?',
            params + [limit],
        ).fetchall()
        rows, by_id = [], {}
        for m in msgs:
            row = {'id': m['id'], 'msg_id': m['msg_id'],
                   'timestamp': m['timestamp'], 'device_type': m['device_type']}
            try:
                meta = json.loads(m['meta_json'] or '{}')
            except ValueError:
                meta = {}
            if isinstance(meta, dict):
                row.update(meta)
            rows.append(row)
            by_id[m['id']] = row

        ids = list(by_id)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ', '.join(['?'] * len(chunk))
            for item in db.conn.execute(
                    f'SELECT msg_row_id, key, value FROM {_FU_ITEMS} WHERE msg_row_id IN ({ph})', chunk):
                by_id[item[0]][item[1]] = item[2]
        return rows

    def get_recent(self,
                   system_name: str,
                   data_type:   str,
                   limit:       int = 100) -> List[dict]:
        """
        Return the most recent `limit` rows from a typed table as plain dicts.
        Returns [] if the system or table doesn't exist yet.
        """
        try:
            return self.fetch_rows(system_name, data_type, limit=limit, newest_first=True)[0]
        except Exception as e:
            logger.error(f"[data_store] get_recent failed for {system_name}/{data_type}: {e}")
            return []

    def get_between(self,
                    system_name: str,
                    data_type:   str,
                    start:       str,
                    end:         str,
                    limit:       int = DEFAULT_ROW_LIMIT) -> List[dict]:
        """
        Return up to `limit` rows whose timestamp falls in [start, end), oldest first.
        """
        try:
            return self.fetch_rows(system_name, data_type, start, end, limit=limit, newest_first=False)[0]
        except Exception as e:
            logger.error(f"[data_store] get_between failed for {system_name}/{data_type}: {e}")
            return []

    def get_field_series(self,
                         system_name: str,
                         data_type:   str,
                         field:       str,
                         start:       Optional[str] = None,
                         end:         Optional[str] = None,
                         limit:       int = 500,
                         max_points:  Optional[int] = None) -> Optional[dict]:
        """
        Time series of one numeric field, oldest first.

        With no start: the newest `limit` points (tail query); "truncated"
        when older points exist.
        With start (and optionally end): points in [start, end), at most
        `limit`.  If more match, the whole range is bucket-averaged down to
        `limit` points ("downsampled") rather than cut off, so a caller that
        sends no max_points (an older Dashboard's custom range) still sees
        the full range, including its newest data.
        With max_points: the selected range is bucket-averaged in SQL into at
        most max_points points (equal-width time buckets, empty buckets
        omitted); `limit` then only bounds a tail query.

        `field` is resolved as given, then with a leading '/' (volume paths
        lose it in URL routing).  A folder_usage field that is not a folder /
        volume key but a numeric metadata key (e.g. "_scan_seconds", which the
        old wide table kept as a column) is read from the runs' metadata.

        Returns None if the system/table has no rows at all, else
        {"field", "unit", "points": [(timestamp, float)], "truncated", "downsampled"}.
        """
        db = self._get_db(system_name)
        if db is None:
            return None

        with db.lock:
            conn = db.conn
            tables = self._list_tables(conn)
            table = _readable_table(data_type, tables)
            if table is None:
                return None
            # A folder_usage table not migrated yet (held back) is read as a wide table
            wide_folder = table == FOLDER_USAGE and _FU_MSGS not in tables and FOLDER_USAGE in tables
            if table == FOLDER_USAGE and not wide_folder:
                if _FU_MSGS not in tables or conn.execute(f'SELECT 1 FROM {_FU_MSGS} LIMIT 1').fetchone() is None:
                    return None
                resolved, meta_key = field, None
                if conn.execute(f'SELECT 1 FROM {_FU_ITEMS} WHERE key = ? LIMIT 1', (field,)).fetchone() is None:
                    if conn.execute(f'SELECT 1 FROM {_FU_ITEMS} WHERE key = ? LIMIT 1', ('/' + field,)).fetchone():
                        logger.info(f"Field resolved with leading slash: '{field}' -> '/{field}'")
                        resolved = '/' + field
                    else:
                        meta_key = self._folder_meta_key(conn, field)
                if meta_key is not None:
                    resolved = meta_key
                    src = f'{_FU_MSGS} m'
                    ts_expr = 'm.timestamp'
                    val_expr = f'lm_meta_number(m.meta_json, {_sql_literal(meta_key)})'
                    where, params = [_numeric_filter(val_expr)], []
                else:
                    src = f'{_FU_ITEMS} i JOIN {_FU_MSGS} m ON m.id = i.msg_row_id'
                    ts_expr, val_expr = 'm.timestamp', 'i.value'
                    where, params = [f'i.key = ?', _numeric_filter(val_expr)], [resolved]
                unit = self._folder_unit(conn, resolved)
            else:
                if table not in tables or conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None:
                    return None
                cols = self._get_columns(conn, table)
                by_lower = {c.lower(): c for c in cols}
                resolved = by_lower.get(field.lower())
                if resolved is None and ('/' + field).lower() in by_lower:
                    resolved = by_lower[('/' + field).lower()]
                    logger.info(f"Field resolved with leading slash: '{field}' -> '{resolved}'")
                if resolved is None or resolved.lower() in _INTERNAL_LOWER:
                    return {'field': field, 'unit': None, 'points': [],
                            'truncated': False, 'downsampled': False}
                src = f'"{table}"'
                ts_expr, val_expr = 'timestamp', f'"{_quote_col(resolved)}"'
                where, params = [_numeric_filter(val_expr)], []
                unit = self._wide_unit(conn, table, by_lower, resolved)

            points, truncated, downsampled = self._series(
                conn, src, ts_expr, val_expr, where, params, start, end, limit, max_points)

        return {'field': resolved, 'unit': unit, 'points': points,
                'truncated': truncated, 'downsampled': downsampled}

    @staticmethod
    def _series(conn, src, ts, val, where, params, start, end, limit, max_points):
        conds, p = list(where), list(params)
        if start:
            conds.append(f'{ts} >= ?')
            p.append(start)
        if end:
            conds.append(f'{ts} < ?')
            p.append(end)
        tail = not start
        limit = max(1, int(limit))
        if not tail and not max_points:
            # Window without max_points: never drop part of the range; more
            # than `limit` points are bucket-averaged down to `limit`.
            max_points = limit

        def to_points(rows):
            out = []
            for t, v in rows:
                try:
                    out.append((t, float(v)))
                except (TypeError, ValueError):
                    continue
            return out

        if not max_points:
            # Tail query: newest `limit` points, returned oldest first
            rows = conn.execute(
                f'SELECT {ts}, {val} FROM {src} WHERE {" AND ".join(conds)} '
                f'ORDER BY {ts} DESC LIMIT ?', p + [limit + 1]).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            rows.reverse()
            return to_points(rows), truncated, False

        truncated = False
        if tail:
            # Bound the tail to the newest `limit` points, then downsample.
            row = conn.execute(
                f'SELECT {ts} FROM {src} WHERE {" AND ".join(conds)} '
                f'ORDER BY {ts} DESC LIMIT 1 OFFSET ?', p + [limit - 1]).fetchone()
            if row is not None:
                truncated = conn.execute(
                    f'SELECT 1 FROM {src} WHERE {" AND ".join(conds)} AND {ts} < ? LIMIT 1',
                    p + [row[0]]).fetchone() is not None
                conds.append(f'{ts} >= ?')
                p.append(row[0])

        wsql = ' AND '.join(conds) + f' AND julianday({ts}) IS NOT NULL'
        count, jmin, jmax = conn.execute(
            f'SELECT COUNT(*), MIN(julianday({ts})), MAX(julianday({ts})) FROM {src} WHERE {wsql}', p).fetchone()
        if count <= max_points:
            rows = conn.execute(
                f'SELECT {ts}, {val} FROM {src} WHERE {wsql} ORDER BY {ts} ASC', p).fetchall()
            return to_points(rows), truncated, False

        width = (jmax - jmin) / max_points or 1.0
        rows = conn.execute(
            f'SELECT AVG(julianday({ts})) AS jd, AVG(CAST({val} AS REAL)) AS v FROM {src} '
            f'WHERE {wsql} '
            f'GROUP BY MIN(CAST((julianday({ts}) - ?) / ? AS INTEGER), ?) ORDER BY jd',
            p + [jmin, width, max_points - 1]).fetchall()
        return [(_jd_to_timestamp(jd), float(v)) for jd, v in rows if v is not None], truncated, True

    @staticmethod
    def _wide_unit(conn, table, by_lower, field) -> Optional[str]:
        """
        Two-tier unit lookup on the newest row:
          1. {field}_unit — per-field sibling (system_metrics style)
          2. _unit        — message-level default
        """
        unit_cols = [by_lower[c] for c in (f'{field}_unit'.lower(), '_unit') if c in by_lower]
        if not unit_cols:
            return None
        sel = ', '.join(f'"{_quote_col(c)}"' for c in unit_cols)
        row = conn.execute(f'SELECT {sel} FROM "{table}" ORDER BY timestamp DESC LIMIT 1').fetchone()
        if row is None:
            return None
        return next((v for v in row if v), None)

    @staticmethod
    def _folder_meta_key(conn, field) -> Optional[str]:
        """field (or '/' + field) if some run stores it as metadata, else None."""
        for key in (field, '/' + field):
            if conn.execute(f'SELECT 1 FROM {_FU_MSGS} WHERE instr(meta_json, ?) > 0 LIMIT 1',
                            (json.dumps(key) + ':',)).fetchone():
                return key
        return None

    @staticmethod
    def _folder_unit(conn, field) -> Optional[str]:
        row = conn.execute(f'SELECT meta_json FROM {_FU_MSGS} ORDER BY timestamp DESC LIMIT 1').fetchone()
        try:
            meta = json.loads(row[0]) if row else {}
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            return None
        return meta.get(f'{field}_unit') or meta.get('_unit')

    def ingest_table(self, system_name: str, data_type) -> str:
        """
        Table a new message with this data_type goes to; raises RejectedMessage.
        A label the current rules refuse is still accepted when this system's
        database already has the table an older Manager made from it (that
        stream of data keeps working); no new table is ever made from one.
        """
        try:
            return table_for_data_type(data_type)
        except RejectedMessage:
            db = self._get_db(system_name)
            if db is not None:
                with db.lock:
                    table = _readable_table(data_type, self._list_tables(db.conn))
                if table is not None:
                    return table
            raise

    def is_readable_data_type(self, system_name: str, data_type) -> bool:
        """True if reads may ask for this data_type (valid label, or an old table of this system)."""
        if is_valid_data_type(data_type):
            return True
        db = self._get_db(system_name)
        if db is None:
            return False
        with db.lock:
            return _readable_table(data_type, self._list_tables(db.conn)) is not None

    def list_data_types(self, system_name: str) -> List[str]:
        """Return the data_types present for a system ([] if unknown)."""
        db = self._get_db(system_name)
        if db is None:
            return []
        try:
            with db.lock:
                tables = self._list_tables(db.conn)
        except Exception:
            return []
        types = {t for t in tables if not _is_internal_table(t)}
        if _FU_MSGS in tables:
            types.add(FOLDER_USAGE)
        return sorted(types)

    def list_systems(self) -> List[str]:
        """Return system directory names that have a data.db in the data_dir."""
        try:
            return sorted(
                d for d in os.listdir(self.data_dir)
                if os.path.isfile(os.path.join(self.data_dir, d, 'data.db'))
            )
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Retention support (archive_metrics.py)                               #
    # ------------------------------------------------------------------ #

    def rows_older_than(self, system_name: str, data_type: str,
                        cutoff: str, limit: int = 5000) -> List[dict]:
        """Oldest-first rows with timestamp < cutoff (flat dicts with 'id')."""
        return self.fetch_rows(system_name, data_type, end=cutoff,
                               limit=limit, newest_first=False)[0]

    def delete_rows(self, system_name: str, data_type: str, ids: List[int]) -> int:
        """Delete rows by id (one transaction). Returns rows deleted."""
        db = self._get_db(system_name)
        if db is None or not ids:
            return 0
        self._check_writable(db)
        with db.lock:
            table = _readable_table(data_type, self._list_tables(db.conn))
        if table is None:
            return 0
        if table == FOLDER_USAGE:
            # ids of rows read from an unmigrated wide table are not narrow ids
            self._check_folder_writable(db)
        deleted = 0
        with db.transaction() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ', '.join(['?'] * len(chunk))
                if table == FOLDER_USAGE:
                    conn.execute(f'DELETE FROM {_FU_ITEMS} WHERE msg_row_id IN ({ph})', chunk)
                    deleted += conn.execute(f'DELETE FROM {_FU_MSGS} WHERE id IN ({ph})', chunk).rowcount
                else:
                    deleted += conn.execute(f'DELETE FROM "{table}" WHERE id IN ({ph})', chunk).rowcount
        return deleted

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def delete_system(self, name: str) -> bool:
        """
        Delete the per-system history database file entirely.
        Closes any open connection first, then removes the .db file (and its
        WAL/SHM side files).  Returns True if the file was deleted, False if
        it didn't exist.
        """
        key = normalize_device_name(name)
        db_path = self._db_path(name)
        with self._cache_lock:
            db = self._dbs.pop(key, None)
            if db is not None:
                db.close()

            if not os.path.exists(db_path):
                logger.warning(f"No per-system DB found at {db_path}")
                return False
            try:
                os.remove(db_path)
                for suffix in ('-wal', '-shm'):
                    if os.path.exists(db_path + suffix):
                        os.remove(db_path + suffix)
                logger.info(f"Deleted per-system DB: {db_path}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete {db_path}: {e}")
                return False

    def close(self):
        with self._cache_lock:
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()
