#!/usr/bin/env python3
"""
Lab Monitor - System Reset Tool

Run on the collector machine. Moves the local collector archives and queue
into a backup folder, then wipes the system from the Manager's databases.

The local JSONL archives are the source of truth: the backup can be sent
back to the Manager at any time with tools/replay_archives.py (the Manager
de-duplicates messages, so replaying is safe to repeat). Pass
--include-legacy so the messages from before the collector's de-duplication
upgrade are sent too (the Manager recognizes any it already holds).

The Manager's DELETE /api/system/<name> endpoint needs an ADMIN token
(manager config "admin_tokens"; a legacy "auth_tokens" token also works).
The collector's own manager_token is normally a collector token, which the
Manager refuses for DELETE (HTTP 403) — pass --admin-token or set
LAB_MONITOR_ADMIN_TOKEN. The token is checked BEFORE anything is moved, so
a token without admin rights (or an unreachable Manager) changes nothing.

Usage:
    python3 reset_system.py --config /path/to/config.json --admin-token <token>
    LAB_MONITOR_ADMIN_TOKEN=<token> python3 reset_system.py --config /path/to/config.json
    python3 reset_system.py --config /path/to/config.json --admin-token <token> --force   # skip confirmation

What it does:
    0. Check that the Manager is reachable and accepts the token for DELETE
    1. Take the collector's queue lock (queue.json.lock: flock on Linux/Synology,
       the msvcrt byte lock on Windows - the locks the collector itself takes),
       waiting up to 2 minutes for a running collector to release it. A
       collector too old to have a queue.json.lock doesn't lock; the reset then
       goes ahead with a warning (pause it first).
    2. Create a timestamped backup folder next to the existing archives
    3. Move all YYYY-MM.jsonl archive files into the backup folder
    4. Move queue.json and any queue.corrupt-*.json (queues the collector set
       aside as unreadable) into the backup folder; queue.json.lock is left in
       place (it holds no data)
    5. Send DELETE /api/system/<name> to the Manager
       - Deletes per-system history DB on the Manager
       - Clears this system from the central summary DB

What it does NOT touch:
    - config.json (never modified)
    - The backup folder itself (your data is safe)
    - Any other system's data on the Manager
"""

import argparse
import errno
import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

try:
    import fcntl          # POSIX
except ImportError:       # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt         # Windows
except ImportError:
    msvcrt = None

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Install with: pip install requests")
    sys.exit(1)


def load_config(config_path: str) -> dict:
    with open(config_path, 'r') as f:
        return json.load(f)


def find_archives(data_dir: str, name: str) -> list:
    """Find all YYYY-MM.jsonl archive files for this system."""
    system_dir = os.path.join(data_dir, name)
    if not os.path.isdir(system_dir):
        return []
    return sorted(
        str(p) for p in Path(system_dir).glob('????-??.jsonl')
    )


def find_queue(data_dir: str, name: str) -> Optional[str]:
    """Return queue.json path if it exists."""
    queue_path = os.path.join(data_dir, name, 'queue.json')
    return queue_path if os.path.exists(queue_path) else None


def find_corrupt_queues(data_dir: str, name: str) -> list:
    """queue.corrupt-*.json files: queues (or entries) the collector set aside as unreadable."""
    system_dir = os.path.join(data_dir, name)
    if not os.path.isdir(system_dir):
        return []
    return sorted(str(p) for p in Path(system_dir).glob('queue.corrupt-*.json') if p.is_file())


# How long to wait for a running collector to release queue.json.lock (it
# holds it only for a read + write of queue.json, never during a send)
QUEUE_LOCK_TIMEOUT_SECONDS = 120.0

# Same errno sets as collector/lib/local_queue.py
_LOCK_CONTENTION = frozenset(getattr(errno, n) for n in
                             ('EAGAIN', 'EWOULDBLOCK', 'EACCES', 'EDEADLK', 'EDEADLOCK') if hasattr(errno, n))
_LOCK_UNSUPPORTED = frozenset(getattr(errno, n) for n in
                              ('ENOLCK', 'EOPNOTSUPP', 'ENOTSUP', 'ENOSYS') if hasattr(errno, n))


class QueueLockTimeout(Exception):
    """A running collector kept queue.json.lock for longer than the timeout."""


def _try_lock(fh):
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(fh):
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass   # closing the file releases it anyway


@contextmanager
def queue_lock(system_dir: str, timeout: Optional[float] = None):
    """
    Hold the collector's queue lock while queue files are moved: an exclusive
    lock on queue.json.lock, taken the way collector/lib/local_queue.py takes
    it (flock on POSIX, msvcrt.locking on byte 0 on Windows), so a collector
    run can't rewrite queue.json in the middle of the move.

    Yields a status: 'held', or why the move goes ahead without the lock
    (no queue.json.lock = a collector that predates queue locking; a
    filesystem without lock support).  Raises QueueLockTimeout if a
    collector keeps the lock for longer than `timeout` seconds.
    """
    if timeout is None:
        timeout = QUEUE_LOCK_TIMEOUT_SECONDS
    lock_path = os.path.join(system_dir, 'queue.json.lock')
    if not os.path.exists(lock_path):
        yield 'none (no queue.json.lock: the collector predates queue locking)'
        return
    if fcntl is None and msvcrt is None:   # pragma: no cover - no known platform
        yield 'unavailable (no file locking on this platform)'
        return
    try:
        fh = open(lock_path, 'a+b')
    except PermissionError:
        fh = open(lock_path, 'rb')   # flock / msvcrt.locking work on a read-only handle too
    try:
        deadline = time.monotonic() + timeout
        status = 'held'
        while True:
            try:
                _try_lock(fh)
                break
            except OSError as e:
                if e.errno in _LOCK_UNSUPPORTED:
                    status = f'unavailable (this filesystem has no file locking: {e})'
                    break
                if e.errno not in _LOCK_CONTENTION:
                    raise
                if time.monotonic() >= deadline:
                    raise QueueLockTimeout(
                        f"{lock_path} is held by a running collector (waited {timeout:.0f}s)")
                time.sleep(0.2)
        try:
            yield status
        finally:
            if status == 'held':
                _unlock(fh)
    finally:
        fh.close()


def backup_and_clear(data_dir: str, name: str) -> str:
    """
    Move archives, queue.json and queue.corrupt-*.json into a timestamped
    backup folder while holding the collector's queue lock (queue_lock).
    queue.json.lock is left in place.  Returns the backup folder path.
    Raises QueueLockTimeout (before anything is moved) if a collector keeps
    the lock.
    """
    system_dir = os.path.join(data_dir, name)
    with queue_lock(system_dir) as lock_status:
        if lock_status != 'held':
            print(f"  WARNING: moving the queue without the collector's queue lock: {lock_status}.")
            print("           Make sure the collector is not running.")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(system_dir, f'backup_{timestamp}')
        os.makedirs(backup_dir, exist_ok=True)

        moved = []
        for src in find_archives(data_dir, name):
            shutil.move(src, os.path.join(backup_dir, os.path.basename(src)))
            moved.append(os.path.basename(src))
            print(f"  Archived: {os.path.basename(src)} → backup_{timestamp}/")

        queue = find_queue(data_dir, name)
        for src in ([queue] if queue else []) + find_corrupt_queues(data_dir, name):
            shutil.move(src, os.path.join(backup_dir, os.path.basename(src)))
            moved.append(os.path.basename(src))
            print(f"  Archived: {os.path.basename(src)} → backup_{timestamp}/")

    if not moved:
        print("  (no archive files or queue found)")

    return backup_dir


def check_admin_token(manager_url: str, token: str) -> tuple:
    """
    Before anything is moved, check the token may DELETE on the Manager.
    Returns (ok, message).

    New Manager: GET /api/dead_letter is admin-only (200 vs 401/403), the same
    scope as DELETE /api/system/<name>. Older Manager (no such route, 404):
    every valid token had full rights, so GET /api/systems just checks it is valid.
    """
    headers = {'Authorization': f'Bearer {token}'}
    base = manager_url.rstrip('/')
    try:
        resp = requests.get(f"{base}/api/dead_letter", params={'limit': 1},
                            headers=headers, timeout=30)
        if resp.status_code == 404:
            resp = requests.get(f"{base}/api/systems", headers=headers, timeout=30)
            if resp.status_code == 200:
                return True, "token accepted (older Manager: every valid token may delete)"
    except Exception as e:
        return False, f"could not reach the Manager at {manager_url}: {e}"
    if resp.status_code == 200:
        return True, "admin token accepted"
    if resp.status_code in (401, 403):
        return False, (f"the Manager refused this token for admin requests (HTTP {resp.status_code}). "
                       "DELETE /api/system/<name> needs an ADMIN token (manager config "
                       "'admin_tokens'): pass --admin-token or set LAB_MONITOR_ADMIN_TOKEN.")
    return False, f"unexpected answer from the Manager (HTTP {resp.status_code}) while checking the token"


def wipe_manager(manager_url: str, token: str, name: str) -> bool:
    """
    Send DELETE /api/system/<name> to the Manager.
    Returns True on success.
    """
    url = f"{manager_url.rstrip('/')}/api/system/{quote(name, safe='')}"
    try:
        resp = requests.delete(
            url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Manager wiped: per_system_db_deleted={data.get('per_system_db_deleted')}, "
                  f"central_rows_cleared={data.get('central_db_rows_cleared')}")
            return True
        elif resp.status_code in (401, 403):
            print(f"  ERROR: Manager returned {resp.status_code}: {resp.text[:200]}")
            print("         DELETE /api/system/<name> requires an ADMIN token (manager config")
            print("         'admin_tokens'). Pass --admin-token or set LAB_MONITOR_ADMIN_TOKEN.")
            return False
        else:
            print(f"  ERROR: Manager returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  ERROR: Could not reach Manager: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Reset a lab-monitor collector system — archive local data and wipe Manager records'
    )
    parser.add_argument('--config', required=True, help='Path to collector config.json')
    parser.add_argument('--admin-token', metavar='TOKEN',
                        help='Manager ADMIN token for DELETE /api/system/<name> '
                             '(default: $LAB_MONITOR_ADMIN_TOKEN, else the config manager_token, '
                             'which only works if it is an admin/legacy token)')
    parser.add_argument('--manager-url', metavar='URL',
                        help='Manager base URL (default: manager_url from the config)')
    parser.add_argument('--force',  action='store_true', help='Skip confirmation prompt')
    args = parser.parse_args()

    # Load config
    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)
    name        = config.get('name') or config.get('nas_name')
    data_dir    = config.get('data_dir')
    manager_url = args.manager_url or config.get('manager_url')
    token       = args.admin_token or os.environ.get('LAB_MONITOR_ADMIN_TOKEN')
    token_source = 'admin token'
    if not token:
        token = config.get('manager_token')
        token_source = "config manager_token (needs admin rights on the Manager)"

    if not all([name, data_dir, manager_url, token]):
        print("ERROR: Config missing required fields (name, data_dir, manager_url, manager_token)")
        sys.exit(1)

    # Inventory what will be affected
    archives = find_archives(data_dir, name)
    queue    = find_queue(data_dir, name)
    corrupt  = find_corrupt_queues(data_dir, name)
    system_dir = os.path.join(data_dir, name)

    print()
    print("=" * 60)
    print(f"Lab Monitor - System Reset: {name}")
    print("=" * 60)
    print()
    print("This will:")
    print(f"  1. Move archives + queue into a timestamped backup folder")
    print(f"     Location: {system_dir}/backup_YYYYMMDD_HHMMSS/")
    print(f"     Archives found: {len(archives)}")
    if archives:
        for a in archives:
            print(f"       - {os.path.basename(a)}")
    if queue:
        print(f"       - queue.json")
    for c in corrupt:
        print(f"       - {os.path.basename(c)}")
    print()
    print(f"  2. DELETE /api/system/{name} on Manager ({manager_url})")
    print(f"     - Deletes per-system history database")
    print(f"     - Removes '{name}' from central summary database")
    print(f"     - Authenticates with: {token_source}")
    print()
    print("What is NOT touched:")
    print("  - config.json")
    print("  - The backup folder (your archives are safe)")
    print()
    print("WARNING: The Manager wipe is irreversible from the Manager side.")
    print("         You can rebuild from the backup archives if needed.")
    print()

    # Check the token BEFORE moving anything: a collector/read token would
    # otherwise leave the system half reset (local data moved, Manager intact).
    ok, message = check_admin_token(manager_url, token)
    if not ok:
        print(f"ERROR: {message}")
        print("Nothing was changed.")
        sys.exit(1)
    print(f"Manager token check: {message}")
    print()

    # Confirmation
    if not args.force:
        try:
            answer = input(f"Type the system name '{name}' to confirm reset, or Ctrl-C to cancel: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)

        if answer != name:
            print(f"ERROR: '{answer}' does not match '{name}'. Aborting.")
            sys.exit(1)

    print()
    print("Step 1: Archiving local data...")
    try:
        backup_dir = backup_and_clear(data_dir, name)
    except QueueLockTimeout as e:
        print(f"ERROR: {e}.")
        print("Nothing was changed. Retry when the collector run has finished, or pause it")
        print(f"(\"active\": false in {args.config}) first.")
        sys.exit(1)
    print(f"  Backup saved to: {backup_dir}")
    print()

    print("Step 2: Wiping Manager records...")
    ok = wipe_manager(manager_url, token, name)
    print()

    print("=" * 60)
    if ok:
        print(f"[OK] Reset complete for '{name}'")
        print()
        print("Next steps:")
        print(f"  - The collector will start fresh on next run")
        print(f"  - To rebuild the Manager from the backup archives (safe to repeat;")
        print(f"    the Manager skips messages it already has):")
        print(f"      1. Pause the collector (\"active\": false in {args.config},")
        print(f"         or tools/toggle_collectors.py --mode off)")
        print(f"      2. python3 tools/replay_archives.py --config {args.config} --include-legacy \\")
        print(f"             --source {backup_dir}")
        print(f"      3. Resume it (\"active\": true / --mode on). Pausing it during the replay")
        print(f"         lets the lifetime network totals be rebuilt in order: the Manager adds")
        print(f"         nothing for messages older than the newest network reading it has.")
    else:
        print(f"[PARTIAL] Local archives moved to backup, but Manager wipe failed.")
        print("  Check Manager logs and retry, or delete manually with an ADMIN token via:")
        print(f"    curl -X DELETE -H 'Authorization: Bearer <admin-token>' {manager_url}/api/system/{name}")
    print()

    sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
