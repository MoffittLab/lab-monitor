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
| `exclude_users` | (Optional) Username patterns to exclude from user activity metrics, per-user GPU data, the sessions list and the `*_users` counts (not from storage owners, and not from `clients_total` / `<svc>_clients`, which count client IPs from the TCP table: use `sessions.exclude_clients` for those). Case-insensitive wildcards. Built-in defaults already exclude `UMFD-*`, `NT AUTHORITY\*`, etc.; sessions also leave out machine accounts (`*$`). |
| `exclude_system_accounts` | (Optional, Linux/Synology) `true` = leave daemon accounts out of the per-user metrics: UID 1 to `UID_MIN`−1 (from `/etc/login.defs`, 1000 on Ubuntu) and `nobody` (65534), such as `systemd-resolve`, `messagebus` or `nvidia-persistenced`. `root` and UIDs without an account on the host (e.g. a container's own user, shown as the number) are kept. Default `true` on Linux, `false` on Synology (its previous behaviour). The collector's own service account (`labmon`, a system account) is never counted either way, including the nightly disk scan. |
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

### Usage-metrics settings

Interval counters, per-user CPU and GPU, who is connected, storage health and the storage breakdown
(the usage-metrics release). Every key is optional: a missing key means the default, and an invalid
value falls back to the default with a `WARNING` in the log, so a typo never stops a run. Nested keys
live in a block (`"sessions": {"detail": "users"}`). The installers write all of them into new configs;
existing configs are not changed. The definitions are in [`lib/settings.py`](lib/settings.py) (`SCHEMA`),
which the collector and `config_tool.py` share.

| Key | Default | Meaning |
|-----|---------|---------|
| `perf_counters` | `true` | `false` restores the old 1-s CPU sample and psutil per-user seeding (no interval averages, no per-user CPU-seconds, no disk I/O) |
| `counter_max_window_seconds` | `86400` | Longest averaging window (minimum 60); a longer gap starts a new baseline |
| `user_metrics` | `"auto"` | `all`, `active`, `off` or `auto` (= `active` on Synology, `all` elsewhere). `active`: a user is reported when `cpu_cores` ≥ `user_metrics_min_cores`, RAM ≥ `user_metrics_min_ram_bytes`, or they hold a GPU. `off`: no `user_metrics` messages |
| `user_metrics_min_cores` | `0.02` | Threshold for `active` |
| `user_metrics_min_ram_bytes` | `1073741824` | Threshold for `active` (1 GiB) |
| `user_cpu_count_children` | `true` | Linux and DSM: credit the CPU time of exited child processes to their parent's user |
| `disk_io` | `true` | Disk throughput, IOPS, latency and busiest disk |
| `disk_io_exclude` | `[]` | Device-name globs left out of the disk totals, e.g. `["sdz", "usb*"]` (`PhysicalDrive3` on Windows) |
| `gpu_per_user` | `"auto"` | `"off"` = GPU stats per device only |
| `metrics_interval_seconds` | `300` | The GPU-seconds interval when `interval_s` is unusable |
| `sessions.enabled` | `true` | Send the `sessions` message (who is connected) |
| `sessions.detail` | `"full"` | `full`; `users` (no client IPs or host names); `counts` (no list) |
| `sessions.history` | `"changes"` | `changes` (the list when it changed, plus the heartbeat) or `every_run` |
| `sessions.list_heartbeat_seconds` | `3600` | Send the full list at least this often (minimum 60) |
| `sessions.max_entries` | `200` | List rows (1–5000); the counts are always complete |
| `sessions.ports` | `null` | `null` = the platform's map; `{"service": port or [ports]}` replaces it |
| `sessions.extra_ports` | `{}` | Added to the map, e.g. `{"jupyter": 8888}` |
| `sessions.exclude_clients` | `[]` | IPs or CIDRs left out of counts and list |
| `sessions.timeout_seconds` | `20` | Time budget of the sessions step |
| `storage_health.enabled` | `true` | Send `storage_health` (`--mode health` runs even when this is false) |
| `storage_health.interval_seconds` | `3600` | Heartbeat, and how often the expensive sources run (hwmon, Windows PowerShell); minimum 60 |
| `storage_health.timeout_seconds` | `90` | Windows PowerShell timeout; the step is abandoned after this + 10 s |
| `storage_health.temperature_c` | `{"hdd": [50, 60], "ssd": [70, 80]}` | Warning / critical °C; NVMe uses `ssd`. On Windows a reported `TemperatureMax` gives critical = that and warning = that − 5 |
| `storage_health.ignore` | `[]` | Component ids to leave out, e.g. `"sdb"`, `"/volume2"`, `"pd3"`, `"E:"` |
| `storage_breakdown.enabled` | `true` | Send `storage_breakdown` from the daily disk run (Linux, DSM and Windows) |
| `storage_breakdown.owners` | `"file"` | `file` (per-owner usage from file owners) or `none`; ignored on Windows, which has no owners |
| `storage_breakdown.top_owners` | `10` | Owners listed per folder (1–1000) |
| `storage_breakdown.device_top_owners` | `25` | Owners listed for the whole device (1–1000) |
| `storage_breakdown.max_rows` | `5000` | Owner rows over all folders (100–45000) |
| `storage_breakdown.cold_days` | `[365, 1095]` | The two "not modified for" thresholds, in days (two increasing integers) |
| `storage_breakdown.name_lookup_budget_seconds` | `30` | Total time for uid → name lookups; the rest stay `uid:<n>` |
| `storage_breakdown.max_timeout_fraction` | `0.6` | Time guard: a folder gets the breakdown walk only if its previous scan took at most this fraction of `timeout_seconds` (0.05–1) |

Service names in `sessions.ports` / `extra_ports` must match `^[a-z][a-z0-9]{0,15}$` and ports 1–65535;
an invalid entry is dropped on its own with a warning. Check a config without running anything:

```bash
python3 config_tool.py --config local/config.json check          # exit code 3 if a value is invalid
python3 config_tool.py --config local/config.json set sessions.detail users
python3 config_tool.py --config local/config.json set disk_io_exclude '["sdz"]'
```

`set` takes `block.key` for nested keys and parses values starting with `[`, `{`, `"` or `null` as JSON.
A value the collector would reject is replaced by its default, with a warning.

---

## Modes

```bash
python collector.py --config local/config.json --mode disk      # Daily
python collector.py --config local/config.json --mode metrics   # Every 5 min
python collector.py --config local/config.json --mode health    # By hand: a full storage-health check, sent now
python collector.py --config local/config.json --mode metrics --dry-run   # Print, send nothing
```

**`--dry-run`** works with every mode. It prints the messages as one JSON list on stdout (the log goes to
stderr) and archives, queues, sends and saves nothing: no state file, no network or perf snapshot, no
lock, no data directory. It still writes to the configured log file. Because it saves no baseline, a
metrics dry run shows interval averages only after at least one real run has written
`perf_snapshot.json`.

**`--mode health`** runs every storage-health source at once (`trigger: "manual"`) and sends the message,
even when `storage_health.enabled` is false. With `--dry-run` it is a quick way to see what the collector
account can read: `"sources"` lists each source's status.

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

#### Storage breakdown (`storage_breakdown`)

With `storage_breakdown.enabled` (default) the same walk also measures, per scanned folder, the file count,
how much has not been modified for 1 and 3 years (`cold_days`, by mtime; atime is never used), and on Linux
and DSM the same per file owner (`st_uid`, resolved to the account name, `uid:<n>` without one). It is sent
as a second message, `storage_breakdown`, next to `folder_usage`, which is built exactly as without it.
Windows gets bytes, file counts and cold data from the same PowerShell pass (`LastWriteTimeUtc`), with no
owners. Fields: [METRICS.md](../METRICS.md#storage_breakdown).

- **Time guard.** The breakdown walker is slower than the plain one. A folder gets it only when the previous
  disk run measured that folder in at most `max_timeout_fraction` (0.6) of `timeout_seconds`; the times are
  kept in `storage_breakdown_state.json`. So the **first disk run after the upgrade** measures every folder
  the old way and sends bytes and file counts only (every folder in `_breakdown_unavailable`); the second
  run has cold data and owners. If a breakdown walk still runs out of time, the folder is measured again
  without it in the same run, so `folder_usage` never loses a folder to the breakdown.
- **Cost.** No second walk: the counts come from the `lstat` the scan already makes. Measured on an x86 test
  box with a warm cache: +7.6 % wall time for 966k files. Not measured on armv7 or Windows yet: see
  [docs/FIELD_CHECKS.md](../docs/FIELD_CHECKS.md); turn it off with `"storage_breakdown": {"enabled": false}`
  if a NAS gets more than 15 % slower.
- Owner-name lookups (NSS, LDAP, winbind) get `name_lookup_budget_seconds` (30) in total; owners not
  resolved by then are sent as `uid:<n>`.
- The log line `Storage breakdown: N folder(s), M measured without it (first run, or too close to their
  timeout)` follows each disk run.

### Metrics mode (`system_metrics`)

Measures CPU%, RAM%, uptime, network traffic, disk I/O and GPUs, plus per-user metrics, who is connected and
storage health (below). Network bandwidth is computed by comparing against the previous run's snapshot.

**Interval averages.** CPU %, iowait, steal, disk throughput, IOPS, latency, the busiest disk and per-user
CPU-seconds are averages over the whole time since the previous run, from the kernel's counters
(`/proc/stat`, `/proc/diskstats` and `/proc/<pid>/stat` on Linux and DSM; `GetSystemTimes` and psutil on
Windows). The previous run's counters are kept in `perf_snapshot.json`. `interval_s` in the message is the
window. There is no window, and `cpu_percent` is the old 1-second sample, on the first run after the upgrade,
after a run less than 5 s ago (the old baseline is kept), after a gap longer than
`counter_max_window_seconds`, when another run holds `perf_snapshot.json.lock` for more than 20 s, and with
`perf_counters: false`. After a reboot the averages are since boot. Without a boot id (Windows) a wall-clock
step of 600 s or more starts a new baseline instead (logged at INFO). Fields:
[METRICS.md](../METRICS.md#system_metrics).

The INFO line of each run looks like:

```
CPU: 6.7% (avg 299s) | iowait: 0.7% | RAM: 15.9% | Uptime: 35d 5h | Network: {...} | Disk: r 0.0 B/s w 5.0 MB/s busy 11% sda | Users: 2 (coverage 100%) | GPUs: 2 (NVIDIA RTX A6000 37%, Tesla T4 n/a)
```

The first run shows `(1-s sample)`. `GPUs: ...` stays the last segment (`install-collector-linux.sh` reads it).

**Order of a run.** System and user metrics are archived and queued first. Then sessions and storage health
run in parallel threads within their budgets (`sessions.timeout_seconds`; `storage_health.timeout_seconds` +
10 s), their messages are recorded, the queue is sent once, and their state files are written last, only if
the record succeeded. A source that hangs or fails costs only its own message; a failed sessions step still
sends a `sessions` message with null counts and `_sources` set to `timeout` or `error`.

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
fallback, since loopback traffic never leaves the machine. On Linux and Synology, **virtual interfaces** are left
out too: those with no device behind them (no `/sys/class/net/<if>/device`), such as `docker0`, each
container's `veth*`, `br-*`, `virbr*`, bonds, VLANs, `tun`/`tap`, WireGuard and `tailscale0`. Their traffic is
also counted on a physical NIC, or never leaves the host. A veth also disappears when its container stops, so
counting it would make the totals drop, which the Manager would read as a reboot (and would add all traffic
since boot again). A bond is counted through its physical ports. If every interface is virtual (a collector
inside a container), they are all counted. `network_bandwidth_*_mbps` is the average rate since
the previous metrics run; it is `null` on the first run and right after a counter reset (reboot). If the
counters can't be read at all, all four fields are `null` and the stored snapshot is left untouched.

*Upgrade transition:* older collectors counted loopback on the psutil path. Dropping it at once would make the
counters fall, which the Manager reads as a reboot, adding all traffic since boot to the lifetime totals a second
time. So a psutil host upgraded from an older collector keeps counting loopback until its counters next reset
(reboot), then switches; `network_snapshot.json` records which mode is in use (`"loopback"`). New installs exclude
loopback from the first run. Virtual interfaces follow the same rule (`"virtual"` in the snapshot): a host
upgraded from a collector that counted them keeps counting them until its counters next go backwards (a reboot,
or a container stopping, which the Manager already reads as a reset), then stops.

Without psutil (common on Synology armv7l) the collector reads `/proc`: CPU and per-user CPU-seconds from the
counters above (the 1-s fallback is two `/proc/stat` samples one second apart), per-user RAM and process
counts from `/proc/<pid>/status`, RAM from `/proc/meminfo` (`MemAvailable`, or `MemFree + Buffers + Cached`
on older kernels), network from `/proc/net/dev`, uptime from `/proc/uptime`. Windows without psutil gets
system CPU % through `GetSystemTimes` (ctypes), but no per-user metrics, no disk I/O and no TCP session
counts.

#### Per-user metrics (`user_metrics`)

One message per reported user (config `user_metrics`; on Synology only active users by default). Each
process's CPU time is compared with the previous run's, so `cpu_seconds` covers the whole interval;
`cpu_percent` is now `cpu_cores / cpu_count × 100`. On Linux and DSM the CPU time of exited children is
recovered from their parents (`user_cpu_count_children`). `proc_cpu_coverage_percent` in `system_metrics`
says how much of the CPU the processes seen account for. Accounts left out: `exclude_users` (plus the
Windows defaults), on Linux `exclude_system_accounts`, and the collector's own account. A uid without an
account is `uid:<n>`. Per-user accounting is skipped, with a WARNING, when `/proc` is mounted with
`hidepid` (and the collector is neither root nor in its `gid=` group) or more than 20 % of
`/proc/<pid>` entries can't be read.

#### Who is connected (`sessions`)

Counts clients per service from the TCP table (a service counts only while something listens on one of its
ports; default ports in [METRICS.md](../METRICS.md#sessions)), and names users from utmp (or logind when
there is no utmp file, Ubuntu 25.10+; tmux panes, screen windows and detached mosh sessions are not
logins and are left out), `smbstatus -b` (only when the collector itself runs as root; never
sudo, never plain `smbstatus`, 15 s timeout), and on Windows WTS (console and RDP) and NetSessionEnum
(SMB; needs Administrators, which SYSTEM has). Without root on a NAS, SMB, NFS, AFP and the other services are
listed by client IP only. The full list is sent when it changes and every `list_heartbeat_seconds`; other
runs send the counts and `_list_state: "unchanged"`. `sessions_state.json` remembers the last full list's
hash; deleting it forces a full list. Log line:
`Sessions: 9 clients, 1 user (tcp ok, utmp ok, smb not_permitted) 0.01s`.

#### Storage health (`storage_health`)

Every run reads md RAID state (md sysfs, else `/proc/mdstat`), the disks in `/sys/block` (model, serial,
`device/state`, `ioerr_cnt`), `/proc/mounts` and, where it exists, `zpool list`; once per
`storage_health.interval_seconds` also the drivetemp / nvme temperatures (hwmon). On Windows one PowerShell
run per interval reads `Get-PhysicalDisk` with `Get-StorageReliabilityCounter`, Storage Spaces, volumes and
the System log's disk events. The message is sent when a status changes, once per interval, and by
`--mode health`. SMART is not read on Linux or DSM (it needs root). Rules:
[METRICS.md](../METRICS.md#storage_health). Log line:
`Storage health: ok (2 disk(s), 0 array(s), 1 volume(s); md ok, sysfs ok, mounts ok, hwmon unavailable); sent (change)`
or `...; unchanged, not sent`.

---

## Local Storage

```
data/<name>/
├── YYYY-MM.jsonl            ← append-only archive; one message per line (permanent)
├── queue.json               ← messages not yet acknowledged by the Manager
├── queue.json.lock          ← lock file guarding queue.json (empty; safe to leave)
├── queue.corrupt-<ts>.json  ← only if a queue.json was ever unreadable (see below)
├── network_snapshot.json    ← previous network counters, for bandwidth
├── perf_snapshot.json       ← previous CPU, disk and process counters, for the interval averages
├── perf_snapshot.json.lock  ← lock file guarding it (empty; safe to leave)
├── sessions_state.json      ← {last_full_utc, hash} of the last full session list
├── storage_health_state.json ← last sent / last full run, signature, cached hwmon and Windows results, error baselines
└── storage_breakdown_state.json ← {folder: seconds} of the last disk run (the breakdown time guard)
```

Both the archive and the queue use the same message format. The archive is a permanent local record written
before any network call — it is never deleted regardless of whether the Manager receives the data.

The state files of the usage-metrics release are written atomically, only after the run's messages were
recorded, and never by `--dry-run`. All can be deleted safely: a missing `perf_snapshot.json` (about 10–70 KB;
PIDs and counters, no user names) means one run without averages; a missing `sessions_state.json` sends the
full list next run; a missing `storage_health_state.json` sends a health message next run; a missing
`storage_breakdown_state.json` makes the next disk run measure every folder without the breakdown.
`tools/reset_system.py` moves `sessions_state.json` and `storage_health_state.json` into its backup folder
(they describe what the Manager was last sent) and leaves the other two (they describe the host).

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
`total_timeout_seconds` (and, on Windows, the Disk Collection task's time limit). If the disk run got slower
after the upgrade, compare with `"storage_breakdown": {"enabled": false}` (see
[docs/FIELD_CHECKS.md](../docs/FIELD_CHECKS.md)).

**No per-user metrics, WARNING about `hidepid`:**
`/proc` is mounted with `hidepid=1` or `2` (`grep -w proc /proc/mounts`), so the collector account can't see
other users' processes, or more than 20 % of `/proc/<pid>` entries were unreadable. Per-user data is then
skipped rather than reported for the visible processes only. Run the collector as root, add its account to
the group named by the mount's `gid=` option, or remount `/proc` with `hidepid=0`.

**`proc_cpu_coverage_percent` stays low:**
Much of the CPU went to processes that started and ended between two runs (build jobs, shell loops,
short-lived workers), so their CPU can't be given to a user. On Linux and DSM keep
`user_cpu_count_children: true`, which recovers the CPU of children their parents waited for. On Windows,
with no child-time recovery, and where psutil can't read protected processes (AccessDenied), coverage is
usually lower; the system-wide `cpu_percent` is still complete.

**GPU users show `—` for VRAM on Windows:**
The GPU runs under WDDM, which reports no per-process VRAM (see [GPU servers](#gpu-servers)). The held time,
process counts and busy split are still reported. The TCC driver model, on GPUs that support it, gives
per-process VRAM.

**A GPU job is not attributed on Windows (WDDM):**
`nvidia-smi` lists the job as "C+G": the collector counts such processes as graphics. A plain CUDA job shows
as "C".

**`smb not_permitted` in the Sessions log line:**
SMB user names need `smbstatus`, which only root may run. SMB clients are still counted and listed by IP.
On Windows `netsession not_permitted` means the task runs as a user without Administrators rights.

**Storage health `unknown`, or `hwmon unavailable`:**
`python collector.py --config local/config.json --mode health --dry-run` shows each source's status. hwmon
needs the `drivetemp` kernel module for SATA disks (`nvme` sensors need nothing); without it there are no
temperatures, which is not an error.

---

## Linux / Ubuntu

Installed by `install-collector-linux.sh` (Ubuntu 22.04 / 24.04, Debian 12; see
[INSTALLATION.md](../INSTALLATION.md#4-collector-on-linux-server-ubuntu--debian)). Example config:
[config.linux.example.json](config.linux.example.json).

| Path | Contents |
|------|----------|
| `/opt/lab-monitor/collector` | Code, owned by root (the service can't modify it) |
| `/opt/lab-monitor/venv` | Python venv: `requirements.txt` (+ `requirements-gpu.txt` on [GPU servers](#gpu-servers)) |
| `/etc/lab-monitor/collector.json` | Config, mode `0640 root:labmon` (it holds the token) |
| `/var/lib/lab-monitor/<name>/` | `data_dir`: archive, queue, network snapshot, the state files |
| `/var/log/lab-monitor/collector.log` | Log file (every line is in the journal too) |
| `/etc/systemd/system/lab-monitor-*` | Units, copied from [`systemd/`](systemd/) |

**Scheduling.** `lab-monitor-metrics.timer` starts `lab-monitor-metrics.service` every 5 minutes
(`OnCalendar=*:0/5`); `lab-monitor-disk.timer` starts `lab-monitor-disk.service` daily at 02:00 plus up to
15 minutes of random delay. Both timers are `Persistent=`, so a run missed while the server was off happens
at boot.

**Sandbox.** Both services run as the system user `labmon` (no login shell) with `ProtectSystem=strict`
(writable: only `/var/lib/lab-monitor` and `/var/log/lab-monitor`), `PrivateTmp`, `NoNewPrivileges`, a
system-call filter (`@system-service`) and only `AF_INET`/`AF_INET6`/`AF_UNIX` sockets; the disk service
also gets no W+X memory and no devices. The metrics service has no capabilities and no access to `/home`, but
can reach the NVIDIA devices (see [GPU servers](#gpu-servers)); it reads `/proc` for every user's processes (per-user metrics),
which world-readable `/proc` files allow (unless `/proc` is mounted with `hidepid=`). The disk service must
read every file under `scan_paths` whoever owns it, so it gets exactly one capability,
`CAP_DAC_READ_SEARCH` (skip read/search permission checks — no write access, nothing else of root), sees
`/home` read-only, and runs at `Nice=19` with idle I/O priority. Change a setting with a drop-in
(`sudo systemctl edit lab-monitor-disk.service`): e.g. `ReadWritePaths=` for a `data_dir` elsewhere.

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

**Metrics.** With psutil (in the venv) as on other platforms. Without it the `/proc` readers provide CPU,
RAM, uptime, network, disk I/O and per-user metrics (CPU-seconds, RAM, process counts). Only with
`"perf_counters": false` and no psutil are there no per-user metrics (an INFO line says so).

**What the usage-metrics features read here, and what needs root.** Everything runs in the existing two
units, as `labmon`, with no new capability; neither unit was changed. The metrics unit reads `/proc/stat`,
`/proc/uptime`, `/proc/diskstats`, `/proc/mounts`, `/proc/<pid>/stat` and `status` of every process,
`/proc/sys/kernel/random/boot_id`, `/proc/net/tcp` and `tcp6`, `/proc/mdstat`, `/sys/block`,
`/sys/class/hwmon`, `/run/utmp`, `/run/systemd/sessions`, `/etc/passwd` and `/etc/fstab`, and the NVIDIA
devices. `ProtectSystem=strict`, `ProtectHome=yes` and `ProtectKernelTunables` leave all of these readable,
and the unit has no `ProtectProc`, `ProcSubset`, `PrivateUsers` or `PrivateDevices`.
`tests/collector/test_collector_linux.py` fails if the unit gains one of those blockers. The state files go
in `/var/lib/lab-monitor/<name>/`. The disk unit keeps only `CAP_DAC_READ_SEARCH`: the storage breakdown
uses the `lstat` the scan already makes, and owner names come through NSS (`/etc/passwd`, or
sssd/nscd/winbind over `AF_UNIX`, which the unit allows).
- **Not available without root, and not collected:** SMART data, and SMB user names (`smbstatus` refuses
  non-root users; `_sources.smb` reads `not_permitted`, and SMB clients are still counted by IP). No root
  helper is part of this release (see [Possible future options](#possible-future-options)).
- A drop-in adding `ProtectProc=invisible`, `ProcSubset=pid` or `PrivateUsers=` would hide other users'
  processes, and with them per-user metrics and per-user GPU attribution.
- `/proc` mounted with `hidepid=1/2` hides other users' processes from `labmon` too; the collector then
  skips per-user data with a WARNING.

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

### GPU servers

A Linux server with an NVIDIA GPU reports exactly what a Windows GPU server reports, from the same code
(nvidia-ml-py, i.e. NVML): in `system_metrics`, `gpu_count`, the `gpus` list (per GPU: `index`, `name`,
`gpu_percent`, `vram_used_bytes`, `vram_total_bytes`, `vram_used_formatted`, `vram_total_formatted`,
`temperature_c`, `power_watts`) and the flat history fields `gpu_N_percent` (`%`), `gpu_N_vram_used`
(`bytes`), `gpu_N_temp_c` (`°C`) and `gpu_N_power_w` (`W`), each with its `*_unit` sibling. A value the GPU
can't report is `null`; `gpu_N_temp_c` / `gpu_N_power_w` are left out for a GPU without that sensor.
[`tests/collector/test_collector_gpu_parity.py`](../tests/collector/test_collector_gpu_parity.py) builds the
metrics, per-user and disk messages as Windows and as Linux and fails on any difference in keys, units or
value types. The expected differences are in its `KNOWN_DIFFERENCES`: Linux paths and user names, and a per-user
row for `root`. Windows hides its service accounts by default, and Linux hides daemon accounts
(`exclude_system_accounts`, on by default). Neither platform counts the collector itself: on Linux that means
every process of `labmon`, the nightly disk scan included. `root` stays visible because docker containers and
`sudo` jobs run as root. So that row also carries the system services and dockerd; add `"root"` to
`exclude_users` to hide it. Network totals leave out docker's virtual interfaces (see
[Metrics mode](#metrics-mode-system_metrics)), so containers starting and stopping don't reset them.

**Install.** The installer adds `requirements-gpu.txt` (pinned `nvidia-ml-py`) when it finds an NVIDIA
driver by any of: `/proc/driver/nvidia/version` (kernel module loaded), `libnvidia-ml.so.1` in `ldconfig -p`,
or `nvidia-smi` on `PATH`. It prints which it found. A re-run or upgrade keeps nvidia-ml-py when the venv
already has it, even if the driver isn't visible at that moment. The installer then runs NVML as `labmon` and
explains a failure, for example a driver/library version mismatch (reboot after a driver upgrade), missing
`/dev/nvidia*` nodes or restrictive device permissions. After the test run it checks the service's
`GPUs: N (...)` log line.

**Sandbox (metrics unit).** NVML opens `/dev/nvidiactl`, `/dev/nvidia0`..`N` (plus `/dev/nvidia-caps/*`, and
`/dev/nvidia-nvswitch*` on NVSwitch systems). It reads `/proc/driver/nvidia` and `/sys`, talks to
nvidia-persistenced over `AF_UNIX`, and loads `libnvidia-ml.so.1` from wherever `ldconfig` finds it
(`/usr/lib/x86_64-linux-gnu` on Ubuntu; `ProtectSystem=strict` leaves it readable and loadable). So
[`lab-monitor-metrics.service`](systemd/lab-monitor-metrics.service), unlike the disk unit:
- has no `PrivateDevices=yes` (it hides every `/dev/nvidia*`), and no `DevicePolicy=closed|strict` or
  `DeviceAllow=` (any allow-list blocks every device it doesn't name);
- has no `ProtectClock=yes`. On systemd < 252, which includes Ubuntu 22.04's systemd 249, that setting implies
  `DeviceAllow=char-rtc r` and blocks the GPUs the same way
  ([systemd#18142](https://github.com/systemd/systemd/issues/18142)). Without capabilities the service can't
  set the clock anyway;
- has no `ProcSubset=pid` (it hides `/proc/driver/nvidia`) or `ProtectProc=invisible`;
- sets `MemoryDenyWriteExecute=no`. NVIDIA doesn't document libnvidia-ml as W^X-safe. Its 470 and 580 builds
  have no executable stack, text relocations or W+X segments, but some memory protections are computed at run
  time and it can load `libcuda.so.1`. For a Python service the setting adds little;
- keeps `SystemCallFilter=@system-service`. libnvidia-ml's system calls (`ioctl`, `mmap`, the NUMA calls in
  `@resources`, fork+exec of nvidia-modprobe) are all in that set. The one exception, `iopl`, needs
  `CAP_SYS_RAWIO` anyway.

`tests/collector/test_collector_linux.py` fails if the unit gains any of those blockers. To restrict devices
in a drop-in anyway, allow the NVIDIA majors too: `DevicePolicy=closed` plus `DeviceAllow=char-nvidia* rw`
(the pattern matches the `nvidia*` names in `/proc/devices`). With `NoNewPrivileges=yes`, NVML can't use the
setuid `nvidia-modprobe` to create missing device nodes, so they must exist before the service runs:
`sudo systemctl enable --now nvidia-persistenced` creates them at boot.

**Troubleshooting.** NVML problems are logged as warnings in `journalctl -u lab-monitor-metrics.service` and
`collector.log`. `NVIDIA GPU stats unavailable: NVML could not start (...)` names the cause:

| NVML error | Likely cause |
|------------|--------------|
| `Driver Not Loaded`, `Unknown Error` | kernel module not loaded, `/dev/nvidiactl` missing, or a sandbox blocks the devices |
| `Insufficient Permissions` | `/dev/nvidia*` not readable by `labmon` (`NVreg_DeviceFileMode` / `NVreg_DeviceFileGID`): add `labmon` to that group |
| `RM has detected an NVML/RM version mismatch` | driver upgraded without a reboot: reboot |
| `NVML Shared Library Not Found` | `libnvidia-ml.so.1` not in `ldconfig -p` (driver's NVML package missing) |

`NVML errors (values sent as null): GPU 1 utilization: ...` means a single query failed. "Not supported"
(e.g. no power sensor) is normal and isn't logged.

**First-install checklist (GPU servers):**
```bash
# 1. NVML works for the service user (prints the number of GPUs)
sudo -u labmon /opt/lab-monitor/venv/bin/python -s -c "import pynvml; pynvml.nvmlInit(); print(pynvml.nvmlDeviceGetCount())"
# 2. The service found them, and NVML logged no errors
journalctl -u lab-monitor-metrics.service --since '15 min ago' | grep -E 'GPUs:|NVML'
# 3. The collector's values match the driver's
nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv
```
Compare step 3 with the GPU section of the device's Dashboard card, or with the `GPUs:` log line and
`gpu_N_*` fields in `/var/lib/lab-monitor/<name>/<YYYY-MM>.jsonl`. Temperature should agree within a degree
or two and power within a few watts. Utilization is a sample, so compare under a steady load. VRAM used can
differ by the driver-reserved amount (a few hundred MiB), since NVML's memory queries and nvidia-smi
account for reserved memory differently.

**Per-user GPU** (usage-metrics release, Linux and Windows alike). NVML's compute, MPS and graphics process
lists are merged per GPU, each process's owner is found (the counters' process walk, then
`/proc/<pid>/status` on Linux, then psutil), and per GPU the collector sends `users` (VRAM, attributed busy %,
process count per user, at most 20), and per user in `user_metrics` `gpu_vram_bytes`, `gpu_util_percent`,
`gpu_busy_seconds`, `gpus_held`, `gpu_held_seconds` and `gpu_processes`. Fields and the busy-% split:
[METRICS.md](../METRICS.md#user_metrics). As `labmon` on Linux this works for every user's processes:
NVML lists them for non-root callers and `/proc` gives the owner.
`tests/collector/test_collector_gpu_parity.py` proves the per-user GPU fields identical in keys, units and
types between Linux and Windows TCC; Windows WDDM differs only in per-process VRAM (null).

- **Windows TCC vs WDDM.** Under TCC (datacenter GPUs) everything is reported. Under WDDM (the desktop driver
  model, also used by many workstation GPUs) NVML gives no per-process VRAM, so `vram_bytes` and
  `gpu_vram_bytes` are null; held GPUs, process counts and the busy split still work. The Windows installer
  prints each GPU's driver model (`nvidia-smi --query-gpu=index,name,driver_model.current`).
- **The WDDM "C+G" rule.** Under WDDM desktop processes (explorer.exe, browsers) appear in both the compute
  and the graphics list ("C+G" in `nvidia-smi`). A process in both lists counts as graphics: it adds VRAM but
  doesn't hold the GPU or take a share of its busy %. A CUDA job that shows as compute-only ("C") still
  holds it. An unknown driver model counts as WDDM (harmless under TCC, which has no graphics processes).
- Drivers without `nvmlDeviceGetComputeRunningProcesses_v3` give `process_source: null` for that GPU and one
  warning naming the driver. Windows drivers 500.00–525.99, which list every GPU's processes for each GPU,
  turn per-process data off with a warning. MIG GPUs give null or `ambiguous` splits.
- An INFO line is logged when a GPU's VRAM is over 50 % used but none of its processes are visible to the
  collector.
- `"gpu_per_user": "off"` keeps only the device stats.

Not reported yet, and possible later additions: fan speed, clocks, ECC errors and throttle reasons.

---

## Possible future options

Neither of these is part of this release, and nothing needs to be set up for them.

**A privileged helper.** A small root-owned job (for example a DSM Task Scheduler task running as root)
could write `smbstatus -b` and `smartctl` output into files the non-root collector reads. That would give SMB
user names and SMART attributes (reallocated and pending sectors, power-on hours) on a NAS without running
the collector itself as root. It is not implemented: the owner decided that the NAS units get nothing that
needs root. The code marks where it would plug in (`lib/sessions.py` `_user_sources`, `lib/storage_health.py`),
and the status values `stale` and `unsafe` are reserved for its files.

**SNMP on DSM.** DSM's SNMP agent publishes disk status, temperature, remaining life and bad-sector counts,
the RAID and SMART tables, and per-service user counts (AFP, NFS, FTP, SFTP, HTTP), all without root. Reading
it from `127.0.0.1` would add these to storage health and sessions. It is not implemented in this release;
the `snmp` source names are reserved.

---

## Upgrading

### Upgrading to the usage-metrics release

**Order.** Upgrade the Manager and the Dashboard first (the installer does both; the new Dashboard works with
the old and the new Manager), then the collectors: one Linux GPU server, then one x86 Synology, then one
armv7 Synology, then the Windows servers, then the rest, each after its checks in
[docs/FIELD_CHECKS.md](../docs/FIELD_CHECKS.md) have passed. **Never point a new collector at an OLD
(`f086e7e`) Manager:** it works, but the OLD Manager backfills each new column with a full-table UPDATE (slow
on a large `user_metrics` table, so a batch can time out and, without dedup, be stored twice). A CURRENT
(`e5cd699`, `160da09` or `fb6498c`) Manager stores the new messages in wide tables, which a later NEW Manager migrates.

What changes on a collector:

- New local files `perf_snapshot.json` (+ `.lock`), `sessions_state.json`, `storage_health_state.json` and
  `storage_breakdown_state.json` (see [Local Storage](#local-storage)). An older collector ignores them, so
  rolling a collector back is just checking out the old code.
- The first metrics run has no averages (`interval_s` null, `cpu_percent` the 1-s sample); from the second
  run on `cpu_percent` is the average over the interval. The default run no longer sleeps 1 s.
- The first disk run sends `storage_breakdown` with bytes and file counts only; the second has cold data
  and owners (the time guard).
- New messages per host: one `sessions` per run and about 24 `storage_health` a day, plus one
  `storage_breakdown` per disk run.
- Linux and DSM hosts without psutil (armv7 DSM) now send per-user metrics; on DSM only active users by
  default (`user_metrics: "auto"`).
- On Linux an account-less uid (a container's user) is now `uid:<n>` instead of psutil's bare number, so such
  a user appears as a new row.
- Existing configs keep working unchanged; every new key has a default. `config_tool.py check` lists
  invalid values.

### Upgrading from the previous collector (review-fix release)

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
- `psutil` — CPU, RAM, network measurements (optional; `/proc` readers on Linux/Synology). On Windows it is also what per-user metrics, disk I/O and the TCP session counts need
- `nvidia-ml-py` — NVIDIA GPU stats (optional; `requirements-gpu.txt`; see [GPU servers](#gpu-servers))

```bash
pip install -r requirements.txt
pip install -r requirements-gpu.txt   # only on machines with an NVIDIA GPU
```
