#
# Lab Monitor - Manager + Dashboard Installation Script
# For Windows Server (atlantis.med.harvard.edu)
#
# Usage (as Administrator):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-manager-dashboard.ps1 -ManagerToken "your-secret-token" -DashboardToken "same-token"
#
# This script:
# 1. Creates directory structure
# 2. Clones lab-monitor repository
# 3. Creates/activates Conda environment
# 4. Installs dependencies (Flask, psutil, pywin32)
# 5. Creates Manager and Dashboard config files
# 6. Tests both services locally
# 7. Installs as Windows services (pywin32)
# 8. Configures Windows Firewall
# 9. Tests end-to-end
#

param(
    [string]$ManagerToken = "CHANGE-ME-TO-SECURE-TOKEN",
    [string]$DashboardToken = $ManagerToken,
    [string]$ManagerPort = "5000",
    [string]$DashboardPort = "5001"
)

# Check if running as Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator" -ForegroundColor Red
    exit 1
}

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Lab Monitor - Manager + Dashboard Install" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$RootDir = "E:\Users\lab-monitor"
$DataDir = "$RootDir\data"
$LogsDir = "$RootDir\logs"
$ScriptsDir = "$RootDir\scripts"
$CondaEnv = "lab-monitor"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"

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
if ($LASTEXITCODE -ne 0) {
    Write-Error-Custom "Conda not found on PATH"
    Write-Host "Please run this script from an Anaconda PowerShell prompt"
    Write-Host "Or activate conda with: conda init powershell"
    exit 1
}
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

Write-Host "  Installing Flask, requests, psutil, pywin32..."
conda run -n $CondaEnv pip install Flask==2.3.3 Flask-CORS==4.0.0 requests==2.31.0 psutil==5.9.6 pywin32==305 2>&1 | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Success "Dependencies installed"
} else {
    Write-Host "[WARNING] Some dependencies may have failed to install" -ForegroundColor Yellow
}
Write-Host ""

# Step 6: Locate Python executable
Write-Step "Step 6: Locating environment Python"
$PythonExe = conda run -n $CondaEnv python -c "import sys; print(sys.executable)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error-Custom "Could not locate Python in environment"
    exit 1
}
Write-Success "Found Python: $PythonExe"
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

# Step 9: Create Manager configuration
Write-Step "Step 9: Creating Manager configuration"
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

# Step 10: Create Dashboard configuration
Write-Step "Step 10: Creating Dashboard configuration"
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



# Step 11: Test Dashboard locally
Write-Step "Step 11: Testing Dashboard service"
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

# Step 12: Install Windows services (pywin32)
Write-Step "Step 12: Installing Windows services (pywin32)"
Write-Host "This allows Manager and Dashboard to start automatically on reboot..."

# Install Manager service
$ManagerService = "$ManagerDir\ManagerService.py"
if (Test-Path $ManagerService) {
    Write-Host "  Installing Manager service..."
    conda run -n $CondaEnv python $ManagerService install 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Manager service installed"
        # Start the service
        conda run -n $CondaEnv python $ManagerService start 2>&1 | Out-Null
        Write-Host "    (service started)"
    } else {
        Write-Host "[WARNING] Manager service installation may have failed" -ForegroundColor Yellow
    }
} else {
    Write-Host "[WARNING] ManagerService.py not found at $ManagerService" -ForegroundColor Yellow
}

# Install Dashboard service
$DashboardService = "$DashboardDir\DashboardService.py"
if (Test-Path $DashboardService) {
    Write-Host "  Installing Dashboard service..."
    conda run -n $CondaEnv python $DashboardService install 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Dashboard service installed"
        # Start the service
        conda run -n $CondaEnv python $DashboardService start 2>&1 | Out-Null
        Write-Host "    (service started)"
    } else {
        Write-Host "[WARNING] Dashboard service installation may have failed" -ForegroundColor Yellow
    }
} else {
    Write-Host "[WARNING] DashboardService.py not found at $DashboardService" -ForegroundColor Yellow
}
Write-Host ""

# Step 13: Configure Windows Firewall
Write-Step "Step 13: Configuring Windows Firewall"
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

# Step 14: Verify services
Write-Step "Step 14: Verifying services"
Start-Sleep -Seconds 2
$ManagerRunning = Get-Service -Name "Lab Monitor Manager" -ErrorAction SilentlyContinue | Where-Object {$_.Status -eq "Running"}
$DashboardRunning = Get-Service -Name "Lab Monitor Dashboard" -ErrorAction SilentlyContinue | Where-Object {$_.Status -eq "Running"}

if ($ManagerRunning) {
    Write-Success "Manager service is running"
} else {
    Write-Host "[WARNING] Manager service may not be running (check Event Viewer)" -ForegroundColor Yellow
}

if ($DashboardRunning) {
    Write-Success "Dashboard service is running"
} else {
    Write-Host "[WARNING] Dashboard service may not be running (check Event Viewer)" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[OK] Manager + Dashboard Installation Complete" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Verify services running:"
Write-Host "   Get-Service 'Lab Monitor*' | select Name, Status"
Write-Host ""
Write-Host "2. Access Dashboard:"
Write-Host "   http://atlantis.med.harvard.edu:$DashboardPort"
Write-Host ""
Write-Host "3. Check logs:"
Write-Host "   Get-Content '$LogsDir\manager.log' -Tail 20"
Write-Host "   Get-Content '$LogsDir\dashboard.log' -Tail 20"
Write-Host ""
Write-Host "4. Deploy collectors on Synology and Windows servers:"
Write-Host "   See collector/README.md and use install-collector-*.sh/ps1"
Write-Host ""
Write-Host "[IMPORTANT] Make sure collector config has correct manager_token:" -ForegroundColor Yellow
Write-Host "   Token used: $ManagerToken"
Write-Host ""
