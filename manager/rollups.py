#!/usr/bin/env python3
r"""
Rollups — hourly and daily aggregates of the per-system history, kept forever.

Each system's data.db gets five tables, created once the database is fully
migrated and holds a table that is rolled up (CREATE ... IF NOT EXISTS:
additive, so no pre-upgrade backup is needed, and an empty or unrelated
database is left alone):

    _rollup_series   one row per (data_type, entity, field); entity is the
                     username for user_metrics, '' otherwise
    _rollup_hour     (series_id, bucket) -> n, vsum, vmin, vmax
                     bucket = UTC epoch seconds, a multiple of 3600
    _rollup_day      the same per UTC day (multiple of 86400)
    _rollup_state    per table: spec_version, last_live_id (newest row id
                     rolled up or queued), frozen_before (retention cutoff)
    _rollup_pending  id ranges (lo, hi] of a table still to be rolled up;
                     rows with id <= cursor are done

The "_" table prefix is reserved (data_store._RESERVED_PREFIXES, also in the
CURRENT Manager), so neither this Manager nor an older one after a downgrade
lists these tables as data types or treats them as old tables to migrate.

What is rolled up (REGISTRY): every REAL-declared column of system_metrics,
user_metrics (per username), sessions and storage_health (daily only),
except the internal columns, "*_unit", "payload_*", "_*" and a per-table
denylist; a value counts only when it is stored as a number (typeof integer
or real: NULL, NaN, text never do, so a gap never becomes 0; booleans are
0/1, so their average is the fraction of runs that were true).  The average
of a bucket is vsum / n.  Old rows are rolled up as stored: a system_metrics
or user_metrics row without interval_s holds the old 1-second cpu_percent
sample, and the rows without cpu_seconds / interval_s simply add nothing to
those series.

Buckets are computed in SQL only, (CAST(strftime('%s', timestamp) AS
INTEGER) / W) * W, the same expression on every path; a timestamp SQLite
can't parse gives no bucket and the row is skipped (counted in the log).

Write path (data_store.store_message): inside the message's transaction,
after the insert, on_insert() rolls up that one row under a SAVEPOINT.  A
storage error (is_transient_db_error) is re-raised, so the message gets
'error' and the collector retries; any other error rolls back to the
savepoint, queues the row as a 'repair' range and the raw row is still
stored: a rollup bug never costs valid data.

Backfill: the first open of a database queues (0, MAX(id)] per table
('initial'); every open queues rows stored without rollups since
('gap': rollups disabled, or an older Manager ran on the database after a
downgrade).  start_backfill_thread() works through the ranges in bounded
batches of backfill_batch_ids ids, one short write transaction each, and
sleeps elapsed * (1/backfill_max_duty - 1) between batches, so ingest never
waits for more than one batch.  The cursor is read and advanced inside the
batch's own transaction, so a restart (or a kill) resumes where the last
committed batch ended and nothing is counted twice; the archive CLI can run
batches at the same time.  Progress: GET /api/rollups/status, or
"python rollups.py --status".

Read side: find_series() and read_series() (hour or day points with
value = Σvsum/Σn, min, max, count; consecutive buckets merged down to
max_points) are what data_store.get_field_series' resolution planner and
/api/user_metrics/usage build on; data_store.rollups_complete() says when a
table's rollups cover all its raw rows.

Retention (archive_metrics.run_history_retention) prunes a rolled-up table
only once its rollups are complete, after setting frozen_before = the
cutoff: from then on a row older than frozen_before is never rolled up again
(a replay of a pruned message would otherwise be counted twice).

Usage (safe while the Manager is running):
    python rollups.py --config config.json --status
    python rollups.py --config config.json --rebuild [--system S] [--data-type T]

--status only reads (each data.db opened read-only): it never backs up,
migrates or creates anything, so it also works on a data_dir an older
Manager still runs on (databases without rollup tables are left out).
--rebuild deletes the rollups of the selected tables and rolls them up again
from the raw rows; when retention has pruned raw rows, the buckets before
frozen_before (rounded up to a UTC day) are kept as they are.  It is for
data_dirs this Manager version has already opened: it refuses (exit 1,
nothing changed) while a selected database still needs its one-time
migration.

Settings (manager config "rollups", every key optional):
    enabled               true   false: no rollups are written; rows stored
                                 meanwhile are queued ('gap') when re-enabled
    backfill_batch_ids    2000   ids per backfill transaction
    backfill_max_duty     0.5    fraction of the time the backfill may work
    max_series_per_table  5000   new series beyond this are skipped (logged once);
                                 retention does not prune a table at the cap
"""
import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage_util import format_timestamp, is_transient_db_error, parse_timestamp, utc_now  # noqa: E402

logger = logging.getLogger(__name__)

# Bumped when REGISTRY changes what a series means; a database rolled up
# under another version is rolled up again from its raw rows (if retention
# has not pruned them).  The old rollups are deleted inside the database's
# open transaction (at startup): on a large database that takes a while.
SPEC_VERSION = 1

LEVELS = {'hour': 3600, 'day': 86400}

# table -> entity column ('' series when None), levels, columns never rolled
# up, default units (used before any <field>_unit / _unit the rows carry).
REGISTRY = {
    'system_metrics': {'entity': None, 'levels': ('hour', 'day'), 'deny': ('cpu_count',), 'units': {}},
    'user_metrics': {
        'entity': 'username', 'levels': ('hour', 'day'), 'deny': ('interval_s',),
        'units': {'cpu_percent': '%', 'ram_bytes': 'bytes', 'cpu_seconds': 's', 'cpu_cores': 'cores',
                  'gpu_vram_bytes': 'bytes', 'gpu_util_percent': '%', 'gpu_busy_seconds': 's',
                  'gpu_held_seconds': 's'},
    },
    'sessions': {'entity': None, 'levels': ('hour', 'day'), 'deny': ('sessions_truncated',), 'units': {}},
    'storage_health': {'entity': None, 'levels': ('day',), 'deny': (), 'units': {}},
}
# Backfill order within a system
TABLE_ORDER = ('system_metrics', 'user_metrics', 'sessions', 'storage_health')

DEFAULTS = {
    'enabled': True,
    'backfill_batch_ids': 2000,
    'backfill_max_duty': 0.5,
    'max_series_per_table': 5000,
}

# data_store._INTERNAL_COLS (data_store imports this module, not the reverse)
_INTERNAL_LOWER = frozenset(('id', 'timestamp', 'device_type', 'msg_id'))
# Columns per grouped pass: SQLite allows 500 UNION ALL arms
# (SQLITE_MAX_COMPOUND_SELECT) and 2000 result columns (up to 5 per column here)
_COLUMNS_PER_STATEMENT = 300
# A failed init (database locked by another program, ...) is retried this often
INIT_RETRY_SECONDS = 60
# How often the backfill thread looks for new pending ranges when idle
IDLE_SECONDS = 60
CHECKPOINT_EVERY_BATCHES = 50
# A long backfill logs its progress this often
PROGRESS_SECONDS = 300

_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS _rollup_series (
        id        INTEGER PRIMARY KEY,
        data_type TEXT NOT NULL,
        entity    TEXT NOT NULL DEFAULT '',
        field     TEXT NOT NULL,
        unit      TEXT,
        UNIQUE (data_type, entity, field)
    )''',
    '''CREATE TABLE IF NOT EXISTS _rollup_hour (
        series_id INTEGER NOT NULL,
        bucket    INTEGER NOT NULL,
        n         INTEGER NOT NULL,
        vsum      REAL    NOT NULL,
        vmin      REAL    NOT NULL,
        vmax      REAL    NOT NULL,
        PRIMARY KEY (series_id, bucket)
    ) WITHOUT ROWID''',
    '''CREATE TABLE IF NOT EXISTS _rollup_day (
        series_id INTEGER NOT NULL,
        bucket    INTEGER NOT NULL,
        n         INTEGER NOT NULL,
        vsum      REAL    NOT NULL,
        vmin      REAL    NOT NULL,
        vmax      REAL    NOT NULL,
        PRIMARY KEY (series_id, bucket)
    ) WITHOUT ROWID''',
    '''CREATE TABLE IF NOT EXISTS _rollup_state (
        tbl           TEXT PRIMARY KEY,
        spec_version  INTEGER NOT NULL,
        last_live_id  INTEGER NOT NULL DEFAULT 0,
        frozen_before TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS _rollup_pending (
        tbl        TEXT    NOT NULL,
        lo         INTEGER NOT NULL,
        hi         INTEGER NOT NULL,
        cursor     INTEGER NOT NULL,
        reason     TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (tbl, lo)
    )''',
)


# One batch's grouped values, per connection (TEMP: never written to data.db)
_BATCH_TABLE = ('CREATE TEMP TABLE IF NOT EXISTS _rollup_batch (entity TEXT, field TEXT, b INTEGER, '
                'n INTEGER, vsum REAL, vmin REAL, vmax REAL, unit TEXT)')


# --------------------------------------------------------------------------- #
# Settings                                                                    #
# --------------------------------------------------------------------------- #

def _setting_ok(key: str, value) -> bool:
    if key == 'enabled':
        return isinstance(value, bool)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if key == 'backfill_max_duty':
        return math.isfinite(value) and 0 < value <= 1
    return isinstance(value, int) and 1 <= value <= 1000000


def settings_from_config(config: dict, log: Optional[logging.Logger] = None) -> dict:
    """
    The "rollups" settings with defaults filled in.  A bad value is logged
    (when log is given) and replaced by its default: it never stops the
    Manager from starting.
    """
    out = dict(DEFAULTS)
    raw = config.get('rollups')
    if raw is None:
        return out
    if not isinstance(raw, dict):
        if log:
            log.warning(f"Config 'rollups' must be an object; using the defaults {DEFAULTS}")
        return out
    for key, value in raw.items():
        if key not in DEFAULTS:
            if log:
                log.warning(f"Config 'rollups.{key}' is not a known setting (ignored); known: "
                            f"{', '.join(DEFAULTS)}")
        elif _setting_ok(key, value):
            out[key] = float(value) if key == 'backfill_max_duty' else value
        elif log:
            log.warning(f"Config 'rollups.{key}' = {value!r} is invalid; using the default "
                        f"{DEFAULTS[key]!r}")
    return out


# --------------------------------------------------------------------------- #
# Schema and SQL                                                              #
# --------------------------------------------------------------------------- #

def ensure_schema(conn):
    for sql in _SCHEMA:
        conn.execute(sql)


def _q(name: str) -> str:
    return name.replace('"', '""')


def _lit(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _bucket_sql(ts: str, width: int) -> str:
    return f"(CAST(strftime('%s', {ts}) AS INTEGER) / {width}) * {width}"


def rollable_columns(table: str, columns) -> List[str]:
    """
    Columns of a registered table that are rolled up: REAL-declared, not
    internal, not *_unit / payload_* / _*, not the entity column and not in
    the table's denylist.  columns: [(name, declared type)].
    """
    spec = REGISTRY[table]
    deny = {c.lower() for c in spec['deny']}
    entity = (spec['entity'] or '').lower()
    out = []
    for name, decl in columns:
        low = name.lower()
        if (decl or '').upper() != 'REAL' or low in _INTERNAL_LOWER or low in deny or low == entity:
            continue
        if low.endswith('_unit') or low.startswith(('payload_', '_')):
            continue
        out.append(name)
    return out


class _Plan:
    """
    The cached statements that roll up one table with one column set.

    A batch is grouped ONCE: the raw rows of the id range are aggregated per
    (entity, bucket of the table's finest level) with per-column COUNT /
    TOTAL / MIN / MAX of the numeric values, unpivoted into the connection's
    TEMP table _rollup_batch (a few rows per bucket and field), and the
    series, hour and day statements read that.  The day rows are summed
    from the hour groups, which is exact: a UTC day is 24 whole UTC hours.
    """

    def __init__(self, table: str, columns):
        spec = REGISTRY[table]
        by_lower = {name.lower(): name for name, _ in columns}
        entity_col = by_lower.get(spec['entity']) if spec['entity'] else None
        entity = f'COALESCE(CAST("{_q(entity_col)}" AS TEXT), \'\')' if entity_col else "''"
        levels = sorted(spec['levels'], key=LEVELS.get)
        width = LEVELS[levels[0]]
        self.columns = rollable_columns(table, columns)
        self.fill = []
        for i in range(0, len(self.columns), _COLUMNS_PER_STATEMENT):
            chunk = self.columns[i:i + _COLUMNS_PER_STATEMENT]
            xs, aggs, arms = [], [], []
            for j, col in enumerate(chunk):
                qc = _q(col)
                xs.append(f'CASE WHEN typeof("{qc}") IN (\'integer\', \'real\') AND abs("{qc}") < 1e308 '
                          f'THEN "{qc}" END AS x{j}')
                aggs.append(f'COUNT(x{j}) AS n{j}, TOTAL(x{j}) AS s{j}, MIN(x{j}) AS lo{j}, MAX(x{j}) AS hi{j}')
                unit = self._unit(col, by_lower, spec['units'].get(col.lower()))
                if unit.startswith(('NULLIF', 'COALESCE')):
                    # Taken from the rows: aggregated with the values
                    xs.append(f'{unit} AS u{j}')
                    aggs.append(f'MAX(u{j}) AS u{j}')
                    unit = f'u{j}'
                arms.append(f'SELECT entity, {_lit(col)}, b, n{j}, s{j}, lo{j}, hi{j}, {unit} FROM g WHERE n{j} > 0')
            self.fill.append(
                f'INSERT INTO temp._rollup_batch (entity, field, b, n, vsum, vmin, vmax, unit) '
                f'WITH g AS (SELECT entity, b, {", ".join(aggs)} FROM ('
                f'SELECT {entity} AS entity, {_bucket_sql("timestamp", width)} AS b, {", ".join(xs)} '
                f'FROM "{_q(table)}" WHERE id > :lo AND id <= :hi AND (:frozen IS NULL OR timestamp >= :frozen)'
                f') WHERE b IS NOT NULL GROUP BY entity, b) '
                + '\nUNION ALL\n'.join(arms))
        join = ('FROM temp._rollup_batch r JOIN _rollup_series s '
                'ON s.data_type = :tbl AND s.entity = r.entity AND s.field = r.field WHERE r.n > 0')
        upsert = ('ON CONFLICT (series_id, bucket) DO UPDATE SET n = n + excluded.n, vsum = vsum + excluded.vsum, '
                  'vmin = min(vmin, excluded.vmin), vmax = max(vmax, excluded.vmax)')
        self.levels = []
        for level in levels:
            w = LEVELS[level]
            if w == width:
                self.levels.append(f'INSERT INTO _rollup_{level} (series_id, bucket, n, vsum, vmin, vmax) '
                                   f'SELECT s.id, r.b, r.n, r.vsum, r.vmin, r.vmax {join} {upsert}')
            else:
                self.levels.append(f'INSERT INTO _rollup_{level} (series_id, bucket, n, vsum, vmin, vmax) '
                                   f'SELECT s.id, (r.b / {w}) * {w} AS d, SUM(r.n), TOTAL(r.vsum), MIN(r.vmin), '
                                   f'MAX(r.vmax) {join} GROUP BY s.id, d {upsert}')
        new = ('FROM temp._rollup_batch r WHERE NOT EXISTS (SELECT 1 FROM _rollup_series s '
               'WHERE s.data_type = :tbl AND s.entity = r.entity AND s.field = r.field)')
        self.series = (f'INSERT OR IGNORE INTO _rollup_series (data_type, entity, field, unit) '
                       f'SELECT :tbl, entity, field, MAX(unit) {new} GROUP BY entity, field LIMIT :room')
        self.missing = f'SELECT 1 {new} LIMIT 1'
        self.unparsed = (f'SELECT COUNT(*) FROM "{_q(table)}" WHERE id > :lo AND id <= :hi '
                         f'AND strftime(\'%s\', timestamp) IS NULL')

    @staticmethod
    def _unit(col, by_lower, default_unit) -> str:
        """SQL for a field's unit: the registry default, else <field>_unit, else _unit, else NULL."""
        if default_unit:
            return _lit(default_unit)
        units = [f'NULLIF(CAST("{_q(by_lower[c])}" AS TEXT), \'\')'
                 for c in (f'{col}_unit'.lower(), '_unit') if c in by_lower]
        if not units:
            return 'NULL'
        return units[0] if len(units) == 1 else f'COALESCE({", ".join(units)})'


# --------------------------------------------------------------------------- #
# Engine                                                                      #
# --------------------------------------------------------------------------- #

class RollupEngine:
    """
    Rolls up rows of the registered tables of one or more per-system
    databases (data_store.TypedDataStore holds one engine for all of them).
    Every method that takes `conn` runs inside the caller's transaction; the
    ones that take `db` (a storage_util.LockedConnection) open their own.
    """

    def __init__(self, settings: Optional[dict] = None):
        s = dict(DEFAULTS)
        s.update(settings or {})
        self.batch_ids = int(s['backfill_batch_ids'])
        self.max_duty = float(s['backfill_max_duty'])
        self.max_series = int(s['max_series_per_table'])
        self._plans: Dict[tuple, _Plan] = {}
        self._logged: Dict[tuple, float] = {}
        self._lock = threading.Lock()
        # One backfill loop per database at a time in this process (the
        # background thread and a retention run), so the duty cycle holds
        self._runs: Dict[str, threading.Lock] = {}

    # ---- helpers ---- #

    def _log_limited(self, key, level, msg, every=3600.0, exc_info=False):
        """Log msg at most once per `every` seconds for key (None = once per process)."""
        now = time.monotonic()
        with self._lock:
            last = self._logged.get(key)
            if last is not None and (every is None or now - last < every):
                return
            self._logged[key] = now
        logger.log(level, msg, exc_info=exc_info)

    def _plan(self, conn, table: str) -> _Plan:
        columns = tuple((r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{_q(table)}")'))
        key = (table, columns)
        with self._lock:
            plan = self._plans.get(key)
        if plan is None:
            plan = _Plan(table, columns)
            with self._lock:
                if len(self._plans) > 256:
                    self._plans.clear()
                self._plans[key] = plan
        return plan

    @staticmethod
    def _state(conn, table: str):
        return conn.execute('SELECT spec_version, last_live_id, frozen_before FROM _rollup_state '
                            'WHERE tbl = ?', (table,)).fetchone()

    @staticmethod
    def _add_pending(conn, table: str, lo: int, hi: int, reason: str):
        """Queue the ids (lo, hi]; ranges never overlap, one starting at the same lo is extended."""
        if hi <= lo:
            return
        conn.execute(
            'INSERT INTO _rollup_pending (tbl, lo, hi, cursor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (tbl, lo) DO UPDATE SET hi = max(hi, excluded.hi)',
            (table, lo, hi, lo, reason, utc_now()))

    @staticmethod
    def _table_exists(conn, table: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                            (table,)).fetchone() is not None

    # ---- rolling up ---- #

    def roll_range(self, conn, table: str, lo: int, hi: int, frozen_before: Optional[str] = None,
                   path: str = '') -> None:
        """
        Add the rows lo < id <= hi of `table` (timestamp >= frozen_before,
        when set) to its hour and day rollups: one grouped pass over the rows
        (per 300 columns), then one statement for new series and one per
        level.  Never call it twice for the same rows: the ranges in
        _rollup_pending and last_live_id are what keep that from happening.
        """
        if table not in REGISTRY or hi <= lo:
            return
        plan = self._plan(conn, table)
        if not plan.fill:
            return
        params = {'tbl': table, 'lo': lo, 'hi': hi, 'frozen': frozen_before}
        conn.execute(_BATCH_TABLE)
        conn.execute('DELETE FROM temp._rollup_batch')
        for sql in plan.fill:
            conn.execute(sql, params)
        room = self.max_series - conn.execute(
            'SELECT COUNT(*) FROM _rollup_series WHERE data_type = ?', (table,)).fetchone()[0]
        if room > 0:
            room -= conn.execute(plan.series, {'tbl': table, 'room': room}).rowcount
        if room <= 0 and (path, table, 'cap') not in self._logged and \
                conn.execute(plan.missing, {'tbl': table}).fetchone() is not None:
            self._log_limited(
                (path, table, 'cap'), logging.WARNING,
                f"[rollups] {path}: {table} has reached max_series_per_table "
                f"({self.max_series}); new fields/users are not rolled up (raw history is "
                f"unaffected: history retention keeps this table's rows meanwhile). Raise "
                f"rollups.max_series_per_table and run 'rollups.py --rebuild' to include them.",
                every=None)
        for sql in plan.levels:
            conn.execute(sql, {'tbl': table})

    def on_insert(self, conn, table: str, row_id: int, path: str = '') -> None:
        """
        Roll up the row just inserted (inside store_message's transaction).
        Transient storage errors propagate; anything else queues the row as
        a 'repair' range and returns, so the raw row is still stored.
        """
        if table not in REGISTRY or not row_id:
            return
        conn.execute('SAVEPOINT rollup')
        try:
            self._roll_live(conn, table, row_id, path)
        except Exception as e:
            if is_transient_db_error(e):
                self._rollback_to(conn, release=True)
                raise
            self._rollback_to(conn)
            self._log_limited(
                (path, table, 'live'), logging.ERROR,
                f"[rollups] {path}: rolling up {table} row {row_id} failed "
                f"({type(e).__name__}: {e}); the row is stored and queued for the backfill "
                f"(logged at most once an hour)", exc_info=True)
            try:
                ensure_schema(conn)
                self._queue_live(conn, table, row_id, 'repair')
            except Exception as e2:
                if is_transient_db_error(e2):
                    self._rollback_to(conn, release=True)
                    raise
                # Found as a 'gap' (MAX(id) > last_live_id) the next time the
                # database is opened
                self._rollback_to(conn)
                self._log_limited((path, table, 'queue'), logging.ERROR,
                                  f"[rollups] {path}: could not queue {table} row {row_id} "
                                  f"for repair ({type(e2).__name__}: {e2})")
        conn.execute('RELEASE rollup')

    @staticmethod
    def _rollback_to(conn, release: bool = False):
        """Undo the rollup savepoint (best effort when the error is re-raised anyway)."""
        try:
            conn.execute('ROLLBACK TO rollup')
            if release:
                conn.execute('RELEASE rollup')
        except sqlite3.Error:
            if not release:
                raise

    def _queue_live(self, conn, table: str, row_id: int, reason: str):
        """Make sure a state row exists and every id up to row_id is rolled up or queued."""
        state = self._state(conn, table)
        if state is None:
            conn.execute('INSERT INTO _rollup_state (tbl, spec_version, last_live_id) VALUES (?, ?, 0)',
                         (table, SPEC_VERSION))
            last = 0
        else:
            last = state['last_live_id']
        if reason == 'repair':
            self._add_pending(conn, table, last, row_id, 'repair' if last >= row_id - 1 else 'gap')
        else:
            self._add_pending(conn, table, last, row_id - 1, reason)
        conn.execute('UPDATE _rollup_state SET last_live_id = max(last_live_id, ?) WHERE tbl = ?',
                     (row_id, table))
        return state

    def _roll_live(self, conn, table: str, row_id: int, path: str):
        try:
            state = self._state(conn, table)
        except sqlite3.OperationalError as e:
            if 'no such table' not in str(e).lower():
                raise
            ensure_schema(conn)          # first registered table of a new database
            state = None
        if state is None or state['last_live_id'] < row_id - 1:
            # Rows (last_live_id, row_id) were stored without rollups
            state = self._queue_live(conn, table, row_id, 'gap')
            frozen = state['frozen_before'] if state is not None else None
        else:
            frozen = state['frozen_before']
            conn.execute('UPDATE _rollup_state SET last_live_id = ? WHERE tbl = ? AND last_live_id < ?',
                         (row_id, table, row_id))
        self.roll_range(conn, table, row_id - 1, row_id, frozen, path)

    # ---- opening a database ---- #

    def init_db(self, db) -> None:
        """
        One BEGIN IMMEDIATE per database open: create the tables (only when
        the database has a registered table), queue a new table's existing
        rows ('initial'), rows stored without rollups since the last open
        ('gap'), and a re-roll after a SPEC_VERSION change.
        """
        with db.transaction() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            present = [t for t in TABLE_ORDER if t in tables]
            if not present:
                return
            ensure_schema(conn)
            for table in present:
                self._open_table(conn, db.path, table)

    def _open_table(self, conn, path: str, table: str):
        max_id = conn.execute(f'SELECT MAX(id) FROM "{_q(table)}"').fetchone()[0] or 0
        state = self._state(conn, table)
        if state is None:
            conn.execute('INSERT INTO _rollup_state (tbl, spec_version, last_live_id) VALUES (?, ?, ?)',
                         (table, SPEC_VERSION, max_id))
            if max_id:
                self._add_pending(conn, table, 0, max_id, 'initial')
                logger.info(f"[rollups] {path}: {table} ids 1..{max_id} queued for the rollup "
                            f"backfill (runs in the background)")
            return
        last = state['last_live_id']
        if state['spec_version'] != SPEC_VERSION:
            if state['frozen_before'] is None:
                self._delete_series(conn, table)
                conn.execute('DELETE FROM _rollup_pending WHERE tbl = ?', (table,))
                self._add_pending(conn, table, 0, last, 'spec')
                logger.warning(f"[rollups] {path}: {table} was rolled up by rollup spec "
                               f"v{state['spec_version']}; rolling it up again (v{SPEC_VERSION})")
            else:
                logger.warning(f"[rollups] {path}: {table} was rolled up by rollup spec "
                               f"v{state['spec_version']} and raw rows before "
                               f"{state['frozen_before']} are pruned: existing rollups are kept, "
                               f"new rows follow v{SPEC_VERSION}")
            conn.execute('UPDATE _rollup_state SET spec_version = ? WHERE tbl = ?', (SPEC_VERSION, table))
        if max_id > last:
            self._add_pending(conn, table, last, max_id, 'gap')
            conn.execute('UPDATE _rollup_state SET last_live_id = ? WHERE tbl = ?', (max_id, table))
            logger.warning(f"[rollups] {path}: {table} ids {last + 1}..{max_id} were stored without "
                           f"rollups (rollups disabled, or an older Manager used this database); "
                           f"queued for the backfill")

    @staticmethod
    def _delete_series(conn, table: str, from_bucket: Optional[Dict[str, int]] = None):
        """Delete a table's rollups (all, or per level the buckets >= from_bucket[level])."""
        sids = 'SELECT id FROM _rollup_series WHERE data_type = ?'
        for level in LEVELS:
            if from_bucket is None:
                conn.execute(f'DELETE FROM _rollup_{level} WHERE series_id IN ({sids})', (table,))
            else:
                conn.execute(f'DELETE FROM _rollup_{level} WHERE series_id IN ({sids}) AND bucket >= ?',
                             (table, from_bucket[level]))
        if from_bucket is None:
            conn.execute('DELETE FROM _rollup_series WHERE data_type = ?', (table,))

    # ---- backfill ---- #

    def backfill_step(self, db, table: str) -> Optional[int]:
        """
        One batch of the oldest pending range of `table`, in one write
        transaction.  Returns the number of ids it advanced, or None when
        nothing is pending.
        """
        with db.transaction() as conn:
            try:
                row = conn.execute('SELECT lo, hi, cursor FROM _rollup_pending WHERE tbl = ? '
                                   'ORDER BY lo LIMIT 1', (table,)).fetchone()
            except sqlite3.OperationalError as e:
                if 'no such table' in str(e).lower():
                    return None
                raise
            if row is None:
                return None
            lo, hi, cur = row['lo'], row['hi'], row['cursor']
            end = hi
            if self._table_exists(conn, table):
                # Skip ids retention already deleted in one step
                nxt = conn.execute(f'SELECT MIN(id) FROM "{_q(table)}" WHERE id > ?', (cur,)).fetchone()[0]
                if nxt is not None and nxt <= hi:
                    start = max(cur, nxt - 1)
                    end = min(hi, start + self.batch_ids)
                    state = self._state(conn, table)
                    frozen = state['frozen_before'] if state is not None else None
                    plan = self._plan(conn, table)
                    unparsed = conn.execute(plan.unparsed, {'lo': start, 'hi': end}).fetchone()[0]
                    if unparsed:
                        logger.warning(f"[rollups] {db.path}: {table}: {unparsed} row(s) with ids "
                                       f"{start + 1}..{end} have a timestamp SQLite can't parse; "
                                       f"not rolled up")
                    self.roll_range(conn, table, start, end, frozen, db.path)
            if end >= hi:
                conn.execute('DELETE FROM _rollup_pending WHERE tbl = ? AND lo = ?', (table, lo))
            else:
                conn.execute('UPDATE _rollup_pending SET cursor = ? WHERE tbl = ? AND lo = ?', (end, table, lo))
            return end - cur

    def has_pending(self, db, tables=None) -> bool:
        tables = list(tables or TABLE_ORDER)
        ph = ', '.join('?' * len(tables))
        with db.lock:
            try:
                return db.conn.execute(f'SELECT 1 FROM _rollup_pending WHERE tbl IN ({ph}) LIMIT 1',
                                       tables).fetchone() is not None
            except sqlite3.OperationalError as e:
                if 'no such table' in str(e).lower():
                    return False
                raise

    def run_backfill(self, db, tables=None, stop: Optional[threading.Event] = None,
                     deadline: Optional[float] = None) -> bool:
        """
        Batches until nothing is pending for `tables` (default: all, in
        TABLE_ORDER), `stop` is set or time.monotonic() passes `deadline`.
        Sleeps elapsed * (1/backfill_max_duty - 1) after each batch.  A
        second caller for the same database (in this process) waits for the
        first to finish.  Returns True when complete.
        """
        with self._lock:
            run_lock = self._runs.setdefault(db.path, threading.Lock())
        while not run_lock.acquire(timeout=1.0):
            if (stop is not None and stop.is_set()) or (deadline is not None and time.monotonic() >= deadline):
                return False
        try:
            return self._run_backfill(db, tables, stop, deadline)
        finally:
            run_lock.release()

    def _run_backfill(self, db, tables, stop, deadline) -> bool:
        tables = [t for t in TABLE_ORDER if tables is None or t in tables]
        idle = 1.0 / self.max_duty - 1.0
        batches, ids, started = 0, 0, time.monotonic()
        reported = started
        while True:
            if time.monotonic() - reported >= PROGRESS_SECONDS:
                reported = time.monotonic()
                left = {t: v['pending_ids'] for t, v in self.status(db).items() if v['pending_ids']}
                logger.info(f"[rollups] {db.path}: backfill running for {reported - started:.0f}s, "
                            f"{ids} ids done, still pending: {left}")
            for table in tables:
                t0 = time.monotonic()
                n = self.backfill_step(db, table)
                if n is not None:
                    break
            else:
                if batches:
                    logger.info(f"[rollups] {db.path}: backfill complete ({ids} ids in {batches} "
                                f"batches, {time.monotonic() - started:.1f}s)")
                return True
            batches += 1
            ids += n
            elapsed = time.monotonic() - t0
            if batches % CHECKPOINT_EVERY_BATCHES == 0:
                try:
                    db.execute('PRAGMA wal_checkpoint(PASSIVE)')
                except sqlite3.Error:
                    pass
            pause = elapsed * idle
            if stop is not None:
                if stop.wait(pause):
                    return False
            elif pause > 0:
                time.sleep(pause)
            if deadline is not None and time.monotonic() >= deadline:
                return False

    # ---- retention / rebuild ---- #

    @staticmethod
    def freeze(db, table: str, cutoff: str) -> None:
        """frozen_before = max(frozen_before, cutoff) for a table with rollup state."""
        with db.transaction() as conn:
            try:
                conn.execute('UPDATE _rollup_state SET frozen_before = ? WHERE tbl = ? AND '
                             '(frozen_before IS NULL OR frozen_before < ?)', (cutoff, table, cutoff))
            except sqlite3.OperationalError as e:
                if 'no such table' not in str(e).lower():
                    raise

    def rebuild(self, db, table: str) -> dict:
        """
        Delete `table`'s rollups and queue all its rows again, in one
        transaction.  When retention has pruned raw rows (frozen_before
        set), the buckets before frozen_before rounded up to a UTC day are
        kept and frozen_before is raised to that day.
        """
        with db.transaction() as conn:
            if not self._table_exists(conn, table):
                return {'table': table, 'queued_ids': 0}
            ensure_schema(conn)
            max_id = conn.execute(f'SELECT MAX(id) FROM "{_q(table)}"').fetchone()[0] or 0
            state = self._state(conn, table)
            frozen = state['frozen_before'] if state is not None else None
            kept_before = None
            if frozen is None:
                self._delete_series(conn, table)
            else:
                cut = _ceil_epoch(frozen, LEVELS['day'])
                kept_before = format_timestamp(datetime.fromtimestamp(cut, timezone.utc))
                self._delete_series(conn, table, {level: cut for level in LEVELS})
                frozen = kept_before
            conn.execute('DELETE FROM _rollup_pending WHERE tbl = ?', (table,))
            last = max(max_id, state['last_live_id'] if state is not None else 0)
            conn.execute(
                'INSERT INTO _rollup_state (tbl, spec_version, last_live_id, frozen_before) VALUES (?, ?, ?, ?) '
                'ON CONFLICT (tbl) DO UPDATE SET spec_version = excluded.spec_version, '
                'last_live_id = excluded.last_live_id, frozen_before = excluded.frozen_before',
                (table, SPEC_VERSION, last, frozen))
            self._add_pending(conn, table, 0, last, 'rebuild')
        return {'table': table, 'queued_ids': last, 'kept_buckets_before': kept_before}

    # ---- reporting ---- #

    @staticmethod
    def status(db) -> Dict[str, dict]:
        """{table: {pending_ids, done_ids, frozen_before, series, complete, last_live_id}} (id spans)."""
        with db.lock:
            return table_status(db.conn)


def table_status(conn) -> Dict[str, dict]:
    """RollupEngine.status() of one open database connection (only reads; {} without rollup tables)."""
    out = {}
    try:
        states = conn.execute('SELECT tbl, last_live_id, frozen_before FROM _rollup_state').fetchall()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e).lower():
            return out
        raise
    for s in states:
        table = s['tbl']
        pending = conn.execute('SELECT COALESCE(SUM(hi - cursor), 0), COUNT(*) FROM _rollup_pending '
                               'WHERE tbl = ?', (table,)).fetchone()
        series = conn.execute('SELECT COUNT(*) FROM _rollup_series WHERE data_type = ?',
                              (table,)).fetchone()[0]
        out[table] = {
            'pending_ids': pending[0],
            'done_ids': max(0, s['last_live_id'] - pending[0]),
            'frozen_before': s['frozen_before'],
            'series': series,
            'complete': pending[1] == 0,
            'last_live_id': s['last_live_id'],
        }
    return out


def _open_read_only(path: str) -> sqlite3.Connection:
    """
    A connection that can't change the database (mode=ro): no backup,
    migration or table is ever made through it.  SQLite may still create
    the -wal / -shm side files of a WAL database to read it.
    """
    conn = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _read_only_status(store) -> Dict[str, Dict[str, dict]]:
    """data_store.rollup_status() read with read-only connections (rollups.py --status)."""
    out = {}
    for system in store.list_systems():
        try:
            conn = _open_read_only(store._db_path(system))
            try:
                status = table_status(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error(f"[rollups] could not read {store._db_path(system)}: {e}")
            continue
        if status:
            out[system] = status
    return out


def _needs_migration(store, systems) -> Dict[str, str]:
    """{system: why} for databases this Manager has not migrated yet (read-only check)."""
    out = {}
    for system in systems:
        try:
            conn = _open_read_only(store._db_path(system))
            try:
                reasons = store._upgrade_reasons(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            reasons = [f'unreadable: {e}']
        if reasons:
            out[system] = '; '.join(reasons)
    return out


def _ceil_epoch(timestamp: str, width: int) -> int:
    epoch = parse_timestamp(timestamp).timestamp()
    return int(math.ceil(epoch / width) * width)


# --------------------------------------------------------------------------- #
# Read side (used by data_store.get_field_series / the usage endpoint)        #
# --------------------------------------------------------------------------- #

def find_series(conn, table: str, field: str, entity: str = '') -> Optional[sqlite3.Row]:
    """The _rollup_series row (id, field, unit) for a field (case-insensitive), or None."""
    try:
        return conn.execute('SELECT id, field, unit FROM _rollup_series WHERE data_type = ? AND entity = ? '
                            'AND field = ? COLLATE NOCASE ORDER BY field = ? DESC LIMIT 1',
                            (table, entity or '', field, field)).fetchone()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e).lower():
            return None
        raise


def merge_factor(level: str, start_epoch: float, end_epoch: float, max_points: int) -> int:
    """How many consecutive buckets read_series merges into one point (k; a point covers k * width s)."""
    width = LEVELS[level]
    lo = int(math.floor(start_epoch / width) * width)
    n_buckets = max(1, int(math.ceil((end_epoch - lo) / width)))
    return max(1, int(math.ceil(n_buckets / max(1, int(max_points)))))


def read_series(conn, series_id: int, level: str, start_epoch: float, end_epoch: float,
                max_points: int, within: Optional[tuple] = None) -> List[dict]:
    """
    Points of one series at `level` over [start, end): consecutive buckets
    are merged k at a time (k = ceil(buckets / max_points)), so at most
    max_points points come back, oldest first, each
    {"timestamp", "value" (= Σvsum/Σn), "min", "max", "count" (= Σn)}.
    The bucket holding `start` is included (partial first bucket).  The
    timestamp is the n-weighted bucket centre, kept within `within`
    (oldest, newest epoch: the series' first and last value in the
    window), default [start, end]: a centre can lie before the first or
    after the last value of a partly filled bucket.
    """
    width = LEVELS[level]
    lo = int(math.floor(start_epoch / width) * width)
    k = merge_factor(level, start_epoch, end_epoch, max_points)
    t_lo, t_hi = within if within is not None else (start_epoch, end_epoch)
    rows = conn.execute(
        f'SELECT SUM(n * (bucket + {width / 2.0})) / SUM(n), SUM(vsum) / SUM(n), MIN(vmin), MAX(vmax), SUM(n) '
        f'FROM _rollup_{level} WHERE series_id = ? AND bucket >= ? AND bucket < ? '
        f'GROUP BY (bucket - ?) / ? ORDER BY 1',
        (series_id, lo, end_epoch, lo, k * width)).fetchall()
    out = []
    for centre, value, vmin, vmax, n in rows:
        centre = min(max(centre, t_lo), t_hi)
        out.append({'timestamp': format_timestamp(datetime.fromtimestamp(centre, timezone.utc)),
                    'value': value, 'min': vmin, 'max': vmax, 'count': n})
    return out


# --------------------------------------------------------------------------- #
# Background backfill                                                         #
# --------------------------------------------------------------------------- #

class BackfillThread:
    """
    Works through the pending ranges of every per-system database, system by
    system (system_metrics, then user_metrics, then the rest), in bounded,
    duty-cycled batches.  Never stops the Manager: errors are logged (at
    most once an hour per database) and retried on the next pass.
    """

    def __init__(self, data_store, log: Optional[logging.Logger] = None):
        self.data_store = data_store
        self.log = log or logger
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='rollup-backfill', daemon=True)

    def start(self) -> 'BackfillThread':
        self.thread.start()
        return self

    def stop(self, timeout: float = 10.0):
        self.stop_event.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout)

    def run_once(self) -> bool:
        """One pass over every system; True when nothing is pending anywhere."""
        complete = True
        for system in self.data_store.list_systems():
            if self.stop_event.is_set():
                return False
            try:
                if not self.data_store.run_rollup_backfill(system, stop=self.stop_event):
                    complete = False
            except Exception as e:
                if not self.data_store.has_system(system):
                    continue            # deleted meanwhile (DELETE /api/system/<name>)
                complete = False
                engine = self.data_store.rollups
                if engine is not None:
                    engine._log_limited(
                        (system, 'backfill'), logging.ERROR,
                        f"[rollups] backfill of {system} failed ({type(e).__name__}: {e}); "
                        f"retried in {IDLE_SECONDS}s (logged at most once an hour)", exc_info=True)
        return complete

    def _run(self):
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(IDLE_SECONDS)


def start_backfill_thread(data_store, log: Optional[logging.Logger] = None) -> Optional[BackfillThread]:
    """Start the background backfill for data_store (None when its rollups are off)."""
    if data_store.rollups is None:
        return None
    return BackfillThread(data_store, log).start()


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description='Lab Monitor rollups: status and rebuild')
    parser.add_argument('--config', required=True, help='Manager config file')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--status', action='store_true', help='print the backfill state as JSON')
    mode.add_argument('--rebuild', action='store_true', help='roll the selected tables up again from raw rows')
    parser.add_argument('--system', help='only this system (its directory name or device name)')
    parser.add_argument('--data-type', help='only this table (%s)' % ', '.join(TABLE_ORDER))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    with open(args.config, 'r') as f:
        config = json.load(f)
    if args.data_type and args.data_type not in REGISTRY:
        parser.error(f"--data-type must be one of {', '.join(TABLE_ORDER)}")

    from data_store import TypedDataStore
    from storage_util import normalize_device_name
    settings = settings_from_config(config, logger)
    store = TypedDataStore(config.get('data_dir', 'data'),
                           backup_before_migration=config.get('backup_before_migration', True),
                           rollups=RollupEngine(settings))
    try:
        if args.status:
            # Read only: safe next to any Manager, also an older one
            print(json.dumps(_read_only_status(store), indent=2, sort_keys=True))
            return 0
        systems = store.list_systems()
        if args.system:
            systems = [s for s in systems if s == normalize_device_name(args.system)]
            if not systems:
                print(f"ERROR: no history database for system {args.system!r}")
                return 1
        # A database an older Manager wrote is backed up and migrated by
        # this Manager's first start, never by this tool
        pending = _needs_migration(store, systems)
        if pending:
            for system, why in sorted(pending.items()):
                print(f"ERROR: {system}: not migrated by this Manager version yet ({why})")
            print("Nothing changed. Start this Manager once (it backs up and migrates the databases), "
                  "then run --rebuild again.")
            return 1
        tables = [args.data_type] if args.data_type else list(TABLE_ORDER)
        for system in systems:
            for table in tables:
                result = store.rebuild_rollups(system, table)
                if result and result.get('queued_ids'):
                    kept = result.get('kept_buckets_before')
                    print(f"{system}/{table}: re-queued ids 1..{result['queued_ids']}"
                          + (f" (pruned history: buckets before {kept} kept)" if kept else ''))
            store.run_rollup_backfill(system, tables)
        print(json.dumps(store.rollup_status(), indent=2, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
