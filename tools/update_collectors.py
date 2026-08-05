#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Updater

Reads a CSV of collector systems and runs 'git pull origin main' on each via SSH.

CSV format (one header row required):
    ip,username,password,git_path
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor
    192.168.1.43,john,secret456,/volume1/lab-monitor/scripts/lab-monitor
    atlantis.med.harvard.edu,admin,secret789,E:/Users/lab-monitor/scripts/lab-monitor

Usage:
    python3 update_collectors.py                        # uses collectors.csv in same dir
    python3 update_collectors.py --csv /path/to/file.csv
    python3 update_collectors.py --dry-run              # print commands without running
    python3 update_collectors.py --timeout 30           # SSH timeout in seconds

Requirements:
    pip install paramiko

Security note:
    Keep your CSV file out of version control. It is listed in .gitignore by default.
    Prefer SSH key auth where possible (leave password blank in CSV to use key auth).
"""

import argparse
import csv
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


# ---------------------------------------------------------------------------
# Verbose logging
# ---------------------------------------------------------------------------

_verbose = False

def vprint(msg: str):
    """Print if verbose mode is enabled."""
    if _verbose:
        print(f"  [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(ip: str, username: str, password: str, timeout: int):
    """Open an SSH connection. Uses key auth if password is blank."""
    auth = 'password' if password else 'key/agent'
    vprint(f"Connecting to {ip} as '{username}' (auth: {auth}, timeout: {timeout}s)")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=ip, username=username, timeout=timeout)
    if password:
        kwargs['password'] = password
    else:
        kwargs['look_for_keys'] = True
        kwargs['allow_agent']   = True
    client.connect(**kwargs)
    vprint(f"Connected to {ip}")
    return client


def run_remote(client, command: str, timeout: int) -> tuple:
    """Run a command remotely. Returns (stdout, stderr, exit_code)."""
    vprint(f"→ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    vprint(f"← exit {exit_code}")
    if out: vprint(f"  stdout: {out[:500]}{'...' if len(out) > 500 else ''}")
    if err: vprint(f"  stderr: {err[:500]}{'...' if len(err) > 500 else ''}")
    return out, err, exit_code


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

def update_system(row: dict, dry_run: bool, timeout: int) -> dict:
    """
    SSH into one system and run git pull.
    Returns a result dict with status, output, and timing.
    """
    ip       = row['ip'].strip()
    username = row['username'].strip()
    password = row.get('password', '').strip()
    git_path = row['git_path'].strip()

    result = {'ip': ip, 'status': None, 'output': '', 'error': '', 'elapsed': 0.0}

    # Windows paths: convert backslash to forward slash for the cd command
    git_path_cmd = git_path.replace('\\', '/')

    command = f'cd "{git_path_cmd}" && git pull origin main'

    if dry_run:
        result['status'] = 'dry-run'
        result['output'] = f'Would run: {command}'
        return result

    start = time.time()
    try:
        vprint(f"--- {ip} ---")
        client = ssh_connect(ip, username, password, timeout)
        try:
            stdout, stderr, exit_code = run_remote(client, command, timeout)
            result['elapsed'] = time.time() - start
            if exit_code == 0:
                result['status'] = 'ok'
                result['output'] = stdout or '(no output)'
            else:
                result['status'] = 'error'
                result['output'] = stdout
                result['error']  = stderr or f'exit code {exit_code}'
        finally:
            client.close()

    except Exception as e:
        result['elapsed'] = time.time() - start
        result['status']  = 'failed'
        result['error']   = str(e)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if paramiko is None:
        print("ERROR: paramiko is required. Install with: pip install paramiko")
        sys.exit(1)

    parser = argparse.ArgumentParser(description='Update lab-monitor collectors via SSH')
    parser.add_argument('--csv',     required=True, metavar='PATH', help='Path to collectors.csv inventory file')
    parser.add_argument('--dry-run', action='store_true',      help='Print commands without running')
    parser.add_argument('--timeout', type=int, default=30,     help='SSH timeout in seconds (default 30)')
    parser.add_argument('--verbose', action='store_true',      help='Log SSH connections, commands, and responses')
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

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
    missing  = required - set(reader.fieldnames or [])
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(missing)}")
        sys.exit(1)

    print("=" * 60)
    print(f"Lab Monitor - Remote Update")
    print(f"Systems: {len(rows)}  |  Timeout: {args.timeout}s  |  Dry-run: {args.dry_run}")
    print("=" * 60)
    print()

    results = []
    for i, row in enumerate(rows, 1):
        ip = row['ip'].strip()
        print(f"[{i}/{len(rows)}] {ip} ... ", end='', flush=True)
        result = update_system(row, dry_run=args.dry_run, timeout=args.timeout)
        results.append(result)

        if result['status'] == 'ok':
            print(f"[OK] ({result['elapsed']:.1f}s)")
            # Show git output on success (one line summary)
            for line in result['output'].splitlines():
                print(f"       {line}")
        elif result['status'] == 'dry-run':
            print(f"[DRY-RUN]")
            print(f"       {result['output']}")
        else:
            print(f"[FAILED]")
            if result['output']:
                print(f"       stdout: {result['output'][:200]}")
            if result['error']:
                print(f"       error:  {result['error'][:200]}")
        print()

    # Summary
    ok      = [r for r in results if r['status'] == 'ok']
    failed  = [r for r in results if r['status'] in ('error', 'failed')]
    dry     = [r for r in results if r['status'] == 'dry-run']

    print("=" * 60)
    print("Summary")
    print("=" * 60)
    if dry:
        print(f"  Dry-run:  {len(dry)}")
    else:
        print(f"  [OK]:     {len(ok)}")
        print(f"  Failed:   {len(failed)}")
        if failed:
            print()
            print("  Failed systems:")
            for r in failed:
                print(f"    {r['ip']} — {r['error']}")
    print()


if __name__ == '__main__':
    main()
