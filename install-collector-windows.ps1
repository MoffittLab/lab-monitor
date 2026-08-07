#
# Lab Monitor - Windows Server Collector Installation Script
#
# Usage (as Administrator from Anaconda PowerShell Prompt):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-collector-windows.ps1
#
# This script:
# 0. Prompts for drive selection (E:\ by default, but customizable)
# 1. Automatically detects and uses the 'lab-monitor' conda environment (if it exists)
# 2. Creates directory structure at \<Drive>\Users\lab-monitor
# 3. Clones or updates lab-monitor repository
# 4. Installs dependencies
# 5. Creates config.json from template (interactive)
# 6. Sets up Task Scheduler jobs
# 7. Tests collection modes
# 8. Displays manual Task Scheduler configuration (optional fallback)
#
# Pre-requisites:
#   - MUST run from Anaconda PowerShell Prompt as Administrator
#   - CONDA MUST be available in your PATH
#   - Optional: Create conda environment first with:
#     conda create -n lab-monitor python=3.11
#
# Administrator Rights:
#   This script requires administrator privileges for:
#   - Creating directories in the selected drive root
#   - Installing Python dependencies via pip
#   - Registering Task Scheduler jobs (runs as SYSTEM)
#
#   To run as Administrator:
#   1. Search for 'Anaconda PowerShell Prompt' in Start Menu
#   2. Right-click it and select 'Run as administrator'
#   3. Accept the UAC prompt when asked
#   4. Then run this script
#

# Verify conda is available and find/activate lab-monitor environment
if (-not $env:CONDA_EXE) {
    Write-Host "ERROR: Conda not found (CONDA_EXE not set)" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "Please run this script from an Anaconda Prompt or Anaconda PowerShell Prompt" -ForegroundColor Yellow
    exit 1
}

# Initialize conda for PowerShell if needed
if (-not (Test-Path Function:\conda)) {
    & "$env:CONDA_EXE" shell.powershell hook | Out-String | Invoke-Expression
}

Write-Host "Checking for lab-monitor conda environment..." -ForegroundColor Cyan

# Check if lab-monitor environment exists using conda info
try {
    $CondaInfo = & cmd /c "$env:CONDA_EXE info --envs" 2>&1 | Out-String
    $LabMonitorExists = $CondaInfo | Select-String -Pattern "lab-monitor"
    
    if ($LabMonitorExists) {
        Write-Host "[OK] lab-monitor conda environment found" -ForegroundColor Green
        Write-Host "Activating lab-monitor environment..." -ForegroundColor Cyan
        
        # Activate the lab-monitor environment
        conda activate lab-monitor
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK] Using conda environment: $env:CONDA_DEFAULT_ENV" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Activation may have issues, but continuing..." -ForegroundColor Yellow
        }
    } else {
        Write-Host "INFO: lab-monitor conda environment not yet created" -ForegroundColor Cyan
        Write-Host "Note: You can create it before running with:" -ForegroundColor Yellow
        Write-Host "  conda create -n lab-monitor python=3.11" -ForegroundColor Yellow
        Write-Host "" -ForegroundColor Yellow
        Write-Host "Continuing with current conda environment..." -ForegroundColor Cyan
    }
} catch {
    Write-Host "WARNING: Could not check for lab-monitor environment" -ForegroundColor Yellow
    Write-Host "Continuing with current environment..." -ForegroundColor Yellow
}

# Final verification: ensure we have an active conda environment
if (-not $env:CONDA_DEFAULT_ENV -and -not $env:CONDA_PREFIX) {
    Write-Host "ERROR: No active conda environment detected" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "Please ensure you're running from Anaconda Prompt or PowerShell with conda initialized" -ForegroundColor Yellow
    exit 1
}

# Check if running as Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "Right-click on Anaconda Prompt and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

# Define helper functions early (before they're used)
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

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Lab Monitor - Windows Collector Install" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Step 0: Prompt for installation drive
Write-Step "Step 0: Selecting installation drive"
Write-Host "Detected drives on this system:" -ForegroundColor Cyan
$AvailableDrives = @()
foreach ($drive in [System.IO.DriveInfo]::GetDrives() | Where-Object {$_.DriveType -eq 'Fixed'}) {
    $driveLetter = $drive.Name.TrimEnd(':\')
    $AvailableDrives += $driveLetter
    $freeSpace = [math]::Round($drive.AvailableFreeSpace / 1GB, 2)
    $totalSpace = [math]::Round($drive.TotalSize / 1GB, 2)
    Write-Host "  ${driveLetter}: $freeSpace GB free / $totalSpace GB total" -ForegroundColor Gray
}

if ($AvailableDrives.Count -eq 0) {
    Write-Error-Custom "No fixed drives found on this system"
    exit 1
}

$DefaultDrive = if ($AvailableDrives -contains "E") { "E" } else { $AvailableDrives[0] }
Write-Host ""
Write-Host "Where would you like to install the lab-monitor collector?" -ForegroundColor Cyan
Write-Host "(This will create \\Users\\lab-monitor on the selected drive)" -ForegroundColor Gray
$DriveInput = Read-Host "Enter drive letter (default: $DefaultDrive)"
$SelectedDrive = if ([string]::IsNullOrWhiteSpace($DriveInput)) { $DefaultDrive } else { $DriveInput.TrimEnd(':') }

if ($SelectedDrive -notin $AvailableDrives) {
    Write-Error-Custom "Drive '$SelectedDrive' not found. Available drives: $($AvailableDrives -join ', ')"
    exit 1
}

Write-Success "Using drive: $SelectedDrive"
Write-Host ""

# Configuration
$RootDir = "${SelectedDrive}:\Users\lab-monitor"
$DataDir = "$RootDir\data"
$LogsDir = "$RootDir\logs"
$ScriptsDir = "$RootDir\scripts"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"
$CollectorDir = "$ScriptsDir\lab-monitor\collector"
$ConfigFile = "$CollectorDir\local\config.json"
$CondaEnv = $env:CONDA_DEFAULT_ENV
if (-not $CondaEnv) {
    Write-Host "ERROR: Could not determine active conda environment" -ForegroundColor Red
    exit 1
}
$PythonExe = (Get-Command python.exe).Source  # Use conda env's python

# Step 1: Create directory structure
Write-Step "Step 1: Creating directory structure at $RootDir"
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
        Write-Host " WARNING:  Warning: URL starts with https:// but Manager runs plain HTTP" -ForegroundColor Yellow
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
        Write-Host " WARNING:  Warning: '$DeviceType' is not a recognized device type. Continuing anyway." -ForegroundColor Yellow
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

    # Display what will be profiled based on scan depth
    Write-Step "Disk Profiling Preview (Scan Depth: $ScanDepth)"
    Write-Host "The disk collection will profile the following folder levels:" -ForegroundColor Cyan
    Write-Host ""
    
    switch ($ScanDepth) {
        1 {
            Write-Host "  [Level 1] Volume root only" -ForegroundColor Yellow
            Write-Host "    Example: E:\ (filesystem stats only)" -ForegroundColor Gray
            Write-Host "    [OK] Fastest" -ForegroundColor Green
            Write-Host "    [X] No per-folder breakdown" -ForegroundColor Red
        }
        2 {
            Write-Host "  [Level 1] Volume root" -ForegroundColor Yellow
            Write-Host "    Example: E:\" -ForegroundColor Gray
            Write-Host "  [Level 2] Top-level folders" -ForegroundColor Yellow
            Write-Host "    Examples: E:\Data, E:\Backups, E:\Projects" -ForegroundColor Gray
            Write-Host "    [OK] Standard: captures main data areas" -ForegroundColor Green
        }
        3 {
            Write-Host "  [Level 1] Volume root" -ForegroundColor Yellow
            Write-Host "    Example: E:\" -ForegroundColor Gray
            Write-Host "  [Level 2] Top-level folders" -ForegroundColor Yellow
            Write-Host "    Examples: E:\Data, E:\Backups, E:\Projects" -ForegroundColor Gray
            Write-Host "  [Level 3] Subfolders" -ForegroundColor Yellow
            Write-Host "    Examples: E:\Data\2024, E:\Data\2024\Experiments" -ForegroundColor Gray
            Write-Host "    [OK] Detailed breakdown of folder structures" -ForegroundColor Green
            Write-Host "    [X] Takes longer on deep hierarchies" -ForegroundColor Red
        }
        default {
            Write-Host "  Custom depth: $ScanDepth levels" -ForegroundColor Yellow
        }
    }
    Write-Host ""
    Write-Host "These profiled folders and their sizes will be sent to the Manager" -ForegroundColor Cyan
    Write-Host "The full filesystem hierarchy is preserved in your local data directory" -ForegroundColor Cyan
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
    Write-Host " WARNING:  Metrics collection test failed (may be normal if Manager not running yet)" -ForegroundColor Yellow
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
    Write-Host " WARNING:  Could not register disk job: $_" -ForegroundColor Yellow
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
    Write-Host " WARNING:  Could not register metrics job: $_" -ForegroundColor Yellow
}
Write-Host ""

# Step 9: Display manual Task Scheduler configuration instructions
Write-Step "Step 9: Manual Task Scheduler Configuration (Optional)"
Write-Host "If automatic registration failed, you can manually create the scheduled tasks:" -ForegroundColor Cyan
Write-Host ""
Write-Host "DISK COLLECTION TASK (Daily at 2:00 AM):" -ForegroundColor Yellow
Write-Host "===============================================" -ForegroundColor Yellow
Write-Host "1. Open Windows Task Scheduler (search 'Task Scheduler' in Start Menu)" -ForegroundColor White
Write-Host "2. Click 'Create Task' on the right sidebar" -ForegroundColor White
Write-Host "3. Go to the 'General' tab:" -ForegroundColor White
Write-Host "   - Name: Lab Monitor - Disk Collection" -ForegroundColor Gray
Write-Host "   - Check: 'Run with highest privileges'" -ForegroundColor Gray
Write-Host "4. Go to the 'Triggers' tab:" -ForegroundColor White
Write-Host "   - Click 'New'" -ForegroundColor Gray
Write-Host "   - Begin the task: On a schedule" -ForegroundColor Gray
Write-Host "   - Set to: Daily" -ForegroundColor Gray
Write-Host "   - Start time: 02:00:00 (2:00 AM)" -ForegroundColor Gray
Write-Host "   - Repeat every: 1 day" -ForegroundColor Gray
Write-Host "5. Go to the 'Actions' tab:" -ForegroundColor White
Write-Host "   - Click 'New'" -ForegroundColor Gray
Write-Host "   - Action: Start a program" -ForegroundColor Gray
Write-Host "   - Program/script: $PythonExe" -ForegroundColor Gray
Write-Host "   - Arguments: $CollectorDir\collector.py --config local\config.json --mode disk" -ForegroundColor Gray
Write-Host "   - Start in: $CollectorDir" -ForegroundColor Gray
Write-Host "6. Click 'OK' to save" -ForegroundColor White
Write-Host ""
Write-Host "METRICS COLLECTION TASK (Every 5 minutes):" -ForegroundColor Yellow
Write-Host "===============================================" -ForegroundColor Yellow
Write-Host "1. Open Windows Task Scheduler" -ForegroundColor White
Write-Host "2. Click 'Create Task'" -ForegroundColor White
Write-Host "3. Go to the 'General' tab:" -ForegroundColor White
Write-Host "   - Name: Lab Monitor - Metrics Collection" -ForegroundColor Gray
Write-Host "   - Check: 'Run with highest privileges'" -ForegroundColor Gray
Write-Host "4. Go to the 'Triggers' tab:" -ForegroundColor White
Write-Host "   - Click 'New'" -ForegroundColor Gray
Write-Host "   - Begin the task: On a schedule" -ForegroundColor Gray
Write-Host "   - Set to: Daily" -ForegroundColor Gray
Write-Host "   - Start time: 00:00:00 (midnight, or any time)" -ForegroundColor Gray
Write-Host "   - Repeat task every: 5 minutes" -ForegroundColor Gray
Write-Host "   - For a duration of: 1 day (repeats continuously)" -ForegroundColor Gray
Write-Host "5. Go to the 'Actions' tab:" -ForegroundColor White
Write-Host "   - Click 'New'" -ForegroundColor Gray
Write-Host "   - Action: Start a program" -ForegroundColor Gray
Write-Host "   - Program/script: $PythonExe" -ForegroundColor Gray
Write-Host "   - Arguments: $CollectorDir\collector.py --config local\config.json --mode metrics" -ForegroundColor Gray
Write-Host "   - Start in: $CollectorDir" -ForegroundColor Gray
Write-Host "6. Click 'OK' to save" -ForegroundColor White
Write-Host ""
Write-Host "VERIFY TASKS:" -ForegroundColor Yellow
Write-Host "=============" -ForegroundColor Yellow
Write-Host "- Open Task Scheduler and look for tasks named 'Lab Monitor'" -ForegroundColor White
Write-Host "- Right-click a task and select 'Run' to test it immediately" -ForegroundColor White
Write-Host "- Check the log file for results: $LogsDir\collector.log" -ForegroundColor White
Write-Host ""

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[OK] Windows Collector Installation Complete" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host 'VERIFICATION & NEXT STEPS:' -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Review your configuration:" -ForegroundColor White
Write-Host "   notepad '$ConfigFile'" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Verify scheduled tasks were created:" -ForegroundColor White
Write-Host "   Get-ScheduledTask | Select-String 'Lab Monitor'" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Test disk collection manually:" -ForegroundColor White
Write-Host "   cd '$CollectorDir'" -ForegroundColor Gray
Write-Host '   & $PythonExe collector.py --config local\config.json --mode disk' -ForegroundColor Gray
Write-Host ""
Write-Host "4. Monitor ongoing collections (follow log in real-time):" -ForegroundColor White
Write-Host "   Get-Content -Path '$LogsDir\collector.log' -Tail 50 -Wait" -ForegroundColor Gray
Write-Host ""
Write-Host "5. Check task execution history in Task Scheduler:" -ForegroundColor White
Write-Host "   Open 'Task Scheduler' and navigate to Task Scheduler Library" -ForegroundColor Gray
Write-Host "   Right-click 'Lab Monitor - Disk Collection' -> View All Tasks" -ForegroundColor Gray
Write-Host ""
Write-Host "IMPORTANT NOTES:" -ForegroundColor Yellow
Write-Host "===================" -ForegroundColor Yellow
Write-Host "- The collector will profile folders at depth $ScanDepth" -ForegroundColor White
Write-Host "- Disk collection runs daily at 2:00 AM (slow operation)" -ForegroundColor White
Write-Host "- Metrics collection runs every 5 minutes (lightweight)" -ForegroundColor White
Write-Host "- Check logs in: $LogsDir" -ForegroundColor White
Write-Host ""
