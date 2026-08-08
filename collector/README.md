# Collector

System monitoring agent. Runs on each NAS or server, collects data, and posts it to the Manager.

See [INSTALLATION.md](../INSTALLATION.md) for setup instructions.

---

## Configuration

`local/config.json`:

```json
{
  "name":             "Triton",
  "id":               "synology-triton",
  "device_type":      "synology",
  "manager_url":      "http://atlantis.med.harvard.edu:5000",
  "manager_token":    "YOUR-MANAGER-TOKEN",

  "volumes":          ["/volume1", "/volume2"],

  "scan_paths": [
    "/volume1/*",
    "/volume1/Data1/*",
    "/volume2/*"
  ],

  "data_dir":         "/volume1/lab-monitor/data",
  "log_file":         "/volume1/lab-monitor/logs/collector.log",
  "log_level":        "INFO",
  "timeout_seconds":  3600,
  "exclude_users":    []
}
```

| Field | Description |
|-------|-------------|
| `name` | Display name — appears on the Dashboard and in database folder names |
| `id` | Stable unique identifier — use something that won't change (e.g., `synology-triton`) |
| `device_type` | System type: `synology`, `windows`, or any label you choose |
| `manager_url` | URL of the Manager service |
| `manager_token` | Bearer token — must match one of Manager's `auth_tokens` |
| `volumes` | Drives/volumes to report **capacity** for (used/free/total bytes). Used only for capacity reporting, not folder scanning. |
| `scan_paths` | Folders to measure for **usage breakdown**. Each entry is either a path (`/volume1/Data1` = measure that folder as a single total) or a glob (`/volume1/Data1/*` = measure each immediate subfolder separately). If omitted, falls back to scanning one level deep in each volume. |
| `data_dir` | Local data directory for archives and queue |
| `timeout_seconds` | Max time to measure a single folder (default 3600) |
| `exclude_users` | (Optional) Username patterns to exclude from user activity metrics. Supports wildcards. Built-in defaults already exclude `UMFD-*`, `NT AUTHORITY\*`, etc. |

`volumes` examples:
- Synology: `["/volume1", "/volume2"]`
- Windows: `["E:/Users", "F:"]`

---

## Modes

```bash
python collector.py --config local/config.json --mode disk      # Daily
python collector.py --config local/config.json --mode metrics   # Every 5 min
```

### Disk mode (`folder_usage`)

Measures the total size of each immediate subdirectory under every configured volume. Adds per-volume sums and a grand total.

**What gets measured for `volumes: ["/volume1", "/volume2"]`:**
```
/volume1/JeffMoffitt  →  total size of all contents
/volume1/LabData      →  total size of all contents
/volume2/Archive      →  total size of all contents
```

System directories (`@eaDir`, `#recycle`, etc.) are skipped automatically.

**Message generated:**
```json
{
  "header": {
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "timestamp":   "2026-08-04T02:00:00Z"
  },
  "data": {
    "data_type":            "folder_usage",
    "/volume1/JeffMoffitt": 4832847265792,
    "/volume1/LabData":     12043821957120,
    "/volume2/Archive":     8000000000000,
    "/volume1":             16876669222912,
    "/volume2":             8000000000000,
    "total_usage":          24876669222912
  }
}
```

Each folder path is a key; its value is the size in bytes. Volume-level sums (e.g., `/volume1`) and `total_usage` are computed and added automatically.

### Metrics mode (`system_metrics`)

Measures CPU%, RAM%, uptime, and network traffic. Network bandwidth is computed by comparing against the previous run's snapshot.

**Message generated:**
```json
{
  "header": {
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "timestamp":   "2026-08-04T07:05:00Z"
  },
  "data": {
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
}
```

`network_bytes_in/out` are cumulative OS counters (since last boot). `network_bandwidth_*_mbps` is the average rate since the previous metrics run.

---

## Local Storage

```
data/<name>/
├── YYYY-MM.jsonl    ← append-only archive; one message per line (permanent)
└── queue.json       ← unsent messages; deleted after successful ACK
```

Both files use the same message format. The archive is a permanent local record written before any network call — it is never deleted regardless of whether the Manager receives the data.

---

## Queue and Handshake

Each collection run:

1. Measure data
2. Append message to `YYYY-MM.jsonl` (permanent archive)
3. Append message to `queue.json`
4. POST `queue.json` to Manager as:
   ```json
   {
     "queue_id": "Triton-2026-08-04-07-05-00",
     "name":     "Triton",
     "id":       "synology-triton",
     "messages": [ ... ]
   }
   ```
5. If Manager responds `{"status": "ok", "queue_id": "Triton-2026-08-04-07-05-00"}` and the `queue_id` matches — delete `queue.json`
6. If the POST fails or `queue_id` doesn't match — keep `queue.json` and retry next run

Multiple failed runs accumulate in the queue; all are sent together on the next successful connection.

---

## Troubleshooting

**Queue not syncing:**
```bash
# Check log
tail -f /volume1/lab-monitor/logs/collector.log

# Verify Manager reachable
curl http://atlantis.med.harvard.edu:5000/health

# Check if queue has accumulated entries
cat /volume1/lab-monitor/data/Triton/queue.json
```

**"Invalid token" error:**
Verify `manager_token` in `local/config.json` exactly matches one of the `auth_tokens` in the Manager's config.

**Disk measurement slow:**
Large folders (50 TB+) take time. Increase `timeout_seconds` if runs are timing out. Check logs for per-folder timing.

---

## Dependencies

- `requests` — HTTP POST to Manager
- `psutil` — CPU, RAM, network measurements (optional; fallbacks available without it)

```bash
pip install -r requirements.txt
```
