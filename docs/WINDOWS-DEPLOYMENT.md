# Windows Server Deployment Guide

Complete setup instructions for running lab-monitor on Windows Server 2019+.

## Overview

We'll deploy three components on Windows Server:
1. **Manager** (Flask API) - Receives and stores data
2. **Dashboard** (Flask web UI) - Displays real-time statistics
3. **Collector** (Python script) - Runs on each NAS via Task Scheduler

This guide focuses on the **Manager + Dashboard** on a central Windows Server.
See [NAS Deployment](#nas-deployment) for per-NAS setup.

---

## Prerequisites

- Windows Server 2019 or later
- Administrator access
- Static IP and DNS entry (e.g., `atlantis.med.harvard.edu`)
- Python 3.11+ installed
- Git installed

---

## Step 1: Initial Setup

### 1.1 Create Dedicated Service User

Open **PowerShell as Administrator** and run:

```powershell
# Create user
net user lab-monitor "YourComplexPassword123!" /add /active:yes /expires:never

# Grant admin privileges (for service account creation)
net localgroup administrators lab-monitor /add

# Verify
net user lab-monitor
```

### 1.2 Create Directory Structure

```powershell
mkdir C:\lab-monitor\data
mkdir C:\lab-monitor\logs
mkdir C:\lab-monitor\scripts
```

Give the user permissions:

```powershell
icacls C:\lab-monitor /grant "lab-monitor:(OI)(CI)F" /T
```

### 1.3 Clone Repository

```powershell
cd C:\lab-monitor\scripts
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor
```

### 1.4 Install Python Dependencies

```powershell
# Manager
cd C:\lab-monitor\scripts\lab-monitor\manager
pip install -r requirements.txt

# Dashboard
cd C:\lab-monitor\scripts\lab-monitor\dashboard
pip install -r requirements.txt
```

---

## Step 2: Configure Services

### 2.1 Create Configuration Files

**Manager config** (`C:\lab-monitor\manager\config.json`):

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "C:\\lab-monitor\\data",
  "auth_tokens": ["your-secret-token-here"],
  "log_file": "C:\\lab-monitor\\logs\\manager.log",
  "log_level": "INFO",
  "debug": false
}
```

**Dashboard config** (`C:\lab-monitor\dashboard\config.json`):

```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://localhost:5000",
  "refresh_interval_seconds": 30,
  "log_file": "C:\\lab-monitor\\logs\\dashboard.log",
  "log_level": "INFO",
  "debug": false,
  "manager_timeout_seconds": 5
}
```

### 2.2 Install NSSM (Service Manager)

Download NSSM from https://nssm.cc/

```powershell
# Extract to C:\tools\nssm\
# (Create C:\tools\ first if needed)
```

---

## Step 3: Install as Windows Services

Open **PowerShell as Administrator**:

### 3.1 Install Manager Service

```powershell
$pythonPath = "C:\Python311\python.exe"  # Adjust version if needed
$nssm = "C:\tools\nssm\nssm.exe"

# Install service
& $nssm install LabMonitorManager $pythonPath `
  C:\lab-monitor\scripts\lab-monitor\manager\manager.py `
  --config C:\lab-monitor\manager\config.json

# Run as dedicated user
& $nssm set LabMonitorManager ObjectName "lab-monitor" "YourComplexPassword123!"

# Set to auto-start
& $nssm set LabMonitorManager Start SERVICE_AUTO_START

# Auto-restart on crash
& $nssm set LabMonitorManager AppExit Default Restart
& $nssm set LabMonitorManager AppRestartDelay 5000

# Verify
& $nssm get LabMonitorManager
```

### 3.2 Install Dashboard Service

```powershell
# Install service
& $nssm install LabMonitorDashboard $pythonPath `
  C:\lab-monitor\scripts\lab-monitor\dashboard\app.py `
  --config C:\lab-monitor\dashboard\config.json

# Run as dedicated user
& $nssm set LabMonitorDashboard ObjectName "lab-monitor" "YourComplexPassword123!"

# Set to auto-start
& $nssm set LabMonitorDashboard Start SERVICE_AUTO_START

# Auto-restart on crash
& $nssm set LabMonitorDashboard AppExit Default Restart
& $nssm set LabMonitorDashboard AppRestartDelay 5000
```

---

## Step 4: Configure Firewall

```powershell
# Allow Dashboard (external access)
netsh advfirewall firewall add rule `
  name="Lab Monitor Dashboard" `
  dir=in action=allow protocol=tcp localport=5001 `
  remoteip=192.168.0.0/16

# Allow Manager (localhost only)
netsh advfirewall firewall add rule `
  name="Lab Monitor Manager" `
  dir=in action=allow protocol=tcp localport=5000 `
  remoteip=127.0.0.1

# Verify rules
netsh advfirewall firewall show rule name="Lab Monitor*"
```

---

## Step 5: Start Services

```powershell
# Start Manager
net start LabMonitorManager

# Start Dashboard
net start LabMonitorDashboard

# Check status
Get-Service LabMonitor*
```

---

## Step 6: Verify Installation

### 6.1 Check Logs

```powershell
# Manager log
Get-Content C:\lab-monitor\logs\manager.log -Tail 20

# Dashboard log
Get-Content C:\lab-monitor\logs\dashboard.log -Tail 20
```

### 6.2 Test API

```powershell
# Health check
curl.exe http://localhost:5000/health

# Dashboard
# Open browser: http://localhost:5001
```

### 6.3 Test from Another Machine

From any machine on the intranet:

```
http://atlantis.med.harvard.edu:5001
```

---

## NAS Deployment

### Per-NAS Setup (Synology or Windows)

1. Copy `collector/` folder to each NAS
2. Create `config.json`:

```json
{
  "nas_name": "nas-01",
  "nas_id": "synology_01",
  "manager_url": "http://atlantis.med.harvard.edu:5000",
  "manager_token": "your-secret-token-here",
  "folders_to_monitor": [
    "/volume1/shared",
    "/volume1/media"
  ],
  "queue_path": "/var/lib/lab-monitor/queue.jsonl",
  "log_file": "/var/log/lab-monitor-collector.log",
  "log_level": "INFO",
  "manager_timeout_seconds": 5,
  "timeout_seconds": 300,
  "retry_attempts": 3,
  "retry_delay_seconds": 10
}
```

3. Schedule via Task Scheduler (daily at 2 AM)

**For Windows NAS:**

```json
{
  "nas_name": "windows-nas",
  "nas_id": "win_nas_01",
  "manager_url": "http://atlantis.med.harvard.edu:5000",
  "manager_token": "your-secret-token-here",
  "folders_to_monitor": [
    "D:\\Projects",
    "E:\\Data"
  ],
  "queue_path": "C:\\lab-monitor\\queue.jsonl",
  "log_file": "C:\\lab-monitor\\logs\\collector.log"
}
```

---

## Troubleshooting

### Services won't start

Check logs:
```powershell
Get-Content C:\lab-monitor\logs\*.log -Tail 50
```

Verify Python path:
```powershell
C:\Python311\python.exe --version
```

### Can't reach Dashboard from another machine

1. Check firewall rules: `netsh advfirewall firewall show rule name="Lab Monitor*"`
2. Check service status: `Get-Service LabMonitorDashboard`
3. Test connectivity: `Test-NetConnection -ComputerName atlantis.med.harvard.edu -Port 5001`

### No data appearing in Dashboard

1. Check Manager logs: `Get-Content C:\lab-monitor\logs\manager.log -Tail 50`
2. Verify Manager is running: `Get-Service LabMonitorManager`
3. Test API: `curl.exe http://localhost:5000/api/usage/all`

### Collector not posting data

1. Check collector logs on NAS
2. Verify config has correct `manager_url` and `manager_token`
3. Test connectivity from NAS: `ping atlantis.med.harvard.edu`

---

## Management

### View Service Status

```powershell
Get-Service LabMonitor* | Format-Table Name, Status, StartType
```

### Restart Services

```powershell
# Restart Manager
Restart-Service LabMonitorManager

# Restart Dashboard
Restart-Service LabMonitorDashboard

# Restart both
Restart-Service LabMonitor*
```

### View Real-Time Logs

```powershell
# Manager (follow last 20 lines)
Get-Content -Path C:\lab-monitor\logs\manager.log -Tail 20 -Wait

# Dashboard
Get-Content -Path C:\lab-monitor\logs\dashboard.log -Tail 20 -Wait
```

### Stop Services (if needed)

```powershell
net stop LabMonitorManager
net stop LabMonitorDashboard
```

---

## Security Notes

1. Change `manager_token` in both configs to a strong random string
2. Distribute token only to NAS collectors
3. NSSM stores service password securely (encrypted)
4. Firewall is configured to restrict Manager to localhost only
5. Dashboard is accessible from intranet (adjust firewall rules if restricting further)

---

## Next Steps

1. Deploy collectors to each NAS
2. Update each collector config with correct `manager_token`
3. Schedule collectors to run daily (e.g., 2 AM)
4. Monitor logs and dashboard for data flow
