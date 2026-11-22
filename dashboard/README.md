# Dashboard

Web interface for viewing system usage statistics (disk, CPU, RAM, network, disk I/O, GPU, per-user activity, who is connected, storage health and storage owners) and their history.

The Dashboard owns no data. Its Flask server polls the Manager's read API on behalf of the browser (the Manager token never reaches the browser) and serves one page that renders a card per system.

## Installation & Setup

**See [INSTALLATION.md](../INSTALLATION.md) for complete setup instructions.**

Run `install-manager-dashboard-taskscheduler.ps1` on the Windows server (atlantis). It installs the Manager and the Dashboard from the same checkout, registers the "Lab Monitor Dashboard" task, and gives the Dashboard its own Manager **read** token, never the admin token. At the end it says whether the Dashboard login is enabled, and how to enable it (see [Security](#security)).

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
    "username": "labadmin",
    "password_hash": "PASTE-OUTPUT-OF: python app.py --hash-password"
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
| `auth` | none | Optional HTTP basic auth for the whole dashboard: `{"username", "password_hash"}` |
| `refresh_interval_seconds` | 30 | Browser poll interval (minimum 5). Also the server-side cache TTL. |
| `cache_ttl_seconds` | = refresh interval | Override for how long one Manager poll is reused |
| `manager_timeout_seconds` | 5 | Timeout for each Manager request |
| `stale_minutes` | 15 | A system with no `system_metrics` for this long is shown as **stale** (amber) |
| `offline_minutes` | 120 | ...and after this long as **offline** (red) |
| `disk_stale_hours` | 36 | The disk snapshot age on a card is flagged after this long (scans run daily) |
| `chart_max_points` | 2000 | Max points per history chart; longer ranges are bucket-averaged |
| `history_max_rows` | 100000 | Row cap (`limit`) sent to the Manager for time-range history queries |
| `threads` | 8 | waitress worker threads |

The usage-metrics release adds no config key: the Dashboard finds out from the Manager's answers which
features it has (see [With an older Manager](#with-an-older-manager)).

The dashboard refuses to start if `auth` is present but incomplete or its `password_hash` is not a hash. It logs a warning at startup if `manager_token` is empty or still a placeholder, and if it is bound to a non-loopback address without `auth`.

---

## Security

- **Manager token:** the dashboard only reads from the Manager. Give it a token from the Manager's `read_tokens` list, not an admin or collector token. A read token cannot ingest data or delete systems if the dashboard host or its config file is compromised. Legacy Manager `auth_tokens` still work but carry full admin rights.
- **Who can see the dashboard:** the page shows host names, user names, client IP addresses, per-user CPU, GPU and storage use, and folder paths, to everyone who can open it (owner decision; see [docs/PRIVACY.md](../docs/PRIVACY.md)). Basic auth is the only access control. When it is bound to `0.0.0.0` (reachable from the network), turn it on:

  ```
  python app.py --hash-password
  ```

  Paste the printed `auth` block into `config.json` and restart. Every page, API route and static file then requires the username/password, except `GET /health`, which returns only `{"status": "ok"}`. Basic auth sends the password with every request, so on an untrusted network either put the dashboard behind an HTTPS reverse proxy or bind it to `127.0.0.1` and reach it through the proxy.
- The browser never sees the Manager token or the Manager URL. All Manager calls go through the dashboard server, which validates system names and URL-encodes every path segment it forwards. A name must match `^[A-Za-z0-9_.-]{1,64}$`, or be exactly a name the Manager's device registry listed in a recent poll (devices an older Manager registered under other names, such as `Lab PC`). Error responses carry short generic messages; details go to the log.
- Plotly is loaded from the CDN at a pinned version (1.58.5) with a Subresource Integrity hash.

---

## Usage

1. **Start the service** (or verify it's running via Task Scheduler)
2. **Open in browser:** `http://atlantis.med.harvard.edu:5001`
3. **View:**
   - Real-time metrics (CPU%, RAM%, network, GPU, per-user activity). Refreshes every 30 seconds while the tab is visible, pauses while it is hidden, and refreshes immediately when you come back.
   - Disk usage by volume and folder, with the age of the last disk scan
   - Click any metric or volume for its history chart (24h / 7d / 30d / 90d / 1y / all / custom range, PNG and CSV export)

### Sections of the usage-metrics release

Each appears only when the Manager and the collector provide its data.

- **System:** the CPU tooltip says `CPU: average over N s` (`interval_s`) or `CPU: 1-s sample` (older rows, a
  first run), and lists logical CPUs, steal % when above 0 and the per-process coverage. `(avg 2h 10m)` appears
  when the interval was longer than 600 s. An **I/O wait** tile appears when it was measured.
- **Disk I/O** (every card; hidden when no disk field was measured): Read and Write in decimal B/s, KB/s or
  MB/s (IOPS in the tooltip), **Busiest** disk % with its name (amber at 60 %, red at 85 %) and **Latency** in
  ms. Each tile opens its history chart.
- **User Activity** (servers only; NAS cards still hide it): a **Cores** column when per-user CPU is measured
  (tooltip `N CPU-seconds in M s`), and a **GPU** column when some user holds a GPU, e.g.
  `1× 87% · 20.00 GiB`: busy % coloured at 70 / 90 %, VRAM by its share of the host's VRAM at 75 / 90 %. `—` =
  not attributed; an empty grey cell = holds no GPU. Clicking CPU, Cores, RAM or the GPU values opens that
  user's history. Users whose row is more than 120 s older than the system's latest metrics are hidden (they
  logged off; the Manager keeps their last row). On screens 480 px wide or less the GPU cell moves to a line
  under the row.
- **Last 24 h** under User Activity: the top 5 users by GPU busy hours, then held hours
  (`alice 18.2 h busy / 23.9 h held`), when someone used a GPU in the window, otherwise by CPU core-hours
  (`alice 12.3 core-h`). A user who held a GPU whose use could never be attributed (no utilisation reading,
  or a busy GPU shared without process samples, common under Windows WDDM) is still listed, after the known
  busy totals, as `bob — busy / 2.0 h held`; busy hours count only the runs that could be attributed. The
  tooltip says so, and notes when the rollups are still being built.
- **GPU rows:** a line under each GPU, e.g. `alice 20.00 GiB 87% · bob 1.00 GiB 3% · +2`; the tooltip lists
  every user with the full `DOMAIN\name`, plus "other" for VRAM of unknown or excluded owners.
- **Connected** (every card, NAS included): `Connected — N clients · M users` (the tooltip shows each source's
  status), chips per service such as `SMB 7` (zero counts greyed; a chip opens its `<svc>_clients` history),
  and rows User / Client / Via / Since. Named users come first; the list scrolls after 8 rows. `—` in User has
  a tooltip with the reason (e.g. SMB user names need root access). With `sessions.detail: "counts"` only the
  chips appear.
- **Storage health** at the top of the Storage section: a pill (Healthy, Warning, Critical, Unknown) with the
  first reason, or e.g. `Healthy · 4 disks · md2 clean · 34–41 °C`; up to 5 rows for warning and critical
  components; temperature chips that open the disk's temperature history (only for disks the collector keeps
  history for: not USB disks, only the first 48 others, and one disk per history key). The summary-strip button shows ⚠
  for warning or ✖ for critical.
- **Storage owners:** each scanned folder gets a thin bar for the share not modified for 3 years or more (dark)
  and 1–3 years (mid); clicking it loads that folder's owner list (not on Windows, which has no owners). **Top
  storage users (scanned folders)** lists the top 5 owners with size, share and share not modified for a year;
  `+N more` expands it. Clicking an owner opens their storage history (default range All).
- **Charts:** when the Manager answers with aggregated points (hourly or daily rollups, or downsampled data
  with min/max), the chart shades the min–max band behind the average line and hover shows `avg (min–max)`.
  The info line reads e.g. `Hourly averages in 5-hour buckets · 1,752 points from 105,120 measurements · shaded
  = min–max`. CSV export then has `timestamp,value_avg,value_min,value_max,count`, and the file name gains
  `_hourly` or `_daily` (per-user charts also the user name).

### With an older Manager

Against an OLD (`f086e7e`) or CURRENT (`e5cd699`, `160da09` or `fb6498c`) Manager the Dashboard works as before. The new endpoints
answer 404 (the OLD Manager answers `/api/user_metrics/usage` with 200 `[]`, which counts the same), which is
**not** an error: no banner, the poll is not degraded, and the new sections are simply not shown. Charts have
no band. Per-user cells don't open charts, and a per-user chart request answers 404 `Per-user history needs a
newer Manager` instead of a series that mixes every user. These 404s are logged at DEBUG only.

After the Manager is upgraded without restarting the Dashboard, the cached 404s keep the new sections hidden
for up to 10 minutes (storage owners), 5 minutes (usage totals) or 1 minute (storage health). The installer
restarts both, which clears the caches.

### What the indicators mean

| Indicator | Meaning |
|-----------|---------|
| Red banner "Manager unreachable" | The dashboard couldn't get the device list from the Manager. The last good data stays on screen, greyed out, until the Manager answers again. |
| Red banner "Dashboard server unreachable" | The browser can't reach the dashboard server itself. With "(HTTP 502/503/504)" a reverse proxy answered but the dashboard process behind it didn't. |
| Red banner "Dashboard server not responding" | A request to the dashboard server got no answer within 4 × `manager_timeout_seconds` + 5 s (at least 15 s) and was abandoned. Polling continues. |
| Amber banner "Some Manager data is unavailable" | Some Manager calls failed (listed in the banner). The card sections those calls supply keep their last good values, greyed out, with a "Showing … from an earlier refresh" note. Everything else on the page is current. |
| Grey dashed card / **Unknown** badge | The Manager's metrics call failed and the page has no earlier copy for this system (for example, the first load). This is not evidence that the system is offline. |
| Small notice "Some data could not be displayed" | A frontend rendering error (details in the browser console), not a connection problem |
| Connected list greyed with "as of hh:mm" | The list was last confirmed more than 15 minutes ago (it is resent when it changes and at least hourly). After 60 minutes it is hidden: "List not reported since …". The counts grey out after 15 minutes. |
| "List shortened by the collector; the counts above are complete" | More rows than `sessions.max_entries` |
| Storage health line greyed "(as of …)" | The last storage-health message is older than 3 hours |
| Shaded band around a chart line | The points are averages; the band is the min–max of the values behind each point |
| Amber card / **Stale** badge | No `system_metrics` for more than `stale_minutes` |
| Red card / **Offline** badge | No `system_metrics` for more than `offline_minutes` |
| "Disk scan N h ago (expected daily)" in amber | The disk snapshot is older than `disk_stale_hours` |
| "N folder(s) timed out / could not be scanned" | The collector couldn't measure those folders in the last scan. They are left out, not shown as 0. |
| `—` | Not measured. The collector couldn't read this value; it isn't zero. |

Card and summary-strip colours use the same thresholds: CPU/RAM amber at 50% and red at 75%; volumes amber at 75% and red at 90%. Byte values use binary units (KiB, MiB, GiB, TiB = 1024-based).

---

## Dashboard HTTP API

These routes are used by the page. With `auth` configured, they require basic auth too.

| Route | Description |
|-------|-------------|
| `GET /` | The dashboard page |
| `GET /health` | Liveness probe: `{"status": "ok"}`. No Manager call, no system data, no basic auth. Used by `tools/restart_manager_dashboard.py`. |
| `GET /api/data` | Aggregated state of all systems (cached for one refresh interval) |
| `GET /api/history/<system>/<data_type>/<field>` | Time series for one field. Query: `from`, `to` (ISO 8601), `max_points`, `limit`; `entity` (one user of `user_metrics`, 1–256 printable characters) and `resolution` (`auto`, `raw`, `hour`, `day`), forwarded to the Manager, else 400 |
| `GET /api/storage/breakdown/<system>?folder=<path>` | The Manager's latest storage breakdown of one system with the full owner rows of that folder (every folder without `folder`). `folder` is at most 4096 characters with no `.` / `..` segments (else 400). Passes the Manager's 404 through (older Manager, or no breakdown for that system); 502 on other Manager errors. |

`/api/data` fetches the Manager's `/api/devices`, `/api/metrics/all`, `/api/usage/all`, `/api/totals`, `/api/user_metrics/all`, `/api/sessions/all`, `/api/storage/health/all`, `/api/storage/breakdown/all` and `/api/user_metrics/usage` concurrently and returns:

- **200** `{"success": true, "degraded": false, "errors": [], "timestamp", "systems", "global_totals", "features"}`
- **200** with `"degraded": true` and `"errors": [{"endpoint", "error"}, ...]` when some calls failed but `/api/devices` worked
- **502** `{"success": false, "error": "Manager unavailable", "errors": [...]}` when `/api/devices` failed

A healthy result is cached for one refresh interval. Failed and degraded results are cached for at most 5 s, so recovery shows up quickly.

`features` is `{"sessions", "storage_health", "breakdown", "usage"}`: `true` if the Manager answered, `false` if it lacks the endpoint (404), `null` if the call failed this poll (the page then keeps the last answer). A system gains the keys `sessions`, `storage_health`, `breakdown` and `usage` only when the Manager reports them, and `metrics` gains the new `system_metrics` keys only when the Manager sends them, so the response for old data is unchanged. A failure of one of the four newer calls other than 404 makes the poll degraded as before; its section stays greyed with its last good value ("connections", "storage health", "storage owners", "usage totals").

The slow-changing endpoints are fetched at most once per TTL, whatever the refresh interval: `/api/storage/breakdown/all` every 600 s, `/api/user_metrics/usage` every 300 s, `/api/storage/health/all` every 60 s (`/api/sessions/all` every refresh). A 200 or 404 answer is cached for the TTL; a failure never is, so the next poll retries.

`/api/history` forwards `max_points` (default `chart_max_points`) so the Manager can bucket-average long ranges (`"downsampled": true`); `"truncated": true` means the row `limit` cut the result. If the Manager is older and ignores `max_points`, the dashboard downsamples the response itself and sets `"downsampled": true` and `"downsampled_from": N`; points that carry `min`, `max` and `count` keep the min of mins, the max of maxes and the summed count, with a count-weighted mean. The field may be up to 4400 characters for `storage_breakdown` (folder paths), 512 otherwise. With `entity`, an answer without a non-null `entity` (an older Manager that ignored it) becomes 404 `Per-user history needs a newer Manager`.

---

## Troubleshooting

**"Manager unreachable":**
- Verify Manager is running: `tasklist | findstr manager`
- Check Manager URL in config matches actual address
- Verify network connectivity: `ping atlantis.med.harvard.edu`
- The banner lists the failing calls. `HTTP 401` means the Manager rejected `manager_token`. `HTTP 403` means the token isn't allowed to read (for example, a collector token).

**Browser keeps asking for a password:**
- Regenerate the hash with `python app.py --hash-password` and check the `auth.username` spelling

**Dashboard doesn't update:**
- Check `refresh_interval_seconds` (should be 30 or less)
- Verify Manager is responding: visit `http://atlantis.med.harvard.edu:5000/health`
- Polling pauses while the browser tab is hidden. Switching back to the tab refreshes immediately.

**Log says "using the Flask DEVELOPMENT server":**
- `pip install waitress` (it's in `requirements.txt`), or set `"debug": false`

See [INSTALLATION.md](../INSTALLATION.md) troubleshooting section for more.

---

## Upgrade notes

### Usage-metrics release

- **Order:** the installer upgrades the Manager and the Dashboard together, then come the collectors. The new
  Dashboard works with the OLD, CURRENT and new Managers (see [With an older Manager](#with-an-older-manager)),
  and the CURRENT Dashboard works with the new Manager (it ignores the new keys).
- No config change. Existing configs keep working.
- One intended change for old data: User Activity hides users whose row is more than 120 s older than the
  system's latest metrics (they logged off; before, their last row stayed on the card). Rows are kept when
  either timestamp is missing or unreadable.
- Each refresh makes up to four more Manager calls, three of them cached for 1–10 minutes.

### Review-fix release

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
