# Collector

Runs on each Synology NAS. Periodically measures disk usage of shared folders and reports to the central Manager.

## Installation on Synology

1. Copy `collector.py` to your NAS (e.g., `/volume1/scripts/collector/`)
2. Copy `config.json` (customize for your NAS)
3. Install Python dependencies: `pip install -r requirements.txt`
4. Schedule via Synology Task Scheduler to run daily

## Configuration

Edit `config.json`:
```json
{
  "nas_name": "nas-01",
  "manager_url": "http://manager.internal:5000",
  "manager_token": "your-auth-token",
  "folders_to_monitor": [
    "/volume1/shared",
    "/volume1/media"
  ],
  "log_file": "/var/log/collector.log"
}
```

## Running

```bash
python3 collector.py --config config.json
```

## Scheduling on Synology

Via Control Panel → Task Scheduler:
- Create a new scheduled task (custom script)
- Set to run daily (e.g., 2 AM)
- Run with highest privileges
- Command: `/usr/bin/python3 /path/to/collector.py --config /path/to/config.json`

## Output

Sends a POST request to Manager with JSON payload:
```json
{
  "nas_name": "nas-01",
  "timestamp": "2026-07-29T02:00:00Z",
  "folders": [
    {"path": "/volume1/shared", "usage_bytes": 5368709120},
    {"path": "/volume1/media", "usage_bytes": 1099511627776}
  ]
}
```
