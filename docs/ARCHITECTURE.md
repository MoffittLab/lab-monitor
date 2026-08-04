# Architecture

## Overview

Lab Monitor is a distributed monitoring platform. Collectors run on each system, generate messages, queue them locally, and POST them to a central Manager. The Manager stores full history per system and maintains a fast-access summary database for the Dashboard.

```
┌─────────────────────────────────────────────────────┐
│                   Systems (Collectors)               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐        │
│  │  Triton  │   │  Atlas   │   │  Other   │        │
│  │ Synology │   │ Windows  │   │   ...    │        │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘        │
└───────┼──────────────┼──────────────┼───────────────┘
        │              │              │
        │   POST /api/data/queue (Bearer token auth)
        └──────────────┼──────────────┘
                       │
              ┌────────▼────────┐
              │    Manager      │
              │  (Flask API)    │
              ├─────────────────┤
              │ Central DB      │  ← devices, snapshots, totals
              │ Per-system DBs  │  ← full typed history
              └────────┬────────┘
                       │  REST API
              ┌────────▼────────┐
              │   Dashboard     │
              │   (Web UI)      │
              └─────────────────┘
```

---

## Collector

Runs on each system. Two modes, invoked on separate schedules:

| Mode | Flag | Schedule | What it measures |
|------|------|----------|-----------------|
| Disk | `--mode disk` | Daily (2 AM) | Folder sizes by path, per-volume totals, grand total |
| Metrics | `--mode metrics` | Every 5 min | CPU%, RAM%, uptime, network bandwidth |

### Local storage

```
data/<name>/
├── YYYY-MM.jsonl        ← append-only archive (one message per line)
└── queue.json           ← unsent messages (deleted after ACK)
```

Both the archive and the queue store messages in the same format.

### Message format

Every message has a standard header and a data payload:

```json
{
  "header": {
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "timestamp":   "2026-08-04T07:00:00Z"
  },
  "data": {
    "data_type": "folder_usage",
    "...": "..."
  }
}
```

`data_type` tells the Manager how to store the payload. If absent, data goes to a `not_specified` table.

**Disk message (`data_type: folder_usage`):**
```json
{
  "data_type":             "folder_usage",
  "/volume1/JeffMoffitt":  4832847265792,
  "/volume1/LabData":      12043821957120,
  "/volume2/Archive":      8000000000000,
  "/volume1":              16876669222912,
  "/volume2":              8000000000000,
  "total_usage":           24876669222912
}
```
Each folder path is a key, usage in bytes is the value. Volume-level sums and a grand total are added automatically.

**Metrics message (`data_type: system_metrics`):**
```json
{
  "data_type":                  "system_metrics",
  "cpu_percent":                18.4,
  "ram_percent":                61.2,
  "uptime_seconds":             863492,
  "uptime_formatted":           "9d 23h",
  "network_bytes_in":           847392719104,
  "network_bytes_out":          123847392012,
  "network_bandwidth_in_mbps":  3.84,
  "network_bandwidth_out_mbps": 0.21
}
```

### Queue and handshake

1. Collector measures data and writes a message to the local archive (JSONL, append-only)
2. Appends the same message to `queue.json`
3. POSTs the queue to the Manager
4. Manager responds `{"status": "ok", "queue_id": "..."}` — echoing the same `queue_id`
5. Collector verifies the echoed `queue_id` matches, then deletes `queue.json`
6. If the POST fails or the `queue_id` doesn't match, the queue persists and is retried next run

The archive is never deleted — it is a permanent local record regardless of whether the Manager ever receives the data.

---

## Manager

Central Flask service. Receives queues from collectors, stores data in two layers, and serves a REST API to the Dashboard.

### Ingest endpoint

`POST /api/data/queue` accepts:

```json
{
  "queue_id": "Triton-2026-08-04-07-00-00",
  "name":     "Triton",
  "id":       "synology-triton",
  "messages": [ { "header": {...}, "data": {...} }, ... ]
}
```

Backward-compatible with the legacy `entries` format from older collectors.

### Storage — two layers

**Layer 1: Per-system typed history (`TypedDataStore`)**

```
data/
└── triton/
    └── data.db
        ├── folder_usage    ← one row per disk collection run
        ├── system_metrics  ← one row per metrics run
        └── not_specified   ← fallback for untyped messages
```

Each `data_type` gets its own SQLite table. Column names are the exact keys from the data payload — folder paths like `/volume1/JeffMoffitt` become literal column names (quoted SQLite identifiers).

Schema evolution is automatic: when a message arrives with a key the table has never seen, the Manager adds the column and backfills prior rows with `NaN`. This makes "we didn't measure this yet" unambiguous vs. a measured value of zero.

**Layer 2: Central summary (`metrics.db`, three tables)**

| Table | Purpose | Key |
|-------|---------|-----|
| `devices` | Device registry — name, id, type, first/last seen | `name` |
| `device_snapshot` | Latest metrics + disk state per device | `name` |
| `device_totals` | Cumulative network bytes (survives reboots) + current disk total | `name` |

`device_totals` accumulates network bytes as deltas (`max(0, current − last_seen)`) so counter resets on reboot don't corrupt the lifetime total. The Dashboard reads from these three tables for fast current-state queries.

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/data/queue` | Ingest queue from collector |
| GET | `/health` | Health check |
| GET | `/api/devices` | Device registry |
| GET | `/api/totals` | Per-device and global running totals |
| GET | `/api/systems` | List of known system names |
| GET | `/api/metrics/all` | Latest system_metrics snapshot, all systems |
| GET | `/api/metrics/nas/<name>` | Latest system_metrics, one system |
| GET | `/api/usage/all` | Latest folder_usage snapshot, all systems |
| GET | `/api/usage/nas/<name>` | Latest folder_usage, one system |
| GET | `/api/usage/history/<name>` | Disk usage history |
| GET | `/api/data/<system>` | List data_types present for a system |
| GET | `/api/data/<system>/<data_type>` | Typed history rows |

All endpoints except `/health` require `Authorization: Bearer <token>`.

---

## Dashboard

Read-only Flask web app. Polls the Manager API on a configurable interval (default 30 s).

- Reads device registry, snapshots, and global totals from Manager
- Displays one card per system showing both metrics and disk state
- Disk cards show per-folder usage, per-volume totals, and grand total
- Summary header shows lifetime network totals (from `device_totals`)
- Detail modal shows first-seen date, system ID, and running totals

The Dashboard owns no data. All state lives in the Manager.

---

## Security

- Bearer token authentication on all Manager API endpoints
- Configurable CORS origin whitelist (Dashboard URL)
- Tokens stored in `config.json` (excluded from git via `.gitignore`)
- Communication over HTTP — assumed intranet deployment

---

## Adding a new system

1. Install the collector on the new system (see INSTALLATION.md)
2. Configure `name`, `id`, `device_type`, `manager_url`, `manager_token`
3. Schedule disk and metrics collection jobs
4. The Manager auto-creates `data/<name>/data.db` on first message
5. The device appears in the Dashboard on next refresh
