#!/usr/bin/env python3
"""
Lab Monitor - Remote Archive All Collectors

Archive all YYYY-MM.jsonl files on all collectors by moving them to
timestamped backup folders. Useful for refreshing the collector archives
and Manager history without deleting data.

CSV format (one header row required):
    ip,username,password,git_path
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor
    atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor

Usage:
    python3 archive_all_collectors.py --csv collectors.csv
    python3 archive_all_collectors.py --csv collectors.csv --dry-run
    python3 archive_all_collectors.py --csv collectors.csv --timeout 60 --verbose
    python3 archive_all_collectors.py --csv collectors.csv --also-delete-queues

What it does:
    1. For each collector, SSHes in
    2. Reads the collector's config to find data_dir and system name
    3. Creates a timestamped backup folder (backup_YYYYMMDD_HHMMSS)
    4. Moves all YYYY-MM.jsonl archive files into the backup folder
    5. Optionally deletes queue.json to force fresh sync
    6. Shows backup location and file counts

What it does NOT do:
    - Does NOT wipe the Manager (unlike reset_system.py)
    - Does NOT delete config.json or any other files
    - Does NOT modify collectors.csv

Why archive instead of delete:
    - Backup folders are safe indefinitely
    - Can recover data if needed via manual import/replay
    - Seamless refresh of collector history without data loss

Requirements:
    pip install paramiko

Note:
    - Keep collectors.csv out of version control
    - Always --dry-run first to preview what will happen
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko is required. Install with: pip install paramiko")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

_verbose = False
_dry_run = False


def vprint(msg: str):
    """Print if verbose mode is enabled."""
    if _verbose:
        print(f"    [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(ip: str, username: str, password: str, timeout: int):
    """Open an SSH connection. Uses key auth if password is blank."""
    auth = 'password' if password else 'key/agent'
    vprint(f"Connecting as '{username}' (auth: {auth}, timeout: {timeout}s)")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=ip, username=username, timeout=timeout)
    if password:
        kwargs['password'] = password
    else:
        kwargs['look_for_keys'] = True
        kwargs['allow_agent'] = True
    client.connect(**kwargs)
    vprint(f"Connected")
    return client


def run_remote(client, command: str, timeout: int) -> tuple:
    """Run a command remotely. Returns (stdout, stderr, exit_code)."""
    vprint(f"→ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    vprint(f"← exit {exit_code}")
    if out:
        vprint(f"  out: {out[:300]}{'...' if len(out) > 300 else ''}")
    if err:
        vprint(f"  err: {err[:300]}{'...' if len(err) > 300 else ''}")
    return out, err, exit_code


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

def get_config(client, git_path: str, timeout: int) -> tuple:
    """
    Fetch collector config via config_tool.py.
    Returns (config_dict, None) on success, (None, error_str) on failure.
    """
    vprint(f"Reading config from {git_path}/collector/local/config.json")
    command = f'cd "{git_path}/collector" && python3 config_tool.py --config local/config.json list --json'
    
    out, err, code = run_remote(client, command, timeout)
    
    if code != 0:
        return None, f"config_tool.py failed (exit {code}): {err}"
    
    try:
        response = json.loads(out)
        # config_tool.py returns {"status": "ok", "config": {...}}
        if isinstance(response, dict) and 'config' in response:
            return response['config'], None
        # Fallback: if it's a plain config dict (shouldn't happen, but be safe)
        return response, None
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from config_tool: {e}"


# ---------------------------------------------------------------------------
# Archive logic
# ---------------------------------------------------------------------------

def archive_collector(client, git_path: str, config: dict, timeout: int) -> tuple:
    """
    Archive all YYYY-MM.jsonl files and optionally queue.json on a remote collector.
    
    Returns:
        (success: bool, message: str, backup_dir: str, files_moved: int)
    """
    name = config.get('name')
    data_dir = config.get('data_dir')
    
    if not name or not data_dir:
        return False, "Missing 'name' or 'data_dir' in config", "", 0
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_folder = f'backup_{timestamp}'
    
    # Determine OS (Windows paths contain : or \, Unix paths don't)
    is_windows = ':' in data_dir or '\\' in data_dir
    
    if is_windows:
        # Windows path
        system_dir = f'{data_dir}\\{name}'
        backup_dir = f'{system_dir}\\{backup_folder}'
        # PowerShell commands for Windows
        commands = [
            f'cd "{system_dir}"',
            f'if (!(Test-Path "{backup_folder}")) {{ mkdir "{backup_folder}" | Out-Null }}',
            f'$count = 0',
            f'Get-Item "????-??.jsonl" -ErrorAction SilentlyContinue | ForEach-Object {{ Move-Item $_.FullName "{backup_dir}\\$($_.Name)"; $count += 1 }}',
            f'Write-Output "MOVED: $count"'
        ]
        command = ' ; '.join(commands)
    else:
        # Unix path (Synology/Linux)
        system_dir = f'{data_dir}/{name}'
        backup_dir = f'{system_dir}/{backup_folder}'
        # Bash commands for Unix
        commands = [
            f'cd "{system_dir}"',
            f'mkdir -p "{backup_folder}"',
            f'count=$(ls -1 ????-??.jsonl 2>/dev/null | wc -l)',
            f'ls -1 ????-??.jsonl 2>/dev/null | while read f; do mv "$f" "{backup_folder}/$f"; done',
            f'echo "MOVED: $count"'
        ]
        command = ' ; '.join(commands)
    
    if _dry_run:
        vprint(f"[DRY-RUN] Would execute: {command}")
        return True, "[DRY-RUN] Archives would be moved", backup_dir, 0
    
    vprint(f"Creating backup folder: {backup_folder}")
    out, err, code = run_remote(client, command, timeout)
    
    if code != 0:
        return False, f"Archive command failed (exit {code}): {err}", backup_dir, 0
    
    # Extract file count from output
    try:
        for line in out.split('\n'):
            if 'MOVED:' in line:
                count = int(line.split(':')[1].strip())
                return True, f"Moved {count} archive file(s) to {backup_folder}", backup_dir, count
    except (ValueError, IndexError):
        pass
    
    return True, f"Archive completed (see backup: {backup_folder})", backup_dir, 0


def archive_queue_if_requested(client, git_path: str, config: dict, timeout: int) -> tuple:
    """
    Optionally delete queue.json to force fresh sync.
    Returns (success: bool, message: str)
    """
    name = config.get('name')
    data_dir = config.get('data_dir')
    
    if not name or not data_dir:
        return False, "Missing 'name' or 'data_dir'"
    
    is_windows = ':' in data_dir or '\\' in data_dir
    
    if is_windows:
        queue_path = f'{data_dir}\\{name}\\queue.json'
        command = f'if (Test-Path "{queue_path}") {{ Remove-Item "{queue_path}"; Write-Output "queue.json deleted" }} else {{ Write-Output "queue.json not found" }}'
    else:
        queue_path = f'{data_dir}/{name}/queue.json'
        command = f'rm -f "{queue_path}" && echo "queue.json deleted" || echo "queue.json not found"'
    
    if _dry_run:
        vprint(f"[DRY-RUN] Would delete queue: {queue_path}")
        return True, "[DRY-RUN] queue.json would be deleted"
    
    vprint(f"Deleting queue: {queue_path}")
    out, err, code = run_remote(client, command, timeout)
    
    return code == 0, out.strip() if out else err.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Archive all collectors — move YYYY-MM.jsonl files to backup folders'
    )
    parser.add_argument(
        '--csv',
        default='collectors.csv',
        help='Path to collectors.csv (default: collectors.csv)'
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=60,
        help='SSH timeout in seconds (default: 60)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview commands without executing'
    )
    parser.add_argument(
        '--also-delete-queues',
        action='store_true',
        help='Also delete queue.json on each collector (forces fresh sync)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output'
    )
    args = parser.parse_args()

    global _verbose, _dry_run
    _verbose = args.verbose
    _dry_run = args.dry_run

    # Check paramiko
    if paramiko is None:
        print("ERROR: paramiko is required. Install with: pip install paramiko")
        sys.exit(1)

    # Load CSV
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    try:
        with open(csv_path, 'r') as f:
            readers = csv.DictReader(f)
            collectors = list(readers)
    except Exception as e:
        print(f"ERROR: Failed to read CSV: {e}")
        sys.exit(1)

    if not collectors:
        print("ERROR: CSV is empty (no collectors found)")
        sys.exit(1)

    print()
    print("=" * 70)
    print("Lab Monitor - Archive All Collectors")
    print("=" * 70)
    print()
    if _dry_run:
        print("[DRY-RUN MODE] - No changes will be made")
        print()
    
    print(f"Collectors: {len(collectors)}")
    print(f"Timeout: {args.timeout}s")
    print(f"Also delete queues: {args.also_delete_queues}")
    print()

    # Process each collector
    results = []
    for i, collector in enumerate(collectors, 1):
        ip = collector.get('ip')
        username = collector.get('username')
        password = collector.get('password', '')
        git_path = collector.get('git_path')

        if not all([ip, username, git_path]):
            print(f"[{i}/{len(collectors)}] {ip} ... [SKIP] Missing required fields")
            results.append((ip, False, "Missing fields", ""))
            continue

        print(f"[{i}/{len(collectors)}] {ip} ", end='', flush=True)
        
        try:
            # Connect
            client = ssh_connect(ip, username, password, args.timeout)
            vprint(f"Connected to {ip}")

            # Get config
            config, err = get_config(client, git_path, args.timeout)
            if err:
                print(f"[ERROR] {err}")
                results.append((ip, False, f"Config error: {err}", ""))
                client.close()
                continue

            collector_name = config.get('name', '?')

            # Archive
            success, msg, backup_dir, count = archive_collector(
                client, git_path, config, args.timeout
            )
            if not success:
                print(f"[ERROR] {msg}")
                results.append((ip, False, msg, ""))
                client.close()
                continue

            # Optionally delete queue
            if args.also_delete_queues:
                q_success, q_msg = archive_queue_if_requested(client, git_path, config, args.timeout)
                if not q_success:
                    vprint(f"Queue deletion warning: {q_msg}")

            print(f"[OK] {collector_name}: {msg}")
            results.append((ip, True, msg, backup_dir))
            client.close()

        except paramiko.AuthenticationException as e:
            print(f"[AUTH ERROR] {e}")
            results.append((ip, False, f"Auth failed: {e}", ""))
        except paramiko.SSHException as e:
            print(f"[SSH ERROR] {e}")
            results.append((ip, False, f"SSH error: {e}", ""))
        except Exception as e:
            print(f"[ERROR] {e}")
            results.append((ip, False, str(e), ""))

    # Summary
    print()
    print("=" * 70)
    success_count = sum(1 for _, ok, _, _ in results if ok)
    print(f"Summary: {success_count}/{len(collectors)} collectors archived successfully")
    print("=" * 70)
    print()

    # Show successful backups
    if success_count > 0:
        print("Backup locations:")
        for ip, ok, msg, backup_dir in results:
            if ok and backup_dir:
                print(f"  {ip:30} → {backup_dir}")
        print()

    # Show failures
    failures = [(ip, msg) for ip, ok, msg, _ in results if not ok]
    if failures:
        print("Failures:")
        for ip, msg in failures:
            print(f"  {ip:30} {msg}")
        print()

    # Exit code
    sys.exit(0 if success_count == len(collectors) else 2)


if __name__ == '__main__':
    main()
