# Restart Manager/Dashboard Tool

Uses SSH (paramiko) to connect to the atlantis manager/dashboard server and restart the services.

## Installation

```bash
pip install paramiko
```

## Usage

### Full restart (kill + restart)
```bash
python restart_manager_dashboard.py --csv collectors.csv
```

### List currently running processes
```bash
python restart_manager_dashboard.py --csv collectors.csv --list-processes
```

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
3. **Lists current processes** — Shows what Python processes are running
4. **Kills processes** — Sends `taskkill /F` for manager.py and app.py
5. **Restarts services** — Uses Windows Task Scheduler (`schtasks /run`) to restart:
   - "Lab Monitor Manager"
   - "Lab Monitor Dashboard"
6. **Verifies** — Checks that processes are running again

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

The tool uses Windows Task Scheduler tasks:
- **"Lab Monitor Manager"** — Flask manager service (port 5000)
- **"Lab Monitor Dashboard"** — Flask dashboard UI (port 5001)

These task names must match what was created during `install-manager-dashboard-taskscheduler.ps1`.

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

**Processes still running after restart** — Services take a few seconds to start. Wait 5-10 seconds and check again with `--list-processes`.

## SSH as Non-Admin User

If the SSH user is not admin, Task Scheduler restart may fail. Either:
- Use an admin account
- Store the password in collectors.csv
- Set up sudoers rules (if on Linux-based SSH)

For Windows, ensure the SSH user is in the Administrators group.
