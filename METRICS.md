# Metrics Collection

Reference for the two collector modes and how their data is stored.

---

## Collection Modes

| Mode | Flag | Schedule | `data_type` | What it measures |
|------|------|----------|-------------|-----------------|
| Disk | `--mode disk` | Daily, 2 AM | `folder_usage` | Folder sizes, volume totals, grand total |
| Metrics | `--mode metrics` | Every 5 min | `system_metrics` | CPU%, RAM%, uptime, network bandwidth |

Both modes use the same queue-and-archive pipeline:

```
Collect data
    ↓
Append to YYYY-MM.jsonl  (permanent local archive)
    ↓
Append to queue.json
    ↓
POST queue to Manager
    ↓
Manager ACKs with matching queue_id?
    Yes → delete queue.json
    No  → keep queue.json, retry next run
```

---

## Message Format

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
    "data_type": "...",
    "...": "..."
  }
}
```

### folder_usage

Folder paths are data keys; values are sizes in bytes. The collector automatically adds per-volume sums and a grand total.

```json
{
  "data_type":            "folder_usage",
  "/volume1/JeffMoffitt": 4832847265792,
  "/volume1/LabData":     12043821957120,
  "/volume2/Archive":     8000000000000,
  "/volume1":             16876669222912,
  "/volume2":             8000000000000,
  "total_usage":          24876669222912
}
```

Keys classified by depth:
- Two or more components (`/volume1/JeffMoffitt`) → leaf folder
- One component (`/volume1`) → volume-level sum
- `total_usage` → grand total across all volumes

### system_metrics

```json
{
  "data_type":                  "system_metrics",
  "cpu_percent":                18.4,
  "ram_percent":                61.2,
  "uptime_seconds":             863492,
  "uptime_formatted":           "9d 23h",
  "network_bytes_in":           847392719104,
  "network_bytes_out":          123847392012,
  "network_bandwidth_in_mbps":  30.72,
  "network_bandwidth_out_mbps": 1.68
}
```

`network_bytes_in/out` — cumulative OS counters since last boot.
`network_bandwidth_in_mbps` / `network_bandwidth_out_mbps` — **megabits per second** (not MB/s), computed as the average rate between the previous and current metrics run snapshot. Conversion: `(bytes_delta / time_delta_sec) * 8 bits/byte / 1,000,000 bits/Mbit`.

---

## Storage on the Collector

```
data/<name>/
├── YYYY-MM.jsonl    ← all messages, appended, never deleted
└── queue.json       ← pending messages, deleted after Manager ACK
```

Archives are written before any network call. They are a permanent local record even if the Manager is never reachable.

---

## Storage on the Manager

### Per-system history: `data/<name>/data.db`

One table per `data_type`. Column names come directly from the message data keys.

**`folder_usage` table (example):**

```
id | timestamp            | device_type | /volume1/JeffMoffitt | /volume1/LabData  | /volume1           | total_usage
---|----------------------|-------------|----------------------|-------------------|--------------------|------------
1  | 2026-08-04T02:00:00Z | synology    | 4832847265792        | 12043821957120    | 16876669222912     | 24876669222912
2  | 2026-08-05T02:00:00Z | synology    | 4833000000000        | 12044000000000    | 16877000000000     | 24877000000000
```

**`system_metrics` table (example):**

```
id | timestamp            | device_type | cpu_percent | ram_percent | uptime_seconds | ...
---|----------------------|-------------|-------------|-------------|----------------|----
1  | 2026-08-04T07:00:00Z | synology    | 18.4        | 61.2        | 863492         | ...
2  | 2026-08-04T07:05:00Z | synology    | 22.1        | 61.8        | 863792         | ...
```

**Schema evolution:** When a message arrives with a new key (e.g., a new folder appears, or a collector update adds a new metric), the Manager:
1. `ALTER TABLE` to add the column
2. Backfills all prior rows with `NaN` — clearly "not measured then" vs. zero
3. Inserts the new row with the real value

### Central summary: `metrics.db`

Three tables used by the Dashboard for fast current-state queries:

**`devices`** — one row per system, updated on every ingest:

```
name   | system_id        | device_type | first_seen           | last_seen
-------|------------------|-------------|----------------------|---------------------
Triton | synology-triton  | synology    | 2026-08-01T02:00:00Z | 2026-08-04T07:05:00Z
```

**`device_snapshot`** — latest metrics and disk state per system, overwritten on each ingest:

```
name   | metrics_timestamp    | cpu_percent | ram_percent | ... | disk_timestamp       | total_disk_bytes | folders_json
-------|----------------------|-------------|-------------|-----|----------------------|------------------|-------------
Triton | 2026-08-04T07:05:00Z | 22.1        | 61.8        | ... | 2026-08-04T02:00:00Z | 24876669222912   | {...}
```

Metrics and disk columns update independently — a disk run doesn't clobber fresh CPU data.

**`device_totals`** — running cumulative counters per system:

```
name   | total_bytes_in | total_bytes_out | last_bytes_in | last_bytes_out | total_disk_bytes
-------|----------------|-----------------|---------------|----------------|------------------
Triton | 847394000000   | 123847500000    | 847394000000  | 123847500000   | 24876669222912
```

Network bytes accumulate as deltas: on each ingest, `delta = max(0, current − last_seen)` is added to the running total. The `max(0, ...)` guard means a reboot (counter reset) contributes zero for that interval rather than corrupting the lifetime total.

---

## Configuration

Collector config fields relevant to collection:

```json
{
  "volumes":         ["/volume1", "/volume2"],
  "timeout_seconds": 3600
}
```

`volumes` — base paths to measure. The collector measures one level of immediate subdirectories within each volume.

`timeout_seconds` — maximum time to spend measuring a single subdirectory (default 3600 s = 1 hour). Useful for very large volumes.

---

## Dependencies

**Collector:** `psutil` for CPU/RAM/network. Fallback implementations are included for systems where `psutil` cannot be installed.

**Manager:** Python standard library only (`sqlite3`, `json`).

---

## See Also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design and data flow
- [collector/README.md](collector/README.md) — Collector configuration and modes
- [manager/README.md](manager/README.md) — Manager API and storage
- [INSTALLATION.md](INSTALLATION.md) — Setup instructions
