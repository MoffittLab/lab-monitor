#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Toggle

Toggle the 'active' field on all remote collectors to on or off.
Reads collector locations from collectors.csv, SSHes into each,
uses config_tool.py to manage the 'active' field, and displays
before/after config state for audit/verification.

CSV format (one header row required):
    ip,username,password,git_path
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor
    atlantis.med.harvard.edu,admin,secret789,E:/Users/lab-monitor/scripts/lab-monitor

Usage:
    python3 toggle_collectors.py --mode off                    # pause all
    python3 toggle_collectors.py --mode on                     # resume all
    python3 toggle_collectors.py --mode off --csv /path/to.csv # custom CSV
    python3 toggle_collectors.py --mode off --dry-run          # preview
    python3 toggle_collectors.py --mode off --timeout 30       # SSH timeout

Requirements:
    pip install paramiko

Note:
    - If 'active' field doesn't exist, it will be created
    - Full config (with tokens masked) is shown before and after each change
    - Keep collectors.csv out of version control
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(ip: str, username: str, password: str, timeout: int):
    """Open an SSH connection. Uses key auth if password is blank."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=ip, username=username, timeout=timeout)
    if password:
        kwargs['password'] = password
    else:
        kwargs['look_for_keys'] = True
        kwargs['allow_agent']   = True
    client.connect(**kwargs)
    return client


def run_remote(client, command: str, timeout: int) -> tuple:
    """Run a command remotely. Returns (stdout, stderr, exit_code)."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode().strip(), stderr.read().decode().strip(), exit_code


# ---------------------------------------------------------------------------
# Config management via config_tool.py
# ---------------------------------------------------------------------------

def get_config(client, git_path: str, timeout: int) -> tuple:
    """
    Fetch full config via config_tool.py list --json.
    Returns (config_dict, None) on success, (None, error_str) on failure.
    """
    command = f'cd "{git_path}/collector" && python3 config_tool.py --config config.json list --json'
    try:
        stdout, stderr, exit_code = run_remote(client, command, timeout)
        if exit_code != 0:
            return None, f'exit {exit_code} | stderr: {stderr!r} | stdout: {stdout!r}'
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return None, f'JSON parse error | stdout: {stdout!r}'
        if result.get('status') == 'ok':
            return result.get('config', {}), None
        return None, f'config_tool error: {result.get("error", "unknown")}'
    except Exception as e:
        return None, f'exception: {e}'


def set_active(client, git_path: str, value: bool, timeout: int) -> tuple:
    """
    Set active field via config_tool.py set.
    Returns (True, None) on success, (False, error_str) on failure.
    """
    value_str = 'true' if value else 'false'
    command = f'cd "{git_path}/collector" && python3 config_tool.py --config config.json set active {value_str} --json'
    try:
        stdout, stderr, exit_code = run_remote(client, command, timeout)
        if exit_code != 0:
            return False, f'exit {exit_code} | stderr: {stderr!r} | stdout: {stdout!r}'
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return False, f'JSON parse error | stdout: {stdout!r}'
        if result.get('status') == 'ok':
            return True, None
        return False, f'config_tool error: {result.get("error", "unknown")}'
    except Exception as e:
        return False, f'exception: {e}'


# ---------------------------------------------------------------------------
# Toggle logic
# ---------------------------------------------------------------------------

def toggle_system(row: dict, mode: str, dry_run: bool, timeout: int) -> dict:
    """
    SSH into one collector and toggle the 'active' field.
    Returns result dict with before/after config and status.
    """
    ip       = row['ip'].strip()
    username = row['username'].strip()
    password = row.get('password', '').strip()
    git_path = row['git_path'].strip()

    result = {
        'ip': ip,
        'status': None,
        'config_before': None,
        'config_after': None,
        'error': '',
        'elapsed': 0.0
    }

    target_value = mode == 'on'
    mode_display = 'ON' if target_value else 'OFF'

    if dry_run:
        result['status'] = 'dry-run'
        result['error'] = f'Would toggle to {mode_display}'
        return result

    start = time.time()
    try:
        client = ssh_connect(ip, username, password, timeout)
        try:
            # Get before-state
            config_before, err = get_config(client, git_path, timeout)
            if config_before is None:
                result['status'] = 'failed'
                result['error'] = f'Could not fetch config before change: {err}'
                result['elapsed'] = time.time() - start
                return result

            result['config_before'] = config_before

            # Set active field
            ok, err = set_active(client, git_path, target_value, timeout)
            if not ok:
                result['status'] = 'failed'
                result['error'] = f'Could not set active field: {err}'
                result['elapsed'] = time.time() - start
                return result

            # Get after-state
            config_after, err = get_config(client, git_path, timeout)
            if config_after is None:
                result['status'] = 'failed'
                result['error'] = f'Could not fetch config after change: {err}'
                result['elapsed'] = time.time() - start
                return result

            result['config_after'] = config_after
            result['status'] = 'ok'
            result['elapsed'] = time.time() - start

        finally:
            client.close()

    except Exception as e:
        result['elapsed'] = time.time() - start
        result['status'] = 'failed'
        result['error'] = str(e)

    return result


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def display_config(config: dict, title: str, indent: int = 4):
    """Pretty-print a config dict."""
    prefix = ' ' * indent
    print(f"{prefix}{title}")
    for k, v in sorted(config.items()):
        if isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, (list, dict)):
            v = json.dumps(v)
        print(f"{prefix}  {k}: {v}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if paramiko is None:
        print("ERROR: paramiko is required. Install with: pip install paramiko")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Toggle active flag on all remote collectors',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--mode', required=True, choices=['on', 'off'],
                        help='Toggle collectors on or off')
    parser.add_argument('--csv', required=True, metavar='PATH',
                        help='Path to collectors.csv inventory file')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview without making changes')
    parser.add_argument('--timeout', type=int, default=30,
                        help='SSH timeout in seconds (default: 30)')
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    # Read CSV
    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("ERROR: CSV file is empty or has no data rows")
        sys.exit(1)

    required = {'ip', 'username', 'git_path'}
    missing = required - set(reader.fieldnames or [])
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(missing)}")
        sys.exit(1)

    mode_display = 'ON' if args.mode == 'on' else 'OFF'
    print("=" * 70)
    print(f"Lab Monitor - Toggle Collectors {mode_display}")
    print(f"Systems: {len(rows)}  |  Timeout: {args.timeout}s  |  Dry-run: {args.dry_run}")
    print("=" * 70)
    print()

    results = []
    for i, row in enumerate(rows, 1):
        ip = row['ip'].strip()
        print(f"[{i}/{len(rows)}] {ip} ... ", end='', flush=True)
        result = toggle_system(row, mode=args.mode, dry_run=args.dry_run, timeout=args.timeout)
        results.append(result)

        if result['status'] == 'ok':
            print(f"[OK] ({result['elapsed']:.1f}s)")
            print()
            display_config(result['config_before'], "Before:")
            print()
            display_config(result['config_after'], "After:")
            print()
        elif result['status'] == 'dry-run':
            print(f"[DRY-RUN] {result['error']}")
            print()
        else:
            print(f"[FAILED] {result['error']}")
            if result['config_before']:
                print()
                display_config(result['config_before'], "Config (before attempt):")
            print()

    # Summary
    ok = [r for r in results if r['status'] == 'ok']
    failed = [r for r in results if r['status'] in ('error', 'failed')]
    dry = [r for r in results if r['status'] == 'dry-run']

    print("=" * 70)
    print("Summary")
    print("=" * 70)
    if dry:
        print(f"  Dry-run: {len(dry)} collectors would be toggled {mode_display}")
    else:
        print(f"  [OK]:    {len(ok)} collectors toggled {mode_display}")
        print(f"  Failed:  {len(failed)}")
        if failed:
            print()
            print("  Failed systems:")
            for r in failed:
                print(f"    {r['ip']} — {r['error']}")
    print()


if __name__ == '__main__':
    main()
