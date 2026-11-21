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
| `name` | Display name — appears on the Dashboard and in database folder names. 1–64 characters from `A-Z a-z 0-9 _ . -`; the Manager rejects anything else. |
| `id` | Stable unique identifier — use something that won't change (e.g., `synology-triton`) |
| `device_type` | System type: `synology`, `windows`, or any label you choose (default on Linux: `Server`, which the Dashboard groups under Servers) |
| `manager_url` | URL of the Manager service (`http://` or `https://`). With `http://` the token travels unencrypted. |
| `manager_token` | Bearer token for this device — the Manager's `collector_tokens` entry keyed by this `name` (or a legacy `auth_tokens` entry) |
| `volumes` | Drives/volumes to report **capacity** for (used/free/total bytes). Used only for capacity reporting, not folder scanning. |
| `scan_paths` | Folders to measure for **usage breakdown**. Each entry is either a path (`/volume1/Data1` = measure that folder as a single total) or a glob (`/volume1/Data1/*` = measure each immediate subfolder separately). If omitted, falls back to scanning one level deep in each volume (Synology/Windows); on Linux only volume capacity is reported (see [Linux / Ubuntu](#linux--ubuntu)). |
| `data_dir` | Local data directory for archives and queue |
| `log_file` | Log file; rotated at 5 MB, 5 backups kept (`collector.log.1` … `.5`) |
| `log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. An invalid value falls back to `INFO` with a warning. |
| `timeout_seconds` | Max time to measure a single folder (default 3600) |
| `total_timeout_seconds` | Max time for the whole folder scan of one disk run (default: 3 × `timeout_seconds` on Windows; max(3, number of `scan_paths` entries) × `timeout_seconds` elsewhere). See [Time budgets](#time-budgets). |
| `request_timeout_seconds` | HTTP timeout per POST to the Manager (default 30) |
| `max_batch_messages` | Max messages per POST (default 100) |
| `max_queue_messages` | Max messages kept in `queue.json` while the Manager is unreachable (default 50000; `0` = unlimited). When exceeded, the **oldest** are dropped from the queue with a warning — they stay in the monthly archive. |
| `exclude_users` | (Optional) Username patterns to exclude from user activity metrics. Supports wildcards. Built-in defaults already exclude `UMFD-*`, `NT AUTHORITY\*`, etc. |
| `platform` | (Optional) `synology`, `linux` or `windows`. Default: detected — Windows; Synology if `/etc/synoinfo.conf` or `/usr/syno` exists; any other host is `linux`. |
| `include_network_mounts` | (Optional, Linux) `true` = also auto-discover network and cluster filesystems (nfs/nfs4/cifs/smb3, ceph, Lustre, GPFS, BeeGFS, GlusterFS, AFS) as volumes and let scans enter them. Default `false`: a `scan_paths` entry on one is not scanned and is listed in `_errors`. |
| `cross_filesystems` | (Optional) `false` = measuring a folder never descends into another filesystem or mount point below it (like `du -x`). Default `false` on Linux, `true` (the historical behaviour) on Synology/Windows. |

`volumes` examples:
- Synology: `["/volume1", "/volume2"]`
- Windows: `["E:/Users", "F:"]`
- Linux: leave it out to auto-discover the real filesystems, or e.g. `["/", "/data"]`

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
    "_scan_seconds":          5321.4,
    "_volumes":               ["/volume1", "/volume2"]
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
| `_volumes` | The keys above that are volumes (those whose capacity was measured). On Linux a volume can be `/mnt/data` and a folder `/data`, so path depth can't tell them apart; the Dashboard uses this list (and, for older collectors without it, the `_total_bytes` keys and then path depth). |

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

## Linux / Ubuntu

Installed by `install-collector-linux.sh` (Ubuntu 22.04 / 24.04, Debian 12; see
[INSTALLATION.md](../INSTALLATION.md#collector-on-linux-ubuntu--debian)). Example config:
[config.linux.example.json](config.linux.example.json).

| Path | Contents |
|------|----------|
| `/opt/lab-monitor/collector` | Code, owned by root (the service can't modify it) |
| `/opt/lab-monitor/venv` | Python venv: `requirements.txt` (+ `requirements-gpu.txt` when `nvidia-smi` exists) |
| `/etc/lab-monitor/collector.json` | Config, mode `0640 root:labmon` (it holds the token) |
| `/var/lib/lab-monitor/<name>/` | `data_dir`: archive, queue, network snapshot |
| `/var/log/lab-monitor/collector.log` | Log file (every line is in the journal too) |
| `/etc/systemd/system/lab-monitor-*` | Units, copied from [`systemd/`](systemd/) |

**Scheduling.** `lab-monitor-metrics.timer` starts `lab-monitor-metrics.service` every 5 minutes
(`OnCalendar=*:0/5`); `lab-monitor-disk.timer` starts `lab-monitor-disk.service` daily at 02:00 plus up to
15 minutes of random delay. Both timers are `Persistent=`, so a run missed while the server was off happens
at boot.

**Sandbox.** Both services run as the system user `labmon` (no login shell) with `ProtectSystem=strict`
(writable: only `/var/lib/lab-monitor` and `/var/log/lab-monitor`), `PrivateTmp`, `NoNewPrivileges`, a
system-call filter, no W+X memory and only `AF_INET`/`AF_INET6`/`AF_UNIX` sockets. The metrics service has
no capabilities and no access to `/home`; it reads `/proc` for every user's processes (per-user metrics),
which world-readable `/proc` files allow (unless `/proc` is mounted with `hidepid=`). The disk service must
read every file under `scan_paths` whoever owns it, so it gets exactly one capability,
`CAP_DAC_READ_SEARCH` (skip read/search permission checks — no write access, nothing else of root), sees
`/home` read-only, and runs at `Nice=19` with idle I/O priority. Change a setting with a drop-in
(`sudo systemctl edit lab-monitor-disk.service`): e.g. `ReadWritePaths=` for a `data_dir` elsewhere, or
`MemoryDenyWriteExecute=no` if NVIDIA GPU stats stop appearing.

**Time budget.** The disk service's `TimeoutStartSec` is the collector's overall scan budget
(`total_timeout_seconds`, 3 h in the installer's config) + 1 h for volume capacity and the upload. The
installer writes it to `/etc/systemd/system/lab-monitor-disk.service.d/50-timeout.conf` from the config;
re-run it after changing `scan_paths`, `timeout_seconds` or `total_timeout_seconds`. The metrics service
gets 4 minutes, below its 5-minute interval.

**Volumes** (when `volumes` is left out) come from `/proc/self/mounts`: ext2/3/4, xfs, btrfs, zfs, f2fs (and
bcachefs, jfs, reiserfs, nilfs2), plus nfs/nfs4/cifs/smb3 with `"include_network_mounts": true`. Pseudo,
virtual and ephemeral filesystems (proc, sysfs, tmpfs, devtmpfs, overlay, snap squashfs, cgroup, efivarfs,
the vfat `/boot/efi`, fuse.\*, autofs, ...) and mounts under `/proc`, `/sys`, `/dev`, `/run`, `/snap` and
container runtimes' directories are left out. A filesystem mounted several times (btrfs subvolumes of one
device such as `/` and `/home`, bind mounts) is reported once, under its first mount point — reporting each
would count it twice in `total_usage`. Each ZFS dataset is its own volume; as its capacity is "used by the
dataset + free in the pool", the Dashboard's summed capacity over-counts a pool with many datasets: list
`volumes` explicitly there.

**Folders.** Set `scan_paths` (typically `["/home/*", "/data/*"]`): without it only volume capacity is
reported, and a warning says so — one level deep in `/` would walk `/usr`, `/var`, `/proc`, ...
- A glob lists the subfolders except hidden ones, `lost+found`, and subfolders that are pseudo-filesystem
  mounts (`/proc`, `/sys`, `/dev`, `/run`, tmpfs, snaps, fuse, ...), network mounts (unless
  `include_network_mounts`) or automount points of unknown type (see below). A subfolder that is another *local* filesystem (say one ZFS dataset per
  project) is kept and measured on its own.
- With `"cross_filesystems": false` (the Linux default) measuring a folder stays on that folder's
  filesystem, like `du -x`: it doesn't descend into mount points, bind mounts or nested btrfs subvolumes
  (e.g. snapper's `.snapshots`), so a volume mounted inside a scanned folder isn't counted twice. The log
  lists what was left out ("did not enter N other filesystem(s) / mount point(s)"). `true` enters them —
  but never pseudo filesystems, and network mounts only with `include_network_mounts`.
- Synology's system names (`@eaDir`, `#recycle`, `*.hbk`) are ordinary folders on Linux and are counted;
  `lost+found` and `.snapshots` are skipped at every level.
- Nothing under `/proc`, `/sys`, `/dev` or `/run` is ever measured, even when listed in `scan_paths` (such
  entries are reported in `_errors`): sizes there are meaningless (`/proc/kcore` "is" 128 TiB).
- The same goes for a `scan_paths` entry or glob root that is itself on a never-entered filesystem: a network
  mount (unless `include_network_mounts`), a pseudo filesystem, or an automount point of unknown type. It is
  checked against the mount table before anything is `stat`'ed, so a dead NFS server can't hang the run. So
  on a server whose `/home` is NFS, the default `/home/*` lands in `_errors` with a hint, instead of every
  node walking the shared home tree each night: measure shared storage on its file server (or set
  `include_network_mounts` on one node only).
- Each mount point is judged by the filesystem mounted on top of it. A systemd automount
  (`x-systemd.automount` in fstab) that is mounted counts as that filesystem. One not mounted right now is
  entered (which mounts it) only when `/etc/fstab` says it holds a local filesystem — or a network one with
  `include_network_mounts`; any other automount point is skipped with a warning.

**Metrics.** With psutil (in the venv) as on other platforms, per-user metrics included. Without it the
`/proc` fallbacks provide CPU, RAM, uptime and network, and there are no per-user metrics (an INFO line
says so).

```bash
systemctl list-timers 'lab-monitor-*'                    # next and last runs
journalctl -u lab-monitor-metrics.service -f              # metrics log
journalctl -u lab-monitor-disk.service --since today      # disk scan log
sudo systemctl start lab-monitor-metrics.service          # collect metrics now (waits, shows failure)
sudo systemctl start --no-block lab-monitor-disk.service  # scan disks now, in the background
sudoedit /etc/lab-monitor/collector.json
```

Run a scan through `systemctl start`, not `sudo -u labmon .../collector.py`: only the unit grants
`CAP_DAC_READ_SEARCH`, so a manual run as `labmon` can't read other users' private folders (they land in
`_errors`, or are skipped inside a measured folder and undercount it). A
manual run as root leaves root-owned files in `data_dir`; the installer gives them back to `labmon` on its
next run.

---

## Dependencies

- `requests` — HTTP POST to Manager (required)
- `psutil` — CPU, RAM, network measurements (optional; `/proc` fallbacks on Linux/Synology)
- `nvidia-ml-py` — NVIDIA GPU stats (optional; `requirements-gpu.txt`)

```bash
pip install -r requirements.txt
pip install -r requirements-gpu.txt   # only on machines with an NVIDIA GPU
```
