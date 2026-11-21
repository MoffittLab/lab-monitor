#!/usr/bin/env python3
"""
Lab Monitor - Remote Archive All Collectors

Move every collector's YYYY-MM.jsonl archive files into a timestamped backup
folder next to them, on all collectors in collectors.csv. Nothing is deleted:
files are only moved, so they can be inspected later, moved back, or re-sent
with tools/replay_archives.py to a Manager that lost that data. The Manager
is not touched — its history stays as it is (to wipe a system on the
Manager, see reset_system.py).

CSV format (one header row required):
    ip,username,password,git_path,python_path[,key_filename][,port]
    192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
    atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe

Usage:
    python3 archive_all_collectors.py --csv collectors.csv --dry-run
    python3 archive_all_collectors.py --csv collectors.csv                 # asks you to type ARCHIVE
    python3 archive_all_collectors.py --csv collectors.csv --yes           # no prompt (scripts)
    python3 archive_all_collectors.py --csv collectors.csv --also-archive-queues
    python3 archive_all_collectors.py --csv collectors.csv --timeout 60 --verbose --parallel 4

What it does:
    1. Asks you to type ARCHIVE to confirm (skipped with --yes or --dry-run)
    2. For each collector, SSHes in
    3. Reads the collector's config (config_tool.py) to find data_dir and system name
    4. Creates a timestamped backup folder <data_dir>/<name>/backup_YYYYMMDD_HHMMSS
    5. Moves all YYYY-MM.jsonl archive files into the backup folder
    6. With --also-archive-queues, also MOVES queue.json into the same backup
       folder, so messages still waiting to be sent are not sent (they remain in
       the backup and in the archives; send them later with
       replay_archives.py --source <backup>/queue.json)
    7. Shows backup locations and file counts. Any failure (unreachable host,
       failed cd/mkdir/move, unexpected output) is reported as a failure.

--also-delete-queues is a deprecated alias of --also-archive-queues. It used to
delete queue.json; it now moves it into the backup folder instead.

What it does NOT do:
    - Does NOT delete any file (archives and queue are moved, never removed)
    - Does NOT wipe or change the Manager (unlike reset_system.py)
    - Does NOT modify config.json or collectors.csv

Tips:
    - Always --dry-run first to preview what will happen
    - Pause collectors first (toggle_collectors.py --mode off) so no run is
      writing to the archive while it is moved
    - Keep collectors.csv out of version control

Exit code: 0 all archived, 1 bad arguments/CSV or confirmation declined,
2 one or more collectors failed.

Requirements:
    pip install paramiko
"""

import argparse
import json
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

import _ssh
from _ssh import is_windows_path

try:
    import paramiko
except ImportError:
    paramiko = None


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

_verbose = False

# Typed confirmation required before anything is moved (unless --yes).
CONFIRM_WORD = 'ARCHIVE'

# Same pattern the Manager uses for device names; anything else is refused
# before it gets near a remote shell command.
_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def vprint(msg: str):
    """Print if verbose mode is enabled."""
    if _verbose:
        print(f"    [v] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def run_remote(client, command: str, timeout: float) -> tuple:
    """Run a command remotely. Returns (stdout, stderr, exit_code)."""
    return _ssh.exec_command(client, command, timeout=timeout, log=vprint)


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

def get_config(client, git_path: str, python_path: str, timeout: float) -> tuple:
    """
    Fetch collector config via config_tool.py.
    Returns (config_dict, None) on success, (None, error_str) on failure.
    """
    # Normalize Windows paths
    if is_windows_path(git_path) or is_windows_path(python_path):
        git_path = git_path.replace('\\', '/')
        python_path = python_path.replace('\\', '/')

    config_tool_path = f"{git_path}/collector/config_tool.py"
    config_path = f"{git_path}/collector/local/config.json"
    vprint(f"Reading config from {config_path}")
    command = f'"{python_path}" "{config_tool_path}" --config "{config_path}" list --json'

    out, err, code = run_remote(client, command, timeout)

    if code != 0:
        return None, f"config_tool.py failed (exit {code}): {err or out}"

    try:
        response = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from config_tool: {e}"
    # config_tool.py returns {"status": "ok", "config": {...}}
    if isinstance(response, dict) and response.get('status') == 'ok' \
            and isinstance(response.get('config'), dict):
        return response['config'], None
    error = response.get('error', 'unexpected response') if isinstance(response, dict) else 'unexpected response'
    return None, f"config_tool error: {error}"


# ---------------------------------------------------------------------------
# Archive logic
# ---------------------------------------------------------------------------

def _is_absolute(path: str, windows: bool) -> bool:
    if windows:
        return bool(re.match(r'^[A-Za-z]:[\\/]', path)) or path.startswith('\\\\')
    return path.startswith('/')


def build_unix_command(system_dir: str, backup_folder: str, include_queue: bool) -> str:
    """
    Shell command (Synology/Linux) that moves the archives (and optionally
    queue.json) into backup_folder inside system_dir.

    Steps are chained with && so a failed cd/mkdir/mv aborts the rest and
    gives a non-zero exit status; paths are shell-quoted.
    """
    backup = shlex.quote(backup_folder)
    steps = [
        f'cd {shlex.quote(system_dir)}',
        f'mkdir -p {backup}',
        'count=0',
        ('for f in [0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl; do '
         f'if [ -e "$f" ]; then mv "$f" {backup}/ || exit 1; count=$((count+1)); fi; '
         'done'),
        'echo "MOVED: $count"',
    ]
    if include_queue:
        steps.append(
            f'if [ -e queue.json ]; then mv queue.json {backup}/ && echo "QUEUE: moved"; '
            'else echo "QUEUE: none"; fi'
        )
    return ' && '.join(steps)


def build_windows_script(system_dir: str, backup_folder: str, include_queue: bool) -> str:
    """PowerShell 5.1 script equivalent of build_unix_command (stops on the first error)."""
    q = _ssh.ps_quote
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        f"    Set-Location -LiteralPath {q(system_dir)}",
        f"    $backup = Join-Path (Get-Location).Path {q(backup_folder)}",
        "    if (-not (Test-Path -LiteralPath $backup)) { New-Item -ItemType Directory -Path $backup | Out-Null }",
        "    $count = 0",
        "    foreach ($f in @(Get-ChildItem -LiteralPath . -File | Where-Object { $_.Name -match '^\\d{4}-\\d{2}\\.jsonl$' })) {",
        "        Move-Item -LiteralPath $f.FullName -Destination (Join-Path $backup $f.Name)",
        "        $count++",
        "    }",
        "    Write-Output \"MOVED: $count\"",
    ]
    if include_queue:
        lines += [
            "    if (Test-Path -LiteralPath 'queue.json') {",
            "        Move-Item -LiteralPath 'queue.json' -Destination (Join-Path $backup 'queue.json')",
            "        Write-Output 'QUEUE: moved'",
            "    } else {",
            "        Write-Output 'QUEUE: none'",
            "    }",
        ]
    lines += [
        "    exit 0",
        "} catch {",
        "    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)",
        "    exit 1",
        "}",
    ]
    return '\n'.join(lines)


def build_archive_command(name: str, data_dir: str, backup_folder: str,
                          include_queue: bool, windows: bool) -> tuple:
    """Returns (command, backup_dir) for one collector."""
    if windows:
        system_dir = data_dir.rstrip('\\/') + '\\' + name
        backup_dir = f'{system_dir}\\{backup_folder}'
        command = _ssh.powershell_command(build_windows_script(system_dir, backup_folder, include_queue))
    else:
        system_dir = f"{data_dir.rstrip('/')}/{name}"
        backup_dir = f'{system_dir}/{backup_folder}'
        command = build_unix_command(system_dir, backup_folder, include_queue)
    return command, backup_dir


def parse_archive_output(out: str, include_queue: bool) -> tuple:
    """
    Parse the MOVED:/QUEUE: lines. Returns (files_moved, queue_state, error);
    error is set when the expected lines are missing.
    """
    moved = None
    queue_state = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('MOVED:'):
            try:
                moved = int(line.split(':', 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith('QUEUE:'):
            queue_state = line.split(':', 1)[1].strip()
    if moved is None:
        return 0, queue_state, f"could not confirm the move (unexpected output: {out[:200]!r})"
    if include_queue and queue_state not in ('moved', 'none'):
        return moved, queue_state, f"queue.json state unknown (output: {out[:200]!r})"
    return moved, queue_state, None


def archive_collector(client, git_path: str, config: dict, timeout: float,
                      include_queue: bool = False, dry_run: bool = False,
                      python_path: str = '') -> dict:
    """
    Move all YYYY-MM.jsonl files (and optionally queue.json) into a new
    backup folder on a remote collector.

    Returns {'ok', 'message', 'backup_dir', 'moved', 'queue'}.
    """
    name = str(config.get('name') or '').strip()
    data_dir = str(config.get('data_dir') or '').strip()
    result = {'ok': False, 'message': '', 'backup_dir': '', 'moved': 0, 'queue': None}

    if not name or not data_dir:
        result['message'] = "Missing 'name' or 'data_dir' in config"
        return result
    if not _NAME_RE.match(name):
        result['message'] = f"Refusing unusual system name {name!r} (allowed: letters, digits, _ . -)"
        return result

    windows = is_windows_path(data_dir) or is_windows_path(git_path) or is_windows_path(python_path)
    if not _is_absolute(data_dir, windows):
        result['message'] = (f"data_dir {data_dir!r} is not an absolute path; set an absolute "
                             f"data_dir in the collector config so the archives can be located")
        return result

    backup_folder = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    command, backup_dir = build_archive_command(name, data_dir, backup_folder, include_queue, windows)
    result['backup_dir'] = backup_dir

    what = 'Archives' + (' and queue.json' if include_queue else '')
    if dry_run:
        vprint(f"[DRY-RUN] Would execute: {command}")
        result['ok'] = True
        result['message'] = f"[DRY-RUN] {what} would be moved to {backup_folder}"
        return result

    vprint(f"Creating backup folder: {backup_dir}")
    out, err, code = run_remote(client, command, timeout)
    if code != 0:
        result['message'] = f"Archive command failed (exit {code}): {err or out}"
        return result

    moved, queue_state, error = parse_archive_output(out, include_queue)
    result['moved'] = moved
    result['queue'] = queue_state
    if error:
        result['message'] = error
        return result

    msg = f"Moved {moved} archive file(s) to {backup_folder}"
    if include_queue:
        msg += '; queue.json moved too' if queue_state == 'moved' else '; no queue.json present'
    result['ok'] = True
    result['message'] = msg
    return result


def archive_host(row: dict, timeout: int, command_timeout: float,
                 include_queue: bool, dry_run: bool) -> dict:
    """SSH into one collector, read its config and archive. Returns a result dict."""
    ip = _ssh.row_value(row, 'ip')
    git_path = _ssh.row_value(row, 'git_path')
    python_path = _ssh.row_value(row, 'python_path', 'python3')
    result = {'ip': ip, 'ok': False, 'name': '?', 'message': '', 'backup_dir': ''}

    if not all([ip, _ssh.row_value(row, 'username'), git_path]):
        result['message'] = "[SKIP] Missing required fields (ip, username, git_path)"
        return result

    try:
        client = _ssh.connect_row(row, timeout=timeout, log=vprint)
        try:
            config, err = get_config(client, git_path, python_path, command_timeout)
            if err:
                result['message'] = f"Config error: {err}"
                return result
            result['name'] = config.get('name', '?')
            outcome = archive_collector(client, git_path, config, command_timeout,
                                        include_queue=include_queue, dry_run=dry_run,
                                        python_path=python_path)
            result.update(ok=outcome['ok'], message=outcome['message'],
                          backup_dir=outcome['backup_dir'])
        finally:
            client.close()
    except _ssh.RemoteCommandTimeout as e:
        result['message'] = f"{e} (see --command-timeout)"
    except paramiko.AuthenticationException as e:
        result['message'] = f"Auth failed: {e}"
    except paramiko.SSHException as e:
        result['message'] = f"SSH error: {e}"
    except Exception as e:
        result['message'] = str(e) or type(e).__name__
    return result


def confirm(count: int, include_queue: bool, input_fn=None) -> bool:
    """Ask the operator to type CONFIRM_WORD. Returns True only on an exact match."""
    input_fn = input_fn or input
    what = 'archive files' + (' and queue.json' if include_queue else '')
    prompt = (f"Move {what} into backup folders on {count} collector(s)?\n"
              f"Type {CONFIRM_WORD} to continue, or anything else to cancel: ")
    try:
        answer = input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == CONFIRM_WORD


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Archive all collectors — move YYYY-MM.jsonl files to backup folders',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
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
        help='SSH connect timeout in seconds (default: 60)'
    )
    parser.add_argument(
        '--command-timeout',
        type=int,
        default=300,
        metavar='SECONDS',
        help='Limit for each remote command; 0 = no limit (default: 300)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview commands without executing'
    )
    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help=f'Do not ask for the typed {CONFIRM_WORD} confirmation'
    )
    parser.add_argument(
        '--also-archive-queues',
        action='store_true',
        help='Also move queue.json into the backup folder (pending messages are not sent)'
    )
    parser.add_argument(
        '--also-delete-queues',
        action='store_true',
        help='Deprecated alias of --also-archive-queues (queue.json is moved, not deleted)'
    )
    parser.add_argument(
        '--parallel',
        type=_ssh.positive_int,
        default=1,
        metavar='N',
        help='Archive up to N collectors at once (default: 1 = one at a time)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output'
    )
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    _ssh.require_paramiko()

    include_queue = args.also_archive_queues or args.also_delete_queues
    if args.also_delete_queues:
        print("NOTE: --also-delete-queues is deprecated; queue.json is now MOVED into the "
              "backup folder (use --also-archive-queues).")

    # Load CSV
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    try:
        collectors, _fieldnames = _ssh.read_inventory(csv_path)
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
    if args.dry_run:
        print("[DRY-RUN MODE] - No changes will be made")
        print()

    print(f"Collectors: {len(collectors)}")
    print(f"Timeout: {args.timeout}s connect, "
          f"{f'{args.command_timeout}s' if args.command_timeout else 'no limit'} per command")
    print(f"Also archive queues: {include_queue}")
    print(f"Parallel: {args.parallel}")
    print()

    if not args.dry_run and not args.yes:
        if not confirm(len(collectors), include_queue):
            print("Aborted — nothing was changed.")
            sys.exit(1)
        print()

    def worker(row):
        return archive_host(row, args.timeout, args.command_timeout, include_queue, args.dry_run)

    def on_start(i, row):
        print(f"[{i}/{len(collectors)}] {_ssh.row_value(row, 'ip')} ", end='', flush=True)

    def on_done(i, row, r):
        if r['ok']:
            print(f"[OK] {r['name']}: {r['message']}")
        else:
            print(f"[FAILED] {r['message']}")

    results = _ssh.run_hosts(collectors, worker, parallel=args.parallel,
                             on_start=on_start, on_done=on_done)

    # Summary
    print()
    print("=" * 70)
    success_count = sum(1 for r in results if r['ok'])
    verb = 'checked (dry-run)' if args.dry_run else 'archived successfully'
    print(f"Summary: {success_count}/{len(collectors)} collectors {verb}")
    print("=" * 70)
    print()

    # Show successful backups
    if success_count > 0:
        print("Backup locations:" if not args.dry_run else "Backup locations (would be):")
        for r in results:
            if r['ok'] and r['backup_dir']:
                print(f"  {r['ip']:30} → {r['backup_dir']}")
        print()

    # Show failures
    failures = [r for r in results if not r['ok']]
    if failures:
        print("Failures:")
        for r in failures:
            print(f"  {r['ip']:30} {r['message']}")
        print()

    # Exit code
    sys.exit(0 if success_count == len(collectors) else 2)


if __name__ == '__main__':
    main()
