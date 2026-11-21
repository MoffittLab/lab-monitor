# Collector

System monitoring agent. Runs on each NAS or server, collects data, and posts it to the Manager.

See [INSTALLATION.md](../INSTALLATION.md) for setup instructions.

---

## Configuration

`local/config.json`:

```json
{
  "name":             "Triton",
  "id":               "synology-triton",
  "device_type":      "synology",
  "manager_url":      "http://atlantis.med.harvard.edu:5000",
  "manager_token":    "YOUR-COLLECTOR-TOKEN-FOR-THIS-DEVICE",

  "volumes":          ["/volume1", "/volume2"],

  "scan_paths": [
    "/volume1/*",
    "/volume1/Data1/*",
    "/volume2/*"
  ],

  "data_dir":                "/volume1/lab-monitor/data",
  "log_file":                "/volume1/lab-monitor/logs/collector.log",
  "log_level":               "INFO",
  "timeout_seconds":         3600,
  "total_timeout_seconds":   10800,
  "request_timeout_seconds": 30,
  "max_batch_messages":      100,
  "max_queue_messages":      50000,
  "exclude_users":           []
}
```

| Field | Description |
|-------|-------------|
| `name` | Display name — appears on the Dashboard and in database folder names. 1–64 characters from `A-Z a-z 0-9 _ . -`; the Manager rejects anything else for a new device. A device the previous Manager already registered under another name (such as `Lab PC`) keeps working under exactly that spelling; don't rename it, since a new name starts a new device with its own, empty history. |
| `id` | Stable unique identifier — use something that won't change (e.g., `synology-triton`) |
| `device_type` | System type: `synology`, `windows`, or any label you choose |
| `manager_url` | URL of the Manager service (`http://` or `https://`). With `http://` the token travels unencrypted. |
| `manager_token` | Bearer token for this device — the Manager's `collector_tokens` entry keyed by this `name` (or a legacy `auth_tokens` entry) |
| `volumes` | Drives/volumes to report **capacity** for (used/free/total bytes). Used only for capacity reporting, not folder scanning. |
| `scan_paths` | Folders to measure for **usage breakdown**. Each entry is either a path (`/volume1/Data1` = measure that folder as a single total) or a glob (`/volume1/Data1/*` = measure each immediate subfolder separately). If omitted, falls back to scanning one level deep in each volume. |
| `data_dir` | Local data directory for archives and queue |
| `log_file` | Log file; rotated at 5 MB, 5 backups kept (`collector.log.1` … `.5`) |
| `log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. An invalid value falls back to `INFO` with a warning. |
| `timeout_seconds` | Max time to measure a single folder (default 3600) |
| `total_timeout_seconds` | Max time for the whole folder scan of one disk run (default: 3 × `timeout_seconds` on Windows; max(3, number of `scan_paths` entries) × `timeout_seconds` elsewhere). See [Time budgets](#time-budgets). |
| `request_timeout_seconds` | HTTP timeout per POST to the Manager (default 30) |
| `max_batch_messages` | Max messages per POST (default 100) |
| `max_queue_messages` | Max messages kept in `queue.json` while the Manager is unreachable (default 50000; `0` = unlimited). When exceeded, the **oldest** are dropped from the queue with a warning — they stay in the monthly archive. |
| `exclude_users` | (Optional) Username patterns to exclude from user activity metrics. Supports wildcards. Built-in defaults already exclude `UMFD-*`, `NT AUTHORITY\*`, etc. |

`volumes` examples:
- Synology: `["/volume1", "/volume2"]`
- Windows: `["E:/Users", "F:"]`

`config.json` holds the token. The Synology installer creates it with mode `600` (readable only by the
installing user, who must also be the Task Scheduler user — or use root). Re-running the installer over an
existing, more open `config.json` asks before restricting it, because tasks running as any other user would
then fail to read it. The Windows installer restricts `collector\local\` to SYSTEM and Administrators.

---

## Modes

```bash
python collector.py --config local/config.json --mode disk      # Daily
python collector.py --config local/config.json --mode metrics   # Every 5 min
```

### Unmeasured values are `null`

If the collector can't measure something (CPU, RAM, uptime, network counters, a GPU field, a folder), it
sends JSON `null` or leaves the field out — never `0`, which would be indistinguishable from a real zero.
The Dashboard shows `—` for these.

### Disk mode (`folder_usage`)

Measures the total size of each configured folder (see `scan_paths`), the used/free/total capacity of each
volume, and a grand total.

**What gets measured for `scan_paths: ["/volume1/*", "/volume2/*"]`:**
```
/volume1/JeffMoffitt  →  total size of all contents
/volume1/LabData      →  total size of all contents
/volume2/Archive      →  total size of all contents
```

System directories (`@eaDir`, `#recycle`, etc.) are skipped automatically. Symlinks are not followed; on
Windows, directory junctions/mount points are not followed either. Hard-linked files are counted once.
On Windows folders are measured with PowerShell `Get-ChildItem` (the path is handed over in the
`LM_SCAN_PATH` environment variable, never pasted into the command), falling back to the Python scanner.

**Message generated:**
```json
{
  "header": {
    "msg_id":      "3f2b9c0e5d7a4e1f9b8c6d5e4f3a2b1c",
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "timestamp":   "2026-08-04T02:00:00Z"
  },
  "data": {
    "data_type":              "folder_usage",
    "_unit":                  "bytes",
    "/volume1/JeffMoffitt":   4832847265792,
    "/volume1/LabData":       12043821957120,
    "/volume1":               16876669222912,
    "/volume1_total_bytes":   20000000000000,
    "/volume1_free_bytes":    3123330777088,
    "/volume2":               8000000000000,
    "/volume2_total_bytes":   16000000000000,
    "/volume2_free_bytes":    8000000000000,
    "total_usage":            24876669222912,
    "_timed_out":             ["/volume2/Archive"],
    "_errors":                [],
    "_scan_seconds":          5321.4
  }
}
```

The payload stays flat: each folder path is a key whose value is its size in bytes; each volume has its used
bytes plus `<vol>_total_bytes` / `<vol>_free_bytes`; `total_usage` is the sum of the measured volumes
(`null` if none could be measured). Keys starting with `_` are metadata, never folders:

| Key | Meaning |
|-----|---------|
| `_unit` | Unit of every numeric value (`bytes`) |
| `_timed_out` | Folders that hit a time budget. They are **omitted** from the folder keys, not reported as 0. |
| `_errors` | Folders that couldn't be scanned (unreadable, or a configured path that doesn't exist). Also omitted. A glob root (`/x/*`) or volume that can't be listed is listed here itself, since its subfolders are unknown. |
| `_scan_seconds` | How long the folder scan took |

Volume capacity is measured first and is always sent, however the folder scan goes.

**A scan path equal to a volume root** (e.g. `scan_paths: ["/volume1"]` with `volumes: ["/volume1"]`) is not
scanned: both values would land under the same key, and the volume's used bytes from the filesystem are the
authoritative figure (they include snapshots, metadata and the system folders the scanner skips). A warning is
logged; use `/volume1/*` for a per-folder breakdown. (If that volume's capacity couldn't be read, the folder
is scanned instead.)

#### Time budgets

- Each folder gets at most `timeout_seconds` (default 3600 s).
- The whole folder scan gets `total_timeout_seconds`. Default:
  - Windows: 3 × `timeout_seconds` (3 h), to fit the Disk Collection task's time limit (below).
  - Synology/Linux (no task time limit): max(3, number of `scan_paths` entries — or of `volumes` when
    `scan_paths` is absent) × `timeout_seconds`. Older collectors gave every entry its own `timeout_seconds`,
    so an existing config keeps the total scan time it had. Set `total_timeout_seconds` explicitly to cap it.
- A folder that runs over its budget is listed in `_timed_out`; once the overall budget is used up, every
  folder not yet measured is listed in `_timed_out` too, so nothing is silently dropped. Scanning continues with
  the next `scan_paths` entry after a single folder times out.
- The Windows installer sets the Disk Collection task's *Stop the task if it runs longer than* limit to
  `total_timeout_seconds` + 1 hour (4 h with the defaults, the same limit older installs already have).
  **If you raise `total_timeout_seconds`, raise that task setting too** (or re-run the installer).

### Metrics mode (`system_metrics`)

Measures CPU%, RAM%, uptime, and network traffic. Network bandwidth is computed by comparing against the previous run's snapshot.

**Message generated:**
```json
{
  "header": {
    "msg_id":      "9a8b7c6d5e4f40312a1b0c9d8e7f6a5b",
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "timestamp":   "2026-08-04T07:05:00Z"
  },
  "data": {
    "data_type":                  "system_metrics",
    "cpu_percent":                18.4,
    "ram_percent":                61.2,
    "uptime_seconds":             863492,
    "uptime_formatted":           "9d 23h",
    "network_bytes_in":           847392719104,
    "network_bytes_out":          123847392012,
    "network_bandwidth_in_mbps":  3.84,
    "network_bandwidth_out_mbps": 0.21
  }
}
```

`network_bytes_in/out` are cumulative OS counters (since last boot), summed over every interface **except
loopback** (`lo`, `lo0`, `Loopback Pseudo-Interface 1`) — the same rule with psutil and with the `/proc/net/dev`
fallback, since loopback traffic never leaves the machine. `network_bandwidth_*_mbps` is the average rate since
the previous metrics run; it is `null` on the first run and right after a counter reset (reboot). If the
counters can't be read at all, all four fields are `null` and the stored snapshot is left untouched.

*Upgrade transition:* older collectors counted loopback on the psutil path. Dropping it at once would make the
counters fall, which the Manager reads as a reboot, adding all traffic since boot to the lifetime totals a second
time. So a psutil host upgraded from an older collector keeps counting loopback until its counters next reset
(reboot), then switches; `network_snapshot.json` records which mode is in use (`"loopback"`). New installs exclude
loopback from the first run.

Without psutil (common on Synology armv7l) the collector reads `/proc`: CPU from two `/proc/stat` samples one
second apart, RAM from `/proc/meminfo` (`MemAvailable`, or `MemFree + Buffers + Cached` on older kernels),
network from `/proc/net/dev`, uptime from `/proc/uptime`.

---

## Local Storage

```
data/<name>/
├── YYYY-MM.jsonl            ← append-only archive; one message per line (permanent)
├── queue.json               ← messages not yet acknowledged by the Manager
├── queue.json.lock          ← lock file guarding queue.json (empty; safe to leave)
├── queue.corrupt-<ts>.json  ← only if a queue.json was ever unreadable (see below)
└── network_snapshot.json    ← previous network counters, for bandwidth
```

Both the archive and the queue use the same message format. The archive is a permanent local record written
before any network call — it is never deleted regardless of whether the Manager receives the data.

The 5-minute metrics run and the multi-hour disk run can overlap, so every change to `queue.json` happens
under an exclusive lock (`fcntl` on Linux/Synology, `msvcrt` on Windows) and is written atomically (temp file +
fsync + rename). If `queue.json` is ever unparseable it is renamed to `queue.corrupt-<timestamp>.json` and an
error is logged; a fresh queue is started. Its messages are still in the archive.

These files hold no secrets and get the normal umask-based mode (e.g. `644`), so a one-off run under another
account (say `sudo` while debugging) doesn't leave a queue the scheduled account can no longer read; a lock file
it can't write is opened read-only, which is enough to lock it. If `data_dir` is on a filesystem without lock
support (some NFS/CIFS mounts), the collector logs an error on every run and uses the queue unlocked, as older
collectors did; overlapping runs may then lose queue updates (never archive lines). Keep `data_dir` local.

---

## Queue and Handshake

Every message gets a unique `header.msg_id` when it is created; it is written to both the archive and the
queue and kept through every retry, so the Manager can recognise re-sent messages. Entries queued by an older
collector without one get, the first time the queue is read, the same deterministic id the Manager derives for
messages without `msg_id` (`legacy-` + sha1 of `name|data_type|timestamp|username`), so a message the old
collector already delivered is recognised as a duplicate.

Each collection run:

1. Measure data
2. Append the message(s) to `YYYY-MM.jsonl` (permanent archive)
3. Append the same message(s) to `queue.json`
4. POST the queue to the Manager, oldest first, at most `max_batch_messages` per request:
   ```json
   {
     "queue_id": "Triton-1a2b3c4d5e6f",
     "name":     "Triton",
     "id":       "synology-triton",
     "messages": [ ... ]
   }
   ```
5. The Manager answers with the echoed `queue_id` and a status per message:
   ```json
   {"status": "ok", "queue_id": "Triton-1a2b3c4d5e6f", "received": 3, "stored": 2,
    "counts": {"stored": 1, "duplicate": 1, "rejected": 0, "error": 1},
    "results": [{"msg_id": "…", "status": "stored"}, {"msg_id": "…", "status": "duplicate"},
                {"msg_id": "…", "status": "error", "error": "database is locked"}]}
   ```
6. If the `queue_id` matches, the collector removes from `queue.json` exactly the messages reported
   `stored`, `duplicate` (already had it) or `rejected` (permanently unstorable; kept in the Manager's
   dead-letter table and logged here). `error` messages stay queued. It re-reads the queue under the lock
   when removing, so messages added by another run in the meantime are kept. The lock is never held during
   the HTTP request.
7. An older Manager that doesn't send `results` gets the previous behaviour: the batch is removed only if
   `stored` equals the number of messages sent.
8. On a network error, a non-200 response, a `queue_id` mismatch, or a batch where nothing could be stored, the
   run stops and the remaining messages are retried next run. An HTTP 413 (batch too large) is retried in halves.

Multiple failed runs accumulate in the queue (up to `max_queue_messages`); they are sent oldest first on the next
successful connection.

---

## Troubleshooting

**Queue not syncing:**
```bash
# Check log
tail -f /volume1/lab-monitor/logs/collector.log

# Verify Manager reachable
curl http://atlantis.med.harvard.edu:5000/health

# How many messages are waiting?
python3 -c "import json; print(len(json.load(open('/volume1/lab-monitor/data/Triton/queue.json'))))"
```

**HTTP 401 (`Manager rejected manager_token`):**
`manager_token` doesn't match any token in the Manager's config. It should be this device's entry in
`collector_tokens` (or a legacy `auth_tokens` entry).

**HTTP 403 (`may not send data for device ...`):**
The token is valid but belongs to another device: the Manager's `collector_tokens` key for this token must be this
collector's `name`.

**`queue.corrupt-*.json` appeared:** an unreadable `queue.json` was set aside (e.g. a crash of an older collector
mid-write). Nothing in it was lost from the archive; once you've looked at it you can delete it.

**`QUEUE BACKLOG OVER LIMIT` warning:** the Manager has been unreachable long enough to queue
`max_queue_messages` messages; the oldest are being dropped from the queue (not from the archive).

**Disk measurement slow / `_timed_out` folders:**
Large folders (50 TB+) take time. Check logs for per-folder timing and raise `timeout_seconds` and/or
`total_timeout_seconds` (and, on Windows, the Disk Collection task's time limit).

---

## Upgrading

**Rollout order: Dashboard, then Manager, then collectors.** Upgrade a collector only once the Manager and the
Dashboard run the new version. The new Manager accepts old collectors unchanged: they keep their token, and
anything they re-send that it already stored comes back as a duplicate. The other way round goes wrong:

- **Old Manager:** it reads a `null` (unmeasured) network counter as 0, resets its running total and then adds the
  whole counter again on the next reading, inflating the lifetime network totals.
- **Old Manager:** a collector token from `collector_tokens` gets HTTP 401; an old Manager knows only `auth_tokens`.
- **Old Dashboard:** it shows the new `_scan_seconds` folder_usage key as a volume on every card.

When upgrading:

- Python 3.8 or later is required.
- Don't update a collector while its daily disk run is in progress. The old code finishing that run afterwards
  deletes the whole `queue.json` without the lock, which can drop messages queued in between (they stay in the
  archive).
- `queue.json` entries queued by the old collector are kept and sent. They get the deterministic `legacy-` id
  (see [Queue and Handshake](#queue-and-handshake)), so any the Manager already stored come back as duplicates.
- `tools/update_collectors.py --upgrade-pip` installs `collector/requirements.txt` the way the Synology installer
  does: `requests` must install, `psutil` is best-effort on Synology/Linux (no armv7l wheel).

---

## Dependencies

- `requests` — HTTP POST to Manager (required)
- `psutil` — CPU, RAM, network measurements (optional; `/proc` fallbacks on Linux/Synology)
- `nvidia-ml-py` — NVIDIA GPU stats (optional; `requirements-gpu.txt`)

```bash
pip install -r requirements.txt
pip install -r requirements-gpu.txt   # only on machines with an NVIDIA GPU
```
