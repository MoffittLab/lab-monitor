"""
Nightly snapshots of the Manager's databases to a second location.

The live databases stay on a local disk (SQLite over a network share is
unreliable, above all in WAL mode); once a day the Manager copies them to
`snapshot.dest_dir`, typically a NAS share, so they are backed up there.

One run:
  1. Each database is copied to a local staging folder with SQLite's online
     backup API: every <system>/data.db first, metrics.db LAST.  That is a
     consistent copy of each database including committed WAL content, taken
     while the Manager keeps ingesting.  Ingest commits metrics.db before the
     system's data.db, so with metrics.db taken last, every message a staged
     data.db holds also has its central updates in the staged metrics.db.
     The copies are not of one instant: metrics.db can be a little newer, and
     a replay after a restore stores those messages in data.db again (dedup
     is per data.db; re-applying central updates is safe).  The staged copy
     is written without fsync (it is temporary and is checked below), switched
     to journal_mode=DELETE so it is a single self-contained file, then
     checked with PRAGMA quick_check.  A database that can't be copied or
     fails the check (a damaged file) is listed as failed in the manifest and
     the others are still copied: the run is then 'partial'.
  2. Only then are the other stored files listed: files under
     <system>/archive/ (history retention writes a row there before it
     deletes it from its database, so a row missing from a staged database
     is in a listed archive file; a .jsonl archive is copied up to its last
     complete line), the previous Manager's <name>/usage.jsonl, and the
     <db>.post-upgrade-<stamp> files tools/rollback_upgrade.py set aside.
  3. Staged databases and those files are copied to
     <dest_dir>/snapshot-<UTC stamp>.partial/ with a plain file copy (SQLite
     never opens a file on the share), hashing as they go.  The run creates
     that folder itself with an atomic mkdir (the next second's if it exists:
     two Managers sharing dest_dir never write into the same one).
  4. With verify_copy (default) every copied file is read back from the
     destination and its SHA-256 compared.
  5. manifest.json (files, sizes, hashes, row counts, and the databases that
     failed) is written last, then the folder is renamed to snapshot-<stamp>.
     A folder without the .partial suffix therefore holds every file its
     manifest lists.
  6. Old snapshots are pruned: the newest `keep_daily`, plus the newest one
     of each of the last `keep_monthly` months, are kept; for a database that
     failed in a kept snapshot, the newest snapshot with a good copy of it is
     kept too.  Only this Manager's own snapshots (manifest host and
     source_data_dir) and its own stale .partial folders are ever deleted;
     nothing else in dest_dir is touched.

Not copied: the pre-upgrade *.bak copies (each a full copy of a database,
needed only to go back to the f086e7e Manager), a collector's own files in a
shared data_dir, logs, lock files and the staging folder.

Status (last success / partial run / failure) is kept in
<data_dir>/snapshot-status.json and served by GET /api/snapshot.

Check a snapshot by hand:
    python snapshot.py verify <dest_dir>/snapshot-<stamp>
"""
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# file: URIs that also work for a UNC path (`verify` of a snapshot on the NAS
# share, staging from a data_dir on one); storage_util is standard library
# only, like this module
from storage_util import sqlite_uri

logger = logging.getLogger('snapshot')

PREFIX = 'snapshot-'
NAME_RE = re.compile(r'^snapshot-(\d{8}T\d{6}Z)$')
PARTIAL_RE = re.compile(r'^snapshot-(\d{8}T\d{6}Z)\.partial$')
PARTIAL_SUFFIX = '.partial'
MANIFEST = 'manifest.json'
# Written into a .partial folder when it is created, removed just before the
# manifest: says which Manager may prune the folder if the run never finishes
PARTIAL_OWNER = 'partial-owner.json'
STATUS_FILE = 'snapshot-status.json'
STAGING_DIR = '.snapshot-staging'
STAMP_FMT = '%Y%m%dT%H%M%SZ'
CHUNK = 4 * 1024 * 1024
BUSY_TIMEOUT_S = 60
# Extra room demanded beyond the databases' size (staging and destination)
SPACE_MARGIN = 1.1
SPACE_FLOOR = 64 * 1024 * 1024
STALE_PARTIAL_SECONDS = 24 * 3600
# tools/rollback_upgrade.py MOVED_INFIX: <db>.post-upgrade-<stamp>[-wal|-journal]
MOVED_INFIX = '.post-upgrade-'

DEFAULTS = {
    'enabled': False,
    'dest_dir': None,
    'time': '03:30',            # local server time, daily
    'keep_daily': 14,
    'keep_monthly': 12,
    'verify_copy': True,
    'catch_up': True,           # run soon after start if the last scheduled run was missed
    'startup_delay_seconds': 600,
}


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip('\\/')


def _inside(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p + os.sep)


def _parse_time(value) -> tuple:
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,2}:\d{2}', value.strip()):
        raise ValueError("snapshot.time must be 'HH:MM' (24-hour, local server time)")
    h, m = (int(x) for x in value.strip().split(':'))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("snapshot.time must be 'HH:MM' (24-hour, local server time)")
    return h, m


def settings_from_config(config: dict) -> dict:
    """
    The normalized `snapshot` settings of a Manager config.  Raises ValueError
    for an unusable section.  Touches no file.
    """
    section = config.get('snapshot')
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("snapshot must be an object")
    unknown = set(section) - set(DEFAULTS) - {'comment'}
    if unknown:
        raise ValueError(f"snapshot: unknown setting(s) {', '.join(sorted(unknown))}")
    s = dict(DEFAULTS)
    s.update({k: v for k, v in section.items() if k != 'comment'})

    for key in ('enabled', 'verify_copy', 'catch_up'):
        if not isinstance(s[key], bool):
            raise ValueError(f"snapshot.{key} must be true or false")
    for key, minimum in (('keep_daily', 1), ('keep_monthly', 0), ('startup_delay_seconds', 0)):
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise ValueError(f"snapshot.{key} must be an integer >= {minimum}")
    _parse_time(s['time'])

    if s['enabled']:
        dest = s['dest_dir']
        if not isinstance(dest, str) or not dest.strip():
            raise ValueError("snapshot.dest_dir is required when snapshot.enabled is true")
        s['dest_dir'] = dest.strip()
        data_dir = config.get('data_dir', 'data')
        if _inside(s['dest_dir'], data_dir) or _inside(data_dir, s['dest_dir']):
            raise ValueError(
                "snapshot.dest_dir must be outside data_dir (and data_dir outside it): "
                "a snapshot next to the live data protects against nothing")
    return s


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def utc_stamp(t: float = None) -> str:
    return time.strftime(STAMP_FMT, time.gmtime(time.time() if t is None else t))


def stamp_to_datetime(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FMT).replace(tzinfo=timezone.utc)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def find_databases(data_dir: str) -> list:
    """
    [(relative path with '/', absolute path)] of the databases to snapshot,
    in staging order: every <system>/data.db (sorted), then metrics.db LAST
    (see the module docstring).
    """
    out = []
    try:
        entries = sorted(os.listdir(data_dir))
    except OSError:
        entries = []
    for name in entries:
        if name.startswith('.'):
            continue
        db = os.path.join(data_dir, name, 'data.db')
        if os.path.isfile(db):
            out.append((f'{name}/data.db', db))
    central = os.path.join(data_dir, 'metrics.db')
    if os.path.isfile(central):
        out.append(('metrics.db', central))
    return out


def _moved_aside(filename: str, base: str) -> bool:
    """<base>.post-upgrade-<stamp> and its -wal / -journal (a -shm holds no data)."""
    return filename.startswith(base + MOVED_INFIX) and not filename.endswith('-shm')


def find_files(data_dir: str) -> list:
    """
    [(relative path with '/', absolute path)] of the other stored files:
    everything under <system>/archive/ (history moved out of a database by
    history retention), the previous Manager's <name>/usage.jsonl, and the
    <db>.post-upgrade-<stamp> files tools/rollback_upgrade.py moved aside.
    Pre-upgrade .bak files, a collector's own files in a shared data_dir,
    logs, lock files and the staging folder are left out.
    """
    out = []

    def add(path):
        out.append((os.path.relpath(path, data_dir).replace(os.sep, '/'), path))
    try:
        entries = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for name in entries:
        if name.startswith('.'):
            continue
        path = os.path.join(data_dir, name)
        if not os.path.isdir(path):
            if _moved_aside(name, 'metrics.db') and os.path.isfile(path):
                add(path)
            continue
        try:
            names = sorted(os.listdir(path))
        except OSError:
            continue
        for fn in names:
            f = os.path.join(path, fn)
            if (fn == 'usage.jsonl' or _moved_aside(fn, 'data.db')) and os.path.isfile(f):
                add(f)
        archive = os.path.join(path, 'archive')
        if os.path.isdir(archive):
            for root, dirs, files in os.walk(archive):
                dirs.sort()
                for fn in sorted(files):
                    add(os.path.join(root, fn))
    return out


def _is_archive_jsonl(rel: str) -> bool:
    parts = rel.split('/')
    return len(parts) >= 3 and parts[1] == 'archive' and rel.endswith('.jsonl')


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _whole_lines_size(path: str) -> int:
    """
    Bytes of `path` up to and including its last newline.  History retention
    may be appending to an archive file (the Manager's thread, or
    archive_metrics.py run by hand) while it is copied; what follows the last
    newline is then a line still being written, whose rows are still in
    their database (retention deletes rows only after their lines are
    written), so leaving it out loses nothing and keeps the copy a valid
    JSONL file that later appends can't corrupt.
    """
    with open(path, 'rb') as f:
        pos = f.seek(0, os.SEEK_END)
        while pos > 0:
            step = min(CHUNK, pos)
            f.seek(pos - step)
            i = f.read(step).rfind(b'\n')
            if i >= 0:
                return pos - step + i + 1
            pos -= step
    return 0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def _copy_hash(src: str, dst: str, limit: int = None) -> tuple:
    """Plain byte copy src -> dst (fsync'd; at most `limit` bytes), returning (size, sha256)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    h = hashlib.sha256()
    size = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while limit is None or size < limit:
            block = fi.read(CHUNK if limit is None else min(CHUNK, limit - size))
            if not block:
                break
            fo.write(block)
            h.update(block)
            size += len(block)
        fo.flush()
        os.fsync(fo.fileno())
    return size, h.hexdigest()


def _open_staged(path: str) -> sqlite3.Connection:
    """
    The connection a staged copy is written through.  synchronous=OFF: the
    staged file is temporary (an interrupted run leaves only staging files,
    removed at the next start or run) and is checked by quick_check and
    SHA-256 before anything relies on it, so it needs no fsync.  An fsync of
    the whole copy would make the live Manager's own commits wait for the
    disk, and with them every collector's request.
    """
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA synchronous=OFF')
    return conn


def _stage_database(src_path: str, staged: str) -> dict:
    """
    Consistent copy of a live database into `staged` (a local file) with the
    online backup API, made self-contained (journal_mode=DELETE) and checked.
    Returns {'tables': {name: rows}}.  Raises on failure.
    """
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    # mode=rw: never create a database that has just been deleted
    src = sqlite3.connect(sqlite_uri(src_path, 'mode=rw'), uri=True, timeout=BUSY_TIMEOUT_S)
    dst = None
    try:
        src.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}')
        dst = _open_staged(staged)
        # One step: a stepwise backup restarts whenever another connection
        # writes, which the live Manager does every few seconds.  In WAL mode
        # the read transaction this holds doesn't block those writers.
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
        check = dst.execute('PRAGMA quick_check').fetchall()
        if [tuple(r) for r in check] != [('ok',)]:
            raise sqlite3.DatabaseError(
                f"quick_check of the staged copy of {src_path} failed: "
                f"{'; '.join(str(r[0]) for r in check[:5])}")
        tables = {}
        for (t,) in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"):
            q = t.replace('"', '""')
            tables[t] = dst.execute(f'SELECT COUNT(*) FROM "{q}"').fetchone()[0]
        return {'tables': tables}
    finally:
        if dst is not None:
            dst.close()
        src.close()


def _rmtree(path: str):
    def onerror(func, p, exc_info):
        # Windows: read-only attribute, or an indexer/antivirus holding a file briefly
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def _rename_dir(src: str, dst: str):
    for attempt in range(6):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.5 * (attempt + 1))


def next_run_after(now: datetime, hhmm: str) -> datetime:
    """The first scheduled time strictly after `now` (naive local time)."""
    h, m = _parse_time(hhmm)
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def last_slot_before(now: datetime, hhmm: str) -> datetime:
    """The most recent scheduled time at or before `now` (naive local time)."""
    return next_run_after(now, hhmm) - timedelta(days=1)


def snapshots_to_keep(stamps: list, keep_daily: int, keep_monthly: int) -> set:
    """
    The newest `keep_daily` stamps, plus the newest stamp of each of the
    `keep_monthly` most recent calendar months (UTC) that have one.
    """
    ordered = sorted(stamps, reverse=True)
    keep = set(ordered[:keep_daily])
    months = []
    for s in ordered:
        month = s[:6]
        if month not in months:
            months.append(month)
            if len(months) <= keep_monthly:
                keep.add(s)
    return keep


def list_snapshots(dest_dir: str) -> list:
    """Complete snapshots in dest_dir: [(stamp, path)], oldest first."""
    out = []
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return out
    for name in names:
        m = NAME_RE.match(name)
        p = os.path.join(dest_dir, name)
        if m and os.path.isfile(os.path.join(p, MANIFEST)):
            out.append((m.group(1), p))
    return sorted(out)


def _read_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def keep_good_copies(snaps: list, keep: set) -> set:
    """
    `keep` plus, for every database listed as failed in a kept snapshot's
    manifest, the newest snapshot that holds a good copy of it, so pruning
    never deletes the last good copy of a database that has since been
    damaged.  snaps: [(stamp, path, manifest)].
    """
    keep = set(keep)
    while True:
        failed = {f.get('path') for stamp, _, m in snaps if stamp in keep
                  for f in (m.get('failed') or []) if isinstance(f, dict)}
        added = False
        for rel in sorted(p for p in failed if p):
            good = [stamp for stamp, _, m in snaps
                    if any(isinstance(e, dict) and e.get('path') == rel and e.get('kind') == 'db'
                           for e in (m.get('files') or []))]
            if good and max(good) not in keep:
                keep.add(max(good))
                added = True
        if not added:
            return keep


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #

class SnapshotRunner:
    """Takes snapshots (run()) and schedules them (start())."""

    def __init__(self, data_dir: str, settings: dict, log: logging.Logger = None):
        self.data_dir = data_dir
        self.settings = settings
        self.log = log or logger
        self._lock = threading.Lock()
        self._trigger_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._manual = False
        self.running_since = None
        self.next_run = None
        self.status = self._load_status()

    # ---- status ----------------------------------------------------------- #

    @property
    def status_path(self) -> str:
        return os.path.join(self.data_dir, STATUS_FILE)

    def _load_status(self) -> dict:
        data = _read_json(self.status_path)
        return data if isinstance(data, dict) else {}

    def _save_status(self):
        tmp = self.status_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.status, f, indent=2)
            os.replace(tmp, self.status_path)
        except OSError as e:
            self.log.warning(f"Could not write {self.status_path}: {e}")

    def describe(self) -> dict:
        """Status for the API (no secrets in it)."""
        s = self.settings
        body = {
            'enabled': s['enabled'],
            'dest_dir': s['dest_dir'],
            'time': s['time'],
            'keep_daily': s['keep_daily'],
            'keep_monthly': s['keep_monthly'],
            'running': self.running_since is not None,
            'running_since': self.running_since,
            'next_run': self.next_run,
        }
        body.update({k: self.status.get(k) for k in (
            'last_attempt', 'last_result', 'last_success', 'last_partial', 'last_snapshot',
            'last_bytes', 'last_files', 'last_failed', 'last_seconds', 'last_failure', 'last_error')})
        last = self.status.get('last_success')
        body['hours_since_success'] = None
        if last:
            try:
                age = datetime.now(timezone.utc) - datetime.strptime(
                    last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                body['hours_since_success'] = round(age.total_seconds() / 3600, 1)
            except ValueError:
                pass
        return body

    # ---- one run ---------------------------------------------------------- #

    def run(self, reason: str = 'scheduled') -> dict:
        """
        Take one snapshot now (blocking).  Returns {'status': 'ok', ...},
        {'status': 'partial', ..., 'failed': [...]} if some database could
        not be copied (the others were), {'status': 'busy'} if one is
        already running, or {'status': 'error', 'error': ...}.  Never raises.
        """
        if not self._lock.acquire(blocking=False):
            return {'status': 'busy'}
        with self._trigger_lock:
            self._manual = False        # a pending request is served by this run
        started = time.time()
        self.running_since = _iso(started)
        self.status['last_attempt'] = self.running_since
        try:
            result = self._run_locked(reason, started)
            now = _iso(time.time())
            self.status.update({
                'last_result': result['status'],
                'last_snapshot': result['path'],
                'last_bytes': result['bytes'],
                'last_files': result['files'],
                'last_failed': [f['path'] for f in result['failed']],
                'last_seconds': result['seconds'],
            })
            if result['failed']:
                msg = 'partial snapshot, not copied: ' + '; '.join(
                    f"{f['path']} ({f['error']})" for f in result['failed'])
                self.status.update({'last_partial': now, 'last_failure': now, 'last_error': msg})
                self.log.error(f"Snapshot {result['stamp']} ({reason}) is PARTIAL: {msg}. Every other "
                               f"database was copied; the newest snapshot with a good copy of "
                               f"{'each' if len(result['failed']) > 1 else 'that'} database is kept "
                               f"until it is copied again.")
            else:
                self.status['last_success'] = now
            self._save_status()
            return dict(result)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.status.update({'last_result': 'error', 'last_failure': _iso(time.time()),
                                'last_error': msg})
            self._save_status()
            self.log.error(f"Snapshot to {self.settings['dest_dir']} FAILED ({reason}): {msg}",
                           exc_info=True)
            return {'status': 'error', 'error': msg}
        finally:
            self.running_since = None
            self._lock.release()

    def _new_stamp(self, dest_dir: str, started: float) -> tuple:
        """
        (stamp, final folder, .partial folder) for a run started at `started`.
        The .partial folder is created here, with os.mkdir, which is atomic
        (also on an SMB share): of two Managers sharing dest_dir that start in
        the same second only one gets it, so a run only ever writes into and
        removes a .partial folder it created itself.  A folder of that second
        may exist already (a manual run right after another one, or another
        Manager's run): take the next second rather than fail.
        """
        t = started
        for _ in range(5):
            stamp = utc_stamp(t)
            final = os.path.join(dest_dir, PREFIX + stamp)
            part = final + PARTIAL_SUFFIX
            if not os.path.exists(final):
                try:
                    os.mkdir(part)
                except FileExistsError:
                    pass
                else:
                    if not os.path.exists(final):
                        return stamp, final, part
                    # its owner renamed that second's .partial meanwhile: ours is still empty
                    os.rmdir(part)
            time.sleep(max(0.0, int(t) + 1 - time.time()) + 0.01)
            t = time.time()
        raise FileExistsError(f"{final} already exists")

    def _owner(self) -> dict:
        return {'host': socket.gethostname(), 'source_data_dir': os.path.abspath(self.data_dir)}

    def owns(self, info) -> bool:
        """True if a manifest (or a .partial folder's owner file) was written by this Manager."""
        if not isinstance(info, dict):
            return False
        host, src = info.get('host'), info.get('source_data_dir')
        if not isinstance(host, str) or not isinstance(src, str) or not src:
            return False
        return host.lower() == socket.gethostname().lower() and _norm(src) == _norm(self.data_dir)

    def _run_locked(self, reason: str, started: float) -> dict:
        dest_dir = self.settings['dest_dir']
        databases = find_databases(self.data_dir)
        if not databases:
            raise FileNotFoundError(f"No databases found in data_dir {self.data_dir!r}")

        need = int((sum(_size(p) + _size(p + '-wal') for _, p in databases) +
                    sum(_size(p) for _, p in find_files(self.data_dir))) * SPACE_MARGIN) + SPACE_FLOOR
        staging_root = os.path.join(self.data_dir, STAGING_DIR)
        self._clear_staging(staging_root)
        os.makedirs(staging_root, exist_ok=True)
        free_local = shutil.disk_usage(staging_root).free
        if free_local < need:
            raise OSError(f"Not enough free space for staging in {staging_root}: "
                          f"{free_local:,} bytes free, about {need:,} needed")

        os.makedirs(dest_dir, exist_ok=True)
        free_dest = shutil.disk_usage(dest_dir).free
        if free_dest < need:
            raise OSError(f"Not enough free space in {dest_dir}: "
                          f"{free_dest:,} bytes free, about {need:,} needed")

        stamp, final, part = self._new_stamp(dest_dir, started)     # creates `part`: ours
        staging = os.path.join(staging_root, stamp)
        files, failed = [], []
        try:
            self.log.info(f"Snapshot {stamp} ({reason}): {len(databases)} database(s) from "
                          f"{self.data_dir} to {dest_dir}")
            owner = os.path.join(part, PARTIAL_OWNER)
            with open(owner, 'w', encoding='utf-8') as f:
                json.dump(dict(self._owner(), stamp=stamp), f)

            # 1. Every database into local staging, metrics.db last
            staged_dbs = []
            for rel, path in databases:
                staged = os.path.join(staging, *rel.split('/'))
                try:
                    info = _stage_database(path, staged)
                except sqlite3.Error as e:
                    if isinstance(e, sqlite3.OperationalError) and not os.path.exists(path):
                        # deleted meanwhile (DELETE /api/system)
                        self.log.warning(f"Snapshot {stamp}: {rel} disappeared, skipped")
                        continue
                    msg = f"{type(e).__name__}: {e}"
                    self.log.error(f"Snapshot {stamp}: {rel} could NOT be copied ({msg}); "
                                   f"copying the other databases")
                    failed.append({'path': rel, 'kind': 'db', 'error': msg})
                    continue
                staged_dbs.append((rel, staged, info))
            if not staged_dbs:
                raise sqlite3.DatabaseError(
                    "no database could be copied: " +
                    ('; '.join(f"{f['path']}: {f['error']}" for f in failed) or 'all disappeared'))

            # 2. The other files, listed only now (see the module docstring)
            others = find_files(self.data_dir)

            # 3. Copy to dest_dir
            for rel, staged, info in staged_dbs:
                size, digest = _copy_hash(staged, os.path.join(part, *rel.split('/')))
                os.remove(staged)
                entry = {'path': rel, 'kind': 'db'}
                entry.update(info)
                entry.update({'bytes': size, 'sha256': digest})
                files.append(entry)
            for rel, path in others:
                try:
                    limit = _whole_lines_size(path) if _is_archive_jsonl(rel) else None
                    size, digest = _copy_hash(path, os.path.join(part, *rel.split('/')), limit)
                except FileNotFoundError:
                    self.log.warning(f"Snapshot {stamp}: {rel} disappeared, skipped")
                    continue
                files.append({'path': rel, 'kind': 'file', 'bytes': size, 'sha256': digest})

            if self.settings['verify_copy']:
                for entry in files:
                    got = _sha256(os.path.join(part, *entry['path'].split('/')))
                    if got != entry['sha256']:
                        raise IOError(f"Verification failed for {entry['path']} in {part}: "
                                      f"the copy on the destination differs from what was written")

            total = sum(e['bytes'] for e in files)
            manifest = {
                'format': 1,
                'stamp': stamp,
                'created_utc': _iso(started),
                'finished_utc': _iso(time.time()),
                'reason': reason,
                'host': socket.gethostname(),
                'source_data_dir': os.path.abspath(self.data_dir),
                'verified_copy': bool(self.settings['verify_copy']),
                'complete': not failed,
                'total_bytes': total,
                'files': files,
                'failed': failed,
            }
            os.remove(owner)
            mpath = os.path.join(part, MANIFEST)
            with open(mpath, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _rename_dir(part, final)
        except BaseException:
            if os.path.isdir(part):         # this run's own (_new_stamp created it)
                _rmtree(part)
            raise
        finally:
            if os.path.isdir(staging):
                _rmtree(staging)

        seconds = round(time.time() - started, 1)
        self.log.info(f"Snapshot {stamp} {'complete' if not failed else 'PARTIAL'}: {len(files)} "
                      f"file(s), {total:,} bytes, {seconds}s -> {final}")
        try:
            self.prune()
        except Exception:
            self.log.error("Pruning old snapshots failed (the new snapshot is complete)",
                           exc_info=True)
        return {'status': 'partial' if failed else 'ok', 'path': final, 'stamp': stamp,
                'files': len(files), 'bytes': total, 'seconds': seconds, 'failed': failed}

    def _clear_staging(self, staging_root: str, remove_root: bool = False):
        """Remove leftovers of an interrupted run (our own staging folder only)."""
        if not os.path.isdir(staging_root):
            return
        for name in os.listdir(staging_root):
            p = os.path.join(staging_root, name)
            if re.fullmatch(r'\d{8}T\d{6}Z', name) and os.path.isdir(p):
                _rmtree(p)
        if remove_root:
            try:
                os.rmdir(staging_root)
            except OSError:
                pass

    def prune(self) -> list:
        """
        Delete this Manager's snapshots beyond keep_daily / keep_monthly (and
        beyond the newest good copy of any database that failed since), and
        its own .partial folders older than a day.  Snapshots and .partial
        folders of another Manager or data_dir are never touched.  Returns the
        deleted paths.
        """
        dest_dir = self.settings['dest_dir']
        snaps, foreign = [], []
        for stamp, path in list_snapshots(dest_dir):
            manifest = _read_json(os.path.join(path, MANIFEST))
            if self.owns(manifest):
                snaps.append((stamp, path, manifest))
            else:
                foreign.append(path)
        keep = snapshots_to_keep([s for s, _, _ in snaps],
                                 self.settings['keep_daily'], self.settings['keep_monthly'])
        keep = keep_good_copies(snaps, keep)
        deleted = []
        for stamp, path, _ in snaps:
            if stamp not in keep:
                _rmtree(path)
                deleted.append(path)
        now = datetime.now(timezone.utc)
        for name in os.listdir(dest_dir):
            m = PARTIAL_RE.match(name)
            p = os.path.join(dest_dir, name)
            if m and os.path.isdir(p) and \
                    (now - stamp_to_datetime(m.group(1))).total_seconds() > STALE_PARTIAL_SECONDS and \
                    self.owns(_read_json(os.path.join(p, PARTIAL_OWNER))):
                _rmtree(p)
                deleted.append(p)
        for p in deleted:
            self.log.info(f"Pruned old snapshot {p}")
        if foreign:
            self.log.info(f"{len(foreign)} snapshot(s) in {dest_dir} were made by another Manager or "
                          f"from another data_dir; they are never pruned by this one")
        return deleted

    # ---- schedule --------------------------------------------------------- #

    def missed_last_slot(self, now: datetime = None) -> bool:
        """True if no snapshot ran to the end (complete or partial) since the last scheduled time."""
        now = now or datetime.now()
        slot = last_slot_before(now, self.settings['time']).astimezone(timezone.utc)
        done = None
        for key in ('last_success', 'last_partial'):
            try:
                t = datetime.strptime(self.status.get(key) or '', '%Y-%m-%dT%H:%M:%SZ').replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            done = t if done is None or t > done else done
        return done is None or done < slot

    def trigger(self) -> str:
        """
        Ask the scheduler thread to run now.  'started'; or 'running' (a run
        is in progress) or 'pending' (an earlier request has not started yet;
        the run that serves it serves this one too), and then nothing more is
        queued.
        """
        with self._trigger_lock:
            if self._lock.locked():
                return 'running'
            if self._manual:
                return 'pending'
            self._manual = True
            self._wake.set()
            return 'started'

    def start(self):
        # A Manager killed mid-snapshot leaves its staging copies (as large as
        # the databases); remove them now, also when snapshots are off
        try:
            self._clear_staging(os.path.join(self.data_dir, STAGING_DIR), remove_root=True)
        except OSError as e:
            self.log.warning(f"Could not remove old snapshot staging files: {e}")
        if not self.settings['enabled']:
            self.log.info("Snapshots disabled (snapshot.enabled is false)")
            return None
        self._manual = False
        self._thread = threading.Thread(target=self._loop, name='nas-snapshot', daemon=True)
        self._thread.start()
        self.log.info(
            f"Snapshots ENABLED: daily at {self.settings['time']} (local time) to "
            f"{self.settings['dest_dir']}, keeping {self.settings['keep_daily']} daily and "
            f"{self.settings['keep_monthly']} monthly")
        return self._thread

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True if woken by trigger() or stop()."""
        woke = self._wake.wait(max(0.0, seconds))
        self._wake.clear()
        return woke

    def _loop(self):
        s = self.settings
        if s['catch_up'] and self.missed_last_slot():
            self.next_run = _iso(time.time() + s['startup_delay_seconds'])
            self.log.info(f"Last scheduled snapshot was missed; catching up in "
                          f"{s['startup_delay_seconds']}s")
            self._wait(s['startup_delay_seconds'])
            if self._stop.is_set():
                return
            self.run('catch-up')
        while not self._stop.is_set():
            nxt = next_run_after(datetime.now(), s['time'])
            self.next_run = _iso(nxt.timestamp())
            # Wake at least hourly so clock changes (DST, NTP) are followed
            while not self._stop.is_set():
                remaining = (nxt - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                if self._wait(min(remaining, 3600)) and self._manual:
                    break
            if self._stop.is_set():
                return
            self.run('manual' if self._manual else 'scheduled')


# --------------------------------------------------------------------------- #
# Command line: verify a snapshot                                             #
# --------------------------------------------------------------------------- #

def verify_snapshot(path: str) -> list:
    """
    Problems found in one snapshot folder ([] = good): every file listed in
    manifest.json exists with its size and SHA-256, every database passes
    PRAGMA quick_check (opened read-only, nothing written next to it), and no
    database is listed as failed (a partial snapshot).
    """
    problems = []
    try:
        with open(os.path.join(path, MANIFEST), 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return [f"manifest.json unreadable: {e}"]
    for entry in manifest.get('failed') or []:
        problems.append(f"{entry.get('path')}: not in this snapshot (its copy failed: {entry.get('error')})")
    for entry in manifest.get('files', []):
        p = os.path.join(path, *entry['path'].split('/'))
        if not os.path.isfile(p):
            problems.append(f"{entry['path']}: missing")
            continue
        if os.path.getsize(p) != entry['bytes']:
            problems.append(f"{entry['path']}: size {os.path.getsize(p)} != {entry['bytes']}")
            continue
        if _sha256(p) != entry['sha256']:
            problems.append(f"{entry['path']}: SHA-256 differs")
            continue
        if entry.get('kind') == 'db':
            try:
                conn = sqlite3.connect(sqlite_uri(p, 'mode=ro&immutable=1'), uri=True)
                try:
                    res = conn.execute('PRAGMA quick_check').fetchall()
                finally:
                    conn.close()
                if [tuple(r) for r in res] != [('ok',)]:
                    problems.append(f"{entry['path']}: quick_check {res[:3]}")
            except sqlite3.Error as e:
                problems.append(f"{entry['path']}: {e}")
    return problems


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Lab Monitor Manager snapshots")
    sub = parser.add_subparsers(dest='cmd', required=True)
    v = sub.add_parser('verify', help='Check a snapshot folder against its manifest')
    v.add_argument('path')
    args = parser.parse_args(argv)
    if args.cmd == 'verify':
        problems = verify_snapshot(args.path)
        if problems:
            print(f"FAIL: {args.path}")
            for p in problems:
                print(f"  {p}")
            return 1
        print(f"OK: {args.path}")
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
