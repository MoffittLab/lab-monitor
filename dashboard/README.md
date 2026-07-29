# Dashboard

Web interface for viewing NAS usage statistics and trends.

## Installation

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `config.example.json` to `config.json` and customize
3. Run: `python3 app.py`
4. Open `http://localhost:5001` in your browser

## Configuration

Edit `config.json`:
```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://manager.internal:5000",
  "refresh_interval_seconds": 30,
  "log_file": "/var/log/lab-monitor-dashboard.log"
}
```

## Features

- Real-time NAS usage display
- Historical usage trends (30-day graph)
- Capacity warnings (customizable thresholds)
- Per-folder breakdown

## UI Components

- Header: System health summary
- Main grid: Per-NAS cards with current usage + sparkline
- Detail view: Folder-level breakdown and historical graph

## Development

Static files in `static/` (CSS, JS)
Templates in `templates/` (HTML)

Refresh interval: 30 seconds (configurable)
