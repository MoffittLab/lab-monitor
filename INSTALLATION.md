# Installation Guide

Three automated installation scripts are provided for quick deployment.

## Quick Start

**Installation order:**

1. **Manager + Dashboard** on Windows Server (atlantis)
2. **Collector** on each Synology NAS
3. **Collector** on each Windows server

---

## 1. Manager + Dashboard (Windows Server)

**Prerequisites:**
- Windows Server 2019 or later
- Administrator access
- Miniconda installed (https://docs.conda.io/projects/miniconda/en/latest/)
- Git installed

**Installation:**

1. **Open Anaconda Prompt as Administrator**
   - Search for "Anaconda Prompt" in Start Menu
   - Right-click → "Run as Administrator"

2. **Clone repository and run installer**
   ```powershell
   mkdir -p E:\Users\lab-monitor
   cd E:\Users\lab-monitor
   git clone https://github.com/MoffittLab/lab-monitor.git
   cd lab-monitor
   
   # Generate secure token
   $Token = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes((New-Guid).ToString())) -replace '=', ''
   $Token = $Token.Substring(0, 32)
   Write-Host "Your Manager Token: $Token"
   
   # Run installer
   .\install-manager-dashboard-taskscheduler.ps1 -ManagerToken $Token
   ```

**What it does:**
- Creates directory structure (`E:\Users\lab-monitor\data`, `logs`, `scripts`)
- Sets up conda environment (`lab-monitor`)
- Clones lab-monitor repository to `E:\Users\lab-monitor\scripts\lab-monitor`
- Installs dependencies (Flask, requests, psutil)
- Creates Manager and Dashboard configs
- Registers auto-startup task in Windows Task Scheduler
- Starts services and verifies they're running

**After installation:**
- Manager runs on port 5000 (receives data from collectors)
- Dashboard runs on port 5001 (web UI)
- Both auto-start on server reboot
- Both auto-restart if they crash

**Verify:**
```powershell
# Check services running
Get-ScheduledTask | Select-String "Lab Monitor"

# Access Dashboard
# http://atlantis.med.harvard.edu:5001

# Check logs
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 20
Get-Content E:\Users\lab-monitor\logs\dashboard.log -Tail 20
```

**Troubleshooting:**

If services don't start:
```powershell
# Check task status
Get-ScheduledTask -TaskName "Lab Monitor Startup" | Format-List

# Check if services are running
Get-Process python | Where-Object {$_.CommandLine -match "manager|app"}

# Restart manually
E:\Users\lab-monitor\start-services.bat
```

---

## 2. Collector on Synology NAS

**Prerequisites:**
- SSH access to NAS as admin user (not root)
- Git installed (Control Panel → Package Center → Git Server)
- Python 3 installed (Control Panel → Package Center → Python)
- Admin user must have read/write access to `/volume1`

**Installation:**

1. **SSH as admin user (not root)**
   ```bash
   ssh your-admin-user@nas.local
   # e.g., ssh jeff@nas.local
   ```

2. **Clone repository and run installer**
   ```bash
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
- **Prompts for configuration interactively:**
  - Manager URL (e.g., `http://atlantis.med.harvard.edu:5000`)
  - Manager Token (copy from Manager config)
  - Device Type (`NAS`, `NAS-Instrument`, `NAS-Backup`, or `Server`)
  - Scan Depth (1, 2, or 3 — auto-suggested based on device type)
  - Volumes (auto-detected; defaults shown)
- Tests metrics collection

**Schedule jobs (Synology Control Panel):**

After the installer completes, set up two scheduled tasks:

1. **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined Script**

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
curl -H "Authorization: Bearer <your-token>" http://atlantis.med.harvard.edu:5000/health
```

**Edit config later:**
```bash
nano /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json
```

---

## 3. Collector on Windows Server

**Prerequisites:**
- Windows Server 2019 or later
- Administrator access
- Miniconda installed (https://docs.conda.io/projects/miniconda/en/latest/)
- Git installed

**Installation:**

1. **Open Anaconda Prompt as Administrator**
   - Search for "Anaconda Prompt" in Start Menu
   - Right-click → "Run as Administrator"

2. **(Optional) Create a conda environment**
   ```powershell
   conda create -n lab-monitor python=3.11
   conda activate lab-monitor
   ```

3. **Clone repository and run installer**
   ```powershell
   mkdir -p E:\Temp
   cd E:\Temp
   git clone https://github.com/MoffittLab/lab-monitor.git
   cd lab-monitor
   
   # If you created a conda env above:
   # conda activate lab-monitor
   
   # Run installer
   .\install-collector-windows.ps1
   ```

**What it does:**
- Creates directory structure at `E:\Users\lab-monitor`
- Uses Python from the active conda environment
- Clones lab-monitor repository to `E:\Users\lab-monitor\scripts\lab-monitor`
- Installs dependencies (requests, psutil) to the conda environment
- **Prompts for configuration interactively:**
  - Manager URL (e.g., `http://atlantis.med.harvard.edu:5000`)
  - Manager Token (copy from Manager config)
  - Device Type (`NAS`, `NAS-Instrument`, `NAS-Backup`, or `Server`)
  - Scan Depth (1, 2, or 3 — auto-suggested based on device type)
  - Volumes (e.g., `E:`, `F:`, `G:`, etc.)
- Tests metrics collection
- Registers two Task Scheduler jobs (disk daily at 2 AM, metrics every 5 min)

**Verify:**
```powershell
# Check jobs registered
Get-ScheduledTask | Select-String "Lab Monitor"

# Check logs
Get-Content E:\Users\lab-monitor\logs\collector.log -Tail 20 -Wait

# After first collection, verify on Manager
curl -H "Authorization: Bearer <your-token>" http://atlantis.med.harvard.edu:5000/health
```

**Edit config later:**
```powershell
notepad E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json
```

---

## Configuration Reference

All collectors use the same config format (created interactively by the installer):

```json
{
  "name":            "system-name",
  "id":              "unique-system-id",
  "device_type":     "NAS",
  "scan_depth":      2,
  "manager_url":     "http://MANAGER-HOST:5000",
  "manager_token":   "YOUR-MANAGER-TOKEN",
  "volumes":         ["/volume1", "/volume2"],
  "data_dir":        "/volume1/lab-monitor/data",
  "log_file":        "/volume1/lab-monitor/logs/collector.log",
  "log_level":       "INFO",
  "timeout_seconds": 3600,
  "request_timeout_seconds": 30
}
```

**Key fields:**
- `name` — Display name shown on Dashboard (e.g., "Triton", "fileserver", "compute-01")
- `id` — Stable unique identifier (e.g., `synology-triton`, `windows-fileserver`)
- `device_type` — System type: `NAS`, `NAS-Instrument`, `NAS-Backup`, `Server`
- `scan_depth` — Optional; folder recursion depth. Defaults: `NAS-Backup=1`, `NAS=2`, `Server=2`, `NAS-Instrument=3`
- `manager_url` — URL of your Manager service (e.g., `http://manager-host:5000`)
- `manager_token` — **Must match one of Manager's `auth_tokens`** (same token works for all collectors)
- `volumes` — Base paths to monitor; collector measures immediate subdirectories of each

---

## Troubleshooting

### Windows: "This script must be run from an Anaconda Prompt"

You need to launch from Anaconda Prompt, not regular cmd/PowerShell:

1. Search for "Anaconda Prompt" in Start Menu
2. Right-click and select "Run as Administrator"
3. Run the installer

Or initialize conda in PowerShell:
```powershell
conda init powershell
# Then restart PowerShell and run again
```

### Windows: Python or pip not found

Make sure conda is activated:
```powershell
conda activate lab-monitor
```

Then run the installer.

### Synology: Could not clone repository

Verify git is installed:
```bash
Control Panel → Package Center → Git Server (install if missing)
```

### Collector: Manager connection failed

Check that the Manager URL and token are correct:
```bash
# Verify Manager is reachable
curl http://atlantis.med.harvard.edu:5000/health

# Check collector config
cat /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json
```

### Collector: "Invalid token" error

Verify the token matches exactly with one in Manager's config:
- No leading/trailing spaces
- Same token should be in all collector configs

---

## Next Steps

After all installations:

1. **Monitor first 24 hours** to ensure:
   - Disk collection runs daily at 2 AM
   - Metrics collection runs every 5 minutes
   - No errors in logs
   - Dashboard shows data from all systems

2. **Archive and long-term maintenance**:
   - Monthly archives happen automatically
   - SQLite databases stay lean (default 90-120 day window)
   - JSONL archives grow ~40 MB/month per system

---

## See Also

- [README.md](README.md) — Project overview
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design
- [CONFIG.md](CONFIG.md) — Configuration and security
- [METRICS.md](METRICS.md) — Metrics reference
- [manager/README.md](manager/README.md) — Manager API reference
- [collector/README.md](collector/README.md) — Collector reference
