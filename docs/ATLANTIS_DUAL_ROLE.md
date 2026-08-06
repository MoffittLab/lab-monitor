# Atlantis Dual Role: Manager + Collector

## Overview

Atlantis (Windows Server) will run **both** the Manager (central service) and the Collector (monitoring its own system). This document explains how the data storage works when both components share the same machine and data directory.

---

## Data Directory Structure

Both components use the **same `data_dir`**: `E:\Users\lab-monitor\data`

```
E:\Users\lab-monitor\data\
├── metrics.db                    ← Manager: central summary DB
│                                   (devices, device_snapshot, device_totals tables)
│
├── atlantis\                     ← System folder: "atlantis" (collector name)
│   ├── data.db                   ← Manager: full history for atlantis
│   │                               (folder_usage, system_metrics, etc. tables)
│   ├── 2026-08.jsonl             ← Collector: archive (append-only)
│   ├── 2026-09.jsonl             ← Collector: archive
│   └── queue.json                ← Collector: pending messages (temporary)
│
├── triton\                       ← System folder: "triton" (other collector)
│   ├── data.db                   ← Manager: full history for triton
│   ├── 2026-08.jsonl             ← Collector: archive
│   └── queue.json                ← Collector: pending messages
│
└── other-system\
    ├── data.db
    ├── 2026-08.jsonl
    └── queue.json
```

---

## Component Responsibilities

### Manager (Flask service, port 5000)

1. **Central summary database** (`metrics.db`):
   - `devices` table — all registered systems + metadata
   - `device_snapshot` — latest metrics and disk state per system
   - `device_totals` — lifetime network bytes, current disk total

2. **Per-system history databases** (`<system_name>/data.db`):
   - `folder_usage` table — one row per daily disk run
   - `system_metrics` table — one row per 5-min metrics run
   - Any other `data_type` tables

### Collector (runs every 5 min for metrics, daily for disk)

1. **Local archives** (`<system_name>/YYYY-MM.jsonl`):
   - Permanent, append-only record of all measurements
   - Never deleted; survives network outages
   - Written **before** any network call

2. **Local queue** (`<system_name>/queue.json`):
   - Temporary file holding messages ready to send
   - POSTs to Manager as `POST /api/data/queue`
   - Deleted only after Manager ACKs with matching `queue_id`

---

## Data Flow: Atlantis (Dual Role)

### 1. Collector runs (every 5 min for metrics, daily for disk)

```
┌─────────────────────────────────────┐
│  Collector (atlantis)               │
│  - Measure CPU%, RAM%, network      │
│  - Measure disk usage by folder     │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│  Append to E:\Users\lab-monitor\data\atlantis\          │
│              2026-08.jsonl (archive)                    │
│                                                         │
│  Example: {"header": {...}, "data": {...}}             │
└─────────────┬───────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│  Append to E:\Users\lab-monitor\data\atlantis\          │
│            queue.json (staging)                         │
└─────────────┬───────────────────────────────────────────┘
              │
              ▼ POST /api/data/queue
              │ (to http://localhost:5000)
┌─────────────▼───────────────────────────────────────────┐
│  Manager (Flask service, local on same machine)         │
│  - Receives queue batch                                 │
│  - Stores in data/atlantis/data.db (per-system)        │
│  - Updates data/metrics.db (central summary)            │
│  - Responds with {status: ok, queue_id: "..."}         │
└─────────────┬───────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│  Collector deletes queue.json                           │
│  (only after verified ACK with matching queue_id)       │
└─────────────────────────────────────────────────────────┘
```

### 2. Dashboard queries (every 30 sec)

```
┌────────────────────────────────────────────────────────┐
│  Dashboard (port 5001)                                 │
│  - GET /api/devices                                    │
│  - GET /api/metrics/all                                │
│  - GET /api/usage/all                                  │
│  - etc.                                                │
└────────────────────┬─────────────────────────────────┘
                     │
                     ▼ (Bearer token auth)
        ┌────────────────────────────────┐
        │  Manager (Flask service)       │
        │  - Query metrics.db            │
        │  - Return current state        │
        └────────────────────┬───────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  E:\Users\lab-monitor\       │
              │  - data/metrics.db           │
              │  - data/atlantis/data.db     │
              │  - data/triton/data.db       │
              │  - etc.                      │
              └──────────────────────────────┘
```

---

## Configuration: Atlantis Setup

### Manager config
**File:** `E:\Users\lab-monitor\scripts\lab-monitor\manager\local\config.json`

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "E:\\Users\\lab-monitor\\data",
  "auth_tokens": ["YOUR-SECURE-TOKEN"],
  "cors_origins": [
    "http://localhost:5001",
    "http://atlantis.med.harvard.edu:5001"
  ],
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
  "manager_token": "YOUR-SECURE-TOKEN",
  "volumes": ["E:", "F:"],
  "data_dir": "E:\\Users\\lab-monitor\\data",
  "log_file": "E:\\Users\\lab-monitor\\logs\\collector.log",
  "log_level": "INFO",
  "timeout_seconds": 3600,
  "request_timeout_seconds": 30
}
```

**Key points:**
- `manager_url` is `http://localhost:5000` (same machine)
- `manager_token` must be in Manager's `auth_tokens` list
- Both use the same `data_dir` — **no conflict**

### Dashboard config
**File:** `E:\Users\lab-monitor\scripts\lab-monitor\dashboard\local\config.json`

```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://localhost:5000",
  "manager_token": "YOUR-SECURE-TOKEN",
  "refresh_interval_seconds": 30,
  "log_file": "E:\\Users\\lab-monitor\\logs\\dashboard.log",
  "log_level": "INFO",
  "debug": false
}
```

---

## Concurrency & Safety

### No conflicts between Collector and Manager

| Component | Files Created | Location | Access |
|-----------|---------------|----------|--------|
| **Collector** | `YYYY-MM.jsonl` | `data/<name>/` | Write-append only |
| **Collector** | `queue.json` | `data/<name>/` | Create, write, delete |
| **Manager** | `data.db` | `data/<name>/` | SQLite (threaded) |
| **Manager** | `metrics.db` | `data/` | SQLite (threaded) |

### SQLite WAL mode

Both Manager databases use SQLite WAL (Write-Ahead Logging):
```python
db.execute('PRAGMA journal_mode=WAL')
```

This means:
- ✅ Multiple readers can access the same database simultaneously
- ✅ A writer doesn't block readers (and vice versa)
- ✅ Collector can write to `queue.json` while Manager reads `data.db`
- ✅ Safe for concurrent access from multiple processes

### Network loop (all on localhost)

Collector POSTs to Manager at `http://localhost:5000`:
- ✅ No network latency; local IPC-like speed
- ✅ Instant acknowledgment
- ✅ Queue deletion happens immediately after successful ACK

---

## Data Retention

### Archives (permanent)

`data/atlantis/2026-08.jsonl`, `data/atlantis/2026-09.jsonl`, etc.
- **Retention:** Forever (unless manually deleted)
- **Size:** Small (one entry per collection run)
- **Purpose:** Permanent local record

### Central summary (Manager)

`data/metrics.db`
- **Retention:** Configurable via `retention_days` in Manager config (default: 90 days)
- **Size:** Very small (one snapshot per system)
- **Purpose:** Current state for Dashboard

### Per-system history (Manager)

`data/atlantis/data.db`, `data/triton/data.db`, etc.
- **Retention:** Configurable via `retention_days`
- **Size:** Moderate (one row per collection run; ~24 metrics runs/day × systems)
- **Purpose:** Full typed history for trend analysis

### Queue (temporary)

`data/atlantis/queue.json`
- **Retention:** Until ACK'd (usually seconds)
- **Size:** Small (batched messages)
- **Purpose:** Ensures reliable delivery

---

## Disk Space Estimate (Atlantis)

For atlantis running both Manager and as a collector for 1 year:

```
Archives (2026-01.jsonl through 2026-12.jsonl):
  - 12 months × 30 disk runs/month + ~260 metric runs/month
  - ~290 messages/month × 500 bytes/message ≈ 145 KB/month
  - 1 year: ~1.7 MB

data.db per system (atlantis + other collectors, e.g., 5 systems):
  - Per system: ~260 metric runs/month × 12 months = 3,120 rows/year
  - Plus daily disk runs: ~30 rows/month × 12 months = 360 rows/year
  - Per system: ~3,480 rows/year × 200 bytes/row ≈ 700 KB/system/year
  - 5 systems: ~3.5 MB

metrics.db (central summary, ~10 KB per snapshot):
  - devices table: ~10 rows × 200 bytes = 2 KB
  - device_snapshot table: ~10 rows × 1 KB = 10 KB
  - device_totals table: ~10 rows × 500 bytes = 5 KB
  - Total: ~20 KB

TOTAL for 1 year with 5 collectors: ~6-8 MB
```

**Atlantis drive space:** 6-8 MB is negligible. Even 10 years would be <100 MB.

---

## Advantages of This Setup

1. **Simple installation** — only one Windows server needed for central monitoring
2. **Self-monitoring** — atlantis automatically reports its own health
3. **No external dependencies** — no separate Manager host required
4. **Low overhead** — localhost traffic is instant; no network delays
5. **Reliable** — queue survives Manager outages (though unlikely since it's the same machine)
6. **Scalable** — add more collectors (Synology, other Windows servers) without changing atlantis setup

---

## Troubleshooting Atlantis Dual Role

### "Manager connection refused" in Collector logs

**Cause:** Manager service not running  
**Fix:** Check Windows Task Scheduler:
```powershell
Get-ScheduledTask -TaskName "Lab Monitor*" | Select-Object -Property TaskName, State
tasklist | findstr python
```

### "Invalid token" from Manager

**Cause:** Collector's `manager_token` doesn't match Manager's `auth_tokens`  
**Fix:** Verify both configs have same token:
```powershell
notepad E:\Users\lab-monitor\scripts\lab-monitor\manager\local\config.json
notepad E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json
```

### Queue.json accumulating (not being deleted)

**Cause:** Manager not responding with ACK or ACK has wrong `queue_id`  
**Fix:** Check Manager logs:
```powershell
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 50
```

### Dashboard shows stale data

**Cause:** Manager running but Dashboard not refreshing  
**Fix:** Verify Dashboard can reach Manager:
```powershell
curl http://localhost:5000/health
# Should return: {"status": "ok"}
```

---

## See Also

- [ARCHITECTURE.md](ARCHITECTURE.md) — System design
- [INSTALLATION.md](../INSTALLATION.md) — Installation steps
- [CONFIG.md](../CONFIG.md) — Configuration security
