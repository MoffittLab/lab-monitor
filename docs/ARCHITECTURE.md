# Architecture

## Overview

Lab Monitor is a distributed monitoring platform. Collectors run on each system, write every
message to a local archive and queue, and POST the queue to a central Manager. The Manager stores
full history per system, keeps a summary database for current state, and serves a REST API. The
Dashboard is a web page whose server reads that API.

```
┌─────────────────────────────────────────────────────┐
│                   Systems (Collectors)              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐         │
│  │  Triton  │   │  Atlas   │   │  Other   │         │
│  │ Synology │   │ Windows  │   │   ...    │         │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘         │
└───────┼──────────────┼──────────────┼───────────────┘
        │              │              │
        │   POST /api/data/queue (collector token, batches of ≤100)
        └──────────────┼──────────────┘
                       │  per-message ACK
              ┌────────▼────────┐
              │    Manager      │  Flask + waitress, port 5000
              ├─────────────────┤
              │ metrics.db      │  ← devices, snapshots, totals, per-user, dead letters
              │ <system>/data.db│  ← full typed history
              └────────┬────────┘
                       │  REST API (read token)
              ┌────────▼────────┐
              │   Dashboard     │  Flask + waitress, port 5001
              │ server + page   │  (optional sign-in)
              └─────────────────┘
```

Fleet tools in `tools/` manage collectors over SSH and talk to the Manager API
([tools/README.md](../tools/README.md)).

---

## Collector

Runs on each system. Two modes on separate schedules, plus one run by hand:

| Mode | Flag | Schedule | Messages |
|------|------|----------|----------|
| Disk | `--mode disk` | Daily (02:00) | One `folder_usage` (folder sizes, volume used/free/total, grand total) and one `storage_breakdown` (file counts, cold data, storage per owner) |
| Metrics | `--mode metrics` | Every 5 min | One `system_metrics` (CPU and disk I/O averaged over the interval, RAM, uptime, network, GPUs), one `user_metrics` per reported user (CPU-seconds, RAM, GPU use), one `sessions` (who is connected), and `storage_health` (RAID, disks, volumes) when it changed or once an hour |
| Health | `--mode health` | By hand | One `storage_health` with `trigger: "manual"` |

In the metrics run the system and user messages are archived and queued first; sessions and storage
health then run in parallel threads, each within its own time budget, so a hung source costs only
its own message. Unmeasured values are sent as `null` (or left out), never `0`. Message contents:
[METRICS.md](../METRICS.md); wire rules: [PROTOCOL.md](PROTOCOL.md).

### Local storage

```
data/<name>/
├── YYYY-MM.jsonl            ← append-only archive, one message per line (never deleted)
├── queue.json               ← messages not yet acknowledged
├── queue.json.lock          ← lock guarding queue.json
├── queue.corrupt-<ts>.json  ← only if a queue.json was ever unreadable
├── network_snapshot.json    ← previous counters, for bandwidth
├── perf_snapshot.json(.lock) ← previous CPU, disk and process counters (interval averages)
├── sessions_state.json      ← when the session list was last sent in full
├── storage_health_state.json ← last health signature, cached hourly readings
└── storage_breakdown_state.json ← per-folder scan times (breakdown time guard)
```

The state files are written only after the run's messages were recorded, and never by `--dry-run`.

The 5-minute run and the multi-hour disk run can overlap, so every change to `queue.json` happens
under an exclusive lock and is written atomically (temporary file, fsync, rename).

### Message format

```json
{
  "header": {
    "msg_id":      "5b0d6c1e9f8a4c3b9e2d7a6f1c0b4e3d",
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "ip_address":  "10.0.0.42",
    "timestamp":   "2026-08-04T07:00:00Z"
  },
  "data": {
    "data_type": "folder_usage",
    "...": "..."
  }
}
```

`data_type` tells the Manager which table to use; without it the message goes to `not_specified`.
In `folder_usage`, keys starting with `_` (`_unit`, `_timed_out`, `_errors`, `_scan_seconds`, `_volumes`) are
metadata, never folders.

### Queue and acknowledgement

1. The collector measures and writes each message to the archive, then appends it to `queue.json`.
   Every message gets a `header.msg_id` (uuid4) when it is created; retries keep it.
2. It POSTs the queue oldest first, at most `max_batch_messages` (default 100) per request:
   `{"queue_id": "<name>-<12 hex>", "name", "id", "messages": [...]}`. The lock is not held during
   the request.
3. The Manager handles each message on its own and answers HTTP 200 with the echoed `queue_id`:
   ```json
   {"status": "ok", "queue_id": "Triton-1a2b3c4d5e6f", "received": 3, "stored": 2,
    "counts": {"stored": 1, "duplicate": 1, "rejected": 0, "error": 1},
    "results": [{"msg_id": "…", "status": "stored"}, {"msg_id": "…", "status": "duplicate"},
                {"msg_id": "…", "status": "error", "error": "database is locked"}]}
   ```
   - `stored`: newly saved; snapshot, totals, per-user rows and device registry updated.
   - `duplicate`: already stored (same `msg_id`); nothing re-applied.
   - `rejected`: permanently unstorable (bad shape or value, name mismatch); saved in the
     `dead_letter` table with the reason.
   - `error`: storage failure (locked, I/O, disk full, damaged file); nothing saved.
   - The top-level `stored` is `stored + duplicate + rejected`.
4. If the `queue_id` matches, the collector re-reads the queue under the lock and removes exactly the
   messages reported `stored`, `duplicate` or `rejected`; `error` messages stay for the next run.
   Messages appended meanwhile by another run are kept.
5. On a network error, a non-200 answer, a `queue_id` mismatch or a batch with no progress, the run
   stops and the rest is retried next run. HTTP 413 (too large) is retried in halves.

**Older collectors and Managers.** An old collector posts its whole queue and deletes it only if
`stored` equals the number sent; because `stored` counts duplicates and rejected messages, it keeps
draining. Old messages without `msg_id` get a derived id (`legacy-` + sha1 of
`name|data_type|timestamp|username`); the new collector gives old queue entries the same id. Rows
the previous Manager stored have no `msg_id`, so a message whose id is not stored yet (none, a
derived `legacy-` id, or the id of a collector upgraded before the Manager, which the old Manager
dropped) is also a duplicate when such a pre-upgrade row has the same timestamp (and user name for
per-user rows). A new collector talking to an old Manager (no `results`) removes a batch only if
`stored` equals its size.

The archive is never deleted by the collector: it is the local record whatever the Manager received.

---

## Manager

Central Flask service, served by waitress (falls back to the Flask development server with a loud
warning when waitress is missing). Configuration: [manager/README.md](../manager/README.md).

### Ingest

`POST /api/data/queue` takes the batch above (the legacy `entries` format of v1 collectors is also
accepted). Checks: a token allowed to write for `name`; every message's `header.device_name` equal
to `name` after normalization; a header `timestamp`; device names matching `^[A-Za-z0-9_.-]{1,64}$`
(names an older Manager already registered stay accepted). Each message is stored or rolled back
on its own together with its central side effects, so one bad message never fails the batch: its
own savepoint inside one transaction per database (the system's `data.db` and `metrics.db`) that
holds up to `ingest_commit_messages` (100) consecutive messages of the request or 0.5 s of work. The
answer is sent only after those transactions are committed; if a COMMIT fails, every message of
that group is answered `error` and the collector sends it again
([manager/README.md](../manager/README.md#ingest)). The two databases are always locked in the
same order, `data.db` first.
Limits: `max_request_bytes` (32 MiB) and `max_messages_per_request` (10000, only for requests whose
messages carry `msg_id`).

### Storage: two layers

**Layer 1: per-system history (`TypedDataStore`, `data_store.py`)**

```
data/
└── triton/                    ← normalized name: lowercase, other characters → _
    ├── data.db
    │   ├── folder_usage_msgs    ← one row per disk scan
    │   ├── folder_usage_items   ← one row per (scan, folder/volume key) → bytes
    │   ├── folder_usage_legacy  ← pre-upgrade wide table, kept after migration
    │   ├── system_metrics       ← one row per metrics run
    │   ├── user_metrics         ← one row per user per metrics run
    │   ├── sessions             ← one row per metrics run (the list as JSON text when sent)
    │   ├── storage_health       ← one row per health message
    │   ├── storage_breakdown_msgs  ← one row per daily breakdown
    │   ├── storage_breakdown_rows  ← one row per (run, folder or device, owner / total / other)
    │   ├── storage_breakdown_legacy ← only if an older Manager stored the type wide; kept
    │   ├── _rollup_series, _rollup_hour, _rollup_day  ← hourly / daily aggregates, kept forever
    │   ├── _rollup_state, _rollup_pending             ← rollup progress
    │   └── not_specified        ← messages without data_type
    └── archive/                 ← only if history retention is enabled
```

- `folder_usage` is narrow (one row per folder) so large shares don't hit SQLite's 2000-column
  limit; the API returns each scan as the same flat dict the collector sent. `storage_breakdown` is
  narrow too; the API returns each run nested, exactly as sent.
- The table prefixes `_` and `storage_breakdown_` are reserved: no `data_type` maps to them, and
  the review-fix Manager (`e5cd699`, `160da09`, `fb6498c`) ignores such tables after a downgrade.
- Other data types have one column per payload key. A new key adds a column; earlier rows read
  `NULL` for it ("not measured"), which is different from a measured zero.
- Every history table has a `msg_id` column with a UNIQUE index for de-duplication.
- Timestamps are stored as fixed-width UTC (`…T07:00:00.000000Z`).
- Read endpoints never create a directory or database; an unknown system is 404.

**Layer 2: central summary (`metrics.db`, `metrics.py`)**

| Table | Purpose | Key |
|-------|---------|-----|
| `devices` | Registry: name, system_id, device_type, first/last seen | `name` |
| `device_snapshot` | Latest metrics and disk state per device | `name` |
| `device_totals` | Lifetime network bytes (across reboots), last counters, current disk total | `name` |
| `device_user_snapshot`, `user_metrics_history` | Latest and historical per-user CPU/RAM and usage fields (CPU-seconds, cores, process count, GPU) | |
| `device_session_snapshot` | Latest sessions counts and list, with `list_timestamp` / `list_confirmed_at` | `name` |
| `device_storage_health` | Latest storage health (without the flattened history keys) | `name` |
| `device_storage_breakdown` | Latest storage-breakdown summary (≤ 50 KB per device) | `name` |
| `dead_letter` | Rejected messages with reason and raw JSON | `id` |

The snapshot, `last_seen` and disk total only move forward in time: an older message stored later
adds history without rolling them back.

**Network totals.** A message adds `current − last` per counter. Nothing is added when a counter is
`null`/missing or the message is not newer than the last counter reading. When a counter goes down,
it counts as a reboot, and `current` (traffic since boot) is added, if `uptime_seconds` is shorter
than the time since the last counter reading plus 300 s, or if `uptime_seconds` is `null` or
missing. Otherwise nothing is added, the counter is re-baselined and a warning is logged.

**Device names.** One function (`storage_util.normalize_device_name`) turns a name into its storage
key, so `Lab-A` and `lab-a` resolve to the same directory and the same central rows. Central rows
are never renamed: if an older Manager registered two spellings that normalize alike (a renamed
collector, or two machines such as `lab-a` and `lab_a`), both rows are kept with their own totals
and stay readable under their exact names; data sent under a registered spelling goes to that row,
data under a new spelling to the spelling seen most recently. A message stamped more than an hour
ahead of the server's clock is stored in the history but does not move a device's state.

**Upgrading old databases.** The first time the Manager opens a database written by an older
version, it copies it to `<db>.pre-upgrade-<stamp>.bak`, then migrates it (narrow folder tables,
`msg_id` columns, new central tables and columns; with the usage-metrics release also the new
per-user columns and snapshot tables in `metrics.db`, and a wide `storage_breakdown` table).
Existing rows are never rewritten or deleted. This happens at startup, before the Manager listens.
Adding the rollup tables to a database that already has a table (even one without rows) is a schema
change too, so it gets the same one-time copy first (one per database, taken with the migration when
there is one); if that copy fails, history is stored and served as usual and is rolled up once a retry
succeeds. See [UPGRADE.md](UPGRADE.md).

**Retention.** History is kept forever unless `history_retention_days` is set; then rows older
than that are archived to JSONL and deleted once a day. A rolled-up table is pruned only once its
rollups are complete (below).

### Rollups

`manager/rollups.py` keeps hourly and daily aggregates (n, sum, min, max per UTC hour and day) of
every numeric field of `system_metrics`, `user_metrics` (one series per user), `sessions` and
`storage_health` (daily only), in the system's own `data.db`.

1. **Live roll.** Each stored message is rolled up in the same transaction (its savepoint) as its
   row, so the row and its rollup are committed together. A storage error gets
   the message `error` (the collector retries); any other rollup failure queues the row for repair,
   and the raw row is stored anyway.
2. **Backfill.** The first open of a database queues all its existing rows, and every later open
   queues rows stored without rollups (rollups disabled, or an older Manager ran after a
   downgrade). A background thread works through them system by system in batches of
   `backfill_batch_ids` row ids, one short write transaction each, sleeping so it works at most
   `backfill_max_duty` of the time. It never blocks startup; ingest waits at most one batch. The
   cursor commits with each batch, so a restart resumes without counting anything twice. Progress:
   `GET /api/rollups/status` or `python rollups.py --status`.
3. **Freeze.** Before retention prunes a rolled-up table, it finishes any pending backfill (up to an
   hour per table; otherwise the table is skipped that day) and sets the table's `frozen_before` to
   the cutoff. A row older than that is never rolled up again, so a pruned message replayed later
   is stored but not counted twice.
4. **Retention.** Rollups are kept forever; raw rows follow `history_retention_days`. Charts over
   ranges the raw rows no longer cover are answered from the rollups.

`/api/history` picks raw rows, hourly or daily rollups per request (`resolution=auto`): raw for
tail queries, short ranges and small series, rollups when a point would span an hour or more. See
[PROTOCOL.md](PROTOCOL.md#17-history-api-extension-and-rollup-semantics).

### API endpoints

Details and response shapes: [manager/README.md](../manager/README.md#api-endpoints).

| Method | Path | Token scope | Description |
|--------|------|-------------|-------------|
| POST | `/api/data/queue` | collector (own device) or admin | Ingest a batch |
| GET | `/health` | none | `{"status": "ok"}` |
| GET | `/api/devices` | read | Device registry |
| GET | `/api/systems` | read | System names |
| GET | `/api/totals` | read | Per-device and global running totals |
| GET | `/api/metrics/all`, `/api/metrics/nas/<name>` | read | Latest `system_metrics` |
| GET | `/api/usage/all`, `/api/usage/nas/<name>` | read | Latest `folder_usage` |
| GET | `/api/usage/history/<name>` | read | `folder_usage` rows of the last N days |
| GET | `/api/user_metrics/all`, `/api/user_metrics/<name>` | read | Latest per-user rows |
| GET | `/api/user_metrics/history/<name>/<user>` | read | One user's history |
| GET | `/api/data/<system>` | read | Data types present for a system |
| GET | `/api/data/<system>/<data_type>` | read | Typed history rows (recent, or a time window) |
| GET | `/api/history/<system>/<data_type>/<field>` | read | One field as a time series (`from`, `to`, `limit`, `max_points` for server-side averaging; `resolution` raw/hour/day/auto, `entity` = one user) |
| GET | `/api/user_metrics/usage` | read | Per-user CPU and GPU totals over 24 h / 7 d / 30 d |
| GET | `/api/sessions/all`, `/api/sessions/<name>` | read | Who is connected (latest counts and list) |
| GET | `/api/storage/health/all`, `/api/storage/health/<name>` | read | Latest storage health |
| GET | `/api/storage/breakdown/all`, `/api/storage/breakdown/<name>` | read | Storage per owner and cold data (summary; full owner rows per system) |
| GET | `/api/rollups/status` | read | Rollup backfill progress |
| GET | `/api/dead_letter` | admin | Rejected messages |
| DELETE | `/api/system/<name>` | admin | Permanently delete a system's history and central rows |

`POST /api/usage/report` (the v0 report format) has been removed. Errors are
`{"status": "error", "error": "<short message>"}` and never contain exception text.

**Optional endpoints.** The eight endpoints added by the usage-metrics release answer 404 on an older
Manager. A client treats that 404 as "feature not available on this Manager", not as an error; the
Dashboard then hides the matching sections and stays healthy.

---

## Dashboard

Read-only Flask app ([dashboard/README.md](../dashboard/README.md)). The browser talks only to
the Dashboard server; the server calls the Manager with its read token, which the browser never
sees.

- `GET /api/data` fetches `/api/devices`, `/api/metrics/all`, `/api/usage/all`, `/api/totals`,
  `/api/user_metrics/all`, `/api/sessions/all`, `/api/storage/health/all`,
  `/api/storage/breakdown/all` and `/api/user_metrics/usage` concurrently and caches the result for
  one refresh interval (default 30 s). Storage breakdown is fetched at most every 600 s, usage
  totals every 300 s and storage health every 60 s. It answers 200, 200 with `"degraded": true` and
  the failed calls listed, or 502 when the device list itself failed. A 404 from one of the four
  newer endpoints sets `features.<name>` to false and hides that section; it is not a failure.
- The page renders one card per system (metrics, disk I/O, disk, users, GPUs, who is connected,
  storage health and owners), the summary strip and totals, and history charts
  (`GET /api/history/...`, bucket-averaged to `chart_max_points`, with a min–max band when the
  Manager answers from rollups). It shows stale / offline / unknown states, keeps the last good data
  greyed out during an outage, and renders unmeasured values as `—`.
- `GET /health` answers without auth, for monitoring and the restart tool.

The Dashboard owns no data. All state lives in the Manager.

---

## Security

- **Scoped bearer tokens** on every Manager endpoint except `/health`: `admin_tokens`,
  `read_tokens` (Dashboard), `collector_tokens` (one per device, write only for that device);
  legacy `auth_tokens` keep full rights with a deprecation warning. Constant-time comparison. The
  Manager refuses to start with placeholder or short tokens. See [CONFIG.md](../CONFIG.md).
- **CORS is off** unless `cors_origins` is set; the Dashboard doesn't need it.
- **Dashboard:** optional sign-in (one password, then a signed cookie that keeps the browser
  signed in) on every route except `/health` and the static files; all values are
  HTML-escaped; Plotly is pinned with a Subresource Integrity hash.
- **Per-user data** (user names, client IPs, per-user CPU, GPU and storage) is shown to everyone who
  can open the Dashboard; the optional sign-in is the access control. What is recorded and how
  to turn it off: [PRIVACY.md](PRIVACY.md).
- **Transport: plain HTTP.** Neither the Manager nor the Dashboard terminates TLS, so tokens and
  the Dashboard password travel unencrypted. This assumes a trusted lab network. Otherwise put a
  TLS reverse proxy in front of both and point collectors at `https://` (they verify the
  certificate).
- **Secrets on disk:** configs are kept out of git; the collector config is mode 600 on Synology
  and readable only by SYSTEM/Administrators on Windows.
- **Fleet tools** pin SSH host keys on first use (`tools/.known_hosts`).

---

## Adding a new system

1. Pick its `name` (`^[A-Za-z0-9_.-]{1,64}$`) and add a token for it to the Manager's
   `collector_tokens`; restart the Manager.
2. Install the collector ([INSTALLATION.md](../INSTALLATION.md)) with that token, and schedule the
   disk and metrics jobs (the Windows installer does this itself).
3. The Manager creates `data/<normalized name>/data.db` and the central rows on the first stored
   message.
4. The device appears on the Dashboard at the next refresh.
