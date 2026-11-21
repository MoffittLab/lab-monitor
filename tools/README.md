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
3. Moves `YYYY-MM.jsonl` and `queue.json` into `<data_dir>/<name>/backup_YYYYMMDD_HHMMSS/`, and with them
   `sessions_state.json` and `storage_health_state.json` when present (they record what the Manager was
   last sent, so after the wipe the next run sends a full session list and a storage-health message).
   `perf_snapshot.json` and `storage_breakdown_state.json` stay: they describe the host.
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
  back as "duplicate". The tool never adds a `msg_id` itself. History rows the previous Manager
  stored have no `msg_id`; the upgraded Manager matches every message whose id it doesn't hold
  against them by timestamp (plus the user name for per-user rows). So messages a collector
  delivered to the previous Manager also come back as "duplicate", including those sent by a
  collector upgraded before the Manager (the previous Manager dropped their ids).
- **Messages without a `msg_id`** (written before the collector's de-duplication upgrade) are
  **skipped by default**, with a count. `--include-legacy` sends them too; that is safe on a live
  system as well, because the same timestamp match recognizes the ones the Manager already holds.
  `--since` / `--until` limit the replay to a time window (header timestamps, e.g.
  `--since 2026-08-04T07:00`).
- **Dashboard card:** only a message at least as new as what the Manager holds updates the device
  card (metrics, disk, "last seen") and the disk total, so replaying older data adds history
  without rolling the card back. The lifetime network totals skip messages older than the newest
  network reading. To rebuild a system's totals after `reset_system.py`, pause the collector first
  (`toggle_collectors.py --mode off`), replay, then resume it (`--mode on`).
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
up. It does not start the services while a `manager.py` process that the PID file does not name
is still running (two Managers must never share a `data_dir`). See
[RESTART_MANAGER.md](RESTART_MANAGER.md).

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

# Nested keys of the usage-metrics settings, JSON values, and a check of every validated value
python3 config_tool.py --config local/config.json set sessions.detail users
python3 config_tool.py --config local/config.json set disk_io_exclude '["sdz"]'
python3 config_tool.py --config local/config.json check
```

- `get` and `set` take `block.key` for nested blocks (`sessions`, `storage_health`, `storage_breakdown`); a
  missing or null block is created.
- Values: `true` / `false`, numbers, and anything starting with `[`, `{`, `"` or `null` is parsed as JSON.
- `set` of a value the collector would reject stores the default instead and prints `WARNING: Invalid ...`
  (a `warning` field with `--json`); for `ports`, `extra_ports` and `exclude_clients` only the invalid
  entries are dropped. An unknown key inside a block is stored with a warning.
- `check` lists invalid values, non-object blocks and unknown block keys.
- `--json` goes after the subcommand (`set field value --json`, `check --json`).

**Exit codes:**
- `0` — success
- `1` — error (missing file, parse failure, etc.)
- `2` — field not found (get only)
- `3` — `check` found a problem: invalid values, a non-object block, or an unknown key inside a block

**Used by:** `toggle_collectors.py` and `archive_all_collectors.py` call this remotely.

---

### 8. `verify_upgrade.py` — Prove Existing Data Survives the Upgrade (run on the Manager server)

**Purpose:** before production switches to new Manager code, check that everything the old
Manager stored (every history row, folder history, per-user history, lifetime network totals,
device registry and snapshots) is still readable, unchanged, through the new Manager API and
Dashboard. It runs the OLD Manager and the NEW Manager side by side on two copies of your real
`data_dir` and compares them. **The live `data_dir` is only read, never written.**

**Where to run it:** on the server that holds the databases (e.g. `E:\Users\lab-monitor\data`),
with the same Python environment as the Manager and git installed. Run it from a **separate
checkout of the new code**, so the running Manager keeps its code:

```bat
cd E:\Users\lab-monitor\scripts\lab-monitor
git fetch
git worktree add ..\lab-monitor-new origin/fix/review-findings
cd ..\lab-monitor-new

REM same python.exe the Manager task uses (see start-manager.bat)
python tools\verify_upgrade.py --manager-config ..\lab-monitor\manager\config.json ^
       --work-dir F:\verify --with-dashboard --report F:\verify\report.json
```

The OLD code comes from `git archive f086e7e` (the last commit before the upgrade; `--old-ref` to
change it, or `--old-code DIR` with an extracted copy). The NEW code is the checkout the tool is
in (`--new-code DIR` to test another one). Both are copied into the work directory, so PID files
and logs never touch the installed copies.

**Options:**

| Option | Meaning |
|--------|---------|
| `--manager-config PATH` | the live Manager's `config.json`: `data_dir` is read from it, and the NEW Manager's `--check-config` is run on a copy of it (`config` FAILs if the new Manager would refuse to start, e.g. a placeholder token: see docs/UPGRADE.md, "Fix a refused token without downtime") |
| `--data-dir PATH` | the live `data_dir` (instead of, or overriding, the config) |
| `--work-dir PATH` | where the two copies, the code and the logs go (a new subdirectory is created; default: the temp dir). Needs about **4x the data_dir size** free (two copies, the new Manager's backups, the rollback check's restored databases); checked before anything is copied. Must not be inside the data_dir. |
| `--report PATH` | JSON report (all checks, every difference with the request, old and new value) |
| `--keep-work` | keep the copies and Manager logs afterwards (for inspection) |
| `--with-dashboard` | also start the OLD and NEW Dashboards and compare `/api/data` and charts |
| `--max-folders N` | folder keys per system charted through `/api/history` (default 200; `-1` = all). Volumes and totals are always included, and every folder value of every row is compared anyway through `/api/data`. The sample is listed in the report. |
| `--window-cells N` | size of the time windows used to compare full histories (rows x columns, default 500000) |
| `--dashboard-max-points N` | `max_points` of the "All Available" chart checks (default 2000, the Dashboard's `chart_max_points`); a series with more points is checked as a bucket-averaged chart |
| `--old-history-windows` | also ask the OLD `/api/history` per field and window (slow on big data; by default the OLD answer is computed from the OLD Manager's rows of the same window, which is the same query) |
| `--startup-timeout`, `--request-timeout` | seconds to wait for `/health` (300) and per request (1800) |
| `--rollup-timeout` | seconds to wait, after the NEW Manager's first start, for its rollup backfill to finish before anything is compared (3600; `/api/rollups/status`). A long window is answered from raw rows while the backfill is pending and from hourly rollups afterwards, so comparing earlier could make the `restart` check report answers that only changed because the backfill finished. Past the timeout the run goes on with a warning. The wait is in the report as `timings_seconds.rollup_backfill`. |
| `--no-hash`, `--no-quick-check` | faster: skip per-table content hashes / `PRAGMA quick_check` |

**How it keeps the live data safe:**
- Every file in the `data_dir` is fingerprinted (size, mtime, sha256) before and after the run.
- Databases are copied with SQLite's online backup API from a read-only source: opened
  `immutable` when no process has them open; when a `-wal` file exists (the live Manager is
  running, or was killed) the database and its `-wal` are first copied to a private staging
  directory while their size/mtime stay stable; only if they keep changing is the live file read
  through a read-only SQLite connection. Other files (`usage.jsonl`, ...) are copied with shutil.
- Copy A (OLD Manager) and copy B (NEW Manager, which migrates it on its first start exactly as
  production will be migrated) are made from the same snapshot.
- While the live Manager keeps running, its database files change during the run; that is
  reported as `WARN source` (the tool only reads them). For a strict byte-identical check, stop
  the "Lab Monitor Manager" task while it runs; collectors keep queueing in the meantime.

**What it checks** (each line of the summary is PASS / WARN / FAIL):

| Check | Meaning |
|-------|---------|
| `source` | the live data_dir is unchanged |
| `copies` | every database copied and passes `PRAGMA quick_check` |
| `config` | the NEW Manager accepts your config (FAIL if it would refuse it: the upgraded Manager would not start) |
| `storage` | in copy B after migration: every original table and row is still there (row counts and content hashes); the old wide `folder_usage` table is kept as `folder_usage_legacy` with identical content; `folder_usage_msgs` has one row per original folder row and `folder_usage_items` one per measured folder value; a wide `storage_breakdown` table (a usage-metrics collector reported to the old Manager) is kept as `storage_breakdown_legacy` with identical content |
| `endpoints` | every read endpoint the Dashboard and tools use, OLD vs NEW, for every system: `/api/devices`, `/api/systems`, `/api/metrics/all`, `/api/usage/all`, `/api/totals`, `/api/user_metrics/*`, `/api/data/*` (every row of every table over the full time range), `/api/usage/*`, `/api/history/*` for every field and the sampled folders (tail query, full range, and the Dashboard's own "All Available" query) |
| `devices` | every old device is in the new `/api/devices`, `/api/metrics/all`, `/api/usage/all`, `/api/totals` |
| `totals` | lifetime network totals (and disk totals) identical per device, via the API and in the database |
| `restart` | the NEW Manager restarted on the migrated copy: nothing migrated twice, every answer identical |
| `ingest` | one new-format metrics + folder message per device: totals continue from the old values, the new point appears after the old history, a re-send is a duplicate. Two old spellings of one name (`Atlantis` / `ATLANTIS`): the message is sent under the most recently seen one, and the other rows must stay exactly as they were (data under a registered spelling goes to that row) |
| `legacy-resend` | already-stored messages re-sent the way an OLD collector would (no `msg_id`), and again the way a collector upgraded before the Manager would (its own random `msg_id`, which the old Manager dropped), are acknowledged as duplicates, not stored twice. A scan the old Manager kept only in the device card (its history insert failed, e.g. a folder whose name differs only in case from another) is stored now |
| `backups` | every database the NEW Manager altered has a `*.pre-upgrade-*.bak` copy with exactly the original content (a backup from an earlier attempt that the new Manager reused counts only if it holds the original) |
| `files` | non-database files (`usage.jsonl`, ...) are byte-identical after the run |
| `rollback` | `rollback_upgrade.py --apply` on the migrated copy B (after the new messages above): every database holds its original content again, and the OLD Manager started on it gives the same answers it gave before the upgrade |
| `dashboard` | (`--with-dashboard`) the NEW Dashboard shows the same systems, metrics, folders and totals as the OLD one, and charts load; a chart longer than `--dashboard-max-points` must cover the whole old time range within one bucket, with every average inside the old values' range |

Systems are taken from the device registry **and** from the `data_dir`: a `data/<name>/data.db`
without a registry row (the old `DELETE /api/system/<name>` could leave one when Windows kept the
file open), a registry row without a directory, and per-user rows of a deleted device are all
compared too (listed under "Warnings").

**Comparing against the review-fix release.** The tool was written for OLD-format originals. Run with
`--old-ref fb6498c` on a `data_dir` that is already in the review-fix layout, the `backups`, `rollback` and
`legacy-resend` checks and some record counts report failures that come from that assumption, not from
lost data; use such a run for the timings and the other checks only
([docs/FIELD_CHECKS.md](../docs/FIELD_CHECKS.md#29-first-start-on-the-current-data)).

Only a documented list of **intended** differences is normalized away (timestamps compared as
instants, fields the new Manager adds such as `msg_id`, NaN as null, `1234.0` = `1234`,
unmeasured folder keys of the old wide table, generic error bodies, endpoints the old Manager
could not serve, ...; N21: data a usage-metrics collector sent to the old Manager, accepted only where it
equals what is stored). N17 (bucket-averaged "All Available" charts) allows one point of drift at
either end; for an answer from rollups one point is its `bucket_seconds`, capped at what the
Manager's planner can produce for the series' time span, so an answer that claims wider points
can't hide a chart cut short. The full list with the reason for each is `INTENDED` at the top of
`verify_upgrade.py` and in the report under `intended_differences`, with how often each was
applied. Anything else is reported under "Unexpected differences", grouped by endpoint, with the
request, the old and the new value, and a likely cause.

**Exit codes:** `0` PASS (no unexpected differences), `1` differences found (see the summary and
the report), `2` the verification could not run (bad arguments, not enough space, a Manager that
would not start; nothing was compared). On FAIL, re-run with `--keep-work` to inspect copy B and
`logs\new-manager.log` in the work directory.

**Run time:** roughly 5-10 minutes per GB of `data_dir` (one pass over every row on each Manager,
plus the per-field chart queries); a progress line is printed per system.

**After a PASS:** back up the `data_dir`, then upgrade in the order the Dashboard README gives
(Dashboard, then Manager, then collectors). The new Manager keeps its own
`*.pre-upgrade-*.bak` copies next to each database; keep them until you are sure you won't roll
back (see `rollback_upgrade.py`), then delete them.

---

### 9. `rollback_upgrade.py` — Put the Pre-upgrade Databases Back (run on the Manager server)

**Purpose:** go back to the old Manager after the upgrade. The new Manager copied every database
it migrated to `<db>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak` first; this restores those copies so the
OLD code serves exactly the data it had. Standard library only; nothing is deleted.

**Not for going back from the usage-metrics release to the review-fix release (`fb6498c`).** That needs
no rollback: start the review-fix code on the same `data_dir` ([docs/UPGRADE.md](../docs/UPGRADE.md#going-back-from-the-usage-metrics-release-to-current)).
This tool restores the layout the OLD (`f086e7e`) code reads; after OLD → review-fix → usage-metrics that
means the copies taken before the review-fix upgrade, and everything stored since would be moved aside to
`*.post-upgrade-*` files.

```bat
REM 1. stop the Manager (and Dashboard): Task Scheduler -> End "Lab Monitor Manager", "Lab Monitor Dashboard"
REM 2. see the plan (changes nothing)
python tools\rollback_upgrade.py --manager-config manager\config.json
REM 3. do it
python tools\rollback_upgrade.py --manager-config manager\config.json --apply
```

For `metrics.db` and every `<system>\data.db` that has a backup: the newest backup that still has
the old Manager's layout is chosen (a newer one that already has the new layout, a copy of a partly
migrated database an earlier build of the new Manager could take, is skipped with a warning; if
no backup has the old layout, the newest is used with a warning), checked
(`PRAGMA quick_check`; if any fails, nothing is changed), copied next to the database, the current
database and its `-wal` / `-shm` / `-journal` files are **renamed** to
`<db>.post-upgrade-<stamp>` (+ `-wal` ...), and the copy becomes `<db>`. The `.bak` files stay.
Databases without a backup (systems first seen after the upgrade) are left as they are and listed.
A database that can't be renamed (still open) is left unchanged and the tool stops with exit 1;
run it again once the cause is fixed.

| Option | Meaning |
|--------|---------|
| `--manager-config PATH` | the Manager `config.json` (`data_dir`, and `port` for the "is a Manager running?" check) |
| `--data-dir PATH` | the `data_dir` instead |
| `--apply` | perform the rollback (without it: dry run) |
| `--use-oldest` | restore the oldest backup of each database instead of the newest old-layout one |
| `--force` | skip the check that no Manager answers `/health` on the configured port |

Then start the OLD code: check out the commit you ran before (`git checkout f086e7e` in the
installation, or whatever `git rev-parse HEAD` showed before upgrading) and start the two tasks
again (the launchers `start-manager.bat` / `start-dashboard.bat` live outside the repository and
start whichever code is checked out). The old Manager only accepts tokens listed in
`auth_tokens`: restore `manager\config.json` and `dashboard\config.json` from the
`config.json.bak-<date>` copies the installer made, or add the Dashboard's `manager_token` (and
any upgraded collector's token) to `auth_tokens`. Messages received after the upgrade are in the
`*.post-upgrade-*` files and in the collectors' archives.

**Exit codes:** `0` rolled back (or the dry-run plan is ready), `1` a problem (bad backup, a file
that could not be renamed), `2` refused (a Manager is running, bad arguments, no backups found).
`verify_upgrade.py` runs this tool on its migrated copy (check `rollback`), so a PASS there means
the rollback works on your data.

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
| `verify_upgrade.py` | PASS: no unexpected differences | differences found | could not run (bad args, space, a Manager did not start) |
| `rollback_upgrade.py` | rolled back / plan ready (dry run) | bad backup or a file could not be renamed | refused: Manager running, bad args, no backups |

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
