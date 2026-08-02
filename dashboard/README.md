# Dashboard

Web interface for viewing NAS usage statistics and trends.

## Installation

### Linux/macOS

1. Install Python 3.8+: `sudo apt-get install python3`
2. Install dependencies: `pip3 install -r requirements.txt`
3. Copy `config.example.json` to `config.json` and customize
4. Run: `python3 app.py --config config.json`
5. Open `http://localhost:5001` in your browser

### Windows Server

1. **Install Python 3.8+**
   - Download from https://www.python.org/downloads/
   - **Important:** Check "Add Python to PATH" during installation
   - Run PowerShell as Administrator
   
2. **Install dependencies**
   ```powershell
   pip install -r requirements.txt
   ```

3. **Configure**
   - Copy `config.example.json` to `config.json`
   - Edit `config.json`:
     ```json
     {
       "host": "0.0.0.0",
       "port": 5001,
       "manager_url": "http://manager.internal:5000",
       "manager_token": "your-bearer-token-here",
       "refresh_interval_seconds": 30,
       "manager_timeout_seconds": 5,
       "log_file": "C:\\lab-monitor\\logs\\dashboard.log",
       "log_level": "INFO",
       "debug": false
     }
     ```

4. **Run as Windows Service (Recommended)**
   
   Option A: Using NSSM
   ```powershell
   nssm install LabMonitorDashboard "C:\Python311\python.exe" "C:\lab-monitor\dashboard\app.py --config C:\lab-monitor\config.json"
   nssm start LabMonitorDashboard
   ```
   
   Option B: Manual startup (for testing)
   ```powershell
   cd C:\lab-monitor\dashboard
   python app.py --config config.json
   ```

5. **Configure Windows Firewall**
   ```powershell
   # Allow Dashboard port (5001)
   netsh advfirewall firewall add rule name="Lab Monitor Dashboard" dir=in action=allow protocol=tcp localport=5001
   ```

6. **Access Dashboard**
   - Open `http://localhost:5001` (local)
   - Or `http://<server-ip>:5001` from other machines

## Configuration

Edit `config.json`:
```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://manager.internal:5000",
  "manager_token": "change-me-token",
  "refresh_interval_seconds": 30,
  "manager_timeout_seconds": 5,
  "log_file": "/var/log/lab-monitor-dashboard.log",
  "log_level": "INFO",
  "debug": false
}
```

## Configuration Fields

- **host**: IP to listen on (`0.0.0.0` = all interfaces)
- **port**: HTTP port (default: 5001)
- **manager_url**: URL to Manager service (e.g., `http://manager.internal:5000`)
- **manager_token**: Bearer token for Manager authentication (must match Manager config)
- **refresh_interval_seconds**: How often to update dashboard from Manager (default: 30)
- **manager_timeout_seconds**: Timeout for Manager API calls (default: 5)
- **log_file**: Path to log file
- **log_level**: DEBUG, INFO, WARNING, ERROR (default: INFO)
- **debug**: Enable Flask debug mode (never true in production)

## Features

- Real-time NAS usage display (refreshes every 30 seconds)
- Current usage for all NAS systems
- Per-folder breakdown
- Connection status indicator
- Modal detail view for each NAS

## UI Components

- **Header**: Status indicator and last update time
- **Summary**: NAS count and total usage
- **NAS Cards**: Grid of all NAS systems with:
  - NAS name and ID
  - Total usage and bar chart (estimated capacity 100TB)
  - Top 5 folders with sizes
- **Detail Modal**: Full folder list when clicking a card

## Security

- **Authentication:** Dashboard uses Bearer token to authenticate with Manager
- **Manager must provide the same token** in its config
- **Test connectivity:** If dashboard shows "Unable to reach Manager", check:
  - Manager is running: `curl http://manager.internal:5000/health`
  - Token matches: `manager_token` in Dashboard must match `auth_tokens` in Manager
  - Firewall: Is port 5000 open from Dashboard to Manager?

## Development

Static files in `static/` (CSS, JS)
Templates in `templates/` (HTML)

### Customizing Capacity Estimate

In `static/js/dashboard.js`, the `createNasCard()` function has:
```javascript
const estimatedCapacity = 100 * 1024 * 1024 * 1024 * 1024; // 100 TB estimate
```

Change this to match your actual NAS capacity. Ideally, Manager would provide actual capacity info, but for now this is an estimate.

## Troubleshooting

### Dashboard shows "Unable to reach Manager"
- Check Manager is running: `curl http://manager.internal:5000/health`
- Check token: Does `manager_token` in Dashboard match an entry in Manager's `auth_tokens`?
- Check network: Can Dashboard reach Manager's URL?
- Check logs: `tail -f <log_file>`

### No NAS systems showing
- Collectors haven't reported yet (daily at 2 AM)
- Manager hasn't received reports
- Check Manager logs: Look for "Stored report from"

### Usage bar shows 0%
- Estimated capacity (100TB) is much larger than actual usage
- This is normal for small installations
- Consider reducing `estimatedCapacity` in `dashboard.js`
