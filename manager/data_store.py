"""
TypedDataStore — per-system SQLite history, one table (or table pair) per data_type.

Storage layout:
    data_dir/
    └── <normalized_system_name>/          (storage_util.normalize_device_name)
        └── data.db
            ├── folder_usage_msgs     one row per disk collection run
            ├── folder_usage_items    one row per (run, folder/volume key) -> value
            ├── folder_usage_legacy   pre-upgrade wide table, kept after migration
            ├── storage_breakdown_msgs    one row per storage attribution run
            ├── storage_breakdown_rows    one row per (run, folder / device, total / owner / other)
            ├── storage_breakdown_legacy  wide table an older Manager wrote, kept after migration
            ├── system_metrics        one row per metrics collection run
            ├── user_metrics          one row per user per metrics run
            ├── not_specified         fallback for messages with no data_type
            ├── _rollup_*             hourly / daily rollups (rollups.py)
            └── <any_future_type>

folder_usage is stored NARROW.  A folder_usage payload has one key per
scanned folder, and the old layout added one column per folder path, which
runs into SQLite's 2000-column limit on big shares.  The read API reassembles
each run into the same flat dict as before:
    {"id", "msg_id", "timestamp", "device_type", "_unit", "/volume1/Data": 123, ...}
An existing wide "folder_usage" table is migrated on first open: rows are
copied into the narrow tables in one transaction and the old table is
renamed to folder_usage_legacy (never dropped).

storage_breakdown is stored NARROW too (the folder_usage pattern): a run
can list thousands of folders with their owners, and per-owner history needs
rows, not hundreds of KB of JSON per chart.  The read API rebuilds each run
as it was sent: {"id", "msg_id", "timestamp", "device_type", <top-level
keys>, "folders": [{path, folder_owner, bytes, ..., owners: [...], other}],
"device": {...}}.  A wide "storage_breakdown" table (an older Manager stored
a newer collector's runs one column per key, the entries as JSON text) is
migrated like folder_usage, after the pre-upgrade backup: copied in one
transaction, then renamed to storage_breakdown_legacy (never dropped).  A
failed copy blocks only new storage_breakdown messages ('error') and is
retried.  Meanwhile a first migration's wide table (no narrow tables yet)
keeps being served; after a downgrade, when the older Manager made the wide
table again next to the narrow tables, only the narrow runs are served until
the copy succeeds (the runs in the wide table are not lost, only not shown).

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

Rollups: with a rollups.RollupEngine (manager config "rollups", on by
default) every row stored in a rolled-up table (rollups.REGISTRY:
system_metrics, user_metrics, sessions, storage_health) is added to its
hourly and daily rollups in the same transaction, and the rows already stored
when a database is first opened are rolled up by the background backfill
(rollups.start_backfill_thread).  The _rollup_* tables are created after a
needed migration has run (never while it is held back), only in a database
that has a rolled-up table, and only ever added to.  Adding them is a schema
change, so a database that already holds rows gets a pre-upgrade backup
first (_rollup_backup), unless the migration of the same open already took
one; a database this Manager creates gets them with its first rolled-up
row, no backup.

History reads (get_field_series) of a rolled-up field may be answered from
its rollups: resolution 'auto' picks raw rows, hourly or daily rollups from
the point width of the window (_plan_level), and stays on the raw rows while
a backfill is pending, when the window holds no more than max_points values
and for user_metrics without entity (every user mixed), so data an older
Manager stored charts as before.  A rollup point never lies outside the
first and last value of the window; a table retention emptied is still
charted from its rollups.

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

import rollups as rollup_engine
from storage_util import (
    UPGRADE_RETRY_SECONDS, LockedConnection, MigrationBlockedError, backup_database,
    canonical_timestamp, current_group, format_timestamp, group_member, normalize_device_name,
    parse_timestamp, row_keys_digest, table_fingerprint,
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

STORAGE_BREAKDOWN = 'storage_breakdown'
_SB_MSGS = 'storage_breakdown_msgs'
_SB_ROWS = 'storage_breakdown_rows'
_SB_LEGACY = 'storage_breakdown_legacy'
_SB_REASON = 'wide storage_breakdown table'
# Numbers of a folder / device / owner / other entry (one column each)
_SB_NUMBERS = ('bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')
_SB_COLUMNS = ('scope', 'path', 'kind', 'owner') + _SB_NUMBERS + ('owner_count', 'folder_owner', 'ord', 'extra_json')
# History fields: owner/<owner>/<metric> (device rollup of one owner),
# folder/<path>/<metric> (folder total)
_SB_FIELD_RE = re.compile(r'(owner|folder)/(.+)/(bytes|files|cold_1y_bytes|cold_3y_bytes)', re.DOTALL)

# data_type labels: letter first, then letters/digits/_/-, max 64 chars.
_DATA_TYPE_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
# Table-name prefixes reserved for SQLite, for the folder_usage layout, and
# for tables a later Manager adds (storage_breakdown_*, _rollup_* ...).  They
# are reserved now so that running this Manager again on a database a later
# one has written neither lists those tables nor "migrates" (and backs up)
# them as old tables on every start.
_RESERVED_PREFIXES = ('sqlite_', 'folder_usage_', 'storage_breakdown_', '_')
# The tables that really are internal (never listed or read as a data_type)
_INTERNAL_TABLE_RE = re.compile(
    r'sqlite_.*|folder_usage_(?:msgs|items|legacy(?:_\d+)?)|storage_breakdown_.*|_.*')

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


def _sb_number(value) -> bool:
    """A value a storage_breakdown number column stores as sent: null or a finite int64 / float."""
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return _INT64_MIN <= value <= _INT64_MAX
    return math.isfinite(value)


def _sb_extra(entry: dict, standard: Sequence[str], nullable=()) -> Optional[str]:
    """
    extra_json of a narrow row: what its columns can't say about the entry -
    standard keys it did not have ("absent"), list/object keys that were
    null ("null") and keys outside the standard ones ("extra").  None when
    there is nothing (the usual case).
    """
    out = {}
    absent = [k for k in standard if k not in entry]
    nulls = [k for k in nullable if k in entry and entry[k] is None]
    extra = {k: _scrub_json(v) for k, v in entry.items() if k not in standard}
    if absent:
        out['absent'] = absent
    if nulls:
        out['null'] = nulls
    if extra:
        out['extra'] = extra
    return json.dumps(out, allow_nan=False) if out else None


def _sb_entry_rows(scope: str, path: str, entry, ord_: int) -> Optional[list]:
    """
    Narrow rows of one folder (or the device) entry: its 'total' row, one
    'owner' row per owner (ord = position) and an 'other' row; None if the
    entry can't be stored narrow (it is then kept as sent in meta_json).
    """
    if not isinstance(entry, dict) or not all(_sb_number(entry.get(k)) for k in _SB_NUMBERS):
        return None
    folder_owner = entry.get('folder_owner') if scope == 'folder' else None
    if folder_owner is not None and not isinstance(folder_owner, str):
        return None
    owners, other = entry.get('owners'), entry.get('other')
    if owners is not None and not isinstance(owners, list):
        return None
    if other is not None and not (isinstance(other, dict) and _sb_number(other.get('owner_count'))
                                  and all(_sb_number(other.get(k)) for k in _SB_NUMBERS)):
        return None
    standard = (('path', 'folder_owner') if scope == 'folder' else ()) + _SB_NUMBERS + ('owners', 'other')
    rows = [(scope, path, 'total', '') + tuple(entry.get(k) for k in _SB_NUMBERS)
            + (None, folder_owner, ord_, _sb_extra(entry, standard, nullable=('owners',)))]
    seen = set()
    for i, o in enumerate(owners or ()):
        if not (isinstance(o, dict) and isinstance(o.get('owner'), str) and o['owner'] and o['owner'] not in seen
                and all(_sb_number(o.get(k)) for k in _SB_NUMBERS)):
            return None
        seen.add(o['owner'])
        rows.append((scope, path, 'owner', o['owner']) + tuple(o.get(k) for k in _SB_NUMBERS)
                    + (None, None, i, _sb_extra(o, ('owner',) + _SB_NUMBERS)))
    if isinstance(other, dict):
        rows.append((scope, path, 'other', '') + tuple(other.get(k) for k in _SB_NUMBERS)
                    + (other.get('owner_count'), None, 0, _sb_extra(other, ('owner_count',) + _SB_NUMBERS)))
    return rows


def _split_breakdown_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], list]:
    """
    (meta, rows) of a storage_breakdown payload: "folders" and "device" go to
    narrow rows, everything else (and any of the two that can't be stored
    narrow, an empty list, null) stays in meta as sent.
    """
    meta = {k: _scrub_json(v) for k, v in payload.items() if k not in ('folders', 'device')}
    rows = []
    folders = payload.get('folders')
    if isinstance(folders, list) and folders:
        frows, paths = [], set()
        for i, f in enumerate(folders):
            path = f.get('path') if isinstance(f, dict) else None
            entry_rows = _sb_entry_rows('folder', path, f, i) \
                if isinstance(path, str) and path and path not in paths else None
            if entry_rows is None:
                frows = None
                break
            paths.add(path)
            frows.extend(entry_rows)
        if frows is None:
            logger.warning("[data_store] storage_breakdown: folders kept as sent (an entry can't be stored "
                           "in the narrow layout: no path, a duplicate path or owner, or a non-number)")
            meta['folders'] = _scrub_json(folders)
        else:
            rows.extend(frows)
    elif 'folders' in payload:
        meta['folders'] = _scrub_json(folders)
    device = payload.get('device')
    drows = _sb_entry_rows('device', '', device, 0) if isinstance(device, dict) else None
    if drows is not None:
        rows.extend(drows)
    elif 'device' in payload:
        meta['device'] = _scrub_json(device)
    return meta, rows


def _sb_decode(total, owners, other, scope: str) -> dict:
    """One folder / device entry rebuilt from its narrow rows (the inverse of _sb_entry_rows)."""
    def info(row):
        try:
            return json.loads(row['extra_json']) if row['extra_json'] else {}
        except ValueError:
            return {}

    def numbers(row, first, absent):
        out = dict(first)
        for k in _SB_NUMBERS:
            if k not in absent:
                out[k] = row[k]
        return out

    meta = info(total)
    absent = set(meta.get('absent') or ())
    head = {}
    if scope == 'folder':
        head['path'] = total['path']
        if 'folder_owner' not in absent:
            head['folder_owner'] = total['folder_owner']
    entry = numbers(total, head, absent)
    if 'owners' not in absent:
        if 'owners' in (meta.get('null') or ()):
            entry['owners'] = None
        else:
            entry['owners'] = []
            for o in sorted(owners, key=lambda r: r['ord']):
                oi = info(o)
                row = numbers(o, {'owner': o['owner']}, set(oi.get('absent') or ()))
                row.update(oi.get('extra') or {})
                entry['owners'].append(row)
    if 'other' not in absent:
        if other is None:
            entry['other'] = None
        else:
            oi = info(other)
            oabsent = set(oi.get('absent') or ())
            row = numbers(other, {} if 'owner_count' in oabsent else {'owner_count': other['owner_count']}, oabsent)
            row.update(oi.get('extra') or {})
            entry['other'] = row
    entry.update(meta.get('extra') or {})
    return entry


def _decode_wide_value(value):
    """A wide-table value as sent: JSON text of a list / object (how lists were stored) decoded."""
    if isinstance(value, str) and value[:1] in ('[', '{'):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, (list, dict)):
            return decoded
    return value


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


# storage_health history keys carry no *_unit siblings (spec §14): the unit
# follows from the name
_SUFFIX_UNITS = (('_temperature_c', '°C'), ('_percent', '%'), ('_hours', 'h'),
                 ('_sectors', 'count'), ('_errors', 'count'), ('_code', 'count'), ('_degraded', 'count'))


def _suffix_unit(table: str, field: str) -> Optional[str]:
    if table != 'storage_health':
        return None
    low = field.lower()
    return next((unit for suffix, unit in _SUFFIX_UNITS if low.endswith(suffix)), None)


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

    def __init__(self, data_dir: str, backup_before_migration: bool = True,
                 rollups: Optional['rollup_engine.RollupEngine'] = None):
        self.data_dir = data_dir
        self.backup_before_migration = backup_before_migration
        self.rollups = rollups        # None: no rollups are written (manager config rollups.enabled false)
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
        the directory or database for an unknown system.  Inside a group
        commit (storage_util.CommitGroup) the group's connection to this
        system is returned without taking the cache lock: the thread holds
        that connection's lock, and delete_system() / close() take the cache
        lock before it.
        """
        if current_group() is not None:
            member = group_member(self._db_path(system_name))
            if member is not None:
                return member
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
            db.breakdown_migrated = False
            db.breakdown_error = None     # why the wide -> narrow storage_breakdown copy failed
            db.breakdown_attempted = 0.0
            db.upgrade_blocked = None     # reason while a needed migration is held back
            db.upgrade_attempted = 0.0
            db.rollups_ready = False      # rollup tables checked / gaps queued for this open
            db.rollups_attempted = 0.0
            db.migration_backed_up = False  # this open's migration decided its pre-upgrade backup
            db.rollup_backup_done = False   # the rollup tables may be added (backed up, or not needed)
            try:
                self._upgrade_db(db)
            except BaseException:
                db.close()                # e.g. a damaged file: retried on the next access
                raise
            self._init_rollups(db)
            self._dbs[key] = db
            return db

    def cached_connection(self, system_name: str) -> Optional[LockedConnection]:
        """The open connection of a system's database, or None if it isn't open (yet)."""
        with self._cache_lock:
            return self._dbs.get(normalize_device_name(system_name))

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
            if not _is_internal_table(t) and t not in (FOLDER_USAGE, STORAGE_BREAKDOWN)
            and 'msg_id' not in {c.lower() for c in cls._get_columns(conn, t)}
        )
        if old:
            reasons.append('no msg_id column in ' + ', '.join(old))
        if STORAGE_BREAKDOWN in tables:
            reasons.append(_SB_REASON)
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
                db.breakdown_migrated = True
                db.upgrade_blocked = None
                return True
            reason = '; '.join(reasons)
            if reasons == [_SB_REASON]:
                # Only the storage_breakdown table left to migrate (a database
                # the release before it wrote, or a retry): this Manager never
                # writes to that table, so a backup holding it as it is now
                # holds all the migration changes.  Its row keys too: after a
                # downgrade an older Manager makes the table again, with ids
                # from 1 (as many rows as an earlier, migrated one)
                wide = (table_fingerprint(db.conn, STORAGE_BREAKDOWN), row_keys_digest(db.conn, STORAGE_BREAKDOWN))
                kwargs = {'covered': lambda c: (table_fingerprint(c, STORAGE_BREAKDOWN),
                                                row_keys_digest(c, STORAGE_BREAKDOWN)) == wide}
            else:
                # An older Manager only appended rows / added columns here
                kwargs = {'original_layout': self._has_original_layout, 'append_only': True}
            if self.backup_before_migration:
                try:
                    backup_database(db.conn, db.path, reason, **kwargs)
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
            # The backup (or the decision not to take one) is from before any
            # change of this open, so it covers the rollup tables added next
            db.migration_backed_up = True

            self._try_folder_migration(db)
            try:
                with db.transaction() as conn:
                    for table in sorted(self._list_tables(conn)):
                        if not _is_internal_table(table) and table not in (FOLDER_USAGE, STORAGE_BREAKDOWN):
                            self._ensure_table(conn, table)
            except Exception as e:
                # Retried table by table on the next write (store_message)
                logger.error(f"[data_store] adding msg_id columns failed for {db.path}: {e}",
                             exc_info=True)
            self._try_breakdown_migration(db)
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
                db.group_barrier('the folder_usage migration')
                self._try_folder_migration(db)
            if not db.folder_migrated:
                raise MigrationBlockedError(
                    f"{db.path}: the folder_usage migration is not done yet "
                    f"({db.folder_error or 'pending'}); retried every {UPGRADE_RETRY_SECONDS}s")

    def _try_breakdown_migration(self, db: LockedConnection) -> bool:
        """
        Run the wide -> narrow storage_breakdown copy; True when done (or not
        needed).  On failure the wide table stays, and only new
        storage_breakdown messages get 'error' until a retry succeeds
        (_check_breakdown_writable), as for folder_usage.  The wide table is
        served meanwhile only while no narrow table exists (a first
        migration); next to the narrow tables (after a downgrade) its runs
        are not shown until the copy succeeds.
        """
        with db.lock:
            db.breakdown_attempted = time.monotonic()
            try:
                self._migrate_wide_storage_breakdown(db)
            except Exception as e:
                db.breakdown_error = f'{type(e).__name__}: {e}'
                if _SB_MSGS in self._list_tables(db.conn):
                    served = ("are not shown until it succeeds (the runs already in the narrow tables "
                              "are; nothing is lost)")
                else:
                    served = "are still served"
                logger.error(
                    f"[data_store] storage_breakdown migration failed for {db.path}: {e}. The "
                    f"storage_breakdown rows in the wide table stay there and {served}; new "
                    f"storage_breakdown messages for this system get 'error' (kept queued by the "
                    f"collector) until the migration succeeds. Retried on the next storage_breakdown "
                    f"message after {UPGRADE_RETRY_SECONDS}s and at every start.",
                    exc_info=True,
                )
                return False
            if db.breakdown_error:
                logger.warning(f"[data_store] storage_breakdown migration of {db.path} succeeded on retry")
            db.breakdown_error = None
            return True

    def _check_breakdown_writable(self, db: LockedConnection):
        """Raise MigrationBlockedError while the wide storage_breakdown table is not migrated yet."""
        if db.breakdown_migrated:
            return
        with db.lock:
            if not db.breakdown_migrated and \
                    time.monotonic() - db.breakdown_attempted >= UPGRADE_RETRY_SECONDS:
                db.group_barrier('the storage_breakdown migration')
                self._try_breakdown_migration(db)
            if not db.breakdown_migrated:
                raise MigrationBlockedError(
                    f"{db.path}: the storage_breakdown migration is not done yet "
                    f"({db.breakdown_error or 'pending'}); retried every {UPGRADE_RETRY_SECONDS}s")

    def upgrade_pending(self, system_name: str) -> Optional[str]:
        """Why this system's database migration is held back, or None."""
        db = self._get_db(system_name)
        return db.upgrade_blocked if db is not None else None

    def type_migration_pending(self, system_name: str, data_type: str, retry: bool = True) -> Optional[str]:
        """
        Why rows of this data type can't be written or deleted now, or None:
        its wide folder_usage / storage_breakdown table is not migrated yet
        (the copy failed; with retry, tried again here once it is due).  Its
        rows are still read from the wide table.
        """
        db = self._get_db(system_name)
        if db is None:
            return None
        if db.upgrade_blocked:
            return f'its database migration is held back (pre-upgrade backup failed: {db.upgrade_blocked})'
        with db.lock:
            table = _readable_table(data_type, self._list_tables(db.conn))
            if table == FOLDER_USAGE and not db.folder_migrated and not retry:
                return f'the folder_usage migration is not done yet ({db.folder_error or "pending"})'
            if table == STORAGE_BREAKDOWN and not db.breakdown_migrated and not retry:
                return f'the storage_breakdown migration is not done yet ({db.breakdown_error or "pending"})'
        try:
            if table == FOLDER_USAGE:
                self._check_folder_writable(db)
            elif table == STORAGE_BREAKDOWN:
                self._check_breakdown_writable(db)
        except MigrationBlockedError as e:
            return str(e)
        return None

    # ------------------------------------------------------------------ #
    # Rollups (rollups.py)                                                 #
    # ------------------------------------------------------------------ #

    def _init_rollups(self, db: LockedConnection):
        """
        Rollup tables, state and the backfill queue for one open database
        (RollupEngine.init_db), once its migration is complete: never while
        it is held back or while a migration step still has to be retried.
        (An internal table would make a database that is still in its
        original layout look partly migrated, and its next pre-upgrade
        backup would then not be taken.)  A storage_breakdown migration
        still to be retried does not hold them back: its backup is decided
        by that one table (_upgrade_db).  A failure (e.g. the database
        locked by another program) never stops the open or ingest: rows
        stored meanwhile are not rolled up live and are queued as a 'gap' by
        the retry (rollups.INIT_RETRY_SECONDS, on the next write or backfill
        pass).
        """
        if self.rollups is None or db.upgrade_blocked:
            return
        db.rollups_attempted = time.monotonic()
        try:
            with db.lock:
                if [r for r in self._upgrade_reasons(db.conn) if r != _SB_REASON]:
                    return
            if not self._rollup_backup(db):
                return
            self.rollups.init_db(db)
            db.rollups_ready = True
        except Exception as e:
            logger.error(f"[data_store] rollup setup failed for {db.path} ({type(e).__name__}: {e}); "
                         f"history is stored as usual and rolled up once a retry succeeds "
                         f"(every {rollup_engine.INIT_RETRY_SECONDS}s)")

    @staticmethod
    def _layout(conn) -> dict:
        """{table: columns} of every table but SQLite's own."""
        return {t: tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{_quote_col(t)}")'))
                for t in sorted(TypedDataStore._list_tables(conn)) if not t.startswith('sqlite_')}

    def _rollup_backup(self, db: LockedConnection, adding: Optional[str] = None) -> bool:
        """
        Whether the _rollup_* tables may be added to this database now (by
        RollupEngine.init_db, or with the first row of `adding`, a rolled-up
        table it doesn't have yet).  A database that holds rows and has no
        rollup tables gets one pre-upgrade backup first (CLAUDE.md: schema
        changes add tables only after a backup_database copy), unless this
        open's migration already decided its backup.  False (rollups wait,
        retried after rollups.INIT_RETRY_SECONDS) while that backup fails;
        ingest is never blocked by it.  A copy already taken of the layout
        the database has now, without rollup tables, is reused, so a retry
        never takes a second one.
        """
        if db.rollup_backup_done:
            return True
        db.group_barrier('deciding the rollup tables\' backup')
        with db.lock:
            conn = db.conn
            tables = self._list_tables(conn)
            if '_rollup_state' in tables or db.migration_backed_up:
                db.rollup_backup_done = True
                return True
            if not ({adding} | tables) & set(rollup_engine.TABLE_ORDER):
                return True            # nothing to add yet (decided again when a rolled-up table comes)
            if not any(conn.execute(f'SELECT 1 FROM "{_quote_col(t)}" LIMIT 1').fetchone()
                       for t in tables if not t.startswith('sqlite_')):
                db.rollup_backup_done = True     # nothing stored yet: nothing to back up
                return True
            reason = 'adding the rollup tables (_rollup_*)'
            if not self.backup_before_migration:
                logger.warning(f"[data_store] {reason} to {db.path} WITHOUT a backup "
                               f"(backup_before_migration is false)")
                db.rollup_backup_done = True
                return True
            layout = self._layout(conn)
            try:
                backup_database(conn, db.path, reason, covered=lambda c: self._layout(c) == layout)
            except Exception as e:
                logger.error(
                    f"[data_store] NOT adding the rollup tables to {db.path}: the pre-upgrade backup "
                    f"failed ({type(e).__name__}: {e}). History is stored and served as usual; it is "
                    f"rolled up once a retry of the backup succeeds (every "
                    f"{rollup_engine.INIT_RETRY_SECONDS}s). Free disk space / fix permissions, or set "
                    f"\"backup_before_migration\": false.")
                return False
            db.rollup_backup_done = True
            return True

    def _ensure_rollups(self, db: LockedConnection):
        if self.rollups is not None and not db.rollups_ready and not db.upgrade_blocked and \
                time.monotonic() - db.rollups_attempted >= rollup_engine.INIT_RETRY_SECONDS:
            db.group_barrier('the rollup setup')
            self._init_rollups(db)

    def _rollup_db(self, system_name: str) -> Optional[LockedConnection]:
        """The database of a system, ready for rollup work, or None (unknown, held back, rollups off)."""
        if self.rollups is None:
            return None
        db = self._get_db(system_name)
        if db is None or db.upgrade_blocked:
            return None
        self._ensure_rollups(db)
        return db if db.rollups_ready else None

    def rollups_complete(self, system_name: str, data_type: str) -> bool:
        """
        True when every stored row of this table is rolled up (nothing
        pending), or the table is not rolled up at all / rollups are off.
        False while a backfill range is pending or the rollup setup of the
        database has not succeeded yet.
        """
        if self.rollups is None:
            return True
        db = self._get_db(system_name)
        if db is None:
            return True
        with db.lock:
            tables = self._list_tables(db.conn)
        table = _readable_table(data_type, tables)
        if table is None or table not in rollup_engine.REGISTRY or table not in tables:
            return True
        if self._rollup_db(system_name) is None:
            return False
        with db.lock:
            try:
                state = db.conn.execute('SELECT 1 FROM _rollup_state WHERE tbl = ?', (table,)).fetchone()
            except sqlite3.OperationalError:
                return False
        return state is not None and not self.rollups.has_pending(db, [table])

    def rollups_at_series_cap(self, system_name: str, data_type: str) -> bool:
        """
        True when this table's rollups hold max_series_per_table series:
        fields / users beyond it are not rolled up, so their raw rows are
        all there is of them (retention keeps the table's rows).
        """
        if self.rollups is None:
            return False
        db = self._get_db(system_name)
        if db is None:
            return False
        with db.lock:
            table = _readable_table(data_type, self._list_tables(db.conn))
            if table not in rollup_engine.REGISTRY:
                return False
            try:
                series = db.conn.execute('SELECT COUNT(*) FROM _rollup_series WHERE data_type = ?',
                                         (table,)).fetchone()[0]
            except sqlite3.OperationalError:
                return False
        return series >= self.rollups.max_series

    def run_rollup_backfill(self, system_name: str, data_types: Optional[Sequence[str]] = None,
                            stop: Optional[threading.Event] = None,
                            deadline: Optional[float] = None) -> bool:
        """
        Roll up this system's pending ranges (all tables, or data_types) in
        bounded, duty-cycled batches; True when nothing is left.  Safe next
        to the Manager's own backfill thread and live ingest (also from
        another process).
        """
        if self.rollups is None:
            return True
        db = self._rollup_db(system_name)
        if db is None:
            return self._get_db(system_name) is None
        return self.rollups.run_backfill(db, data_types, stop=stop, deadline=deadline)

    def freeze_rollups(self, system_name: str, data_type: str, cutoff: str):
        """
        Retention: frozen_before = max(frozen_before, cutoff), so a row older
        than the prune cutoff (a replay of a pruned message) is never rolled
        up again.  Works whether or not rollups are enabled now.
        """
        db = self._get_db(system_name)
        if db is None:
            return
        with db.lock:
            table = _readable_table(data_type, self._list_tables(db.conn))
            if table is None or table not in rollup_engine.REGISTRY or \
                    '_rollup_state' not in self._list_tables(db.conn):
                return
        rollup_engine.RollupEngine.freeze(db, table, cutoff)

    def rebuild_rollups(self, system_name: str, data_type: str) -> Optional[dict]:
        """Delete and re-queue one table's rollups (rollups.py --rebuild); None if not possible."""
        db = self._rollup_db(system_name) if self.rollups is not None else None
        if db is None or data_type not in rollup_engine.REGISTRY:
            return None
        return self.rollups.rebuild(db, data_type)

    def rollup_status(self) -> Dict[str, Dict[str, dict]]:
        """
        {system directory: {table: {pending_ids, done_ids, frozen_before,
        series, complete, last_live_id}}} for every database with rollup
        state.  pending_ids / done_ids are id spans (ids retention deleted
        count too), not row counts.
        """
        out = {}
        for key in self.list_systems():
            try:
                db = self._get_db(key)
                if db is None:
                    continue
                self._ensure_rollups(db)
                status = rollup_engine.RollupEngine.status(db)
            except Exception as e:
                logger.error(f"[data_store] rollup status of {self._db_path(key)}: {e}")
                continue
            if status:
                out[key] = status
        return out

    def _check_writable(self, db: LockedConnection):
        """Raise MigrationBlockedError while a held-back migration is pending."""
        if not db.upgrade_blocked:
            return
        with db.lock:
            if db.upgrade_blocked and \
                    time.monotonic() - db.upgrade_attempted >= UPGRADE_RETRY_SECONDS:
                db.group_barrier('the held-back migration')
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

    @staticmethod
    def _ensure_breakdown_tables(conn):
        """
        storage_breakdown, NARROW: one msgs row per run (everything but the
        folder and device entries in meta_json) and one row per folder /
        device total, owner and "other" group.  ord (list position) and
        extra_json (keys the columns can't hold, see _sb_extra) let
        fetch_rows rebuild the message exactly as sent.
        """
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {_SB_MSGS} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id      TEXT UNIQUE,
                timestamp   TEXT NOT NULL,
                device_type TEXT DEFAULT 'unknown',
                meta_json   TEXT NOT NULL DEFAULT '{{}}'
            )
        ''')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{_SB_MSGS}_ts ON {_SB_MSGS} (timestamp)')
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {_SB_ROWS} (
                msg_row_id    INTEGER NOT NULL,
                scope         TEXT    NOT NULL,          -- 'folder' | 'device' | 'volume'
                path          TEXT    NOT NULL,          -- folder path, volume, or '' for device
                kind          TEXT    NOT NULL,          -- 'total' | 'owner' | 'other'
                owner         TEXT    NOT NULL DEFAULT '',
                bytes         INTEGER,
                files         INTEGER,
                cold_1y_bytes INTEGER,
                cold_3y_bytes INTEGER,
                owner_count   INTEGER,
                folder_owner  TEXT,
                ord           INTEGER,
                extra_json    TEXT,
                PRIMARY KEY (msg_row_id, scope, path, kind, owner)
            ) WITHOUT ROWID
        ''')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_sbr_owner ON {_SB_ROWS} (owner, msg_row_id)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_sbr_path ON {_SB_ROWS} (scope, path, msg_row_id)')

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
    # Wide → narrow storage_breakdown migration                            #
    # ------------------------------------------------------------------ #

    def _migrate_wide_storage_breakdown(self, db: LockedConnection):
        """
        Copy the rows of a "storage_breakdown" table an older Manager stored
        WIDE (one column per payload key, the folders / device entries as
        JSON text) into the narrow tables in one transaction, then rename it
        to storage_breakdown_legacy (kept, never dropped).  msg_id is kept
        (NULL when the table has none); a row whose entries can't be stored
        narrow keeps them as sent in meta_json, so every row stays readable.
        Other values stay as the older Manager stored them (a boolean as
        0 / 1, a number in a REAL column as a float).

        A second migration (after a downgrade the older Manager made the
        wide table again next to the narrow ones) skips the runs already
        stored narrow: the older Manager stores a run again when its ACK
        was lost or an archive is replayed.  The rule is store_message's:
        the same msg_id, or the same timestamp as a run stored before this
        copy without a msg_id (or, for a row without one, with any).  The
        skipped rows stay in the renamed table like every other.
        """
        with db.lock:
            tables = self._list_tables(db.conn)
            if STORAGE_BREAKDOWN not in tables:
                db.breakdown_migrated = True
                return

            cols = self._get_columns(db.conn, STORAGE_BREAKDOWN)
            data_cols = [c for c in cols if c.lower() not in _INTERNAL_LOWER]
            has_msg_id = any(c.lower() == 'msg_id' for c in cols)
            legacy_name, n = _SB_LEGACY, 1
            while legacy_name in tables:
                legacy_name = f'{_SB_LEGACY}_{n}'
                n += 1

            copied = kept = skipped = 0
            with db.transaction() as conn:
                if STORAGE_BREAKDOWN not in self._list_tables(conn):
                    db.breakdown_migrated = True
                    return
                self._ensure_breakdown_tables(conn)
                keep_ids = conn.execute(f'SELECT COUNT(*) FROM {_SB_MSGS}').fetchone()[0] == 0
                # The newest run stored before this copy (None: a first migration, nothing to skip)
                before = None if keep_ids else conn.execute(f'SELECT MAX(id) FROM {_SB_MSGS}').fetchone()[0]
                cursor = conn.execute(f'SELECT * FROM "{STORAGE_BREAKDOWN}" ORDER BY id')
                while True:
                    batch = cursor.fetchmany(100)
                    if not batch:
                        break
                    for row in batch:
                        row = dict(row)
                        if before is not None and self._breakdown_run_stored(conn, row, has_msg_id, before):
                            skipped += 1
                            continue
                        if not self._copy_wide_breakdown_row(conn, row, data_cols, has_msg_id, keep_ids):
                            kept += 1
                        copied += 1
                conn.execute(f'ALTER TABLE "{STORAGE_BREAKDOWN}" RENAME TO "{legacy_name}"')
                self._rename_inherited_indexes(conn, STORAGE_BREAKDOWN, legacy_name)

            db.breakdown_migrated = True
            logger.warning(
                f"[data_store] migrated {copied} storage_breakdown rows in {db.path} from the wide "
                f"layout to storage_breakdown_msgs/rows"
                + (f" ({kept} with entries kept as sent: not storable narrow)" if kept else '')
                + (f"; {skipped} already stored here (the same msg_id or timestamp) not copied again"
                   if skipped else '')
                + f"; old table kept as '{legacy_name}'"
            )

    @staticmethod
    def _breakdown_run_stored(conn, row: dict, has_msg_id: bool, before: int) -> bool:
        """
        Whether a wide storage_breakdown row is a run the narrow tables already
        hold (_migrate_wide_storage_breakdown): its msg_id is there, or a run
        stored before this copy (id <= before) has its timestamp and no
        msg_id (any msg_id when the row has none).
        """
        msg_id = row.get('msg_id') if has_msg_id else None
        if msg_id is not None and conn.execute(
                f'SELECT 1 FROM {_SB_MSGS} WHERE msg_id = ?', (msg_id,)).fetchone():
            return True
        raw_ts = row.get('timestamp')
        stamps = [raw_ts] if isinstance(raw_ts, str) else []
        try:
            stamps.append(canonical_timestamp(raw_ts))
        except ValueError:
            pass
        if not stamps:
            return False
        ph = ', '.join(['?'] * len(stamps))
        null_id = '' if msg_id is None else ' AND msg_id IS NULL'
        return conn.execute(
            f'SELECT 1 FROM {_SB_MSGS} INDEXED BY idx_{_SB_MSGS}_ts '
            f'WHERE timestamp IN ({ph}) AND id <= ?{null_id} LIMIT 1', stamps + [before]).fetchone() is not None

    @staticmethod
    def _rename_inherited_indexes(conn, old_table: str, new_table: str):
        """
        ALTER TABLE ... RENAME keeps a table's indexes under their names
        (idx_<old>_ts, idx_<old>_msg_id).  An older Manager that later makes
        a new wide <old> table (after a downgrade) would find those names
        taken: its CREATE INDEX IF NOT EXISTS does nothing and its
        "INDEXED BY idx_<old>_ts" fails, so every such message would be
        dead-lettered.  The renamed table's indexes are made again under its
        own name (idx_<new>_ts, ...); indexes hold no data.
        """
        prefix = f'idx_{old_table}_'
        for index in conn.execute(f'PRAGMA index_list("{new_table}")').fetchall():
            name, unique = index[1], index[2]
            if not name.startswith(prefix):
                continue
            cols = [(r[2], r[3]) for r in conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall() if r[5]]
            conn.execute(f'DROP INDEX "{name}"')
            if cols and all(col is not None for col, _ in cols):
                conn.execute(
                    f'CREATE {"UNIQUE " if unique else ""}INDEX IF NOT EXISTS '
                    f'"idx_{new_table}_{name[len(prefix):]}" ON "{new_table}" ('
                    + ', '.join(f'"{_quote_col(col)}"' + (' DESC' if desc else '') for col, desc in cols) + ')')

    def _copy_wide_breakdown_row(self, conn, row: dict, data_cols: List[str], has_msg_id: bool,
                                 keep_ids: bool) -> bool:
        """One wide row into the narrow tables; False if its entries had to stay in meta_json."""
        raw_ts = row.get('timestamp')
        try:
            ts = canonical_timestamp(raw_ts)
        except ValueError:
            ts = str(raw_ts)
        # Every column, as the older Manager's /api/data showed it (NULL and
        # '' included), with the JSON text of lists / objects decoded
        payload = {col: _decode_wide_value(row.get(col)) for col in data_cols
                   if not isinstance(row.get(col), bytes)}
        msg_id = row.get('msg_id') if has_msg_id else None
        meta, rows = _split_breakdown_payload(payload)
        if keep_ids:
            cur = conn.execute(
                f'INSERT INTO {_SB_MSGS} (id, msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?, ?)',
                (row['id'], msg_id, ts, row.get('device_type') or 'unknown', json.dumps(meta, allow_nan=False)))
        else:
            cur = conn.execute(
                f'INSERT INTO {_SB_MSGS} (msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?)',
                (msg_id, ts, row.get('device_type') or 'unknown', json.dumps(meta, allow_nan=False)))
        self._insert_breakdown_rows(conn, cur.lastrowid, rows)
        # a non-empty folders list / a device object left in meta could not be stored narrow
        return not ((payload.get('folders') and 'folders' in meta) or
                    (isinstance(payload.get('device'), dict) and 'device' in meta))

    @staticmethod
    def _insert_breakdown_rows(conn, row_id: int, rows: list):
        cols = ', '.join(('msg_row_id',) + _SB_COLUMNS)
        conn.executemany(f'INSERT INTO {_SB_ROWS} ({cols}) VALUES ({", ".join(["?"] * (1 + len(_SB_COLUMNS)))})',
                         [(row_id,) + r for r in rows])

    def _insert_storage_breakdown(self, conn, msg_id, timestamp, device_type, payload) -> int:
        """Insert one storage_breakdown run (storage_breakdown_msgs row + narrow rows); returns the msgs row id."""
        meta, rows = _split_breakdown_payload(payload)
        cur = conn.execute(
            f'INSERT INTO {_SB_MSGS} (msg_id, timestamp, device_type, meta_json) VALUES (?, ?, ?, ?)',
            (msg_id, timestamp, device_type, json.dumps(meta, allow_nan=False)))
        self._insert_breakdown_rows(conn, cur.lastrowid, rows)
        return cur.lastrowid

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
        elif table == STORAGE_BREAKDOWN:
            self._check_breakdown_writable(db)
        self._ensure_rollups(db)
        if db.rollups_ready and table in rollup_engine.REGISTRY and not db.rollup_backup_done \
                and not self._rollup_backup(db, adding=table):
            # This row would add the rollup tables to a database holding other
            # data, and its backup failed: store it without them (a 'gap' later)
            db.rollups_ready = False
            db.rollups_attempted = time.monotonic()

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
                elif table == STORAGE_BREAKDOWN:
                    if STORAGE_BREAKDOWN in self._list_tables(conn):
                        # Never add narrow rows next to an unmigrated wide table
                        db.breakdown_migrated = False
                        raise MigrationBlockedError(
                            f"{db.path}: the wide storage_breakdown table is not migrated yet")
                    self._ensure_breakdown_tables(conn)
                    if msg_id and conn.execute(
                            f'SELECT 1 FROM {_SB_MSGS} WHERE msg_id = ?', (msg_id,)).fetchone():
                        raise _Duplicate()
                    if self._has_pre_upgrade_row(conn, table, natural_ts, payload):
                        raise _Duplicate()
                    self._insert_storage_breakdown(conn, msg_id, timestamp, device_type, payload)
                else:
                    self._ensure_table(conn, table)
                    if msg_id and conn.execute(
                            f'SELECT 1 FROM "{table}" WHERE msg_id = ?', (msg_id,)).fetchone():
                        raise _Duplicate()
                    if self._has_pre_upgrade_row(conn, table, natural_ts, payload):
                        raise _Duplicate()
                    row_id = self._insert_wide(conn, table, msg_id, timestamp, device_type, payload)
                    if self.rollups is not None and db.rollups_ready:
                        # Same transaction: the row and its rollup commit together
                        self.rollups.on_insert(conn, table, row_id, db.path)
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
        if table == STORAGE_BREAKDOWN:
            # rows migrated from a wide table an older Manager wrote without msg_id
            return conn.execute(f'SELECT 1 FROM {_SB_MSGS} INDEXED BY idx_{_SB_MSGS}_ts '
                                f'WHERE timestamp IN ({ph}) AND msg_id IS NULL LIMIT 1',
                                params).fetchone() is not None

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

    def _insert_wide(self, conn, table, msg_id, timestamp, device_type, payload) -> int:
        """Insert one row into a wide table (adding columns as needed); returns its id."""
        existing = self._get_columns(conn, table)
        mapped = _prepare_wide_payload(payload, existing)
        self._evolve_schema(conn, table, existing, mapped)

        # Column names stored verbatim (quoted); values cleaned for SQLite
        cols   = ['msg_id', 'timestamp', 'device_type'] + list(mapped.keys())
        vals   = [msg_id,   timestamp,   device_type]   + list(mapped.values())
        ph     = ', '.join(['?'] * len(cols))
        cnames = ', '.join(f'"{_quote_col(c)}"' for c in cols)
        return conn.execute(f'INSERT INTO "{table}" ({cnames}) VALUES ({ph})', vals).lastrowid

    @staticmethod
    def _insert_folder_usage(conn, msg_id, timestamp, device_type, payload) -> int:
        """Insert one folder_usage run (folder_usage_msgs row + items); returns the msgs row id."""
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
        return row_id

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
            elif table == STORAGE_BREAKDOWN and _SB_MSGS in tables:
                rows = self._breakdown_rows(db, where, params, order, limit + 1)
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

    def _breakdown_rows(self, db: LockedConnection, where: str, params: list, order: str, limit: int,
                        folder: Optional[str] = None) -> List[dict]:
        """
        Narrow storage_breakdown runs rebuilt into the message as sent:
        {"id", "msg_id", "timestamp", "device_type", <meta keys>, "folders": [...],
        "device": {...}}.  folder: only that folder's entry (plus the device).
        """
        msgs = db.conn.execute(
            f'SELECT id, msg_id, timestamp, device_type, meta_json FROM {_SB_MSGS} '
            f'{where} ORDER BY timestamp {order}, id {order} LIMIT ?',
            params + [limit],
        ).fetchall()
        rows, entries = [], {}
        for m in msgs:
            row = {'id': m['id'], 'msg_id': m['msg_id'], 'timestamp': m['timestamp'], 'device_type': m['device_type']}
            try:
                meta = json.loads(m['meta_json'] or '{}')
            except ValueError:
                meta = {}
            if isinstance(meta, dict):
                row.update(meta)
            rows.append(row)
            entries[m['id']] = {}

        ids = list(entries)
        only = ' AND (scope = ? OR path = ?)' if folder is not None else ''
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ', '.join(['?'] * len(chunk))
            for r in db.conn.execute(f'SELECT * FROM {_SB_ROWS} WHERE msg_row_id IN ({ph}){only}',
                                     chunk + (['device', folder] if folder is not None else [])):
                if folder is not None and r['scope'] == 'folder' and r['path'] != folder:
                    continue
                e = entries[r['msg_row_id']].setdefault((r['scope'], r['path']), {'owners': []})
                if r['kind'] == 'owner':
                    e['owners'].append(r)
                else:
                    e[r['kind']] = r
        for row in rows:
            folders = []
            for (scope, path), e in entries[row['id']].items():
                if 'total' not in e:
                    continue
                entry = _sb_decode(e['total'], e['owners'], e.get('other'), scope)
                if scope == 'device':
                    row['device'] = entry
                elif scope == 'folder':
                    folders.append((e['total']['ord'], entry))
            if folders:
                row['folders'] = [entry for _, entry in sorted(folders, key=lambda x: x[0])]
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
                         max_points:  Optional[int] = None,
                         entity:      Optional[str] = None,
                         resolution:  str = 'auto') -> Optional[dict]:
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

        entity: only that user's rows (user_metrics; the table's
        rollups.REGISTRY entity column).  resolution 'auto' (default), 'raw',
        'hour' or 'day': a window query on a rolled-up field may be answered
        from its hourly or daily rollups, merged down to max_points
        (_plan_level).

        `field` is resolved as given, then with a leading '/' (volume paths
        lose it in URL routing).  A folder_usage field that is not a folder /
        volume key but a numeric metadata key (e.g. "_scan_seconds", which the
        old wide table kept as a column) is read from the runs' metadata.
        storage_breakdown fields are owner/<owner>/<metric> (the device
        rollup of one owner) and folder/<path>/<metric> (a folder's total),
        metric = bytes, files, cold_1y_bytes or cold_3y_bytes, or a numeric
        top-level key of the runs.

        Returns None if the system/table has no rows at all, else
        {"field", "unit", "points", "truncated", "downsampled", "resolution",
        "bucket_seconds", "downsampled_from", "entity"}.  A point is
        (timestamp, value), or (timestamp, average, min, max, count) when it
        aggregates several values (bucket average or rollup).
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
            elif table == STORAGE_BREAKDOWN and _SB_MSGS in tables:
                if conn.execute(f'SELECT 1 FROM {_SB_MSGS} LIMIT 1').fetchone() is None:
                    return None
                source = self._breakdown_source(conn, field)
                if source is None:
                    return self._empty_series(field, entity)
                src, ts_expr, val_expr, where, params, resolved, unit = source
            else:
                if table not in tables:
                    return None
                # A table retention emptied can still be charted from its rollups
                empty = conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None
                cols = self._get_columns(conn, table)
                by_lower = {c.lower(): c for c in cols}
                resolved = by_lower.get(field.lower())
                if resolved is None and ('/' + field).lower() in by_lower:
                    resolved = by_lower[('/' + field).lower()]
                    logger.info(f"Field resolved with leading slash: '{field}' -> '{resolved}'")
                if resolved is None or resolved.lower() in _INTERNAL_LOWER:
                    return None if empty else self._empty_series(field, entity)
                src = f'"{table}"'
                ts_expr, val_expr = 'timestamp', f'"{_quote_col(resolved)}"'
                where, params = [_numeric_filter(val_expr)], []
                if entity is not None:
                    spec = rollup_engine.REGISTRY.get(table) or {}
                    entity_col = by_lower.get(spec['entity']) if spec.get('entity') else None
                    if entity_col is None:
                        return None if empty else self._empty_series(field, entity)
                    where.append(f'"{_quote_col(entity_col)}" = ?')
                    params.append(entity)
                plan = self._plan_level(conn, table, resolved, entity, start, end,
                                        max_points or limit, resolution, where, params)
                if plan is None and empty:
                    return None
                unit = self._field_unit(conn, table, by_lower, resolved, plan[1] if plan else None)
                if plan is not None:
                    level, series, first, last, within = plan
                    mp = max_points or limit
                    pts = rollup_engine.read_series(conn, series['id'], level, first, last, mp, within)
                    return {'field': resolved, 'unit': unit,
                            'points': [(p['timestamp'], p['value'], p['min'], p['max'], p['count'])
                                       for p in pts if p['value'] is not None],
                            'truncated': False, 'downsampled': True, 'resolution': level,
                            'bucket_seconds': rollup_engine.merge_factor(level, first, last, mp)
                            * rollup_engine.LEVELS[level],
                            'downsampled_from': sum(p['count'] for p in pts), 'entity': entity}

            points, truncated, downsampled, info = self._series(
                conn, src, ts_expr, val_expr, where, params, start, end, limit, max_points)

        return {'field': resolved, 'unit': unit, 'points': points,
                'truncated': truncated, 'downsampled': downsampled, 'resolution': 'raw',
                'bucket_seconds': info.get('bucket_seconds'), 'downsampled_from': info.get('downsampled_from'),
                'entity': entity}

    @staticmethod
    def _empty_series(field: str, entity: Optional[str]) -> dict:
        """The answer for a field the table doesn't have (or can't chart)."""
        return {'field': field, 'unit': None, 'points': [], 'truncated': False, 'downsampled': False,
                'resolution': 'raw', 'bucket_seconds': None, 'downsampled_from': None, 'entity': entity}

    def _plan_level(self, conn, table: str, field: str, entity: Optional[str], start: Optional[str],
                    end: Optional[str], max_points: int, resolution: str, where: List[str], params: list):
        """
        Which rollup level answers a window query (spec §17), or None for
        the raw rows.  Raw for a tail query (no start), resolution 'raw',
        rollups off, a table or field that is not rolled up, nothing rolled
        up in the window, and user_metrics without entity (every user mixed,
        as before: the rollups hold one series per user).  'hour' / 'day'
        asked for: that level, if the table has it.  'auto':
          - raw while backfill ranges of the table are pending;
          - by the point width w = span / max_points, where span is the
            part of the window that holds values (from the series' first to
            its last value, not the requested from/to: an "All Available"
            query from 2000-01-01 is about the data there is): raw below
            3600 s, hourly below 86400 s, daily above;
          - where retention pruned raw rows of the window (rollup buckets
            before frozen_before), at least hourly;
          - but raw when the window holds at most max_points values: they
            are returned exactly, as before rollups existed;
          - a level the table has no rollups for (storage_health is daily
            only): raw if it covers the window, else the coarser level.
        where / params: the raw rows' value (and entity) conditions.
        Returns (level, series row, first, end, (oldest, newest)): the
        buckets to read are those over [first, end) epoch seconds, and the
        points' timestamps are kept within the series' oldest and newest
        value in the window (a bucket edge where the raw rows are pruned).
        """
        spec = rollup_engine.REGISTRY.get(table)
        if resolution == 'raw' or not start or not end or self.rollups is None or spec is None:
            return None
        if spec['entity'] and entity is None:
            return None
        series = rollup_engine.find_series(conn, table, field, entity or '')
        if series is None:
            return None
        try:
            lo, hi = parse_timestamp(start).timestamp(), parse_timestamp(end).timestamp()
        except ValueError:
            return None
        levels = sorted(spec['levels'], key=rollup_engine.LEVELS.get)
        width = rollup_engine.LEVELS[levels[0]]
        first_bucket, last_bucket, values = conn.execute(
            f'SELECT MIN(bucket), MAX(bucket), SUM(n) FROM _rollup_{levels[0]} WHERE series_id = ? '
            f'AND bucket >= ? AND bucket < ?', (series['id'], math.floor(lo / width) * width, hi)).fetchone()
        if first_bucket is None:
            return None
        # The series' first / last value in the window lies in its first /
        # last bucket (±1 s: SQLite rounds a timestamp to the millisecond
        # when it buckets it); none there = pruned, the bucket edge is used
        oldest = self._value_time(conn, table, where, params, start, end, first_bucket - 1,
                                  first_bucket + width + 1, 'ASC')
        newest = self._value_time(conn, table, where, params, start, end, last_bucket - 1,
                                  last_bucket + width + 1, 'DESC')
        first, last = max(lo, first_bucket), min(hi, last_bucket + width)
        within = (first if oldest is None else oldest, last if newest is None else newest)
        if resolution in rollup_engine.LEVELS:
            return (resolution, series, first, last, within) if resolution in levels else None

        state = conn.execute('SELECT frozen_before FROM _rollup_state WHERE tbl = ?', (table,)).fetchone()
        if state is None or conn.execute('SELECT 1 FROM _rollup_pending WHERE tbl = ? LIMIT 1',
                                         (table,)).fetchone() is not None:
            return None
        try:
            frozen = parse_timestamp(state['frozen_before']).timestamp() if state['frozen_before'] else None
        except ValueError:
            frozen = None
        raw_covers = frozen is None or lo >= frozen or first_bucket >= frozen
        w = (within[1] - within[0]) / max(1, max_points)
        level = None if w < 3600 else 'hour' if w < 86400 else 'day'
        if level is None and not raw_covers:
            level = 'hour'
        if level is not None and raw_covers and values <= max_points:
            return None          # at most max_points values (no more raw ones either): exact points
        if level is not None and level not in levels:
            level = None if raw_covers else levels[-1]
        return (level, series, first, last, within) if level is not None else None

    @staticmethod
    def _value_time(conn, table, where, params, start, end, lo_epoch, hi_epoch, order) -> Optional[float]:
        """Epoch of the oldest (ASC) / newest (DESC) raw value in [lo, hi) and in [start, end), or None."""
        lo_ts = max(start, format_timestamp(datetime.fromtimestamp(lo_epoch, timezone.utc)))
        hi_ts = min(end, format_timestamp(datetime.fromtimestamp(hi_epoch, timezone.utc)))
        row = conn.execute(f'SELECT timestamp FROM "{_quote_col(table)}" WHERE {" AND ".join(where)} '
                           f'AND timestamp >= ? AND timestamp < ? ORDER BY timestamp {order} LIMIT 1',
                           list(params) + [lo_ts, hi_ts]).fetchone()
        try:
            return parse_timestamp(row[0]).timestamp()
        except (TypeError, ValueError):
            return None

    def _field_unit(self, conn, table, by_lower, field, series=None) -> Optional[str]:
        """
        Unit of a wide-table field: the rollup registry's default unit
        (user_metrics carries no *_unit siblings), the rollup series' unit
        on a rollup answer, the newest row's <field>_unit / _unit, and for
        storage_health the unit its name ends in.  A raw answer never takes
        the series' unit: where the newest row has none, the unit stays null
        as before the upgrade.
        """
        default = ((rollup_engine.REGISTRY.get(table) or {}).get('units') or {}).get(field.lower())
        if default:
            return default
        if series is not None and series['unit']:
            return series['unit']
        return self._wide_unit(conn, table, by_lower, field) or _suffix_unit(table, field)

    def _breakdown_source(self, conn, field: str):
        """
        (src, timestamp, value, where, params, resolved field, unit) of a
        storage_breakdown history field, or None if it is none of the forms.
        A folder path that lost its leading '/' in URL routing is restored.
        """
        unit_of = self._breakdown_unit(conn)
        m = _SB_FIELD_RE.fullmatch(field)
        if m is not None:
            form, key, metric = m.groups()
            if form == 'owner':
                where, params = ["r.scope = 'device'", "r.path = ''", "r.kind = 'owner'", 'r.owner = ?'], [key]
            else:
                exists = f"SELECT 1 FROM {_SB_ROWS} INDEXED BY idx_sbr_path WHERE scope = 'folder' AND path = ? LIMIT 1"
                if conn.execute(exists, (key,)).fetchone() is None and \
                        conn.execute(exists, ('/' + key,)).fetchone() is not None:
                    logger.info(f"Field resolved with leading slash: '{key}' -> '/{key}'")
                    key = '/' + key
                where, params = ["r.scope = 'folder'", 'r.path = ?', "r.kind = 'total'"], [key]
            val = f'r.{metric}'
            return (f'{_SB_ROWS} r JOIN {_SB_MSGS} m ON m.id = r.msg_row_id', 'm.timestamp', val,
                    where + [_numeric_filter(val)], params, f'{form}/{key}/{metric}',
                    'count' if metric == 'files' else (unit_of.get('_unit') or 'bytes'))
        if self._folder_meta_key(conn, field, _SB_MSGS, slash=False) is None:
            return None
        val = f'lm_meta_number(m.meta_json, {_sql_literal(field)})'
        # the two-tier rule of every wide table (_wide_unit): {field}_unit, then the message's _unit
        unit = unit_of.get(f'{field}_unit') or unit_of.get('_unit')
        return f'{_SB_MSGS} m', 'm.timestamp', val, [_numeric_filter(val)], [], field, unit if isinstance(unit, str) else None

    @staticmethod
    def _breakdown_unit(conn) -> dict:
        """The newest storage_breakdown run's metadata ({} if unreadable), for its units."""
        row = conn.execute(f'SELECT meta_json FROM {_SB_MSGS} ORDER BY timestamp DESC LIMIT 1').fetchone()
        try:
            meta = json.loads(row[0]) if row else {}
        except ValueError:
            return {}
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _series(conn, src, ts, val, where, params, start, end, limit, max_points):
        """
        (points, truncated, downsampled, info) for get_field_series; a bucket
        average is (timestamp, average, min, max, count), and info then has
        its bucket_seconds and the number of values it averaged
        (downsampled_from).
        """
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
            return to_points(rows), truncated, False, {}

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
            return to_points(rows), truncated, False, {}

        width = (jmax - jmin) / max_points or 1.0
        rows = conn.execute(
            f'SELECT AVG(julianday({ts})) AS jd, AVG(CAST({val} AS REAL)) AS v, MIN(CAST({val} AS REAL)), '
            f'MAX(CAST({val} AS REAL)), COUNT(*) FROM {src} '
            f'WHERE {wsql} '
            f'GROUP BY MIN(CAST((julianday({ts}) - ?) / ? AS INTEGER), ?) ORDER BY jd',
            p + [jmin, width, max_points - 1]).fetchall()
        points = [(_jd_to_timestamp(jd), float(v), float(vmin), float(vmax), n)
                  for jd, v, vmin, vmax, n in rows if v is not None]
        return points, truncated, True, {'bucket_seconds': max(1, int(round(width * 86400))),
                                         'downsampled_from': count}

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
    def _folder_meta_key(conn, field, table=_FU_MSGS, slash=True) -> Optional[str]:
        """field (or '/' + field) if some run of `table` stores it as metadata, else None."""
        for key in (field, '/' + field) if slash else (field,):
            if conn.execute(f'SELECT 1 FROM {table} WHERE instr(meta_json, ?) > 0 LIMIT 1',
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
        if _SB_MSGS in tables:
            types.add(STORAGE_BREAKDOWN)
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
    # Reads for the newer endpoints (manager.py)                           #
    # ------------------------------------------------------------------ #

    def latest_storage_breakdown(self, system_name: str, folder: Optional[str] = None) -> Optional[dict]:
        """
        The newest storage_breakdown run of a system as sent (nested folders
        with their owner rows, device), only `folder`'s entry when given;
        None if the system has none.  A wide table not migrated yet is read
        with its JSON text decoded.
        """
        db = self._get_db(system_name)
        if db is None:
            return None
        with db.lock:
            tables = self._list_tables(db.conn)
            if _SB_MSGS in tables:
                rows = self._breakdown_rows(db, '', [], 'DESC', 1, folder=folder)
            elif STORAGE_BREAKDOWN in tables:
                rows = [{k: _decode_wide_value(v) for k, v in dict(r).items()} for r in db.conn.execute(
                    f'SELECT * FROM "{STORAGE_BREAKDOWN}" ORDER BY timestamp DESC, id DESC LIMIT 1')]
            else:
                return None
        if not rows:
            return None
        row = rows[0]
        if folder is not None:
            folders = row.get('folders')
            row['folders'] = [f for f in folders if isinstance(f, dict) and f.get('path') == folder] \
                if isinstance(folders, list) else []
        return row

    USAGE_SUMS = ('cpu_seconds', 'gpu_busy_seconds', 'gpu_held_seconds')
    USAGE_PEAKS = ('cpu_cores', 'gpu_vram_bytes')

    def user_usage(self, system_name: str, start: str, end: str) -> Optional[dict]:
        """
        Per-user totals of a system's user_metrics over [start, end) for
        /api/user_metrics/usage: {"source": "rollups" | "raw", "complete",
        "users": {username: {"samples", "sums": {field: total}, "peaks":
        {field: max}}}} over USAGE_SUMS / USAGE_PEAKS; a total or max over no
        measured value is None.  From the hourly rollups when they are
        complete (they include the hour `start` falls in), else from the raw
        rows; "complete" is rollups_complete().  None for an unknown system.
        """
        db = self._get_db(system_name)
        if db is None:
            return None
        complete = self.rollups_complete(system_name, 'user_metrics')
        with db.lock:
            conn = db.conn
            if 'user_metrics' not in self._list_tables(conn):
                return {'source': 'raw', 'complete': complete, 'users': {}}
            if self.rollups is not None and complete:
                return {'source': 'rollups', 'complete': True, 'users': self._usage_from_rollups(conn, start, end)}
            return {'source': 'raw', 'complete': complete, 'users': self._usage_from_raw(conn, start, end)}

    def _usage_from_rollups(self, conn, start: str, end: str) -> dict:
        lo, hi = parse_timestamp(start).timestamp(), parse_timestamp(end).timestamp()
        users: Dict[str, dict] = {}
        for entity, field, n, total, peak in conn.execute(
                'SELECT s.entity, s.field, SUM(h.n), TOTAL(h.vsum), MAX(h.vmax) FROM _rollup_series s '
                'JOIN _rollup_hour h ON h.series_id = s.id '
                "WHERE s.data_type = 'user_metrics' AND h.bucket >= ? AND h.bucket < ? GROUP BY s.id",
                (math.floor(lo / 3600) * 3600, hi)):
            if not entity:
                continue
            u = users.setdefault(entity, {'samples': 0, 'sums': dict.fromkeys(self.USAGE_SUMS),
                                          'peaks': dict.fromkeys(self.USAGE_PEAKS)})
            # a row counts in every series it has a number for
            u['samples'] = max(u['samples'], n)
            low = field.lower()
            if low in u['sums']:
                u['sums'][low] = total
            elif low in u['peaks']:
                u['peaks'][low] = peak
        return users

    def _usage_from_raw(self, conn, start: str, end: str) -> dict:
        by_lower = {c.lower(): c for c in self._get_columns(conn, 'user_metrics')}
        if 'username' not in by_lower:
            return {}

        def num(field):
            col = by_lower.get(field)
            if col is None:
                return 'NULL'
            q = f'"{_quote_col(col)}"'
            return f"CASE WHEN typeof({q}) IN ('integer', 'real') AND abs({q}) < 1e308 THEN {q} END"

        inner = ', '.join(f'{num(f)} AS x{i}' for i, f in enumerate(self.USAGE_SUMS + self.USAGE_PEAKS))
        sums = ', '.join(f'CASE WHEN COUNT(x{i}) > 0 THEN TOTAL(x{i}) END' for i in range(len(self.USAGE_SUMS)))
        peaks = ', '.join(f'MAX(x{len(self.USAGE_SUMS) + i})' for i in range(len(self.USAGE_PEAKS)))
        users = {}
        for row in conn.execute(
                f'SELECT u, COUNT(*), {sums}, {peaks} FROM (SELECT "{_quote_col(by_lower["username"])}" AS u, {inner} '
                f'FROM user_metrics WHERE timestamp >= ? AND timestamp < ?) WHERE u IS NOT NULL GROUP BY u',
                (start, end)):
            values = list(row)
            users[str(values[0])] = {
                'samples': values[1],
                'sums': dict(zip(self.USAGE_SUMS, values[2:2 + len(self.USAGE_SUMS)])),
                'peaks': dict(zip(self.USAGE_PEAKS, values[2 + len(self.USAGE_SUMS):])),
            }
        return users

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
        elif table == STORAGE_BREAKDOWN:
            self._check_breakdown_writable(db)
        deleted = 0
        with db.transaction() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ', '.join(['?'] * len(chunk))
                if table == FOLDER_USAGE:
                    conn.execute(f'DELETE FROM {_FU_ITEMS} WHERE msg_row_id IN ({ph})', chunk)
                    deleted += conn.execute(f'DELETE FROM {_FU_MSGS} WHERE id IN ({ph})', chunk).rowcount
                elif table == STORAGE_BREAKDOWN:
                    conn.execute(f'DELETE FROM {_SB_ROWS} WHERE msg_row_id IN ({ph})', chunk)
                    deleted += conn.execute(f'DELETE FROM {_SB_MSGS} WHERE id IN ({ph})', chunk).rowcount
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
