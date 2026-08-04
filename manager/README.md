# Manager — API Reference

Central Flask service that receives data from collectors, stores it, and serves a REST API to the Dashboard.

See [INSTALLATION.md](../INSTALLATION.md) for setup instructions.

---

## Configuration

`config.json` in the manager directory:

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "E:\\Users\\lab-monitor\\data",
  "auth_tokens": ["your-secure-token"],
  "cors_origins": ["http://localhost:5001", "http://atlantis.med.harvard.edu:5001"],
  "log_file": "E:\\Users\\lab-monitor\\logs\\manager.log",
  "log_level": "INFO",
  "debug": false
}
```

`auth_tokens` — list of Bearer tokens accepted from collectors and Dashboard. All collectors must use one of these tokens.

See [CONFIG.md](../CONFIG.md) for security guidance.

---

## Storage

### Per-system history: `data/<name>/data.db`

One SQLite database per system. One table per `data_type`:

```
data/
└── triton/
    └── data.db
        ├── folder_usage    ← one row per daily disk run
        ├── system_metrics  ← one row per 5-min metrics run
        └── not_specified   ← messages with no data_type label
```

Column names come directly from the message data keys. Folder paths like `/volume1/JeffMoffitt` are literal column names (quoted SQLite identifiers). When a message arrives with a key the table hasn't seen before, the Manager adds the column and backfills prior rows with `NaN`.

### Central summary: `metrics.db`

Three tables for fast Dashboard queries:

| Table | Contents | Key |
|-------|----------|-----|
| `devices` | Name, system_id, device_type, first_seen, last_seen | `name` |
| `device_snapshot` | Latest cpu_percent, ram_percent, uptime, bandwidth, disk total, folder data | `name` |
| `device_totals` | Lifetime network bytes (delta-accumulated across reboots), current disk total | `name` |

---

## API Endpoints

All endpoints require `Authorization: Bearer <token>` except `/health`.

### Ingest

#### `POST /api/data/queue`

Receive a queue batch from a collector.

**Request (current format):**
```json
{
  "queue_id": "Triton-2026-08-04-07-00-00",
  "name":     "Triton",
  "id":       "synology-triton",
  "messages": [
    {
      "header": {
        "device_name": "Triton",
        "device_id":   "synology-triton",
        "device_type": "synology",
        "timestamp":   "2026-08-04T07:00:00Z"
      },
      "data": {
        "data_type":            "folder_usage",
        "/volume1/JeffMoffitt": 4832847265792,
        "/volume1/LabData":     12043821957120,
        "/volume1":             16876669222912,
        "total_usage":          16876669222912
      }
    },
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
  ]
}
```

**Response:**
```json
{
  "status":    "ok",
  "queue_id":  "Triton-2026-08-04-07-00-00",
  "stored":    2,
  "timestamp": "2026-08-04T07:05:01Z"
}
```

The `queue_id` is echoed back so the collector can verify the correct batch was acknowledged before deleting its local queue.

Also accepts legacy `entries` format from older collectors (logged as a deprecation warning).

---

### Device Registry

#### `GET /api/devices`

All registered systems.

```json
{
  "devices": [
    {
      "name":        "Triton",
      "system_id":   "synology-triton",
      "device_type": "synology",
      "first_seen":  "2026-08-01T02:00:00Z",
      "last_seen":   "2026-08-04T07:05:00Z"
    }
  ]
}
```

#### `GET /api/systems`

List of system names only: `{"systems": ["Atlas", "Triton"]}`

---

### Running Totals

#### `GET /api/totals`

Cumulative network bytes and current disk totals.

```json
{
  "global": {
    "total_bytes_in":   1234567890,
    "total_bytes_out":  987654321,
    "total_disk_bytes": 28575692478464
  },
  "devices": {
    "Triton": {
      "total_bytes_in":   847394000000,
      "total_bytes_out":  123847500000,
      "total_disk_bytes": 28575692478464
    }
  }
}
```

Network totals accumulate deltas across reboots — they represent lifetime traffic, not just since last boot.

---

### Latest Snapshots

#### `GET /api/metrics/all`

Latest `system_metrics` snapshot for every system.

#### `GET /api/metrics/nas/<name>`

Latest `system_metrics` for one system.

#### `GET /api/usage/all`

Latest `folder_usage` snapshot for every system. Data keys are folder paths and volume sums.

#### `GET /api/usage/nas/<name>`

Latest `folder_usage` for one system.

#### `GET /api/usage/history/<name>?days=30`

Historical disk usage records for one system.

---

### Typed History

#### `GET /api/data/<system>`

List data types that have been recorded for a system:
```json
{"system": "Triton", "data_types": ["folder_usage", "system_metrics"]}
```

#### `GET /api/data/<system>/<data_type>?limit=100&start=<iso>&end=<iso>`

Historical rows for a (system, data_type) pair. Returns raw column values as stored, including literal path keys for `folder_usage`.

---

### Health

#### `GET /health`

`{"status": "ok"}` — no auth required.

---

## Troubleshooting

**Collector reports "Invalid token":**
Verify `manager_token` in the collector config exactly matches one of the `auth_tokens` in Manager's config. No leading/trailing spaces.

**Dashboard shows "Unable to reach Manager":**
Check Manager is running, firewall allows port 5000, and Dashboard config has the correct `manager_url`.

**"Database locked" errors:**
Ensure only one Manager instance is running.
