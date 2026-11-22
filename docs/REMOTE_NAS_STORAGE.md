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

**Default and simplest: a local `data_dir`** (`E:\Users\lab-monitor\data`), backed up by copying
it (see [Backup & Snapshot Strategy](#backup--snapshot-strategy)). Use a share only if you need
the NAS to hold the live databases.

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
SQLite's locking. With **exactly one Manager, on one server,** using the share this generally works,
but a local disk is more robust:

- Never point two Managers (or a Manager and a copy job that writes) at the same files.
- Network latency makes every write slower. The Manager commits up to `ingest_commit_messages` (100)
  messages of a request together, one COMMIT (and sync) per database, rather than two per message.
- If the share is briefly unreachable, writes fail as `error` and the collectors keep those messages
  queued; they are stored on a later run.
- If the share is unreachable at startup, the Manager exits with "Cannot open data_dir …" and the
  launcher retries every 10 seconds.

---

## Backup & Snapshot Strategy

**Option B (Manager on the share):** NAS snapshots capture `metrics.db` and every `data.db`. A
snapshot that captures a database and its `-wal` file at the same instant is crash-consistent:
SQLite recovers it on open, as after a power cut.

**Local `data_dir`:** copy it regularly. A consistent copy needs the Manager stopped for the copy
(collectors queue meanwhile):

```powershell
Stop-ScheduledTask -TaskName "Lab Monitor Manager"
# make sure the python process is gone (see INSTALLATION.md, "Restart a service")
robocopy E:\Users\lab-monitor\data \\triton1.hms.harvard.edu\lab-monitor-backup\data /MIR /COPY:DAT /R:2 /W:5
Start-ScheduledTask -TaskName "Lab Monitor Manager"
```

(`/MIR` deletes files in the destination that no longer exist in the source; use a dated folder and
`/E` instead if you want to keep every copy.)

The collector archives (`YYYY-MM.jsonl` on each collector) are a second, independent record: a lost
Manager history can be rebuilt from them with `tools/replay_archives.py`.

---

## Setup Steps (switching to a share)

1. **Create the share** on the NAS (e.g. `lab-monitor-data`) with read/write for the account the
   Manager tasks will run as.
2. **Stop the Manager and Dashboard** and make sure the processes are gone.
3. **Copy the existing data** (keeps all history):
   ```powershell
   robocopy E:\Users\lab-monitor\data \\triton1.hms.harvard.edu\lab-monitor-data /E /COPY:DAT /R:2 /W:5
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

Stop the Manager, copy the share's contents back to `E:\Users\lab-monitor\data`, set `data_dir` back,
and re-run the installer (it then uses S4U again).

---

## See Also

- [ATLANTIS_DUAL_ROLE.md](ATLANTIS_DUAL_ROLE.md) — Manager and collector on one server
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design
- [UPGRADE.md](UPGRADE.md) — Upgrading an existing installation
- [CONFIG.md](../CONFIG.md) — Configuration and security
- [manager/README.md](../manager/README.md#running-from-task-scheduler) — Task Scheduler details
