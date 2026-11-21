#!/usr/bin/env python3
"""
Lab Monitor Manager/Dashboard Restart Tool

Reads collectors.csv to find the Manager/Dashboard server (Windows), connects
via SSH (paramiko), stops the running Manager and Dashboard processes, starts
them again through Windows Task Scheduler and verifies they are really up.

Scheduled tasks: install-manager-dashboard-taskscheduler.ps1 registers
"Lab Monitor Manager" and "Lab Monitor Dashboard"; each runs a launcher that
restarts its service when it exits. Stopping therefore first ends those tasks
(schtasks /end) so the launcher loop doesn't immediately bring the process
back, then kills the verified PIDs. Older installs with the single
"Lab Monitor Startup" task are handled too.

How the processes are found:
    manager/manager.py and dashboard/app.py write their PID at startup to
    <git_path>\\.pids\\manager.pid and <git_path>\\.pids\\dashboard.pid
    (git_path is the server's row in collectors.csv). Before a PID is killed
    it is checked with Get-CimInstance Win32_Process: it must be a live python
    process whose command line runs manager.py / app.py. A stale PID file that
    now points at some other process is reported and never killed. The
    Manager writes its PID file before it migrates databases on its first
    start after an upgrade. After the stop, no python process may still run
    manager.py (a leftover not named by the PID file): the tool then refuses
    to start the services, because two Managers must never share a data_dir.

How the restart is verified:
    Each service's PID file must name a live python manager.py / app.py
    process (a different PID than the one just stopped) AND its HTTP endpoint
    must answer on the server (Manager: /health → 200; Dashboard: /health or
    /, where 401 from its optional login also counts as answering),
    polled for up to --verify-timeout seconds. A PID file merely existing is
    not treated as "running".

Requirements:
    pip install paramiko

Usage:
    python restart_manager_dashboard.py --csv collectors.csv [--host atlantis.med.harvard.edu]
    python restart_manager_dashboard.py --csv collectors.csv --list-processes
    python restart_manager_dashboard.py --csv collectors.csv --kill-only
    python restart_manager_dashboard.py --csv collectors.csv --start-only
    python restart_manager_dashboard.py --csv collectors.csv --task "Lab Monitor Startup"

Exit code: 0 success, 1 CSV/connection error, 2 a stop, start or verification
step failed.
"""
import argparse
import json
import logging
import re
import sys
import time
from typing import Optional, Dict

import _ssh

# Setup logging
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# Services managed by this tool. port is the fallback when the service's
# config.json on the server has no "port". The Manager's /health is
# unauthenticated and must return 200; the Dashboard may have HTTP Basic auth
# enabled (every route then answers 401), so any non-5xx answer proves it is up.
SERVICES = [
    {'label': 'Manager',   'pid_file': 'manager.pid',   'script': 'manager.py',
     'subdir': 'manager',   'port': 5000, 'health_paths': ['/health'], 'any_answer': False},
    {'label': 'Dashboard', 'pid_file': 'dashboard.pid', 'script': 'app.py',
     'subdir': 'dashboard', 'port': 5001, 'health_paths': ['/health', '/'], 'any_answer': True},
]

# Scheduled task names, in order of preference, when --task is not given:
# the per-service tasks registered by install-manager-dashboard-taskscheduler.ps1
# (launchers with a restart loop) if both exist, else the single startup task
# of older installs.
SPLIT_TASKS = ['Lab Monitor Manager', 'Lab Monitor Dashboard']
STARTUP_TASK = 'Lab Monitor Startup'

DEFAULT_GIT_PATH = 'E:\\Users\\lab-monitor\\scripts\\lab-monitor'

# Per-command limit (seconds) for remote commands; set from --command-timeout.
_command_timeout = 60


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def load_collectors_csv(csv_path: str) -> Dict[str, Dict]:
    """Load collectors.csv and index by hostname."""
    collectors = {}
    try:
        rows, _fieldnames = _ssh.read_inventory(csv_path)
        for row in rows:
            host = _ssh.row_value(row, 'ip')
            if host:
                collectors[host] = row
        logger.info(f"Loaded {len(collectors)} collectors from {csv_path}")
        return collectors
    except Exception as e:
        logger.error(f"Failed to load {csv_path}: {e}")
        sys.exit(1)


def find_manager_host(collectors: Dict[str, Dict], target_host: str) -> Optional[Dict]:
    """Find manager/dashboard host in collectors."""
    if target_host in collectors:
        return collectors[target_host]

    # Try to find atlantis in the list
    for host, config in collectors.items():
        if 'atlantis' in host.lower():
            logger.info(f"Found manager host: {host}")
            return config

    logger.error(f"Manager host {target_host} not found in collectors.csv")
    return None


# ---------------------------------------------------------------------------
# Paths and process matching
# ---------------------------------------------------------------------------

def windows_path(path: str) -> str:
    """Normalize a CSV path (forward or back slashes) to a Windows path without trailing slash."""
    return (path or '').strip().replace('/', '\\').rstrip('\\')


def pid_file_path(git_path: str, pid_filename: str) -> str:
    """<git_path>\\.pids\\<pid_filename> — where manager.py / app.py write their PID."""
    return f"{windows_path(git_path)}\\.pids\\{pid_filename}"


def config_file_path(git_path: str, subdir: str) -> str:
    """<git_path>\\<subdir>\\config.json — the service's config (for its port)."""
    return f"{windows_path(git_path)}\\{subdir}\\config.json"


def is_service_process(name: str, command_line: str, script: str) -> bool:
    """
    True if a process looks like python running the given script, e.g.
    name 'python.exe', command line 'C:\\...\\python.exe manager.py --config config.json'.
    """
    if not command_line or not (name or '').lower().startswith('python'):
        return False
    pattern = r'(^|[\s"\'\\/])' + re.escape(script) + r'($|[\s"\'])'
    return re.search(pattern, command_line, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(row: dict, key_filename: Optional[str] = None, timeout: float = 10):
    """Establish SSH connection (trust-on-first-use host keys; see _ssh.py)."""
    row = dict(row)
    if not _ssh.row_value(row, 'username'):
        row['username'] = 'admin'
    hostname = _ssh.row_value(row, 'ip')
    try:
        logger.info(f"Connecting to {hostname} as {row['username']}...")
        client = _ssh.connect_row(row, timeout=timeout, key_filename=key_filename,
                                  log=logger.debug)
        logger.info(f"✓ Connected to {hostname}")
        return client
    except Exception as e:
        logger.error(f"SSH connection failed: {e}")
        sys.exit(1)


def run_command(client, command: str, description: str = None) -> tuple:
    """Run a command via SSH and return (stdout, stderr, exit_code)."""
    if description:
        logger.info(f"Running: {description}")
    else:
        logger.info(f"Running: {command}")

    try:
        out, err, exit_code = _ssh.exec_command(client, command, timeout=_command_timeout,
                                                log=logger.debug)
        if exit_code != 0 and err:
            logger.warning(f"Command returned {exit_code}: {err.strip()}")
        return out, err, exit_code
    except Exception as e:
        logger.error(f"Command execution failed: {e}")
        raise


def run_powershell(client, script: str, description: str) -> tuple:
    """Run a PowerShell script on the server. Returns (stdout, stderr, exit_code)."""
    return run_command(client, _ssh.powershell_command(script), description)


def parse_json_output(out: str):
    """Parse the JSON document a status script printed (last line starting with { or [)."""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            return json.loads(line)
    raise ValueError(f"no JSON in output: {out[:200]!r}")


# PowerShell fragment: $proc = the Win32_Process for PID $target (or $null).
# Falls back to Get-WmiObject where CIM is unavailable.
_PS_GET_PROCESS = '''
$proc = $null
try { $proc = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId=" + $target) }
catch { $proc = Get-WmiObject -Class Win32_Process -Filter ("ProcessId=" + $target) }
'''


def build_status_script(git_path: str, service: dict) -> str:
    """PowerShell 5.1 script printing one JSON line: PID file, process and HTTP state."""
    q = _ssh.ps_quote
    paths = ', '.join(q(p) for p in service['health_paths'])
    return f'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pidFile = {q(pid_file_path(git_path, service['pid_file']))}
$configFile = {q(config_file_path(git_path, service['subdir']))}
$port = {int(service['port'])}
$hostName = '127.0.0.1'
$info = [ordered]@{{ pid_file_exists = $false; pid = $null; alive = $false; name = $null;
    command_line = $null; port = $null; http_status = $null; http_url = $null; http_error = $null }}
try {{
    $cfg = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    if ($cfg.port) {{ $port = [int]$cfg.port }}
    if ($cfg.host -and $cfg.host -ne '0.0.0.0' -and $cfg.host -ne '::') {{ $hostName = [string]$cfg.host }}
}} catch {{ }}
$info.port = $port
if (Test-Path -LiteralPath $pidFile) {{
    $info.pid_file_exists = $true
    $raw = ([string](Get-Content -LiteralPath $pidFile -Raw)).Trim()
    if ($raw -match '^\\d+$') {{
        $info.pid = [int]$raw
        $target = $raw
{_PS_GET_PROCESS}
        if ($proc) {{
            $info.alive = $true
            $info.name = [string]$proc.Name
            $info.command_line = [string]$proc.CommandLine
        }}
    }}
}}
foreach ($p in @({paths})) {{
    $url = 'http://' + $hostName + ':' + $port + $p
    $info.http_url = $url
    try {{
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        $info.http_status = [int]$r.StatusCode
        $info.http_error = $null
        break
    }} catch {{
        $info.http_status = $null
        $resp = $_.Exception.Response
        if ($resp) {{ try {{ $info.http_status = [int]$resp.StatusCode }} catch {{ }} }}
        $info.http_error = [string]$_.Exception.Message
    }}
}}
$info | ConvertTo-Json -Compress
'''


def build_kill_script(pid: int, command_line: str) -> str:
    """
    PowerShell 5.1 script that kills PID only if it still runs exactly the
    command line we verified, then waits for it to exit. Prints RESULT: KILLED,
    NOT_RUNNING, CHANGED or STILL_RUNNING.
    """
    return f'''$ErrorActionPreference = 'Stop'
$target = {int(pid)}
$expected = {_ssh.ps_quote(command_line)}
{_PS_GET_PROCESS}
if (-not $proc) {{ Write-Output 'RESULT: NOT_RUNNING'; exit 0 }}
if ([string]$proc.CommandLine -cne $expected) {{ Write-Output 'RESULT: CHANGED'; exit 0 }}
$ErrorActionPreference = 'Continue'
$out = & taskkill.exe /PID $target /F 2>&1
Write-Output ($out | Out-String).Trim()
for ($i = 0; $i -lt 20; $i++) {{
    if (-not (Get-Process -Id $target -ErrorAction SilentlyContinue)) {{ Write-Output 'RESULT: KILLED'; exit 0 }}
    Start-Sleep -Milliseconds 500
}}
Write-Output 'RESULT: STILL_RUNNING'
exit 1
'''


_PYTHON_PROCESSES_SCRIPT = '''$ErrorActionPreference = 'Stop'
$procs = @()
try { $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name like 'python%'") }
catch { $procs = @(Get-WmiObject -Class Win32_Process -Filter "Name like 'python%'") }
$rows = @($procs | ForEach-Object { [ordered]@{ pid = [int]$_.ProcessId; name = [string]$_.Name; command_line = [string]$_.CommandLine } })
ConvertTo-Json -InputObject $rows -Compress
'''

_FIND_TASKS_SCRIPT = '''$names = @({names})
$found = @()
foreach ($n in $names) {{
    & schtasks.exe /query /tn $n *> $null
    if ($LASTEXITCODE -eq 0) {{ $found += $n }}
}}
ConvertTo-Json -InputObject @($found) -Compress
'''

_RUN_TASK_SCRIPT = '''$ErrorActionPreference = 'Continue'
$out = & schtasks.exe /run /tn {name} 2>&1
Write-Output ($out | Out-String).Trim()
exit $LASTEXITCODE
'''

_END_TASK_SCRIPT = '''$ErrorActionPreference = 'Continue'
$out = & schtasks.exe /end /tn {name} 2>&1
Write-Output ($out | Out-String).Trim()
exit $LASTEXITCODE
'''


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def http_answering(service: dict, http_status) -> bool:
    """True if the service's HTTP endpoint answered as a running service should."""
    if http_status == 200:
        return True
    return bool(service.get('any_answer')) and http_status is not None and int(http_status) < 500


def get_status(client, git_path: str, service: dict) -> dict:
    """
    Query one service. Returns the status dict from build_status_script plus:
      matches  PID file names a live python process running the service script
      healthy  matches and the HTTP endpoint answered (see http_answering)
    """
    out, err, code = run_powershell(client, build_status_script(git_path, service),
                                    f"Check {service['label']}")
    try:
        status = parse_json_output(out)
    except ValueError as e:
        status = {'error': f"{e} {err[:200]}".strip()}
    status['matches'] = bool(status.get('alive')) and is_service_process(
        status.get('name'), status.get('command_line'), service['script'])
    status['healthy'] = status['matches'] and http_answering(service, status.get('http_status'))
    return status


def describe_status(service: dict, status: dict) -> str:
    """One-line human summary of a status dict."""
    if status.get('error'):
        return f"status check failed: {status['error']}"
    if not status.get('pid_file_exists'):
        proc = "no PID file"
    elif status.get('pid') is None:
        proc = "PID file unreadable"
    elif not status.get('alive'):
        proc = f"PID {status['pid']} not running (stale PID file)"
    elif not status['matches']:
        proc = (f"PID {status['pid']} is {status.get('name')!r} "
                f"({(status.get('command_line') or 'command line unavailable')[:120]}), "
                f"not {service['script']} — stale PID file")
    else:
        proc = f"PID {status['pid']} running {service['script']}"
    if status.get('http_status') == 200:
        http = f"HTTP 200 from {status.get('http_url')}"
    elif http_answering(service, status.get('http_status')):
        http = f"HTTP {status['http_status']} from {status.get('http_url')} (answering; login required?)"
    elif status.get('http_status'):
        http = f"HTTP {status['http_status']} from {status.get('http_url')}"
    else:
        http = f"no HTTP response on port {status.get('port')} ({status.get('http_error') or 'unknown'})"
    return f"{proc}; {http}"


def python_processes(client) -> list:
    """All python* processes on the server: [{pid, name, command_line}]."""
    out, _err, _code = run_powershell(client, _PYTHON_PROCESSES_SCRIPT, "List Python processes")
    try:
        procs = parse_json_output(out)
    except ValueError:
        return []
    if isinstance(procs, dict):     # a single object if PowerShell unrolled the array
        procs = [procs]
    return procs or []


def list_processes(client, git_path: str) -> None:
    """Show each service's PID file / process / HTTP state, plus all Python processes."""
    logger.info("=== Current Processes ===")
    for service in SERVICES:
        status = get_status(client, git_path, service)
        logger.info(f"{service['label']}: {describe_status(service, status)}")
        logger.info(f"  PID file: {pid_file_path(git_path, service['pid_file'])}")

    logger.info("=" * 50)
    logger.info("All Python processes:")
    procs = python_processes(client)
    if procs:
        for p in procs:
            logger.info(f"  PID {p.get('pid')}: {p.get('command_line') or p.get('name')}")
    else:
        logger.info("  (none found)")
    logger.info("=" * 50)


# ---------------------------------------------------------------------------
# Stop / start / verify
# ---------------------------------------------------------------------------

def end_tasks(client, tasks: list) -> None:
    """
    End the given scheduled tasks (schtasks /end) so their launcher loops stop
    restarting the services. A task that isn't running is not an error.
    """
    for task_name in tasks:
        out, err, code = run_powershell(client, _END_TASK_SCRIPT.format(name=_ssh.ps_quote(task_name)),
                                        f"End task {task_name}")
        if code == 0:
            logger.info(f"✓ Task '{task_name}' ended ({out.strip() or 'ok'})")
        else:
            logger.info(f"Task '{task_name}' not ended (probably not running): {(out or err).strip()[:200]}")


def kill_processes(client, git_path: str, tasks: Optional[list] = None) -> tuple:
    """
    Stop Manager and Dashboard: end their scheduled tasks (if given), then kill
    any process still named by a PID file — but only after confirming it runs
    manager.py / app.py.

    Returns (ok, stopped_pids): ok is False if a verified process could not be
    stopped; stopped_pids maps label -> PID that was verified running before
    and is gone now. A PID that wasn't running is not counted as stopped.
    """
    logger.info("=== Stopping Processes ===")
    ok = True
    stopped = {}
    others = None   # python process list, fetched lazily for stale-PID hints

    before = {s['label']: get_status(client, git_path, s) for s in SERVICES}
    if tasks:
        end_tasks(client, tasks)

    for service in SERVICES:
        label = service['label']
        was = before[label]
        status = get_status(client, git_path, service) if tasks else was

        if not status['matches']:
            if was['matches'] and status.get('pid') == was.get('pid') and not status.get('alive'):
                logger.info(f"✓ {label} stopped with its task (PID {was['pid']})")
                stopped[label] = was['pid']
                continue
            if was['matches'] and status.get('pid') != was.get('pid'):
                # The launcher restarted it between our checks; handled on the next run.
                logger.warning(f"{label}: PID changed from {was['pid']} to {status.get('pid')} "
                               f"while stopping — {describe_status(service, status)}")
            else:
                logger.warning(f"{label}: nothing to stop — {describe_status(service, status)}")
            if others is None:
                others = python_processes(client)
            strays = [p for p in others
                      if is_service_process(p.get('name'), p.get('command_line'), service['script'])]
            for p in strays:
                logger.warning(f"  Found python process PID {p.get('pid')} running {service['script']} "
                               f"that is not in the PID file — NOT killed: {p.get('command_line')}")
                logger.warning("  If it is a leftover Lab Monitor process, stop it manually "
                               f"(taskkill /PID {p.get('pid')} /F) before restarting.")
            continue

        pid = status['pid']
        logger.info(f"Stopping {label} (PID {pid}: {status['command_line'][:120]})...")
        out, _err, _code = run_powershell(client, build_kill_script(pid, status['command_line']),
                                          f"Kill {label} PID {pid}")
        result = re.search(r'RESULT: (\w+)', out)
        result = result.group(1) if result else 'UNKNOWN'
        if result == 'KILLED':
            logger.info(f"✓ {label} stopped (PID {pid})")
            stopped[label] = pid
        elif result == 'NOT_RUNNING':
            logger.warning(f"{label} PID {pid} exited before it could be killed")
            if was['matches'] and was.get('pid') == pid:
                stopped[label] = pid
        elif result == 'CHANGED':
            logger.warning(f"{label} PID {pid} now runs a different command line — not killed")
        else:
            logger.error(f"✗ Could not stop {label} PID {pid} ({result}): {out.strip()[:300]}")
            ok = False

    # No manager.py may still run when the Manager is started again: two
    # Managers on one data_dir must never happen.  The new Manager refuses a
    # data_dir another one holds, but an older Manager takes no lock, and on
    # Windows a second server can bind the same port (SO_REUSEADDR), so
    # neither the lock nor the port stops it.
    leftovers = manager_processes(client)
    if leftovers:
        ok = False
        for p in leftovers:
            logger.error(f"✗ python PID {p.get('pid')} still runs manager.py after the stop "
                         f"(not the Manager named by the PID file, or it did not exit): "
                         f"{p.get('command_line')}")
        logger.error("  Not starting the Manager while it runs. If it is a leftover Lab Monitor "
                     "Manager (for example one still migrating after its task was ended), let it "
                     "finish or stop it (taskkill /PID <pid> /F), then run this tool again.")

    if stopped:
        logger.info(f"✓ Stopped {len(stopped)} process(es)")
    else:
        logger.warning("No processes were stopped.")
    return ok, stopped


def manager_processes(client) -> list:
    """python processes on the server that run manager.py: [{pid, name, command_line}]."""
    return [p for p in python_processes(client)
            if is_service_process(p.get('name'), p.get('command_line'), 'manager.py')]


def find_tasks(client) -> list:
    """Which of the known Lab Monitor scheduled tasks exist on the server."""
    names = ', '.join(_ssh.ps_quote(n) for n in SPLIT_TASKS + [STARTUP_TASK])
    out, _err, _code = run_powershell(client, _FIND_TASKS_SCRIPT.format(names=names),
                                      "Look up scheduled tasks")
    try:
        found = parse_json_output(out)
    except ValueError:
        return []
    return [found] if isinstance(found, str) else list(found or [])


def choose_tasks(found: list) -> list:
    """Tasks to run: both split tasks if present, else the startup task, else whatever exists."""
    if all(t in found for t in SPLIT_TASKS):
        return list(SPLIT_TASKS)
    if STARTUP_TASK in found:
        return [STARTUP_TASK]
    return list(found)


def resolve_tasks(client, explicit: Optional[list] = None) -> list:
    """The --task names if given, else the Lab Monitor tasks found on the server."""
    if explicit:
        return list(explicit)
    tasks = choose_tasks(find_tasks(client))
    if tasks:
        logger.info(f"Scheduled task(s): {', '.join(tasks)}")
    return tasks


def start_processes(client, tasks: Optional[list] = None) -> bool:
    """Start manager and dashboard via Task Scheduler on Windows. Returns True if all tasks ran."""
    logger.info("=== Starting Services via Task Scheduler ===")

    if not tasks:
        tasks = resolve_tasks(client)
        if not tasks:
            logger.error("No Lab Monitor scheduled task found "
                         f"(looked for {', '.join(SPLIT_TASKS + [STARTUP_TASK])}); pass --task NAME")
            return False

    ok = True
    for task_name in tasks:
        logger.info(f"Starting task: {task_name}...")
        out, err, code = run_powershell(client, _RUN_TASK_SCRIPT.format(name=_ssh.ps_quote(task_name)),
                                        f"Start {task_name}")
        if code == 0:
            logger.info(f"✓ {task_name} started ({out.strip() or 'ok'})")
        else:
            logger.error(f"✗ {task_name} did not start (exit {code}): {out.strip() or err.strip()}")
            ok = False
    return ok


def verify_services(client, git_path: str, stopped_pids: Optional[dict] = None,
                    timeout: float = 30, interval: float = 3) -> bool:
    """
    Poll until every service is healthy: its PID file names a live python
    manager.py/app.py process (not the PID just stopped) and its HTTP endpoint
    answers 200. Returns True if all services verified within timeout.
    """
    logger.info("=== Verifying Services ===")
    stopped_pids = stopped_pids or {}
    deadline = time.monotonic() + timeout
    while True:
        statuses = {s['label']: get_status(client, git_path, s) for s in SERVICES}
        good = {}
        for service in SERVICES:
            st = statuses[service['label']]
            old = stopped_pids.get(service['label'])
            good[service['label']] = st['healthy'] and (old is None or st.get('pid') != old)
        if all(good.values()) or time.monotonic() >= deadline:
            break
        time.sleep(interval)

    logger.info("=" * 50)
    for service in SERVICES:
        label = service['label']
        st = statuses[label]
        if good[label]:
            logger.info(f"✓ {label}: {describe_status(service, st)}")
        else:
            detail = describe_status(service, st)
            if st.get('healthy') and st.get('pid') == stopped_pids.get(label):
                detail += " (PID file still names the process that was stopped)"
            logger.error(f"✗ {label} not verified: {detail}")
    if all(good.values()):
        logger.info("✓ All services verified and running!")
    else:
        logger.error(f"⚠ {sum(1 for v in good.values() if not v)} service(s) not verified "
                     f"after {timeout:g}s — check the logs on the server")
    logger.info("=" * 50)
    return all(good.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _ssh.require_paramiko()

    parser = argparse.ArgumentParser(
        description='Restart Lab Monitor Manager/Dashboard on atlantis server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--csv', required=True, help='Path to collectors.csv')
    parser.add_argument('--host', default='atlantis.med.harvard.edu', help='Manager hostname')
    parser.add_argument('--key', help='SSH private key file (optional; overrides the CSV key_filename)')
    parser.add_argument('--list-processes', action='store_true', help='List processes and exit')
    parser.add_argument('--kill-only', action='store_true', help='Kill processes only, no restart')
    parser.add_argument('--start-only', action='store_true', help='Start processes only, no kill')
    parser.add_argument('--task', action='append', metavar='NAME',
                        help='Scheduled task to end when stopping and run when starting (repeatable). '
                             f'Default: "{SPLIT_TASKS[0]}" + "{SPLIT_TASKS[1]}" if both exist, '
                             f'else "{STARTUP_TASK}"')
    parser.add_argument('--timeout', type=int, default=10,
                        help='SSH connect timeout in seconds (default: 10)')
    parser.add_argument('--command-timeout', type=int, default=60, metavar='SECONDS',
                        help='Limit for each remote command (default: 60)')
    parser.add_argument('--verify-timeout', type=int, default=60, metavar='SECONDS',
                        help='How long to wait for both services to come up (default: 60)')

    args = parser.parse_args()

    global _command_timeout
    _command_timeout = args.command_timeout

    # Load collectors
    collectors = load_collectors_csv(args.csv)
    config = find_manager_host(collectors, args.host)
    if not config:
        sys.exit(1)

    git_path = _ssh.row_value(config, 'git_path', DEFAULT_GIT_PATH)
    logger.info(f"Repo on server: {windows_path(git_path)} (PID files in .pids\\)")

    client = ssh_connect(config, args.key, timeout=args.timeout)

    ok = True
    try:
        if args.list_processes:
            list_processes(client, git_path)
        elif args.kill_only:
            ok, _stopped = kill_processes(client, git_path, resolve_tasks(client, args.task))
            if ok:
                logger.info("Services stay stopped until started again (--start-only, or reboot).")
        elif args.start_only:
            mgr = get_status(client, git_path, SERVICES[0])
            strays = [p for p in manager_processes(client)
                      if not (mgr['matches'] and p.get('pid') == mgr.get('pid'))]
            if strays:
                ok = False
                logger.error("✗ Not starting: manager.py runs as python PID(s) "
                             f"{', '.join(str(p.get('pid')) for p in strays)}, which the PID file "
                             "does not name; two Managers must never share a data_dir. Stop that "
                             "process first (or use a full restart, which refuses the same way).")
            else:
                ok = start_processes(client, args.task)
            if ok:
                time.sleep(3)
                ok = verify_services(client, git_path, timeout=args.verify_timeout)
        else:
            # Full restart
            list_processes(client, git_path)
            tasks = resolve_tasks(client, args.task)
            ok, stopped = kill_processes(client, git_path, tasks)
            if not ok:
                logger.error("Not starting services: a running process could not be stopped.")
            else:
                time.sleep(1)
                ok = start_processes(client, tasks)
                if ok:
                    time.sleep(3)
                    ok = verify_services(client, git_path, stopped, timeout=args.verify_timeout)
            if ok:
                logger.info("✓ Manager/Dashboard restart complete!")
            else:
                logger.error("✗ Manager/Dashboard restart did not complete — see messages above")
    finally:
        client.close()

    sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
