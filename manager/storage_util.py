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
    each other's transactions.  Once a write found the database locked by
    another process, the next writes give up quickly for a while.

  - Pre-migration backups.  Before the Manager alters a database written by
    an older version for the first time, backup_database() copies it with
    SQLite's online backup API to "<db>.pre-upgrade-<UTC stamp>.bak" next to
    it.  If that copy fails the migration is held back (MigrationBlockedError)
    rather than run without a backup.  A database an earlier start already
    partly migrated gets no further "pre-upgrade" copy: its first copy is the
    one that holds the pre-upgrade layout.

  - InstanceLock.  One Manager per data_dir: an exclusive lock on
    <data_dir>/manager.lock, taken before any database is opened.
"""

import errno
import glob
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl          # POSIX
except ImportError:       # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt         # Windows
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

# Device names accepted on ingest and on every read endpoint.
DEVICE_NAME_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')

# How long a writer waits for another process (e.g. archive_metrics.py)
# holding the database lock before giving up with "database is locked".
BUSY_TIMEOUT_MS = 10000

# After a write gave up on a database locked by another process, writes to it
# for the next LOCKED_RETRY_SECONDS wait only LOCKED_BUSY_TIMEOUT_MS: a
# database held locked for minutes (an open write transaction in a DB browser)
# then costs one full wait per interval instead of one per request, and the
# requests queued behind it don't each hold a server thread for the full wait.
# Any successful write ends this early.
LOCKED_RETRY_SECONDS = 60
LOCKED_BUSY_TIMEOUT_MS = 100


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


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED: another connection holds the lock."""
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return (code & 0xFF) in (5, 6)
    msg = str(exc).lower()
    return 'locked' in msg or 'busy' in msg


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
        self.locked_until = 0.0     # time.monotonic(); while later, BEGIN waits briefly
        self.conn.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
        self.conn.execute('PRAGMA journal_mode=WAL')

    def _lock_statement(self, sql: str, short_wait: bool = False):
        """
        BEGIN / COMMIT: the statements that wait for another process's lock,
        for BUSY_TIMEOUT_MS (LOCKED_BUSY_TIMEOUT_MS with short_wait).  Giving
        up after a full wait starts LOCKED_RETRY_SECONDS of short waits; an
        error raised inside the transaction body (e.g. by another database)
        does not.
        """
        if short_wait:
            self.conn.execute(f'PRAGMA busy_timeout = {LOCKED_BUSY_TIMEOUT_MS}')
        try:
            self.conn.execute(sql)
        except sqlite3.OperationalError as e:
            if not short_wait and _is_busy_error(e):
                self.locked_until = time.monotonic() + LOCKED_RETRY_SECONDS
            raise
        finally:
            if short_wait:
                self.conn.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')

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

            self._lock_statement('BEGIN IMMEDIATE', short_wait=time.monotonic() < self.locked_until)
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self._depth = 0
                self._rollback()
                raise
            self._depth = 0
            try:
                self._lock_statement('COMMIT')
            except BaseException:
                self._rollback()
                raise
            self.locked_until = 0.0

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


# --------------------------------------------------------------------------- #
# Pre-migration backups                                                       #
# --------------------------------------------------------------------------- #

BACKUP_INFIX = '.pre-upgrade-'

# A migration held back by a failed backup is retried at most this often
# (on the next write to that database), so freeing disk space or fixing
# permissions un-blocks it without a restart.
UPGRADE_RETRY_SECONDS = 300


class MigrationBlockedError(OSError):
    """
    A database needs a schema migration that was held back because its
    pre-migration backup failed.  Subclasses OSError so ingest reports the
    message as a transient 'error': the collector keeps it and retries.
    """


def existing_backups(db_path: str) -> list:
    """Pre-upgrade backup files of db_path, oldest first (by mtime)."""
    paths = glob.glob(glob.escape(db_path) + BACKUP_INFIX + '*.bak')

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    return sorted(paths, key=mtime)


def _db_mtime(db_path: str) -> float:
    """Newest modification time of a database file and its -wal side file."""
    newest = 0.0
    for p in (db_path, db_path + '-wal'):
        try:
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            pass
    return newest


def _fsync_file(path: str):
    # Read-write: on Windows os.fsync (FlushFileBuffers) needs write access
    fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_backup_ro(path: str) -> sqlite3.Connection:
    """A read-only connection to a backup file that never creates -wal/-shm files."""
    conn = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + '?mode=ro&immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def logical_fingerprint(conn: sqlite3.Connection) -> dict:
    """
    {table: (columns, row count, max rowid)} for every user table.  For a
    database that is only ever appended to (and gains columns), two equal
    fingerprints mean the same data.
    """
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    return {t: table_fingerprint(conn, t) for t in sorted(tables)}


def table_fingerprint(conn: sqlite3.Connection, table: str):
    """(columns, row count, max rowid) of one table, None if it does not exist."""
    q = table.replace('"', '""')
    cols = tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{q}")'))
    if not cols:
        return None
    try:
        count, top = conn.execute(f'SELECT COUNT(*), MAX(rowid) FROM "{q}"').fetchone()
    except sqlite3.OperationalError:     # WITHOUT ROWID table
        count, top = conn.execute(f'SELECT COUNT(*), NULL FROM "{q}"').fetchone()
    return (cols, count, top)


def row_keys_digest(conn: sqlite3.Connection, table: str, columns=('timestamp', 'msg_id')):
    """
    sha1 of every row's rowid and `columns` (those the table has), in rowid
    order; None if the table does not exist.  With table_fingerprint() it
    tells apart a table renamed away and then made again by an older
    Manager with as many rows (its AUTOINCREMENT ids start at 1 again).
    """
    q = table.replace('"', '""')
    have = {r[1].lower(): r[1] for r in conn.execute(f'PRAGMA table_info("{q}")')}
    if not have:
        return None
    cols = ['"%s"' % have[c].replace('"', '""') for c in columns if c in have]
    digest = hashlib.sha1()
    for row in conn.execute(f'SELECT {", ".join(["rowid"] + cols)} FROM "{q}" ORDER BY rowid'):
        digest.update(repr(tuple(row)).encode('utf-8'))
    return digest.hexdigest()


def _backup_matches(path: str, check) -> bool:
    """check(connection) on a backup file; False if it can't be read."""
    try:
        bconn = _open_backup_ro(path)
        try:
            return bool(check(bconn))
        finally:
            bconn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Could not inspect backup {path}: {e}")
        return False


def backup_database(conn: sqlite3.Connection, db_path: str, reason: str = '',
                    original_layout=None, append_only: bool = False, covered=None):
    """
    Copy the database open on `conn` to
        <db_path>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak
    with SQLite's online backup API (a consistent snapshot that includes
    committed WAL content).  The copy is written under a temporary name,
    fsync'd and then renamed, so a .bak file is always complete; a partial
    copy is removed.  Raises on failure.  Returns the backup that holds the
    pre-upgrade data (the new copy or an existing one), or None.

    No new copy is taken (so a migration that fails at every restart can't
    fill the disk with copies, and a copy named "pre-upgrade" always shows
    the layout the older Manager wrote) when:
      - the newest existing backup is still current (nothing has been
        written to the database since it was taken);
      - original_layout (callable(connection) -> bool: True while nothing
        has been migrated yet) says an earlier start already partly migrated
        this database: the newest backup that still has the original layout
        is returned, or None if there is none (the remaining steps only add
        tables / columns or copy and rename, nothing is deleted);
      - append_only (the older Manager only ever appended rows and added
        columns to this database) and a backup with the original layout has
        the same logical_fingerprint();
      - covered (callable(backup connection) -> bool) says an existing
        backup already holds everything this migration changes (the one
        table it moves, unchanged since), however much else was written.
    """
    backups = existing_backups(db_path)
    if backups:
        newest = backups[-1]
        try:
            if os.path.getmtime(newest) >= _db_mtime(db_path):
                logger.warning(
                    f"Pre-upgrade backup {newest} is still current (nothing written to "
                    f"{db_path} since); not taking another"
                )
                return newest
        except OSError:
            pass

    if covered is not None:
        for b in reversed(backups):
            if _backup_matches(b, covered):
                logger.warning(f"Pre-upgrade backup {b} already holds what this migration of {db_path} "
                               f"changes ({reason}); not taking another")
                return b

    if original_layout is not None:
        if not original_layout(conn):
            for b in reversed(backups):
                if _backup_matches(b, original_layout):
                    logger.warning(
                        f"{db_path} was already partly migrated by an earlier start; its "
                        f"pre-upgrade backup is {b}. Not taking another copy (it would not "
                        f"show the pre-upgrade layout)."
                    )
                    return b
            logger.warning(
                f"{db_path} was already partly migrated by an earlier start and no backup of "
                f"its pre-upgrade layout exists{' (only copies of the partly migrated layout)' if backups else ''}. "
                f"Not taking a 'pre-upgrade' copy of the partly migrated database; the remaining "
                f"steps only add tables and columns or copy and rename, nothing is deleted."
            )
            return None
        if append_only and backups:
            current = logical_fingerprint(conn)
            for b in reversed(backups):
                if _backup_matches(b, lambda c: original_layout(c) and logical_fingerprint(c) == current):
                    logger.warning(
                        f"Pre-upgrade backup {b} holds the same data as {db_path} (same tables, "
                        f"columns, row counts and last row ids); not taking another"
                    )
                    return b

    # Our own incomplete copies from an interrupted earlier attempt
    for stale in glob.glob(glob.escape(db_path) + BACKUP_INFIX + '*.bak.tmp'):
        try:
            os.remove(stale)
        except OSError:
            pass

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    dest = f"{db_path}{BACKUP_INFIX}{stamp}.bak"
    n = 1
    while os.path.exists(dest):
        dest = f"{db_path}{BACKUP_INFIX}{stamp}-{n}.bak"
        n += 1
    tmp = dest + '.tmp'
    started = time.monotonic()
    target = None
    try:
        target = sqlite3.connect(tmp)
        conn.backup(target)
        target.close()
        target = None
        _fsync_file(tmp)
        for attempt in range(5):
            try:
                os.replace(tmp, dest)
                break
            except PermissionError:
                # Windows: an antivirus / indexer may still have the new file open
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    except BaseException:
        if target is not None:
            try:
                target.close()
            except sqlite3.Error:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    logger.warning(
        f"Backed up {db_path} to {dest} ({os.path.getsize(dest)} bytes, "
        f"{time.monotonic() - started:.1f}s) before migrating it"
        f"{' (' + reason + ')' if reason else ''}. Keep the copy until you have checked "
        f"the upgrade; delete it afterwards to free the space."
    )
    return dest


# --------------------------------------------------------------------------- #
# One Manager per data_dir                                                    #
# --------------------------------------------------------------------------- #

INSTANCE_LOCK_NAME = 'manager.lock'


def _errnos(*names):
    return frozenset(getattr(errno, n) for n in names if hasattr(errno, n))


# "Another process holds the lock": flock -> EWOULDBLOCK/EAGAIN (EACCES when
# emulated); msvcrt.locking(LK_NBLCK) -> EACCES or EDEADLOCK.
_LOCK_HELD_ERRNOS = _errnos('EAGAIN', 'EWOULDBLOCK', 'EACCES', 'EDEADLK', 'EDEADLOCK')
# "This filesystem can't lock at all" (e.g. some NFS / FUSE mounts)
_LOCK_UNSUPPORTED_ERRNOS = _errnos('ENOLCK', 'EOPNOTSUPP', 'ENOTSUP', 'ENOSYS')


class InstanceLockError(RuntimeError):
    """Another process (a second Manager) holds the data_dir lock."""

    def __init__(self, path: str, holder: str):
        self.path = path
        self.holder = holder
        super().__init__(f"{path} is locked by another Manager ({holder or 'holder unknown'})")


class InstanceLock:
    """
    Exclusive lock on <data_dir>/manager.lock for the lifetime of the process
    (the OS releases it when the process ends, however it ends).  Byte 0 is
    the locked byte; the holder's "pid=... started=..." line follows it, so a
    refused process can name the holder (Windows blocks reads of a locked
    byte range only).  The lock is advisory: it keeps a second Manager of
    this version away from the data_dir; an older Manager doesn't take it.
    """

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, INSTANCE_LOCK_NAME)
        self._fh = None

    def acquire(self) -> 'InstanceLock':
        """Take the lock; InstanceLockError if another process holds it."""
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)
        fh = os.fdopen(fd, 'r+b', buffering=0)
        try:
            if os.fstat(fd).st_size == 0:
                fh.write(b'\n')        # byte 0 must exist on every platform
            fh.seek(0)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                if e.errno in _LOCK_HELD_ERRNOS:
                    raise InstanceLockError(self.path, self.holder()) from None
                if e.errno not in _LOCK_UNSUPPORTED_ERRNOS:
                    raise
                logger.warning(
                    f"Cannot lock {self.path} ({e}): this filesystem does not support locks, so "
                    f"nothing stops a second Manager from using this data_dir. Make sure only "
                    f"one runs.")
            fh.seek(1)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started={utc_now()}\n".encode('ascii'))
        except BaseException:
            fh.close()
            raise
        self._fh = fh
        return self

    def holder(self) -> str:
        """The holder's "pid=... started=..." line ('' if it can't be read)."""
        try:
            with open(self.path, 'rb') as f:
                f.seek(1)
                return f.read(200).decode('ascii', 'replace').strip()
        except OSError:
            return ''

    def release(self):
        if self._fh is not None:
            try:
                self._fh.close()      # closing the handle releases the lock
            finally:
                self._fh = None
