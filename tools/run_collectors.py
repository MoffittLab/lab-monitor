#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Execution

Trigger collection events on remote collectors by SSHing in and running
the collector script in disk or metrics mode.

CSV format (one header row required):
    ip,username,password,git_path,python_path[,key_filename][,port]
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
    atlantis.med.harvard.edu,admin,secret789,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe

Usage:
    python3 run_collectors.py --csv collectors.csv --mode disk              # run disk collection on all
    python3 run_collectors.py --csv collectors.csv --mode metrics           # run metrics collection on all
    python3 run_collectors.py --csv collectors.csv --mode disk --dry-run    # preview commands
    python3 run_collectors.py --csv collectors.csv --mode disk --timeout 60 # custom SSH connect timeout
    python3 run_collectors.py --csv collectors.csv --mode metrics --parallel 4

Requirements:
    pip install paramiko

Note:
    - Keep collectors.csv out of version control
    - python_path can be absolute or relative to the git_path directory
    - Collection output is captured and displayed per system
    - SSH host keys are trusted on first use and pinned in tools/.known_hosts
      (see _ssh.py); a changed host key is refused
    - Exit code: 0 all OK, 1 bad arguments/CSV, 2 one or more systems failed
"""

import argparse
import sys
import time
from pathlib import Path

import _ssh
from _ssh import is_windows_path


# ---------------------------------------------------------------------------
# Verbose logging
# ---------------------------------------------------------------------------

_verbose = False

def vprint(msg: str):
    """Print if verbose mode is enabled."""
    if _verbose:
        print(f"  [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Collection execution
# ---------------------------------------------------------------------------

# Default limits for the remote collector run (seconds; 0 = no limit).
# A disk scan can legitimately take hours (collector timeout_seconds applies
# per folder), so disk mode waits indefinitely unless --command-timeout is set.
DEFAULT_COMMAND_TIMEOUT = {'metrics': 600, 'disk': 0}


def build_run_command(row: dict, mode: str) -> str:
    """The remote command that runs one collection on this system."""
    if _ssh.is_linux_install(row):
        # Through systemd, so the run gets the unit's user (labmon), sandbox and
        # capabilities. The units are oneshot: start returns when the run has
        # finished, with a non-zero exit if it failed.
        return f'{_ssh.SUDO} systemctl start lab-monitor-{mode}.service'

    git_path = _ssh.row_value(row, 'git_path')
    python_path = _ssh.row_value(row, 'python_path')
    # Normalize paths: convert backslashes to forward slashes for SSH
    if is_windows_path(git_path) or is_windows_path(python_path):
        git_path = git_path.replace('\\', '/')
        python_path = python_path.replace('\\', '/')

    # Absolute paths: no cd needed; works on both Windows and Unix over SSH
    collector_py = f"{git_path}/collector/collector.py"
    config_json = f"{git_path}/collector/local/config.json"
    return f'"{python_path}" "{collector_py}" --config "{config_json}" --mode {mode}'


def run_collection(row: dict, mode: str, dry_run: bool, timeout: int,
                   command_timeout: float = None) -> dict:
    """
    SSH into one collector and run the collector script in the specified mode.
    Returns result dict with status, output, error, and timing.

    timeout bounds the SSH connect; command_timeout (None/0 = no limit)
    bounds the remote collector run.
    """
    ip = _ssh.row_value(row, 'ip')
    git_path = _ssh.row_value(row, 'git_path')
    python_path = _ssh.row_value(row, 'python_path')

    result = {
        'ip': ip,
        'status': None,
        'output': '',
        'error': '',
        'elapsed': 0.0,
    }

    command = build_run_command(row, mode)
    linux = _ssh.is_linux_install(row)

    if dry_run:
        result['status'] = 'dry-run'
        result['output'] = f'Would run: {command}'
        return result

    start = time.time()
    try:
        vprint(f"--- {ip} ---")
        vprint(f"Mode: {mode} | python: {python_path}")
        client = _ssh.connect_row(row, timeout=timeout, log=vprint)
        try:
            stdout, stderr, exit_code = _ssh.exec_command(
                client, command, timeout=command_timeout, log=vprint)
            result['elapsed'] = time.time() - start

            if exit_code == 0:
                result['status'] = 'ok'
                result['output'] = stdout or (
                    f'lab-monitor-{mode}.service finished; its log: '
                    f'sudo journalctl -u lab-monitor-{mode}.service -n 30'
                    + ('. The disk unit has no network: the next metrics run (within 5 minutes) '
                       'sends the result' if mode == 'disk' else '')
                    if linux else '(no output)')
            else:
                result['status'] = 'error'
                result['output'] = stdout
                result['error'] = stderr or f'exit code {exit_code}'
                if linux:
                    result['error'] += _ssh.sudo_hint(result['error']) or (
                        f' -- see: sudo journalctl -u lab-monitor-{mode}.service -n 30')

        finally:
            client.close()

    except _ssh.RemoteCommandTimeout as e:
        result['elapsed'] = time.time() - start
        result['status'] = 'failed'
        result['output'] = e.stdout
        result['error'] = f'{e} (raise --command-timeout if the collector needs longer)'
    except Exception as e:
        result['elapsed'] = time.time() - start
        result['status'] = 'failed'
        result['error'] = str(e) or type(e).__name__

    return result


def print_result(result: dict):
    """Print one system's outcome (the '[i/n] ip ... ' prefix is already printed)."""
    if result['status'] == 'ok':
        print(f"[OK] ({result['elapsed']:.1f}s)")
        if result['output']:
            lines = result['output'].splitlines()
            for line in lines[:10]:  # Show first 10 lines
                print(f"       {line}")
            if len(lines) > 10:
                print(f"       ... ({len(lines) - 10} more lines)")
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _ssh.require_paramiko()

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
                        help='SSH connect timeout in seconds (default: 60)')
    parser.add_argument('--command-timeout', type=int, default=None, metavar='SECONDS',
                        help='Limit for the remote collector run; 0 = no limit '
                             '(default: 600 for metrics, no limit for disk)')
    parser.add_argument('--parallel', type=_ssh.positive_int, default=1, metavar='N',
                        help='Run up to N systems at once (default: 1 = one at a time)')
    parser.add_argument('--verbose', action='store_true',
                        help='Log SSH connections, commands, and responses')
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    command_timeout = args.command_timeout
    if command_timeout is None:
        command_timeout = DEFAULT_COMMAND_TIMEOUT[args.mode]

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    # Read CSV
    rows, fieldnames = _ssh.read_inventory(csv_path)

    if not rows:
        print("ERROR: CSV file is empty or has no data rows")
        sys.exit(1)

    required = {'ip', 'username', 'git_path', 'python_path'}
    missing = required - set(fieldnames)
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(sorted(missing))}")
        sys.exit(1)

    limit = f"{command_timeout}s" if command_timeout else "none"
    print("=" * 70)
    print(f"Lab Monitor - Run Collectors ({args.mode.upper()})")
    print(f"Systems: {len(rows)}  |  Timeout: {args.timeout}s  |  Command limit: {limit}  |  "
          f"Parallel: {args.parallel}  |  Dry-run: {args.dry_run}")
    print("=" * 70)
    print()

    def worker(row):
        return run_collection(row, mode=args.mode, dry_run=args.dry_run,
                              timeout=args.timeout, command_timeout=command_timeout)

    def on_start(i, row):
        print(f"[{i}/{len(rows)}] {_ssh.row_value(row, 'ip')} ... ", end='', flush=True)

    results = _ssh.run_hosts(rows, worker, parallel=args.parallel,
                             on_start=on_start, on_done=lambda i, row, r: print_result(r))

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

    sys.exit(2 if failed else 0)


if __name__ == '__main__':
    main()
