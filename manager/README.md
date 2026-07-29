# Manager

Central service that receives usage data from Collectors, stores it, and provides an API for the Dashboard.

## Installation

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `config.example.json` to `config.json` and customize
3. Run: `python3 manager.py`

## Configuration

Edit `config.json`:
```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "/var/lib/lab-monitor/data",
  "auth_tokens": ["token1", "token2"],
  "log_file": "/var/log/lab-monitor-manager.log"
}
```

## API Endpoints

### Ingest Data (from Collector)
`POST /api/usage/report`
- Authentication: Bearer token in header
- Body: Usage snapshot from collector

### Query Data (for Dashboard)
`GET /api/usage/nas/<nas_name>`
- Returns latest usage for a specific NAS

`GET /api/usage/all`
- Returns current usage for all NAS systems

`GET /api/usage/history/<nas_name>?days=30`
- Returns usage history for specified period

## Data Storage

Usage data stored in `data_dir`:
```
data/
└── nas-01/
    └── usage.jsonl  # Append-only log of usage snapshots
```

Each line is a JSON record with timestamp and folder usage.
