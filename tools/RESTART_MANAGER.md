# Restart Manager/Dashboard Tool

Uses SSH (paramiko) to connect to the atlantis manager/dashboard server and restart the services.

**Safe approach**: Kills processes by port (5000/5001), not by process name. Won't accidentally kill unrelated Python processes.

## Installation

```bash
pip install paramiko
```

## Usage

### Full restart (kill + start)
```bash
python restart_manager_dashboard.py --csv collectors.csv
```

### List current processes (by port)
```bash
python restart_manager_dashboard.py --csv collectors.csv --list-processes
```
Shows what's listening on ports 5000 (Manager) and 5001 (Dashboard), plus all Python processes.

### Kill only (don't restart)
```bash
python restart_manager_dashboard.py --csv collectors.csv --kill-only
```

### Start only (don't kill first)
```bash
python restart_manager_dashboard.py --csv collectors.csv --start-only
```

### Specify a custom host
```bash
python restart_manager_dashboard.py --csv collectors.csv --host custom.hostname.edu
```

### Use SSH key instead of password
```bash
python restart_manager_dashboard.py --csv collectors.csv --key ~/.ssh/atlantis_key
```

## How It Works

1. **Reads collectors.csv** — Finds the atlantis manager server entry
2. **Establishes SSH connection** — Uses paramiko with password or SSH key
3. **Lists current processes** — Shows what's listening on ports 5000 (Manager) and 5001 (Dashboard)
4. **Kills processes** — Uses `netstat` to find PIDs on specific ports, then `taskkill /PID` to kill them
   - This is **safe** — only kills what's listening on those specific ports
   - Won't accidentally kill unrelated Python processes
5. **Restarts services** — Uses Windows Task Scheduler (`schtasks /run`) to restart:
   - "Lab Monitor Manager" (port 5000)
   - "Lab Monitor Dashboard" (port 5001)
6. **Verifies** — Checks that services are listening on their ports again

## collectors.csv Format

```csv
ip,username,password,git_path,python_path
atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe
```

- **ip**: Hostname or IP address
- **username**: SSH username
- **password**: SSH password (leave blank if using SSH key)
- **git_path**: Path to lab-monitor repo (not used by this tool yet)
- **python_path**: Path to Python (not used by this tool yet)

## What Gets Killed/Restarted

The tool targets specific ports (safer than process names):
- **Port 5000** — Lab Monitor Manager (Flask service)
- **Port 5001** — Lab Monitor Dashboard (Flask UI)

Processes are killed by PID (found via `netstat`), not by name. This is safe and specific.

Services are restarted via Windows Task Scheduler tasks:
- "Lab Monitor Manager"
- "Lab Monitor Dashboard"

These task names must match what was created during `install-manager-dashboard-taskscheduler.ps1`.

## Port-Based Killing (Safe!)

The tool uses **port-based process killing**, which is much safer than targeting by name:

```bash
# Find what's on port 5000
netstat -ano | findstr :5000

# Kill by PID (specific, safe)
taskkill /PID 12345 /F
```

This means:
- ✅ Only kills what's actually listening on those ports
- ✅ Won't kill unrelated `app.py` instances
- ✅ Can safely run even with multiple Python apps
- ✅ You can see exactly what PIDs get killed

## SSH Authentication

Options in order of precedence:
1. `--key` argument (SSH private key file)
2. `collectors.csv` password field
3. Default SSH key (~/.ssh/id_rsa)

## Troubleshooting

**"Connection refused"** — Make sure atlantis is reachable and SSH is enabled.

**"Authentication failed"** — Check username and password in collectors.csv, or provide SSH key with `--key`.

**"Task not found"** — The Task Scheduler tasks may have different names. Check on the server:
```powershell
schtasks /query | findstr "Lab Monitor"
```

Update the task names in the tool if needed.

**Ports not showing as listening** — Services take a few seconds to start. Wait 5-10 seconds and check:
```bash
python restart_manager_dashboard.py --csv collectors.csv --list-processes
```

**SSH as Non-Admin User** — If the SSH user is not admin, Task Scheduler restart may fail. Ensure the SSH user is in the Administrators group on Windows.

**No processes killed but services still restart** — This is normal if the services were already down. The tool will still restart them via Task Scheduler.

## Example Output

```
2025-08-09 08:35:00 [INFO] Loaded 3 collectors from collectors.csv
2025-08-09 08:35:00 [INFO] Found manager host: atlantis.med.harvard.edu
2025-08-09 08:35:01 [INFO] Connecting to atlantis.med.harvard.edu with password...
2025-08-09 08:35:02 [INFO] ✓ Connected to atlantis.med.harvard.edu
2025-08-09 08:35:02 [INFO] === Current Processes ===
2025-08-09 08:35:02 [INFO] Port 5000 (Manager):
2025-08-09 08:35:02 [INFO]   TCP    0.0.0.0:5000    0.0.0.0:0    LISTENING    55692
2025-08-09 08:35:02 [INFO]   → PID 55692 is using this port
2025-08-09 08:35:02 [INFO] === Killing Processes on Ports 5000 & 5001 ===
2025-08-09 08:35:02 [INFO] Killing Manager (PID 55692)...
2025-08-09 08:35:03 [INFO] ✓ Killed 1 process(es)
2025-08-09 08:35:03 [INFO] === Starting Services via Task Scheduler ===
2025-08-09 08:35:04 [INFO] ✓ Manager started
2025-08-09 08:35:04 [INFO] ✓ Dashboard started
2025-08-09 08:35:07 [INFO] === Verifying Services ===
2025-08-09 08:35:08 [INFO] ✓ Manager is listening on port 5000
2025-08-09 08:35:08 [INFO] ✓ Dashboard is listening on port 5001
2025-08-09 08:35:08 [INFO] ✓ All services verified and running!
```
