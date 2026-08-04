# Dashboard

Web interface for viewing system usage statistics (disk, CPU, RAM, network) and trends.

## Installation & Setup

**See [INSTALLATION.md](../INSTALLATION.md) for complete setup instructions.**

Run: `install-manager-dashboard.ps1` on Windows Server atlantis.

---

## Configuration

Dashboard reads `config.json` in its directory:

```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://localhost:5000",
  "manager_token": "SAME-AS-ONE-OF-MANAGER-TOKENS",
  "refresh_interval_seconds": 30,
  "manager_timeout_seconds": 5,
  "log_file": "E:\\Users\\lab-monitor\\logs\\dashboard.log",
  "log_level": "INFO",
  "debug": false
}
```

**Key fields:**
- `manager_url` - URL to Manager service (e.g., `http://atlantis.med.harvard.edu:5000`)
- `manager_token` - Bearer token (must match one of Manager's `auth_tokens`)
- `refresh_interval_seconds` - How often to poll Manager (default: 30 seconds)

---

## Usage

1. **Start the service** (or verify it's running via Task Scheduler)
2. **Open in browser:** `http://atlantis.med.harvard.edu:5001`
3. **View:**
   - Real-time metrics (CPU%, RAM%, network) - updates every 30 seconds
   - Disk usage by system and folder
   - Historical trends

---

## Troubleshooting

**"Unable to reach Manager":**
- Verify Manager is running: `tasklist | findstr manager`
- Check Manager URL in config matches actual address
- Verify network connectivity: `ping atlantis.med.harvard.edu`

**"Authentication failed":**
- Verify `manager_token` in Dashboard config matches one in Manager's `auth_tokens`
- No leading/trailing spaces

**Dashboard doesn't update:**
- Check `refresh_interval_seconds` (should be 30 or less)
- Verify Manager is responding: visit `http://atlantis.med.harvard.edu:5000/health`

See [INSTALLATION.md](../INSTALLATION.md) troubleshooting section for more.

---

## Note on UI Improvements

Dashboard UI is minimal (functional). Future iterations will add:
- Charts and visualizations
- Historical trends
- Per-system breakdowns
- Alerts

These will be added once data collection and storage is stable and running smoothly.

---

## See Also

- [INSTALLATION.md](../INSTALLATION.md) - Complete installation guide
- [METRICS.md](../METRICS.md) - Metrics collection system
- [CONFIG.md](../CONFIG.md) - Configuration management
- [ARCHITECTURE.md](../docs/ARCHITECTURE.md) - System design
