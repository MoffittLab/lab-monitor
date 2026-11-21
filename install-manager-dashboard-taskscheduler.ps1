#
# Lab Monitor - Manager + Dashboard Installation Script (Task Scheduler Version)
# For Windows Server (atlantis.med.harvard.edu)
#
# Usage (as Administrator, from an Anaconda Prompt / Anaconda PowerShell Prompt):
#   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
#   .\install-manager-dashboard-taskscheduler.ps1
#   .\install-manager-dashboard-taskscheduler.ps1 -CollectorNames "Triton,Atlas"
#
# Tokens:
#   - First install: random admin and read tokens are generated. The read token
#     is written into the Dashboard config as manager_token. -CollectorNames
#     generates one collector token per device and prints them.
#   - Re-running KEEPS the existing manager\config.json and dashboard\config.json
#     (tokens are never reset). Pass -ForceNewConfig to regenerate the TOKENS in
#     both configs: every other setting (data_dir, log_file, ports, cors_origins,
#     history_retention_days, the Dashboard login, ...) is kept. The old files
#     are backed up next to them; collectors then need new tokens.
#   - -AdminToken / -ReadToken let you supply your own tokens (>= 16 characters,
#     not a placeholder). -ManagerToken (deprecated) is stored as a shared
#     collector token allowed to write for ANY device ("*").
#
# Task account:
#   - The tasks normally run as the installing user via S4U (no stored
#     password). An S4U task has NO network access, so if data_dir or a
#     log_file is a UNC path (\\server\share\...), the script asks for that
#     account's password (or takes -TaskCredential) and Task Scheduler stores
#     it. Re-run the installer after that password changes.
#   - Mapped drive letters (Z:) are not visible to scheduled tasks: use the UNC
#     path instead. The script stops before changing anything if it finds one.
#
# This script:
# 1.  Creates directory structure
# 2.  Checks Conda
# 3.  Creates/verifies the Conda environment
# 4.  Clones/updates the lab-monitor repository (git errors stop the install;
#     -SkipGitPull skips the pull)
# 5.  Installs dependencies from requirements.txt (Flask, waitress, requests, ...)
# 6.  Creates the Manager config (or keeps the existing one)
# 7.  Creates the Dashboard config (or keeps the existing one)
# 8.  Validates the Manager config and the Python dependencies, and picks the
#     task account (stored password if data_dir/logs are on a network share)
# 9.  Stops Manager/Dashboard processes started by an earlier install
# 10. Creates launcher batch files (output -> logs\*-console.log, restart loop)
# 11. Registers "Lab Monitor Manager" and "Lab Monitor Dashboard" tasks
#     (at startup, whether or not anyone is logged on, restart on failure)
# 12. Configures Windows Firewall
# 13. Starts both tasks
# 14. Verifies the services are running
#

param(
    [string]$AdminToken = "",
    [string]$ReadToken = "",
    [string]$CollectorNames = "",
    [string]$ManagerToken = "",
    [string]$DashboardToken = "",
    [string]$ManagerPort = "5000",
    [string]$DashboardPort = "5001",
    [System.Management.Automation.PSCredential]$TaskCredential,
    [switch]$ForceNewConfig,
    [switch]$SkipGitPull
)

# Check if running from Anaconda Prompt
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: This script must be run from an Anaconda Prompt" -ForegroundColor Red
    Write-Host "" -ForegroundColor Red
    Write-Host "How to launch:" -ForegroundColor Yellow
    Write-Host "  1. Open Anaconda Prompt (or Anaconda PowerShell Prompt)" -ForegroundColor Yellow
    Write-Host "  2. Run this script:" -ForegroundColor Yellow
    Write-Host "       .\install-manager-dashboard-taskscheduler.ps1" -ForegroundColor Yellow
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
$RepoDir = "$ScriptsDir\lab-monitor"
$CondaEnv = "lab-monitor"
$RepoUrl = "https://github.com/MoffittLab/lab-monitor.git"
$ManagerLauncher = "$RootDir\start-manager.bat"
$DashboardLauncher = "$RootDir\start-dashboard.bat"
$ManagerTaskName = "Lab Monitor Manager"
$DashboardTaskName = "Lab Monitor Dashboard"
$LegacyTaskName = "Lab Monitor Startup"
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Same markers the Manager refuses at startup (manager.py _PLACEHOLDER_MARKERS)
$PlaceholderMarkers = @("CHANGE-ME", "CHANGE_ME", "CHANGEME", "YOUR-", "YOUR_",
                        "GENERATE-RANDOM", "GENERATE_RANDOM", "PLACEHOLDER", "REPLACE-ME", "REPLACE_ME")

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

function New-RandomToken {
    # 32 random bytes from the OS CSPRNG, as 64 hex characters
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Get-TokenProblem {
    # Returns $null for a usable token, else a short description of the problem
    param([string]$Token)
    if ([string]::IsNullOrWhiteSpace($Token)) { return "empty" }
    $upper = $Token.Trim().ToUpperInvariant()
    foreach ($marker in $PlaceholderMarkers) {
        if ($upper.Contains($marker)) { return "placeholder value" }
    }
    if ($Token.Trim().Length -lt 16) { return "shorter than 16 characters" }
    return $null
}

function Write-JsonFile {
    # ASCII, no BOM: Python's json.load rejects the BOM PowerShell 5.1 writes for UTF8
    param([string]$Path, $Object)
    $Object | ConvertTo-Json -Depth 5 | Set-Content -Path $Path -Encoding ASCII
}

function Backup-File {
    param([string]$Path)
    if (Test-Path $Path) {
        $Backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -Path $Path -Destination $Backup
        Write-Host "  Previous file backed up to $Backup"
    }
}

function ConvertTo-OrderedHashtable {
    # Top-level properties of a ConvertFrom-Json object, in file order
    param($Object)
    $Result = [ordered]@{}
    foreach ($Prop in $Object.PSObject.Properties) { $Result[$Prop.Name] = $Prop.Value }
    return $Result
}

function Read-JsonFile {
    # Parsed JSON object, or $null if the file is missing or not valid JSON
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Get-PathKind {
    # "unc" for \\server\share paths, "mapped" for a network drive letter or a
    # drive letter this machine doesn't have (e.g. mapped only in a
    # non-elevated session), "local" otherwise (including relative paths)
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return "local" }
    $P = $Path.Trim()
    if ($P -match '^[\\/]{2}[?.][\\/]') {
        # \\?\C:\...  or  \\?\UNC\server\share
        if ($P -match '^[\\/]{2}[?.][\\/]UNC[\\/]') { return "unc" }
        $P = $P.Substring(4)
    } elseif ($P -match '^[\\/]{2}') {
        return "unc"
    }
    if ($P -match '^([A-Za-z]):') {
        $DeviceId = "$($Matches[1].ToUpperInvariant()):"
        try {
            $Disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID = '$DeviceId'" -ErrorAction Stop
        } catch {
            return "local"   # can't tell; don't block the install
        }
        if (-not $Disk -or $Disk.DriveType -eq 4) { return "mapped" }
    }
    return "local"
}

function Test-TaskCredential {
    # $true / $false if the account's password could be checked, $null if not
    param([System.Management.Automation.PSCredential]$Credential)
    try {
        Add-Type -AssemblyName System.DirectoryServices.AccountManagement -ErrorAction Stop
        $Name = $Credential.UserName
        $Domain = $null
        if ($Name -match '^([^\\]+)\\(.+)$') {
            $Domain = $Matches[1]
            $Name = $Matches[2]
        } elseif ($Name -notmatch '@') {
            return $null   # bare user name: local or domain is ambiguous
        }
        $ContextType = [System.DirectoryServices.AccountManagement.ContextType]
        if ($Domain -and ($Domain -eq "." -or $Domain -ieq $env:COMPUTERNAME)) {
            $Context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext($ContextType::Machine)
        } elseif ($Domain) {
            $Context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext($ContextType::Domain, $Domain)
        } else {
            $Context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext($ContextType::Domain)
        }
        try {
            return $Context.ValidateCredentials($Name, $Credential.GetNetworkCredential().Password)
        } finally {
            $Context.Dispose()
        }
    } catch {
        return $null
    }
}

function Show-CommandOutput {
    param($Output, [int]$Last = 20, [string]$Color = "Gray")
    $Output | Select-Object -Last $Last | ForEach-Object { Write-Host "    $_" -ForegroundColor $Color }
}

# Validate tokens passed on the command line (never accept a placeholder)
if ($DashboardToken -and -not $ReadToken) { $ReadToken = $DashboardToken }
$SuppliedTokens = [ordered]@{ AdminToken = $AdminToken; ReadToken = $ReadToken; ManagerToken = $ManagerToken }
foreach ($ParamName in $SuppliedTokens.Keys) {
    $Value = $SuppliedTokens[$ParamName]
    if ($Value) {
        $Problem = Get-TokenProblem $Value
        if ($Problem) {
            Write-Error-Custom "-$ParamName is not usable ($Problem). Omit it to have a random token generated."
            exit 1
        }
    }
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
    $CondaOut = conda create -y -n $CondaEnv python=3.11 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Conda environment '$CondaEnv' created"
    } else {
        Write-Error-Custom "Could not create conda environment"
        Show-CommandOutput $CondaOut -Color Red
        exit 1
    }
} else {
    Write-Success "Conda environment '$CondaEnv' already exists"
}
Write-Host ""

# Step 4: Clone or update repository
Write-Step "Step 4: Cloning/updating lab-monitor repository"
if (Test-Path "$RepoDir\.git") {
    if ($SkipGitPull) {
        Write-Host "  Skipping git pull (-SkipGitPull)"
    } else {
        Write-Host "  Repository already exists, pulling latest..."
        $GitOut = & git -C $RepoDir pull origin main 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error-Custom "git pull failed (exit code $LASTEXITCODE):"
            Show-CommandOutput $GitOut -Color Red
            Write-Host "  Fix the problem (for local changes see: git -C $RepoDir status) and re-run," -ForegroundColor Yellow
            Write-Host "  or re-run with -SkipGitPull to install the code that is already there." -ForegroundColor Yellow
            exit 1
        }
        Show-CommandOutput $GitOut -Last 5
    }
} else {
    $GitOut = & git clone $RepoUrl $RepoDir 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error-Custom "git clone failed (exit code $LASTEXITCODE):"
        Show-CommandOutput $GitOut -Color Red
        exit 1
    }
}
if (Test-Path "$RepoDir\manager\manager.py") {
    Write-Success "Repository ready"
} else {
    Write-Error-Custom "Repository not found at $RepoDir"
    exit 1
}
Write-Host ""

# Step 5: Install dependencies into the conda environment
Write-Step "Step 5: Installing dependencies"
Write-Host "  Upgrading pip..."
conda run -n $CondaEnv python -m pip install --upgrade pip 2>&1 | Out-Null

Write-Host "  Installing from requirements.txt (Flask, waitress, requests, paramiko)..."
$PipOut = conda run -n $CondaEnv python -m pip install -r "$RepoDir\requirements.txt" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Success "Dependencies installed"
} else {
    Write-Host "[WARNING] pip install failed (exit code $LASTEXITCODE):" -ForegroundColor Yellow
    Show-CommandOutput $PipOut -Color Yellow
}

# Full path of the environment's python.exe, used by the launchers
$PythonExe = (conda run -n $CondaEnv python -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
if ($PythonExe) { $PythonExe = $PythonExe.Trim() }
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Host "[WARNING] Could not resolve the environment's python.exe; launchers will use 'python.exe' from PATH" -ForegroundColor Yellow
    $PythonExe = "python.exe"
} else {
    Write-Host "  Python: $PythonExe"
}
Write-Host ""

# Step 6: Create Manager configuration (existing config is kept)
Write-Step "Step 6: Manager configuration"
$ManagerConfigFile = "$RepoDir\manager\config.json"
$ExistingManager = $null
$CreatedManagerConfig = $false
$GeneratedCollectorTokens = [ordered]@{}

if (Test-Path $ManagerConfigFile) {
    try {
        $ExistingManager = Get-Content $ManagerConfigFile -Raw | ConvertFrom-Json
    } catch {
        if (-not $ForceNewConfig) {
            Write-Error-Custom "Existing $ManagerConfigFile is not valid JSON: $_"
            exit 1
        }
        Write-Host "  [WARNING] Existing $ManagerConfigFile is not valid JSON; a new config is written (the old file is backed up)." -ForegroundColor Yellow
        $ExistingManager = $null
    }
}

if ($ExistingManager -and -not $ForceNewConfig) {
    Write-Success "Keeping existing Manager config (tokens unchanged): $ManagerConfigFile"
    if ($AdminToken -or $ReadToken -or $ManagerToken -or $CollectorNames) {
        Write-Host "  [NOTE] Token parameters were ignored because the config was kept (use -ForceNewConfig to regenerate)." -ForegroundColor Yellow
    }

    # The Manager now refuses to start with placeholder or short tokens
    $ExistingTokens = @()
    foreach ($Key in @("admin_tokens", "read_tokens", "auth_tokens")) {
        if ($ExistingManager.PSObject.Properties.Name -contains $Key) { $ExistingTokens += @($ExistingManager.$Key) }
    }
    if ($ExistingManager.PSObject.Properties.Name -contains "collector_tokens") {
        foreach ($Prop in $ExistingManager.collector_tokens.PSObject.Properties) { $ExistingTokens += [string]$Prop.Value }
    }
    $BadTokens = @($ExistingTokens | Where-Object { Get-TokenProblem ([string]$_) })
    if ($BadTokens.Count -gt 0) {
        Write-Host ""
        Write-Host "  [WARNING] The existing Manager config has $($BadTokens.Count) placeholder or too-short token(s)." -ForegroundColor Red
        Write-Host "            The Manager REFUSES TO START with such tokens. Either edit" -ForegroundColor Red
        Write-Host "            $ManagerConfigFile and replace them with random tokens," -ForegroundColor Red
        Write-Host "            or re-run this script with -ForceNewConfig (new tokens only - data_dir and the" -ForegroundColor Red
        Write-Host "            other settings are kept; collectors then need the new tokens)." -ForegroundColor Red
        Write-Host ""
    }
    $HasScoped = ($ExistingManager.PSObject.Properties.Name -contains "admin_tokens") -or
                 ($ExistingManager.PSObject.Properties.Name -contains "read_tokens") -or
                 ($ExistingManager.PSObject.Properties.Name -contains "collector_tokens")
    if (-not $HasScoped) {
        Write-Host "  [NOTE] This config only has legacy 'auth_tokens' (still accepted, with full admin rights)." -ForegroundColor Yellow
        Write-Host "         Recommended: add 'admin_tokens', 'read_tokens' (Dashboard) and 'collector_tokens'" -ForegroundColor Yellow
        Write-Host "         (one per device) - see manager\README.md." -ForegroundColor Yellow
    }
} else {
    Backup-File $ManagerConfigFile
    if (-not $AdminToken) { $AdminToken = New-RandomToken }
    if (-not $ReadToken) { $ReadToken = New-RandomToken }

    $CollectorTokens = [ordered]@{}
    if ($ManagerToken) {
        $CollectorTokens["*"] = $ManagerToken
        Write-Host "  [NOTE] -ManagerToken stored as a shared collector token for ANY device ('*')." -ForegroundColor Yellow
    }
    foreach ($CollectorName in ($CollectorNames -split ",")) {
        $CollectorName = $CollectorName.Trim()
        if (-not $CollectorName) { continue }
        if ($CollectorName -notmatch '^[A-Za-z0-9_.-]{1,64}$') {
            Write-Error-Custom "Invalid collector name '$CollectorName' (letters, digits, '_', '.', '-'; max 64)"
            exit 1
        }
        $Token = New-RandomToken
        $CollectorTokens[$CollectorName] = $Token
        $GeneratedCollectorTokens[$CollectorName] = $Token
    }

    if ($ExistingManager) {
        # -ForceNewConfig on an existing config: replace ONLY the tokens, so
        # data_dir, log_file, retention and the other settings stay as they were
        $ManagerConfig = ConvertTo-OrderedHashtable $ExistingManager
        $ManagerConfig.Remove("auth_tokens")
        $ManagerConfig["admin_tokens"] = @($AdminToken)
        $ManagerConfig["read_tokens"] = @($ReadToken)
        $ManagerConfig["collector_tokens"] = $CollectorTokens
        if ($PSBoundParameters.ContainsKey("ManagerPort")) { $ManagerConfig["port"] = [int]$ManagerPort }
        Write-JsonFile -Path $ManagerConfigFile -Object $ManagerConfig
        $CreatedManagerConfig = $true
        Write-Success "Manager tokens regenerated in $ManagerConfigFile (random admin + read tokens)"
        Write-Host "  Other settings kept, including data_dir = $($ManagerConfig['data_dir'])"
    } else {
        $ManagerConfig = [ordered]@{
            host = "0.0.0.0"
            port = [int]$ManagerPort
            data_dir = $DataDir
            admin_tokens = @($AdminToken)
            read_tokens = @($ReadToken)
            collector_tokens = $CollectorTokens
            cors_origins = @()
            max_request_bytes = 33554432
            max_messages_per_request = 10000
            history_retention_days = $null
            log_file = "$LogsDir\manager.log"
            log_level = "INFO"
            debug = $false
        }
        Write-JsonFile -Path $ManagerConfigFile -Object $ManagerConfig
        $CreatedManagerConfig = $true
        Write-Success "Manager config created at $ManagerConfigFile (random admin + read tokens)"
    }
}
Write-Host ""

# Step 7: Create Dashboard configuration (existing config is kept)
Write-Step "Step 7: Dashboard configuration"
$DashboardConfigFile = "$RepoDir\dashboard\config.json"
if ((Test-Path $DashboardConfigFile) -and -not $ForceNewConfig) {
    Write-Success "Keeping existing Dashboard config: $DashboardConfigFile"
    if ($CreatedManagerConfig) {
        Write-Host "  [WARNING] A new Manager config was generated but the Dashboard config was kept." -ForegroundColor Yellow
        Write-Host "            Set manager_token in $DashboardConfigFile to the Manager's read token." -ForegroundColor Yellow
    }
} else {
    # Dashboard gets a READ token: it can't ingest or delete if its host is compromised
    $DashToken = $ReadToken
    if (-not $CreatedManagerConfig -and $ExistingManager) {
        $DashToken = $null
        foreach ($Key in @("read_tokens", "admin_tokens", "auth_tokens")) {
            if (-not $DashToken -and ($ExistingManager.PSObject.Properties.Name -contains $Key)) {
                $DashToken = @($ExistingManager.$Key) | Select-Object -First 1
            }
        }
    }
    if (-not $DashToken) {
        Write-Host "  [WARNING] No Manager token found for the Dashboard; set manager_token by hand." -ForegroundColor Yellow
        $DashToken = ""
    }
    $ExistingDashboard = Read-JsonFile $DashboardConfigFile
    Backup-File $DashboardConfigFile
    if ($ExistingDashboard) {
        # -ForceNewConfig: new manager_token only; login, URLs and the rest are kept
        $DashboardConfig = ConvertTo-OrderedHashtable $ExistingDashboard
        $DashboardConfig["manager_token"] = $DashToken
        if ($PSBoundParameters.ContainsKey("DashboardPort")) { $DashboardConfig["port"] = [int]$DashboardPort }
        if ($PSBoundParameters.ContainsKey("ManagerPort")) { $DashboardConfig["manager_url"] = "http://localhost:$ManagerPort" }
        Write-JsonFile -Path $DashboardConfigFile -Object $DashboardConfig
        Write-Success "Dashboard manager_token updated in $DashboardConfigFile (other settings kept)"
    } else {
        $DashboardConfig = [ordered]@{
            host = "0.0.0.0"
            port = [int]$DashboardPort
            manager_url = "http://localhost:$ManagerPort"
            manager_token = $DashToken
            refresh_interval_seconds = 30
            manager_timeout_seconds = 5
            log_file = "$LogsDir\dashboard.log"
            log_level = "INFO"
            debug = $false
        }
        Write-JsonFile -Path $DashboardConfigFile -Object $DashboardConfig
        Write-Success "Dashboard config created at $DashboardConfigFile (uses the Manager read token)"
    }
}
Write-Host ""

# Step 8: Validate config and dependencies (no services are started here)
Write-Step "Step 8: Validating Manager config and Python dependencies"
$CheckOut = conda run -n $CondaEnv python "$RepoDir\manager\manager.py" --config "$ManagerConfigFile" --check-config 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Success "Manager config is valid"
} else {
    Write-Host "[WARNING] Manager config check failed - the Manager will not start until this is fixed:" -ForegroundColor Red
    Show-CommandOutput $CheckOut -Color Red
}
$ImportOut = conda run -n $CondaEnv python -c "import flask, flask_cors, waitress, requests" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Success "Python dependencies import cleanly"
} else {
    Write-Host "[WARNING] Some Python dependencies are missing:" -ForegroundColor Yellow
    Show-CommandOutput $ImportOut -Color Yellow
}

# Task account. S4U (no stored password) has no network access, so data_dir
# or log files on a share need an account whose password Task Scheduler stores.
$ServicePaths = [ordered]@{}
$FinalManager = Read-JsonFile $ManagerConfigFile
$FinalDashboard = Read-JsonFile $DashboardConfigFile
if ($FinalManager) {
    $ServicePaths["Manager data_dir"] = [string]$FinalManager.data_dir
    $ServicePaths["Manager log_file"] = [string]$FinalManager.log_file
}
if ($FinalDashboard) {
    $ServicePaths["Dashboard log_file"] = [string]$FinalDashboard.log_file
}
$UncPaths = @()
$MappedPaths = @()
foreach ($Key in $ServicePaths.Keys) {
    $Kind = Get-PathKind $ServicePaths[$Key]
    if ($Kind -eq "unc") { $UncPaths += "$Key = $($ServicePaths[$Key])" }
    elseif ($Kind -eq "mapped") { $MappedPaths += "$Key = $($ServicePaths[$Key])" }
}
if ($MappedPaths.Count -gt 0) {
    Write-Error-Custom "These paths use a mapped (or missing) drive letter, which scheduled tasks can't see:"
    $MappedPaths | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host "  Replace each one with its UNC path (\\server\share\...; 'net use' lists the mappings)" -ForegroundColor Yellow
    Write-Host "  in the config file, then re-run. Nothing has been stopped or re-registered." -ForegroundColor Yellow
    exit 1
}
$UseStoredPassword = $false
$RunAsUser = $CurrentUser
if ($UncPaths.Count -gt 0) {
    Write-Host "  These paths are on a network share:" -ForegroundColor Yellow
    $UncPaths | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Write-Host "  A task without a stored password (S4U) has no network access, so the tasks run as an" -ForegroundColor Yellow
    Write-Host "  account whose password Task Scheduler stores. That account needs access to the share;" -ForegroundColor Yellow
    Write-Host "  re-run this installer whenever its password changes." -ForegroundColor Yellow
    if (-not $TaskCredential) {
        try {
            $TaskCredential = Get-Credential -UserName $CurrentUser -Message "Account for the Lab Monitor tasks (needs access to the network share)" -ErrorAction Stop
        } catch {
            $TaskCredential = $null
        }
    }
    if (-not $TaskCredential) {
        Write-Error-Custom "No credentials given. The tasks can't reach the share without them."
        Write-Host "  Re-run and enter the password, pass -TaskCredential (Get-Credential), or use a local data_dir." -ForegroundColor Yellow
        Write-Host "  Nothing has been stopped or re-registered." -ForegroundColor Yellow
        exit 1
    }
    $CredentialOk = Test-TaskCredential $TaskCredential
    if ($CredentialOk -eq $false) {
        Write-Host "[WARNING] Windows did not accept the password for $($TaskCredential.UserName)" -ForegroundColor Red
        Write-Host "          (wrong password, or the account is locked or expired)." -ForegroundColor Red
        $Answer = Read-Host "Register the tasks with it anyway? [y/N]"
        if ($Answer -ne 'y' -and $Answer -ne 'Y') {
            Write-Host "  Stopped. Nothing has been stopped or re-registered." -ForegroundColor Yellow
            exit 1
        }
    }
    $UseStoredPassword = $true
    $RunAsUser = $TaskCredential.UserName
    Write-Success "Tasks will run as $RunAsUser with a stored password (network access)"
} else {
    Write-Success "Tasks will run as $RunAsUser via S4U (local paths only, no stored password)"
}
Write-Host ""

# Step 9: Stop services started by an earlier install
Write-Step "Step 9: Stopping previously started Manager/Dashboard (if any)"
foreach ($TaskName in @($LegacyTaskName, $ManagerTaskName, $DashboardTaskName)) {
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Existing -and $Existing.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "  Stopped task '$TaskName'"
    }
}
foreach ($PidName in @("manager.pid", "dashboard.pid")) {
    $PidFile = "$RepoDir\.pids\$PidName"
    if (Test-Path $PidFile) {
        $OldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($OldPid -match '^\d+$') {
            $Proc = Get-CimInstance Win32_Process -Filter "ProcessId = $OldPid" -ErrorAction SilentlyContinue
            if ($Proc -and $Proc.Name -eq "python.exe" -and $Proc.CommandLine -match '(manager|app)\.py') {
                Stop-Process -Id ([int]$OldPid) -Force -ErrorAction SilentlyContinue
                Write-Host "  Stopped python.exe PID $OldPid ($PidName)"
            }
        }
    }
}
Write-Host ""

# Step 10: Create launcher batch files
Write-Step "Step 10: Creating launcher batch files"
# Each launcher runs its service in the foreground and restarts it 10 s after
# it exits.  The Manager's exit code 2 means "config refused" (e.g. a
# placeholder token): retrying can't help, so the launcher stops.
# stdout/stderr (startup errors, tracebacks) go to logs\<service>-console.log;
# the services' own rotating logs are logs\manager.log / logs\dashboard.log.
# (echo lines put the redirection first: "code 2>> file" would redirect stderr.)
$ManagerLauncherContent = @"
@echo off
REM Lab Monitor Manager launcher - run by the "$ManagerTaskName" scheduled task.
REM Restarts the Manager if it exits; stops on exit code 2 (config refused).
setlocal
set LOG=$LogsDir\manager-console.log
cd /d $RepoDir\manager
call conda activate $CondaEnv
:run
>> "%LOG%" echo [%DATE% %TIME%] starting Manager
"$PythonExe" manager.py --config config.json >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
>> "%LOG%" echo [%DATE% %TIME%] Manager exited with code %RC%
if "%RC%"=="2" (
    >> "%LOG%" echo [%DATE% %TIME%] config refused - fix manager\config.json, then start the task again
    exit /b 2
)
ping -n 11 127.0.0.1 > nul
goto run
"@

$DashboardLauncherContent = @"
@echo off
REM Lab Monitor Dashboard launcher - run by the "$DashboardTaskName" scheduled task.
REM Restarts the Dashboard if it exits.
setlocal
set LOG=$LogsDir\dashboard-console.log
cd /d $RepoDir\dashboard
call conda activate $CondaEnv
:run
>> "%LOG%" echo [%DATE% %TIME%] starting Dashboard
"$PythonExe" app.py --config config.json >> "%LOG%" 2>&1
>> "%LOG%" echo [%DATE% %TIME%] Dashboard exited with code %ERRORLEVEL%
ping -n 11 127.0.0.1 > nul
goto run
"@

Set-Content -Path $ManagerLauncher -Value $ManagerLauncherContent -Encoding ASCII
Set-Content -Path $DashboardLauncher -Value $DashboardLauncherContent -Encoding ASCII
Write-Success "Launchers created: $ManagerLauncher, $DashboardLauncher"
Write-Host ""

# Step 11: Register Task Scheduler tasks
Write-Step "Step 11: Registering Windows Task Scheduler tasks"

# The old single task started both services in the background; replace it
if (Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  Removed old task '$LegacyTaskName' (replaced by one task per service)"
}

# S4U = run whether or not the user is logged on, without storing a password
# (no network access). With a stored password (step 8) the task can reach
# a UNC data_dir.
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType S4U -RunLevel Highest

# No execution time limit (the services run forever); if the task itself
# fails, Task Scheduler retries every minute.
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$Trigger = New-ScheduledTaskTrigger -AtStartup

$TaskDefs = @(
    @{ Name = $ManagerTaskName;   Launcher = $ManagerLauncher;   Description = "Lab Monitor Manager (port $ManagerPort)" },
    @{ Name = $DashboardTaskName; Launcher = $DashboardLauncher; Description = "Lab Monitor Dashboard (port $DashboardPort)" }
)
$RegisterFailed = $false
foreach ($Def in $TaskDefs) {
    $Action = New-ScheduledTaskAction -Execute $Def.Launcher
    $TaskArgs = @{
        TaskName    = $Def.Name
        Description = $Def.Description
        Action      = $Action
        Trigger     = $Trigger
        Settings    = $Settings
        Force       = $true
        ErrorAction = "Stop"
    }
    if ($UseStoredPassword) {
        $TaskArgs["User"] = $TaskCredential.UserName
        $TaskArgs["Password"] = $TaskCredential.GetNetworkCredential().Password
        $TaskArgs["RunLevel"] = "Highest"
    } else {
        $TaskArgs["Principal"] = $Principal
    }
    try {
        Register-ScheduledTask @TaskArgs | Out-Null
        Write-Success "Task registered: $($Def.Name)"
    } catch {
        Write-Error-Custom "Failed to register task '$($Def.Name)': $($_.Exception.Message)"
        $RegisterFailed = $true
    }
}
if ($RegisterFailed) {
    Write-Host "  Fix the problem (e.g. a wrong password) and re-run with -SkipGitPull." -ForegroundColor Yellow
}
Write-Host "  Trigger: at system startup (no login required), restart on failure"
if ($UseStoredPassword) {
    Write-Host "  Run as: $RunAsUser (stored password, highest privileges)"
} else {
    Write-Host "  Run as: $RunAsUser (S4U, highest privileges)"
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

# Step 13: Start services now (they also start at every boot)
Write-Step "Step 13: Starting services"
foreach ($TaskName in @($ManagerTaskName, $DashboardTaskName)) {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "  Started task '$TaskName'"
    } catch {
        Write-Host "[WARNING] Could not start task '$TaskName': $_" -ForegroundColor Yellow
    }
}
Write-Host ""

# Step 14: Verify services are running
Write-Step "Step 14: Verifying services"
Start-Sleep -Seconds 8

# Win32_Process exposes CommandLine on Windows PowerShell 5.1 (Get-Process does not)
$PyProcs = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue)
$ManagerProc = $PyProcs | Where-Object { $_.CommandLine -match 'manager\.py' } | Select-Object -First 1
$DashboardProc = $PyProcs | Where-Object { $_.CommandLine -match 'app\.py' } | Select-Object -First 1

if ($ManagerProc) {
    Write-Success "Manager is running (PID: $($ManagerProc.ProcessId))"
} else {
    Write-Host "[WARNING] Manager does not appear to be running - see $LogsDir\manager-console.log" -ForegroundColor Yellow
}
if ($DashboardProc) {
    Write-Success "Dashboard is running (PID: $($DashboardProc.ProcessId))"
} else {
    Write-Host "[WARNING] Dashboard does not appear to be running - see $LogsDir\dashboard-console.log" -ForegroundColor Yellow
}
try {
    $Health = Invoke-WebRequest -Uri "http://localhost:$ManagerPort/health" -UseBasicParsing -TimeoutSec 10
    Write-Success "Manager /health: $($Health.StatusCode)"
} catch {
    Write-Host "[WARNING] Manager /health did not answer yet: $($_.Exception.Message)" -ForegroundColor Yellow
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
Write-Host "  OpenSSH Server (optional - enables remote updates)" -ForegroundColor Yellow
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
Write-Host "1. Launchers: $ManagerLauncher, $DashboardLauncher"
Write-Host "2. Task Scheduler: '$ManagerTaskName' and '$DashboardTaskName' (at startup, restart on failure)"
Write-Host "3. Auto-starts on reboot: YES (no login required; runs as $RunAsUser)"
Write-Host "4. Manager config: $ManagerConfigFile"
Write-Host "   - admin_tokens: for tools that DELETE systems (tools\reset_system.py)"
Write-Host "   - read_tokens:  used by the Dashboard (manager_token in $DashboardConfigFile)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Verify services are running:"
Write-Host "   Get-CimInstance Win32_Process -Filter ""Name = 'python.exe'"" | Where-Object CommandLine -match 'manager|app' | Select-Object ProcessId, CommandLine"
Write-Host ""
Write-Host "2. Access Dashboard:"
Write-Host "   http://atlantis.med.harvard.edu:$DashboardPort"
Write-Host "   (optional login: python app.py --hash-password, then add the 'auth' block - see dashboard\README.md)"
Write-Host ""
Write-Host "3. Check logs:"
Write-Host "   Get-Content '$LogsDir\manager.log' -Tail 20"
Write-Host "   Get-Content '$LogsDir\manager-console.log' -Tail 20     (startup errors)"
Write-Host "   Get-Content '$LogsDir\dashboard.log' -Tail 20"
Write-Host ""
Write-Host "4. Restart a service:"
Write-Host "   Stop-ScheduledTask '$ManagerTaskName'; Start-ScheduledTask '$ManagerTaskName'"
Write-Host ""
Write-Host "5. Deploy collectors on Synology and Windows servers (see collector/README.md)."
Write-Host "   Each collector needs its own token in the Manager's collector_tokens:" -ForegroundColor Yellow
if ($GeneratedCollectorTokens.Count -gt 0) {
    Write-Host "   Generated now (put each one in that collector's config.json as manager_token):" -ForegroundColor Yellow
    foreach ($Name in $GeneratedCollectorTokens.Keys) {
        Write-Host "     $Name : $($GeneratedCollectorTokens[$Name])"
    }
}
Write-Host "   To add a collector later, edit $ManagerConfigFile :"
Write-Host '     "collector_tokens": { "Triton": "<random token>", "Atlas": "<another random token>" }'
Write-Host "   Generate a token with:"
Write-Host "     conda run -n $CondaEnv python -c ""import secrets; print(secrets.token_hex(32))"""
Write-Host "   then restart the Manager (step 4) and set the same token as manager_token on that collector."
Write-Host ""
