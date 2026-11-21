# Atlantis Dual Role: Manager + Collector

## Overview

Atlantis (Windows Server) runs **both** the Manager and Dashboard (the central services) and a
Collector (monitoring atlantis itself). This page explains how the two share the machine, the
checkout and, by default, the data directory.

- One checkout: `E:\Users\lab-monitor\scripts\lab-monitor` holds `manager\`, `dashboard\` and
  `collector\`. Updating it (installer or `git pull`) updates all three at once.
- One data directory by default: `E:\Users\lab-monitor\data` is the Manager's `data_dir` and the
  collector's `data_dir`.

---

## Data Directory Structure

```
E:\Users\lab-monitor\data\
├── metrics.db                    ← Manager: central summary DB
│                                   (devices, device_snapshot, device_totals, per-user, dead_letter)
│
├── atlantis\                     ← one folder per system
│   ├── data.db                   ← Manager: full history for atlantis
│   ├── data.db.pre-upgrade-<stamp>.bak  ← Manager: copy taken before the upgrade migration
│   ├── 2026-08.jsonl             ← Collector: archive (append-only)
│   ├── 2026-09.jsonl             ← Collector: archive
│   ├── queue.json                ← Collector: messages not yet acknowledged
│   ├── queue.json.lock           ← Collector: queue lock
│   ├── network_snapshot.json     ← Collector: previous network counters
│   └── perf_snapshot.json, sessions_state.json, storage_health_state.json,
│       storage_breakdown_state.json  ← Collector: usage-metrics state (usage-metrics release)
│
├── triton\                       ← another collector: only the Manager's files here
│   └── data.db
└── other-system\
    └── data.db
```

The Manager names each folder after the **normalized** device name (lowercase; anything other than
`a-z 0-9 _` becomes `_`). The collector names its folder after its configured `name` exactly. They
meet in one folder when the name is already lowercase letters, digits and `_` (like `atlantis`); on
Windows, folder names are case-insensitive, so `Atlantis` also lands in `atlantis\`. A name with
`-` or `.` (e.g. `win-srv`) gives two folders: `win-srv\` (collector) and `win_srv\` (Manager).
Either way the files don't conflict.

---

## Component Responsibilities

### Manager (port 5000)

1. **Central summary database** (`metrics.db`): device registry, latest snapshots, lifetime totals,
   per-user tables, rejected messages.
2. **Per-system history** (`<system>\data.db`): `folder_usage_msgs` / `folder_usage_items` (one row
   per folder per daily scan; `folder_usage_legacy` after an upgrade), `system_metrics` (one row per
   5-minute run), `user_metrics`, and any other `data_type`.

### Collector (metrics every 5 min, disk daily at 02:00)

1. **Local archive** (`<name>\YYYY-MM.jsonl`): permanent, append-only record of every message,
   written before any network call.
2. **Local queue** (`<name>\queue.json`): messages waiting for the Manager. Sent in batches to
   `POST /api/data/queue`; each message is removed once the Manager reports it `stored`,
   `duplicate` or `rejected`.

---

## Data Flow on Atlantis

```
Collector (atlantis, run by Task Scheduler)
  │  measure CPU / RAM / network / users (every 5 min), disk (02:00)
  ▼
append to data\atlantis\YYYY-MM.jsonl  (archive)
  ▼
append to data\atlantis\queue.json     (under queue.json.lock)
  ▼  POST http://localhost:5000/api/data/queue  (atlantis's collector token)
Manager (task "Lab Monitor Manager")
  │  store each message in data\atlantis\data.db + update data\metrics.db
  │  answer per message: stored / duplicate / rejected / error
  ▼
Collector removes the acknowledged messages from queue.json; "error" ones stay for the next run

Dashboard (task "Lab Monitor Dashboard", port 5001)
  │  GET /api/devices, /api/metrics/all, /api/usage/all, /api/totals, /api/user_metrics/all
  ▼  (read token, http://localhost:5000)
Manager reads metrics.db (and data.db for history charts)
```

---

## Configuration: Atlantis Setup

### Manager config
**File:** `E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json`

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "E:\\Users\\lab-monitor\\data",
  "admin_tokens": ["<random admin token>"],
  "read_tokens": ["<random read token for the Dashboard>"],
  "collector_tokens": {
    "atlantis": "<random token for atlantis>",
    "triton":   "<random token for triton>"
  },
  "cors_origins": [],
  "history_retention_days": null,
  "backup_before_migration": true,
  "log_file": "E:\\Users\\lab-monitor\\logs\\manager.log",
  "log_level": "INFO",
  "debug": false
}
```

### Collector config (atlantis as a collector)
**File:** `E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`

```json
{
  "name": "atlantis",
  "id": "windows-atlantis",
  "device_type": "Server",
  "manager_url": "http://localhost:5000",
  "manager_token": "<the atlantis entry of collector_tokens>",
  "volumes": ["E:", "F:"],
  "scan_paths": ["E:/Users/*"],
  "data_dir": "E:\\Users\\lab-monitor\\data",
  "log_file": "E:\\Users\\lab-monitor\\logs\\collector.log",
  "log_level": "INFO",
  "timeout_seconds": 3600,
  "request_timeout_seconds": 30
}
```

### Dashboard config
**File:** `E:\Users\lab-monitor\scripts\lab-monitor\dashboard\config.json`

```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://localhost:5000",
  "manager_token": "<one of the Manager's read_tokens>",
  "refresh_interval_seconds": 30,
  "log_file": "E:\\Users\\lab-monitor\\logs\\dashboard.log",
  "log_level": "INFO",
  "debug": false
}
```

**Key points:**
- `manager_url` is `http://localhost:5000` for both the collector and the Dashboard.
- The collector's `manager_token` is its own `collector_tokens` entry (key = its `name`); the
  Dashboard's is a read token. A legacy `auth_tokens` token still works for both, with full rights.
- Both components may use the same `data_dir`; their files never conflict.

---

## Concurrency & Safety

| Component | Files | Location | Access |
|-----------|-------|----------|--------|
| Collector | `YYYY-MM.jsonl` | `data\<name>\` | Append only |
| Collector | `queue.json`, `queue.json.lock` | `data\<name>\` | Locked, atomic rewrite |
| Collector | `perf_snapshot.json` (+ `.lock`), `sessions_state.json`, `storage_health_state.json`, `storage_breakdown_state.json` | `data\<name>\` | Atomic rewrite, after the run's messages are recorded (usage-metrics release) |
| Manager | `data.db` (+ `-wal`, `-shm`) | `data\<normalized name>\` | SQLite, one locked connection per database |
| Manager | `metrics.db` (+ `-wal`, `-shm`) | `data\` | SQLite, one locked connection |

- The Manager uses SQLite WAL mode: readers don't block the writer. Writes for one message happen in
  one transaction. A writer waits up to 10 s for a lock another process holds (for example a database
  browser with an open transaction); if the database stays locked, the messages are answered `error`
  and stay queued on the collectors.
- Only **one** Manager may use the data directory. The installer stops older instances before it
  starts the task; check with
  `Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object CommandLine -match 'manager\.py'`.
- The collector and the Manager don't share any file.

**Things that touch the shared data directory:**
- `tools\verify_upgrade.py` reads it and fingerprints every file; the collector's own archive and
  queue changing during the run show up as a `source` difference (see
  [UPGRADE.md](UPGRADE.md#step-1-verify-against-your-real-data)).
- Re-running `install-collector-windows.ps1` resets the permissions of everything under
  `E:\Users\lab-monitor` (SYSTEM and Administrators get full control, Users read), including the
  Manager's databases. The data itself is not changed; the Manager tasks run elevated as an
  administrator and keep access.
- Upgrading the checkout upgrades the collector together with the Manager: don't do it while
  atlantis's 02:00 disk run is in progress. For the usage-metrics release this makes atlantis's
  collector the first Windows collector to get the new features, before the field checks of the other
  Windows servers ([FIELD_CHECKS.md](FIELD_CHECKS.md#windows-server-2016-2019-2022)). Run those checks on
  atlantis first, or put the new features' switches (for example `"storage_breakdown": {"enabled":
  false}`, `"storage_health": {"enabled": false}`) into its collector config before the upgrade; the
  older collector ignores keys it doesn't know.

---

## Data Retention

| Data | Retention |
|------|-----------|
| Collector archives `data\atlantis\YYYY-MM.jsonl` | Forever (never deleted by the collector) |
| Manager history (`data.db`, `user_metrics_history`) | Forever by default. With `history_retention_days` set, rows older than that are appended to `data\<system>\archive\<YYYY-MM>-<data_type>.jsonl` and then deleted, once a day |
| Manager snapshots and totals (`metrics.db`) | One row per system, always current |
| Pre-upgrade backups (`*.pre-upgrade-*.bak`) | Until you delete them (after the upgrade is confirmed) |
| Queue `queue.json` | Until acknowledged (usually seconds) |

`retention_days` (from older configs) has no effect.

---

## Disk Space

Rough growth per system:
- `system_metrics`: 288 rows a day (one per 5 minutes).
- `user_metrics`: 288 rows a day per active user, in `data.db` and in `metrics.db`.
- `folder_usage`: one scan a day, one row per folder.
- The collector archive holds every message once more as JSON.

To see the real numbers:
```powershell
Get-ChildItem E:\Users\lab-monitor\data -Recurse -File | Measure-Object -Property Length -Sum
Get-ChildItem E:\Users\lab-monitor\data -Recurse -Filter *.db | Sort-Object Length -Descending | Select-Object FullName, Length -First 10
```

---

## Advantages of This Setup

1. **Simple installation**: one Windows server for the central services
2. **Self-monitoring**: atlantis reports its own health
3. **No external dependencies**: no separate Manager host
4. **Low overhead**: the collector reaches the Manager over localhost
5. **Reliable**: the collector's queue survives a Manager restart
6. **Scalable**: add more collectors without changing atlantis

---

## Troubleshooting Atlantis Dual Role

### "Network error" / connection refused in the collector log
The Manager isn't running (or is still migrating after an upgrade):
```powershell
Get-ScheduledTask -TaskName "Lab Monitor*" | Select-Object TaskName, State
Get-Content E:\Users\lab-monitor\logs\manager-console.log -Tail 20
Invoke-RestMethod http://localhost:5000/health
```

### HTTP 401 / 403 in the collector log
401: the collector's `manager_token` matches no Manager token. 403: it is another device's
collector token (the `collector_tokens` key must be this collector's `name`) or a read token.
```powershell
notepad E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json
notepad E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json
```
Restart the Manager after changing its config.

### queue.json keeps growing
The Manager is not acknowledging. Check the collector log for the HTTP status and the Manager log:
```powershell
Get-Content E:\Users\lab-monitor\logs\collector.log -Tail 50
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 50
```
Messages answered `error` stay queued on purpose (a database problem on the Manager side).

### Dashboard shows "Manager unreachable" or stale data
```powershell
Invoke-RestMethod http://localhost:5000/health      # {"status": "ok"}
Invoke-RestMethod http://localhost:5001/health
```
The Dashboard banner lists the failing Manager calls; `HTTP 401` means its `manager_token` is wrong.

---

## See Also

- [ARCHITECTURE.md](ARCHITECTURE.md) — System design
- [UPGRADE.md](UPGRADE.md) — Upgrading an existing installation
- [INSTALLATION.md](../INSTALLATION.md) — Installation steps
- [CONFIG.md](../CONFIG.md) — Configuration and security
