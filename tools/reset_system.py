#!/usr/bin/env python3
"""
Lab Monitor - System Reset Tool

Run on the collector machine. Moves the local collector archives and queue
into a backup folder, then wipes the system from the Manager's databases.

The local JSONL archives are the source of truth: the backup can be sent
back to the Manager at any time with tools/replay_archives.py (the Manager
de-duplicates messages, so replaying is safe to repeat). Pass
--include-legacy so the messages from before the collector's de-duplication
upgrade are sent too — safe here because the system was wiped.

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
    1. Create a timestamped backup folder next to the existing archives
    2. Move all YYYY-MM.jsonl archive files into the backup folder
    3. Move queue.json (if present) into the backup folder
    4. Send DELETE /api/system/<name> to the Manager
       - Deletes per-system history DB on the Manager
       - Clears this system from the central summary DB

What it does NOT touch:
    - config.json (never modified)
    - The backup folder itself (your data is safe)
    - Any other system's data on the Manager
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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


def backup_and_clear(data_dir: str, name: str) -> str:
    """
    Move archives and queue into a timestamped backup folder.
    Returns the backup folder path.
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(data_dir, name, f'backup_{timestamp}')
    os.makedirs(backup_dir, exist_ok=True)

    archives = find_archives(data_dir, name)
    queue    = find_queue(data_dir, name)

    moved = []
    for src in archives:
        dst = os.path.join(backup_dir, os.path.basename(src))
        shutil.move(src, dst)
        moved.append(os.path.basename(src))
        print(f"  Archived: {os.path.basename(src)} → backup_{timestamp}/")

    if queue:
        dst = os.path.join(backup_dir, 'queue.json')
        shutil.move(queue, dst)
        moved.append('queue.json')
        print(f"  Archived: queue.json → backup_{timestamp}/")

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
    backup_dir = backup_and_clear(data_dir, name)
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
        print(f"      3. Resume it (\"active\": true / --mode on) and run it once in metrics and")
        print(f"         disk mode, so the dashboard card shows current values again (the")
        print(f"         Manager shows the last message it stored, even an older replayed one)")
    else:
        print(f"[PARTIAL] Local archives moved to backup, but Manager wipe failed.")
        print("  Check Manager logs and retry, or delete manually with an ADMIN token via:")
        print(f"    curl -X DELETE -H 'Authorization: Bearer <admin-token>' {manager_url}/api/system/{name}")
    print()

    sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
