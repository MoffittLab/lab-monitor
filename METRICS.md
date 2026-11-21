# Metrics Collection

Reference for the collector's two modes, the messages they produce, and how the Manager stores them.

---

## Collection Modes

| Mode | Flag | Schedule | `data_type` | What it measures |
|------|------|----------|-------------|-----------------|
| Disk | `--mode disk` | Daily, 02:00 | `folder_usage` | Folder sizes, volume used/free/total, grand total |
| Metrics | `--mode metrics` | Every 5 min | `system_metrics` + one `user_metrics` per active user | CPU %, RAM, uptime, network counters and bandwidth, GPUs; per-user CPU and RAM |

Both modes use the same pipeline:

```
Collect data
    ↓
Append to YYYY-MM.jsonl           (permanent local archive)
    ↓
Append to queue.json              (under queue.json.lock)
    ↓
POST to the Manager in batches    (at most max_batch_messages, default 100, oldest first)
    ↓
Manager answers per message: stored / duplicate / rejected / error
    ↓
Remove stored, duplicate and rejected messages from queue.json; keep error ones for the next run
```

Details of the handshake: [collector/README.md](collector/README.md#queue-and-handshake) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#queue-and-acknowledgement).

**Unmeasured values are `null`.** When the collector can't measure something (CPU, RAM, uptime,
network counters, a GPU field, a folder), it sends `null` or leaves the key out, never `0`. The
Manager stores `NULL`, and the Dashboard shows `—`.

---

## Message Format

```json
{
  "header": {
    "msg_id":      "5b0d6c1e9f8a4c3b9e2d7a6f1c0b4e3d",
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "ip_address":  "10.0.0.42",
    "timestamp":   "2026-08-04T07:00:00.123456Z"
  },
  "data": {
    "data_type": "...",
    "...": "..."
  }
}
```

`msg_id` is assigned when the message is created and kept through every retry; the Manager uses it
to recognize re-sent messages. Messages from older collectors have no `msg_id`; the Manager derives
one (`legacy-` + sha1 of `name|data_type|timestamp|username`).

### folder_usage

The payload is flat: folder paths are keys, sizes in bytes are values. Keys starting with `_` are
metadata, never folders.

```json
{
  "data_type":            "folder_usage",
  "_unit":                "bytes",
  "/volume1/JeffMoffitt": 4832847265792,
  "/volume1/LabData":     12043821957120,
  "/volume1":             16876669222912,
  "/volume1_total_bytes": 20000000000000,
  "/volume1_free_bytes":  3123330777088,
  "/volume2":             8000000000000,
  "/volume2_total_bytes": 16000000000000,
  "/volume2_free_bytes":  8000000000000,
  "total_usage":          24876669222912,
  "_timed_out":           ["/volume2/Archive"],
  "_errors":              [],
  "_scan_seconds":        5321.4
}
```

| Key | Meaning |
|-----|---------|
| `<folder path>` | Total size of everything in that folder |
| `<volume>` | Used bytes of the volume (from the filesystem) |
| `<volume>_total_bytes`, `<volume>_free_bytes` | Volume capacity |
| `total_usage` | Sum of the measured volumes (`null` if none could be measured) |
| `_timed_out` | Folders that hit a time budget; they are left out, not reported as 0 |
| `_errors` | Folders (or glob roots / volumes) that could not be scanned, and configured paths that don't exist |
| `_scan_seconds` | Duration of the folder scan |

Volume capacity is measured first and always sent. A scan path equal to a volume root is not
scanned (the volume's own figure is authoritative).

### system_metrics

```json
{
  "data_type":                  "system_metrics",
  "cpu_percent":                18.4,
  "ram_percent":                61.2,
  "total_ram_bytes":            68719476736,
  "uptime_seconds":             863492,
  "uptime_formatted":           "9d 23h",
  "network_bytes_in":           847392719104,
  "network_bytes_out":          123847392012,
  "network_bandwidth_in_mbps":  30.72,
  "network_bandwidth_out_mbps": 1.68,
  "gpu_count":                  0,
  "gpus":                       []
}
```

Each numeric field also has a `<field>_unit` key (`%`, `bytes`, `s`, `Mbps`), used for chart labels.
With GPUs, `gpus` lists them and flat keys `gpu_<i>_percent`, `gpu_<i>_vram_used` (and
`gpu_<i>_temp_c`, `gpu_<i>_power_w` when available) are added.

- `network_bytes_in/out`: cumulative OS counters since boot, summed over all interfaces except
  loopback. A collector upgraded on a host with psutil keeps counting loopback until the next reboot
  (so the counters don't drop at the upgrade), then excludes it.
- `network_bandwidth_in_mbps` / `_out_mbps`: **megabits per second**, the average since the previous
  metrics run: `(bytes_delta / seconds) * 8 / 1,000,000`. `null` on the first run and after a counter
  reset (reboot).
- If the counters can't be read, all four network fields are `null`.

### user_metrics

One message per active user per metrics run (system accounts and `exclude_users` patterns are
skipped):

```json
{"data_type": "user_metrics", "username": "jeff", "cpu_percent": 12.5,
 "ram_bytes": 2147483648, "ram_formatted": "2.0 GB"}
```

---

## Storage on the Collector

```
data/<name>/
├── YYYY-MM.jsonl            ← every message, appended, never deleted
├── queue.json               ← messages not yet acknowledged (removed when empty)
├── queue.json.lock          ← lock file (leave it)
├── queue.corrupt-<ts>.json  ← only if a queue.json was ever unreadable
└── network_snapshot.json    ← previous counters, for bandwidth
```

The archive is written before any network call, so it is a complete local record even if the
Manager is never reachable. `tools/replay_archives.py` can send it to the Manager again.

---

## Storage on the Manager

### Per-system history: `data/<normalized name>/data.db`

The directory name is the device name in lowercase with every character other than `a-z 0-9 _`
replaced by `_` (`win-srv.lab` → `win_srv_lab`).

**`folder_usage` is stored narrow**, one row per folder per scan, so shares with thousands of folders
fit (SQLite allows at most 2000 columns per table):

```
folder_usage_msgs   id | msg_id | timestamp                   | device_type | meta_json
                    1  | 5b0d…  | 2026-08-04T02:00:00.000000Z | synology    | {"_unit": "bytes", "_timed_out": [...], ...}

folder_usage_items  msg_row_id | key                  | value
                    1          | /volume1/JeffMoffitt | 4832847265792
                    1          | /volume1             | 16876669222912
                    1          | total_usage          | 24876669222912
```

The read API puts each scan back together as the same flat dict the collector sent. A database
written by the previous Manager had one wide `folder_usage` table with one column per folder; on
first open it is copied into the narrow tables and kept, renamed, as `folder_usage_legacy`.

**Every other data type** (`system_metrics`, `user_metrics`, `not_specified` for messages without
`data_type`, and any custom type) gets one table with one column per payload key:

```
system_metrics  id | msg_id | timestamp                   | device_type | cpu_percent | ram_percent | ...
                1  | 9a8b…  | 2026-08-04T07:00:00.000000Z | synology    | 18.4        | 61.2        | ...
```

**Schema evolution:** when a message has a key the table hasn't seen (a new metric, a new GPU), the
column is added. Earlier rows read back `NULL` for it: "not measured then", which is different from
a measured zero. (The previous Manager wrote `NaN`, which SQLite also stores as `NULL`, or `''` for
text columns.) Lists and objects are stored as JSON text.

Every history table has a `msg_id` column with a UNIQUE index. Rows stored before the upgrade have
`msg_id = NULL`; a re-sent old message is recognized by its timestamp (and user name for
`user_metrics`).

Timestamps are stored as fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Rows written by the
previous Manager keep the text the collector sent, except migrated folder scans.

### Central summary: `metrics.db`

Used by the Dashboard for current state:

| Table | Contents |
|-------|----------|
| `devices` | One row per system: name, system_id, device_type, first_seen, last_seen |
| `device_snapshot` | Latest metrics (`metrics_timestamp`, CPU, RAM, uptime, bandwidth, all fields in `metrics_json`) and latest disk state (`disk_timestamp`, `total_disk_bytes`, `folders_json`) |
| `device_totals` | Lifetime network bytes (`total_bytes_in/out`), the last counters (`last_bytes_in/out`), `network_last_updated`, current `total_disk_bytes` |
| `device_user_snapshot`, `user_metrics_history` | Latest and historical per-user CPU/RAM |
| `dead_letter` | Messages the Manager rejected, with the reason and the raw message |

The snapshot, `last_seen` and the disk total follow the **newest** message: an older message stored
later (a gap-fill replay, a retried `error`) adds history but doesn't roll the card back. Metrics
and disk columns update independently.

**Lifetime network totals.** For each `system_metrics` message:

- Skip it if either counter is `null` or missing, or if it is not newer than
  `network_last_updated` (replay or out of order).
- If a counter went up, add `current − last`.
- If a counter went down:
  - it is a **reboot** (or counter reset) when `uptime_seconds` is shorter than the time since the
    previous counter reading plus 300 s, or when `uptime_seconds` is `null` or missing: add `current`
    (the traffic since boot);
  - otherwise the machine was up all along (an interface went away, loopback stopped being
    counted): add nothing, take the new value as the baseline, and log a warning.
- The first message of a device seeds the totals with its current counters.

At the upgrade, `network_last_updated` is seeded from `device_snapshot.metrics_timestamp` (when the
stored counters were read), else `last_updated`, so totals continue exactly from the stored values.
The previous Manager used `max(0, current − last)`, which dropped the traffic since each reboot.

---

## Configuration

Collector config fields that affect collection (all fields:
[collector/README.md](collector/README.md#configuration)):

```json
{
  "volumes":               ["/volume1", "/volume2"],
  "scan_paths":            ["/volume1/*", "/volume2/Data"],
  "timeout_seconds":       3600,
  "total_timeout_seconds": 10800,
  "exclude_users":         []
}
```

- `volumes`: drives or volumes whose capacity (used, free, total) is reported.
- `scan_paths`: what the folder breakdown measures. `"/volume1/*"` measures each subfolder of
  `/volume1` separately; `"/volume2/Data"` measures that folder as one total. Without `scan_paths`,
  each volume is scanned one level deep.
- `timeout_seconds`: maximum time for one folder (default 3600 s).
- `total_timeout_seconds`: maximum time for the whole folder scan. Default 3 × `timeout_seconds` on
  Windows; on Synology/Linux max(3, number of `scan_paths` entries) × `timeout_seconds`.
- `exclude_users`: extra user-name patterns to leave out of `user_metrics`.

---

## Dependencies

**Collector:** `requests` (required); `psutil` (optional: without it the collector reads `/proc`
on Linux/Synology); `nvidia-ml-py` (optional, for NVIDIA GPUs). Python 3.8 or later.

**Manager:** Flask, waitress (production server; the Flask development server is used, with a
warning, if it is missing), Flask-CORS (only if `cors_origins` is set), and the standard library's
`sqlite3`.

---

## See Also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design and data flow
- [collector/README.md](collector/README.md) — Collector configuration and modes
- [manager/README.md](manager/README.md) — Manager API and storage
- [INSTALLATION.md](INSTALLATION.md) — Setup instructions
- [docs/UPGRADE.md](docs/UPGRADE.md) — What happens to existing data at the upgrade
