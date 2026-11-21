# Upgrading an Existing Installation

This guide is for an installation that has been running the previous code (commit `f086e7e`, the
last commit before this upgrade, or older) and has months of data in its Manager databases. It is
written for the owner and for a Claude session asked to check the upgrade on the server against
the real databases (see the [checklist at the end](#for-the-next-claude-session)).

**The rule for the whole procedure: existing data must stay fully usable, and nothing is deleted
or rewritten in place.** The new Manager never removes or edits a stored row, it backs up every
database before changing its schema for the first time, and the verification tool only reads the
live `data_dir`.

Paths below are the installer defaults. Check yours: `data_dir` is in `manager\config.json`.

| What | Default |
|------|---------|
| Repository (Manager, Dashboard, and on atlantis also the collector) | `E:\Users\lab-monitor\scripts\lab-monitor` |
| Manager config | `E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json` |
| Dashboard config | `E:\Users\lab-monitor\scripts\lab-monitor\dashboard\config.json` |
| Manager `data_dir` | `E:\Users\lab-monitor\data` (`metrics.db` + `<system>\data.db`) |
| Logs | `E:\Users\lab-monitor\logs` |
| Old scheduled task | "Lab Monitor Startup" (runs `E:\Users\lab-monitor\start-services.bat`) |
| New scheduled tasks | "Lab Monitor Manager", "Lab Monitor Dashboard" (run `start-manager.bat`, `start-dashboard.bat`) |

## The short version

| Step | Where | What |
|------|-------|------|
| [0](#step-0-back-up) | server | Back up `data_dir`, both config files and the logs; record the running commit; save `/api/devices` and `/api/totals` |
| [1](#step-1-verify-against-your-real-data) | server | Run `tools\verify_upgrade.py` against the real `data_dir`. Go on only after **PASS** (a config the new Manager would refuse is a FAIL: [fix it first](#fix-a-refused-token-without-downtime)) |
| [2](#step-2-make-the-new-code-available) | developer | Merge the upgrade into `main` and push (installers and `update_collectors.py` pull `origin main`) |
| [3](#step-3-upgrade-the-manager-and-dashboard) | server | Re-run `install-manager-dashboard-taskscheduler.ps1` (upgrades Dashboard and Manager together); let the first start migrate |
| [4](#step-4-check-the-upgraded-server) | server | Check `/health`, devices, totals, the `.bak` files and the Dashboard |
| [5](#step-5-upgrade-the-collectors) | each collector | Upgrade collectors, one first, then the rest |
| [6](#step-6-afterwards) | server | Later: move to scoped tokens; delete the `.bak` files once you won't roll back |

Rollout order is **Dashboard, then Manager, then collectors**. The Dashboard and Manager share one
checkout and one installer, so steps 3 and 4 do both. Collectors always come last.
[Rollback](#rollback) is tested and restores the exact pre-upgrade databases.

---

## What changed and why

| Area | Before | Now | Why |
|------|--------|-----|-----|
| Delivery | A collector posted its whole queue; the Manager answered "stored N"; one bad message failed the whole batch; a timed-out request was re-sent and stored twice | Every message has a `header.msg_id`. Batches of at most 100. The Manager answers per message (`stored` / `duplicate` / `rejected` / `error`) and de-duplicates. Rejected messages are kept in a `dead_letter` table | Lost and duplicated history |
| Collector queue | `queue.json` rewritten without a lock; the 5-minute and the daily run could overwrite each other | Locked (`queue.json.lock`), atomic writes, unreadable files set aside as `queue.corrupt-*.json` | Lost messages |
| Unmeasured values | Reported as `0` | Sent and stored as `null`, shown as `—` | A failed reading looked like a real zero |
| Folder history | One SQLite column per folder (limit 2000 columns; two folders differing only in case broke the insert) | Narrow tables: one row per folder per scan | Large shares could not be stored |
| Auth | One shared token with full rights; placeholder tokens accepted | Scoped `admin_tokens`, `read_tokens`, `collector_tokens`; placeholder or short tokens stop the Manager | A leaked Dashboard or collector token could delete systems |
| Network totals | `max(0, current − last)`: traffic since a reboot was lost | A drop with a fresh `uptime_seconds` counts as a reboot and adds the traffic since boot; a drop without a reboot adds nothing | Wrong lifetime totals |
| Servers | Flask development server, SQLite connections shared between threads | waitress, one lock per connection, one transaction per message | Crashes and lost writes under load |
| Dashboard | An outage looked like a healthy page; HTML injection possible; no login | Outage and "degraded" banners, escaping everywhere, optional login, `—` for unmeasured values | Misleading and unsafe page |
| Tools | Any SSH host key accepted; destructive tools ran without asking | Host keys pinned on first use; confirmations; new `replay_archives.py`, `verify_upgrade.py`, `rollback_upgrade.py` | Safety |
| Retention | `retention_days` only trimmed the legacy `usage.jsonl` reports | `history_retention_days`, **off** by default | The old setting never pruned the history databases, so yours hold everything since installation; that stays so |

Component details: [manager/README.md](../manager/README.md) ("Upgrading from the previous
Manager"), [collector/README.md](../collector/README.md) ("Upgrading"),
[dashboard/README.md](../dashboard/README.md) ("Upgrade notes"), [tools/README.md](../tools/README.md).

---

## What happens to your existing data

The first time the new Manager starts, it opens every existing database **before it starts
listening** and brings it to the new schema:

- **Backup first.** Before the first change to a database, it is copied with SQLite's online
  backup API to `<db>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak` next to it (for example
  `data\triton\data.db.pre-upgrade-20261201T140312Z.bak`, `data\metrics.db.pre-upgrade-….bak`). The copy
  is written under a temporary name, flushed to disk and then renamed, so a `.bak` file is always
  complete. Each database gets one copy, taken once; a database that already has the new schema
  gets none. If the newest copy is still current (nothing written since), it is reused instead of
  taking another, and so is a copy that holds the same data (same tables, columns, row counts and
  last row ids). A database that an earlier start already partly migrated (a step failed, see
  below) gets no further copy: its first copy is the one with the pre-upgrade layout, and a copy
  of the partly migrated file would not be. The log line is `Backed up <db> to <bak> (… bytes, …s)
  before migrating it`.
- **If the copy fails** (disk full, permissions), that database is **not** migrated and the log
  says why. Its data stays readable through the API and the Dashboard. New messages for it get
  `error`, so collectors keep them queued. The backup is retried on the next write after 5 minutes,
  and at every start. `"backup_before_migration": false` in `manager\config.json` migrates without
  a copy (not recommended).
- **Per-system `data.db`:**
  - A wide `folder_usage` table is copied, in one transaction, into `folder_usage_msgs` (one row
    per scan) and `folder_usage_items` (one row per folder value). The original table is then
    **renamed** to `folder_usage_legacy` and kept with identical content. It is never dropped; you
    can delete it by hand once you are satisfied.
  - **If that copy fails** (another program holds the database open for writing, a full disk), it
    is rolled back and nothing changes: the wide table keeps serving the old folder history to the
    API and the Dashboard. New folder scans for that system get `error` (collectors keep them)
    until the copy succeeds; it is retried on the next scan after 5 minutes and at every start.
    Other data of that system is stored normally. The log says `folder_usage migration failed`.
  - Every other history table (`system_metrics`, `user_metrics`, `not_specified`, any other
    `data_type`) gets a `msg_id` column with a UNIQUE index. Existing rows keep `msg_id = NULL`
    and are otherwise untouched.
- **Central `metrics.db`:** new table `dead_letter`, new column `device_totals.network_last_updated`
  (seeded from the time the stored counters were read, `device_snapshot.metrics_timestamp`, else
  `last_updated`), `user_metrics_history.msg_id`, and `device_snapshot.metrics_json` if missing.
  Existing values are not changed. Lifetime network totals continue from the stored values.
- **Device names are not migrated.** Nothing is renamed or merged in `metrics.db`. Names are
  matched at runtime through one normalization (lowercase; anything other than `a-z 0-9 _` becomes
  `_`), which is also how the per-system directories were always named.
  - A `metrics.db` can hold two spellings that normalize alike: one device whose collector was
    renamed (`Triton`, then `triton`), or two machines (`lab-a` and `lab_a`). Both rows are kept
    unchanged with their own snapshot and lifetime totals, both stay readable under their own
    names, the global totals count each once, and the Dashboard shows two cards. Data sent under
    one of the registered spellings goes to that row, exactly as the old Manager filed it; data
    under a new spelling goes to the alike row with the same device id (else the one seen most
    recently), and a new spelling from a machine with another device id gets its own row. A
    warning is logged at startup. They
    share one `data\<name>\data.db`, as they always did, so `DELETE /api/system/<one spelling>`
    removes only that spelling's central rows and keeps the shared `data.db` while the other
    spelling is registered.
  - New device names must match `^[A-Za-z0-9_.-]{1,64}$`. A name the old Manager already
    registered outside that pattern (for example `Lab NAS`) keeps working, exactly as registered,
    for ingest and reads (logged once per start), until the device is deleted.
- **Timestamps:** new rows are stored as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Old rows keep their stored
  text, except migrated folder scans, which get that form (same instant; the original text is in
  `folder_usage_legacy` and in the `.bak`).
- **A clock that runs ahead:** a message stamped more than 1 hour ahead of the server's clock (a
  collector with a dead clock battery, or before its clock synced) is stored in the history, but
  does not move the device's last-seen time, card, per-user snapshot or lifetime totals; the log
  says `ahead of this server's clock` (once an hour per device). A stored time that far ahead,
  which the old Manager may have kept from such a message, no longer holds back current messages.
- **Old messages sent again are not stored twice.** A message whose `msg_id` is not stored yet is
  matched against the old rows (which have no `msg_id`) by timestamp (as sent or canonical) and,
  for per-user rows, user name, and comes back as `duplicate`. That covers an old collector's queue,
  an upgraded collector re-sending entries the old one queued (derived `legacy-…` ids),
  `replay_archives.py --include-legacy`, and messages from a collector upgraded before the Manager
  (the old Manager stored them but dropped their ids). One exception, on purpose: a scan the old
  Manager kept only on the device card because its history insert failed (folder names differing
  only in case) is stored when it is re-sent.
- **Leftovers stay readable:** per-user rows of a device deleted with the old
  `DELETE /api/system/<name>` (it never removed them), a `data.db` without a registry row, a registry
  row without a directory, and a table made from a `data_type` the new rules refuse (for example
  `1wire_sensors`).
- **Nothing is deleted.** History retention is off unless you set `history_retention_days`;
  `retention_days` is ignored. `usage.jsonl` files written by the removed `POST /api/usage/report`
  stay where they are.

**Disk space on the `data_dir` drive** for the first start: each `.bak` is a full copy of its
database, and the migrated folder history adds roughly 130 bytes per stored folder value while the
old table is kept. On a test data set the `data_dir` grew from 12.5 MB to 26 MB. Have at least
**1.5 times the current size of the databases** free, more if the folder history is very large
(about 130 bytes × folders per scan × number of daily scans).

**How long the first start takes** depends on the size of the databases and the disk. Step 1
measures it on a copy of your data (`timings_seconds.new_manager_first_start` in the report). Don't
stop the Manager while it migrates.

---

## Step 0: Back up

Do this first, before running anything new. Use an **Anaconda PowerShell Prompt run as
Administrator** on the server. Set these variables in every new window:

```powershell
$Repo    = "E:\Users\lab-monitor\scripts\lab-monitor"
$Data    = (Get-Content "$Repo\manager\config.json" -Raw | ConvertFrom-Json).data_dir
$Upgrade = "F:\lab-monitor-upgrade"     # a folder with room, NOT inside $Data (another disk is best)
$Backup  = "$Upgrade\backup"
$Data                                   # check: the data_dir the Manager uses
```

**0a. Record what runs now** (keep these files; the rollback needs the commit):

```powershell
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
git -C $Repo rev-parse HEAD | Tee-Object -FilePath "$Backup\old-commit.txt"
git -C $Repo status --short | Tee-Object -FilePath "$Backup\old-git-status.txt"
Get-ScheduledTask | Where-Object TaskName -like "Lab Monitor*" |
    Select-Object TaskName, State | Tee-Object -FilePath "$Backup\tasks.txt"
Copy-Item "$Repo\manager\config.json"   "$Backup\manager-config.json"
Copy-Item "$Repo\dashboard\config.json" "$Backup\dashboard-config.json"
robocopy E:\Users\lab-monitor\logs "$Backup\logs" /E /R:2 /W:5 /NP     # the logs too (see below)
```

The logs are copied because the old Manager wrote one ever-growing `manager.log`, and the new one
rotates it (10 MiB × 5). On its first start the new Manager moves a `manager.log` larger than that
to `manager.log.pre-rotation-<stamp>`, which rotation never deletes, but the copy is the safe
record of the months before the upgrade.

`git status` should list nothing, or only untracked files. Local edits to tracked files block the
installer's `git pull`; sort them out before Step 3.

**0b. Save what the old Manager serves now** (used for comparison in Step 4):

```powershell
$Token = (Get-Content "$Repo\dashboard\config.json" -Raw | ConvertFrom-Json).manager_token
$H = @{ Authorization = "Bearer $Token" }
Invoke-RestMethod http://localhost:5000/api/devices -Headers $H | ConvertTo-Json -Depth 8 | Set-Content "$Backup\api-devices.json"
Invoke-RestMethod http://localhost:5000/api/totals  -Headers $H | ConvertTo-Json -Depth 8 | Set-Content "$Backup\api-totals.json"
```

**0c. Copy the `data_dir` with the Manager stopped.** SQLite files (and their `-wal` files) are only
consistent while nothing writes to them. Collectors keep queueing while the Manager is down and
send everything when it is back.

```powershell
# Show the old Manager / Dashboard processes (check that the list holds only those)
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'manager\.py|app\.py' } | Select-Object ProcessId, CommandLine

# Stop them
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'manager\.py|app\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# Copy (robocopy exit codes 0-7 mean success; 8 or more means some files failed: read the log)
robocopy $Data "$Backup\data" /E /COPY:DAT /DCOPY:T /R:2 /W:5 /NP /LOG:"$Backup\robocopy.log"
"robocopy exit code: $LASTEXITCODE"

# Same number of files and bytes?
Get-ChildItem $Data -Recurse -File | Measure-Object -Property Length -Sum | Select-Object Count, Sum
Get-ChildItem "$Backup\data" -Recurse -File | Measure-Object -Property Length -Sum | Select-Object Count, Sum

# Start the old services again
Start-ScheduledTask -TaskName "Lab Monitor Startup"
Start-Sleep -Seconds 10
Invoke-RestMethod http://localhost:5000/health
```

If the server has no "Lab Monitor Startup" task, start the services the way they were started
before (for example `E:\Users\lab-monitor\start-services.bat`).

If atlantis also runs a collector whose `data_dir` is the Manager's `data_dir` (see
[ATLANTIS_DUAL_ROLE.md](ATLANTIS_DUAL_ROLE.md)), its archive files are copied too; that is fine.

---

## Step 1: Verify against your real data

`tools\verify_upgrade.py` runs the OLD Manager and the NEW Manager side by side on two copies of
your `data_dir` and compares everything they serve. **It only reads the live `data_dir`**: every
file there is fingerprinted (size, mtime, sha256) before and after; databases are opened read-only
and copied with SQLite's backup API; the work directory must not be inside the `data_dir`. Full
description: [tools/README.md, section 8](../tools/README.md).

**Needs:** git on PATH (the old code is taken with `git archive`), the Python environment the
Manager uses (no extra packages), and free space in the work directory of about **4.5 times the
size of the databases plus twice the size of the other files** in `data_dir` (it checks this before
it copies anything). It takes roughly 5-10 minutes per GB of `data_dir`.

**1a. Get the new code into a separate folder** (the running Manager keeps its code). The upgrade
branch must have been pushed to GitHub; until it is merged, it is `fix/review-findings`:

```powershell
conda activate lab-monitor
git -C $Repo fetch origin
git -C $Repo worktree add E:\Users\lab-monitor\scripts\lab-monitor-new origin/fix/review-findings
cd E:\Users\lab-monitor\scripts\lab-monitor-new
git log --oneline -1
```

(Without `git worktree`: `git clone --branch fix/review-findings https://github.com/MoffittLab/lab-monitor.git E:\Users\lab-monitor\scripts\lab-monitor-new`.)

**1b. Run it** (the live Manager may keep running):

```powershell
$OldCommit = (Get-Content "$Backup\old-commit.txt").Trim()
New-Item -ItemType Directory -Force -Path "$Upgrade\verify" | Out-Null
python -u tools\verify_upgrade.py `
    --manager-config "$Repo\manager\config.json" `
    --old-ref $OldCommit `
    --work-dir "$Upgrade\verify" `
    --with-dashboard `
    --report "$Upgrade\verify\report.json" | Tee-Object -FilePath "$Upgrade\verify\console.txt"
"exit code: $LASTEXITCODE"
```

- `--old-ref` is the commit that wrote your data. The default is `f086e7e`. If your server ran an
  older commit, the run still works, but it can report differences that come from the older
  code's own behaviour; review those, don't assume they are regressions.
- `--manager-config` also makes the new Manager check your real config (`config` check): a
  placeholder or short token would stop it from starting, so the check FAILs and the result is
  not PASS.

**1c. Read the result.** The console ends with one line per check, the endpoints compared, any
unexpected differences, warnings, and `RESULT: PASS` or `RESULT: FAIL`. Exit code `0` = PASS,
`1` = differences found, `2` = the verification could not run (nothing was compared).

| Check | PASS means |
|-------|-----------|
| `source` | The live `data_dir` is byte-identical before and after. **WARN** if only database files changed while the live Manager was running (it stores collector data; the tool only reads) |
| `copies` | Every database copied and passes `PRAGMA quick_check` |
| `config` | The new Manager accepts your `config.json`. **FAIL** if it would refuse it (it would not start after Step 3) |
| `storage` | After migration, every original table still has every row with identical content; `folder_usage_legacy` equals the old wide table; one narrow row per old folder row and per measured value |
| `endpoints` | Every read endpoint (devices, snapshots, totals, every row of every table over the full time range, every chart series, per-user history) returns the same data from old and new |
| `devices`, `totals` | Every old device is present; lifetime network and disk totals are identical |
| `restart` | A second start of the new Manager migrates nothing and answers identically |
| `ingest` | New messages are stored after the old history; totals continue from the old values; a re-send is a duplicate |
| `legacy-resend` | Stored messages re-sent the way an old collector (no `msg_id`) or a collector upgraded before the Manager (its own `msg_id`) would are duplicates |
| `backups` | Every database the new Manager changed has a `.bak` identical to the original |
| `files` | Other files (`usage.jsonl`, collector archives in a shared `data_dir`) are unchanged |
| `rollback` | `rollback_upgrade.py` on the migrated copy restores every database, and the old Manager answers as before |
| `dashboard` | The new Dashboard shows the same systems, metrics, folders and totals as the old one; charts load |

**PASS** means: no check failed and there is no unexpected difference. A short list of
*intended* differences is normalized away (timestamps compared as instants, new fields such as
`msg_id`, `NaN` equal to `null`, `1234.0` equal to `1234`, "GiB" instead of "GB" for the same number,
bucket-averaged "All Available" charts, and a few more); the report lists each one with its reason
(`intended_differences`) and how often it was applied.

The JSON report holds everything. To print the main parts (works for any report size):

```powershell
python -c "import json,sys; r=json.load(open(sys.argv[1], encoding='utf-8')); print(r['result'], r.get('failed_checks')); [print(c['status'], k, '-', c['summary'][:250]) for k, c in r['checks'].items()]; print('differences:', r.get('differences_total')); print('first start (s):', r.get('timings_seconds', {}).get('new_manager_first_start'))" "$Upgrade\verify\report.json"
```

Other keys: `differences` (each with `endpoint`, `system`, `request`, `what`, `old`, `new`,
`suspected_cause`), `warnings`, `source_changes` (what changed in the live `data_dir`), `sampling`
(which folders were charted), `space`, `new_manager_log`.

**If it is not PASS, don't upgrade.**

- `config` FAIL: the new Manager would refuse your `config.json` (the line says which setting,
  e.g. `auth_tokens[0]`: a placeholder such as `CHANGE-ME-TO-SECURE-TOKEN`, or shorter than 16
  characters). Fix it with the old Manager still running, as in
  [Fix a refused token without downtime](#fix-a-refused-token-without-downtime), then run Step 1
  again.
- Exit code 2: read the error at the end of the console output (not enough space, git missing, a
  Manager that would not start: see `logs\new-manager.log` / `logs\old-manager.log` in the work
  directory with `--keep-work`).
- `source` FAIL with `source_changes` listing only a collector's own files (`<name>\YYYY-MM.jsonl`,
  `queue.json`, `queue.json.lock`, `network_snapshot.json`): a collector on this server writes into
  the same `data_dir` (dual role). The tool did not cause it. For a clean result, pause that
  collector for the run (`Disable-ScheduledTask -TaskName "Lab Monitor - Metrics Collection"` and
  `"Lab Monitor - Disk Collection"`, then `Enable-ScheduledTask` afterwards; that collector's
  5-minute samples are missing for that time), or stop the Manager too for a strict check.
- Any other FAIL or difference: keep `report.json` and `console.txt`, re-run with `--keep-work` to
  keep both copies and the Manager logs, and hand them to whoever maintains the code (or a Claude
  session, see the checklist below). Each difference shows the exact request and the old and new
  answers.

### Fix a refused token without downtime

The new Manager refuses to start if any token in `manager\config.json` is a placeholder or shorter
than 16 characters. Fix that **before** the upgrade, while the old Manager is still running; the
installer would otherwise stop at its config check (Step 3), and `-ForceNewConfig` replaces every
token (all collectors then get HTTP 401 until each one has its new token).

- **A placeholder nobody uses** (for example a leftover `GENERATE-RANDOM-TOKEN-2` from the old
  example config): delete it from the `auth_tokens` list. Nothing else changes.
- **A token collectors or the Dashboard use** (for example the old installer's
  `CHANGE-ME-TO-SECURE-TOKEN`):
  1. Generate a strong token: `python -c "import secrets; print(secrets.token_hex(32))"`.
  2. Add it to `auth_tokens` next to the bad one (`"auth_tokens": ["CHANGE-ME-TO-SECURE-TOKEN",
     "<new token>"]`), set it as `manager_token` in `dashboard\config.json`, and restart the old
     services the way Step 0c does (stop both `python.exe` processes, `manager.py` and `app.py`,
     then `Start-ScheduledTask -TaskName "Lab Monitor Startup"`). Collectors queue the few seconds
     the Manager is down.
  3. Put the new token in `manager_token` of every collector's `collector/local/config.json`. Each
     collector's log must show its next run delivering with no 401.
  4. Remove the bad token from `auth_tokens` and restart the old services again (as in step 2).
  5. Run Step 1 again: `config` must PASS.

When you are done with the verification, remove the work folder (it holds full copies of your
data). The `lab-monitor-new` worktree can stay until Step 3 or be removed with
`git -C $Repo worktree remove E:\Users\lab-monitor\scripts\lab-monitor-new`.

---

## Step 2: Make the new code available

Every installer and `tools/update_collectors.py` run `git pull origin main`. After a PASS, merge
the upgrade into `main` and push it. Until then nothing in the fleet picks it up by accident.

---

## Step 3: Upgrade the Manager and Dashboard

Pick a quiet time: **not between 02:00 and the end of the collectors' disk scans** (the daily disk
runs start at 02:00; on atlantis the collector runs from the same checkout, and its disk run must
not be in progress when its code changes).

Collectors need no change for this step, provided Step 1's `config` check passed: the new Manager
accepts old collectors with their existing token, and anything they re-send is recognized. (If a
collector's token is one the new Manager refuses, fix it first:
[Fix a refused token without downtime](#fix-a-refused-token-without-downtime).)

**3a. Optional: take a fresh `/api/totals` snapshot** right before (Step 0b again, to a new file).

**3b. Update the checkout and run the installer** (Anaconda PowerShell Prompt as Administrator).
The transcript keeps the installer's output; the time it stops the old services (UTC) is also
appended to `E:\Users\lab-monitor\logs\installer-stop-times.txt` (a rollback needs it):

```powershell
Start-Transcript -Path "$Backup\installer-transcript.txt"
cd $Repo
git status --short                    # nothing but untracked files
git fetch origin
git checkout main
git pull origin main
git log --oneline -1                  # the upgrade commit

Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
.\install-manager-dashboard-taskscheduler.ps1 -SkipGitPull
Stop-Transcript
```

What the installer does on an existing installation, in this order (every check comes before
anything that changes the running services):

- **Keeps** `manager\config.json` and `dashboard\config.json` (tokens unchanged; your `data_dir`,
  ports and other settings stay). It warns if a token is a placeholder or shorter than 16
  characters, notes when the config has only legacy `auth_tokens` (still accepted, with full
  rights) and when the Dashboard uses a full-rights token. `-ForceNewConfig` replaces **every**
  token (it removes `auth_tokens` and `collector_tokens` and asks first): all collectors then get
  HTTP 401 until each one has its new token, so don't use it to fix one bad token.
- Checks the paths: S4U (no stored password) for a local `data_dir`; if `data_dir` or a log file
  is a UNC path it asks for the account's password; a mapped drive letter stops the install here,
  before it installs packages or writes a config.
- Installs `requirements.txt` (Flask, waitress, requests, paramiko) into the `lab-monitor` conda
  environment.
- Runs the new `manager.py --check-config` on the config it is about to use (touches no database
  and no log file). **If the new Manager would refuse the config, the installer stops here**: the
  old Manager and Dashboard keep running, no task is changed and no config is written. Fix the
  config ([Fix a refused token without downtime](#fix-a-refused-token-without-downtime)) and re-run
  with `-SkipGitPull` before the next reboot (the checkout already holds the new code, which the
  old startup task would start). `-IgnoreConfigCheck` installs anyway (the Manager then stays down
  until the config is fixed).
- Writes the configs it changed (previous files backed up as `config.json.bak-<date>`).
- Stops the old Manager and Dashboard (scheduled tasks and the processes in the `.pids` files). If
  a `manager.py` process is still running after that, it lists it and offers to stop it; if one
  remains, it stops without registering or starting anything.
- Writes `E:\Users\lab-monitor\start-manager.bat` and `start-dashboard.bat` (restart loop;
  console output to `logs\manager-console.log` and `logs\dashboard-console.log`), registers "Lab
  Monitor Manager" and "Lab Monitor Dashboard" (at startup, whether or not anyone is logged on,
  restart on failure), and only then removes the "Lab Monitor Startup" task. If a registration
  fails, the old task is kept and started again.
- Starts both tasks. **On this first start the Manager backs up and migrates every database before
  it listens**, so "Manager /health did not answer yet" at the end is expected on a large
  `data_dir`, and the installer says so. Don't stop it.

Make sure only one Manager runs (two processes on one `data_dir` must never happen). The new
Manager holds a lock on `data_dir\manager.lock` from before it opens any database, and a second new
Manager on the same `data_dir` exits with code 3 ("Another Manager is already running"). An old
Manager takes no lock, and on Windows a second server can share port 5000 without an error, so
check the process list:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'manager\.py|app\.py' } | Select-Object ProcessId, CommandLine
```

**3c. Follow the first start:**

```powershell
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 40 -Wait
```

Expect `Backed up … before migrating it` for each database, `migrated N folder_usage rows …; old
table kept as 'folder_usage_legacy'`, warnings about legacy `auth_tokens` and any grandfathered or
duplicate device names, then `Listening on 0.0.0.0:5000`. Startup errors (for example a refused
config, exit code 2) are in `logs\manager-console.log`. Press Ctrl+C to stop following the log.

---

## Step 4: Check the upgraded server

```powershell
Invoke-RestMethod http://localhost:5000/health                      # {"status": "ok"}
$Token = (Get-Content "$Repo\dashboard\config.json" -Raw | ConvertFrom-Json).manager_token
$H = @{ Authorization = "Bearer $Token" }
$Dev = Invoke-RestMethod http://localhost:5000/api/devices -Headers $H
$Dev.devices.Count
(Get-Content "$Backup\api-devices.json" -Raw | ConvertFrom-Json).devices.Count   # same count

# Lifetime totals: equal to Step 0b, or a little higher (new data since)
$After  = Invoke-RestMethod http://localhost:5000/api/totals -Headers $H
$Before = Get-Content "$Backup\api-totals.json" -Raw | ConvertFrom-Json
foreach ($p in $Before.devices.PSObject.Properties) {
    $a = $After.devices.($p.Name)
    "{0,-20} in {1,18} -> {2,18}   out {3,18} -> {4,18}" -f $p.Name,
        $p.Value.total_bytes_in, $a.total_bytes_in, $p.Value.total_bytes_out, $a.total_bytes_out
}

# One pre-upgrade backup per migrated database
Get-ChildItem $Data -Recurse -Filter "*.pre-upgrade-*.bak" | Select-Object FullName, Length
```

Then open the Dashboard (`http://<server>:5001`): every system has its card, charts with "All
Available" reach back to the first data, folder lists are complete.

If something is wrong, see [Troubleshooting](#troubleshooting) or [roll back](#rollback).

---

## Step 5: Upgrade the collectors

Only after Steps 3 and 4. Never while a collector's 02:00 disk run is in progress (the old code
finishing that run would delete the whole `queue.json` without the lock, which can drop messages
queued in between; they stay in the archive). Do one collector first, watch it for a few runs,
then the rest.

**Synology / Linux** (from a machine with `tools/collectors.csv`; see
[tools/README.md](../tools/README.md)):

```bash
python3 tools/update_collectors.py --csv tools/collectors.csv --dry-run
python3 tools/update_collectors.py --csv tools/collectors.csv --upgrade-pip
python3 tools/run_collectors.py --csv tools/collectors.csv --mode metrics
```

`--upgrade-pip` installs `collector/requirements.txt`: `requests` must install; `psutil` is
best-effort on Synology/Linux (there is no wheel for armv7l; the collector then uses `/proc`).
Python 3.8 or later is required. Re-running `install-collector-synology.sh` on the NAS also works:
it keeps `config.json` and asks before restricting it to mode 600 (both DSM tasks must then run as
the file's owner or root).

**Windows collectors:** `update_collectors.py` as above (on Windows `--upgrade-pip` is strict), or
re-run `install-collector-windows.ps1` as Administrator. The installer keeps the config, sets the
Disk Collection task's time limit to `total_timeout_seconds` + 1 hour, installs the NVIDIA library
only where a GPU is found, and restricts the install folder (`E:\Users\lab-monitor`) so only SYSTEM
and Administrators can write to it and only they can read `collector\local\`.

**atlantis (Manager and collector on one server):** if its collector runs from the same checkout
(`E:\Users\lab-monitor\scripts\lab-monitor\collector`), Step 3 already upgraded it. Re-running
`install-collector-windows.ps1` is optional. Note that its permission step resets the permissions
of everything under `E:\Users\lab-monitor`, including the Manager's `data` and `logs` (contents are
not changed). The Manager tasks run elevated as an administrator and keep access; if they run under
a stored-password account that is not a local administrator, give that account access again.

**What to look for after a collector's first run:** its log
(`/volume1/lab-monitor/logs/collector.log`, `E:\Users\lab-monitor\logs\collector.log`) shows the
queue draining with no 401/403; entries queued by the old code are sent with `legacy-…` ids and
come back as duplicates if the Manager had them. A psutil host keeps counting loopback traffic
until its next reboot, then stops (so the lifetime totals don't jump).

---

## Step 6: Afterwards

**Move to scoped tokens** when convenient. Keep `auth_tokens` until you are sure you won't roll
back: the old Manager knows only that key.

1. Generate tokens (one admin, one read, one per collector):
   ```powershell
   conda activate lab-monitor
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Add them to `manager\config.json` next to the existing `auth_tokens`:
   ```json
   "admin_tokens":     ["<admin token>"],
   "read_tokens":      ["<read token>"],
   "collector_tokens": {"Triton": "<token>", "Atlas": "<token>"},
   ```
   The `collector_tokens` key is the collector's `name` (compared after normalization, so `Triton`
   matches `triton`). For a grandfathered name such as `Lab NAS`, write `Lab_NAS`.
3. Check and restart the Manager:
   ```powershell
   cd $Repo\manager
   python manager.py --config config.json --check-config
   Stop-ScheduledTask -TaskName "Lab Monitor Manager"
   $MPid = Get-Content "$Repo\.pids\manager.pid"
   Get-CimInstance Win32_Process -Filter "ProcessId = $MPid" | Where-Object CommandLine -match 'manager\.py' |
       ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   # nothing may still run manager.py now (this must list nothing):
   Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object CommandLine -match 'manager\.py'
   Start-ScheduledTask -TaskName "Lab Monitor Manager"
   ```
   (Or `tools/restart_manager_dashboard.py`, see [tools/RESTART_MANAGER.md](../tools/RESTART_MANAGER.md).)
4. Put the read token in `dashboard\config.json` (`manager_token`) and restart the Dashboard the
   same way ("Lab Monitor Dashboard", `.pids\dashboard.pid`, `app\.py`).
5. Put each collector's token in its `collector/local/config.json` (`manager_token`). The next run
   uses it; its log must show no 401/403.
6. When nothing uses the old shared token any more, remove `auth_tokens` and restart the Manager.

**Delete the pre-upgrade backups** (`*.pre-upgrade-*.bak` under `data_dir`) once you are sure you
won't roll back. `DELETE /api/system/<name>` does not remove them. `folder_usage_legacy` can also be
dropped by hand then, but costs only disk space.

**Optional:** turn on the Dashboard login (`python app.py --hash-password` in `dashboard\`, paste
the printed `auth` block into `dashboard\config.json`, restart the Dashboard); set
`"cors_origins": []` (the Dashboard calls the Manager server-side and needs no CORS; old configs
list two origins, which is harmless).

---

## Config changes

**Manager (`manager\config.json`):**

| Key | Notes |
|-----|-------|
| `admin_tokens` | Full access, including `DELETE /api/system/<name>` and `GET /api/dead_letter`. For tools such as `reset_system.py` |
| `read_tokens` | GET endpoints only. The Dashboard's `manager_token` |
| `collector_tokens` | `{"<device name>": "<token>"}`: may only `POST /api/data/queue` for that device. `"*"` = any device (logged as a warning) |
| `auth_tokens` | Legacy: still accepted with full rights; deprecation warning at startup |
| `cors_origins` | Off when empty or missing (the old default allowed `http://localhost:5001`) |
| `history_retention_days` | `null` (default) keeps history forever. A number archives older rows to JSONL daily, then deletes them |
| `retention_days` | Ignored now |
| `backup_before_migration` | `true` (default): pre-upgrade `.bak` copies. Must be `true` or `false` |
| `max_request_bytes`, `max_messages_per_request`, `threads`, `log_max_bytes`, `log_backup_count`, `console_log_level` | New tuning keys with safe defaults ([manager/README.md](../manager/README.md)) |

**The Manager refuses to start (exit code 2)** if any token contains `CHANGE-ME`, `CHANGE_ME`,
`CHANGEME`, `YOUR-`, `YOUR_`, `GENERATE-RANDOM`, `GENERATE_RANDOM`, `PLACEHOLDER`, `REPLACE-ME` or
`REPLACE_ME` (any case), is shorter than 16 characters, or if no token is configured at all. The old
installer's default token `CHANGE-ME-TO-SECURE-TOKEN` is one of those. Check any config with
`python manager.py --config config.json --check-config`.

**Dashboard (`dashboard\config.json`):** `manager_token` should be a read token. New optional keys:
`auth`, `stale_minutes`, `offline_minutes`, `disk_stale_hours`, `chart_max_points`,
`history_max_rows`, `cache_ttl_seconds`, `threads` ([dashboard/README.md](../dashboard/README.md)).

**Collectors (`collector/local/config.json`):** `manager_token` should be the device's
`collector_tokens` entry (a legacy `auth_tokens` token also works). New optional keys:
`total_timeout_seconds`, `max_batch_messages`, `max_queue_messages`
([collector/README.md](../collector/README.md)). `name` must match `^[A-Za-z0-9_.-]{1,64}$` for new
devices.

---

## What operators will notice

**Dashboard**
- `—` where a value was not measured (it used to show `0`).
- Sizes say KiB / MiB / GiB / TiB. The numbers are the same as before (they were already
  1024-based; "GB" was a misnomer).
- New states: amber **Stale** after 15 minutes without metrics, red **Offline** after 120 minutes,
  grey **Unknown** when the Manager's metrics call failed and no earlier copy exists. A red banner
  means the Manager (or the Dashboard server) can't be reached; the last good data stays on screen,
  greyed out. An amber banner means some Manager calls failed.
- Thresholds are the same on cards and the summary strip: CPU/RAM amber at 50 %, red at 75 %
  (the strip used 90 %); volumes amber at 75 % (cards used 70 %), red at 90 %.
- The disk scan age is shown and flagged after 36 hours; folders that timed out or could not be
  scanned are listed as a warning instead of showing 0.
- Long chart ranges are bucket-averaged to 2000 points (the CSV column is then `value_avg` and the
  file name ends in `_avg`).
- Two cards for one device if `metrics.db` holds two spellings of its name (see above).
- `_scan_seconds` no longer appears as a volume.
- Without a login configured, the Dashboard logs a startup warning when it listens on `0.0.0.0`.

**Manager**
- Tasks "Lab Monitor Manager" and "Lab Monitor Dashboard" replace "Lab Monitor Startup".
- Exit code 2 = config refused (the launcher then stops retrying; see `logs\manager-console.log`);
  exit code 1 = `data_dir` could not be opened (the launcher retries every 10 s); exit code 3 =
  another Manager already runs on this `data_dir` (the launcher retries every 60 s).
- The first start takes longer (backup and migration); `.bak` files appear next to the databases.
- `manager.log` now also contains the storage modules' messages; it rotates at 10 MiB × 5. The old,
  unrotated `manager.log`, if larger than that, is kept as `manager.log.pre-rotation-<stamp>`.
  `--check-config` never touches the log file.
- A collector whose clock runs more than an hour ahead is logged (`ahead of this server's clock`);
  its messages are stored but don't move the device card or totals.
- `DELETE /api/system/<name>` removes one device: the central rows of that spelling and, unless
  another registered spelling shares it, `data\<name>\data.db`.
- Startup warnings for legacy `auth_tokens`, grandfathered device names, and two spellings of one
  device. These are informational.
- Rejected messages are kept in `dead_letter` (`GET /api/dead_letter`, admin token).
- An old collector whose queue has grown past `max_request_bytes` (32 MiB) gets 413 on every run;
  the log says what to do (raise the limit temporarily, or upgrade that collector).
- Removed: `POST /api/usage/report` (nothing used it), the stale `install-manager-dashboard.ps1`,
  and the unused `shared/` package. The Dashboard routes `/api/system/<name>/disk_history` and
  `/api/system/<name>/metrics_history` are gone (the page never used them); the Dashboard has a new
  `GET /health`.

**Collectors**
- New files next to `queue.json`: `queue.json.lock` (leave it), and `queue.corrupt-<ts>.json` if a
  queue file was ever unreadable. Logs rotate at 5 MB × 5.
- Folders that time out or fail are left out and listed in `_timed_out` / `_errors`; a configured
  path that doesn't exist is an error. A scan path equal to a volume root is not scanned (the
  volume's own figure is used). Glob roots outside `/volume*` now work. Folder sizes measured
  through the Windows Python fallback jump up to their correct values.
- Bandwidth is `null` on the first run and after a reboot.
- Synology: re-running the installer asks before restricting `config.json` to mode 600.
- Windows: re-running the installer restricts `E:\Users\lab-monitor` (non-admins can no longer
  write there; `collector\local\` becomes unreadable to them), so manual test runs and SSH updates
  need an administrator account.

**Tools**
- The first connection to each host records its SSH key in `tools/.known_hosts`; a changed key is
  refused (`HOST KEY CHANGED`) until you remove that line.
- `run_collectors.py`, `update_collectors.py` and `toggle_collectors.py` exit with code 2 when a
  system fails (they used to exit 0). `--timeout` is now the SSH connect timeout only; remote
  commands have `--command-timeout`.
- `archive_all_collectors.py` asks you to type `ARCHIVE` (`--yes` for scripts), and
  `--also-delete-queues` moves the queue instead of deleting it.
- `reset_system.py` needs an admin token (`--admin-token` or `LAB_MONITOR_ADMIN_TOKEN`) and checks
  it before changing anything.
- `restart_manager_dashboard.py` ends and restarts the scheduled tasks and waits 60 s for both
  services (`--verify-timeout`; on the first start after the upgrade use a larger value, for
  example `--verify-timeout 1800`, because the Manager migrates before it listens). It refuses to
  start the services while a `manager.py` process that the PID file does not name is still
  running.

---

## Rollback

Tested end to end on copies of old data: new Manager, a new message, stop, rollback, old Manager.
Row counts matched a pristine copy and the old Manager's answers were identical. Step 1's
`rollback` check runs the same procedure on a copy of your data.

`tools\rollback_upgrade.py` restores, for `metrics.db` and every `<system>\data.db` that has one,
the newest `*.pre-upgrade-*.bak` that still has the old layout (a newer copy that already has the
new layout is skipped with a warning; such a copy can only come from an earlier build of the new
Manager that copied a partly migrated database). The current database and its `-wal` / `-shm` / `-journal` files
are **renamed** to `<db>.post-upgrade-<stamp>` (+ suffix), never deleted, and the `.bak` stays. If
any backup fails `PRAGMA quick_check`, nothing is changed. It refuses while a Manager answers
`/health` on the configured port. Databases without a backup (systems first seen after the
upgrade) are left as they are and listed.

Run it **before** checking out the old code (the tool exists only in the new code):

```powershell
# 1. Stop both services and keep them stopped; write down when (UTC, for the replay below)
(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss") | Tee-Object -FilePath "$Backup\rollback-stop-utc.txt"
Stop-ScheduledTask -TaskName "Lab Monitor Manager"
Stop-ScheduledTask -TaskName "Lab Monitor Dashboard"
Disable-ScheduledTask -TaskName "Lab Monitor Manager"
Disable-ScheduledTask -TaskName "Lab Monitor Dashboard"
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'manager\.py|app\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 2. See the plan (changes nothing), then do it
cd $Repo
python tools\rollback_upgrade.py --manager-config manager\config.json
python tools\rollback_upgrade.py --manager-config manager\config.json --apply
"exit code: $LASTEXITCODE"      # 0 rolled back, 1 a problem (see message), 2 refused

# 3. Check out the code that ran before (from Step 0)
git -C $Repo checkout (Get-Content "$Backup\old-commit.txt").Trim()

# 4. Tokens: the old Manager only reads "auth_tokens". If you changed the configs, restore them:
#    Copy-Item "$Backup\manager-config.json"   "$Repo\manager\config.json"
#    Copy-Item "$Backup\dashboard-config.json" "$Repo\dashboard\config.json"
#    (the installer's config.json.bak-<date> copies also work)

# 5. Start the services again (the launchers are outside the repository and run whatever is checked out)
Enable-ScheduledTask -TaskName "Lab Monitor Manager"
Enable-ScheduledTask -TaskName "Lab Monitor Dashboard"
Start-ScheduledTask -TaskName "Lab Monitor Manager"
Start-ScheduledTask -TaskName "Lab Monitor Dashboard"
Invoke-RestMethod http://localhost:5000/health
```

After a rollback:

- **Data received after the upgrade** is in the `*.post-upgrade-*` files and in the collectors'
  archives (`YYYY-MM.jsonl`). The old Manager does not see it. To bring it in, replay exactly that
  window from each collector's archive, once the old Manager runs again:

  ```bash
  python3 tools/replay_archives.py --manager-url http://<server>:5000 --token <a token in auth_tokens> \
      --source <copy of that collector's data folder, e.g. /volume1/lab-monitor/data/Triton> \
      --include-legacy --allow-duplicates \
      --since <stop time from installer-stop-times.txt> --until <rollback-stop-utc.txt>
  ```

  - `--since` is the time the installer stopped the old Manager in Step 3: the line of the upgrade
    run in `E:\Users\lab-monitor\logs\installer-stop-times.txt` (also in
    `installer-transcript.txt`, "Stopping at … UTC"). Messages collected while the old Manager was
    down and the new one migrated belong to the window, so don't use the `.bak` time stamp, which
    is later. The restored databases hold nothing from after that point, so the window is safe.
  - `--until` is when the new Manager was stopped for the rollback (`rollback-stop-utc.txt`).
    Later messages are still queued on the collectors and reach the old Manager by themselves.
  - `--include-legacy` is needed because collectors not yet upgraded (Step 5 comes last) write
    their archive lines without `msg_id`; without it nothing from them is sent.
  - `--allow-duplicates` because the old Manager has no de-duplication: run each collector's
    window once.
  - Times are UTC (`YYYY-MM-DDTHH:MM:SS`), compared with the messages' header timestamps.
  - `replay_archives.py` exists only in the new code: run it from a machine that has it (the
    `lab-monitor-new` worktree of Step 1a, a workstation) against a copy of the archives, or on a
    collector that was already upgraded. The token must be one the old Manager accepts
    (`auth_tokens`).
- **Upgraded collectors** keep working against the old Manager, except that it reads their `null`
  network counters as 0 (the lifetime totals then jump once) and it rejects `collector_tokens`
  tokens (401). Keep the rollback short, or roll the collectors back too
  (`git checkout <old commit>` in each collector's checkout).
- Keep the `.bak` files. To undo the rollback, upgrade again (Step 3): the new Manager migrates the
  restored files again, after another backup if the old Manager stored anything since.

To restore by hand instead (all databases together; they belong to one point in time): stop the
Manager, rename `data.db` and its `-wal` / `-shm` files aside, copy the newest
`data.db.pre-upgrade-<…>.bak` that has the old layout (a `folder_usage` table, no
`folder_usage_msgs`) to `data.db`, and the same for `metrics.db` (no `dead_letter` table). The Step 0 copy of the
whole `data_dir` is the last resort.

---

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `/health` doesn't answer right after the upgrade | First start: the Manager backs up and migrates before it listens. Follow `logs\manager.log` ("Backed up", "migrated"). Don't stop it. If `manager.log` shows nothing new, read `logs\manager-console.log` (exit code 2 = config refused, 3 = another Manager runs) |
| Manager stops at once; `manager-console.log` shows exit code 2 | Config refused, usually a placeholder or short token. Run `python manager.py --config config.json --check-config` in `manager\`, fix the token, start the task again |
| Manager restarts every 10 s with "Cannot open data_dir" | `data_dir` unreachable. On a `\\server\share` path the task needs a stored password: re-run the installer (it asks). A mapped drive letter never works for tasks: use the UNC path |
| `manager-console.log`: "Another Manager is already running on data_dir" (exit code 3), retried every 60 s | A Manager started earlier still holds `data_dir` (for example one still migrating after its task was ended). Let it finish, or stop it if it should not run; the task's Manager then takes over by itself. Two Managers must never use one `data_dir` |
| Two `manager.py` processes in the process list | An old Manager is still running next to the new one: it takes no lock, and on Windows both can listen on port 5000 without an error ("Only one usage of each socket address" does not appear). Stop the old one at once (its command line or start time shows which) |
| Log: "needs a migration that is held back" / every message `error` | The pre-upgrade backup failed (startup log says why, e.g. "No space left on device"). Free space or fix permissions; it retries on the next write after 5 minutes and at every start. If it is `metrics.db`, all systems are affected |
| Dashboard: "Manager unreachable", banner shows HTTP 401 | The Dashboard's `manager_token` is not in the Manager config. HTTP 403: it is a collector token; use a read token |
| Collector log: HTTP 401 | Its `manager_token` matches no Manager token (check for spaces) |
| Collector log: HTTP 403 "may not send data for device" | Its token belongs to another `collector_tokens` key; the key must be this collector's `name` |
| Collector keeps getting 413 | An old collector's queue outgrew `max_request_bytes`. Raise it temporarily (for example to 134217728) or upgrade that collector |
| A device shows two cards | Two spellings of its name in `metrics.db` from the old Manager. Both are kept on purpose; data under a registered spelling goes to that row, a new spelling to the most recent. Remove a stale one with `DELETE /api/system/<old spelling>` (admin token) only if you no longer need its card and lifetime totals: that deletes them (the shared history `data.db` stays while the other spelling is registered) |
| A device name with a space (e.g. `Lab NAS`) | Grandfathered: it keeps working. A `collector_tokens` key for it must be `Lab_NAS` |
| Lifetime totals look wrong | See the network rule above. Totals are never recomputed from history; compare with `$Backup\api-totals.json` |
| `restart_manager_dashboard.py` reports "not verified" on the first restart | The Manager is still migrating. Don't run the tool again (it would stop the Manager, and the migration would start over): follow `logs\manager.log` and wait for `/health`. Next time use `--verify-timeout 1800` |
| Device card, "last seen" or totals don't move although history arrives; log: `ahead of this server's clock` | That collector's clock runs ahead (or this server's runs behind). Fix the clock; its messages are stored, and the card moves again with the first message whose time is right |
| Log: `folder_usage migration failed`; folder scans of one system get `error` | Another program held that `data.db` open for writing (a DB browser, a backup tool), or the disk is full. The old folder history is still shown. Close that program or free space; it is retried on the next scan after 5 minutes and at every start |
| Old Dashboard shows a `_scan_seconds` volume | A collector was upgraded before the Dashboard. Upgrade the server (Step 3) |
| SSH tool: `HOST KEY CHANGED` | Expected only after a host was reinstalled; then remove its line from `tools/.known_hosts` |
| `verify_upgrade.py` exit code 2 "not enough free space" | Use a `--work-dir` on a bigger disk (`--ignore-space` only if you know the estimate is too high) |
| A database is damaged ("database disk image is malformed") | Messages for that system get `error` and stay queued. Stop the Manager, move the file aside, restore the `.bak` or the Step 0 copy, start again |

More: [manager/README.md](../manager/README.md#troubleshooting),
[collector/README.md](../collector/README.md#troubleshooting),
[dashboard/README.md](../dashboard/README.md#troubleshooting), [INSTALLATION.md](../INSTALLATION.md).

---

## For the next Claude session

You are on the Windows server and asked to check this upgrade against the **real** databases.

**Never:** write into the live `data_dir`; run the installer, start the new Manager, or run
`rollback_upgrade.py --apply` on the live `data_dir` without the owner's explicit go-ahead; delete
or overwrite `.bak`, `*.post-upgrade-*`, `folder_usage_legacy` or the Step 0 copy; change what is
checked out in the production checkout (`$Repo`) except as Step 3 describes, with the owner's
go-ahead (`git fetch` and the `git worktree add` of Step 1a only add objects and a separate folder;
a separate `git clone` avoids even that); widen the verifier's `INTENDED` list to make a difference
go away.

1. Read this file, [tools/README.md](../tools/README.md) sections 8 and 9, and the "Upgrading"
   section of [manager/README.md](../manager/README.md).
2. Establish the facts and write them down: hostname; `$Repo`; `git -C $Repo rev-parse HEAD` and
   `git status --short`; `data_dir` from `manager\config.json` (local path, UNC, or mapped drive?);
   size of the `data_dir` (`Get-ChildItem $Data -Recurse -File | Measure-Object Length -Sum`) and free
   space on each drive (`Get-PSDrive -PSProvider FileSystem`); the "Lab Monitor*" tasks and their
   state; whether a collector writes into the same `data_dir` (folders with `YYYY-MM.jsonl` next to
   `data.db`); whether git is on PATH and which python the Manager uses (`conda activate
   lab-monitor`; `start-manager.bat` names it on upgraded servers).
3. Confirm Step 0 exists (`$Backup\old-commit.txt`, `$Backup\data` with matching file count and
   size, robocopy exit code below 8). If not, ask the owner before stopping the Manager to make it.
4. Treat tokens as secrets: don't print `manager\config.json`, `dashboard\config.json` or collector
   configs into the transcript or a report. Whether the Manager would accept its tokens is shown by
   the verifier's `config` check (it runs `--check-config`, which touches no data).
5. Run Step 1 exactly as written, with `--old-ref` = the recorded commit and a `--work-dir` on a drive
   with about 4.5 times the database size free. Keep `console.txt` and `report.json`.
6. Read the report: `result`, `failed_checks`, every check's `summary`, `differences_total`, the
   first differences (`endpoint`, `system`, `request`, `old`, `new`, `suspected_cause`), `warnings`,
   `source_changes`, `intended_differences` counts, `timings_seconds.new_manager_first_start`.
7. Classify every non-PASS item:
   - `source` WARN with only `.db` / `-wal` / `-shm` changes while the live Manager runs: expected.
   - `source` FAIL listing only a collector's own files: dual role (see Step 1c); re-run with that
     collector's tasks disabled if a clean PASS is needed.
   - Anything else: reproduce it with `--keep-work` (copy A = old, copy B = new, Manager logs in the
     work folder's `logs\`), find the cause in the code, and report it with the exact request and
     both answers. Do not upgrade.
8. On PASS, report to the owner: PASS, the measured first-start time, the free space on the
   `data_dir` drive versus about 1.5 times the database size, any warnings, and the plan for Steps
   2-5. Wait for their go-ahead.
9. After the owner upgrades (Step 3), run Step 4 and compare with the Step 0 snapshots; check one
   `.bak` per migrated database and that only one `manager.py` process runs.
10. Clean up only what you created: the verification work folder (it holds full copies of the data)
    and, when the owner agrees, the `lab-monitor-new` worktree.
