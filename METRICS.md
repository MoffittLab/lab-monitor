# Metrics Collection

Reference for the collector's modes, the messages they produce, and how the Manager stores them.
The exact wire rules (types, caps, validation, compatibility) are in
[docs/PROTOCOL.md](docs/PROTOCOL.md); what is recorded about people is in
[docs/PRIVACY.md](docs/PRIVACY.md).

---

## Collection Modes

| Mode | Flag | Schedule | `data_type` | What it measures |
|------|------|----------|-------------|-----------------|
| Disk | `--mode disk` | Daily, 02:00 | `folder_usage`, `storage_breakdown` | Folder sizes, volume used/free/total, grand total; per folder also file counts, cold data and (Linux, DSM) storage per owner |
| Metrics | `--mode metrics` | Every 5 min | `system_metrics`, one `user_metrics` per reported user, `sessions`, and `storage_health` when due | CPU % averaged over the interval, iowait, RAM, uptime, network, disk I/O, GPUs; per-user CPU-seconds, RAM and GPU use; who is connected; RAID, disk and volume state |
| Health | `--mode health` | By hand | `storage_health` (`trigger: "manual"`) | A full storage-health check, sent now |

`--dry-run` works with every mode: it prints the messages as one JSON list on stdout and archives,
queues, sends and saves nothing ([collector/README.md](collector/README.md#modes)).

Both modes use the same pipeline:

```
Collect data
    ↓
Append to YYYY-MM.jsonl           (permanent local archive)
    ↓
Append to queue.json              (under queue.json.lock)
    ↓
POST to the Manager in batches    (at most max_batch_messages, default 100, oldest first)
    ↓
Manager answers per message: stored / duplicate / rejected / error
    ↓
Remove stored, duplicate and rejected messages from queue.json; keep error ones for the next run
```

Details of the handshake: [collector/README.md](collector/README.md#queue-and-handshake) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#queue-and-acknowledgement).

**Unmeasured values are `null`.** When the collector can't measure something (CPU, RAM, uptime,
network counters, a GPU field, a folder), it sends `null` or leaves the key out, never `0`. The
Manager stores `NULL`, and the Dashboard shows `—`. A measured zero is `0`.

---

## Message Format

```json
{
  "header": {
    "msg_id":      "5b0d6c1e9f8a4c3b9e2d7a6f1c0b4e3d",
    "device_name": "Triton",
    "device_id":   "synology-triton",
    "device_type": "synology",
    "ip_address":  "10.0.0.42",
    "timestamp":   "2026-08-04T07:00:00.123456Z"
  },
  "data": {
    "data_type": "...",
    "...": "..."
  }
}
```

`msg_id` is assigned when the message is created and kept through every retry; the Manager uses it
to recognize re-sent messages. Messages from older collectors have no `msg_id`; the Manager derives
one (`legacy-` + sha1 of `name|data_type|timestamp|username`).

### folder_usage

The payload is flat: folder paths are keys, sizes in bytes are values. Keys starting with `_` are
metadata, never folders.

```json
{
  "data_type":            "folder_usage",
  "_unit":                "bytes",
  "/volume1/JeffMoffitt": 4832847265792,
  "/volume1/LabData":     12043821957120,
  "/volume1":             16876669222912,
  "/volume1_total_bytes": 20000000000000,
  "/volume1_free_bytes":  3123330777088,
  "/volume2":             8000000000000,
  "/volume2_total_bytes": 16000000000000,
  "/volume2_free_bytes":  8000000000000,
  "total_usage":          24876669222912,
  "_timed_out":           ["/volume2/Archive"],
  "_errors":              [],
  "_scan_seconds":        5321.4,
  "_volumes":             ["/volume1", "/volume2"]
}
```

| Key | Meaning |
|-----|---------|
| `<folder path>` | Total size of everything in that folder |
| `<volume>` | Used bytes of the volume (from the filesystem) |
| `<volume>_total_bytes`, `<volume>_free_bytes` | Volume capacity |
| `total_usage` | Sum of the measured volumes (`null` if none could be measured) |
| `_timed_out` | Folders that hit a time budget; they are left out, not reported as 0 |
| `_errors` | Folders (or glob roots / volumes) that could not be scanned, and configured paths that don't exist |
| `_scan_seconds` | Duration of the folder scan |
| `_volumes` | Which of the keys are volumes (those whose capacity was measured). Sent by the current collector on every platform. On Linux a volume can be `/mnt/data` and a folder `/data`, so path depth can't tell them apart; the Dashboard uses this list, and for older collectors without it falls back to the `_total_bytes` keys, then path depth. |

Volume capacity is measured first and always sent. A scan path equal to a volume root is not
scanned (the volume's own figure is authoritative).

### system_metrics

```json
{
  "data_type":                  "system_metrics",
  "cpu_percent":                18.4,
  "ram_percent":                61.2,
  "total_ram_bytes":            68719476736,
  "uptime_seconds":             863492,
  "uptime_formatted":           "9d 23h",
  "network_bytes_in":           847392719104,
  "network_bytes_out":          123847392012,
  "network_bandwidth_in_mbps":  30.72,
  "network_bandwidth_out_mbps": 1.68,
  "gpu_count":                  0,
  "gpus":                       []
}
```

Each numeric field also has a `<field>_unit` key (`%`, `bytes`, `s`, `Mbps`), used for chart labels.
With GPUs, `gpus` lists them and flat keys `gpu_<i>_percent`, `gpu_<i>_vram_used` (and
`gpu_<i>_temp_c`, `gpu_<i>_power_w` when available) are added.

- `network_bytes_in/out`: cumulative OS counters since boot, summed over all interfaces except
  loopback. A collector upgraded on a host with psutil keeps counting loopback until the next reboot
  (so the counters don't drop at the upgrade), then excludes it.
- `network_bandwidth_in_mbps` / `_out_mbps`: **megabits per second**, the average since the previous
  metrics run: `(bytes_delta / seconds) * 8 / 1,000,000`. `null` on the first run and after a counter
  reset (reboot).
- If the counters can't be read, all four network fields are `null`.

**Interval counters** (usage-metrics release). `cpu_percent` keeps its name but is now the average
over the whole time since the previous run, from the kernel's CPU counters (`/proc/stat` on Linux
and DSM, `GetSystemTimes` on Windows, with or without psutil). `interval_s` is that window. When
`interval_s` is `null` (every row stored before this release, the first run after the upgrade or a
reboot without a usable baseline, a run less than 5 s after the previous one, `perf_counters:
false`), `cpu_percent` is the old 1-second sample. The new fields, each with a `<field>_unit`
sibling (except `cpu_count` and `disk_busy_max_name`):

| Field | Unit | Meaning; when it is `null` |
|-------|------|----------------------------|
| `interval_s` | s | Window covered by every counter-derived field; null without a valid baseline |
| `cpu_count` | – | Logical CPUs (`os.cpu_count()`) |
| `cpu_iowait_percent`, `cpu_steal_percent` | % | Share of the interval spent waiting for I/O / stolen by the hypervisor; null on Windows and without a baseline |
| `proc_cpu_coverage_percent` | % | CPU attributed to processes ÷ CPU processes could have used (see [Accuracy](#accuracy)); null without per-process data |
| `disk_read_bytes_per_s`, `disk_write_bytes_per_s` | B/s | Summed over whole physical disks; null without disk counters (Windows without psutil) or a baseline |
| `disk_read_iops`, `disk_write_iops` | ops/s | The same, operations per second |
| `disk_await_ms` | ms | Average time per I/O; null when no I/O happened |
| `disk_busy_max_percent`, `disk_busy_max_name` | %, – | The busiest physical disk and its name (`sda`, `sata2`); null on Windows |
| `gpu_sample_seconds` | s | The dt behind the GPU-seconds: `interval_s` when it is between 0 and 900 s, else `metrics_interval_seconds` (300). Only on hosts with GPU data. |

Disk I/O counts only whole physical disks (those with `/sys/block/<n>/device`, minus `sr*`,
`synoboot*`, `*boot[0-9]*`, `*rpmb` and `disk_io_exclude`). md arrays, partitions, `loop`, `zram`,
`dm-*`, `nbd` and `ram` devices are never summed, so traffic is not counted twice. Sectors in
`/proc/diskstats` are 512-byte units. Windows needs psutil (`PhysicalDriveN` disks).

**Per-GPU users.** Where NVML works, each `gpus[i]` also has `uuid`, `process_source` (`"nvml"`, or
`null` when there is no per-process data for that GPU), `util_split` (`single_user`,
`process_samples`, `idle`, `ambiguous` or `null`: how the GPU's busy % was split), `users` (up to 20
`{username, vram_bytes, util_percent, processes}`, largest VRAM first), `users_truncated`,
`unattributed_vram_bytes` and `unattributed_processes` (processes of excluded or unknown owners).
Without per-process data for a GPU the last four are `null`. The device keys and the flat `gpu_N_*`
columns are unchanged.

### user_metrics

One message per reported user per metrics run. Which users are reported is config `user_metrics`:
`auto` (default) reports only active users on Synology and every user elsewhere; `active` reports a
user whose `cpu_cores` ≥ `user_metrics_min_cores` (0.02), whose RAM ≥ `user_metrics_min_ram_bytes`
(1 GiB) or who holds a GPU (on a run without a window only the RAM and GPU tests apply); `all`
reports every user; `off` sends none. System accounts, `exclude_users` patterns and the collector's
own account are skipped (`exclude_system_accounts` on Linux).

```json
{"data_type": "user_metrics", "username": "alice", "cpu_percent": 12.5,
 "ram_bytes": 8589934592, "ram_formatted": "8.0 GB",
 "cpu_seconds": 1200.4, "cpu_cores": 4.0, "interval_s": 300.2, "process_count": 17,
 "gpu_vram_bytes": 21474836480, "gpu_util_percent": 87.0, "gpu_busy_seconds": 261.2,
 "gpus_held": 1, "gpu_held_seconds": 300.2, "gpu_processes": 2}
```

user_metrics has no `*_unit` keys; the units below are the Manager's defaults for charts.

| Field | Unit | Meaning; when it is `null` or absent |
|-------|------|--------------------------------------|
| `cpu_percent` | % | `cpu_cores / cpu_count × 100` over the interval (it used to be a 1-s sample); null without a window |
| `ram_bytes`, `ram_formatted` | bytes | Resident memory of the user's processes; null for a user seen only on a GPU |
| `cpu_seconds` | s | CPU-seconds used in the interval, all cores; null without a window |
| `cpu_cores` | cores | `cpu_seconds / interval_s`; null without a window |
| `interval_s` | s | Same window as `system_metrics.interval_s` |
| `process_count` | – | The user's processes when measured, zombies excluded |
| `gpu_vram_bytes` | bytes | VRAM held now, over all GPUs; null if any of the user's processes has unknown VRAM (Windows WDDM) |
| `gpu_util_percent` | % | Attributed busy % over the GPUs the user holds; 0 if none; null when a held GPU's split is `ambiguous` or unknown |
| `gpu_busy_seconds` | s | Σ over GPUs of `util / 100 × gpu_sample_seconds` (an estimate, see [Accuracy](#accuracy)) |
| `gpus_held`, `gpu_held_seconds` | –, s | GPUs where the user has a compute process, and `gpus_held × gpu_sample_seconds` |
| `gpu_processes` | – | The user's compute processes on all GPUs |

The CPU fields appear wherever per-process accounting works: Linux and DSM through `/proc`, with or
without psutil (armv7 DSM units now send per-user metrics too); Windows only with psutil. The six GPU
fields appear on every row of a host where the per-process GPU query worked on every GPU, with 0 for
users who use no GPU; on other hosts they are left out, never partial. A user seen only on a GPU gets
a row with `cpu_percent`, `ram_bytes` and `ram_formatted` `null`. An account-less uid (a container's
user) is named `uid:<n>`; older collectors sent the bare number, so such a user appears as a new row
after the upgrade.

### sessions

Who is connected, one message per metrics run (`sessions.enabled`, default on). Client counts per
service come from the TCP table on every platform; named users from the platform's login sources:

| Source (`_sources` key) | Platform | What it gives |
|-------------------------|----------|---------------|
| `tcp` | all | Clients per service (`/proc/net/tcp*`; psutil on Windows, else `unavailable`) |
| `utmp`, or `logind` when no utmp file exists | Linux, DSM | SSH and console logins by name (mosh logins count as SSH; tmux panes, screen windows and detached mosh sessions are not logins and are left out) |
| `smb` | Linux, DSM | SMB users by name from `smbstatus -b`, only when the collector runs as root (else `not_permitted`); left out when SMB isn't listening |
| `wts` | Windows | Console and RDP sessions, with their state |
| `netsession` | Windows | SMB sessions by name (NetSessionEnum level 10; needs Administrators, which SYSTEM has) |

A service counts only while something listens on one of its ports, so a missing `<svc>_clients` key
means the service isn't running and `0` means nobody is connected. Default ports:

| Platform | Services |
|----------|----------|
| Synology | smb 445/139, nfs 2049, afp 548, ssh 22, dsm 5000/5001, drive 6690, rsync 873, iscsi 3260, ftp 21, webdav 5005/5006 |
| Windows | smb 445, rdp 3389, ssh 22 |
| Linux | ssh 22, smb 445/139, nfs 2049, rdp 3389 |

```json
{"data_type": "sessions", "_schema": 1, "_unit": "count", "_detail": "full",
 "_sources": {"tcp": "ok", "utmp": "ok", "smb": "not_permitted"}, "_collect_seconds": 0.04,
 "_list_state": "full", "_list_hash": "3f9a1c2b7d4e5f60",
 "clients_total": 9, "users_total": 1, "smb_clients": 7, "nfs_clients": 1, "ssh_clients": 1,
 "ssh_users": 1, "console_users": 0, "dsm_clients": 2, "rsync_clients": 0, "sessions_truncated": false,
 "sessions": [{"service": "ssh", "username": "alice", "client": "10.0.0.40", "connections": 1,
               "since": "2026-11-21T12:35:00Z", "source": "utmp"},
              {"service": "smb", "client": "10.0.0.17", "connections": 2, "source": "tcp"}]}
```

- `clients_total`: distinct client IPs; `<svc>_clients` per service. `users_total`: distinct user
  names (DOMAIN\ prefix stripped, case-folded on Windows); `ssh_users`, `console_users`,
  `smb_users`, `rdp_users` (users with a session that isn't disconnected), `rdp_disconnected`
  (Windows). Each count is present only when its source worked.
- List rows: `service`, `username`, `client` (IP), `client_host` (a name the protocol supplied, never
  DNS), `connections`, `since` (UTC), `state` (WTS only), `source`. On a NAS without root, SMB, NFS,
  AFP and the other services appear by client IP only.
- The list is sent in full (`_list_state: "full"`) when it changed, on the first run, every
  `sessions.list_heartbeat_seconds` (3600) or every run with `sessions.history: "every_run"`;
  otherwise `"unchanged"` with the same `_list_hash` and no list. The counts are sent every run.
- `sessions.detail`: `full`; `users` (no client IPs or host names, no TCP-only rows); `counts` (no
  list, `_list_state: "none"`).
- A failed or timed-out sessions step still sends a message with null counts and `_sources` set to
  `error` / `timeout`.

### storage_health

RAID, disk and volume state, built in the metrics run and sent when the status changes
(`trigger: "change"`), every `storage_health.interval_seconds` (3600, `"interval"`), or by
`--mode health` (`"manual"`). Temperatures and percentages alone don't trigger a message.

| Source | Platform | What it reads (without root) |
|--------|----------|------------------------------|
| `md` | Linux, DSM | md sysfs (`/sys/block/md*/md`), else `/proc/mdstat`: state, level, members, resync progress |
| `sysfs` | Linux, DSM | `/sys/block/<disk>`: size, rotational, model, serial (`device/serial` or `vpd_pg80`), `device/state`, `ioerr_cnt` |
| `mounts` | Linux, DSM | `/proc/mounts`: whether a volume is mounted read-only |
| `hwmon` | Linux, DSM | drivetemp and nvme temperatures, read only hourly (drivetemp can reset a disk's spin-down timer) |
| `zpool` | Linux | `zpool list -H -o name,health`, only where `zpool` exists |
| `windows_storage`, `events` | Windows | One PowerShell run per `interval_seconds` (killed after `storage_health.timeout_seconds`, 90 s): physical disks with reliability counters, Storage Spaces virtual disks and jobs, volumes, and System-log disk events since the previous run |

Not collected in this release: SMART on Linux and DSM (it needs root), DSM SNMP, a privileged helper.
Disks behind a Windows hardware RAID controller appear with bus `raid` and blank counters.

Each array, disk and volume gets a status (`ok`, `warning`, `critical`, `unknown`);
`overall_status` is the worst of them (`unknown` only when nothing could be assessed) and
`overall_status_code` its number (0, 1, 2, null). `status_reasons` lists up to 10 problems, worst
first. The rules:

- **md data arrays:** critical when inactive or failed, or degraded with no redundancy left (raid1
  down to 1 member, raid5 down 1, raid6 down 2, raid10 down 2) unless rebuilding; warning while
  rebuilding (recover, resync, reshape: `md2 rebuilding 37.5% (ETA 5h 12m)`), when degraded with
  redundancy left, or with a faulty member; a check or repair is ok with a note. DSM's `md0`
  (system) and `md1` (swap): missing members are empty bays (ok), a faulty member is a warning.
- **zpool:** ONLINE ok, DEGRADED warning, anything else critical.
- **Disks:** device state offline / transport-offline / dead is critical; temperature against
  `storage_health.temperature_c` (HDD warn 50 °C, crit 60 °C; SSD and NVMe 70 / 80 °C); Windows
  Unhealthy, Predictive Failure or Lost Communication is critical, Warning a warning; rising
  uncorrected read/write errors (held 24 h) and wear ≥ 90 % are warnings. `ioerr_cnt` is reported as
  `io_errors` but never sets a status: it also counts failed probe commands and rises on healthy
  disks.
- **Volumes:** mounted read-only when `/etc/fstab` doesn't say `ro` is critical; a configured DSM
  `/volumeN` that isn't mounted is critical; Windows Unhealthy critical, Warning a warning.
- **Windows events:** disk 52 critical; disk 7/11/51/154 or ntfs 50/55/98 a warning; disk 153 or a
  miniport's 129 at 10 or more per hour a warning. Only critical, error and warning events count:
  ntfs 98 is also logged, as information, when a volume is checked and found healthy.

The message carries `arrays`, `disks` (with model and serial) and `volumes` lists, plus flattened
top-level numbers for history: `disk_<sid>_{temperature_c, status_code, power_on_hours,
reallocated_sectors, pending_sectors, uncorrectable_sectors, wear_used_percent, io_errors}` (first 48
non-USB disks), `array_<sid>_{status_code, degraded, sync_percent}` and `volume_<sid>_status_code`
(first 32 each). `sid` is the id lowercased with other characters as `_` (`/volume1` → `volume1`,
`E:` → `e`, `/` → `root`). Example: [docs/PROTOCOL.md](docs/PROTOCOL.md#appendix-example-payloads).

### storage_breakdown

Storage attribution, sent by the daily disk run next to `folder_usage` as its own message
(`storage_breakdown.enabled`, default on). For each folder of `scan_paths` it reports `bytes`,
`files`, `cold_1y_bytes` and `cold_3y_bytes` (content not modified for more than
`cold_days[0]` / `cold_days[1]` days, default 365 / 1095, by mtime), and on Linux and DSM the same
per file owner: the top `top_owners` (10) owners in `owners` and everyone else in `other`, plus the
folder's own `folder_owner`. `device` rolls the top-level folders up (nested folders are not counted
twice), with the top `device_top_owners` (25) owners.

```json
{"data_type": "storage_breakdown", "_schema": 1, "_unit": "bytes", "reference_time": "2026-11-21T02:00:03Z",
 "cold_thresholds_days": [365, 1095], "owner_source": "posix_uid", "age_source": "mtime",
 "top_owners": 10, "top_owners_effective": 10, "owners_truncated": false,
 "folders": [{"path": "/volume1/Lab", "folder_owner": "alice", "bytes": 5000, "files": 120,
              "cold_1y_bytes": 3000, "cold_3y_bytes": 1000,
              "owners": [{"owner": "alice", "bytes": 4000, "files": 100, "cold_1y_bytes": 2500, "cold_3y_bytes": 900}],
              "other": {"owner_count": 3, "bytes": 1000, "files": 20, "cold_1y_bytes": 500, "cold_3y_bytes": 100}}],
 "device": {"bytes": 5000, "files": 120, "cold_1y_bytes": 3000, "cold_3y_bytes": 1000, "owners": [...], "other": {...}},
 "volume_owners": null, "_timed_out": [], "_errors": [], "_breakdown_unavailable": []}
```

- **Windows:** bytes, file counts and cold data (by `LastWriteTimeUtc`, from the same PowerShell
  pass as the folder sizes); no owners: `owner_source` is `none` and `folder_owner`, `owners` and
  `other` are `null`.
- `owners: null` = not collected; `owners: []` = collected, no files. Cold fields are `null` when
  not measured, never 0. An owner without an account is `uid:<n>`; name lookups get
  `name_lookup_budget_seconds` (30) in total. Sizes are apparent sizes with hard links counted once,
  as in `folder_usage`.
- **Time guard:** a folder gets the (slower) breakdown walk only if the previous disk run measured it
  in at most `max_timeout_fraction` (0.6) of `timeout_seconds`. So the **first disk run after the
  upgrade** sends bytes and file counts only, with every folder in `_breakdown_unavailable`; the
  second run has the full breakdown. A breakdown walk that still runs out of time is redone without
  it in the same run, so `folder_usage` never loses a folder.
- **Size limit:** at most `max_rows` (5000) owner rows over all folders; over that, fewer owners per
  folder, then none (`owners_truncated: true`). A message also stays within 10,000 folders and 1 MiB:
  past that (about 6,000 measured folders) the smallest folders are left out of this message only,
  counted in `_folders_omitted`; `folder_usage` and the `device` totals still include them.
- `folder_usage` itself is unchanged.

---

## Accuracy

- **cpu_percent before and after the upgrade.** Rows stored before this release hold 1-second
  samples, newer rows 5-minute averages (`interval_s` set). A chart spanning the upgrade mixes both;
  averages are smoother and lower at peaks.
- **Per-user CPU coverage.** Per-user CPU-seconds come from comparing each process's CPU time with
  the previous run's. A process that started and ended between two runs is never seen, so
  `proc_cpu_coverage_percent` (attributed ÷ available CPU) drops when many short-lived processes run.
  On Linux and DSM the CPU time of exited children is recovered from their parents
  (`user_cpu_count_children`), capped so it can only under-count; coverage can then read 100 %.
  Windows has no child-time recovery and processes psutil may not read (AccessDenied) are skipped, so
  coverage is usually lower there.
- **GPU busy-seconds are a sampling estimate.** A GPU's busy % is split between its users by NVML's
  per-process samples. Expect roughly ±10–20 % per user per day for bursty work and within a few
  percent for steady training (design estimates, not measured on the lab's GPUs). Good for "who uses
  the GPUs", not for billing. `gpus_held` / `gpu_held_seconds` (a compute process on the GPU) are
  exact counts of what was seen at each run.
- **Sessions are a 5-minute sample, not an audit log.** A connection that starts and ends between two
  runs is never seen; NFS over UDP is invisible; `since` is the earliest login among the folded
  sessions. Without root on a NAS, SMB and the other file services are known by client IP only.
- **"Cold" means not modified, not "not read".** atime is never used (it is often disabled or
  relaxed). A file read every day but never changed counts as cold. A future mtime is never cold.
- **Disk temperatures** come from hwmon and are read once an hour; disks without a drivetemp or nvme
  sensor have none.

---

## Storage on the Collector

```
data/<name>/
├── YYYY-MM.jsonl            ← every message, appended, never deleted
├── queue.json               ← messages not yet acknowledged (removed when empty)
├── queue.json.lock          ← lock file (leave it)
├── queue.corrupt-<ts>.json  ← only if a queue.json was ever unreadable
├── network_snapshot.json    ← previous counters, for bandwidth
├── perf_snapshot.json       ← previous CPU, disk and process counters, for the interval averages
├── perf_snapshot.json.lock  ← lock file (leave it)
├── sessions_state.json      ← when the session list was last sent in full, and its hash
├── storage_health_state.json ← last health signature and send time, cached hourly readings
└── storage_breakdown_state.json ← seconds each folder's last scan took (the breakdown time guard)
```

The archive is written before any network call, so it is a complete local record even if the
Manager is never reachable. `tools/replay_archives.py` can send it to the Manager again.

The state files are small and can be deleted: a missing `perf_snapshot.json` means one run without
averages (a new baseline); a missing `sessions_state.json` sends the full list next run; a missing
`storage_health_state.json` sends a health message next run; a missing
`storage_breakdown_state.json` makes the next disk run measure every folder without the breakdown.
`perf_snapshot.json` holds PIDs and counters but no user names. `--dry-run` never writes any of
them. An older collector ignores them.

---

## Storage on the Manager

### Per-system history: `data/<normalized name>/data.db`

The directory name is the device name in lowercase with every character other than `a-z 0-9 _`
replaced by `_` (`win-srv.lab` → `win_srv_lab`).

**`folder_usage` is stored narrow**, one row per folder per scan, so shares with thousands of folders
fit (SQLite allows at most 2000 columns per table):

```
folder_usage_msgs   id | msg_id | timestamp                   | device_type | meta_json
                    1  | 5b0d…  | 2026-08-04T02:00:00.000000Z | synology    | {"_unit": "bytes", "_timed_out": [...], ...}

folder_usage_items  msg_row_id | key                  | value
                    1          | /volume1/JeffMoffitt | 4832847265792
                    1          | /volume1             | 16876669222912
                    1          | total_usage          | 24876669222912
```

The read API puts each scan back together as the same flat dict the collector sent. A database
written by the previous Manager had one wide `folder_usage` table with one column per folder; on
first open it is copied into the narrow tables and kept, renamed, as `folder_usage_legacy`.

**`storage_breakdown` is stored narrow too:** `storage_breakdown_msgs` (one row per run, top-level
keys in `meta_json`) and `storage_breakdown_rows` (one row per folder or device total, owner and
"other" group: `scope`, `path`, `kind`, `owner`, `bytes`, `files`, `cold_1y_bytes`,
`cold_3y_bytes`, `owner_count`, `folder_owner`, plus `ord` and `extra_json` so a run can be rebuilt
exactly as sent). `/api/data/<sys>/storage_breakdown` returns each run as sent, nested. A wide
`storage_breakdown` table, written when a new collector reported to an older Manager, is copied into
the narrow tables on first open (after a backup) and kept as `storage_breakdown_legacy`.

**Every other data type** (`system_metrics`, `user_metrics`, `sessions`, `storage_health`,
`not_specified` for messages without `data_type`, and any custom type) gets one table with one
column per payload key:

```
system_metrics  id | msg_id | timestamp                   | device_type | cpu_percent | ram_percent | ...
                1  | 9a8b…  | 2026-08-04T07:00:00.000000Z | synology    | 18.4        | 61.2        | ...
```

**Schema evolution:** when a message has a key the table hasn't seen (a new metric, a new GPU), the
column is added. Earlier rows read back `NULL` for it: "not measured then", which is different from
a measured zero. (The previous Manager wrote `NaN`, which SQLite also stores as `NULL`, or `''` for
text columns.) Lists and objects are stored as JSON text.

Every history table has a `msg_id` column with a UNIQUE index. Rows stored before the upgrade have
`msg_id = NULL`; a re-sent old message is recognized by its timestamp (and user name for
`user_metrics`).

Timestamps are stored as fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Rows written by the
previous Manager keep the text the collector sent, except migrated folder scans.

**Rollups:** `_rollup_series`, `_rollup_hour`, `_rollup_day`, `_rollup_state` and `_rollup_pending`
hold hourly and daily aggregates (count, sum, min, max per UTC hour or day) of every numeric field of
`system_metrics`, `user_metrics` (per user), `sessions` and `storage_health` (daily only). They are
kept forever, never listed as data types, and filled in the background from the existing history
on the first start. Long history charts are answered from them. Details:
[manager/README.md](manager/README.md#rollups).

The Manager reserves the table prefixes `_` and `storage_breakdown_`, so no `data_type` can produce
such a table, and the review-fix Manager (`e5cd699`, `160da09`, `fb6498c`) ignores them after a downgrade.

### Central summary: `metrics.db`

Used by the Dashboard for current state:

| Table | Contents |
|-------|----------|
| `devices` | One row per system: name, system_id, device_type, first_seen, last_seen |
| `device_snapshot` | Latest metrics (`metrics_timestamp`, CPU, RAM, uptime, bandwidth, all fields in `metrics_json`) and latest disk state (`disk_timestamp`, `total_disk_bytes`, `folders_json`) |
| `device_totals` | Lifetime network bytes (`total_bytes_in/out`), the last counters (`last_bytes_in/out`), `network_last_updated`, current `total_disk_bytes` |
| `device_user_snapshot`, `user_metrics_history` | Latest and historical per-user CPU/RAM, and the usage fields (`cpu_seconds`, `cpu_cores`, `interval_s`, `process_count`, `gpu_vram_bytes`, `gpu_util_percent`, `gpu_busy_seconds`, `gpus_held`, `gpu_held_seconds`, `gpu_processes`; NULL in older rows) |
| `device_session_snapshot` | Latest `sessions` message per device: counts, the last full list, when it was sent and last confirmed |
| `device_storage_health` | Latest `storage_health` per device (without the flattened keys) |
| `device_storage_breakdown` | Latest `storage_breakdown` summary per device (top 25 owners, folders with their top owner, at most 50 KB) |
| `dead_letter` | Messages the Manager rejected, with the reason and the raw message |

The snapshot, `last_seen` and the disk total follow the **newest** message: an older message stored
later (a gap-fill replay, a retried `error`) adds history but doesn't roll the card back. Metrics
and disk columns update independently.

**Lifetime network totals.** For each `system_metrics` message:

- Skip it if either counter is `null` or missing, or if it is not newer than
  `network_last_updated` (replay or out of order).
- If a counter went up, add `current − last`.
- If a counter went down:
  - it is a **reboot** (or counter reset) when `uptime_seconds` is shorter than the time since the
    previous counter reading plus 300 s, or when `uptime_seconds` is `null` or missing: add `current`
    (the traffic since boot);
  - otherwise the machine was up all along (an interface went away, loopback stopped being
    counted): add nothing, take the new value as the baseline, and log a warning.
- The first message of a device seeds the totals with its current counters.

At the upgrade, `network_last_updated` is seeded from `device_snapshot.metrics_timestamp` (when the
stored counters were read), else `last_updated`, so totals continue exactly from the stored values.
The previous Manager used `max(0, current − last)`, which dropped the traffic since each reboot.

---

## Configuration

Collector config fields that affect collection (all fields:
[collector/README.md](collector/README.md#configuration)):

```json
{
  "volumes":               ["/volume1", "/volume2"],
  "scan_paths":            ["/volume1/*", "/volume2/Data"],
  "timeout_seconds":       3600,
  "total_timeout_seconds": 10800,
  "exclude_users":         []
}
```

- `volumes`: drives or volumes whose capacity (used, free, total) is reported.
- `scan_paths`: what the folder breakdown measures. `"/volume1/*"` measures each subfolder of
  `/volume1` separately; `"/volume2/Data"` measures that folder as one total. Without `scan_paths`,
  each volume is scanned one level deep.
- `timeout_seconds`: maximum time for one folder (default 3600 s).
- `total_timeout_seconds`: maximum time for the whole folder scan. Default 3 × `timeout_seconds` on
  Windows; on Synology/Linux max(3, number of `scan_paths` entries) × `timeout_seconds`.
- `exclude_users`: extra user-name patterns to leave out of `user_metrics`, per-user GPU data,
  the sessions list and the `*_users` counts (their client IPs still count in `clients_total` /
  `<svc>_clients`; `sessions.exclude_clients` leaves an IP out).

The usage-metrics settings (all optional; a missing key means the default, an invalid value falls
back to the default with a warning) are listed in
[collector/README.md](collector/README.md#usage-metrics-settings): `perf_counters`, `user_metrics`,
`disk_io`, `gpu_per_user`, and the `sessions`, `storage_health` and `storage_breakdown` blocks.

---

## Dependencies

**Collector:** `requests` (required); `psutil` (optional: without it the collector reads `/proc`
on Linux/Synology); `nvidia-ml-py` (optional, for NVIDIA GPUs). Python 3.8 or later.

**Manager:** Flask, waitress (production server; the Flask development server is used, with a
warning, if it is missing), Flask-CORS (only if `cors_origins` is set), and the standard library's
`sqlite3`.

---

## See Also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design and data flow
- [collector/README.md](collector/README.md) — Collector configuration and modes
- [manager/README.md](manager/README.md) — Manager API and storage
- [INSTALLATION.md](INSTALLATION.md) — Setup instructions
- [docs/UPGRADE.md](docs/UPGRADE.md) — What happens to existing data at the upgrade
