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
  "backup_before_migration": true,
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
| `backup_before_migration` | `true` | Before the Manager changes a database written by an older version for the first time, it copies it to `<db>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak` next to it. If the copy fails, that database is not migrated. `false` migrates without a copy. See [Upgrading](#upgrading-from-the-previous-manager). |
| `threads` | 8 | waitress worker threads. |
| `log_file` | none | Rotating log file (10 MiB × 5; `log_max_bytes`, `log_backup_count`). Every module logs here. An existing file larger than `log_max_bytes` (the previous Manager's unrotated log) is renamed to `<log_file>.pre-rotation-<stamp>` at startup, outside the rotation set, so rotation never deletes it. `--check-config` never opens the log file. |
| `data_dir` | `data` | Where `metrics.db` and the per-system databases live. A UNC path (`\\server\share\...`) works, but see [Running from Task Scheduler](#running-from-task-scheduler). Mapped drive letters don't work for scheduled tasks. |
| `log_level` | `INFO` | Level for the log file. The console gets `WARNING` and above when a log file is set (`console_log_level` to change). |

**The Manager refuses to start** (exit code 2) if any configured token is a placeholder (contains `CHANGE-ME`, `YOUR-`, `GENERATE-RANDOM`, `PLACEHOLDER`, …) or is shorter than 16 characters, or if no tokens are configured at all. `python manager.py --config config.json --check-config` validates the config and exits without starting (it touches no database and no log file).

**One Manager per `data_dir`.** Before it opens any database the Manager takes an exclusive lock on `<data_dir>/manager.lock` (held until the process ends, however it ends; the file names the holder's PID) and writes its PID file. A second Manager on the same `data_dir` exits with code 3 and logs "Another Manager is already running on data_dir … (pid=…)"; the Task Scheduler launcher retries every 60 s, so it takes over once the other one exits. A Manager older than this version takes no lock, so after an upgrade also check that no old `manager.py` is left running.

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

**folder_usage** is stored narrow (one row per folder) so shares with thousands of folders don't hit SQLite's 2000-column limit. The read API reassembles each run into the same flat dict as before. On first open (after the [pre-upgrade backup](#upgrading-from-the-previous-manager)), an existing wide `folder_usage` table is copied into the narrow tables in one transaction and renamed to `folder_usage_legacy`. It is never dropped, so you can delete it by hand once you have checked the migration.

**Other data types** get one column per payload key. When a message has a key the table hasn't seen, the column is added. Earlier rows read back `NULL` for it, meaning "not measured", which is different from a measured zero.
- SQLite column names are case-insensitive. A key that differs only in case from an existing column is stored in that column. A payload with two keys that differ only in case is rejected.
- Payload keys that collide with internal columns (`id`, `timestamp`, `device_type`, `msg_id`) are stored as `payload_<key>`.
- `data_type` must match `[A-Za-z][A-Za-z0-9_-]{0,63}` and must not start with `sqlite_` or `folder_usage_`.

Every history table has a `msg_id` column with a UNIQUE index. Rows stored before the upgrade have `msg_id = NULL`. A message whose `msg_id` isn't stored yet also counts as a `duplicate` when the table already holds such a pre-upgrade row with the same timestamp (as sent, or in the canonical form) and, for `user_metrics`, the same `username`. This applies whatever id the message carries: none (an old collector, an archive replay of old messages), a derived `legacy-…` id (an old queue entry re-sent by an upgraded collector), or a collector's random id (a collector upgraded before the Manager: the previous Manager stored its messages but dropped the id). The new Manager always writes a `msg_id`, so a row without one is always from before the upgrade. The lookup uses the timestamp index. Messages the previous Manager stored are therefore never stored twice.

Timestamps are stored as fixed-width UTC, e.g. `2026-08-04T07:00:00.000000Z`, so they sort correctly as strings. Incoming timestamps with offsets are converted to UTC. Timestamps without an offset are taken as UTC. Rows written by the previous Manager keep the timestamp string exactly as the collector sent it (usually the same form, without the fraction when it was `.000000`); only `folder_usage` runs get the canonical form when they are copied to the narrow layout.

### Central summary: `metrics.db`

| Table | Contents | Key |
|-------|----------|-----|
| `devices` | Name, system_id, device_type, first_seen, last_seen | `name` |
| `device_snapshot` | Latest cpu_percent, ram_percent, uptime, bandwidth, disk total, folder data | `name` |
| `device_totals` | Lifetime network bytes, current disk total | `name` |
| `device_user_snapshot`, `user_metrics_history` | Per-user CPU/RAM (latest + history, `msg_id` UNIQUE) | |
| `dead_letter` | Rejected messages: raw JSON, reason, msg_id, received_at | `id` |

The snapshot tables and `devices.last_seen` follow the **newest** message, not the last one stored: an older message stored later (a gap-fill with `tools/replay_archives.py`, or a retry of an `error` result) adds history rows but does not roll the snapshot, `last_seen` or the disk total back. It can move `first_seen` earlier.

Network totals: a message adds `current − last` for each counter. Nothing is added when either counter is `null` or missing, or when the message is not newer than the last network update (replay or out-of-order). If a counter went down:
- it is a reboot or counter reset, and `current` (the traffic since boot) is added, when the message's `uptime_seconds` is shorter than the time since the previous counter reading plus 300 s, or when `uptime_seconds` is `null` or missing;
- otherwise the machine has been up since the previous reading (an interface went away, or the collector stopped counting loopback), so nothing is added for that counter and it is re-baselined. A warning is logged. This keeps the traffic since boot from being counted a second time.

Device names in the central tables are never renamed. The previous Manager keyed them by the raw name, so a `metrics.db` can hold two spellings that normalize alike: `Triton` and `triton` after a collector was renamed, or two machines `lab-a` and `lab_a`. Both rows are kept unchanged, with their own snapshot and lifetime totals; both appear in `/api/devices` and `/api/totals`, and each is counted once in the global totals. Reads by exact name return that row. Data sent under a registered spelling goes to that row, as the previous Manager filed it; data under a spelling not registered yet (a collector renamed only in case or punctuation) goes to the alike spelling with the same device id (`header.device_id`, the collector's config `id`), else to the one seen most recently, whose counters are current. A new spelling whose device id differs from every alike registered row is another machine and gets its own row. The Manager logs a warning at startup for each such group. Both share one `data/<normalized name>/data.db`, so `DELETE /api/system/<name>` removes the central rows of that one spelling and keeps the shared `data.db` while another spelling is registered (the response lists it in `per_system_db_kept_for`); deleting the last spelling removes the `data.db` too.

Clock checks: a message stamped more than 1 hour (`FUTURE_SLACK_SECONDS`) ahead of this server's clock, from a collector with a wrong clock, is stored in the history but does not move the device's `last_seen`, snapshot, per-user snapshot or network totals; a warning (`ahead of this server's clock`) is logged at most once an hour per device. A stored state timestamp that far ahead (the previous Manager kept whatever arrived) never holds back a current message, and the network totals then add `current − last` as usual.

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

Device names everywhere must match `^[A-Za-z0-9_.-]{1,64}$`. One exception: a name that the previous Manager already registered (it accepted any name, such as `Lab PC`) is still accepted, exactly as registered, for ingest and reads, until the device is deleted with `DELETE /api/system/<name>`. A warning is logged once per Manager start. New devices with such names get `400`. A `collector_tokens` key must follow the pattern; for such a device, use the same name with `_` for the other characters (`Lab_PC`), since names are matched after normalization.

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

- `header.msg_id` identifies a message across retries. Messages without one (old collectors) get `legacy-` + sha1 of `name|data_type|timestamp|username`. Every message whose `msg_id` isn't stored yet is also matched against rows the previous Manager stored (see [Storage](#per-system-history-datanamedatadb)).
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

Each message is stored in its own transaction. One bad message never fails the batch. After the first `error` (a storage failure) the rest of the request is not attempted and is answered `error` too, so the collector keeps it and a locked database costs one wait per request, not one per message.

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
| `DELETE /api/system/<name>` | Permanently deletes the system's central rows (registry, snapshot, totals, per-user rows) and its `data.db`. Any spelling of the name works: the row of exactly that spelling if one is registered, else the one it resolves to. If another registered spelling shares the `data.db` (see [Central summary](#central-summary-metricsdb)), only the named one's central rows go and the `data.db` is kept (`per_system_db_kept_for`). |

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

- **Rollout order: Dashboard, then Manager, then collectors.** The installer updates the Manager and the Dashboard together (one checkout), which is fine. Upgrade collectors only after the Manager:
  - The new Manager accepts old collectors unchanged. They keep their `auth_tokens` token, and whatever they re-send that the previous Manager already stored comes back as `duplicate`.
  - The previous Manager reads a new collector's `null` (unmeasured) network counter as 0. That resets its running total, so the next reading adds the whole counter again.
  - A new collector set up with a `collector_tokens` token gets `401` from the previous Manager.
  - The previous Dashboard shows the new `_scan_seconds` folder_usage key as a volume.
  - The new Dashboard works with the previous Manager. The previous Dashboard works with the new Manager: its "All Available" chart sends no `max_points`, and the new Manager bucket-averages such window queries instead of cutting them off.
- **Tokens:** placeholder or short tokens now stop the Manager from starting (exit code 2). Existing `auth_tokens` keep working. Move to scoped tokens when convenient: `collector_tokens` per device, `read_tokens` for the Dashboard, and `admin_tokens` for tools that delete systems.
- **First start migrates databases in place, after taking a backup.**
  - **What changes:** each wide `folder_usage` table is copied to the narrow layout and the old table is kept as `folder_usage_legacy`. `msg_id` columns are added to the existing history tables. `metrics.db` gains `dead_letter`, `device_totals.network_last_updated` (seeded from the time of the last counter reading, `device_snapshot.metrics_timestamp`, else `last_updated`) and `user_metrics_history.msg_id`. Existing rows are never rewritten or deleted.
  - **Before the first change:** each database is copied with SQLite's online backup API to `data.db.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak` or `metrics.db.pre-upgrade-<…>.bak` next to it, and the copy is logged. A copy is taken once per database: a database that already has the new schema gets none, and none is taken for new databases. A database that an earlier start already partly migrated gets no further copy (its first copy has the pre-upgrade layout), and neither does one whose data is unchanged since its last copy. Each copy is the size of its database, so check free space first. Delete the copies once you have checked the upgrade.
  - **When it happens:** at startup, for every existing database, before the Manager starts listening. On a large or networked `data_dir` the first start takes longer.
  - **If a copy fails** (disk full, permissions), that database is **not** migrated, and an error says why. Its data stays readable on the Dashboard and through the API. New messages for it get `error`, so collectors keep them queued. On the next write after 5 minutes the backup is tried again and, if it works, the migration runs. To migrate without a copy, set `"backup_before_migration": false`.
  - **If the folder_usage copy fails** (another program holds the database open for writing, disk full), it is rolled back: the wide table stays and keeps serving the old folder history. New folder_usage messages for that system get `error` (collectors keep them) until the copy succeeds, on the next one after 5 minutes or at the next start; the narrow tables are never started next to an unmigrated wide table. Other data types are stored normally.
- **Messages the previous Manager stored aren't stored twice when they are re-sent after the upgrade.** This covers old collectors' queues, upgraded collectors re-sending entries an old collector queued, messages a collector upgraded before the Manager delivered to the previous Manager (re-sent after a lost ACK, or by `tools/replay_archives.py`), and `tools/replay_archives.py --include-legacy`. They are matched to the previous Manager's rows by timestamp (and user name).
- **Device names:** names the previous Manager registered keep working even if they fall outside the new pattern (see [API Endpoints](#api-endpoints)). If `metrics.db` holds two spellings of one device, both rows are kept with their own totals (see [Central summary](#central-summary-metricsdb)).
- **Leftovers the previous Manager could produce stay readable:** per-user rows of a device removed with the old `DELETE /api/system/<name>` (it never removed them) are served by `/api/user_metrics/<name>` and `/api/user_metrics/history/<name>/<user>`; a `data/<name>/data.db` without a registry row is served under that name; a registry row without a directory still shows its card. A table the previous Manager made from a `data_type` the new rules refuse (e.g. `1wire_sensors`, starting with a digit) stays readable under that name and keeps receiving that label's messages in that database; no new table is ever made from such a label.
- **Numeric folder_usage metadata** such as `_scan_seconds` (sent by an upgraded collector) is still charted through `/api/history/<system>/folder_usage/_scan_seconds`, for rows the previous Manager stored and new ones.
- **Check before, roll back after:** `tools/verify_upgrade.py` compares the old and new Manager on copies of the real `data_dir` (nothing in it is written), and `tools/rollback_upgrade.py` restores the pre-upgrade backups (see `tools/README.md`).
- **Network totals:** counters that drop while `uptime_seconds` shows no reboot no longer add the traffic since boot again (see [Central summary](#central-summary-metricsdb)).
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
- **Mapped drive letters (`Z:\...`):** scheduled tasks can't see drive mappings. The installer stops before it installs packages, writes a config or touches a service, and asks you to use the UNC path.
- **A config the new Manager refuses** stops the installer before it stops the running services or writes a config (`-IgnoreConfigCheck` overrides that).
- **Re-running the installer** keeps both config files. `-ForceNewConfig` regenerates only the tokens (the old files are backed up). `data_dir`, the log paths, retention, ports and the Dashboard login are kept, but every existing token goes, `auth_tokens` and `collector_tokens` included: collectors get `401` until they have new ones.

---

## Troubleshooting

**Manager exits immediately / Task Scheduler launcher stops:**
Check `logs\manager-console.log`. Exit code 2 means the config was refused, usually a placeholder or short token. Run `python manager.py --config config.json --check-config`. Exit code 3 means another Manager already runs on this `data_dir` (the launcher retries every 60 s). If it keeps restarting with "unable to open database file" or a permission error on a `\\server\share` `data_dir`, the task has no network access; re-run the installer, which then asks for a password (see [Running from Task Scheduler](#running-from-task-scheduler)).

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
Writers wait up to 10 s for another process's lock. If a write still finds the database locked, later writes to that database wait only 0.1 s for the next 60 s (any successful write ends this early), so a database held locked for minutes doesn't tie up the Manager's threads; the messages come back `error` and stay in the collectors' queues. If you see persistent `error` statuses, make sure only one Manager instance is running and that no other program (for example a DB browser with unsaved changes) holds `data.db` or `metrics.db` open for writing.

**Every message from one system comes back `error`:**
The log says "Storage failure ... (kept for retry)". If the error is `DatabaseError: database disk image is malformed` or `file is not a database`, that system's `data/<name>/data.db` is damaged. Collectors keep their messages until it is fixed. Stop the Manager, move the file aside (or restore a backup, or try `sqlite3 data.db ".recover"`), and start it again. The queued messages are then stored.

If the error says the database "needs a migration that is held back", its pre-upgrade backup failed. If it is `metrics.db`, every system is affected. The startup log says why (for example "No space left on device"). Free the space or fix the permissions; the backup is retried on the next write after 5 minutes, or at the next start. You can also set `"backup_before_migration": false`.

**Restoring a pre-upgrade backup:** stop the Manager and run `python tools\rollback_upgrade.py --manager-config manager\config.json` (shows the plan), then again with `--apply`. For every database with a backup it renames the current file (and its `-wal` / `-shm`) to `<db>.post-upgrade-<stamp>` and puts the newest `.bak` in its place; nothing is deleted. By hand: move `data.db` and its `-wal` / `-shm` aside and copy the newest `data.db.pre-upgrade-<…>.bak` to `data.db`; the same for `metrics.db` (restore all databases together, they belong to one point in time). Run the old code on them, or start the new Manager again, which migrates the restored files again. Messages stored since the backup are in the moved-aside files and in the collectors' archives (`tools/replay_archives.py`).
