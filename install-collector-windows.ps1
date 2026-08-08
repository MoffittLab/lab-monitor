#
# Lab Monitor - Windows Server Collector Installation Script
#
# Usage (as Administrator from Anaconda PowerShell Prompt):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-collector-windows.ps1
#
# This script:
#   0. Prompts for installation drive selection
#   1. Detects and activates the 'lab-monitor' conda environment
#   2. Creates directory structure at <Drive>:\Users\lab-monitor
#   3. Clones or updates the lab-monitor repository
#   4. Installs Python dependencies into the conda environment
#   5. Gathers configuration interactively
#   6. Creates wrapper batch scripts with hardcoded Python paths
#   7. Tests collector
#   8. Registers Task Scheduler jobs (auto-detects SYSTEM vs. user logon mode)
#   9. Displays manual Task Scheduler instructions (fallback)
#
# Pre-requisites:
#   - Run from Anaconda PowerShell Prompt as Administrator
#   - Optional: create the conda environment first:
#       conda create -n lab-monitor python=3.11
#
# Why batch scripts for scheduling?
#   Task Scheduler jobs run outside any conda session. Wrapper .bat files
#   embed a hardcoded absolute path to the conda env's python.exe so no
#   'conda activate' is needed at run time. When Python is at a system-wide
#   path the tasks are registered as SYSTEM and run regardless of who is
#   logged on. When Python is in a per-user profile the installer captures the
#   account password for unattended execution, or falls back to 'only when
#   logged on' if no password is supplied.
#
# Administrator rights are required for:
#   - Creating directories on the selected drive
#   - Installing pip packages
#   - Registering Task Scheduler jobs
#
#   To run as Administrator:
#   1. Search for 'Anaconda PowerShell Prompt' in the Start Menu
#   2. Right-click -> Run as administrator
#   3. Accept the UAC prompt
#   4. Navigate to this script and run it
#

# ==============================================================================
# Conda availability check
# ==============================================================================
if (-not $env:CONDA_EXE) {
    Write-Host "ERROR: Conda not found (CONDA_EXE is not set)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please run this script from an Anaconda PowerShell Prompt." -ForegroundColor Yellow
    exit 1
}

# Initialise conda for PowerShell if the 'conda' function is not yet present
if (-not (Test-Path Function:\conda)) {
    & "$env:CONDA_EXE" shell.powershell hook | Out-String | Invoke-Expression
}

Write-Host "Checking for lab-monitor conda environment..." -ForegroundColor Cyan

try {
    $CondaEnvList   = & "$env:CONDA_EXE" env list 2>&1 | Out-String
    $LabMonitorLine = $CondaEnvList | Select-String -Pattern "lab-monitor"

    if ($LabMonitorLine) {
        Write-Host "[OK] lab-monitor environment found" -ForegroundColor Green
        Write-Host "Activating lab-monitor..." -ForegroundColor Cyan
        conda activate lab-monitor
        if ($env:CONDA_DEFAULT_ENV -eq "lab-monitor") {
            Write-Host "[OK] Active environment: lab-monitor" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Activation may not have taken effect" -ForegroundColor Yellow
            Write-Host "         Continuing - Python path will be resolved from CONDA_PREFIX" -ForegroundColor Yellow
        }
    } else {
        Write-Host "INFO: lab-monitor environment not found" -ForegroundColor Cyan
        Write-Host "      Create it first with: conda create -n lab-monitor python=3.11" -ForegroundColor Yellow
        Write-Host "      Continuing with current environment..." -ForegroundColor Cyan
    }
} catch {
    Write-Host "WARNING: Could not enumerate conda environments: $_" -ForegroundColor Yellow
    Write-Host "         Continuing with current environment..." -ForegroundColor Yellow
}

if (-not $env:CONDA_DEFAULT_ENV -and -not $env:CONDA_PREFIX) {
    Write-Host "ERROR: No active conda environment detected" -ForegroundColor Red
    Write-Host "       Run from an Anaconda PowerShell Prompt with an environment active." -ForegroundColor Yellow
    exit 1
}

# ==============================================================================
# Administrator check
# ==============================================================================
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator" -ForegroundColor Red
    Write-Host "       Right-click Anaconda PowerShell Prompt -> Run as administrator" -ForegroundColor Yellow
    exit 1
}

# ==============================================================================
# Helper functions  (defined early so all steps can use them)
# ==============================================================================
function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "  $Message" -ForegroundColor Yellow
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
}

# ==============================================================================
# Banner
# ==============================================================================
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Lab Monitor - Windows Collector Install" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ==============================================================================
# Step 0: Drive selection
# ==============================================================================
Write-Step "Step 0: Selecting installation drive"

Write-Host "Fixed drives detected on this system:" -ForegroundColor Cyan
$AvailableDrives = @()
foreach ($drive in [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.DriveType -eq 'Fixed' }) {
    # Use Substring(0,1) - TrimEnd(':\') is fragile as it trims individual chars
    $letter  = $drive.Name.Substring(0, 1).ToUpper()
    $freeGB  = [math]::Round($drive.AvailableFreeSpace / 1GB, 2)
    $totalGB = [math]::Round($drive.TotalSize / 1GB, 2)
    $AvailableDrives += $letter
    Write-Host "  ${letter}: $freeGB GB free / $totalGB GB total" -ForegroundColor Gray
}

if ($AvailableDrives.Count -eq 0) {
    Write-Err "No fixed drives found on this system"
    exit 1
}

$DefaultDrive = if ($AvailableDrives -contains "E") { "E" } else { $AvailableDrives[0] }

Write-Host ""
Write-Host "Where would you like to install the lab-monitor collector?" -ForegroundColor Cyan
Write-Host "(Creates <Drive>:\Users\lab-monitor)" -ForegroundColor Gray
$DriveInput    = Read-Host "Enter drive letter (default: $DefaultDrive)"
$SelectedDrive = if ([string]::IsNullOrWhiteSpace($DriveInput)) {
    $DefaultDrive
} else {
    # Accept 'e', 'E', 'e:', 'E:' - normalise to single uppercase letter
    $DriveInput.TrimEnd(':').Substring(0, 1).ToUpper()
}

if ($SelectedDrive -notin $AvailableDrives) {
    Write-Err "Drive '$SelectedDrive' not found. Available: $($AvailableDrives -join ', ')"
    exit 1
}

Write-Success "Installing to drive ${SelectedDrive}:"
Write-Host ""

# ==============================================================================
# Configuration variables  (all paths derived from selected drive)
# ==============================================================================
$RootDir      = "${SelectedDrive}:\Users\lab-monitor"
$DataDir      = "$RootDir\data"
$LogsDir      = "$RootDir\logs"
$ScriptsDir   = "$RootDir\scripts"
$BatchDir     = "$RootDir\bin"           # wrapper .bat scripts live here
$RepoUrl      = "https://github.com/MoffittLab/lab-monitor.git"
$CollectorDir = "$ScriptsDir\lab-monitor\collector"
$ConfigFile   = "$CollectorDir\local\config.json"
$CondaEnv     = $env:CONDA_DEFAULT_ENV

# --- Locate python.exe inside the active conda environment -------------------
# CONDA_PREFIX is set by 'conda activate' to the full env directory path.
# We use this rather than relying on whatever 'python' is in PATH, so the
# batch scripts we create will embed the correct absolute path.
$PythonExe = $null
if ($env:CONDA_PREFIX) {
    $candidate = Join-Path $env:CONDA_PREFIX "python.exe"
    if (Test-Path $candidate) { $PythonExe = $candidate }
}
if (-not $PythonExe) {
    # Fallback: first python.exe visible in PATH
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($found) { $PythonExe = $found.Source }
}
if (-not $PythonExe) {
    Write-Err "Cannot locate python.exe. Ensure the lab-monitor conda environment is activated."
    exit 1
}

# Batch script paths (created in Step 6, used by Task Scheduler)
$DiskBat    = "$BatchDir\run-disk-collector.bat"
$MetricsBat = "$BatchDir\run-metrics-collector.bat"

# ScanDepth is initialised here so it is always defined even if config already
# exists and the interactive block is skipped (avoids blank output in summary)
$ScanDepth = 2

# ==============================================================================
# Step 1: Create directory structure
# ==============================================================================
Write-Step "Step 1: Creating directory structure"
foreach ($dir in @($DataDir, $LogsDir, $ScriptsDir, $BatchDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
Write-Success "Directories ready under $RootDir"

# ==============================================================================
# Step 2: Verify Python
# ==============================================================================
Write-Step "Step 2: Verifying Python"
$PythonVersion = & "$PythonExe" --version 2>&1
Write-Success "Python:      $PythonVersion"
Write-Success "Executable:  $PythonExe"
Write-Success "Conda env:   $CondaEnv"
Write-Host "  (CONDA_PREFIX: $env:CONDA_PREFIX)" -ForegroundColor Gray

# ==============================================================================
# Step 3: Clone / update repository
# ==============================================================================
Write-Step "Step 3: Cloning/updating lab-monitor repository"
if (Test-Path "$ScriptsDir\lab-monitor\.git") {
    Write-Host "Repository already present - pulling latest..." -ForegroundColor Cyan
    Push-Location "$ScriptsDir\lab-monitor"
    $gitOut  = & git pull origin main 2>&1
    $gitExit = $LASTEXITCODE
    Pop-Location
    if ($gitExit -ne 0) {
        Write-Warn "git pull exited with code $gitExit"
        Write-Host ($gitOut | Out-String) -ForegroundColor Gray
    } else {
        Write-Success "Repository updated"
    }
} else {
    Write-Host "Cloning from $RepoUrl into $ScriptsDir\lab-monitor ..." -ForegroundColor Cyan
    $gitOut  = & git clone $RepoUrl "$ScriptsDir\lab-monitor" 2>&1
    $gitExit = $LASTEXITCODE
    if ($gitExit -ne 0) {
        Write-Warn "git clone exited with code $gitExit"
        Write-Host ($gitOut | Out-String) -ForegroundColor Gray
    }
}
if (Test-Path "$CollectorDir\collector.py") {
    Write-Success "Repository ready at $CollectorDir"
} else {
    Write-Err "collector.py not found at $CollectorDir"
    Write-Host "  Verify that git is installed and $RepoUrl is reachable" -ForegroundColor Yellow
    exit 1
}

# ==============================================================================
# Step 4: Conda environment info
# ==============================================================================
Write-Step "Step 4: Conda environment"
Write-Success "Environment: $CondaEnv"
Write-Host "  Python:  $PythonExe" -ForegroundColor Gray
Write-Host "  Prefix:  $env:CONDA_PREFIX" -ForegroundColor Gray

# ==============================================================================
# Step 5: Install Python dependencies
# ==============================================================================
Write-Step "Step 5: Installing Python dependencies"
Write-Host "Upgrading pip..." -ForegroundColor Cyan
& "$PythonExe" -m pip install --upgrade pip 2>&1 | Out-Null

Write-Host "Installing from requirements.txt..." -ForegroundColor Cyan
Push-Location $CollectorDir
$pipOut  = & "$PythonExe" -m pip install -r requirements.txt 2>&1
$pipExit = $LASTEXITCODE
Pop-Location

if ($pipExit -eq 0) {
    Write-Success "Dependencies installed"
} else {
    Write-Err "pip install failed (exit code $pipExit)"
    Write-Host ($pipOut | Out-String) -ForegroundColor Gray
    exit 1
}

# ==============================================================================
# Step 6: Create wrapper batch scripts
# ==============================================================================
Write-Step "Step 6: Creating wrapper batch scripts"
Write-Host "These batch scripts embed the full absolute path to python.exe" -ForegroundColor Cyan
Write-Host "inside the conda environment. Task Scheduler calls them via cmd.exe," -ForegroundColor Cyan
Write-Host "so they work even when no conda session is active and even when the" -ForegroundColor Cyan
Write-Host "task runs as a different Windows user account." -ForegroundColor Cyan
Write-Host ""

$DiskBatContent = @"
@echo off
REM Lab Monitor - Disk Collection
REM Auto-generated by install-collector-windows.ps1 -- do not edit manually.
REM Python: $PythonExe
setlocal
cd /d "$CollectorDir"
"$PythonExe" collector.py --config local\config.json --mode disk
endlocal
"@

$MetricsBatContent = @"
@echo off
REM Lab Monitor - Metrics Collection
REM Auto-generated by install-collector-windows.ps1 -- do not edit manually.
REM Python: $PythonExe
setlocal
cd /d "$CollectorDir"
"$PythonExe" collector.py --config local\config.json --mode metrics
endlocal
"@

Set-Content -Path $DiskBat    -Value $DiskBatContent    -Encoding ASCII
Set-Content -Path $MetricsBat -Value $MetricsBatContent -Encoding ASCII

Write-Success "Disk script:    $DiskBat"
Write-Success "Metrics script: $MetricsBat"
Write-Host ""
Write-Host "Test them at any time from an admin prompt:" -ForegroundColor Gray
Write-Host "  cmd /c `"$DiskBat`"" -ForegroundColor Gray
Write-Host "  cmd /c `"$MetricsBat`"" -ForegroundColor Gray

# ==============================================================================
# Step 7: Gather configuration (interactive)
# ==============================================================================
Write-Step "Step 7: Gathering configuration"
$LocalDir = "$CollectorDir\local"
if (-not (Test-Path $LocalDir)) { New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null }

if (-not (Test-Path $ConfigFile)) {

    Write-Host "Auto-detected values:" -ForegroundColor Cyan
    $ServerName = $env:COMPUTERNAME.ToLower()
    Write-Host "  Server name: $ServerName" -ForegroundColor Gray
    Write-Host ""

    # -- Manager URL ----------------------------------------------------------
    $ManagerUrl = Read-Host "Enter Manager URL (e.g., http://atlantis.med.harvard.edu:5000)"
    if ([string]::IsNullOrWhiteSpace($ManagerUrl)) {
        Write-Err "Manager URL cannot be empty"
        exit 1
    }
    if ($ManagerUrl -like "https://*") {
        Write-Warn "URL starts with https:// but Manager runs plain HTTP"
        $Confirm = Read-Host "Continue with https:// anyway? [y/N]"
        if ($Confirm -ne "y" -and $Confirm -ne "Y") {
            Write-Host "Please re-run with the correct URL."
            exit 1
        }
    }

    # -- Manager Token --------------------------------------------------------
    $ManagerToken = Read-Host "Enter Manager Token (from Manager config)"
    if ([string]::IsNullOrWhiteSpace($ManagerToken)) {
        Write-Err "Manager Token cannot be empty"
        exit 1
    }

    # -- Device Type ----------------------------------------------------------
    Write-Host ""
    Write-Host "Device types:" -ForegroundColor Cyan
    Write-Host "  NAS            - Standard NAS (scan top-level folders)" -ForegroundColor Gray
    Write-Host "  NAS-Instrument - Research instrument storage (scan deeper)" -ForegroundColor Gray
    Write-Host "  NAS-Backup     - Backup volume (volume-level only, fast)" -ForegroundColor Gray
    Write-Host "  Server         - Windows Server (scan top-level folders)" -ForegroundColor Gray
    $DeviceType = Read-Host "Enter Device Type [NAS/NAS-Instrument/NAS-Backup/Server] (default: Server)"
    if ([string]::IsNullOrWhiteSpace($DeviceType)) { $DeviceType = "Server" }
    if ($DeviceType -notin @("NAS", "NAS-Instrument", "NAS-Backup", "Server")) {
        Write-Warn "'$DeviceType' is not a recognised device type - continuing anyway"
    }

    # -- Scan depth -----------------------------------------------------------
    $DefaultDepth = switch ($DeviceType) {
        "NAS-Backup"     { 1 }
        "NAS-Instrument" { 3 }
        default          { 2 }
    }
    Write-Host ""
    Write-Host "Scan depth controls how many folder levels are measured:" -ForegroundColor Cyan
    Write-Host "  1 = Volume only        (fast  - recommended for NAS-Backup)" -ForegroundColor Gray
    Write-Host "  2 = Volume + folders   (standard - recommended for NAS/Server)" -ForegroundColor Gray
    Write-Host "  3 = Volume + sub-folders (detailed - recommended for NAS-Instrument)" -ForegroundColor Gray
    $ScanDepthInput = Read-Host "Enter Scan Depth [1/2/3] (default: $DefaultDepth)"
    $ScanDepth = if ([string]::IsNullOrWhiteSpace($ScanDepthInput)) { $DefaultDepth } else { [int]$ScanDepthInput }
    if ($ScanDepth -lt 1 -or $ScanDepth -gt 10) {
        Write-Err "Scan Depth must be between 1 and 10"
        exit 1
    }
    Write-Success "Scan depth: $ScanDepth"

    # -- Volumes --------------------------------------------------------------
    Write-Host ""
    # Show only drives that actually exist on this machine (from Step 0)
    $DriveList = ($AvailableDrives | ForEach-Object { "${_}:" }) -join ", "
    Write-Host "Available fixed drives: $DriveList" -ForegroundColor Cyan
    $VolumesInput = Read-Host "Enter drives to monitor (comma-separated, e.g., E:,F:,G:)"
    if (-not [string]::IsNullOrWhiteSpace($VolumesInput)) {
        $Volumes = @($VolumesInput -split ',' | ForEach-Object { $_.Trim().ToUpper() })
    } else {
        # Default to all detected fixed drives except C: (system drive)
        $Volumes = @($AvailableDrives | Where-Object { $_ -ne "C" } | ForEach-Object { "${_}:" })
        if ($Volumes.Count -eq 0) { $Volumes = @("C:") }
        Write-Host "No input - defaulting to: $($Volumes -join ', ')" -ForegroundColor Gray
    }
    Write-Host "Volumes to monitor: $($Volumes -join ', ')" -ForegroundColor Cyan

    # -- Disk profiling preview -----------------------------------------------
    Write-Host ""
    Write-Host "Disk profiling preview (scan depth $ScanDepth):" -ForegroundColor Cyan
    switch ($ScanDepth) {
        1 {
            Write-Host "  Level 1: Volume root only  (e.g., E:\)" -ForegroundColor Gray
            Write-Host "  [OK] Fastest   [X] No per-folder breakdown" -ForegroundColor Gray
        }
        2 {
            Write-Host "  Level 1: Volume root  (e.g., E:\)" -ForegroundColor Gray
            Write-Host "  Level 2: Top-level folders  (e.g., E:\Data, E:\Backups, E:\Projects)" -ForegroundColor Gray
            Write-Host "  [OK] Standard - captures main data areas" -ForegroundColor Gray
        }
        3 {
            Write-Host "  Level 1: Volume root  (e.g., E:\)" -ForegroundColor Gray
            Write-Host "  Level 2: Top-level folders  (e.g., E:\Data, E:\Backups, E:\Projects)" -ForegroundColor Gray
            Write-Host "  Level 3: Sub-folders  (e.g., E:\Data\2024, E:\Data\2024\Experiments)" -ForegroundColor Gray
            Write-Host "  [OK] Detailed breakdown   [X] Slower on deep hierarchies" -ForegroundColor Gray
        }
        default {
            Write-Host "  Custom depth: $ScanDepth levels" -ForegroundColor Gray
        }
    }
    Write-Host ""

    # -- Write config.json ----------------------------------------------------
    $ConfigContent = @{
        name                    = $ServerName
        id                      = "windows-$ServerName"
        device_type             = $DeviceType
        manager_url             = $ManagerUrl
        manager_token           = $ManagerToken
        volumes                 = $Volumes
        scan_depth              = $ScanDepth
        data_dir                = "$RootDir\data"
        log_file                = "$RootDir\logs\collector.log"
        log_level               = "INFO"
        timeout_seconds         = 3600
        request_timeout_seconds = 30
    } | ConvertTo-Json -Depth 3

    Set-Content -Path $ConfigFile -Value $ConfigContent
    Write-Success "Config written to $ConfigFile"
    Write-Host ""
    Write-Host "  Server name: $ServerName"              -ForegroundColor Gray
    Write-Host "  Device type: $DeviceType"              -ForegroundColor Gray
    Write-Host "  Scan depth:  $ScanDepth"               -ForegroundColor Gray
    Write-Host "  Volumes:     $($Volumes -join ', ')"   -ForegroundColor Gray
    Write-Host "  Manager URL: $ManagerUrl"              -ForegroundColor Gray
    Write-Host "  Token:       (set)"                    -ForegroundColor Gray

} else {
    Write-Success "Config already exists at $ConfigFile"
    Write-Host "  (Edit manually if needed: notepad `"$ConfigFile`")" -ForegroundColor Gray

    # Read ScanDepth from existing config so the final summary is accurate
    try {
        $existingCfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        if ($null -ne $existingCfg.scan_depth) { $ScanDepth = $existingCfg.scan_depth }
    } catch {}
}

# ==============================================================================
# Step 8: Test collector
# ==============================================================================
Write-Step "Step 8: Testing collector (metrics mode)"
Write-Host "Running a quick metrics test against the Manager..." -ForegroundColor Cyan
Push-Location $CollectorDir
& "$PythonExe" collector.py --config local\config.json --mode metrics 2>&1 | Out-Null
$TestExit = $LASTEXITCODE
Pop-Location
if ($TestExit -eq 0) {
    Write-Success "Metrics test passed"
} else {
    Write-Warn "Metrics test returned exit code $TestExit"
    Write-Host "  This is normal if the Manager is not yet running or reachable." -ForegroundColor Gray
}

# ==============================================================================
# Step 9: Register Task Scheduler jobs
# ==============================================================================
Write-Step "Step 9: Registering Task Scheduler jobs"

Write-Host "Tasks invoke the batch scripts in $BatchDir" -ForegroundColor Cyan
Write-Host "The batch scripts embed a hardcoded absolute path to python.exe" -ForegroundColor Cyan
Write-Host "so no conda session is needed when the task runs." -ForegroundColor Cyan
Write-Host ""

$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Current user: $CurrentUser" -ForegroundColor Gray
Write-Host ""

try {
    Import-Module ScheduledTasks -ErrorAction Stop
} catch {
    Write-Warn "Could not import ScheduledTasks module: $_"
    Write-Host "  Attempting to continue anyway..." -ForegroundColor Yellow
}

# --- Determine logon mode ---------------------------------------------------
$UseSystem    = $false
$TaskPassword = $null
$TaskPrincipal = $null

$UserProfileNorm = $env:USERPROFILE.ToLower().TrimEnd('\')
$PythonExeNorm   = $PythonExe.ToLower()

if (-not $PythonExeNorm.StartsWith($UserProfileNorm)) {
    $UseSystem = $true
    Write-Host "Python is at a system-wide path; tasks will run as SYSTEM (no login required)." -ForegroundColor Cyan
    Write-Host "  ($PythonExe)" -ForegroundColor Gray
    # Wrap in try/catch: New-ScheduledTaskPrincipal uses WMI and will throw
    # if the repository is corrupted. A null principal causes Register-LabTask
    # to throw, which triggers the schtasks.exe fallback in each task block.
    try {
        $TaskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest -ErrorAction Stop
    } catch {
        Write-Warn "Could not create task principal via WMI ($_)."
        Write-Host "  schtasks.exe fallback will be used for task registration." -ForegroundColor Yellow
    }
} else {
    Write-Host "Python is inside the per-user profile:" -ForegroundColor Cyan
    Write-Host "  $PythonExe" -ForegroundColor Gray
    Write-Host ""
    Write-Host "To run tasks when '$CurrentUser' is logged off, enter that" -ForegroundColor Yellow
    Write-Host "account's Windows password now. Leave blank to register as" -ForegroundColor Yellow
    Write-Host "'only when logged on' (tasks will not run unattended)." -ForegroundColor Yellow
    Write-Host ""
    $SecurePass = Read-Host "Windows password for $CurrentUser (Enter to skip)" -AsSecureString
    $BSTR       = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePass)
    $PlainPass  = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)
    if (-not [string]::IsNullOrWhiteSpace($PlainPass)) {
        $TaskPassword = $PlainPass
        Write-Success "Password captured; tasks will run unattended."
    } else {
        Write-Warn "No password supplied - tasks will only run while $CurrentUser is logged on."
        Write-Host "  To change later: Task Scheduler -> edit each task -> General tab ->" -ForegroundColor Gray
        Write-Host "  'Run whether user is logged on or not'" -ForegroundColor Gray
    }
}

Write-Host ""

# Helper: register via New-ScheduledTask* cmdlets (WMI path)
function Register-LabTask {
    param(
        [string]$TaskName,
        [string]$Description,
        $Action,
        $Trigger,
        $Settings
    )
    if ($script:UseSystem) {
        if ($null -eq $script:TaskPrincipal) { throw "TaskPrincipal is null (WMI unavailable)" }
        Register-ScheduledTask `
            -TaskName    $TaskName `
            -Description $Description `
            -Action      $Action `
            -Trigger     $Trigger `
            -Settings    $Settings `
            -Principal   $script:TaskPrincipal `
            -Force | Out-Null
    } elseif ($script:TaskPassword) {
        Register-ScheduledTask `
            -TaskName    $TaskName `
            -Description $Description `
            -Action      $Action `
            -Trigger     $Trigger `
            -Settings    $Settings `
            -RunLevel    Highest `
            -User        $script:CurrentUser `
            -Password    $script:TaskPassword `
            -Force | Out-Null
    } else {
        Register-ScheduledTask `
            -TaskName    $TaskName `
            -Description $Description `
            -Action      $Action `
            -Trigger     $Trigger `
            -Settings    $Settings `
            -RunLevel    Highest `
            -User        $script:CurrentUser `
            -Force | Out-Null
    }
}

# Helper: register via schtasks.exe (does not use WMI - reliable fallback)
function Register-LabTask-Schtasks {
    param(
        [string]$TaskName,
        [string]$BatPath,
        [string[]]$ScheduleArgs   # e.g. @("/sc","DAILY","/st","02:00") or @("/sc","MINUTE","/mo","5")
    )
    $ruArgs = if ($script:UseSystem) {
        @("/ru", "SYSTEM")
    } elseif ($script:TaskPassword) {
        @("/ru", $script:CurrentUser, "/rp", $script:TaskPassword)
    } else {
        @("/ru", $script:CurrentUser)
    }
    $schtasksArgs = @("/create", "/f", "/rl", "HIGHEST",
                      "/tn", $TaskName,
                      "/tr", "cmd.exe /c `"$BatPath`"") + $ruArgs + $ScheduleArgs
    $out = & schtasks.exe @schtasksArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "schtasks.exe exited $LASTEXITCODE`: $out" }
}

# -- Disk collection (daily at 2:00 AM) --------------------------------------
try {
    $DiskAction   = New-ScheduledTaskAction `
                        -Execute  "cmd.exe" `
                        -Argument "/c `"$DiskBat`""

    $DiskTrigger  = New-ScheduledTaskTrigger -Daily -At "2:00 AM"

    $DiskSettings = New-ScheduledTaskSettingsSet `
                        -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
                        -StartWhenAvailable

    Register-LabTask `
        -TaskName    "Lab Monitor - Disk Collection" `
        -Description "Lab Monitor daily disk-usage scan (runs at 2 AM)" `
        -Action      $DiskAction `
        -Trigger     $DiskTrigger `
        -Settings    $DiskSettings

    Write-Success "Disk collection task registered (daily at 2:00 AM)"
    Write-Host "  Script: $DiskBat" -ForegroundColor Gray
} catch {
    Write-Warn "ScheduledTasks cmdlets failed ($_) - trying schtasks.exe..."
    try {
        Register-LabTask-Schtasks `
            -TaskName     "Lab Monitor - Disk Collection" `
            -BatPath      $DiskBat `
            -ScheduleArgs @("/sc", "DAILY", "/st", "02:00")
        Write-Success "Disk collection task registered via schtasks.exe (daily at 2:00 AM)"
        Write-Host "  Script: $DiskBat" -ForegroundColor Gray
    } catch {
        Write-Warn "Could not register disk task: $_"
        Write-Host "  See Step 10 for manual instructions." -ForegroundColor Yellow
    }
}

Write-Host ""

# -- Metrics collection (every 5 minutes, indefinitely) ----------------------
try {
    $MetricsAction = New-ScheduledTaskAction `
                         -Execute  "cmd.exe" `
                         -Argument "/c `"$MetricsBat`""

    # Start 1 minute from now so the trigger fires immediately after
    # registration rather than anchoring to a past midnight time.
    # Repetition is set via .Repetition properties rather than the cmdlet
    # constructor parameters, which are silently ignored on some Windows
    # Server versions and would leave a one-shot task instead of a repeating one.
    $MetricsTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
    $MetricsTrigger.Repetition.Interval          = "PT5M"
    $MetricsTrigger.Repetition.Duration          = "P9999D"
    $MetricsTrigger.Repetition.StopAtDurationEnd = $false

    $MetricsSettings = New-ScheduledTaskSettingsSet `
                           -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                           -StartWhenAvailable `
                           -MultipleInstances   IgnoreNew

    Register-LabTask `
        -TaskName    "Lab Monitor - Metrics Collection" `
        -Description "Lab Monitor system-metrics collection (runs every 5 min)" `
        -Action      $MetricsAction `
        -Trigger     $MetricsTrigger `
        -Settings    $MetricsSettings

    Write-Success "Metrics collection task registered (every 5 min, indefinite)"
    Write-Host "  Script: $MetricsBat" -ForegroundColor Gray
} catch {
    Write-Warn "ScheduledTasks cmdlets failed ($_) - trying schtasks.exe..."
    try {
        Register-LabTask-Schtasks `
            -TaskName     "Lab Monitor - Metrics Collection" `
            -BatPath      $MetricsBat `
            -ScheduleArgs @("/sc", "MINUTE", "/mo", "5")
        Write-Success "Metrics collection task registered via schtasks.exe (every 5 min)"
        Write-Host "  Script: $MetricsBat" -ForegroundColor Gray
    } catch {
        Write-Warn "Could not register metrics task: $_"
        Write-Host "  See Step 10 for manual instructions." -ForegroundColor Yellow
    }
}

# ==============================================================================
# Step 10: Manual Task Scheduler instructions (fallback)
# ==============================================================================
Write-Step "Step 10: Manual Task Scheduler Configuration (if auto-registration failed)"
Write-Host "Both tasks call cmd.exe with a batch script as the argument." -ForegroundColor Cyan
Write-Host ""

Write-Host "DISK COLLECTION TASK (Daily at 2:00 AM):" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Yellow
Write-Host "1. Open Task Scheduler (search Start Menu)" -ForegroundColor White
Write-Host "2. Action menu -> Create Task" -ForegroundColor White
Write-Host "3. General tab:" -ForegroundColor White
Write-Host "     Name:  Lab Monitor - Disk Collection" -ForegroundColor Gray
Write-Host "     Check: Run with highest privileges" -ForegroundColor Gray
Write-Host "     Optional: Run whether user is logged on or not" -ForegroundColor Gray
Write-Host "4. Triggers tab -> New:" -ForegroundColor White
Write-Host "     Begin the task: On a schedule -> Daily at 02:00:00" -ForegroundColor Gray
Write-Host "5. Actions tab -> New:" -ForegroundColor White
Write-Host "     Program/script: cmd.exe" -ForegroundColor Gray
Write-Host "     Arguments:      /c `"$DiskBat`"" -ForegroundColor Gray
Write-Host "6. OK to save" -ForegroundColor White
Write-Host ""

Write-Host "METRICS COLLECTION TASK (Every 5 minutes):" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Yellow
Write-Host "1. Open Task Scheduler -> Action menu -> Create Task" -ForegroundColor White
Write-Host "2. General tab:" -ForegroundColor White
Write-Host "     Name:  Lab Monitor - Metrics Collection" -ForegroundColor Gray
Write-Host "     Check: Run with highest privileges" -ForegroundColor Gray
Write-Host "     Optional: Run whether user is logged on or not" -ForegroundColor Gray
Write-Host "3. Triggers tab -> New:" -ForegroundColor White
Write-Host "     Begin the task: On a schedule -> Daily at 00:00:00" -ForegroundColor Gray
Write-Host "     Check: Repeat task every 5 minutes, for Indefinitely" -ForegroundColor Gray
Write-Host "4. Actions tab -> New:" -ForegroundColor White
Write-Host "     Program/script: cmd.exe" -ForegroundColor Gray
Write-Host "     Arguments:      /c `"$MetricsBat`"" -ForegroundColor Gray
Write-Host "5. Settings tab:" -ForegroundColor White
Write-Host "     Check: Start task as soon as possible after a scheduled start is missed" -ForegroundColor Gray
Write-Host "     If task is already running: Do not start a new instance" -ForegroundColor Gray
Write-Host "6. OK to save" -ForegroundColor White
Write-Host ""

Write-Host "TROUBLESHOOTING TASK REGISTRATION:" -ForegroundColor Yellow
Write-Host "====================================" -ForegroundColor Yellow
Write-Host "If you see 'Invalid namespace' on auto-registration, try:" -ForegroundColor White
Write-Host "  a) Close and re-open Anaconda PowerShell Prompt (as Admin), re-run script" -ForegroundColor Gray
Write-Host "  b) Rebuild WMI - run this in Administrator cmd.exe (not PowerShell):" -ForegroundColor Gray
Write-Host "       winmgmt /salvagerepository" -ForegroundColor Gray
Write-Host "  c) Create tasks manually using the GUI steps above" -ForegroundColor Gray
Write-Host ""

# ==============================================================================
# Final summary
# ==============================================================================
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  [OK] Installation Complete" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "INSTALLED FILES:" -ForegroundColor Yellow
Write-Host "================" -ForegroundColor Yellow
Write-Host "  Root:            $RootDir"            -ForegroundColor Gray
Write-Host "  Config:          $ConfigFile"         -ForegroundColor Gray
Write-Host "  Disk script:     $DiskBat"            -ForegroundColor Gray
Write-Host "  Metrics script:  $MetricsBat"         -ForegroundColor Gray
Write-Host "  Logs:            $LogsDir\collector.log" -ForegroundColor Gray
Write-Host "  Scan depth:      $ScanDepth"          -ForegroundColor Gray
Write-Host ""
Write-Host "VERIFICATION:" -ForegroundColor Yellow
Write-Host "=============" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. Review config:" -ForegroundColor White
Write-Host "     notepad `"$ConfigFile`"" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Confirm tasks registered:" -ForegroundColor White
Write-Host '     Get-ScheduledTask | Where-Object { $_.TaskName -like "*Lab Monitor*" }' -ForegroundColor Gray
Write-Host ""
Write-Host "3. Test batch scripts manually:" -ForegroundColor White
Write-Host "     cmd /c `"$DiskBat`"" -ForegroundColor Gray
Write-Host "     cmd /c `"$MetricsBat`"" -ForegroundColor Gray
Write-Host ""
Write-Host "4. Watch the log live:" -ForegroundColor White
Write-Host "     Get-Content `"$LogsDir\collector.log`" -Tail 50 -Wait" -ForegroundColor Gray
Write-Host ""
