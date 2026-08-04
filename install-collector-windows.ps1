#
# Lab Monitor - Windows Server Collector Installation Script
#
# Usage (as Administrator):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-collector-windows.ps1
#
# This script:
# 1. Creates directory structure
# 2. Clones or updates lab-monitor repository
# 3. Installs dependencies (if not using conda)
# 4. Creates config.json from template (interactive)
# 5. Sets up Task Scheduler jobs
# 6. Tests both collection modes
#

# Check if running from Anaconda Prompt (CONDA_DEFAULT_ENV set)
if (-not $env:CONDA_DEFAULT_ENV) {
    Write-Host "ERROR: This script must be run from an Anaconda Prompt" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "How to launch:" -ForegroundColor Yellow
    Write-Host "  1. Open Anaconda Prompt (or Anaconda PowerShell Prompt)" -ForegroundColor Yellow
    Write-Host "  2. (Optional) Create a conda environment:" -ForegroundColor Yellow
    Write-Host "       conda create -n lab-monitor python=3.11" -ForegroundColor Yellow
    Write-Host "  3. (Optional) Activate it:" -ForegroundColor Yellow
    Write-Host "       conda activate lab-monitor" -ForegroundColor Yellow
    Write-Host "  4. Run this script:" -ForegroundColor Yellow
    Write-Host "       .\install-collector-windows.ps1" -ForegroundColor Yellow
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
Write-Host "Lab Monitor - Windows Collector Install" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$RootDir = "E:\Users\lab-monitor"
$DataDir = "$RootDir\data"
$LogsDir = "$RootDir\logs"
$ScriptsDir = "$RootDir\scripts"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"
$CollectorDir = "$ScriptsDir\lab-monitor\collector"
$ConfigFile = "$CollectorDir\local\config.json"
$CondaEnv = $env:CONDA_DEFAULT_ENV
$PythonExe = (Get-Command python.exe).Source  # Use conda env's python

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
Write-Success "Directories created at $RootDir"
Write-Host ""

# Step 2: Verify Python (conda environment)
Write-Step "Step 2: Verifying Python from conda environment"
if (-not $PythonExe) {
    Write-Error-Custom "Python not found in conda environment PATH"
    Write-Host ""
    Write-Host "Make sure you:"
    Write-Host "  1. Opened an Anaconda Prompt (not regular cmd/PowerShell)"
    Write-Host "  2. (Optional) Activated a conda environment: conda activate lab-monitor"
    Write-Host ""
    exit 1
}

$PythonVersion = & $PythonExe --version 2>&1
Write-Success "Using Python from conda: $CondaEnv"
Write-Host "  $PythonVersion"
Write-Host ""

# Step 3: Clone/update repository
Write-Step "Step 3: Cloning/updating lab-monitor repository"
if (Test-Path "$ScriptsDir\lab-monitor\.git") {
    Write-Host "Repository already exists, pulling latest..."
    Push-Location "$ScriptsDir\lab-monitor"
    & git pull origin main 2>&1 | Out-Null
    Pop-Location
} else {
    Write-Host "Cloning from GitHub..."
    Push-Location $ScriptsDir
    & git clone $RepoUrl 2>&1 | Out-Null
    Pop-Location
}
if ($LASTEXITCODE -eq 0 -or (Test-Path "$ScriptsDir\lab-monitor\collector\collector.py")) {
    Write-Success "Repository ready at $ScriptsDir\lab-monitor"
} else {
    Write-Error-Custom "Could not clone repository"
    exit 1
}
Write-Host ""

# Step 4: Conda environment info
Write-Step "Step 4: Using conda environment"
Write-Success "Conda environment: $CondaEnv"
Write-Host "  Python: $PythonExe"
Write-Host ""

# Step 5: Install dependencies
Write-Step "Step 5: Installing dependencies"
Write-Host "Installing from requirements.txt..."
& $PythonExe -m pip install --upgrade pip 2>&1 | Out-Null
Push-Location $CollectorDir
& $PythonExe -m pip install -r requirements.txt 2>&1 | Out-Null
Pop-Location
if ($LASTEXITCODE -eq 0) {
    Write-Success "Dependencies installed"
} else {
    Write-Error-Custom "Could not install dependencies"
    exit 1
}
Write-Host ""

# Step 6: Gather configuration (interactive)
Write-Step "Step 6: Gathering configuration"
$LocalDir = "$CollectorDir\local"
if (-not (Test-Path $LocalDir)) { New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null }

if (-not (Test-Path $ConfigFile)) {
    Write-Host "Auto-detected values:"
    $ServerName = $env:COMPUTERNAME.ToLower()
    Write-Host "  - Server name: $ServerName"
    Write-Host ""
    
    # Prompt for Manager URL
    $ManagerUrl = Read-Host "Enter Manager URL (e.g., http://atlantis.med.harvard.edu:5000)"
    if ([string]::IsNullOrWhiteSpace($ManagerUrl)) {
        Write-Error-Custom "Manager URL cannot be empty"
        exit 1
    }
    if ($ManagerUrl -like "https://*") {
        Write-Host "⚠️  Warning: URL starts with https:// but Manager runs plain HTTP" -ForegroundColor Yellow
        $Confirm = Read-Host "Continue with https:// anyway? [y/N]"
        if ($Confirm -ne "y" -and $Confirm -ne "Y") {
            Write-Host "Please re-run and enter the correct URL."
            exit 1
        }
    }

    # Prompt for Manager Token
    $ManagerToken = Read-Host "Enter Manager Token (from Manager config)"
    if ([string]::IsNullOrWhiteSpace($ManagerToken)) {
        Write-Error-Custom "Manager Token cannot be empty"
        exit 1
    }

    # Prompt for Device Type
    Write-Host ""
    Write-Host "Device types:"
    Write-Host "  NAS           - Standard NAS (scan folders)"
    Write-Host "  NAS-Instrument - Research instrument storage (scan deeper)"
    Write-Host "  NAS-Backup    - Backup volume (volume-only, fast)"
    Write-Host "  Server        - Windows Server (scan folders)"
    $DeviceType = Read-Host "Enter Device Type [NAS/NAS-Instrument/NAS-Backup/Server]"
    if ([string]::IsNullOrWhiteSpace($DeviceType)) {
        $DeviceType = "Server"
    }
    if ($DeviceType -notin @("NAS", "NAS-Instrument", "NAS-Backup", "Server")) {
        Write-Host "⚠️  Warning: '$DeviceType' is not a recognized device type. Continuing anyway." -ForegroundColor Yellow
    }

    # Suggest default scan_depth
    $DefaultDepth = switch ($DeviceType) {
        "NAS-Backup" { 1 }
        "NAS-Instrument" { 3 }
        default { 2 }
    }

    Write-Host ""
    Write-Host "Scan depth controls how many folder levels the disk collector measures:"
    Write-Host "  1 = Volume only        (fast, filesystem stats  -- recommended for NAS-Backup)"
    Write-Host "  2 = Volume/Folder      (standard                -- recommended for NAS and Server)"
    Write-Host "  3 = Volume/Folder/Sub  (one level deeper        -- recommended for NAS-Instrument)"
    $ScanDepthInput = Read-Host "Enter Scan Depth [1/2/3] (default: $DefaultDepth)"
    $ScanDepth = if ([string]::IsNullOrWhiteSpace($ScanDepthInput)) { $DefaultDepth } else { [int]$ScanDepthInput }
    if ($ScanDepth -lt 1 -or $ScanDepth -gt 10) {
        Write-Error-Custom "Scan Depth must be a positive integer"
        exit 1
    }
    Write-Success "Scan depth: $ScanDepth"

    # Prompt for volumes
    Write-Host ""
    Write-Host "Detected drives: C:, D:, E:, F:, G:, H:, I:, J:, K:, L:, M:, N:, O:, P:, Q:, R:, S:, T:, U:, V:, W:, X:, Y:, Z:"
    $VolumesInput = Read-Host "Enter drives to monitor (comma-separated, e.g., E:,F:,G:)"
    $Volumes = @()
    if (-not [string]::IsNullOrWhiteSpace($VolumesInput)) {
        $Volumes = @($VolumesInput -split ',' | ForEach-Object { $_.Trim() })
    } else {
        # Default to E: and F:
        $Volumes = @("E:", "F:")
    }
    Write-Host "Volumes to monitor: $($Volumes -join ', ')"
    Write-Host ""

    # Create config file
    $ConfigContent = @{
        name = $ServerName
        id = "windows-$ServerName"
        device_type = $DeviceType
        manager_url = $ManagerUrl
        manager_token = $ManagerToken
        volumes = $Volumes
        scan_depth = $ScanDepth
        data_dir = "$RootDir\data"
        log_file = "$RootDir\logs\collector.log"
        log_level = "INFO"
        timeout_seconds = 3600
        request_timeout_seconds = 30
    } | ConvertTo-Json -Depth 3

    Set-Content -Path $ConfigFile -Value $ConfigContent
    Write-Success "Config file created at $ConfigFile"
    Write-Host ""
    Write-Host "Configuration saved with:"
    Write-Host "  - Server Name:   $ServerName"
    Write-Host "  - Device Type:   $DeviceType"
    Write-Host "  - Scan Depth:    $ScanDepth"
    Write-Host "  - Volumes:       $($Volumes -join ', ')"
    Write-Host "  - Manager URL:   $ManagerUrl"
    Write-Host "  - Manager Token: (set)"
    Write-Host ""
} else {
    Write-Success "Config file already exists at $ConfigFile"
    Write-Host "   (Edit manually if needed: notepad $ConfigFile)"
}
Write-Host ""

# Step 7: Test collector
Write-Step "Step 7: Testing collector (metrics mode)"
Write-Host "Testing metrics collection..."
Push-Location $CollectorDir
& $PythonExe collector.py --config local\config.json --mode metrics 2>&1 | Out-Null
$TestResult = $LASTEXITCODE
Pop-Location
if ($TestResult -eq 0) {
    Write-Success "Metrics collection test passed"
} else {
    Write-Host "⚠️  Metrics collection test failed (may be normal if Manager not running yet)" -ForegroundColor Yellow
}
Write-Host ""

# Step 8: Register Task Scheduler jobs
Write-Step "Step 8: Registering Task Scheduler jobs"

# Disk collection job (daily 2 AM)
$DiskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "$CollectorDir\collector.py --config local\config.json --mode disk" `
    -WorkingDirectory $CollectorDir

$DiskTrigger = New-ScheduledTaskTrigger -Daily -At "2:00 AM"

$DiskTask = @{
    TaskName = "Lab Monitor - Disk Collection"
    Action = $DiskAction
    Trigger = $DiskTrigger
    RunLevel = "Highest"
    User = "SYSTEM"
    Force = $true
}

try {
    Register-ScheduledTask @DiskTask | Out-Null
    Write-Success "Disk collection job registered (daily at 2:00 AM)"
} catch {
    Write-Host "⚠️  Could not register disk job: $_" -ForegroundColor Yellow
}

# Metrics collection job (every 5 minutes)
$MetricsAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "$CollectorDir\collector.py --config local\config.json --mode metrics" `
    -WorkingDirectory $CollectorDir

$MetricsTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$MetricsTask = @{
    TaskName = "Lab Monitor - Metrics Collection"
    Action = $MetricsAction
    Trigger = $MetricsTrigger
    RunLevel = "Highest"
    User = "SYSTEM"
    Force = $true
}

try {
    Register-ScheduledTask @MetricsTask | Out-Null
    Write-Success "Metrics collection job registered (every 5 minutes)"
} catch {
    Write-Host "⚠️  Could not register metrics job: $_" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[OK] Windows Collector Installation Complete" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Verify configuration:"
Write-Host "   notepad $ConfigFile"
Write-Host ""
Write-Host "2. Verify jobs registered:"
Write-Host "   Get-ScheduledTask | Select-String 'Lab Monitor'"
Write-Host ""
Write-Host "3. Monitor collections:"
Write-Host "   Get-Content -Path '$LogsDir\collector.log' -Tail 20 -Wait"
Write-Host ""
