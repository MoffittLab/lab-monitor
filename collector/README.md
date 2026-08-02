# Collector

Runs on each Synology NAS or Windows system. Periodically measures disk usage of monitored folders and reports to the central Manager.

## Overview

The collector:
1. **Auto-discovers** folders (one level deep from configured volumes)
2. **Excludes** specified folders (e.g., system directories, cache)
3. **Measures** remaining folders daily
4. **Queues locally** (survives Manager downtime)
5. **Posts to Manager** with authentication
6. **Archives queue** to monthly files on successful handshake (records retained indefinitely)

---

## Installation on Synology NAS

### Step 1: Enable SSH

**Control Panel → Terminal & SNMP:**
- Check "Enable SSH service"
- Note the port (usually 22)

### Step 2: SSH Into NAS

```bash
# SSH as your admin user (not root)
ssh Admin@t1.hms.harvard.edu
# Or if using non-standard port:
ssh -p 22222 Admin@t1.hms.harvard.edu
```

You'll be prompted for your admin password.

### Step 2b: Configure Sudo (For Passwordless Script Execution)

To avoid password prompts during automated collection, configure sudo:

```bash
# SSH into NAS as Admin, then:
sudo visudo
```

Add this line at the end:

```
Admin ALL=(ALL) NOPASSWD: /usr/bin/python3
```

Save and exit (Ctrl+X, then Y, then Enter if using nano).

**Alternative:** If you prefer not to modify sudo config, you can run the collection script directly as Admin if folder permissions allow, or use `sudo` with a password in the Task Scheduler.

### Step 3: Check Python

```bash
# Most Synology systems have Python 3 built-in
python3 --version
```

If not installed, install via **Control Panel → Package Center → search "Python"**.

**Note:** Python 3.8+ is required. Synology typically provides python3 or python3.9, both of which support venv.

### Step 4: Create Directory Structure (via Synology GUI)

Create `/volume1/lab-monitor` shared folder via **Control Panel → Shared Folder**, then create subdirectories:

```bash
# Create subdirectories
sudo mkdir -p /volume1/lab-monitor/scripts
sudo mkdir -p /volume1/lab-monitor/data

# Give proper permissions
sudo chmod 755 /volume1/lab-monitor/scripts
sudo chmod 755 /volume1/lab-monitor/data
```

### Step 4.5: Create Python Virtual Environment

Create an isolated Python environment for the collector. This ensures pip works and dependencies are isolated:

```bash
# Navigate to lab-monitor directory
cd /volume1/lab-monitor

# Create virtual environment (use python3.9 if available, otherwise python3)
python3 -m venv lab-monitor-env

# Activate the environment
source lab-monitor-env/bin/activate

# You should see (lab-monitor-env) in your prompt
# Verify pip works
pip --version

# Deactivate for now (we'll activate again during installation)
deactivate
```

**Note:** The virtual environment is at `/volume1/lab-monitor/lab-monitor-env`. All pip installs and Python runs should happen within this activated environment.

### Step 5: Copy Collector Code

**Option A: Git clone**

```bash
cd /volume1/lab-monitor/scripts
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor/collector
```

**Option B: SCP from your machine**

```bash
# From your machine:
scp -r /path/to/lab-monitor/collector/* Admin@t1.hms.harvard.edu:/volume1/lab-monitor/scripts/
```

### Step 6: Install Python Dependencies

**Activate the virtual environment and install dependencies:**

```bash
# Activate the venv
source /volume1/lab-monitor/lab-monitor-env/bin/activate

# Navigate to collector
cd /volume1/lab-monitor/scripts/lab-monitor/collector

# Install dependencies (pip now installs into the venv, not system-wide)
pip install -r requirements.txt

# Deactivate the venv (we'll activate it in the task scheduler)
deactivate
```

### Step 7: Create Configuration

```bash
# Copy example config to local/ directory (git-safe location)
cd /volume1/lab-monitor/scripts/lab-monitor/collector
cp config.synology.example.json local/config.json

# Edit with your values (no sudo needed for local/)
nano local/config.json
```

The `local/` directory is **ignored by git**, so your config won't be committed or overwritten by updates.

**Minimal config (most common):**

```json
{
  "manager_url": "http://a1.med.harvard.edu:5000",
  "manager_token": "your-secret-token-here"
}
```

**Optional fields:**
- `nas_name` — Auto-discovered from system hostname if omitted
- `nas_id` — Defaults to `nas_name` if omitted
- `volumes` — Auto-discovered from system mounts if omitted (reports on all volumes)
- `queue_path` — Defaults to `/volume1/lab-monitor/data/queue.jsonl` if omitted
- `exclude_folders` — Folders to skip when auto-discovering
- `log_file` — Log file path (optional; logs to console if omitted)
- Other fields have sensible defaults

**Example with custom volumes and exclusions:**

```json
{
  "manager_url": "http://a1.med.harvard.edu:5000",
  "manager_token": "your-secret-token-here",
  "volumes": ["/volume1", "/volume2"],
  "exclude_folders": ["@appstore", "@eadir", ".ds_store", "lost+found"]
}
```

### Step 8: Test the Script

**Activate the venv and run the script:**

```bash
# Activate the venv
source /volume1/lab-monitor/lab-monitor-env/bin/activate

# Navigate to collector
cd /volume1/lab-monitor/scripts/lab-monitor/collector

# Run the script
sudo /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json

# Deactivate when done
deactivate
```

You should see output like:

```
Step 1: Measuring folder sizes...
Measured 8 folder(s)
Enqueuing report...
Checking Manager availability...
Flushing queue to Manager...
Collector completed successfully
```

**Note:** Using the venv Python path directly (`/volume1/lab-monitor/lab-monitor-env/bin/python3`) is more reliable for automated Task Scheduler runs.

### Step 9: Schedule Daily Execution

**Via Synology Control Panel:**

1. **Control Panel → Task Scheduler**
2. **Create → Scheduled Task → Custom Script**
3. **General tab:**
   - Task name: `Lab Monitor Collector`
   - Run with: **Admin** (not root)
   - Enable task: ☑

4. **Schedule tab:**
   - Run on: Daily
   - First run time: 02:00 (2 AM)
   - Every: 1 day

5. **Task Settings tab → User-defined script:**

```bash
cd /volume1/lab-monitor/scripts/lab-monitor/collector && sudo /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json
```

**Key difference:** Use the venv Python path (`/volume1/lab-monitor/lab-monitor-env/bin/python3`) instead of the system Python. This ensures the script uses the dependencies you installed in the venv.

If you configured passwordless sudo (Step 2b), this will run without prompting.

6. Click **OK**

### Step 10: Verify

After first scheduled run:

```bash
# Check logs
tail -f /volume1/lab-monitor/data/lab-monitor-collector.log

# Check queue
ls -lh /volume1/lab-monitor/data/queue.jsonl

# Verify on Manager (from Windows server)
curl http://a1.med.harvard.edu:5000/api/usage/nas/nas-01
```

---

## Installation on Windows

### Step 1: Create Directory Structure

```powershell
mkdir C:\lab-monitor\data
mkdir C:\lab-monitor\logs
mkdir C:\lab-monitor\scripts
```

### Step 2: Copy Collector Code

```powershell
cd C:\lab-monitor\scripts
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor\collector
```

### Step 3: Create Python Virtual Environment

```powershell
# Navigate to lab-monitor directory
cd C:\lab-monitor

# Create virtual environment
python -m venv lab-monitor-env

# Activate the environment
.\lab-monitor-env\Scripts\Activate.ps1

# Verify pip works
pip --version

# Deactivate for now (we'll activate again during installation)
deactivate
```

**Note:** The virtual environment is at `C:\lab-monitor\lab-monitor-env`.

### Step 4: Install Python Dependencies

```powershell
# Activate the venv
.\lab-monitor-env\Scripts\Activate.ps1

# Navigate to collector
cd scripts\lab-monitor\collector

# Install dependencies (pip installs into the venv, not system-wide)
pip install -r requirements.txt

# Deactivate
deactivate
```

### Step 5: Create Configuration

```powershell
# Create local/ directory if it doesn't exist
mkdir local -ErrorAction SilentlyContinue

# Copy example config to local/ directory (git-safe location)
Copy-Item config.windows.example.json local\config.json

# Edit config.json with your editor
```

The `local/` directory is **ignored by git**, so your config won't be committed or overwritten by updates.

**Minimal config example:**

```json
{
  "manager_url": "http://a1.med.harvard.edu:5000",
  "manager_token": "your-secret-token-here"
}
```

Volumes and queue path are auto-discovered/auto-defaulted. All optional fields match the Synology configuration reference below.

### Step 6: Schedule via Task Scheduler

1. **Task Scheduler → Create Basic Task**
2. **Name:** Lab Monitor Collector
3. **Trigger:** Daily at 2:00 AM
4. **Action:**
   - Program: `C:\lab-monitor\lab-monitor-env\Scripts\python.exe`
   - Arguments: `C:\lab-monitor\scripts\lab-monitor\collector\collector.py --config local\config.json`
5. **Finish**

**Key difference:** Use the venv Python path (`C:\lab-monitor\lab-monitor-env\Scripts\python.exe`) instead of the system Python.

---

## Configuration Reference

### Required Fields

| Field | Description | Example |
|-------|-------------|---------|

| `manager_url` | Manager API URL | `"http://a1.med.harvard.edu:5000"` |
| `manager_token` | Authentication token | See [Manager Token](#manager-token) |


### Optional Fields

| Field | Default | Description |
|-------|---------|-------------|
| `nas_name` | System hostname | Display name for this NAS (auto-discovered if omitted) |
| `nas_id` | Same as `nas_name` | Unique identifier (defaults to nas_name if omitted) |
| `volumes` | Auto-discovered | Root volumes to scan; auto-discovered from system mounts if omitted |
| `queue_path` | `/volume1/lab-monitor/data/queue.jsonl` | Local queue file path |
| `archive_dir` | `{queue_path parent}/archive` | Directory for monthly archive files (YYYY-MM.jsonl); created automatically |
| `exclude_folders` | `[]` | Folders to skip when auto-discovering (case-insensitive) |
| `log_file` | stdout only | Log file path |
| `log_level` | `"INFO"` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `timeout_seconds` | `300` | Max time to measure all folders |
| `manager_timeout_seconds` | `5` | Timeout for Manager health check |
| `retry_attempts` | `3` | Retry attempts if POST fails |
| `retry_delay_seconds` | `10` | Delay between retries |

### Manager Token

**What is it?**

The `manager_token` is a **secret authentication token** that proves the collector is authorized to post data to the Manager.

**Where do I get it?**

1. Decide on a strong random string (e.g., `my-super-secret-token-abc123`)
2. Add it to the Manager config on Windows Server:
   ```json
   {
     "auth_tokens": ["my-super-secret-token-abc123", "second-token-if-multiple"]
   }
   ```
3. Use the **same token** in each collector's config:
   ```json
   {
     "manager_token": "my-super-secret-token-abc123"
   }
   ```

**Why?**

- Prevents unauthorized systems from posting fake data to your Manager
- Anyone with the token can post data, so keep it secure
- You can rotate tokens by updating Manager and all collectors simultaneously

**Best Practice:**

- Generate a random token: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- Store in a secure location (password manager, encrypted file)
- Never commit to git with the real token (use `config.json`, not tracked)

---

## Folder Discovery

### Volume Filtering

When `volumes` is omitted from config, the collector **auto-discovers mounted volumes** but **only monitors volumes with names starting with 'volume'** (e.g., `/volume1`, `/volume2`, `/volumeX`).

This prevents accidentally measuring system volumes, backup mounts, or network shares.

**Example on Synology:**
- ✓ `/volume1` — MONITORED (matches 'volume*' pattern)
- ✓ `/volume2` — MONITORED (matches 'volume*' pattern)
- ✗ `/mnt/backup` — IGNORED (doesn't match pattern)
- ✗ `/mnt/nfs` — IGNORED (doesn't match pattern)

You can still override this by explicitly setting `volumes` in config:
```json
{
  "volumes": ["/volume1", "/volume2", "/custom/storage"]
}
```

### How It Works

Given this configuration:

```json
{
  "volumes": ["/volume1", "/volume2"],
  "exclude_folders": ["@appstore", "@eadir", ".ds_store", "lost+found"]
}
```

The collector will:

1. Scan `/volume1` and `/volume2` **one level deep only**
2. Find all folders: `/volume1/shared`, `/volume1/media`, `/volume1/backup`, etc.
3. **Exclude** any matching the exclude list (case-insensitive)
4. Measure remaining folders

**Example:**

```
/volume1/
├── @appstore/        ← EXCLUDED (in exclude list)
├── @eadir/           ← EXCLUDED (in exclude list)
├── shared/           ← MEASURED
├── media/            ← MEASURED
├── backup/           ← MEASURED
└── cache/            ← MEASURED (if not excluded)
```

### Customize Excludes

Common system folders to exclude:

**Synology:**
- `@appstore` — Package Center apps
- `@eadir` — Thumbnails/metadata
- `@tmp` — Temporary files
- `lost+found` — Filesystem recovery
- `.ds_store` — macOS junk

**Windows:**
- `$RECYCLE.BIN` — Recycle bin
- `System Volume Information` — System metadata
- `pagefile.sys` — Virtual memory
- `hiberfil.sys` — Hibernation file

---

## Archive Behavior

### How It Works

When reports are successfully posted to the Manager, the collector **archives them to monthly files** instead of deleting them:

1. **Daily measurement** → Enqueue to `queue.jsonl`
2. **POST to Manager** → On success, append queue contents to `archive/YYYY-MM.jsonl`
3. **Clear queue** → Queue file is truncated for the next day

### Directory Structure

```
/volume1/lab-monitor/data/
├── queue.jsonl              # Current day's working queue (cleared after success)
├── lab-monitor-collector.log
└── archive/
    ├── 2026-07.jsonl       # All July measurements (one JSON per line)
    ├── 2026-08.jsonl       # All August measurements
    └── 2026-09.jsonl       # All September measurements
```

### Monthly Files

Each month file (`YYYY-MM.jsonl`) is:
- **Append-only**: New measurements are added to the end
- **JSONL format**: One complete JSON report per line
- **Queryable**: Each line is valid JSON
- **Retained indefinitely**: No automatic cleanup

Example lines from `2026-08.jsonl`:

```json
{"nas_name": "nas-01", "nas_id": "synology_01", "timestamp": "2026-08-01T02:00:00Z", "folders": [...] }
{"nas_name": "nas-01", "nas_id": "synology_01", "timestamp": "2026-08-02T02:00:00Z", "folders": [...] }
```

### Customizing Archive Location

To store archives in a different location, set `archive_dir` in config:

**Synology example:**
```json
{
  "archive_dir": "/volume1/lab-monitor/data/archive"
}
```

**Windows example:**
```json
{
  "archive_dir": "C:\\lab-monitor\\data\\archive"
}
```

If omitted, archives are stored alongside the queue file in an `archive/` subdirectory.

---

## Troubleshooting

### Script fails: "Manager not reachable"

- Check Manager is running on Windows Server
- Verify `manager_url` is correct and accessible from NAS
- Test: `ping a1.med.harvard.edu`

### Script fails: "Invalid token"

- Verify `manager_token` matches Manager config exactly
- Check for leading/trailing spaces in token
- Verify token is in Manager's `auth_tokens` list

### Queue keeps growing (not flushing)

- Check Manager logs for auth errors
- Verify Manager is receiving POST requests
- Check network connectivity: `curl http://a1.med.harvard.edu:5000/health`

### No folders measured

- Verify volumes exist: `ls -la /volume1`
- Check permissions: Can root read the volumes?
- Verify no typos in `volumes` list

### Permission denied errors

- Ensure script runs with sudo privileges (Task Scheduler should use Admin with sudo configured)
- Verify `/volume1/lab-monitor/data/` is writable by Admin (via sudo)
- If using passwordless sudo, test: `sudo -l` to confirm Admin can run python3 without a password

---

## Notes

- The script is designed to run daily (once per day is typical)
- Offline resilience: If Manager is down, reports stay in queue and retry next run
- **Data retention**: Local monthly archives retain all reports indefinitely (never deleted automatically)
- Logs are appended (grow over time; consider rotation if running for months)
- Archives are one file per calendar month; files grow continuously as new measurements are added
