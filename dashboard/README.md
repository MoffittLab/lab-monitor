# Dashboard

Web interface for viewing system usage statistics (disk, CPU, RAM, network, GPU, per-user activity) and their history.

The Dashboard owns no data. Its Flask server polls the Manager's read API on behalf of the browser (the Manager token never reaches the browser) and serves one page that renders a card per system.

## Installation & Setup

**See [INSTALLATION.md](../INSTALLATION.md) for complete setup instructions.**

Run `install-manager-dashboard-taskscheduler.ps1` on the Windows server (atlantis). It installs the Manager and the Dashboard from the same checkout, registers the "Lab Monitor Dashboard" task, and gives the Dashboard its own Manager **read** token, never the admin token. At the end it says whether the Dashboard sign-in is enabled, and how to enable it (see [Security](#security)).

Requirements: `pip install -r requirements.txt` (Flask, requests, waitress). The dashboard runs under **waitress** when it is installed. Without waitress, or with `"debug": true`, it falls back to the Flask development server and logs a loud warning.

---

## Configuration

Dashboard reads `config.json` in its directory (see `config.example.json`):

```json
{
  "host": "0.0.0.0",
  "port": 5001,
  "manager_url": "http://localhost:5000",
  "manager_token": "PASTE-A-MANAGER-READ-TOKEN-HERE",
  "auth": {
    "password_hash": "PASTE-OUTPUT-OF: python app.py --hash-password",
    "remember_days": 180
  },
  "refresh_interval_seconds": 30,
  "manager_timeout_seconds": 5,
  "stale_minutes": 15,
  "offline_minutes": 120,
  "disk_stale_hours": 36,
  "chart_max_points": 2000,
  "threads": 8,
  "log_file": "E:\\Users\\lab-monitor\\logs\\dashboard.log",
  "log_level": "INFO",
  "debug": false
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `host` / `port` | `0.0.0.0` / `5001` | Bind address. See **Security** before using `0.0.0.0`. |
| `manager_url` | `http://localhost:5000` | Manager base URL |
| `manager_token` | — | Bearer token for the Manager. Use a **read token** (one of the Manager's `read_tokens`). |
| `auth` | none | Optional sign-in for the whole dashboard: `{"password_hash"}` (see [Security](#security)) |
| `auth.remember_days` | 180 | How long a browser stays signed in after its last visit (1 to 3650) |
| `auth.session_key_file` | `dashboard/.session_key` | Where the key that signs the sign-in cookies is kept |
| `auth.cookie_secure` | `false` | Set `true` behind an HTTPS proxy that doesn't tell the dashboard the request was HTTPS |
| `refresh_interval_seconds` | 30 | Browser poll interval (minimum 5). Also the server-side cache TTL. |
| `cache_ttl_seconds` | = refresh interval | Override for how long one Manager poll is reused |
| `manager_timeout_seconds` | 5 | Timeout for each Manager request |
| `stale_minutes` | 15 | A system with no `system_metrics` for this long is shown as **stale** (amber) |
| `offline_minutes` | 120 | ...and after this long as **offline** (red) |
| `disk_stale_hours` | 36 | The disk snapshot age on a card is flagged after this long (scans run daily) |
| `chart_max_points` | 2000 | Max points per history chart; longer ranges are bucket-averaged |
| `history_max_rows` | 100000 | Row cap (`limit`) sent to the Manager for time-range history queries |
| `threads` | 8 | waitress worker threads |

The dashboard refuses to start if `auth` is present but incomplete or its `password_hash` is not a hash. It logs a warning at startup if `manager_token` is empty or still a placeholder, and if it is bound to a non-loopback address without `auth`.

---

## Security

- **Manager token:** the dashboard only reads from the Manager. Give it a token from the Manager's `read_tokens` list, not an admin or collector token. A read token cannot ingest data or delete systems if the dashboard host or its config file is compromised. Legacy Manager `auth_tokens` still work but carry full admin rights.
- **Who can see the dashboard:** the page shows host names, user names, and folder paths. When it is bound to `0.0.0.0` (reachable from the network), turn on the sign-in:

  ```
  python app.py --hash-password
  ```

  Paste the printed `auth` block into `config.json` and restart. Every page and API route then needs a signed-in browser. The exceptions hold no system data: `GET /health` (returns only `{"status": "ok"}`), the sign-in page, and the static files (stylesheet, script, images).

  **How the sign-in works**
  - There is one shared password and no user names. The config holds only its hash.
  - A correct password gives that browser a cookie that keeps it signed in for `remember_days` (180 by default). The time counts from the browser's last visit, so a computer in regular use is never asked again. A private window, another browser, or clearing the cookies means signing in again.
  - **Sign out** in the page header signs out that browser only.
  - **To change the password**, run `--hash-password` again, replace the hash and restart: every browser is signed out. To sign everyone out without changing it, delete the key file (`dashboard/.session_key`) and restart. A restart alone signs nobody out.
  - After 5 wrong passwords within 15 minutes from one address, sign-in from that address is refused for 15 minutes. Browsers that are already signed in are not affected. Wrong passwords and sign-ins are logged with the client address.
  - The cookie holds no secret: an issue time, a random value and a signature (HMAC-SHA256). The signing key is derived from the key file and the password hash. The key file is created on first start; protect it like `config.json`, since whoever can read both can make a valid cookie.

  **Limits**
  - The dashboard itself speaks plain HTTP: the password crosses the network unencrypted once per sign-in, and the cookie with every request. On a network you don't trust, put the dashboard behind an HTTPS reverse proxy (or bind it to `127.0.0.1` and reach it through the proxy); the cookie is then marked `Secure`.
  - Behind a proxy every client has the proxy's address, so the wrong-password lockout applies to all of them together.
  - The earlier `auth.username` setting and HTTP basic auth are gone; a `username` left in the config is ignored with a startup warning.
- The browser never sees the Manager token or the Manager URL. All Manager calls go through the dashboard server, which validates system names and URL-encodes every path segment it forwards. A name must match `^[A-Za-z0-9_.-]{1,64}$`, or be exactly a name the Manager's device registry listed in a recent poll (devices an older Manager registered under other names, such as `Lab PC`). Error responses carry short generic messages; details go to the log.
- Plotly is loaded from the CDN at a pinned version (1.58.5) with a Subresource Integrity hash.

---

## Usage

1. **Start the service** (or verify it's running via Task Scheduler)
2. **Open in browser:** `http://atlantis.med.harvard.edu:5001`
3. **View:**
   - Real-time metrics (CPU%, RAM%, network, GPU, per-user activity). Refreshes every 30 seconds while the tab is visible, pauses while it is hidden, and refreshes immediately when you come back.
   - Disk usage by volume and folder, with the age of the last disk scan
   - Click any metric or volume for its history chart (24h / 7d / 30d / all / custom range, PNG and CSV export)

### What the indicators mean

| Indicator | Meaning |
|-----------|---------|
| Red banner "Manager unreachable" | The dashboard couldn't get the device list from the Manager. The last good data stays on screen, greyed out, until the Manager answers again. |
| Red banner "Dashboard server unreachable" | The browser can't reach the dashboard server itself. With "(HTTP 502/503/504)" a reverse proxy answered but the dashboard process behind it didn't. |
| Red banner "Dashboard server not responding" | A request to the dashboard server got no answer within 4 × `manager_timeout_seconds` + 5 s (at least 15 s) and was abandoned. Polling continues. |
| Amber banner "Some Manager data is unavailable" | Some Manager calls failed (listed in the banner). The card sections those calls supply keep their last good values, greyed out, with a "Showing … from an earlier refresh" note. Everything else on the page is current. |
| Grey dashed card / **Unknown** badge | The Manager's metrics call failed and the page has no earlier copy for this system (for example, the first load). This is not evidence that the system is offline. |
| Small notice "Some data could not be displayed" | A frontend rendering error (details in the browser console), not a connection problem |
| Amber card / **Stale** badge | No `system_metrics` for more than `stale_minutes` |
| Red card / **Offline** badge | No `system_metrics` for more than `offline_minutes` |
| "Disk scan N h ago (expected daily)" in amber | The disk snapshot is older than `disk_stale_hours` |
| "N folder(s) timed out / could not be scanned" | The collector couldn't measure those folders in the last scan. They are left out, not shown as 0. |
| `—` | Not measured. The collector couldn't read this value; it isn't zero. |

Card and summary-strip colours use the same thresholds: CPU/RAM amber at 50% and red at 75%; volumes amber at 75% and red at 90%. Byte values use binary units (KiB, MiB, GiB, TiB = 1024-based).

---

## Dashboard HTTP API

These routes are used by the page. With `auth` configured, a browser that is not signed in is redirected from `/` to `/login`, and the `/api/` routes answer 401 `{"success": false, "error": "Sign-in required", "login": "/login"}`, on which the page opens the sign-in page.

| Route | Description |
|-------|-------------|
| `GET /` | The dashboard page |
| `GET /login`, `POST /login` | The sign-in page and its form (`password`, `next`). Only with `auth`. |
| `POST /logout` | Signs this browser out |
| `GET /health` | Liveness probe: `{"status": "ok"}`. No Manager call, no system data, no sign-in. Used by `tools/restart_manager_dashboard.py`. |
| `GET /api/data` | Aggregated state of all systems (cached for one refresh interval) |
| `GET /api/history/<system>/<data_type>/<field>` | Time series for one field. Query: `from`, `to` (ISO 8601), `max_points`, `limit` |

`/api/data` fetches the Manager's `/api/devices`, `/api/metrics/all`, `/api/usage/all`, `/api/totals` and `/api/user_metrics/all` concurrently and returns:

- **200** `{"success": true, "degraded": false, "errors": [], "timestamp", "systems", "global_totals"}`
- **200** with `"degraded": true` and `"errors": [{"endpoint", "error"}, ...]` when some calls failed but `/api/devices` worked
- **502** `{"success": false, "error": "Manager unavailable", "errors": [...]}` when `/api/devices` failed

A healthy result is cached for one refresh interval. Failed and degraded results are cached for at most 5 s, so recovery shows up quickly.

`/api/history` forwards `max_points` (default `chart_max_points`) so the Manager can bucket-average long ranges (`"downsampled": true`); `"truncated": true` means the row `limit` cut the result. If the Manager is older and ignores `max_points`, the dashboard downsamples the response itself and sets `"downsampled": true` and `"downsampled_from": N`.

---

## Troubleshooting

**"Manager unreachable":**
- Verify Manager is running: `tasklist | findstr manager`
- Check Manager URL in config matches actual address
- Verify network connectivity: `ping atlantis.med.harvard.edu`
- The banner lists the failing calls. `HTTP 401` means the Manager rejected `manager_token`. `HTTP 403` means the token isn't allowed to read (for example, a collector token).

**The password is not accepted:**
- Regenerate the hash with `python app.py --hash-password`, replace `auth.password_hash` and restart
- "Too many wrong passwords": wait 15 minutes, or restart the Dashboard (the count is kept in memory)

**A browser is asked for the password again:**
- It was not used for longer than `remember_days`, its cookies were cleared, or it is a private window
- The password hash or the key file changed (both sign every browser out)
- The log says "Cannot save the sign-in key": the key file's folder is not writable, so a new key is made at every start. Make it writable, or set `auth.session_key_file`

**Dashboard doesn't update:**
- Check `refresh_interval_seconds` (should be 30 or less)
- Verify Manager is responding: visit `http://atlantis.med.harvard.edu:5000/health`
- Polling pauses while the browser tab is hidden. Switching back to the tab refreshes immediately.

**Log says "using the Flask DEVELOPMENT server":**
- `pip install waitress` (it's in `requirements.txt`), or set `"debug": false`

See [INSTALLATION.md](../INSTALLATION.md) troubleshooting section for more.

---

## Upgrade notes

- **Rollout order: Dashboard, then Manager, then collectors.** The installer upgrades the Dashboard and the Manager together, which is fine. Upgrade the collectors last:
  - The new dashboard works with the old Manager. It downsamples history itself when the Manager ignores `max_points`.
  - The old dashboard works with the new Manager. Its "All Available" chart sends no `max_points`, and the new Manager bucket-averages such window queries instead of cutting them off.
  - New collectors need the new dashboard. The old one treats their new `_scan_seconds` folder_usage key as a volume and shows a bogus `_scan_seconds` row on every card that has disk data.
  - New collectors also need the new Manager (see `manager/README.md`, "Upgrading").
- History and devices stored by the old Manager show up unchanged. A device the old Manager registered under a name outside `^[A-Za-z0-9_.-]{1,64}$` keeps its card and its charts. Two spellings of one device left by the old Manager (for example `Triton` and `triton`) show as two cards, each with its own totals.
- Existing configs keep working unchanged. New keys are optional, and without `auth` the dashboard stays open as before, now with a startup warning.
- Replace the dashboard's `manager_token` with a Manager **read** token once the Manager has `read_tokens` configured.
- Install `waitress` (`pip install -r requirements.txt`) to get the production server.
- The removed routes `/api/system/<name>/disk_history` and `/api/system/<name>/metrics_history` weren't used by the page.

---

## See Also

- [DASHBOARD-IMPROVEMENTS.md](../DASHBOARD-IMPROVEMENTS.md) - History charts: what is implemented
- [INSTALLATION.md](../INSTALLATION.md) - Complete installation guide
- [METRICS.md](../METRICS.md) - Metrics collection system
- [CONFIG.md](../CONFIG.md) - Configuration management
- [ARCHITECTURE.md](../docs/ARCHITECTURE.md) - System design
