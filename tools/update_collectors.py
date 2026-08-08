#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Updater

Reads a CSV of collector systems, SSHes into each one, runs
'git pull origin main', then reinstalls requirements from the
repo-root requirements.txt.

CSV format (one header row required):
    ip,username,password,git_path,python_path
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
    atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe

Fields:
    ip          Hostname or IP address
    username    SSH username
    password    SSH password (leave blank to use key/agent auth)
    git_path    Absolute path to the lab-monitor repo root on the remote system
    python_path Absolute path to the Python executable in the lab-monitor conda env

Usage:
    python3 update_collectors.py --csv collectors.csv
    python3 update_collectors.py --csv collectors.csv --dry-run
    python3 update_collectors.py --csv collectors.csv --skip-pip
    python3 update_collectors.py --csv collectors.csv --timeout 60 --verbose

Requirements:
    pip install paramiko

Security:
    Keep collectors.csv out of version control (.gitignore covers it).
    Prefer SSH key auth where possible (leave password blank).
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
    if _verbose:
        print(f"  [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_windows_path(path: str) -> bool:
    """Heuristic: Windows paths contain a drive letter or .exe extension."""
    p = path.strip()
    return (
        p.lower().endswith('.exe') or
        (len(p) >= 2 and p[1] == ':') or
        '\\' in p
    )


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(ip: str, username: str, password: str, timeout: int):
    """Open an SSH connection. Uses key/agent auth when password is blank."""
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


# SSH daemon warning lines that are cosmetic and should not be treated as errors.
# Common on Synology NAS when User Home Service is disabled.
_SSHD_NOISE = (
    'could not chdir to home directory',
    'no such file or directory',
    'permission denied',  # only suppress if it comes from sshd preamble
)

def _filter_sshd_preamble(stderr_text: str) -> str:
    """
    Remove known sshd preamble warnings from stderr so they are not
    mistaken for command errors.  Lines are matched case-insensitively
    against a known-noise prefix list.
    """
    filtered = []
    for line in stderr_text.splitlines():
        low = line.lower()
        if any(low.startswith(noise) for noise in ('could not chdir',)):
            vprint(f"  [sshd preamble, ignored] {line}")
            continue
        filtered.append(line)
    return '\n'.join(filtered).strip()


def run_remote(client, command: str, timeout: int) -> tuple:
    """Run a remote command. Returns (stdout, stderr, exit_code)."""
    vprint(f"→ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors='replace').strip()
    err = _filter_sshd_preamble(stderr.read().decode(errors='replace').strip())
    vprint(f"← exit {exit_code}")
    if out: vprint(f"  stdout: {out[:500]}{'...' if len(out) > 500 else ''}")
    if err: vprint(f"  stderr: {err[:500]}{'...' if len(err) > 500 else ''}")
    return out, err, exit_code


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

def build_commands(row: dict, skip_pip: bool) -> list[tuple[str, str]]:
    """
    Return a list of (label, shell_command) pairs for this system.

    Uses 'git -C <path>' and absolute paths throughout so no 'cd' is
    needed and each command is fully self-contained.
    """
    git_path    = row['git_path'].strip()
    python_path = row.get('python_path', '').strip()
    windows     = is_windows_path(git_path) or is_windows_path(python_path)

    if windows:
        gp = git_path.replace('\\', '/')
        pp = python_path.replace('\\', '/')
    else:
        gp = git_path
        pp = python_path

    req_path = f'{gp}/requirements.txt'

    # git -C runs in the repo directory without needing a cd
    git_cmd = f'git -C "{gp}" pull origin main'

    commands = [('git pull', git_cmd)]

    if not skip_pip and python_path:
        # Upgrade pip, then install from the absolute requirements.txt path
        pip_upgrade = f'"{pp}" -m pip install --upgrade pip -q'
        pip_install = f'"{pp}" -m pip install -r "{req_path}" -q'
        commands.append(('pip upgrade', pip_upgrade))
        commands.append(('pip install', pip_install))

    return commands


def update_system(row: dict, dry_run: bool, skip_pip: bool, timeout: int) -> dict:
    """SSH into one system, run git pull + pip install. Returns result dict."""
    ip       = row['ip'].strip()
    username = row['username'].strip()
    password = row.get('password', '').strip()

    result = {
        'ip':      ip,
        'status':  None,
        'steps':   [],   # list of {label, status, output, error}
        'elapsed': 0.0,
    }

    commands = build_commands(row, skip_pip)

    if dry_run:
        result['status'] = 'dry-run'
        for label, cmd in commands:
            result['steps'].append({'label': label, 'status': 'dry-run', 'output': cmd, 'error': ''})
        return result

    start = time.time()
    try:
        client = ssh_connect(ip, username, password, timeout)
        try:
            for label, cmd in commands:
                out, err, code = run_remote(client, cmd, timeout)
                step = {'label': label, 'output': out, 'error': err}
                if code == 0:
                    step['status'] = 'ok'
                else:
                    step['status'] = 'error'
                result['steps'].append(step)
                if code != 0:
                    break   # stop on first failure
        finally:
            client.close()

        result['elapsed'] = time.time() - start
        failed = [s for s in result['steps'] if s['status'] == 'error']
        result['status'] = 'error' if failed else 'ok'

    except Exception as e:
        result['elapsed'] = time.time() - start
        result['status']  = 'failed'
        result['steps'].append({'label': 'connect', 'status': 'failed',
                                'output': '', 'error': str(e)})

    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

OK    = '[OK]    '
FAIL  = '[FAILED]'
DRY   = '[DRY]   '


def print_result(result: dict, index: int, total: int):
    ip = result['ip']
    header = f"[{index}/{total}] {ip}"

    if result['status'] == 'dry-run':
        print(f"{header}")
        for step in result['steps']:
            print(f"  {DRY} {step['label']}: {step['output']}")
        print()
        return

    elapsed = f"({result['elapsed']:.1f}s)"
    if result['status'] == 'ok':
        print(f"{header}  {OK} {elapsed}")
    else:
        print(f"{header}  {FAIL} {elapsed}")

    for step in result['steps']:
        status_tag = OK if step['status'] == 'ok' else FAIL
        print(f"  {status_tag} {step['label']}")
        if step['status'] == 'ok' and step['output']:
            for line in step['output'].splitlines():
                print(f"           {line}")
        if step['status'] != 'ok':
            if step['output']:
                print(f"           stdout: {step['output'][:300]}")
            if step['error']:
                print(f"           error:  {step['error'][:300]}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if paramiko is None:
        print("ERROR: paramiko is required.")
        print("       Install with: pip install paramiko")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Update lab-monitor collectors via SSH: git pull + pip install')
    parser.add_argument('--csv',      required=True, metavar='PATH',
                        help='Path to collectors CSV inventory file')
    parser.add_argument('--dry-run',  action='store_true',
                        help='Print commands without running them')
    parser.add_argument('--skip-pip', action='store_true',
                        help='Run git pull only; skip pip install')
    parser.add_argument('--timeout',  type=int, default=60,
                        help='SSH + command timeout in seconds (default 60)')
    parser.add_argument('--verbose',  action='store_true',
                        help='Log SSH connections, commands, and responses')
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = set(reader.fieldnames or [])

    if not rows:
        print("ERROR: CSV is empty or has no data rows")
        sys.exit(1)

    required = {'ip', 'username', 'git_path'}
    missing  = required - fields
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(sorted(missing))}")
        sys.exit(1)

    if 'python_path' not in fields and not args.skip_pip:
        print("WARNING: 'python_path' column not found — pip install will be skipped.")
        print("         Add python_path to your CSV or pass --skip-pip to suppress this warning.")
        print()
        args.skip_pip = True

    steps_desc = 'git pull' + ('' if args.skip_pip else ' + pip install -r requirements.txt')
    print("=" * 62)
    print("Lab Monitor — Remote Update")
    print(f"Systems : {len(rows)}")
    print(f"Steps   : {steps_desc}")
    print(f"Timeout : {args.timeout}s")
    print(f"Dry-run : {args.dry_run}")
    print("=" * 62)
    print()

    results = []
    for i, row in enumerate(rows, 1):
        result = update_system(row, dry_run=args.dry_run,
                               skip_pip=args.skip_pip, timeout=args.timeout)
        results.append(result)
        print_result(result, i, len(rows))

    # Summary
    ok_n     = sum(1 for r in results if r['status'] == 'ok')
    fail_n   = sum(1 for r in results if r['status'] in ('error', 'failed'))
    dry_n    = sum(1 for r in results if r['status'] == 'dry-run')

    print("=" * 62)
    print("Summary")
    print("=" * 62)
    if dry_n:
        print(f"  Dry-run : {dry_n} system(s)")
    else:
        print(f"  {OK} : {ok_n}")
        print(f"  {FAIL} : {fail_n}")
        if fail_n:
            print()
            print("  Failed systems:")
            for r in results:
                if r['status'] in ('error', 'failed'):
                    err = next((s['error'] for s in r['steps'] if s['status'] != 'ok'), '')
                    print(f"    {r['ip']} — {err[:120]}")
    print()


if __name__ == '__main__':
    main()
