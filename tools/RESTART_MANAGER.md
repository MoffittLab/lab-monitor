# Restart Manager/Dashboard Tool

`restart_manager_dashboard.py` uses SSH (paramiko, via the shared `_ssh.py` helper) to connect to
the atlantis Manager/Dashboard server, stop the running Manager and Dashboard, start them again
through Windows Task Scheduler, and verify that both are really up.

**Safe approach:** it only kills a PID read from the services' own PID files, and only after
confirming that PID is a live `python` process whose command line runs `manager.py` / `app.py`.
Unrelated Python processes — or a PID that Windows has since reused for something else — are
never killed.

## Installation

```bash
pip install paramiko
```

## Usage

### Full restart (stop + start + verify)
```bash
python restart_manager_dashboard.py --csv collectors.csv
```

### Show current state
```bash
python restart_manager_dashboard.py --csv collectors.csv --list-processes
```
Shows, for each service, its PID file, whether that PID is a live `manager.py` / `app.py` python
process, and whether its HTTP endpoint answers — plus every Python process on the server with its
command line.

### Stop only (don't restart)
```bash
python restart_manager_dashboard.py --csv collectors.csv --kill-only
```
Ends the scheduled tasks too, so the launchers' restart loops don't bring the services back.
They stay stopped until `--start-only` (or a reboot).

### Start only (don't stop first), then verify
```bash
python restart_manager_dashboard.py --csv collectors.csv --start-only
```

### Specify a custom host
```bash
python restart_manager_dashboard.py --csv collectors.csv --host custom.hostname.edu
```

### Use a specific SSH key
```bash
python restart_manager_dashboard.py --csv collectors.csv --key ~/.ssh/atlantis_key
```

### Choose the scheduled task(s) to run
```bash
python restart_manager_dashboard.py --csv collectors.csv --task "Lab Monitor Startup"
python restart_manager_dashboard.py --csv collectors.csv --task "Lab Monitor Manager" --task "Lab Monitor Dashboard"
```

### Timeouts
- `--timeout` (default 10 s) — SSH connect
- `--command-timeout` (default 60 s) — each remote command
- `--verify-timeout` (default 60 s) — how long to wait for both services to come up

## How It Works

1. **Reads collectors.csv** — finds the server's row (`--host`, or the first host containing
   "atlantis"). Its `git_path` is the repo on the server (default
   `E:\Users\lab-monitor\scripts\lab-monitor`).
2. **Connects over SSH** — host key trusted on first use and pinned in `tools/.known_hosts`; a
   changed host key is refused (see [README.md](README.md#ssh-host-keys-and-authentication)).
3. **Reads the PID files** — `manager/manager.py` and `dashboard/app.py` write their PID at startup
   to `<git_path>\.pids\manager.pid` and `<git_path>\.pids\dashboard.pid`.
4. **Checks each PID** with `Get-CimInstance Win32_Process -Filter "ProcessId=N"`: the process must
   exist, be `python*.exe`, and have `manager.py` / `app.py` on its command line.
   - PID file missing, or the PID no longer running → "nothing to stop" (a stale PID file is not
     counted as a kill). If a python process running `manager.py` / `app.py` exists but is not the
     one in the PID file, it is listed as a warning and **not** killed — stop it by hand if it is a
     leftover Lab Monitor process.
   - PID now belongs to a different program → reported, not killed.
5. **Ends the scheduled tasks** (`schtasks /end`). `install-manager-dashboard-taskscheduler.ps1`
   registers "Lab Monitor Manager" and "Lab Monitor Dashboard", each running a launcher
   (`start-manager.bat` / `start-dashboard.bat`) that restarts its service 10 s after it exits —
   so the task must be ended or a killed process just comes back. Without `--task` the tool uses
   those two tasks if both exist, otherwise the single "Lab Monitor Startup" task of older
   installs (whose launcher exits right after starting the services, so ending it is harmless).
6. **Stops verified processes** still running — right before `taskkill /PID N /F` the command line
   is checked again; then the tool waits for the process to exit. A process that went away when
   its task ended counts as stopped. If a verified process cannot be stopped, the tool does not
   start new copies and exits with code 2.
7. **Starts the services** via Task Scheduler (`schtasks /run`) — the same tasks.
8. **Verifies** — polls until, for both services, the PID file names a live python
   `manager.py` / `app.py` process (a different PID than the one just stopped) **and** the HTTP
   endpoint on the server answers: Manager `http://127.0.0.1:<port>/health` must return 200;
   Dashboard `/health` or `/` may also answer 401 when its HTTP Basic login is enabled (any
   non-5xx answer shows it is serving). Ports come from `manager\config.json` / `dashboard\config.json` (`port`),
   falling back to 5000 / 5001. A PID file merely existing is not treated as "running".

All remote steps run as PowerShell 5.1 scripts sent with `-EncodedCommand`, so they work whether
the server's OpenSSH default shell is `cmd.exe` or PowerShell.

## collectors.csv Format

```csv
ip,username,password,git_path,python_path,key_filename
atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe,
```

- **ip**: Hostname or IP address
- **username**: SSH username (default `admin` if blank)
- **password**: SSH password (optional; tried after key/agent auth)
- **git_path**: Path to the lab-monitor repo on the server — the PID files are read from `<git_path>\.pids\`
- **python_path**: not used by this tool
- **key_filename**: optional SSH private key (`--key` overrides it)

## Exit Codes

- `0` — restart (or the requested step) completed and verified
- `1` — CSV problem or SSH connection failed
- `2` — a process could not be stopped, a task could not be started, or verification failed

## SSH Authentication

Options in order of precedence:
1. `--key` argument (SSH private key file), else the CSV `key_filename` column (a file that
   doesn't exist is skipped with a warning)
2. ssh-agent keys and the default `~/.ssh/id_*` keys
3. `collectors.csv` password field

## Troubleshooting

**"Connection refused"** — Make sure atlantis is reachable and SSH is enabled.

**"Authentication failed"** — Check username and password in collectors.csv, or provide an SSH key with `--key`.

**"HOST KEY CHANGED"** — The server presented a different SSH host key than the one recorded in
`tools/.known_hosts`. If the server was reinstalled or its SSH server replaced, remove its line from
`tools/.known_hosts` and run again; otherwise investigate before connecting.

**"No Lab Monitor scheduled task found"** — Re-run `install-manager-dashboard-taskscheduler.ps1`, or check
the task names on the server and pass `--task`:
```powershell
schtasks /query | findstr "Lab Monitor"
```

**"not verified" after restart** — Services can take a few seconds to start; raise
`--verify-timeout` or check again with `--list-processes`. If the PID is alive but there is no HTTP
response, check `E:\Users\lab-monitor\logs\manager.log` / `dashboard.log`; startup errors and
tracebacks are in `manager-console.log` / `dashboard-console.log` (written by the launchers). The
Manager launcher stops retrying when the Manager refuses its config (exit code 2, e.g. a
placeholder token) — fix `manager\config.json` and run `--start-only`. If a leftover process
still holds the port, the new one exits immediately — the "not in the PID file" warning from the
stop step names it.

**"command line unavailable"** — Windows hides other users' process command lines from
non-elevated sessions. SSH in as a user in the Administrators group.

**SSH as Non-Admin User** — If the SSH user is not admin, Task Scheduler restart may fail. Ensure the SSH user is in the Administrators group on Windows.

**"nothing to stop" but services restart** — Normal if the services were already down.

## Example Output

```
2026-08-09 08:35:00 [INFO] Loaded 3 collectors from collectors.csv
2026-08-09 08:35:00 [INFO] Found manager host: atlantis.med.harvard.edu
2026-08-09 08:35:00 [INFO] Repo on server: E:\Users\lab-monitor\scripts\lab-monitor (PID files in .pids\)
2026-08-09 08:35:01 [INFO] Connecting to atlantis.med.harvard.edu as admin...
2026-08-09 08:35:02 [INFO] ✓ Connected to atlantis.med.harvard.edu
2026-08-09 08:35:02 [INFO] === Current Processes ===
2026-08-09 08:35:03 [INFO] Manager: PID 55692 running manager.py; HTTP 200 from http://127.0.0.1:5000/health
2026-08-09 08:35:03 [INFO] Dashboard: PID 55710 running app.py; HTTP 200 from http://127.0.0.1:5001/
2026-08-09 08:35:03 [INFO] Scheduled task(s): Lab Monitor Manager, Lab Monitor Dashboard
2026-08-09 08:35:03 [INFO] === Stopping Processes ===
2026-08-09 08:35:04 [INFO] ✓ Task 'Lab Monitor Manager' ended (SUCCESS: The scheduled task "Lab Monitor Manager" has been terminated successfully.)
2026-08-09 08:35:04 [INFO] ✓ Task 'Lab Monitor Dashboard' ended (SUCCESS: The scheduled task "Lab Monitor Dashboard" has been terminated successfully.)
2026-08-09 08:35:05 [INFO] Stopping Manager (PID 55692: "C:\ProgramData\Miniconda3\envs\lab-monitor\python.exe" manager.py --config config.json)...
2026-08-09 08:35:05 [INFO] ✓ Manager stopped (PID 55692)
2026-08-09 08:35:06 [INFO] Stopping Dashboard (PID 55710: "C:\ProgramData\Miniconda3\envs\lab-monitor\python.exe" app.py --config config.json)...
2026-08-09 08:35:06 [INFO] ✓ Dashboard stopped (PID 55710)
2026-08-09 08:35:07 [INFO] === Starting Services via Task Scheduler ===
2026-08-09 08:35:07 [INFO] ✓ Lab Monitor Manager started (SUCCESS: Attempted to run the scheduled task "Lab Monitor Manager".)
2026-08-09 08:35:08 [INFO] ✓ Lab Monitor Dashboard started (SUCCESS: Attempted to run the scheduled task "Lab Monitor Dashboard".)
2026-08-09 08:35:10 [INFO] === Verifying Services ===
2026-08-09 08:35:14 [INFO] ✓ Manager: PID 61204 running manager.py; HTTP 200 from http://127.0.0.1:5000/health
2026-08-09 08:35:14 [INFO] ✓ Dashboard: PID 61230 running app.py; HTTP 200 from http://127.0.0.1:5001/
2026-08-09 08:35:14 [INFO] ✓ All services verified and running!
2026-08-09 08:35:14 [INFO] ✓ Manager/Dashboard restart complete!
```
