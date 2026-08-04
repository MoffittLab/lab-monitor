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
# 3. Creates Python virtual environment
# 4. Installs dependencies
# 5. Creates config.json from template
# 6. Sets up Task Scheduler jobs
# 7. Tests both collection modes
#

param(
    [string]$ServerName = "atlantis",
    [string]$ManagerToken = "CHANGE-ME-TOKEN",
    [string]$ManagerUrl = "http://atlantis.med.harvard.edu:5000"
)

# Check if running as Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator" -ForegroundColor Red
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
$VenvDir = "$RootDir\lab-monitor-env"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"
$CollectorDir = "$ScriptsDir\lab-monitor\collector"
$ConfigFile = "$CollectorDir\local\config.json"

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

# Step 2: Check Python
Write-Step "Step 2: Checking Python installation"
$PythonPath = "C:\Users\Administrator\miniconda3\envs\lab-monitor\python.exe"
if (-not (Test-Path $PythonPath)) {
    Write-Error-Custom "Python environment not found at $PythonPath"
    Write-Host "Install Miniconda and create conda environment first (see WINDOWS-INSTALL.md)"
    exit 1
}
$PythonVersion = & $PythonPath --version
Write-Success "$PythonVersion found"
Write-Host ""

# Step 3: Clone/update repository
Write-Step "Step 3: Cloning/updating lab-monitor repository"
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
if ($LASTEXITCODE -eq 0 -or (Test-Path "$ScriptsDir\lab-monitor\collector\collector.py")) {
    Write-Success "Repository ready at $ScriptsDir\lab-monitor"
} else {
    Write-Error-Custom "Could not clone repository"
    exit 1
}
Write-Host ""

# Step 4: Create virtual environment
Write-Step "Step 4: Creating Python virtual environment"
if (-not (Test-Path $VenvDir)) {
    & $PythonPath -m venv $VenvDir 2>&1 | Out-Null
    if (Test-Path $VenvDir) {
        Write-Success "Virtual environment created"
    } else {
        Write-Error-Custom "Could not create virtual environment"
        exit 1
    }
} else {
    Write-Success "Virtual environment already exists"
}
Write-Host ""

# Step 5: Install dependencies
Write-Step "Step 5: Installing dependencies"
$PipPath = "$VenvDir\Scripts\pip.exe"
& $PipPath install --upgrade pip 2>&1 | Out-Null
Push-Location $CollectorDir
& $PipPath install -r requirements.txt 2>&1 | Out-Null
Pop-Location
if ($LASTEXITCODE -eq 0) {
    Write-Success "Dependencies installed (requests, psutil)"
} else {
    Write-Error-Custom "Could not install dependencies"
    exit 1
}
Write-Host ""

# Step 6: Create configuration
Write-Step "Step 6: Creating configuration"
$LocalDir = "$CollectorDir\local"
if (-not (Test-Path $LocalDir)) { New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null }

if (-not (Test-Path $ConfigFile)) {
    $ConfigContent = @{
        name = $ServerName
        id = "windows-$ServerName"
        manager_url = $ManagerUrl
        manager_token = $ManagerToken
        volumes = @("E:", "F:")
        data_dir = "$RootDir\data"
        log_file = "$RootDir\logs\collector.log"
        log_level = "INFO"
        timeout_seconds = 3600
    } | ConvertTo-Json

    Set-Content -Path $ConfigFile -Value $ConfigContent
    Write-Success "Config file created at $ConfigFile"
    Write-Host ""
    Write-Host "⚠️  IMPORTANT: Edit the configuration:" -ForegroundColor Yellow
    Write-Host "   notepad $ConfigFile"
    Write-Host ""
    Write-Host "   Change:"
    Write-Host "   - `"name`": Server name (e.g., 'atlantis', 'compute-01')"
    Write-Host "   - `"manager_token`": Copy from Manager config"
    Write-Host "   - `"volumes`": Drives to monitor (e.g., ['E:', 'F:'])"
    Write-Host ""
} else {
    Write-Success "Config file already exists at $ConfigFile"
    Write-Host "   (Edit manually if needed: notepad $ConfigFile)"
}
Write-Host ""

# Step 7: Test collector
Write-Step "Step 7: Testing collector (disk mode)"
Write-Host "Testing disk collection (this may take a minute)..."
$PythonExe = "$VenvDir\Scripts\python.exe"
Push-Location $CollectorDir
& $PythonExe collector.py --config local\config.json --mode disk 2>&1 | Out-Null
$TestResult = $LASTEXITCODE
Pop-Location
if ($TestResult -eq 0) {
    Write-Success "Disk collection test passed"
} else {
    Write-Host "⚠️  Disk collection test failed (may be normal if Manager not running yet)" -ForegroundColor Yellow
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
Write-Host "1. Edit configuration:"
Write-Host "   notepad $ConfigFile"
Write-Host ""
Write-Host "2. Verify jobs registered:"
Write-Host "   Get-ScheduledTask | grep 'Lab Monitor'"
Write-Host ""
Write-Host "3. Monitor collections:"
Write-Host "   Get-Content -Path '$RootDir\logs\collector.log' -Tail 20 -Wait"
Write-Host ""
