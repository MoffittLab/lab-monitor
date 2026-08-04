# Installation Guide

Three automated installation scripts are provided for quick deployment.

## Quick Start

1. **Install Manager + Dashboard** on Windows Server (atlantis)
2. **Install Collector** on each Synology NAS
3. **Install Collector** on each Windows server

---

## 1. Manager + Dashboard (Windows Server atlantis)

**Prerequisites:**
- Windows Server 2019 or later
- Administrator access
- Miniconda installed (https://docs.conda.io/projects/miniconda/en/latest/)
- Git installed

**Installation (run as Administrator):**

```powershell
# Clone the repository
mkdir -p E:\Users\lab-monitor
cd E:\Users\lab-monitor
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor

# Allow script execution for this session
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process

# Generate secure token for authentication
$Token = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes((New-Guid).ToString())) -replace '=', ''
$Token = $Token.Substring(0, 32)
Write-Host "Your Manager Token: $Token"

# Run installation from the repo
.\install-manager-dashboard-taskscheduler.ps1 -ManagerToken $Token
```

**What it does:**
- Creates directory structure (`E:\Users\lab-monitor\data`, `logs`, `scripts`)
- Sets up Conda environment (`lab-monitor`)
- Clones/updates lab-monitor repository via git
- Installs dependencies (Flask, requests, psutil)
- Creates Manager config (`manager/config.json`)
- Creates Dashboard config (`dashboard/config.json`)
- Tests both services locally
- Creates launcher batch file (`E:\Users\lab-monitor\start-services.bat`)
- Registers auto-startup task in Windows Task Scheduler
- Configures firewall (ports 5000, 5001)
- Starts services and verifies they're running

**After installation:**
- Manager runs on port 5000 (receives data from collectors)
- Dashboard runs on port 5001 (web UI)
- Both auto-start on server reboot
- Both auto-restart if they crash

**Verify:**
```powershell
# Check services running
Get-Service "Lab Monitor*" | select Name, Status

# Access Dashboard
# http://atlantis.med.harvard.edu:5001

# Check logs
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 20
Get-Content E:\Users\lab-monitor\logs\dashboard.log -Tail 20
```

**Troubleshooting: Services won't start on reboot**

If services don't start automatically on reboot, check:

```powershell
# Check if Task Scheduler task exists and is enabled
Get-ScheduledTask -TaskName "Lab Monitor Startup" | Format-List

# Check if services are running
Get-Process python | Where-Object {$_.CommandLine -match "manager|app"}

# Check logs
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 50
Get-Content E:\Users\lab-monitor\logs\dashboard.log -Tail 50
```

**Common issues:**

1. **Task Scheduler task is disabled**: Open `taskschd.msc`, find "Lab Monitor Startup", right-click and select **Enable**.
2. **Launcher script not found**: Verify `E:\Users\lab-monitor\start-services.bat` exists.
3. **Services start but crash immediately**: Check the log files above. Usually a config or environment issue.
4. **Services don't stay running after manual start**: Try running the launcher batch file directly:
   ```powershell
   E:\Users\lab-monitor\start-services.bat
   ```

**To manually start/stop services:**

```powershell
# Start services manually
E:\Users\lab-monitor\start-services.bat

# Kill services
Stop-Process -Name python -Filter "manager|app" -Force

# Or use Task Scheduler:
taskschd.msc  # Right-click "Lab Monitor Startup" task and select Run
```

**To reinstall the Task Scheduler task:**

```powershell
# Remove old task
Unregister-ScheduledTask -TaskName "Lab Monitor Startup" -Confirm:$false

# Re-run the installation script
.\install-manager-dashboard-taskscheduler.ps1 -ManagerToken $YourToken
```

---

## 2. Collector on Synology NAS

**Prerequisites:**
- SSH access to NAS as admin user (any admin account name: jeff, john, admin, etc.)
- Git installed (Control Panel → Package Center → Git Server)
- Python 3 installed (Control Panel → Package Center → Python)
- Admin user must have read/write access to `/volume1`

**Installation (SSH as any admin user):**

```bash
# SSH as your admin user (not root)
ssh your-admin-user@nas.local
  # e.g., ssh jeff@nas.local
  # or ssh john@nas.local

# Clone the repository and run the installation script
cd /tmp
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor
./install-collector-synology.sh
```

**What it does:**
- Creates directory structure (`/volume1/lab-monitor/data`, `logs`, `scripts`)
- Creates Python virtual environment
- Clones lab-monitor repository to `/volume1/lab-monitor/scripts/lab-monitor`
- Installs dependencies (requests, psutil)
- **Prompts for configuration interactively** (Manager URL, token, device type, scan depth, volumes)
- Creates collector config (`collector/local/config.json`)
- Tests metrics collection

**Configuration (interactive during install):**

The installer will prompt you for:
- **Manager URL** (e.g., `http://atlantis.med.harvard.edu:5000`)
- **Manager Token** (copy from your Manager config)
- **Device Type** (`NAS`, `NAS-Instrument`, `NAS-Backup`, or `Server`)
- **Scan Depth** (1, 2, or 3 — auto-suggested based on device type)
- **Volumes** (auto-detected; defaults shown, press Enter to accept)

After installation, the config is at:
```bash
/volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json
```

To edit later:
```bash
nano /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json
```

**Schedule jobs (Synology Control Panel):**

After the installer completes, set up two scheduled tasks:

1. **Control Panel → Task Scheduler**
2. **Create → Scheduled Task → Custom Script**

**Job 1: Disk Collection (Daily at 2 AM)**
- Task name: `Lab Monitor - Disk Collection`
- User: Your admin user (same one you SSH as)
- Schedule: Daily, 02:00 (2 AM)
- Script:
```bash
cd /volume1/lab-monitor/scripts/lab-monitor/collector && /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json --mode disk
```

**Job 2: Metrics Collection (Every 5 minutes)**
- Task name: `Lab Monitor - Metrics Collection`
- User: Your admin user (same one you SSH as)
- Schedule: Daily, 00:00, repeat every 5 minutes
- Script:
```bash
cd /volume1/lab-monitor/scripts/lab-monitor/collector && /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json --mode metrics
```

**Verify:**
```bash
# Check logs
tail -f /volume1/lab-monitor/logs/collector.log

# Check archive growing
ls -lah /volume1/lab-monitor/data/

# After first 2 AM run, verify on Manager:
curl -H "Authorization: Bearer YOUR-TOKEN" http://atlantis.med.harvard.edu:5000/health
```

---

## 3. Collector on Windows Server

**Prerequisites:**
- Windows Server 2019 or later
- Administrator access
- Python 3 (or Miniconda: https://docs.conda.io/projects/miniconda/en/latest/)
- Git installed

**Installation (run as Administrator):**

```powershell
# Clone the repository
mkdir -p E:\Temp
cd E:\Temp
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor

# Allow script execution for this session
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process

# Run installation from the repo
.\install-collector-windows.ps1
```

**What it does:**
- Creates directory structure at `E:\Users\lab-monitor`
- Auto-detects Python (Conda or system)
- Clones lab-monitor repository to `E:\Users\lab-monitor\scripts\lab-monitor`
- Installs dependencies (requests, psutil)
- **Prompts for configuration interactively** (Manager URL, token, device type, scan depth, volumes)
- Tests metrics collection
- Registers two Task Scheduler jobs (disk daily at 2 AM, metrics every 5 min)

**Configuration (interactive during install):**

The installer will prompt you for:
- **Manager URL** (e.g., `http://atlantis.med.harvard.edu:5000`)
- **Manager Token** (copy from your Manager config)
- **Device Type** (`NAS`, `NAS-Instrument`, `NAS-Backup`, or `Server`)
- **Scan Depth** (1, 2, or 3 — auto-suggested based on device type)
- **Volumes** (e.g., `E:`, `F:`, `G:`, etc.)

If you need to edit later:
```powershell
notepad E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json
```

**Verify:**
```powershell
# Check jobs registered
Get-ScheduledTask | findstr "Lab Monitor"

# Check logs
Get-Content E:\Users\lab-monitor\logs\collector.log -Tail 20 -Wait

# After first collection, verify on Manager
curl -H "Authorization: Bearer YOUR-TOKEN" http://<MANAGER-HOST>:5000/api/usage/all
```

---

## Configuration Template

All collectors use the same config format:

```json
{
  "name":            "system-name",
  "id":              "unique-system-id",
  "device_type":     "synology",
  "manager_url":     "http://MANAGER-HOST:5000",
  "manager_token":   "YOUR-MANAGER-TOKEN",
  "volumes":         ["/volume1", "/volume2"],
  "data_dir":        "/volume1/lab-monitor/data",
  "log_file":        "/volume1/lab-monitor/logs/collector.log",
  "log_level":       "INFO",
  "timeout_seconds": 3600
}
```

**Key fields:**
- `name` — Display name shown on the Dashboard (e.g., "Triton", "fileserver", "compute-01")
- `id` — Stable unique identifier; use something that won't change (e.g., `synology-triton`, `windows-fileserver`)
- `device_type` — System type label: `synology`, `windows`, or any string you choose
- `manager_url` — URL of your Manager service (e.g., `http://manager-host:5000`)
- `manager_token` — **Must match one of Manager's `auth_tokens`** (same token works for all collectors)
- `volumes` — Base paths to monitor; collector measures immediate subdirectories of each

---

## Troubleshooting

### Manager not starting
```powershell
# Check service status
Get-Service "Lab Monitor Manager" | select Status

# Check logs
Get-Content E:\Users\lab-monitor\logs\manager.log

# Try starting manually
cd E:\Users\lab-monitor\scripts\lab-monitor\manager
python manager.py --config config.json
```

### Collector not syncing to Manager
```bash
# Check collector log
tail -f /volume1/lab-monitor/logs/collector.log

# Verify Manager is reachable
curl http://atlantis.med.harvard.edu:5000/health

# Check queue (if Manager down, entries accumulate here)
cat /volume1/lab-monitor/data/Triton/queue.json
```

### "Invalid token" error
- Verify collector's `manager_token` matches **exactly** with one in Manager's config
- No leading/trailing spaces
- Same token should be in all collector configs

### Dashboard shows "Unable to reach Manager"
- Verify Manager service is running (not just failed to start)
- Check firewall: `netsh advfirewall firewall show rule name="Lab Monitor Manager"`
- Verify Dashboard config has correct `manager_url`

---

## Next Steps

After all installations:

1. **Monitor first 24 hours** to ensure:
   - Disk collection runs daily at 2 AM
   - Metrics collection runs every 5 minutes
   - No errors in logs
   - Dashboard shows data

2. **Enhance Dashboard** (future):
   - Add charts and visualizations
   - Historical trend analysis
   - Alerts on threshold

3. **Long-term maintenance**:
   - Monthly archival runs automatically
   - SQLite stays lean (90-120 day window)
   - JSONL archives grow ~40 MB/month (24 systems)

---

## See Also

- [README.md](README.md) - Project overview
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - System design
- [METRICS.md](METRICS.md) - Metrics collection details
- [CONFIG.md](CONFIG.md) - Configuration management
- [WINDOWS-INSTALL.md](WINDOWS-INSTALL.md) - Detailed manual setup (if not using scripts)
- [collector/README.md](collector/README.md) - Detailed collector documentation
