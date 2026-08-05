# Lab Monitor Fleet Management Tools

Remote management tools for coordinating collector systems across your lab.

All tools use the same `collectors.csv` file for inventory and authentication.

---

## Prerequisites

```bash
pip install paramiko
```

---

## Inventory: `collectors.csv`

Central registry of all collector systems. Create one in the `tools/` directory:

```csv
ip,username,password,git_path,python_path
192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
192.168.1.43,john,secret456,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python
atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe
```

**Columns:**
- `ip` — hostname or IP of the collector system
- `username` — SSH username
- `password` — SSH password (leave blank to use SSH keys)
- `git_path` — path to lab-monitor repo on the collector (`{git_path}/collector/config.json` must exist)
- `python_path` — path to Python interpreter (used by `run_collectors.py`)

**Security:** Keep `collectors.csv` out of git — it's listed in `.gitignore`.

---

## Tools

### 1. `update_collectors.py` — Deploy Code Updates

**Purpose:** Deploy lab-monitor code changes to all collectors by pulling from git.

**Use Case:** You've pushed changes to the main branch and want to update all collectors without SSHing into each one manually.

**Usage:**
```bash
python3 update_collectors.py                  # pull latest code
python3 update_collectors.py --dry-run        # preview without running
python3 update_collectors.py --csv /path/to/file.csv
python3 update_collectors.py --timeout 60     # custom SSH timeout
```

**What it does:**
- For each collector, SSHes in and runs `cd {git_path} && git pull origin main`
- Shows per-system git output (e.g., files changed, new commits)
- Summary with success/failure counts

**Example workflow:**
```bash
# Make changes, commit, and push to GitHub
git add . && git commit -m "fix: improve network stats" && git push

# Update all collectors
python3 update_collectors.py

# Result:
# [1/10] 192.168.1.42 ... [OK] (2.3s)
#        Already up to date.
# [2/10] 192.168.1.43 ... [OK] (1.9s)
#        Updating e9d5f7a..3c8f2d1
#        Fast-forward
#         collector/collector.py | 5 +++++
```

---

### 2. `toggle_collectors.py` — Pause / Resume Collectors

**Purpose:** Turn all collectors on or off by setting the `active` field in their config.

**Use Case:** 
- Before deploying changes, pause all collectors to prevent data collection during transition
- Resume collectors after testing is complete
- Perform lab maintenance without cluttering the monitoring database
- Audit the full config state on each system

**Usage:**
```bash
python3 toggle_collectors.py --mode off        # pause all
python3 toggle_collectors.py --mode on         # resume all
python3 toggle_collectors.py --mode off --dry-run
python3 toggle_collectors.py --mode off --timeout 30
```

**What it does:**
- For each collector, uses `config_tool.py` to fetch the full config
- Sets `active: false` or `active: true` (adds field if it doesn't exist)
- Displays before/after full config (with auth tokens masked) for audit
- Summary with success/failure counts

**Example workflow:**
```bash
# Prepare for maintenance: pause all data collection
python3 toggle_collectors.py --mode off

# Result shows before/after for each system:
# [1/10] 192.168.1.42 ... [OK] (2.1s)
#
#     Before:
#       name: "Triton"
#       active: true
#       device_type: "NAS"
#       ...
#
#     After:
#       name: "Triton"
#       active: false
#       device_type: "NAS"
#       ...

# Do your maintenance work, then resume
python3 toggle_collectors.py --mode on
```

---

### 3. `run_collectors.py` — Trigger Collection Events

**Purpose:** Execute an immediate collection run (disk or metrics) on all collectors.

**Use Case:**
- Force a disk measurement before/after capacity changes
- Trigger metrics collection on a specific schedule outside the normal interval
- Test that collectors are working by running them manually
- Collect data on-demand for troubleshooting

**Usage:**
```bash
python3 run_collectors.py --mode disk          # run disk collection
python3 run_collectors.py --mode metrics       # run metrics collection
python3 run_collectors.py --mode disk --dry-run
python3 run_collectors.py --mode metrics --timeout 60
```

**What it does:**
- For each collector, SSHes in and runs:
  ```
  cd {git_path} && {python_path} collector/collector.py --config collector/config.json --mode {mode}
  ```
- Captures and displays output (first 10 lines per system)
- Summary with success/failure counts

**Example workflow:**
```bash
# You just added 2TB of data. Get an immediate disk measurement.
python3 run_collectors.py --mode disk

# Result:
# [1/10] 192.168.1.42 ... [OK] (8.5s)
#        === DISK USAGE COLLECTION ===
#        Measuring disk usage for Triton...
#        Archived to /volume1/lab-monitor/data/Triton/2026-08.jsonl
#        Added to queue (1 entries)
#        Posting queue to Manager...

# Now the dashboard will show the updated disk usage
```

---

### 4. `config_tool.py` (in `collector/`) — Manual Config Management

**Purpose:** Read and write individual config fields on any system via SSH.

**Use Case:**
- Test configuration changes on one collector before rolling out to all
- Audit a specific collector's settings
- Add/modify a single field without touching the rest of the config
- Integrate with paramiko-based orchestration scripts

**Usage:**
```bash
cd collector/

# Read a field
python3 config_tool.py --config config.json get active

# Write a field (adds if missing)
python3 config_tool.py --config config.json set active false

# List all fields (tokens masked)
python3 config_tool.py --config config.json list

# JSON output for programmatic use
python3 config_tool.py --config config.json get manager_url --json
python3 config_tool.py --config config.json set active true --json
```

**Exit codes:**
- `0` — success
- `1` — error (missing file, parse failure, etc.)
- `2` — field not found (get only)

**Used by:** `toggle_collectors.py` calls this remotely to manage the `active` flag.

---

## Common Workflows

### Deploy a Code Change and Verify

```bash
# 1. Test locally, then commit and push
git add . && git commit -m "fix: network stats" && git push

# 2. Update all collectors
python3 update_collectors.py

# 3. Trigger a fresh collection to test the changes
python3 run_collectors.py --mode metrics
```

### Perform Lab Maintenance

```bash
# 1. Pause all collectors (they stay on schedule, log that they ran, but collect nothing)
python3 toggle_collectors.py --mode off

# 2. Do your maintenance (reconfigure storage, reboot systems, etc.)
# ... maintenance work ...

# 3. Resume collectors
python3 toggle_collectors.py --mode on

# 4. Force an immediate collection to pick up any changes
python3 run_collectors.py --mode disk
```

### Troubleshoot a Failing Collector

```bash
# 1. Check its config
cd collector/
python3 config_tool.py --config /path/on/remote/config.json list --show-secrets

# 2. Fix it if needed
python3 config_tool.py --config /path/on/remote/config.json set manager_url http://new.server:5000

# 3. Test it
cd ../..
python3 tools/run_collectors.py --mode metrics  # will show collector output on next run
```

### Scale Up: Add a New Collector

```bash
# 1. Deploy code to the new system
# (manual: install miniconda, clone repo, create config.json)

# 2. Add it to collectors.csv
echo "192.168.1.99,operator,password123,/data/lab-monitor,/opt/miniconda/bin/python" >> tools/collectors.csv

# 3. Test it
python3 tools/run_collectors.py --mode disk --csv tools/collectors.csv

# 4. If it works, add it to update rotation
# (next time you run update_collectors.py, it'll get the latest code)
```

---

## SSH Authentication

All tools support two methods:

**Password auth (default):**
```csv
ip,username,password,git_path,python_path
192.168.1.42,jeff,secret123,...,...
```

**SSH key auth:**
Leave the password blank:
```csv
ip,username,,git_path,python_path
192.168.1.42,jeff,,...,...
```

The tools will look for `~/.ssh/id_rsa` (or `~/.ssh/id_ed25519`) and use SSH agent if available.

---

## Tips

- **Always dry-run first:** Most tools support `--dry-run` to preview commands before running them.
- **Use `--timeout` for slow networks:** If you have flaky SSH connections, increase the timeout: `--timeout 90`.
- **Monitor the Summary:** Each tool ends with a summary showing success/failure. Address failures before moving on.
- **Keep `collectors.csv` updated:** When you add or remove a system, update the CSV so all tools can find it.
- **Combine tools:** Update code → pause → run tests → resume. The tools are designed to compose.

---

## Exit Codes

All tools return:
- `0` — success (all operations completed)
- `1` — error (missing file, bad args, etc.)
- `2` — partial failure (some collectors failed, see summary)

This makes it easy to use them in scripts or cron jobs.

---

## Logs

Collection output goes to each collector's logs (e.g., `/volume1/lab-monitor/logs/collector.log`). You can inspect those after running `run_collectors.py` to troubleshoot failures.

---

## Example: Full Fleet Update Cycle

```bash
cd tools/

# Check what's coming
python3 update_collectors.py --dry-run

# Pause all collectors
python3 toggle_collectors.py --mode off

# Pull latest code on all collectors
python3 update_collectors.py

# Resume collectors
python3 toggle_collectors.py --mode on

# Force a fresh collection to test everything
python3 run_collectors.py --mode metrics

# Check the dashboard to confirm data is flowing
```

Done! All collectors are running the latest code.
