#!/usr/bin/env python3
"""
Lab Monitor - Remote Collector Updater

Reads a CSV of collector systems, SSHes into each one and runs
'git pull origin main' in the repo. With --upgrade-pip it then also
upgrades pip and installs collector/requirements.txt (the same file the
collector installers use) into the python_path environment. Without
--upgrade-pip only the git pull runs.

On Synology/Linux hosts the collector requirements are installed the way
install-collector-synology.sh installs them: every package except psutil
strictly (a failure fails the step), then psutil best-effort. psutil has no
wheel for Synology armv7l and can't be compiled there; the collector then
uses its /proc fallbacks, so a failed psutil install is only a warning.
Windows hosts, and a --requirements file other than the default, are
installed strictly with pip install -r.

CSV format (one header row required):
    ip,username,password,git_path,python_path[,key_filename][,port]
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
    atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe

Fields:
    ip          Hostname or IP address
    username    SSH username
    password    SSH password (optional; tried after key/agent auth)
    git_path    Absolute path to the lab-monitor repo root on the remote system
    python_path Absolute path to the Python executable in the lab-monitor conda env
                (only needed with --upgrade-pip)
    key_filename  Optional private key file to offer first
    port        Optional SSH port (default 22)

Usage:
    python3 update_collectors.py --csv collectors.csv
    python3 update_collectors.py --csv collectors.csv --dry-run
    python3 update_collectors.py --csv collectors.csv --upgrade-pip
    python3 update_collectors.py --csv collectors.csv --upgrade-pip --timeout 60 --verbose
    python3 update_collectors.py --csv collectors.csv --parallel 4
    # One system first (its ip column, or the first part of its host name):
    python3 update_collectors.py --csv collectors.csv --only triton2 --dry-run
    # A host that also runs the Manager/Dashboard from the same env:
    python3 update_collectors.py --csv collectors.csv --upgrade-pip --requirements requirements.txt

Requirements:
    pip install paramiko

Security:
    Keep collectors.csv out of version control (.gitignore covers it).
    Prefer SSH key auth where possible (leave password blank).
    SSH host keys are trusted on first use and pinned in tools/.known_hosts;
    a changed host key is refused (see _ssh.py).

Exit code: 0 all OK, 1 bad arguments/CSV, 2 one or more systems failed.
"""

import argparse
import shlex
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
    if _verbose:
        print(f"  [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _filter_sshd_preamble(stderr_text: str) -> str:
    """
    Remove known sshd preamble warnings from stderr so they are not
    mistaken for command errors (e.g. "Could not chdir to home directory",
    common on Synology NAS when User Home Service is disabled). Lines are
    matched case-insensitively by prefix.
    """
    filtered = []
    for line in stderr_text.splitlines():
        low = line.lower()
        if any(low.startswith(noise) for noise in ('could not chdir',)):
            vprint(f"  [sshd preamble, ignored] {line}")
            continue
        filtered.append(line)
    return '\n'.join(filtered).strip()


def run_remote(client, command: str, timeout: float) -> tuple:
    """Run a remote command. Returns (stdout, stderr, exit_code)."""
    out, err, exit_code = _ssh.exec_command(client, command, timeout=timeout, log=vprint)
    return out, _filter_sshd_preamble(err), exit_code


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

# Requirements file (relative to git_path) installed by --upgrade-pip; the
# collector installers install the same file.
DEFAULT_REQUIREMENTS = 'collector/requirements.txt'


def build_commands(row: dict, skip_pip: bool,
                   requirements: str = DEFAULT_REQUIREMENTS) -> list[tuple[str, str]]:
    """
    Return a list of (label, shell_command) pairs for this system.

    Uses 'git -C <path>' and absolute paths throughout so no 'cd' is
    needed and each command is fully self-contained. requirements is the
    requirements file relative to the repo root (used unless skip_pip).
    """
    git_path    = _ssh.row_value(row, 'git_path')
    python_path = _ssh.row_value(row, 'python_path')
    windows     = is_windows_path(git_path) or is_windows_path(python_path)

    if windows:
        gp = git_path.replace('\\', '/')
        pp = python_path.replace('\\', '/')
    else:
        gp = git_path
        pp = python_path

    req_rel  = requirements.replace('\\', '/').lstrip('/')
    req_path = f'{gp}/{req_rel}'

    # git -C runs in the repo directory without needing a cd
    git_cmd = f'git -C "{gp}" pull origin main'

    commands = [('git pull', git_cmd)]

    if not skip_pip and python_path:
        # Upgrade pip, then install from the absolute requirements.txt path
        pip_upgrade = f'"{pp}" -m pip install --upgrade pip -q'
        if windows or req_rel != DEFAULT_REQUIREMENTS:
            pip_install = f'"{pp}" -m pip install -r "{req_path}" -q'
        else:
            pip_install = _posix_collector_pip_install(pp, req_path)
        commands.append(('pip upgrade', pip_upgrade))
        commands.append(('pip install', pip_install))

    return commands


# Packages the collector can run without (see collector/requirements.txt);
# on POSIX hosts they are installed best-effort, like the Synology installer does
BEST_EFFORT_PACKAGES = ('psutil',)


def _posix_collector_pip_install(python_path: str, req_path: str) -> str:
    """
    POSIX sh command: install every requirement in req_path except the
    BEST_EFFORT_PACKAGES strictly (exit 1 if that fails), then each
    best-effort package on its own; a failure there prints a WARNING and
    the step still succeeds.  Specs are read from the remote file (comments
    and blank lines skipped), so the pins stay in collector/requirements.txt.
    """
    py, req = shlex.quote(python_path), shlex.quote(req_path)
    names = '|'.join(BEST_EFFORT_PACKAGES)
    optional = f"grep -iE '^({names})([^A-Za-z0-9_.-]|$)'"
    specs = f"sed -e 's/#.*//' -e 's/[[:space:]]//g' {req} | grep -v '^$'"
    return (
        f'[ -r {req} ] || {{ echo "ERROR: requirements file not found:" {req} >&2; exit 1; }}; '
        'set -f; '
        f'strict=$({specs} | grep -viE \'^({names})([^A-Za-z0-9_.-]|$)\'); '
        f'if [ -n "$strict" ]; then {py} -m pip install -q $strict || exit 1; fi; '
        f'for spec in $({specs} | {optional}); do '
        f'if {py} -m pip install -q "$spec"; then echo "$spec installed"; '
        'else echo "WARNING: $spec could not be installed (no wheel for this CPU, e.g. Synology '
        'armv7l, and no compiler); the collector uses its /proc fallbacks without it"; fi; '
        'done'
    )


def update_system(row: dict, dry_run: bool, skip_pip: bool, timeout: int,
                  command_timeout: float = None,
                  requirements: str = DEFAULT_REQUIREMENTS) -> dict:
    """
    SSH into one system, run git pull (+ pip install). Returns result dict.

    timeout bounds the SSH connect; command_timeout (None/0 = no limit)
    bounds each remote step.
    """
    ip = _ssh.row_value(row, 'ip')

    result = {
        'ip':      ip,
        'status':  None,
        'steps':   [],   # list of {label, status, output, error}
        'elapsed': 0.0,
    }

    commands = build_commands(row, skip_pip, requirements)

    if dry_run:
        result['status'] = 'dry-run'
        for label, cmd in commands:
            result['steps'].append({'label': label, 'status': 'dry-run', 'output': cmd, 'error': ''})
        return result

    start = time.time()
    try:
        client = _ssh.connect_row(row, timeout=timeout, log=vprint)
        try:
            for label, cmd in commands:
                try:
                    out, err, code = run_remote(client, cmd, command_timeout)
                except _ssh.RemoteCommandTimeout as e:
                    out, err, code = e.stdout, f'{e} (see --command-timeout)', -1
                step = {'label': label, 'output': out, 'error': err}
                if code == 0:
                    step['status'] = 'ok'
                else:
                    step['status'] = 'error'
                    if not step['error']:
                        step['error'] = f'exit code {code}'
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
                                'output': '', 'error': str(e) or type(e).__name__})

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
    _ssh.require_paramiko()

    parser = argparse.ArgumentParser(
        description='Update lab-monitor collectors via SSH: git pull '
                    '(+ pip install with --upgrade-pip)')
    parser.add_argument('--csv',      required=True, metavar='PATH',
                        help='Path to collectors CSV inventory file')
    parser.add_argument('--dry-run',  action='store_true',
                        help='Print commands without running them')
    parser.add_argument('--upgrade-pip', action='store_true',
                        help='Also upgrade pip and install the requirements file '
                             '(default: git pull only). On Synology/Linux psutil is '
                             'best-effort, as in the Synology installer')
    parser.add_argument('--requirements', default=DEFAULT_REQUIREMENTS, metavar='RELPATH',
                        help='Requirements file relative to git_path, used with --upgrade-pip '
                             f'(default: {DEFAULT_REQUIREMENTS})')
    parser.add_argument('--timeout',  type=int, default=60,
                        help='SSH connect timeout in seconds (default 60)')
    parser.add_argument('--command-timeout', type=int, default=600, metavar='SECONDS',
                        help='Limit for each remote step (git pull, pip); 0 = no limit (default 600)')
    parser.add_argument('--parallel', type=_ssh.positive_int, default=1, metavar='N',
                        help='Update up to N systems at once (default: 1 = one at a time)')
    parser.add_argument('--only', action='append', default=[], metavar='HOST',
                        help='Update only this system: its ip column, or the first part of its '
                             'host name (triton2 selects triton2.example.org). Repeat for more '
                             'systems. A value that matches no row stops the tool')
    parser.add_argument('--verbose',  action='store_true',
                        help='Log SSH connections, commands, and responses')
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    rows, fieldnames = _ssh.read_inventory(csv_path)
    fields = set(fieldnames)

    if not rows:
        print("ERROR: CSV is empty or has no data rows")
        sys.exit(1)

    required = {'ip', 'username', 'git_path'}
    missing  = required - fields
    if missing:
        print(f"ERROR: CSV missing required columns: {', '.join(sorted(missing))}")
        sys.exit(1)

    if args.only:
        rows, unmatched = _ssh.select_rows(rows, args.only)
        if unmatched:
            print(f"ERROR: --only matched no system in {csv_path}: {', '.join(unmatched)}")
            print("       Use the ip column's value, or the first part of a host name.")
            sys.exit(1)

    if 'python_path' not in fields and args.upgrade_pip:
        print("ERROR: 'python_path' column not found but --upgrade-pip was requested.")
        print("       Add python_path to your CSV to enable pip operations.")
        print()
        sys.exit(1)

    steps_desc = 'git pull' + (f' + pip install -r {args.requirements}' if args.upgrade_pip else '')
    print("=" * 62)
    print("Lab Monitor — Remote Update")
    print(f"Systems : {len(rows)}" + (f" (--only {', '.join(args.only)})" if args.only else ''))
    print(f"Steps   : {steps_desc}")
    print(f"Timeout : {args.timeout}s connect, "
          f"{f'{args.command_timeout}s' if args.command_timeout else 'no limit'} per step")
    print(f"Parallel: {args.parallel}")
    print(f"Dry-run : {args.dry_run}")
    print("=" * 62)
    print()

    def worker(row):
        return update_system(row, dry_run=args.dry_run,
                             skip_pip=(not args.upgrade_pip), timeout=args.timeout,
                             command_timeout=args.command_timeout,
                             requirements=args.requirements)

    results = _ssh.run_hosts(rows, worker, parallel=args.parallel,
                             on_done=lambda i, row, r: print_result(r, i, len(rows)))

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

    sys.exit(2 if fail_n else 0)


if __name__ == '__main__':
    main()
