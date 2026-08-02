# Manager

Central service that receives usage data from Collectors, stores it, and provides an API for the Dashboard.

## Installation

### Linux/macOS

1. Install Python 3.8+: `sudo apt-get install python3`
2. Install dependencies: `pip3 install -r requirements.txt`
3. Copy `config.example.json` to `config.json` and customize
4. Create data directory: `sudo mkdir -p /var/lib/lab-monitor/data && sudo chown $USER:$USER /var/lib/lab-monitor/data`
5. Run: `python3 manager.py --config config.json`

### Windows Server

1. **Install Python 3.8+**
   - Download from https://www.python.org/downloads/
   - **Important:** Check "Add Python to PATH" during installation
   - Run PowerShell as Administrator
   
2. **Install dependencies**
   ```powershell
   pip install -r requirements.txt
   ```

3. **Prepare directories**
   ```powershell
   # Create data directory (e.g., C:\lab-monitor\data)
   mkdir C:\lab-monitor\data
   mkdir C:\lab-monitor\logs
   ```

4. **Configure**
   - Copy `config.example.json` to `config.json`
   - Edit `config.json`:
     ```json
     {
       "host": "0.0.0.0",
       "port": 5000,
       "data_dir": "C:\\lab-monitor\\data",
       "auth_tokens": ["your-secure-token"],
       "log_file": "C:\\lab-monitor\\logs\\manager.log",
       "debug": false
     }
     ```

5. **Run as Windows Service (Recommended)**
   
   Option A: Using NSSM (Non-Sucking Service Manager)
   ```powershell
   # Download from https://nssm.cc/download
   nssm install LabMonitorManager "C:\Python311\python.exe" "C:\lab-monitor\manager\manager.py --config C:\lab-monitor\config.json"
   nssm start LabMonitorManager
   ```
   
   Option B: Manual startup (for testing)
   ```powershell
   cd C:\lab-monitor\manager
   python manager.py --config config.json
   ```

6. **Configure Windows Firewall**
   ```powershell
   # Allow Manager port (5000)
   netsh advfirewall firewall add rule name="Lab Monitor Manager" dir=in action=allow protocol=tcp localport=5000
   ```

## Configuration

Edit `config.json`:
```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "/var/lib/lab-monitor/data",
  "auth_tokens": ["change-me-token-1", "change-me-token-2"],
  "cors_origins": ["http://localhost:5001", "http://dashboard.internal:5001"],
  "log_file": "/var/log/lab-monitor-manager.log",
  "log_level": "INFO",
  "retention_days": 90,
  "debug": false
}
```

## Configuration Fields

- **host**: IP to listen on (`0.0.0.0` = all interfaces)
- **port**: HTTP port (default: 5000)
- **data_dir**: Directory to store JSONL archives (must be writable)
- **auth_tokens**: Bearer tokens for authentication (change these!)
- **cors_origins**: Allowed origin URLs for Dashboard (security)
- **log_file**: Path to log file
- **log_level**: DEBUG, INFO, WARNING, ERROR (default: INFO)
- **retention_days**: Auto-delete reports older than N days (0 = disabled)
- **debug**: Enable Flask debug mode (never true in production)

## API Endpoints

### Ingest Data (from Collector)
```
POST /api/usage/report
```

Authentication: `Authorization: Bearer <token>` header required

Supports TWO request formats:

**Format 1: Single report**
```json
{
  "nas_name": "triton-01",
  "nas_id": "synology-abc123",
  "timestamp": "2026-08-02T02:00:00Z",
  "folders": [
    {"path": "/volume1/shared", "usage_bytes": 1234567890}
  ]
}
```

**Format 2: Array of reports**
```json
{
  "reports": [
    { "nas_name": "triton-01", ... },
    { "nas_name": "triton-02", ... }
  ]
}
```

Returns: `{"status": "ok", "stored": N}`

### Query Data (for Dashboard)
```
GET /api/usage/all
GET /api/usage/nas/<nas_name>
GET /api/usage/history/<nas_name>?days=30
```

Authentication: `Authorization: Bearer <token>` header required

Returns: Latest usage snapshots for specified NAS(es)

## Data Storage

Usage data stored in `data_dir`:
```
data/
├── nas-01/
│   └── usage.jsonl      # Append-only log of reports
├── nas-02/
│   └── usage.jsonl
└── ...
```

Each line is a complete JSON report with timestamp and folder usage.

## Security Notes

⚠️ **Critical:**
- Change all `auth_tokens` in `config.json`
- Use HTTPS in production (nginx reverse proxy with SSL)
- Restrict `cors_origins` to specific Dashboard URLs
- Don't expose Manager directly to the internet
- Keep credentials secure (use environment variables or secrets management)

## Troubleshooting

### Port already in use
```powershell
# Find what's using port 5000
netstat -ano | findstr :5000

# Change port in config.json
"port": 5001
```

### Permission denied on data directory
- Windows: Right-click folder → Properties → Security → Edit → Give user full access
- Linux: `sudo chown -R $USER:$USER /var/lib/lab-monitor/data`

### Logs growing too fast
- Increase `log_level` from INFO to WARNING
- Implement log rotation (logrotate on Linux, Windows Event Log on Windows)
