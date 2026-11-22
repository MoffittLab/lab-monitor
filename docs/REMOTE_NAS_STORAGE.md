# Remote NAS Storage Configuration

> **Superseded.** Don't put the Manager's `data_dir` on a NAS share. SQLite (in WAL mode above all)
> is not reliable over SMB; in practice it produced thousands of failed transactions a day
> (`cannot commit - no transaction is active`, `disk I/O error`) and, through collector retries,
> duplicate rows. Keep `data_dir` on a local disk (`E:\Users\lab-monitor\manager-data`) and let the
> Manager copy it to the NAS every night: [manager/README.md, "Snapshots to a
> NAS"](../manager/README.md#snapshots-to-a-nas). To move an existing NAS `data_dir`:
> ["Moving data_dir off the NAS"](../manager/README.md#moving-data_dir-off-the-nas). The rest of
> this page is kept for installs that still run this way.

## Overview

You can store the Manager's databases on a NAS share (for example
`\\triton1.hms.harvard.edu\lab-monitor-data`) instead of the local atlantis drive, so they are
covered by the NAS's snapshots and backups. This page explains what that requires.

**The default: a local `data_dir` of its own** (`E:\Users\lab-monitor\manager-data`, not the local
collector's `E:\Users\lab-monitor\data`), copied to the NAS every night by the Manager's
[snapshots](../manager/README.md#snapshots-to-a-nas). The NAS then holds consistent copies, and
its own snapshots and replication cover them. Don't use a share for the live databases (see the
banner above).

---

## Key Insight: Independent Data Directories

**Manager and Collector have independent `data_dir` settings.**

- **Collector's `data_dir`**: local archive (`YYYY-MM.jsonl`), queue (`queue.json` +
  `queue.json.lock`), `network_snapshot.json` and, with the usage-metrics release, the small state
  files `perf_snapshot.json` (+ `.lock`), `sessions_state.json`, `storage_health_state.json` and
  `storage_breakdown_state.json`
- **Manager's `data_dir`**: central database (`metrics.db`) and per-system history
  (`<system>\data.db`), plus the `*.pre-upgrade-*.bak` copies after an upgrade

```
Collector → writes archive + queue to its own data_dir
         ↓
         → POSTs the queue to the Manager (HTTP)
         ↓
Manager  → stores to its own data_dir
```

The Collector never reads the Manager's storage and the Manager never reads the Collector's queue.

---

## Configuration Options

### Option B: Collector Local, Manager on the Share (RECOMMENDED if you use a share)

**Pros:**
- The Manager history (the data that matters) lives on the NAS and is snapshotted with it
- The collector's queue and archive stay local and fast, and keep working if the NAS is down

**Cons:**
- The collector archives on atlantis are not on the NAS (they are a local safety net)
- Every Manager write goes over the network (see [SMB considerations](#smbnetwork-considerations))

**Manager** (`E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json`):
```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  "admin_tokens": ["<random admin token>"],
  "read_tokens": ["<random read token>"],
  "collector_tokens": {"atlantis": "<random token>"},
  "log_file": "E:\\Users\\lab-monitor\\logs\\manager.log"
}
```

**Collector on atlantis** (`E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`):
```json
{
  "name": "atlantis",
  "id": "windows-atlantis",
  "manager_url": "http://localhost:5000",
  "manager_token": "<atlantis's collector token>",
  "data_dir": "E:\\Users\\lab-monitor\\data"
}
```

**Local (`E:\Users\lab-monitor\data`):**
```
E:\Users\lab-monitor\data\
└── atlantis\
    ├── 2026-08.jsonl            ← Collector: archive (local)
    ├── queue.json               ← Collector: queue (local)
    └── queue.json.lock
```

**Remote (`\\triton1.hms.harvard.edu\lab-monitor-data`):**
```
\\triton1.hms.harvard.edu\lab-monitor-data\
├── metrics.db                   ← Manager: central summary
├── atlantis\
│   └── data.db                  ← Manager: full history
├── triton\
│   └── data.db
└── other-system\
    └── data.db
```

### Option A: Collector data_dir on the Share too (not recommended)

Putting a collector's `data_dir` on a share is not recommended:
- The queue relies on file locking, which some network file systems don't support; the collector
  then logs an error on every run and uses the queue unlocked (see
  [collector/README.md](../collector/README.md#local-storage)).
- The Windows collector tasks usually run as SYSTEM, which reaches a share as the computer account
  (`DOMAIN\ATLANTIS$`), not as you.
- If the NAS is unreachable, the collector can neither archive nor queue.

Keep collector `data_dir`s local; their archives can be copied to the NAS separately if wanted.

---

## Network Path Format

### UNC Path (the only supported form)
```json
"data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data"
```
- `\\\\` at the start (each backslash doubled in JSON), then the host name, the share and an optional
  subfolder.

**A UNC `data_dir` needs a task with a stored password.** The installer registers the Manager and
Dashboard tasks with S4U (no stored password) by default, and an S4U task has **no network access**.
When `data_dir` or a log file is a UNC path, `install-manager-dashboard-taskscheduler.ps1` asks for the
account's password (or takes `-TaskCredential (Get-Credential)`) and registers the tasks with it. The
account needs read/write access to the share. Re-run the installer whenever that password changes,
or the tasks stop starting.

### Mapped Drive Letter (not supported)
```json
"data_dir": "Z:\\lab-monitor-data"
```
Scheduled tasks don't see drive mappings, so the Manager would not find `Z:`. The installer stops
with an error (before changing anything) when `data_dir` or a log file uses a mapped or missing
drive letter. Use the UNC path instead.

---

## SMB/Network Considerations

The Manager opens its SQLite databases in WAL mode. SQLite's documentation warns that WAL needs all
processes using a database to be on the same machine and that network file systems can break
SQLite's locking. That is what happened on this lab's server, even with exactly one Manager: failed
transactions and `disk I/O error` (see the banner). Move such an install to a local disk
([Migration from Remote back to Local](#migration-from-remote-back-to-local)). Until then:

- Never point two Managers (or a Manager and a copy job that writes) at the same files.
- Network latency makes every write slower. The Manager commits up to `ingest_commit_messages` (100)
  messages of a request together, one COMMIT (and sync) per database, rather than two per message.
- If the share is briefly unreachable, writes fail as `error` and the collectors keep those messages
  queued; they are stored on a later run.
- If the share is unreachable at startup, the Manager exits with "Cannot open data_dir …" and the
  launcher retries every 10 seconds.

---

## Backup & Snapshot Strategy

**Local `data_dir` (the default):** the Manager's nightly [snapshots](../manager/README.md#snapshots-to-a-nas)
copy every database consistently to `snapshot.dest_dir` on the NAS while it keeps running, verify each
copy and prune old ones. By hand instead, a consistent copy needs the Manager stopped for the copy
(collectors queue meanwhile):

```powershell
Stop-ScheduledTask -TaskName "Lab Monitor Manager"
# make sure the python process is gone (see INSTALLATION.md, "Restart a service")
robocopy E:\Users\lab-monitor\manager-data \\triton1.hms.harvard.edu\lab-monitor-backup\data /MIR /COPY:DAT /R:2 /W:5
Start-ScheduledTask -TaskName "Lab Monitor Manager"
```

**Option B (Manager on the share, not recommended):** NAS snapshots capture `metrics.db` and every
`data.db`. A snapshot that captures a database and its `-wal` file at the same instant is
crash-consistent: SQLite recovers it on open, as after a power cut.

(`/MIR` deletes files in the destination that no longer exist in the source; use a dated folder and
`/E` instead if you want to keep every copy.)

The collector archives (`YYYY-MM.jsonl` on each collector) are a second, independent record: a lost
Manager history can be rebuilt from them with `tools/replay_archives.py`.

---

## Setup Steps (switching to a share)

Not recommended (see the banner); kept for reference. For a NAS copy of the databases, turn on
[snapshots](../manager/README.md#snapshots-to-a-nas) instead.

1. **Create the share** on the NAS (e.g. `lab-monitor-data`) with read/write for the account the
   Manager tasks will run as.
2. **Stop the Manager and Dashboard** and make sure the processes are gone.
3. **Copy the existing data** (keeps all history):
   ```powershell
   robocopy <the current data_dir> \\triton1.hms.harvard.edu\lab-monitor-data /E /COPY:DAT /R:2 /W:5
   ```
   This also copies atlantis's own collector files (`YYYY-MM.jsonl`, `queue.json`); the copies on
   the share are simply not used, and the collector keeps writing locally (Option B). Keep the local
   copy until the Manager runs from the share.
4. **Update `data_dir`** in `manager\config.json` to the UNC path.
5. **Re-run the installer** so the tasks get a stored password:
   ```powershell
   cd E:\Users\lab-monitor\scripts\lab-monitor
   .\install-manager-dashboard-taskscheduler.ps1 -SkipGitPull
   ```
   It keeps both configs, asks for the password, registers and starts the tasks.
6. **Verify:**
   ```powershell
   Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 20
   # "Central metrics DB: \\triton1.hms.harvard.edu\lab-monitor-data\metrics.db"
   Invoke-RestMethod http://localhost:5000/health
   ```
   and check that the Dashboard shows the full history.

---

## Troubleshooting

### Manager restarts every 10 s; `manager-console.log` says "Cannot open data_dir"
The task account can't reach the share. An S4U task has no network access: re-run the installer so
it registers the tasks with a stored password. Also check the account's permissions on the share and
that the password hasn't changed.

### "Permission denied" or "Access is denied"
The task account lacks write permission on the share. Grant it.

### "Network path not found"
UNC path typo, or the share doesn't exist:
```powershell
Test-Path "\\triton1.hms.harvard.edu\lab-monitor-data"
```

### Installer stops: "mapped (or missing) drive letter"
Replace the drive letter with the UNC path (`net use` lists the mappings) and re-run.

### Collector POSTs time out
The Manager waits for the share on every write. Raise `request_timeout_seconds` in the collector
config (default 30); the messages stay queued until they are acknowledged.

### "database is locked"
More than one process is using the databases. Make sure only one Manager runs:
```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object CommandLine -match 'manager\.py' | Select-Object ProcessId, CommandLine
```

---

## Migration from Remote back to Local

Follow [manager/README.md, "Moving `data_dir` off the NAS"](../manager/README.md#moving-data_dir-off-the-nas)
(during the review-fix upgrade: [UPGRADE.md, Step 3b](UPGRADE.md#step-3-upgrade-the-manager-and-dashboard)):

- Copy the Manager's files only (`metrics.db*`, `<system>\data.db*` with their `.bak` copies,
  `<system>\archive\`, `<name>\usage.jsonl`), with the Manager stopped, to
  `E:\Users\lab-monitor\manager-data`. **Not** to `E:\Users\lab-monitor\data`: that is the local
  collector's folder, and the Manager warns at every start when its databases share it. The share's
  copies of collector files (`YYYY-MM.jsonl`, `queue.json*`) are not needed.
- Set `data_dir` to that folder and add the `snapshot` block with `dest_dir` on the NAS (e.g. a
  `snapshots` folder next to the old data).
- Re-run the installer. With an enabled UNC `snapshot.dest_dir` it keeps a stored password for the
  tasks (an S4U task could not reach the share); only with snapshots off does it go back to S4U.
- Keep the share's `data_dir` untouched until a few days of the local one, and a snapshot, have been
  checked (`GET /api/snapshot`, `snapshot.py verify`); then remove it by hand.

---

## See Also

- [ATLANTIS_DUAL_ROLE.md](ATLANTIS_DUAL_ROLE.md) — Manager and collector on one server
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design
- [UPGRADE.md](UPGRADE.md) — Upgrading an existing installation
- [CONFIG.md](../CONFIG.md) — Configuration and security
- [manager/README.md](../manager/README.md#running-from-task-scheduler) — Task Scheduler details
