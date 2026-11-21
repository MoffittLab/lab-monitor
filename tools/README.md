# Lab Monitor Fleet Management Tools

Remote management tools for coordinating collector systems across your lab.

All SSH tools use the same `collectors.csv` file for inventory and authentication,
and the same SSH helper (`_ssh.py`) for host-key checking, authentication and
running remote commands.

---

## Prerequisites

```bash
pip install paramiko requests     # or: pip install -r ../requirements.txt
```

`paramiko` is needed by the SSH tools; `requests` by `reset_system.py` and
`replay_archives.py`.

---

## Inventory: `collectors.csv`

Central registry of all collector systems. Create one in the `tools/` directory
(see `collectors.csv.example`):

```csv
ip,username,password,git_path,python_path,key_filename
192.168.1.42,jeff,secret123,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python,
192.168.1.43,john,secret456,/volume1/lab-monitor/scripts/lab-monitor,/volume1/miniconda/envs/lab-monitor/bin/python,
atlantis.med.harvard.edu,admin,,E:/Users/lab-monitor/scripts/lab-monitor,C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe,~/.ssh/id_ed25519
```

**Columns:**
- `ip` — hostname or IP of the collector system
- `username` — SSH username
- `password` — SSH password (optional; tried after key/agent auth — leave blank to use keys only)
- `git_path` — path to the lab-monitor repo on the collector (`{git_path}/collector/local/config.json` must exist)
- `python_path` — path to the Python interpreter in the collector environment
- `key_filename` — *optional* private key file to offer first
- `port` — *optional* SSH port (default 22)

The optional columns can be added to an existing CSV at any time; CSVs without
them keep working unchanged. A UTF-8 BOM (Excel) is fine.

**Security:** Keep `collectors.csv` out of git — it's listed in `.gitignore`.

---

## SSH host keys and authentication

**Host keys are trusted on first use (TOFU):**
- Keys already in `~/.ssh/known_hosts` are honoured.
- The first time a tool connects to a host, its key fingerprint is printed and appended to
  `tools/.known_hosts` (gitignored).
- If a host later presents a **different** key, the tools refuse to connect and print
  `HOST KEY CHANGED for <host>` — this is what a man-in-the-middle looks like. If the change
  is expected (the NAS or server was reinstalled, the SSH server was replaced), delete that
  host's line from `tools/.known_hosts` (or `~/.ssh/known_hosts`) and run the tool again.

- Lines paramiko can't parse in either file — OpenSSH `@cert-authority` / `@revoked` markers, or a
  line damaged while editing — are skipped with a one-time `WARNING: ignoring N line(s)` instead of
  stopping every connection. A key listed as `@revoked` is never trusted on first use.

Previously the tools accepted any host key silently; on the first run after upgrading,
each host's key is recorded once.

**Authentication order** for each row:
1. `key_filename` (if the column is set; a file that doesn't exist or can't be read is skipped
   with a warning)
2. keys from `ssh-agent` and `~/.ssh/id_*`
3. `password` (if the column is non-empty) — fallback

---

## Common options

| Option | Tools | Meaning |
|--------|-------|---------|
| `--csv PATH` | all SSH tools | inventory file (required except `archive_all_collectors.py`, default `collectors.csv`) |
| `--dry-run` | run, update, toggle, archive | show what would happen; change nothing |
| `--timeout S` | all SSH tools | SSH **connect** timeout |
| `--command-timeout S` | all SSH tools | limit for each remote command (`0` = no limit) |
| `--parallel N` | run, update, toggle, archive | work on up to N systems at once (default `1` = one at a time). Output stays grouped per system, in CSV order. With `--verbose`, the `[v]` lines of concurrent systems interleave. Ctrl-C stops at once: systems not yet started are skipped, finished ones are still reported, and the ones that were mid-command are listed (check those). |
| `--verbose` | run, update, toggle, archive | log SSH connections, commands and responses |

Remote commands are read while they run (stdout and stderr together), so a command that
prints a lot can no longer hang the tool.

---

## Tools

### 0. `archive_all_collectors.py` — Archive & Refresh All Collectors

**Purpose:** Move all `YYYY-MM.jsonl` archive files on every collector into a timestamped
backup folder next to them. Nothing is deleted, and the Manager is not touched.

**Use Case:**
- Start fresh with clean local histories
- Prepare for a major data collection change
- Manually review archived data
- Combine with `reset_system.py` for a full refresh

**Usage:**
```bash
python3 archive_all_collectors.py --csv collectors.csv --dry-run
python3 archive_all_collectors.py --csv collectors.csv                       # asks you to type ARCHIVE
python3 archive_all_collectors.py --csv collectors.csv --yes                 # no prompt (scripts)
python3 archive_all_collectors.py --csv collectors.csv --also-archive-queues
python3 archive_all_collectors.py --csv collectors.csv --timeout 90 --verbose --parallel 4
```

**What it does:**
- Asks you to type `ARCHIVE` before changing anything (skipped with `--yes` or `--dry-run`)
- For each collector, SSHes in and reads its config (`config_tool.py`) for `name` and `data_dir`
- Creates `<data_dir>/<name>/backup_YYYYMMDD_HHMMSS`
- Moves all `YYYY-MM.jsonl` archive files into it
- With `--also-archive-queues`, also **moves** `queue.json` into the same backup folder, so
  messages still waiting to be sent are not sent (they remain in the backup and in the
  archives; send them later with `replay_archives.py --source <backup>/queue.json` if needed)
- Shows backup locations; any failure (unreachable host, failed `cd`/`mkdir`/move, unexpected
  output) is listed under **Failures** and gives exit code 2

`--also-delete-queues` still works as a **deprecated alias** of `--also-archive-queues`: it used
to delete `queue.json`, it now moves it into the backup folder.

On Synology/Linux the remote steps are chained with `&&`, so a failed `cd` stops everything
(nothing is created or moved in the wrong directory). On Windows the steps run as a PowerShell
script that stops at the first error. `data_dir` must be an absolute path in the collector config.

**What it does NOT do:**
- Does NOT delete any file — archives and queue are only moved
- Does NOT wipe Manager databases (unlike `reset_system.py`)
- Does NOT modify any collector configs

**Tip:** pause the collectors first (`toggle_collectors.py --mode off`) so no collection run is
writing to the archive while it is moved.

**Example workflow:**
```bash
# Preview what will happen
python3 archive_all_collectors.py --csv collectors.csv --dry-run

# Archive everything
python3 archive_all_collectors.py --csv collectors.csv
# Type ARCHIVE to continue, or anything else to cancel: ARCHIVE

# Result:
# [1/5] 192.168.1.42 [OK] Triton: Moved 12 archive file(s) to backup_20260806_084530
# [2/5] 192.168.1.43 [OK] Atlas: Moved 11 archive file(s) to backup_20260806_084531
# ...
# Summary: 5/5 collectors archived successfully
```

**Recovery:** the backup folders remain on each collector. Move the files back, or re-send them
with `replay_archives.py` to a Manager that lost that data (the Manager keeps its history when
you archive, so there is normally nothing to replay).

---

### 1. `update_collectors.py` — Deploy Code Updates

**Purpose:** Deploy lab-monitor code changes to all collectors by pulling from git.

**Use Case:** You've pushed changes to the main branch and want to update all collectors
without SSHing into each one manually.

**Usage:**
```bash
python3 update_collectors.py --csv collectors.csv
python3 update_collectors.py --csv collectors.csv --dry-run
python3 update_collectors.py --csv collectors.csv --upgrade-pip
python3 update_collectors.py --csv /path/to/collectors.csv --upgrade-pip --timeout 60 --parallel 4
# A host that also runs the Manager/Dashboard from the same environment:
python3 update_collectors.py --csv collectors.csv --upgrade-pip --requirements requirements.txt
```

**What it does:**
- For each collector, SSHes in and runs `git -C {git_path} pull origin main`
- Only with `--upgrade-pip`: also upgrades pip and runs
  `{python_path} -m pip install -r {git_path}/collector/requirements.txt` — the same
  requirements file the collector installers use (override with `--requirements RELPATH`)
- Stops at the first failing step on a system
- Shows per-system git output (e.g., files changed, new commits)
- Summary with success/failure counts
- Each step is limited by `--command-timeout` (default 600 s)

**Example workflow:**
```bash
# Make changes, commit, and push to GitHub
git add . && git commit -m "fix: improve network stats" && git push

# Update all collectors (git pull only)
python3 update_collectors.py --csv collectors.csv

# Or, update code AND pip requirements
python3 update_collectors.py --csv collectors.csv --upgrade-pip

# Result:
# [1/10] 192.168.1.42  [OK]     (2.3s)
#   [OK]     git pull
#            Already up to date.
# [2/10] 192.168.1.43  [OK]     (1.9s)
#   [OK]     git pull
#            Updating e9d5f7a..3c8f2d1
#            Fast-forward
#             collector/collector.py | 5 +++++
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
python3 toggle_collectors.py --csv collectors.csv --mode off
python3 toggle_collectors.py --csv collectors.csv --mode on
python3 toggle_collectors.py --csv collectors.csv --mode off --dry-run
python3 toggle_collectors.py --csv collectors.csv --mode off --timeout 30 --parallel 4
```

**What it does:**
- For each collector, uses `config_tool.py` to fetch the full config
- Sets `active: false` or `active: true` (adds field if it doesn't exist)
- Displays before/after full config (with auth tokens masked) for audit
- Summary with success/failure counts

**Example workflow:**
```bash
# Prepare for maintenance: pause all data collection
python3 toggle_collectors.py --csv collectors.csv --mode off

# Result shows before/after for each system:
# [1/10] 192.168.1.42 ... [OK] (2.1s)
#
#     Before:
#       active: true
#       device_type: NAS
#       name: Triton
#       ...
#
#     After:
#       active: false
#       device_type: NAS
#       name: Triton
#       ...

# Do your maintenance work, then resume
python3 toggle_collectors.py --csv collectors.csv --mode on
```

---

### 3. `run_collectors.py` — Trigger Collection Events

**Purpose:** Execute an immediate collection run (disk or metrics) on all collectors.

**Use Case:**
- Force a disk measurement before/after capacity changes
- Trigger metrics collection outside the normal interval
- Test that collectors are working by running them manually
- Collect data on-demand for troubleshooting

**Usage:**
```bash
python3 run_collectors.py --csv collectors.csv --mode disk
python3 run_collectors.py --csv collectors.csv --mode metrics
python3 run_collectors.py --csv collectors.csv --mode disk --dry-run
python3 run_collectors.py --csv collectors.csv --mode metrics --timeout 60 --parallel 4
```

**What it does:**
- For each collector, SSHes in and runs (absolute paths, no `cd`):
  ```
  "{python_path}" "{git_path}/collector/collector.py" --config "{git_path}/collector/local/config.json" --mode {mode}
  ```
- Captures and displays output (first 10 lines per system)
- Summary with success/failure counts
- Remote run limit (`--command-timeout`): 600 s for `metrics`; **no limit** for `disk`, because a
  disk scan can legitimately take hours. Pass `--command-timeout` to cap it.

**Example workflow:**
```bash
# You just added 2TB of data. Get an immediate disk measurement.
python3 run_collectors.py --csv collectors.csv --mode disk

# Result:
# [1/10] 192.168.1.42 ... [OK] (8.5s)
#        === DISK USAGE COLLECTION ===
#        Measuring disk usage for Triton...
#        Archived to /volume1/lab-monitor/data/Triton/2026-08.jsonl
#        ...

# Now the dashboard will show the updated disk usage
```

---

### 4. `reset_system.py` — Reset One System (runs on the collector)

**Purpose:** Start one system over: move its local archives and queue into a backup folder and
delete the system's data on the Manager.

**Usage (on the collector machine):**
```bash
python3 reset_system.py --config /path/to/collector/local/config.json --admin-token <ADMIN-TOKEN>
LAB_MONITOR_ADMIN_TOKEN=<ADMIN-TOKEN> python3 reset_system.py --config /path/to/config.json
python3 reset_system.py --config /path/to/config.json --admin-token <ADMIN-TOKEN> --force   # no prompt
```

**What it does:**
1. Checks that the Manager is reachable and accepts the token for admin requests — **before**
   anything is moved. A refused token or an unreachable Manager stops the tool with exit code 1
   and nothing changed.
2. Asks you to type the system name (skipped with `--force`)
3. Moves `YYYY-MM.jsonl` and `queue.json` into `<data_dir>/<name>/backup_YYYYMMDD_HHMMSS/`
4. Sends `DELETE /api/system/<name>` to the Manager (per-system history + central summary rows)
5. Prints how to rebuild the system from the backup: pause the collector, run
   `replay_archives.py ... --include-legacy --source <backup>`, resume it and run it once in
   metrics and disk mode

The DELETE endpoint requires a Manager **admin token** (`admin_tokens` in the Manager config; a
legacy `auth_tokens` token also works). The collector's own `manager_token` is normally a
collector token, which the Manager refuses for DELETE (HTTP 403). Without `--admin-token` /
`LAB_MONITOR_ADMIN_TOKEN` the tool falls back to the config's `manager_token` and says so; the
check in step 1 catches a token without admin rights. (It uses `GET /api/dead_letter`, which is
admin-only; on an older Manager without that route any valid token may delete.)

Exit code 2 means the local backup happened but the Manager wipe failed.

---

### 5. `replay_archives.py` — Send Archives Back to the Manager

**Purpose:** Re-send collector archive files (or a `queue.json`) to the Manager — e.g. to rebuild
a system after `reset_system.py`, or to fill a gap after a Manager outage.

**Usage:**
```bash
# On the collector, rebuilding a system wiped by reset_system.py (it prints this command):
python3 replay_archives.py --config /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json \
    --include-legacy --source /volume1/lab-monitor/data/Triton/backup_20260806_084530

# Count only:
python3 replay_archives.py --config config.json --source data/Triton/2026-08.jsonl --dry-run

# Anywhere, with explicit Manager URL and token:
python3 replay_archives.py --manager-url http://atlantis.med.harvard.edu:5000 --token <TOKEN> --source backup_dir/
```

**How it works:**
- `--source` takes files or directories (repeatable). A directory contributes its `YYYY-MM.jsonl`
  files, oldest month first; `queue.json` is read only when named explicitly (queued messages are
  also in the archives). Files are streamed line by line, so a large backup does not need much
  memory (fine on a NAS); give older sources first, messages are sent in file order.
- Messages are sent unchanged in batches (`--batch-size`, default the config's
  `max_batch_messages` or 100) to `POST /api/data/queue` as `{queue_id, name, id, messages}`,
  per device (the Manager requires each message's device name to match the batch name).
- **Safe to repeat:** the Manager de-duplicates on `header.msg_id`, so already-stored messages come
  back as "duplicate". The tool never adds a `msg_id` itself.
- **Messages without a `msg_id`** (written before the collector's de-duplication upgrade) are
  **skipped by default**, with a count. The Manager's history rows from before *its* upgrade have no
  message id either, so a Manager that still holds them would store them twice. Pass
  `--include-legacy` only for a system wiped by `reset_system.py` (or a Manager that never
  received that period). `--since` / `--until` limit the replay to a time window (header
  timestamps, e.g. `--since 2026-08-04T07:00`).
- **Dashboard card:** the Manager shows the *last stored* message on the device card (metrics, disk,
  "last seen") whatever its timestamp, and its network totals skip messages older than the newest
  it has. Replaying older data into a live system therefore rolls the card back. Pause the
  collector first (`toggle_collectors.py --mode off`), replay, resume it (`--mode on`) and run
  `run_collectors.py --mode metrics` and `--mode disk` to show current values again.
- Transient errors are retried (`--retries`, default 3); a batch the Manager says is too large
  (HTTP 413) is split in half.
- Before sending, it checks that the Manager reports per-message results. An **old Manager without
  de-duplication** would store duplicates, so the tool stops unless you pass `--allow-duplicates`
  (reasonable right after `reset_system.py` wiped that system).
- Token: a collector token works for that collector's own device; use an admin token to replay
  other systems.
- Exit code 0 = everything stored or already present; 2 = some messages not delivered (re-run).
  Messages the Manager rejects as permanently invalid are reported (the Manager keeps them in its
  `dead_letter` table).

---

### 6. `restart_manager_dashboard.py` — Restart Manager + Dashboard

Stops and restarts the Manager and Dashboard on the Windows server, then verifies both are really
up. See [RESTART_MANAGER.md](RESTART_MANAGER.md).

---

### 7. `config_tool.py` (in `collector/`) — Manual Config Management

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
python3 config_tool.py --config local/config.json get active

# Write a field (adds if missing)
python3 config_tool.py --config local/config.json set active false

# List all fields (tokens masked)
python3 config_tool.py --config local/config.json list

# JSON output for programmatic use
python3 config_tool.py --config local/config.json get manager_url --json
python3 config_tool.py --config local/config.json set active true --json
```

**Exit codes:**
- `0` — success
- `1` — error (missing file, parse failure, etc.)
- `2` — field not found (get only)

**Used by:** `toggle_collectors.py` and `archive_all_collectors.py` call this remotely.

---

## Common Workflows

### Refresh Collectors & Manager (Full Reset)

Archive all collector history, then optionally reset the Manager to start fresh.

```bash
# 1. Pause collection
python3 toggle_collectors.py --csv collectors.csv --mode off

# 2. Preview, then archive all collectors (moves YYYY-MM.jsonl files to backup folders)
python3 archive_all_collectors.py --csv collectors.csv --dry-run
python3 archive_all_collectors.py --csv collectors.csv
#    (add --also-archive-queues to also move pending queue.json files aside)

# 3. Optionally wipe a system on the Manager: run reset_system.py on that collector
#    with an admin token (see above)

# 4. Resume; collectors start fresh on their next scheduled run
python3 toggle_collectors.py --csv collectors.csv --mode on
```

**Backup folders are saved on each collector** for recovery. Data is never deleted — only moved.

### Deploy a Code Change and Verify

```bash
# 1. Test locally, then commit and push
git add . && git commit -m "fix: network stats" && git push

# 2. Update all collectors
python3 update_collectors.py --csv collectors.csv

# 3. Trigger a fresh collection to test the changes
python3 run_collectors.py --csv collectors.csv --mode metrics
```

### Perform Lab Maintenance

```bash
# 1. Pause all collectors (they stay on schedule, log that they ran, but collect nothing)
python3 toggle_collectors.py --csv collectors.csv --mode off

# 2. Do your maintenance (reconfigure storage, reboot systems, etc.)
# ... maintenance work ...

# 3. Resume collectors
python3 toggle_collectors.py --csv collectors.csv --mode on

# 4. Force an immediate collection to pick up any changes
python3 run_collectors.py --csv collectors.csv --mode disk
```

### Troubleshoot a Failing Collector

```bash
# 1. Check its config (on the collector)
cd collector/
python3 config_tool.py --config local/config.json list --show-secrets

# 2. Fix it if needed
python3 config_tool.py --config local/config.json set manager_url http://new.server:5000

# 3. Test it (from tools/)
python3 run_collectors.py --csv collectors.csv --mode metrics --verbose
```

### Scale Up: Add a New Collector

```bash
# 1. Deploy code to the new system (see INSTALLATION.md)

# 2. Add it to collectors.csv
echo "192.168.1.99,operator,password123,/data/lab-monitor,/opt/miniconda/bin/python" >> collectors.csv

# 3. Test it (the first connection records its SSH host key in tools/.known_hosts)
python3 run_collectors.py --csv collectors.csv --mode disk

# 4. From now on update_collectors.py keeps it on the latest code
```

---

## Tips

- **Always dry-run first:** Most tools support `--dry-run` to preview commands before running them.
- **Use `--timeout` for slow networks:** If SSH connections are slow to establish, increase it: `--timeout 90`.
- **Use `--parallel` for big fleets:** `--parallel 4` works on four systems at once.
- **Monitor the Summary:** Each tool ends with a summary showing success/failure. Address failures before moving on.
- **Keep `collectors.csv` updated:** When you add or remove a system, update the CSV so all tools can find it.
- **Combine tools:** Update code → pause → run tests → resume. The tools are designed to compose.

---

## Exit Codes

| Tool | 0 | 1 | 2 |
|------|---|---|---|
| `run_collectors.py`, `update_collectors.py`, `toggle_collectors.py` | all systems OK | bad args / CSV | one or more systems failed |
| `archive_all_collectors.py` | all archived | bad args / CSV, or confirmation declined | one or more collectors failed |
| `restart_manager_dashboard.py` | restarted and verified | CSV / SSH connection error | a stop, start or verification step failed |
| `reset_system.py` | reset complete | bad config, token refused or Manager unreachable (nothing changed), confirmation declined | local backup done but Manager wipe failed |
| `replay_archives.py` | all stored or already present | bad args, nothing to send, token refused | some messages not delivered |

`run_collectors.py`, `update_collectors.py` and `toggle_collectors.py` used to exit 0 even when a
system failed; scripts that relied on that should check for 2.

---

## Logs

Collection output goes to each collector's logs (e.g., `/volume1/lab-monitor/logs/collector.log`).
You can inspect those after running `run_collectors.py` to troubleshoot failures.

---

## Example: Full Fleet Update Cycle

```bash
cd tools/

# Check what's coming
python3 update_collectors.py --csv collectors.csv --dry-run

# Pause all collectors
python3 toggle_collectors.py --csv collectors.csv --mode off

# Pull latest code on all collectors
python3 update_collectors.py --csv collectors.csv

# Resume collectors
python3 toggle_collectors.py --csv collectors.csv --mode on

# Force a fresh collection to test everything
python3 run_collectors.py --csv collectors.csv --mode metrics

# Check the dashboard to confirm data is flowing
```

Done! All collectors are running the latest code.
