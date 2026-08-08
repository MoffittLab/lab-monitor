#
# Lab Monitor - Manager + Dashboard Installation Script (Task Scheduler Version)
# For Windows Server (atlantis.med.harvard.edu)
#
# Usage (as Administrator):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-manager-dashboard-taskscheduler.ps1 -ManagerToken "your-secret-token" -DashboardToken "same-token"
#
# This script:
# 1. Creates directory structure
# 2. Clones/updates lab-monitor repository
# 3. Creates/activates Conda environment
# 4. Installs dependencies (Flask, requests, psutil)
# 5. Creates Manager and Dashboard config files
# 6. Creates launcher batch file
# 7. Registers auto-startup task in Windows Task Scheduler
# 8. Configures Windows Firewall
# 9. Tests end-to-end
#

param(
    [string]$ManagerToken = "CHANGE-ME-TO-SECURE-TOKEN",
    [string]$DashboardToken = $ManagerToken,
    [string]$ManagerPort = "5000",
    [string]$DashboardPort = "5001"
)

# Check if running from Anaconda Prompt
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: This script must be run from an Anaconda Prompt" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "How to launch:" -ForegroundColor Yellow
    Write-Host "  1. Open Anaconda Prompt (or Anaconda PowerShell Prompt)" -ForegroundColor Yellow
    Write-Host "  2. Run this script:" -ForegroundColor Yellow
    Write-Host "       .\install-manager-dashboard-taskscheduler.ps1 -ManagerToken \"your-secure-token\"" -ForegroundColor Yellow
    exit 1
}

# Check if running as Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "Right-click on Anaconda Prompt and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Lab Monitor - Manager + Dashboard (Task Scheduler)" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$RootDir = "E:\Users\lab-monitor"
$DataDir = "$RootDir\data"
$LogsDir = "$RootDir\logs"
$ScriptsDir = "$RootDir\scripts"
$CondaEnv = "lab-monitor"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"
$LauncherScript = "$RootDir\start-services.bat"

function Write-Step {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Error-Custom {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
}

# Step 1: Create directory structure
Write-Step "Step 1: Creating directory structure"
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }
if (-not (Test-Path $ScriptsDir)) { New-Item -ItemType Directory -Path $ScriptsDir -Force | Out-Null }
Write-Success "Directories created"
Write-Host ""

# Step 2: Verify Conda is on PATH
Write-Step "Step 2: Checking Conda availability"
$CondaCheck = conda --version 2>&1
Write-Success "Conda available: $CondaCheck"
Write-Host ""

# Step 3: Create or verify Conda environment
Write-Step "Step 3: Setting up Conda environment"
$EnvList = conda info --envs 2>&1
$EnvExists = $EnvList -match $CondaEnv

if (-not $EnvExists) {
    Write-Host "Creating new conda environment '$CondaEnv'..."
    conda create -y -n $CondaEnv python=3.11 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Conda environment '$CondaEnv' created"
    } else {
        Write-Error-Custom "Could not create conda environment"
        exit 1
    }
} else {
    Write-Success "Conda environment '$CondaEnv' already exists"
}
Write-Host ""

# Step 4: Clone repository
Write-Step "Step 4: Cloning lab-monitor repository"
if (Test-Path "$ScriptsDir\lab-monitor\.git") {
    Write-Host "Repository already exists, pulling latest..."
    Push-Location "$ScriptsDir\lab-monitor"
    & git pull origin main 2>&1 | Out-Null
    Pop-Location
} else {
    Push-Location $ScriptsDir
    & git clone $RepoUrl 2>&1 | Out-Null
    Pop-Location
}
if (Test-Path "$ScriptsDir\lab-monitor\manager\manager.py") {
    Write-Success "Repository ready"
} else {
    Write-Error-Custom "Could not clone repository"
    exit 1
}
Write-Host ""

# Step 5: Install dependencies into the conda environment
Write-Step "Step 5: Installing dependencies"
Write-Host "  Upgrading pip..."
conda run -n $CondaEnv pip install --upgrade pip 2>&1 | Out-Null

Write-Host "  Installing Flask, requests, psutil..."
conda run -n $CondaEnv pip install Flask==2.3.3 Flask-CORS==4.0.0 requests==2.31.0 psutil==5.9.6 2>&1 | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Success "Dependencies installed"
} else {
    Write-Host "[WARNING] Some dependencies may have failed to install" -ForegroundColor Yellow
}
Write-Host ""

# Step 6: Create Manager configuration
Write-Step "Step 6: Creating Manager configuration"
$ManagerConfigFile = "$ScriptsDir\lab-monitor\manager\config.json"
$ManagerConfig = @{
    host = "0.0.0.0"
    port = [int]$ManagerPort
    data_dir = $DataDir
    auth_tokens = @($ManagerToken)
    cors_origins = @("http://localhost:5001", "http://atlantis.med.harvard.edu:5001")
    log_file = "$LogsDir\manager.log"
    log_level = "INFO"
    retention_days = 90
    debug = $false
} | ConvertTo-Json

Set-Content -Path $ManagerConfigFile -Value $ManagerConfig
Write-Success "Manager config created at $ManagerConfigFile"
Write-Host ""

# Step 7: Create Dashboard configuration
Write-Step "Step 7: Creating Dashboard configuration"
$DashboardConfigFile = "$ScriptsDir\lab-monitor\dashboard\config.json"
$DashboardConfig = @{
    host = "0.0.0.0"
    port = [int]$DashboardPort
    manager_url = "http://localhost:$ManagerPort"
    manager_token = $DashboardToken
    refresh_interval_seconds = 30
    manager_timeout_seconds = 5
    log_file = "$LogsDir\dashboard.log"
    log_level = "INFO"
    debug = $false
} | ConvertTo-Json

Set-Content -Path $DashboardConfigFile -Value $DashboardConfig
Write-Success "Dashboard config created at $DashboardConfigFile"
Write-Host ""

# Step 8: Test Manager locally
Write-Step "Step 8: Testing Manager service"
Write-Host "Starting Manager for testing (will terminate in 5 seconds)..."
$ManagerDir = "$ScriptsDir\lab-monitor\manager"
Push-Location $ManagerDir
$ManagerProc = Start-Process -FilePath "conda" -ArgumentList "run -n $CondaEnv python manager.py --config config.json" `
    -NoNewWindow -PassThru
Pop-Location

Start-Sleep -Seconds 5
if ($ManagerProc.HasExited -eq $false) {
    Write-Success "Manager started successfully"
    Stop-Process -Id $ManagerProc.Id -Force
} else {
    Write-Host "[WARNING] Manager may have failed to start (check config)" -ForegroundColor Yellow
}
Write-Host ""

# Step 9: Test Dashboard locally
Write-Step "Step 9: Testing Dashboard service"
Write-Host "Starting Dashboard for testing (will terminate in 5 seconds)..."
$DashboardDir = "$ScriptsDir\lab-monitor\dashboard"
Push-Location $DashboardDir
$DashboardProc = Start-Process -FilePath "conda" -ArgumentList "run -n $CondaEnv python app.py --config config.json" `
    -NoNewWindow -PassThru
Pop-Location

Start-Sleep -Seconds 5
if ($DashboardProc.HasExited -eq $false) {
    Write-Success "Dashboard started successfully"
    Stop-Process -Id $DashboardProc.Id -Force
} else {
    Write-Host "[WARNING] Dashboard may have failed to start (check config)" -ForegroundColor Yellow
}
Write-Host ""

# Step 10: Create launcher batch file
Write-Step "Step 10: Creating launcher batch file"
$LauncherContent = @"
@echo off
REM Lab Monitor Service Launcher
REM This batch file is called by Windows Task Scheduler at startup
REM Services run in the background (/b flag) without visible windows

cd /d E:\Users\lab-monitor\scripts\lab-monitor\manager
call conda activate lab-monitor
start /b "Lab Monitor Manager" python.exe manager.py --config config.json

timeout /t 2 /nobreak

cd /d E:\Users\lab-monitor\scripts\lab-monitor\dashboard
start /b "Lab Monitor Dashboard" python.exe app.py --config config.json
"@

Set-Content -Path $LauncherScript -Value $LauncherContent
Write-Success "Launcher script created at $LauncherScript"
Write-Host ""

# Step 11: Register Task Scheduler task
Write-Step "Step 11: Registering Windows Task Scheduler task"

# Check if task already exists
$TaskExists = Get-ScheduledTask -TaskName "Lab Monitor Startup" -ErrorAction SilentlyContinue

if ($TaskExists) {
    Write-Host "Task already exists, unregistering old version..."
    Unregister-ScheduledTask -TaskName "Lab Monitor Startup" -Confirm:$false -ErrorAction SilentlyContinue
}

# Get current user for task
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Create task action
$Action = New-ScheduledTaskAction -Execute $LauncherScript

# Create task trigger (at startup)
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Create task settings
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries

# Register the task
$Task = Register-ScheduledTask `
    -TaskName "Lab Monitor Startup" `
    -Description "Auto-start Lab Monitor Manager and Dashboard on system startup" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $CurrentUser `
    -RunLevel Highest `
    -Force

if ($Task) {
    Write-Success "Task Scheduler task registered"
    Write-Host "  Task name: Lab Monitor Startup"
    Write-Host "  Trigger: At system startup"
    Write-Host "  Run as: $CurrentUser (with highest privileges)"
} else {
    Write-Error-Custom "Failed to register Task Scheduler task"
}
Write-Host ""

# Step 12: Configure Windows Firewall
Write-Step "Step 12: Configuring Windows Firewall"
Write-Host "  Opening ports $ManagerPort (Manager) and $DashboardPort (Dashboard)..."

# Manager
netsh advfirewall firewall show rule name="Lab Monitor Manager" | Out-Null
if ($LASTEXITCODE -ne 0) {
    netsh advfirewall firewall add rule name="Lab Monitor Manager" dir=in action=allow protocol=tcp localport=$ManagerPort 2>&1 | Out-Null
    Write-Host "    Manager port $ManagerPort opened"
}

# Dashboard
netsh advfirewall firewall show rule name="Lab Monitor Dashboard" | Out-Null
if ($LASTEXITCODE -ne 0) {
    netsh advfirewall firewall add rule name="Lab Monitor Dashboard" dir=in action=allow protocol=tcp localport=$DashboardPort 2>&1 | Out-Null
    Write-Host "    Dashboard port $DashboardPort opened"
}
Write-Success "Firewall configured"
Write-Host ""

# Step 13: Start services manually (for this session)
Write-Step "Step 13: Starting services for this session"
Write-Host "  (These will also auto-start on next reboot)"

$ManagerDir = "$ScriptsDir\lab-monitor\manager"
$DashboardDir = "$ScriptsDir\lab-monitor\dashboard"

Write-Host "  Starting Manager..."
Push-Location $ManagerDir
cmd /c "conda activate $CondaEnv && start ""Lab Monitor Manager"" python.exe manager.py --config config.json"
Pop-Location

Start-Sleep -Seconds 2

Write-Host "  Starting Dashboard..."
Push-Location $DashboardDir
cmd /c "conda activate $CondaEnv && start ""Lab Monitor Dashboard"" python.exe app.py --config config.json"
Pop-Location

Start-Sleep -Seconds 2
Write-Success "Services started"
Write-Host ""

# Step 14: Verify services are running
Write-Step "Step 14: Verifying services"
Start-Sleep -Seconds 2

$ManagerProc = Get-Process python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match "manager\.py"}
$DashboardProc = Get-Process python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match "app\.py"}

if ($ManagerProc) {
    Write-Success "Manager is running (PID: $($ManagerProc.Id))"
} else {
    Write-Host "[WARNING] Manager does not appear to be running" -ForegroundColor Yellow
}

if ($DashboardProc) {
    Write-Success "Dashboard is running (PID: $($DashboardProc.Id))"
} else {
    Write-Host "[WARNING] Dashboard does not appear to be running" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[OK] Manager + Dashboard Installation Complete" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
# ==============================================================================
# OpenSSH Server (enables remote update via update_collectors.py)
# ==============================================================================
Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  OpenSSH Server (optional — enables remote updates)" -ForegroundColor Yellow
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "The update_collectors.py tool uses SSH to push git + pip updates" -ForegroundColor Cyan
Write-Host "to this server remotely. OpenSSH Server must be installed for this." -ForegroundColor Cyan
Write-Host ""

$SshdInstalled = (Get-WindowsCapability -Online -Name OpenSSH.Server* -ErrorAction SilentlyContinue |`
                  Where-Object State -eq Installed)
$SshdRunning   = (Get-Service sshd -ErrorAction SilentlyContinue | Where-Object Status -eq Running)

if ($SshdInstalled -and $SshdRunning) {
    Write-Host "[OK] OpenSSH Server is already installed and running" -ForegroundColor Green
} else {
    if ($SshdInstalled) {
        Write-Host "OpenSSH Server is installed but not running." -ForegroundColor Yellow
    } else {
        Write-Host "OpenSSH Server is not installed on this system." -ForegroundColor Yellow
    }
    Write-Host ""
    $InstallSsh = Read-Host "Install/enable OpenSSH Server now? [y/N]"
    if ($InstallSsh -eq 'y' -or $InstallSsh -eq 'Y') {
        if (-not $SshdInstalled) {
            Write-Host "Installing OpenSSH Server..." -ForegroundColor Cyan
            try {
                Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null
                Write-Host "[OK] OpenSSH Server installed" -ForegroundColor Green
            } catch {
                Write-Host "WARNING: Could not install OpenSSH Server: $_" -ForegroundColor Yellow
                Write-Host "  Install manually: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0" -ForegroundColor Gray
            }
        }
        Write-Host "Starting sshd and setting to auto-start..." -ForegroundColor Cyan
        try {
            Start-Service sshd -ErrorAction Stop
            Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
            Write-Host "[OK] sshd started and set to Automatic" -ForegroundColor Green
        } catch {
            Write-Host "WARNING: Could not start sshd: $_" -ForegroundColor Yellow
        }
        $FwRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        if (-not $FwRule) {
            Write-Host "Adding firewall rule for port 22..." -ForegroundColor Cyan
            try {
                New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
                    -DisplayName 'OpenSSH Server (sshd)' `
                    -Enabled True -Direction Inbound `
                    -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
                Write-Host "[OK] Firewall rule added (TCP port 22 inbound)" -ForegroundColor Green
            } catch {
                Write-Host "WARNING: Could not add firewall rule: $_" -ForegroundColor Yellow
            }
        } else {
            Write-Host "[OK] Firewall rule already present" -ForegroundColor Green
        }
    } else {
        Write-Host "  Skipped. To enable later, run in an admin PowerShell:" -ForegroundColor Gray
        Write-Host "    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0" -ForegroundColor Gray
        Write-Host "    Start-Service sshd" -ForegroundColor Gray
        Write-Host "    Set-Service -Name sshd -StartupType Automatic" -ForegroundColor Gray
    }
}
Write-Host ""

Write-Host "Setup Summary:" -ForegroundColor Cyan
Write-Host "1. Launcher script: $LauncherScript"
Write-Host "2. Task Scheduler: 'Lab Monitor Startup' (set to run at system startup)"
Write-Host "3. Auto-starts on reboot: YES (no login required)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Verify services are running:"
Write-Host "   Get-Process python | Where-Object CommandLine -match 'manager|app'"
Write-Host ""
Write-Host "2. Access Dashboard:"
Write-Host "   http://atlantis.med.harvard.edu:$DashboardPort"
Write-Host ""
Write-Host "3. Check logs:"
Write-Host "   Get-Content '$LogsDir\manager.log' -Tail 20"
Write-Host "   Get-Content '$LogsDir\dashboard.log' -Tail 20"
Write-Host ""
Write-Host "4. Manage Task Scheduler task:"
Write-Host "   taskschd.msc"
Write-Host ""
Write-Host "5. Deploy collectors on Synology and Windows servers:"
Write-Host "   See collector/README.md"
Write-Host ""
Write-Host "[IMPORTANT] Make sure collector config has correct manager_token:" -ForegroundColor Yellow
Write-Host "   Token used: $ManagerToken"
Write-Host ""
