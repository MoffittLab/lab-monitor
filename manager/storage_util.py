"""
Storage helpers shared by the Manager's two databases.

  - Device-name validation and normalization.  normalize_device_name() is the
    ONE function that turns a device name into a storage key; data_store
    (per-system directory), metrics (central DB identity) and archive_metrics
    all use it, so "Lab-A" and "lab-a" can never diverge between stores.

  - Canonical timestamps.  Every timestamp is stored as fixed-width ISO-8601
    UTC with microseconds and a 'Z' suffix ("2026-08-04T07:00:00.000000Z"),
    which is what collectors have always sent (datetime.isoformat() + 'Z').
    Fixed width means plain string comparison orders correctly in SQL.

  - LockedConnection.  One SQLite connection + one lock + explicit
    BEGIN IMMEDIATE / COMMIT transactions.  All statements on a connection
    run under its lock, so waitress worker threads never interleave inside
    each other's transactions.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

# Device names accepted on ingest and on every read endpoint.
DEVICE_NAME_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')

# How long a writer waits for another process (e.g. archive_metrics.py)
# holding the database lock before giving up with "database is locked".
BUSY_TIMEOUT_MS = 10000


def is_valid_device_name(name) -> bool:
    """True if name matches ^[A-Za-z0-9_.-]{1,64}$ exactly."""
    return isinstance(name, str) and DEVICE_NAME_RE.fullmatch(name) is not None


def normalize_device_name(name: str) -> str:
    """
    Storage key for a device name: lowercase, anything outside [a-z0-9_]
    becomes '_', truncated to 64 chars.

    This is exactly the transform the per-system data directories have
    always used (data/<key>/data.db), so existing directories keep
    resolving after upgrade.
    """
    return re.sub(r'[^a-z0-9_]', '_', str(name).lower())[:64]


# --------------------------------------------------------------------------- #
# Timestamps                                                                  #
# --------------------------------------------------------------------------- #

def parse_timestamp(value) -> datetime:
    """
    Parse an ISO-8601 timestamp into an aware UTC datetime.
    Naive timestamps are taken to be UTC (collectors send utcnow()).
    Raises ValueError for anything unparseable, including values whose
    UTC conversion falls outside datetime's range (0001-01-01T00:00+01:00).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid timestamp: {value!r}")
    s = value.strip()
    if s[-1] in 'Zz':
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f"invalid timestamp: {value!r}") from None


def format_timestamp(dt: datetime) -> str:
    """Format an aware (or naive-UTC) datetime in the canonical form."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond:06d}Z")


def canonical_timestamp(value) -> str:
    """Normalize an incoming ISO-8601 timestamp; raises ValueError."""
    return format_timestamp(parse_timestamp(value))


def utc_now() -> str:
    """Current time in the canonical form."""
    return format_timestamp(datetime.now(timezone.utc))


def timestamp_le(a: str, b: str) -> bool:
    """
    a <= b for two stored timestamps.  Compares as datetimes so rows written
    before timestamps were canonicalized still compare correctly; falls back
    to string comparison if either side is unparseable.
    """
    try:
        return parse_timestamp(a) <= parse_timestamp(b)
    except ValueError:
        return str(a) <= str(b)


# --------------------------------------------------------------------------- #
# SQLite                                                                      #
# --------------------------------------------------------------------------- #

# Primary SQLite result code for a generic SQL error (bad statement, no such
# column, too many columns ...): caused by what is being stored, not by the
# state of the database file.
_SQLITE_ERROR = 1

# Fallback for Pythons < 3.11, whose exceptions carry no sqlite_errorcode
_STORAGE_ERROR_TEXT = (
    'locked', 'busy', 'disk i/o', 'unable to open', 'disk is full',
    'readonly', 'read-only', 'malformed', 'not a database', 'corrupt',
    'out of memory', 'schema has changed', 'interrupted', 'locking protocol',
    'access permission',
)


def is_transient_db_error(exc: BaseException) -> bool:
    """
    True for failures of the STORAGE rather than of the message: lock
    contention, I/O, disk full, permissions, and a damaged or unreadable
    database file ("database disk image is malformed", "file is not a
    database").  These are reported to the collector as 'error' and the
    message stays in its queue until the database is usable again -- a
    corrupt data.db must never make collectors drop their messages.

    False for failures caused by the message itself (constraint, data and
    binding errors, a generic SQL error from its field names): those are
    permanent and the message is rejected to dead_letter.
    """
    if isinstance(exc, (OSError, MemoryError)):
        return True
    if not isinstance(exc, sqlite3.Error):
        return False
    if isinstance(exc, sqlite3.OperationalError):
        code = getattr(exc, 'sqlite_errorcode', None)
        if code is not None:
            # BUSY, LOCKED, READONLY, IOERR, FULL, CANTOPEN, PERM, SCHEMA, ...
            return (code & 0xFF) != _SQLITE_ERROR
        msg = str(exc).lower()
        return any(s in msg for s in _STORAGE_ERROR_TEXT)
    if isinstance(exc, sqlite3.ProgrammingError):
        # connection closed under us (e.g. DELETE /api/system racing an ingest)
        return 'closed' in str(exc).lower()
    if isinstance(exc, (sqlite3.IntegrityError, sqlite3.DataError,
                        sqlite3.InterfaceError, sqlite3.NotSupportedError)):
        return False
    # sqlite3.DatabaseError itself (SQLITE_CORRUPT, SQLITE_NOTADB and other
    # codes without a dedicated class) and InternalError: the database file.
    return isinstance(exc, sqlite3.DatabaseError)


class LockedConnection:
    """
    A sqlite3 connection in autocommit mode (isolation_level=None) guarded
    by a re-entrant lock.  Writes go through transaction(); reads through
    query()/query_one(), which fetch all rows while holding the lock.
    """

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        self.conn.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
        self.conn.execute('PRAGMA journal_mode=WAL')

    @contextmanager
    def transaction(self):
        """
        BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises.
        Nested use joins the outer transaction (the outermost block commits).
        """
        with self.lock:
            if self._depth > 0:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return

            self.conn.execute('BEGIN IMMEDIATE')
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self._depth = 0
                self._rollback()
                raise
            self._depth = 0
            try:
                self.conn.execute('COMMIT')
            except BaseException:
                self._rollback()
                raise

    def _rollback(self):
        try:
            self.conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass  # no transaction active (e.g. BEGIN itself failed)

    def query(self, sql: str, params=()) -> list:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params=()):
        """Single statement: autocommit, or part of the caller's transaction."""
        with self.lock:
            return self.conn.execute(sql, params)

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
