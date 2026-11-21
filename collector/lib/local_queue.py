"""
Local message queue (queue.json) for the collector.

queue.json holds a JSON list of messages the Manager has not yet
acknowledged. More than one collector process can touch it at the same time
(the 5-minute metrics run overlaps the multi-hour daily disk run, and both
append and sync), so every read-modify-write happens under an exclusive lock
on a sidecar file, queue.json.lock:

  - POSIX:   fcntl.flock
  - Windows: msvcrt.locking

On a filesystem without lock support (some NFS/CIFS mounts: ENOLCK,
EOPNOTSUPP) the queue is used unlocked, as by collectors before locking,
with a loud error in the log; any other locking error is raised at once.

The lock is never held across network I/O. A sync takes a snapshot under the
lock, POSTs without it, then re-reads the queue under the lock to remove only
the acknowledged msg_ids, so messages appended in the meantime survive.

Writes are atomic (temp file in the same directory + fsync + os.replace), so
a crash mid-write leaves the previous queue intact. The files get the usual
umask-based mode (e.g. 0644), not mkstemp's 0600: they hold no secrets, and a
one-off run under another account (sudo while debugging) must not leave a
queue the scheduled account can no longer read. A queue.json that fails to
parse is moved aside to queue.corrupt-<timestamp>.json and logged — it is
never treated as empty and overwritten. Every message is also in the
permanent monthly archive.

Message IDs: every new message carries header.msg_id (uuid4 hex). Entries
queued by older collectors lack one; the first time the queue is read they
are given the same deterministic id the Manager derives for messages that
arrive without one ("legacy-" + sha1 of "name|data_type|timestamp|username"),
and the queue is rewritten, so retries keep the id and a message an older
collector already delivered is recognised as a duplicate.
"""
import errno
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Iterable, List, Optional

try:
    import fcntl          # POSIX
except ImportError:       # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt         # Windows
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

# Default cap on queued messages (config "max_queue_messages"; 0 = no cap)
DEFAULT_MAX_QUEUE_MESSAGES = 50000

# Lock holds are short (a JSON read + write), so this only trips if another
# process is wedged — the OS releases the lock when a process dies.
LOCK_TIMEOUT_SECONDS = 120.0


def _errnos(*names):
    return frozenset(getattr(errno, n) for n in names if hasattr(errno, n))


# "Another process holds the lock": flock → EWOULDBLOCK/EAGAIN; fcntl-emulated
# flock → EACCES/EAGAIN; msvcrt.locking → EACCES (LK_NBLCK) or EDEADLOCK.
_LOCK_CONTENTION_ERRNOS = _errnos('EAGAIN', 'EWOULDBLOCK', 'EACCES', 'EDEADLK', 'EDEADLOCK')
# "This filesystem can't lock at all" (e.g. NFS without lockd, some CIFS/FUSE)
_LOCK_UNSUPPORTED_ERRNOS = _errnos('ENOLCK', 'EOPNOTSUPP', 'ENOTSUP', 'ENOSYS')
_unlocked_warned = set()


class QueueLockTimeout(TimeoutError):
    """The queue lock could not be acquired within the timeout."""


def new_msg_id() -> str:
    """Return a new message id (contract: uuid4().hex)."""
    return uuid.uuid4().hex


def legacy_msg_id(entry: dict, device_name: str = '') -> str:
    """
    Deterministic id for a message queued without one. Must stay identical to
    the Manager's derivation for msg_id-less messages (manager.py _legacy_id);
    device_name stands in for the batch "name" when the header lacks one.
    """
    header = entry.get('header') if isinstance(entry.get('header'), dict) else {}
    data = entry.get('data') if isinstance(entry.get('data'), dict) else {}
    parts = (
        header.get('device_name') or device_name or '',
        data.get('data_type') or '',
        header.get('timestamp') or '',
        data.get('username') or '',
    )
    key = '|'.join(str(p) for p in parts)
    return 'legacy-' + hashlib.sha1(key.encode('utf-8')).hexdigest()


def ensure_msg_id(entry: dict, device_name: str = '') -> bool:
    """Give entry a header.msg_id if it lacks one. Returns True if one was added."""
    header = entry.get('header')
    if header is None:
        header = entry['header'] = {}
    if not header.get('msg_id'):
        header['msg_id'] = legacy_msg_id(entry, device_name)
        return True
    return False


def msg_id_of(entry: dict) -> Optional[str]:
    header = entry.get('header')
    return header.get('msg_id') if isinstance(header, dict) else None


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class QueueLock:
    """
    Exclusive cross-process lock for one queue.json (context manager).

    Not re-entrant: a process must not acquire the lock for the same queue
    twice (flock/msvcrt locks are per open file, so it would wait on itself).
    """

    def __init__(self, queue_path: str, timeout: float = LOCK_TIMEOUT_SECONDS,
                 poll_interval: float = 0.05):
        self.lock_path = queue_path + '.lock'
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fh = None
        self._locked = False

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
        fh = _open_lock_file(self.lock_path)
        deadline = time.monotonic() + self.timeout
        locked = False
        while True:
            try:
                _lock_file(fh)
                locked = True
                break
            except OSError as e:
                if e.errno in _LOCK_UNSUPPORTED_ERRNOS:
                    _warn_unlocked(self.lock_path, e)
                    break   # carry on unlocked, as collectors before locking did
                if e.errno not in _LOCK_CONTENTION_ERRNOS:
                    fh.close()
                    raise   # a real error (EIO, EBADF, ...): report it, don't wait it out
                if time.monotonic() >= deadline:
                    fh.close()
                    raise QueueLockTimeout(
                        f"Timed out after {self.timeout:.0f}s waiting for {self.lock_path}"
                    )
                time.sleep(self.poll_interval)
        self._fh, self._locked = fh, locked
        return self

    def __exit__(self, exc_type, exc, tb):
        fh, self._fh = self._fh, None
        try:
            if self._locked:
                _unlock_file(fh)
        finally:
            fh.close()
        return False


def _open_lock_file(lock_path: str):
    """Open (creating if needed) the lock file. Its content is irrelevant; 'a+b' never truncates it."""
    try:
        return open(lock_path, 'a+b')
    except PermissionError:
        # Created by another account (e.g. a one-off run under sudo). flock and
        # msvcrt.locking work on a read-only handle as well.
        return open(lock_path, 'rb')


def _warn_unlocked(lock_path: str, err: OSError):
    if lock_path in _unlocked_warned:
        return
    _unlocked_warned.add(lock_path)
    logger.error(
        f"File locking is not supported on the filesystem holding {lock_path} ({err}); using "
        f"queue.json WITHOUT a lock. Overlapping collector runs (the daily disk run and the "
        f"5-minute metrics run) can then lose each other's queue updates; those messages stay in "
        f"the monthly archive. Put data_dir on a local filesystem to fix this."
    )


def _lock_file(fh):
    """Non-blocking exclusive lock; raises OSError if another process holds it."""
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - no known platform lacks both
        logger.warning("No file-locking primitive available; queue.json is unlocked")


def _unlock_file(fh):
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError as e:  # closing the handle releases the lock anyway
        logger.debug(f"Unlock of {fh.name} failed: {e}")


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------

def atomic_write_json(path: str, obj) -> None:
    """Write obj as compact JSON to path atomically (temp + fsync + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + os.path.basename(path) + '.',
                               suffix='.tmp')
    try:
        if hasattr(os, 'fchmod'):   # POSIX: mkstemp's 0600 → the mode open() would give
            os.fchmod(fd, _default_file_mode())
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _default_file_mode() -> int:
    """0o666 minus the process umask (single-threaded caller: umask is set and restored)."""
    umask = os.umask(0o022)
    os.umask(umask)
    return 0o666 & ~umask


def _replace_with_retry(src: str, dst: str, attempts: int = 5):
    # On Windows os.replace fails with PermissionError while any other process
    # (antivirus, backup agent, an old unlocked collector) has dst open.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _fsync_dir(directory: str):
    """Persist the rename itself (POSIX only; best-effort)."""
    if os.name != 'posix':
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sidecar_path(queue_path: str, kind: str) -> str:
    """queue.<kind>-<UTC timestamp>.json next to queue.json, never overwriting one."""
    directory = os.path.dirname(os.path.abspath(queue_path))
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    candidate = os.path.join(directory, f"queue.{kind}-{stamp}.json")
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"queue.{kind}-{stamp}-{n}.json")
        n += 1
    return candidate


def quarantine_corrupt_queue(queue_path: str, reason: str, log: logging.Logger = None) -> str:
    """
    Move an unparseable queue.json aside so it is neither lost nor re-read.
    Raises OSError if it can't be moved (the caller must then not write a
    fresh queue over it).
    """
    log = log or logger
    dest = _sidecar_path(queue_path, 'corrupt')
    os.replace(queue_path, dest)
    log.error(
        f"queue.json could not be parsed ({reason}); moved it to {dest}. "
        f"Its messages are still in the monthly archive files; starting a new queue."
    )
    return dest


# ---------------------------------------------------------------------------
# Queue operations (load_queue / save_queue / enqueue_locked need the lock held)
# ---------------------------------------------------------------------------

def load_queue(queue_path: str, log: logging.Logger = None) -> List[dict]:
    """
    Read queue.json. Caller must hold QueueLock.

    - Missing file → [].
    - Unparseable / not a JSON list → quarantined (see above) → [].
    - Entries without header.msg_id get one (see legacy_msg_id), and the
      queue is rewritten so the id sticks across retries.
    - Entries that are not message dicts are moved to a queue.corrupt-*.json
      sidecar (they can never be acknowledged by msg_id).
    """
    log = log or logger
    try:
        with open(queue_path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        return []

    if not raw.strip():
        # Only an interrupted non-atomic write by an older collector leaves this.
        log.warning(f"{queue_path} is empty; treating it as an empty queue")
        return []

    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError(f"expected a JSON list, got {type(entries).__name__}")
    except ValueError as e:  # includes JSONDecodeError and UnicodeDecodeError
        quarantine_corrupt_queue(queue_path, str(e)[:200], log)
        return []

    # queue.json lives in data/<device name>/
    device_name = os.path.basename(os.path.dirname(os.path.abspath(queue_path)))
    valid, invalid, changed = [], [], False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('header', {}), dict):
            invalid.append(entry)
            continue
        if ensure_msg_id(entry, device_name):
            changed = True
        valid.append(entry)

    if invalid:
        dest = _sidecar_path(queue_path, 'corrupt')
        atomic_write_json(dest, invalid)
        log.error(f"Moved {len(invalid)} malformed queue entr{'y' if len(invalid) == 1 else 'ies'} to {dest}")
        changed = True

    if changed:
        save_queue(queue_path, valid)
    return valid


def save_queue(queue_path: str, entries: List[dict]) -> None:
    """Atomically replace queue.json (deletes it when empty). Caller must hold QueueLock."""
    if entries:
        atomic_write_json(queue_path, entries)
    else:
        try:
            os.remove(queue_path)
        except FileNotFoundError:
            pass


def cap_queue(entries: List[dict], max_messages: int, log: logging.Logger = None) -> List[dict]:
    """Drop the oldest entries beyond max_messages (0/None = unlimited)."""
    log = log or logger
    if max_messages and len(entries) > max_messages:
        excess = len(entries) - max_messages
        log.warning(
            f"QUEUE BACKLOG OVER LIMIT: {len(entries)} messages queued, max_queue_messages="
            f"{max_messages}. Dropping the {excess} OLDEST from queue.json — they will not be "
            f"sent to the Manager (they remain in the local monthly archive files)."
        )
        return entries[excess:]
    return entries


def enqueue_locked(queue_path: str, new_entries: Iterable[dict],
                   max_messages: int = DEFAULT_MAX_QUEUE_MESSAGES,
                   log: logging.Logger = None) -> int:
    """Append messages to the queue. Caller must hold QueueLock. Returns queue length."""
    entries = load_queue(queue_path, log)
    for entry in new_entries:
        ensure_msg_id(entry)   # new messages already carry a uuid4 msg_id from build_header()
        entries.append(entry)
    entries = cap_queue(entries, max_messages, log)
    save_queue(queue_path, entries)
    return len(entries)


def enqueue(queue_path: str, new_entries: Iterable[dict],
            max_messages: int = DEFAULT_MAX_QUEUE_MESSAGES,
            log: logging.Logger = None) -> int:
    """Append messages to the queue under the lock. Returns queue length."""
    with QueueLock(queue_path):
        return enqueue_locked(queue_path, new_entries, max_messages, log)


def snapshot(queue_path: str, log: logging.Logger = None) -> List[dict]:
    """Return the current queue contents (read under the lock)."""
    with QueueLock(queue_path):
        return load_queue(queue_path, log)


def remove_acked(queue_path: str, msg_ids: Iterable[str], log: logging.Logger = None) -> int:
    """
    Remove the given msg_ids from the queue. Re-reads queue.json under the
    lock, so entries appended since the snapshot are kept. Returns the number
    of entries removed.
    """
    ids = set(msg_ids)
    if not ids:
        return 0
    with QueueLock(queue_path):
        entries = load_queue(queue_path, log)
        keep = [e for e in entries if msg_id_of(e) not in ids]
        removed = len(entries) - len(keep)
        if removed:
            save_queue(queue_path, keep)
        return removed
