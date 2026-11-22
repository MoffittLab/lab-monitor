"""
Nightly snapshots of the Manager's databases to a second location.

The live databases stay on a local disk (SQLite over a network share is
unreliable, above all in WAL mode); once a day the Manager copies them to
`snapshot.dest_dir`, typically a NAS share, so they are backed up there.

One run:
  1. Each database (metrics.db and every <system>/data.db) is copied to a
     local staging folder with SQLite's online backup API.  That is a
     consistent copy including committed WAL content, taken while the Manager
     keeps ingesting.  The staged copy is switched to journal_mode=DELETE so
     it is a single self-contained file, then checked with PRAGMA quick_check.
  2. Staged databases (and any retention archives under <system>/archive/)
     are copied to <dest_dir>/snapshot-<UTC stamp>.partial/ with a plain file
     copy (SQLite never opens a file on the share), hashing as they go.
  3. With verify_copy (default) every copied file is read back from the
     destination and its SHA-256 compared.
  4. manifest.json (files, sizes, hashes, row counts) is written last, then
     the folder is renamed to snapshot-<stamp>.  A folder without the
     .partial suffix is therefore always complete.
  5. Old snapshots are pruned: the newest `keep_daily`, plus the newest one
     of each of the last `keep_monthly` months, are kept.  Only folders named
     snapshot-<stamp> that hold a manifest.json, and our own stale .partial
     folders, are ever deleted; nothing else in dest_dir is touched.

Status (last success / failure) is kept in <data_dir>/snapshot-status.json and
served by GET /api/snapshot.

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
from pathlib import Path

logger = logging.getLogger('snapshot')

PREFIX = 'snapshot-'
NAME_RE = re.compile(r'^snapshot-(\d{8}T\d{6}Z)$')
PARTIAL_RE = re.compile(r'^snapshot-(\d{8}T\d{6}Z)\.partial$')
PARTIAL_SUFFIX = '.partial'
MANIFEST = 'manifest.json'
STATUS_FILE = 'snapshot-status.json'
STAGING_DIR = '.snapshot-staging'
STAMP_FMT = '%Y%m%dT%H%M%SZ'
CHUNK = 4 * 1024 * 1024
BUSY_TIMEOUT_S = 60
# Extra room demanded beyond the databases' size (staging and destination)
SPACE_MARGIN = 1.1
SPACE_FLOOR = 64 * 1024 * 1024
STALE_PARTIAL_SECONDS = 24 * 3600

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


def find_sources(data_dir: str) -> list:
    """
    [(relative path with '/', absolute path, 'db' | 'file')] to snapshot:
    metrics.db, every <system>/data.db, and files under <system>/archive/
    (history moved out of data.db by history retention).  Pre-upgrade .bak
    files, a collector's own JSONL archive in a shared data_dir, logs and
    lock files are left out.
    """
    out = []
    central = os.path.join(data_dir, 'metrics.db')
    if os.path.isfile(central):
        out.append(('metrics.db', central, 'db'))
    try:
        entries = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for name in entries:
        if name.startswith('.'):
            continue
        sysdir = os.path.join(data_dir, name)
        if not os.path.isdir(sysdir):
            continue
        db = os.path.join(sysdir, 'data.db')
        if os.path.isfile(db):
            out.append((f'{name}/data.db', db, 'db'))
        archive = os.path.join(sysdir, 'archive')
        if os.path.isdir(archive):
            for root, dirs, files in os.walk(archive):
                dirs.sort()
                for f in sorted(files):
                    p = os.path.join(root, f)
                    rel = os.path.relpath(p, data_dir).replace(os.sep, '/')
                    out.append((rel, p, 'file'))
    return out


def _source_bytes(path: str, kind: str) -> int:
    total = 0
    for p in ((path, path + '-wal') if kind == 'db' else (path,)):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def _copy_hash(src: str, dst: str) -> tuple:
    """Plain byte copy src -> dst (fsync'd), returning (size, sha256)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    h = hashlib.sha256()
    size = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        for block in iter(lambda: fi.read(CHUNK), b''):
            fo.write(block)
            h.update(block)
            size += len(block)
        fo.flush()
        os.fsync(fo.fileno())
    return size, h.hexdigest()


def _stage_database(src_path: str, staged: str) -> dict:
    """
    Consistent copy of a live database into `staged` (a local file) with the
    online backup API, made self-contained (journal_mode=DELETE) and checked.
    Returns {'tables': {name: rows}}.  Raises on failure.
    """
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    # mode=rw: never create a database that has just been deleted
    uri = Path(os.path.abspath(src_path)).as_uri() + '?mode=rw'
    src = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S)
    dst = None
    try:
        src.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}')
        dst = sqlite3.connect(staged)
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
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.running_since = None
        self.next_run = None
        self.status = self._load_status()

    # ---- status ----------------------------------------------------------- #

    @property
    def status_path(self) -> str:
        return os.path.join(self.data_dir, STATUS_FILE)

    def _load_status(self) -> dict:
        try:
            with open(self.status_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

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
            'last_attempt', 'last_success', 'last_snapshot', 'last_bytes', 'last_files',
            'last_seconds', 'last_failure', 'last_error')})
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
        {'status': 'busy'} if one is already running, or
        {'status': 'error', 'error': ...}.  Never raises.
        """
        if not self._lock.acquire(blocking=False):
            return {'status': 'busy'}
        started = time.time()
        self.running_since = _iso(started)
        self.status['last_attempt'] = self.running_since
        try:
            result = self._run_locked(reason, started)
            self.status.update({
                'last_success': _iso(time.time()),
                'last_snapshot': result['path'],
                'last_bytes': result['bytes'],
                'last_files': result['files'],
                'last_seconds': result['seconds'],
            })
            self._save_status()
            return dict(result, status='ok')
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.status.update({'last_failure': _iso(time.time()), 'last_error': msg})
            self._save_status()
            self.log.error(f"Snapshot to {self.settings['dest_dir']} FAILED ({reason}): {msg}",
                           exc_info=True)
            return {'status': 'error', 'error': msg}
        finally:
            self.running_since = None
            self._lock.release()

    def _run_locked(self, reason: str, started: float) -> dict:
        dest_dir = self.settings['dest_dir']
        stamp = utc_stamp(started)
        sources = find_sources(self.data_dir)
        if not any(kind == 'db' for _, _, kind in sources):
            raise FileNotFoundError(f"No databases found in data_dir {self.data_dir!r}")

        need = int(sum(_source_bytes(p, k) for _, p, k in sources) * SPACE_MARGIN) + SPACE_FLOOR
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

        final = os.path.join(dest_dir, PREFIX + stamp)
        part = final + PARTIAL_SUFFIX
        if os.path.exists(final) or os.path.exists(part):
            raise FileExistsError(f"{final} already exists")
        staging = os.path.join(staging_root, stamp)

        self.log.info(f"Snapshot {stamp} ({reason}): {len(sources)} file(s) from "
                      f"{self.data_dir} to {dest_dir}")
        files = []
        try:
            os.makedirs(part)
            for rel, path, kind in sources:
                target = os.path.join(part, *rel.split('/'))
                entry = {'path': rel, 'kind': kind}
                if kind == 'db':
                    staged = os.path.join(staging, *rel.split('/'))
                    try:
                        entry.update(_stage_database(path, staged))
                    except sqlite3.OperationalError as e:
                        if not os.path.exists(path):   # deleted meanwhile (DELETE /api/system)
                            self.log.warning(f"Snapshot {stamp}: {rel} disappeared, skipped")
                            continue
                        raise sqlite3.OperationalError(f"{rel}: {e}") from e
                    size, digest = _copy_hash(staged, target)
                    os.remove(staged)
                else:
                    try:
                        size, digest = _copy_hash(path, target)
                    except FileNotFoundError:
                        self.log.warning(f"Snapshot {stamp}: {rel} disappeared, skipped")
                        continue
                entry.update({'bytes': size, 'sha256': digest})
                files.append(entry)

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
                'total_bytes': total,
                'files': files,
            }
            mpath = os.path.join(part, MANIFEST)
            with open(mpath, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _rename_dir(part, final)
        except BaseException:
            if os.path.isdir(part):
                _rmtree(part)
            raise
        finally:
            if os.path.isdir(staging):
                _rmtree(staging)

        seconds = round(time.time() - started, 1)
        self.log.info(f"Snapshot {stamp} complete: {len(files)} file(s), {total:,} bytes, "
                      f"{seconds}s -> {final}")
        try:
            self.prune()
        except Exception:
            self.log.error("Pruning old snapshots failed (the new snapshot is complete)",
                           exc_info=True)
        return {'path': final, 'stamp': stamp, 'files': len(files), 'bytes': total,
                'seconds': seconds}

    def _clear_staging(self, staging_root: str):
        """Remove leftovers of an interrupted run (our own staging folder only)."""
        if not os.path.isdir(staging_root):
            return
        for name in os.listdir(staging_root):
            p = os.path.join(staging_root, name)
            if re.fullmatch(r'\d{8}T\d{6}Z', name) and os.path.isdir(p):
                _rmtree(p)

    def prune(self) -> list:
        """
        Delete snapshots beyond keep_daily / keep_monthly, and our own .partial
        folders older than a day.  Returns the deleted paths.
        """
        dest_dir = self.settings['dest_dir']
        snaps = list_snapshots(dest_dir)
        keep = snapshots_to_keep([s for s, _ in snaps],
                                 self.settings['keep_daily'], self.settings['keep_monthly'])
        deleted = []
        for stamp, path in snaps:
            if stamp not in keep:
                _rmtree(path)
                deleted.append(path)
        now = datetime.now(timezone.utc)
        for name in os.listdir(dest_dir):
            m = PARTIAL_RE.match(name)
            p = os.path.join(dest_dir, name)
            if m and os.path.isdir(p) and \
                    (now - stamp_to_datetime(m.group(1))).total_seconds() > STALE_PARTIAL_SECONDS:
                _rmtree(p)
                deleted.append(p)
        for p in deleted:
            self.log.info(f"Pruned old snapshot {p}")
        return deleted

    # ---- schedule --------------------------------------------------------- #

    def missed_last_slot(self, now: datetime = None) -> bool:
        """True if no snapshot succeeded since the last scheduled time."""
        now = now or datetime.now()
        slot = last_slot_before(now, self.settings['time']).astimezone(timezone.utc)
        last = self.status.get('last_success')
        if not last:
            return True
        try:
            done = datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return done < slot

    def trigger(self) -> bool:
        """Ask the scheduler thread to run now.  False if one is running already."""
        if self._lock.locked():
            return False
        self._manual = True
        self._wake.set()
        return True

    def start(self):
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
            self._manual = False
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
            reason = 'manual' if self._manual else 'scheduled'
            self._manual = False
            self.run(reason)


# --------------------------------------------------------------------------- #
# Command line: verify a snapshot                                             #
# --------------------------------------------------------------------------- #

def verify_snapshot(path: str) -> list:
    """
    Problems found in one snapshot folder ([] = good): every file listed in
    manifest.json exists with its size and SHA-256, and every database passes
    PRAGMA quick_check (opened read-only, nothing written next to it).
    """
    problems = []
    try:
        with open(os.path.join(path, MANIFEST), 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return [f"manifest.json unreadable: {e}"]
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
                conn = sqlite3.connect(Path(os.path.abspath(p)).as_uri() + '?mode=ro&immutable=1',
                                       uri=True)
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
