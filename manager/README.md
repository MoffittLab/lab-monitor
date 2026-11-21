# Manager — API Reference

Central Flask service that receives data from collectors, stores it, and serves a REST API to the Dashboard. It runs under [waitress](https://docs.pylonsproject.org/projects/waitress/) when installed (the installer installs it); without waitress it falls back to the Flask development server and logs a loud warning.

See [INSTALLATION.md](../INSTALLATION.md) for setup instructions.

---

## Configuration

`config.json` in the manager directory (see `config.example.json`):

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "E:\\Users\\lab-monitor\\data",

  "admin_tokens": ["<random admin token>"],
  "read_tokens": ["<random read token for the Dashboard>"],
  "collector_tokens": {
    "Triton": "<random token for Triton>",
    "Atlas":  "<random token for Atlas>"
  },

  "cors_origins": [],
  "max_request_bytes": 33554432,
  "max_messages_per_request": 10000,
  "history_retention_days": null,
  "threads": 8,

  "log_file": "E:\\Users\\lab-monitor\\logs\\manager.log",
  "log_level": "INFO",
  "debug": false
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `admin_tokens` | `[]` | Full access, including `DELETE /api/system/<name>` and `/api/dead_letter`. Use for tools such as `tools/reset_system.py`. |
| `read_tokens` | `[]` | GET endpoints only. Put one of these in the Dashboard's `manager_token`. |
| `collector_tokens` | `{}` | `{"<device name>": "<token>"}`. A collector token may only `POST /api/data/queue`, and only for its own device. The key `"*"` allows any device (explicit opt-in; logged as a warning at startup). |
| `auth_tokens` | `[]` | **Legacy.** Still accepted, with full admin + any-device write rights. A deprecation warning is logged at startup. |
| `cors_origins` | `[]` | Browser origins allowed to call the API. Off by default because the Dashboard calls the Manager server-side. |
| `max_request_bytes` | 32 MiB | Larger request bodies get `413` (JSON error body; under waitress, a body far over the limit, more than twice it and at least 1 MiB over, is refused by waitress itself with a plain-text `413`). Old collectors post their whole queue in one request, so one whose backlog grows past this limit gets `413` on every run until you raise the limit or upgrade the collector. See [Upgrading](#upgrading-from-the-previous-manager). |
| `max_messages_per_request` | 10000 | More messages in one POST get `413`, but only when the messages carry `header.msg_id` (new collectors, which send at most `max_batch_messages`, default 100, per request). Requests from old collectors (no `msg_id`) are exempt because they can't split their queue; `max_request_bytes` still applies to them. |
| `history_retention_days` | `null` | `null` keeps history forever. A number N makes the Manager archive rows older than N days to JSONL once a day and then delete them (see [Retention](#retention)). |
| `retention_days` | — | Legacy. It only applied to the old `usage.jsonl` reports, which the Manager no longer writes, so it has no effect. It does **not** affect the history databases. |
| `threads` | 8 | waitress worker threads. |
| `log_file` | none | Rotating log file (10 MiB × 5; `log_max_bytes`, `log_backup_count`). Every module logs here. |
| `data_dir` | `data` | Where `metrics.db` and the per-system databases live. A UNC path (`\\server\share\...`) works, but see [Running from Task Scheduler](#running-from-task-scheduler). Mapped drive letters don't work for scheduled tasks. |
| `log_level` | `INFO` | Level for the log file. The console gets `WARNING` and above when a log file is set (`console_log_level` to change). |

**The Manager refuses to start** (exit code 2) if any configured token is a placeholder (contains `CHANGE-ME`, `YOUR-`, `GENERATE-RANDOM`, `PLACEHOLDER`, …) or is shorter than 16 characters, or if no tokens are configured at all. `python manager.py --config config.json --check-config` validates the config and exits without starting.

Generate a token: `python -c "import secrets; print(secrets.token_hex(32))"`.

All token comparisons are constant-time (`hmac.compare_digest`). See [CONFIG.md](../CONFIG.md) for security guidance.

---

## Storage

### Per-system history: `data/<name>/data.db`

One SQLite database per system. The directory name is the normalized device name (lowercase; anything other than `a-z 0-9 _` becomes `_`), so `Lab-A` and `lab-a` share `data/lab_a/`. The central DB resolves names the same way: data for `lab-a` is filed under whichever spelling registered first.

```
data/
└── triton/
    ├── data.db
    │   ├── folder_usage_msgs    ← one row per daily disk run (msg_id, timestamp, metadata)
    │   ├── folder_usage_items   ← one row per (run, folder/volume key) → bytes
    │   ├── folder_usage_legacy  ← pre-upgrade wide table, kept after migration
    │   ├── system_metrics       ← one row per 5-min metrics run
    │   ├── user_metrics         ← one row per user per metrics run
    │   └── not_specified        ← messages with no data_type label
    └── archive/                 ← only if history retention is enabled
```

**folder_usage** is stored narrow (one row per folder) so shares with thousands of folders don't hit SQLite's 2000-column limit. The read API reassembles each run into the same flat dict as before. On first open, an existing wide `folder_usage` table is copied into the narrow tables in one transaction and renamed to `folder_usage_legacy`. It is never dropped, so you can delete it by hand once you have checked the migration.

**Other data types** get one column per payload key. When a message has a key the table hasn't seen, the column is added. Earlier rows read back `NULL` for it, meaning "not measured", which is different from a measured zero.
- SQLite column names are case-insensitive. A key that differs only in case from an existing column is stored in that column. A payload with two keys that differ only in case is rejected.
- Payload keys that collide with internal columns (`id`, `timestamp`, `device_type`, `msg_id`) are stored as `payload_<key>`.
- `data_type` must match `[A-Za-z][A-Za-z0-9_-]{0,63}` and must not start with `sqlite_` or `folder_usage_`.

Every history table has a `msg_id` column with a UNIQUE index. Rows stored before the upgrade have `msg_id = NULL`.

Timestamps are stored as fixed-width UTC, e.g. `2026-08-04T07:00:00.000000Z`, so they sort correctly as strings. Incoming timestamps with offsets are converted to UTC. Timestamps without an offset are taken as UTC.

### Central summary: `metrics.db`

| Table | Contents | Key |
|-------|----------|-----|
| `devices` | Name, system_id, device_type, first_seen, last_seen | `name` |
| `device_snapshot` | Latest cpu_percent, ram_percent, uptime, bandwidth, disk total, folder data | `name` |
| `device_totals` | Lifetime network bytes, current disk total | `name` |
| `device_user_snapshot`, `user_metrics_history` | Per-user CPU/RAM (latest + history, `msg_id` UNIQUE) | |
| `dead_letter` | Rejected messages: raw JSON, reason, msg_id, received_at | `id` |

The snapshot tables and `devices.last_seen` follow the **newest** message, not the last one stored: an older message stored later (a gap-fill with `tools/replay_archives.py`, or a retry of an `error` result) adds history rows but does not roll the snapshot, `last_seen` or the disk total back. It can move `first_seen` earlier.

Network totals: a message adds `current − last` for each counter. If a counter went down (reboot or reset), it adds `current`, the traffic since boot. Nothing is added when either counter is `null` or missing, or when the message is not newer than the last network update (replay or out-of-order).

Unmeasured values are `null` everywhere, never `0`. Non-finite numbers (`NaN`, `Infinity`) are treated as unmeasured too.

---

## API Endpoints

All endpoints except `/health` require `Authorization: Bearer <token>`. Errors are `{"status": "error", "error": "<message>"}`:

| Status | Meaning |
|--------|---------|
| 400 | Malformed body or invalid parameter / name |
| 401 | Missing or unknown token |
| 403 | Token valid but not allowed for this endpoint or device |
| 404 | Unknown system (read endpoints never create directories or databases) |
| 413 | Body larger than `max_request_bytes`, or more than `max_messages_per_request` messages that carry `msg_id` |

Device names everywhere must match `^[A-Za-z0-9_.-]{1,64}$`.

### Ingest

#### `POST /api/data/queue` (collector token for that device, or admin)

```json
{
  "queue_id": "Triton-3f9a1c2b7d4e",
  "name":     "Triton",
  "id":       "synology-triton",
  "messages": [
    {
      "header": {
        "msg_id":      "5b0d6c1e9f8a4c3b9e2d7a6f1c0b4e3d",
        "device_name": "Triton",
        "device_id":   "synology-triton",
        "device_type": "synology",
        "timestamp":   "2026-08-04T07:00:00Z"
      },
      "data": {
        "data_type":            "folder_usage",
        "_unit":                "bytes",
        "/volume1/JeffMoffitt": 4832847265792,
        "/volume1/LabData":     12043821957120,
        "/volume1":             16876669222912,
        "/volume1_total_bytes": 20000000000000,
        "/volume1_free_bytes":  3123330777088,
        "total_usage":          16876669222912,
        "_timed_out":           ["/volume1/Huge"],
        "_scan_seconds":        812.4
      }
    },
    {
      "header": { "msg_id": "…", "device_name": "Triton", "device_id": "synology-triton",
                  "device_type": "synology", "timestamp": "2026-08-04T07:05:00Z" },
      "data": {
        "data_type":                  "system_metrics",
        "cpu_percent":                18.4,
        "ram_percent":                61.2,
        "uptime_seconds":             863492,
        "network_bytes_in":           847392719104,
        "network_bytes_out":          null
      }
    }
  ]
}
```

- `header.msg_id` identifies a message across retries. Messages without one (old collectors) get `legacy-` + sha1 of `name|data_type|timestamp|username`.
- `header.device_name` must match `name` (after normalization). The token must be allowed to write for `name`.
- A message without a header `timestamp` is rejected.
- Keys starting with `_` in `folder_usage` are metadata, not folders. A folder key that can't be stored (empty, or longer than 4096 characters) is dropped and listed, shortened, in that run's `_errors`; the rest of the scan is stored.
- If the message's `data` (or `header`) isn't an object, it is `rejected`; the result still echoes a valid `header.msg_id` so the collector can drop it.
- The legacy `entries` format from v1 collectors is still accepted.

**Response (always 200 once the batch is accepted):**
```json
{
  "status":   "ok",
  "queue_id": "Triton-3f9a1c2b7d4e",
  "received": 3,
  "stored":   3,
  "counts":   {"stored": 1, "duplicate": 1, "rejected": 1, "error": 0},
  "results": [
    {"msg_id": "5b0d…", "status": "stored"},
    {"msg_id": "a17c…", "status": "duplicate"},
    {"msg_id": "c9e2…", "status": "rejected", "error": "field 'cpu_percent' must be a number or null"}
  ],
  "timestamp": "2026-08-04T07:05:01.000000Z"
}
```

| Status | Meaning | Collector should |
|--------|---------|------------------|
| `stored` | Newly persisted; snapshot, totals, user rows and device registry updated | remove from queue |
| `duplicate` | `msg_id` already stored; nothing re-applied | remove from queue |
| `rejected` | Permanently unstorable (bad shape or value, name mismatch); saved in `dead_letter` | remove from queue |
| `error` | Storage failure: database locked, I/O, disk full, permissions, or a damaged database file ("database disk image is malformed", "file is not a database"); nothing persisted | keep and retry |

The top-level `stored` is `stored + duplicate + rejected`, the number of messages durably handled. Old collectors delete their queue only when `stored` equals the number sent, so they keep draining. New collectors remove exactly the `msg_id`s with a final status.

Each message is stored in its own transaction. One bad message never fails the batch.

### Device Registry

#### `GET /api/devices`

```json
{
  "devices": [
    {
      "name":        "Triton",
      "system_id":   "synology-triton",
      "device_type": "synology",
      "first_seen":  "2026-08-01T02:00:00.000000Z",
      "last_seen":   "2026-08-04T07:05:00.000000Z"
    }
  ]
}
```

#### `GET /api/systems`

List of system names only: `{"systems": ["Atlas", "Triton"]}`

### Running Totals

#### `GET /api/totals`

```json
{
  "global":  {"total_bytes_in": 1234567890, "total_bytes_out": 987654321, "total_disk_bytes": 28575692478464},
  "devices": {"Triton": {"total_bytes_in": 847394000000, "total_bytes_out": 123847500000,
                         "total_disk_bytes": 28575692478464, "last_bytes_in": 847392719104,
                         "last_bytes_out": 123847392012, "last_updated": "…", "network_last_updated": "…"}}
}
```

### Latest Snapshots

| Endpoint | Returns |
|----------|---------|
| `GET /api/metrics/all` | Latest `system_metrics` for every system. Unmeasured values are `null`. |
| `GET /api/metrics/nas/<name>` | Same for one system (404 if none) |
| `GET /api/usage/all` | Latest `folder_usage` for every system: the flat payload inlined (folder paths, volumes, `_`-metadata) |
| `GET /api/usage/nas/<name>` | Same for one system |
| `GET /api/usage/history/<name>?days=30&limit=20000` | `folder_usage` rows from the last N days, oldest first, plus `truncated` |
| `GET /api/user_metrics/all` | `{device: [user rows]}` |
| `GET /api/user_metrics/<name>` | User rows for one device |
| `GET /api/user_metrics/history/<name>/<username>?limit=200` | One user's history, newest first |

### Typed History

#### `GET /api/data/<system>`

```json
{"system": "Triton", "data_types": ["folder_usage", "system_metrics"]}
```

#### `GET /api/data/<system>/<data_type>?limit=100&start=<iso>&end=<iso>`

Without `start`/`end`: the newest `limit` rows (default 100), newest first. With both: rows in `[start, end)`, oldest first, at most `limit` (default 20000). The response has `"truncated": true` when more rows matched. `folder_usage` rows keep the flat shape `{"id", "msg_id", "timestamp", "device_type", "_unit", "<folder path>": bytes, …}`.

#### `GET /api/history/<system>/<data_type>/<field>`

Time series of one numeric field, oldest first. A volume path that lost its leading `/` in the URL (`volume1`) is matched to `/volume1`.

| Param | Meaning |
|-------|---------|
| `from`, `to` | Window `[from, to)`. `to` defaults to now. Without `from`: tail query. |
| `limit` | Tail query: the last N points (default 500); `truncated` is true when older points exist. Window: at most N points (default 20000). If more match, the whole window is bucket-averaged down to N points (`downsampled: true`) instead of being cut off. |
| `max_points` | Bucket-average the range server-side into at most N points (equal-width time buckets, empty buckets omitted). |

```json
{"system_name": "Triton", "data_type": "system_metrics", "field": "cpu_percent", "unit": "%",
 "data": [{"timestamp": "2026-08-04T18:30:00.000000Z", "value": 45.2}],
 "truncated": false, "downsampled": false}
```

### Admin

| Endpoint | Meaning |
|----------|---------|
| `GET /api/dead_letter?limit=100` | Most recent rejected messages with the reason and raw message |
| `DELETE /api/system/<name>` | Permanently deletes the system's `data.db` and its central rows (registry, snapshot, totals, per-user rows). Any spelling of the name works. |

### Health

#### `GET /health`

`{"status": "ok"}`. No auth required.

---

## Retention

History is kept forever unless `history_retention_days` is set. When it is set, the Manager runs `archive_metrics.py`'s retention once a day (first run one minute after start). Every history table, including the central `user_metrics_history`, is handled the same way: rows older than the cutoff are appended to `data/<system>/archive/<YYYY-MM>-<data_type>.jsonl` and fsync'd, and only then deleted. `folder_usage_legacy` is not touched.

To run it by hand or preview it:
```
python archive_metrics.py --config config.json --dry-run
python archive_metrics.py --config config.json --days 365
```

---

## Upgrading from the previous Manager

- **Tokens:** placeholder or short tokens now stop the Manager from starting (exit code 2). Existing `auth_tokens` keep working. Move to scoped tokens when convenient: `collector_tokens` per device, `read_tokens` for the Dashboard, and `admin_tokens` for tools that delete systems.
- **First start migrates databases in place:** each wide `folder_usage` table is copied to the narrow layout (old table kept as `folder_usage_legacy`), and `msg_id` columns are added to the existing tables. Back up `data_dir` first. On a large share the first open of each system takes a little longer.
- **ACK:** responses gain `received`, `counts` and `results`. The top-level `stored` now also counts duplicates and rejected messages, which lets old collectors with stuck queues drain. Rejected messages are kept in `dead_letter`.
- **CORS is off unless `cors_origins` is set.** Existing configs that list origins keep them.
- **Large backlogs from old collectors:** an old collector posts its whole `queue.json` in one request and keeps all of it on any non-200 reply. If its queue grows past `max_request_bytes` (32 MiB by default, typically a few weeks of backlog; for example while the new Manager refused to start over a placeholder token), every run gets `413` and nothing from that device arrives. The Manager logs a warning with this remedy each time. Fix: raise `max_request_bytes` until the queue drains (it can take a few runs; a very large batch may outlast the collector's 30 s timeout, but it is stored anyway and the next run comes back as duplicates), or upgrade the collector, which sends batches of 100. The message-count cap never applies to old collectors.
- **Task Scheduler account:** the installer now registers the tasks with S4U (no stored password). S4U has no network access. If `data_dir` or a log file is on a UNC path, the installer asks for the account's password instead. See [Running from Task Scheduler](#running-from-task-scheduler).
- **`POST /api/usage/report` is gone.** Nothing in the repository used it. The other `/api/usage/*` GET endpoints remain.
- `retention_days` no longer does anything. History is never deleted unless you set `history_retention_days`.

---

## Running from Task Scheduler

`install-manager-dashboard-taskscheduler.ps1` registers the tasks "Lab Monitor Manager" and "Lab Monitor Dashboard". They start at boot, whether or not anyone is logged on, and restart on failure.

- **Local `data_dir` (the default, `E:\Users\lab-monitor\data`):** the tasks run as the installing user through S4U, so no password is stored.
- **`data_dir` or a `log_file` on a UNC path (`\\server\share\...`):** an S4U task has no network access and couldn't open the share. The installer detects this, asks for the account's password (or takes `-TaskCredential (Get-Credential)`), and registers the tasks with that stored password. The account needs access to the share. Re-run the installer after that password changes, or the tasks stop starting.
- **Mapped drive letters (`Z:\...`):** scheduled tasks can't see drive mappings. The installer stops before changing anything and asks you to use the UNC path.
- **Re-running the installer** keeps both config files. `-ForceNewConfig` regenerates only the tokens (the old files are backed up). `data_dir`, the log paths, retention, ports and the Dashboard login are kept.

---

## Troubleshooting

**Manager exits immediately / Task Scheduler launcher stops:**
Check `logs\manager-console.log`. Exit code 2 means the config was refused, usually a placeholder or short token. Run `python manager.py --config config.json --check-config`. If it keeps restarting with "unable to open database file" or a permission error on a `\\server\share` `data_dir`, the task has no network access; re-run the installer, which then asks for a password (see [Running from Task Scheduler](#running-from-task-scheduler)).

**Collector keeps getting 413:**
An old collector's queue has outgrown `max_request_bytes`. Raise it temporarily or upgrade the collector (see [Upgrading](#upgrading-from-the-previous-manager)).

**Collector gets 401 "Invalid token":**
The collector's `manager_token` matches no configured token. Check for leading or trailing spaces.

**Collector gets 403 "Token not allowed for this device":**
The token belongs to a different device in `collector_tokens`, or it is a read token. The collector's `name` must match the `collector_tokens` key.

**Messages show as `rejected`:**
`GET /api/dead_letter` with an admin token shows the reason and the raw message.

**Dashboard shows "Unable to reach Manager":**
Check that the Manager is running, that the firewall allows port 5000, and that the Dashboard config has the correct `manager_url` and a read token.

**"Database locked" errors:**
Writers wait up to 10 s for each other. If you see persistent `error` statuses, make sure only one Manager instance is running.

**Every message from one system comes back `error`:**
The log says "Storage failure ... (kept for retry)". If the error is `DatabaseError: database disk image is malformed` or `file is not a database`, that system's `data/<name>/data.db` is damaged. Collectors keep their messages until it is fixed. Stop the Manager, move the file aside (or restore a backup, or try `sqlite3 data.db ".recover"`), and start it again. The queued messages are then stored.
