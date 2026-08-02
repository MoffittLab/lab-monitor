# Lab Monitor - Architecture

## System Overview

Three-service distributed system for tracking NAS usage:

```
┌──────────────────────────────────────────────────────────────┐
│                        NAS Systems                            │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐        │
│  │  NAS-01     │   │  NAS-02     │   │  NAS-03     │        │
│  │ (Collector) │   │ (Collector) │   │ (Collector) │        │
│  └──────┬──────┘   └──────┬──────┘   └──────┬──────┘        │
└─────────┼────────────────┼────────────────┼────────────────┘
          │                │                │
          │ Daily Report   │                │ (HTTP POST)
          │ (JSON)         │                │
          └────────────────┼────────────────┘
                           │
                    ┌──────▼───────┐
                    │   Manager    │
                    │   Service    │
                    │ (Flask API)  │
                    └──────┬───────┘
                           │
                      ┌────┴─────┐
                      │           │
                   ┌──▼──┐   ┌───▼───┐
                   │Data │   │  API  │
                   │Store│   │Points │
                   └─────┘   └───┬───┘
                                 │
                          ┌──────▼──────┐
                          │  Dashboard  │
                          │  (Web UI)   │
                          └─────────────┘
```

## Services

### 1. Collector (NAS-side)

**Location:** Each Synology NAS
**Language:** Python 3
**Schedule:** Daily (configurable)
**Task:** 
- Measure disk usage of configured folders
- POST usage data to Manager API
- Handle errors gracefully (retry logic)

**Payload:**
```json
{
  "nas_name": "nas-01",
  "nas_id": "synology_01",
  "timestamp": "2026-07-29T02:00:00Z",
  "folders": [
    {"path": "/volume1/shared", "usage_bytes": 5368709120},
    {"path": "/volume1/media", "usage_bytes": 1099511627776},
    {"path": "/volume2/backup", "usage_bytes": 2199023255552}
  ],
  "volume_totals": {
    "/volume1": 1104880336896,
    "/volume2": 2199023255552
  },
  "total_usage_bytes": 3303903592448,
  "execution_time_seconds": 1285.47,
  "collector_version": "v0.0.9",
  "collector_commit": "358f94e"
}
```

**Payload Fields:**

| Field | Description |
|-------|-------------|
| `nas_name` | Human-readable NAS name (auto-discovered from hostname) |
| `nas_id` | Stable unique identifier (auto-discovered from boot_id) |
| `timestamp` | ISO-8601 UTC when measurement completed |
| `folders` | Array of measured directories with sizes in bytes |
| `volume_totals` | Sum of usage for each volume (e.g., "/volume1": 1.0 TB) |
| `total_usage_bytes` | Grand total usage across all volumes |
| `execution_time_seconds` | Total time for collector to run (useful for performance tracking) |
| `collector_version` | Git tag of collector version |
| `collector_commit` | Git commit hash of collector |

### 2. Manager (Central Service)

**Location:** Central server on intranet
**Language:** Python 3 + Flask
**Task:**
- Receive usage reports from collectors
- Store reports in append-only log (JSONL)
- Provide REST API for dashboard queries
- Manage authentication

**Data Storage:**
```
data/
├── nas-01/
│   └── usage.jsonl
├── nas-02/
│   └── usage.jsonl
└── nas-03/
    └── usage.jsonl
```

Each line in `usage.jsonl` is a complete JSON report with timestamp.

**API Endpoints:**
- `POST /api/usage/report` - Ingest data from collectors
- `GET /api/usage/all` - Get current state of all NAS
- `GET /api/usage/nas/<name>` - Get latest for one NAS
- `GET /api/usage/history/<name>?days=30` - Get history

### 3. Dashboard (Web UI)

**Location:** Web server on intranet
**Language:** Python 3 + Flask + HTML/JS
**Task:**
- Query Manager API every 30 seconds
- Display usage in real-time
- Show trends/historical data
- Render charts and alerts

**Features:**
- NAS status cards (current usage + trend)
- Folder breakdown per NAS
- Capacity warnings (threshold-based)
- 30-day historical graphs

## Data Flow

1. **Daily Collection**: Collector runs on schedule, measures folders
2. **Transmission**: Collector POSTs to Manager API with auth token
3. **Storage**: Manager appends to NAS-specific JSONL log
4. **Serving**: Dashboard polls Manager API every 30s
5. **Display**: Dashboard renders current state + historical trend

## Security

- Collectors authenticate with Manager via bearer token
- Manager validates token on every ingest request
- All communication over HTTP (assume intranet is trusted)
- No external internet exposure required

## Scalability Notes

- Each NAS is independent; adding new collectors is trivial
- Manager storage: JSONL files (append-only, efficient)
- Dashboard: Stateless, can be multi-instance if needed
- Typical data growth: ~1KB per NAS per day

## Future Extensions

- Metrics: Transfer speed, temperature, S.M.A.R.T. stats
- Alerts: Email/Slack when thresholds crossed
- Retention policies: Auto-cleanup old data
- Multi-tenancy: Support multiple lab groups
