#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Execution

Trigger collection events on remote collectors by SSHing in and running
the collector script in disk or metrics mode.

CSV format (one header row required):
    ip,username,password,git_path,python_path
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
    atlantis.med.harvard.edu,admin,secret789,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe

Usage:
    python3 run_collectors.py --mode disk              # run disk collection on all
    python3 run_collectors.py --mode metrics           # run metrics collection on all
    python3 run_collectors.py --mode disk --dry-run    # preview commands
    python3 run_collectors.py --mode disk --timeout 60 # custom SSH timeout

Requirements:
    pip install paramiko

Note:
    - Keep collectors.csv out of version control
    - python_path can be absolute or relative to the git_path directory
    - Collection output is captured and displayed per system
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
        kwargs['allow_agent'] = True
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
# Collection execution
# ---------------------------------------------------------------------------

def run_collection(row: dict, mode: str, dry_run: bool, timeout: int) -> dict:
    """
    SSH into one collector and run the collector script in the specified mode.
    Returns result dict with status, output, error, and timing.
    """
    ip = row['ip'].strip()
    username = row['username'].strip()
    password = row.get('password', '').strip()
    git_path = row['git_path'].strip()
    python_path = row['python_path'].strip()

    result = {
        'ip': ip,
        'status': None,
        'output': '',
        'error': '',
        'elapsed': 0.0,
    }

    # Construct the collection command
    # Keep paths as-is from CSV (Windows or Unix); just quote them properly
    # Format: cd {git_path} && {python_path} collector/collector.py --config collector/local/config.json --mode {mode}
    command = f'cd "{git_path}" && "{python_path}" collector/collector.py --config collector/local/config.json --mode {mode}'

    if dry_run:
        result['status'] = 'dry-run'
        result['output'] = f'Would run: {command}'
        return result

    start = time.time()
    try:
        vprint(f"--- {ip} ---")
        vprint(f"Mode: {mode} | python: {python_path}")
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
                result['error'] = stderr or f'exit code {exit_code}'

        finally:
            client.close()

    except Exception as e:
        result['elapsed'] = time.time() - start
        result['status'] = 'failed'
        result['error'] = str(e)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if paramiko is None:
        print("ERROR: paramiko is required. Install with: pip install paramiko")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Run collection events on remote collectors',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--mode', required=True, choices=['disk', 'metrics'],
                        help='Collection mode: disk (folder usage) or metrics (CPU/RAM/network)')
    parser.add_argument('--csv', required=True, metavar='PATH',
                        help='Path to collectors.csv inventory file')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview commands without running')
    parser.add_argument('--timeout', type=int, default=60,
                        help='SSH timeout in seconds (default: 60)')
    parser.add_argument('--verbose', action='store_true',
                        help='Log SSH connections, commands, and responses')
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

    required = {'ip', 'username', 'git_path', 'python_path'}
    missing = required - set(reader.fieldnames or [])
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(missing)}")
        sys.exit(1)

    print("=" * 70)
    print(f"Lab Monitor - Run Collectors ({args.mode.upper()})")
    print(f"Systems: {len(rows)}  |  Timeout: {args.timeout}s  |  Dry-run: {args.dry_run}")
    print("=" * 70)
    print()

    results = []
    for i, row in enumerate(rows, 1):
        ip = row['ip'].strip()
        print(f"[{i}/{len(rows)}] {ip} ... ", end='', flush=True)
        result = run_collection(row, mode=args.mode, dry_run=args.dry_run, timeout=args.timeout)
        results.append(result)

        if result['status'] == 'ok':
            print(f"[OK] ({result['elapsed']:.1f}s)")
            if result['output']:
                for line in result['output'].splitlines()[:10]:  # Show first 10 lines
                    print(f"       {line}")
                if result['output'].count('\n') > 10:
                    print(f"       ... ({result['output'].count(chr(10)) - 10} more lines)")
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
    ok = [r for r in results if r['status'] == 'ok']
    failed = [r for r in results if r['status'] in ('error', 'failed')]
    dry = [r for r in results if r['status'] == 'dry-run']

    print("=" * 70)
    print("Summary")
    print("=" * 70)
    if dry:
        print(f"  Dry-run: {len(dry)} collectors would run {args.mode.upper()} collection")
    else:
        print(f"  [OK]:    {len(ok)} collectors completed successfully")
        print(f"  Failed:  {len(failed)}")
        if failed:
            print()
            print("  Failed systems:")
            for r in failed:
                print(f"    {r['ip']} — {r['error']}")
    print()


if __name__ == '__main__':
    main()
