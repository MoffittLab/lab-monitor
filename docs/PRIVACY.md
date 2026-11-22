# Privacy: what Lab Monitor records about people

The usage-metrics release records who uses the lab's machines: per-user CPU, memory and GPU use, who
is connected to each server, and who owns the data in the scanned folders. This page says what is
recorded, where it is kept, who can see it, how long it is kept, and how to turn each part off.
The wire-level rule is item R9 of [PROTOCOL.md](PROTOCOL.md#10-compatibility-rules-for-every-new-field-and-data_type).

## Who can see it

**Everyone who can open the Dashboard sees all per-user data** (owner decision, 2026-11-21): user
names, client IP addresses and host names, per-user CPU, memory and GPU use, and storage per owner.
There is no per-user or per-group view restriction.

- The access control is the Dashboard's optional sign-in (one shared password, `auth` in
  `dashboard/config.json`; see [dashboard/README.md](../dashboard/README.md#security)). Without it,
  anyone who can reach port 5001 sees everything. With it, everyone who has the Dashboard password,
  and every browser that has signed in with it (for 180 days after its last visit), sees everything.
- The Dashboard serves plain HTTP: the password crosses the network once per sign-in, the sign-in
  cookie with every request. On an untrusted network put it behind a TLS reverse proxy
  ([CONFIG.md](../CONFIG.md#other-security-settings)).
- The Manager API returns the same data to any **read** or **admin** token
  ([CONFIG.md](../CONFIG.md#manager-tokens)). Collector tokens can't read.
- Anyone with file access to the Manager's `data_dir`, to a collector's `data_dir`, or to their
  backups can read the databases and archives directly.

## What is recorded

| Data | Comes from | Message | Shown on the Dashboard |
|------|------------|---------|------------------------|
| User names with CPU-seconds, cores, RAM, process count | Process owners (`/proc`, psutil) | `user_metrics` | User Activity (server cards), "Last 24 h" line |
| User names with GPU memory, busy %, GPUs held | NVML process lists and their owners | `user_metrics`, `system_metrics` `gpus[].users` | GPU column and GPU rows |
| Who is connected: user name, client IP, client host name, service, login time, session state | TCP table, utmp / logind, `smbstatus -b` (root only), Windows WTS and NetSessionEnum | `sessions` | Connected section |
| Storage per owner: account name, bytes, files, cold data per scanned folder; the folder's owner | File owners (`st_uid`) from the existing daily scan (not on Windows) | `storage_breakdown` | Storage owners, cold-data bars |
| Folder paths | Configured `scan_paths` and their subfolders (as `folder_usage` already reported) | `storage_breakdown` | Storage section |
| Disk model and serial, GPU UUIDs | sysfs, PowerShell, NVML | `storage_health`, `system_metrics` | Not on the card; in the API |

Names are as the operating system reports them: `DOMAIN\user` on Windows, the account name on Linux
and DSM, `uid:<n>` for a uid with no account. Client host names are only those the protocol itself
supplies (RDP ClientName, a utmp or logind host, an SMB machine name). A utmp or logind host is sent
only when it is an address or looks like a host name; tmux, GNU screen and mosh write their PID
there, so their panes and windows are left out and a mosh login is sent with its client IP only.

**Never recorded:** file or directory names below a scanned folder; SMB share names or open-file
names; process names, command lines or PIDs (the collector keeps PIDs locally in
`perf_snapshot.json`, without user names, and never sends them); anything from a DNS lookup.

## Where it is kept

| Place | What | Removed by |
|-------|------|------------|
| Collector `data_dir/<name>/YYYY-MM.jsonl` | Every message ever sent, user names and client IPs included | Nobody: the collector never deletes its archive. Delete or move files by hand (`tools/archive_all_collectors.py`, `tools/reset_system.py`) |
| Collector `queue.json` | Messages not yet acknowledged | Removed once the Manager acknowledges them |
| Collector state files | `perf_snapshot.json` (PIDs, counters, no names), `sessions_state.json` (a hash and a time), `storage_health_state.json` (cached disk readings), `storage_breakdown_state.json` (seconds per folder) | Overwritten every run |
| Manager `data/<system>/data.db` | Full history: `user_metrics`, `sessions` (lists as JSON text), `storage_breakdown_rows` (owner names), `storage_health` (serials); rollups with one series per user name | `history_retention_days` (raw rows); `DELETE /api/system/<name>` (the whole file) |
| Manager `metrics.db` | Latest per-user snapshot and per-user history, latest session list, latest storage-owner summary | `history_retention_days` (per-user history); `DELETE /api/system/<name>` (that device's rows) |
| Manager `metrics.db` `dead_letter` | Raw messages the Manager rejected | Nobody: neither retention nor `DELETE /api/system/<name>` removes them |
| Manager `data/<system>/archive/*.jsonl` | Rows pruned by retention | Nobody: delete by hand |
| `*.pre-upgrade-*.bak`, `*.post-upgrade-*` | Full copies of databases from an upgrade or rollback | Delete by hand once you won't roll back |
| NAS snapshots (`snapshot.dest_dir/snapshot-<stamp>/`) | Nightly copies of `metrics.db`, every `data.db`, the retention archives and `*.post-upgrade-*` files: the same per-user data as above | Pruning after `keep_daily` (14) days, except the newest of each of the last `keep_monthly` (12) months, so up to about a year; the newest good copy of a database that later failed to copy is kept longer. `DELETE /api/system/<name>` and `tools/reset_system.py` do **not** remove a system from snapshots already taken: delete its folder in each snapshot by hand (the `manifest.json` then lists a missing file, so `snapshot.py verify` reports it) |

## How long it is kept

- **By default, forever.** `history_retention_days` is `null`, so nothing is pruned.
- **With `history_retention_days` set**, raw history rows older than that are moved once a day to
  `data/<system>/archive/<YYYY-MM>-<data_type>.jsonl` and deleted from the database. That covers the
  per-user rows, the session lists and the storage-owner rows. The archive files stay until you
  delete them. A rolled-up table is pruned only after its rollups are complete.
- **Per-user rollups are kept forever** in this release: hourly and daily aggregates of each user's
  CPU and GPU fields, keyed by user name, are never pruned. There is no tool to delete one user's
  rollups; `DELETE /api/system/<name>` deletes the whole system's `data.db`, rollups included.
- **Session lists** are stored whenever the list changes and at least once an hour (the heartbeat),
  as decided by the owner; the counts are stored every 5 minutes.
- **Latest snapshots** in `metrics.db` (the last row of each user, the last session list, the last
  storage-owner summary) stay until a newer message replaces them or the device is deleted. A user
  who logged off keeps a last snapshot row; the Dashboard hides rows more than 120 s older than the
  system's latest metrics.
- **The collectors' archives** are never pruned by Lab Monitor.
- **NAS snapshots** (when `snapshot.enabled`) keep a copy of everything in the Manager's databases for up
  to `keep_monthly` months (12 by default). Data deleted or pruned in the live databases stays in the
  snapshots taken before, until they are pruned.

## How to record less

Set these in the collector config (`collector/local/config.json`, `/etc/lab-monitor/collector.json`
on Linux; all keys are described in
[collector/README.md](../collector/README.md#usage-metrics-settings)). A change takes effect on the
next run. **Turning a feature off stops new data; what is already stored stays** (nothing is
deleted).

| To stop recording | Setting | Effect |
|-------------------|---------|--------|
| Client IP addresses and host names | `"sessions": {"detail": "users"}` | The list keeps user names and services only; no TCP-only rows |
| The connected-users list | `"sessions": {"detail": "counts"}` | Only per-service counts, no names, no IPs |
| Everything about connections | `"sessions": {"enabled": false}` | No `sessions` message |
| Particular clients | `"sessions": {"exclude_clients": ["10.0.5.0/24"]}` | Those IPs or networks are left out of counts and list |
| Particular users | `"exclude_users": ["alice", "LAB\\svc-*"]` | Left out of per-user CPU/RAM, per-user GPU, the sessions list and the `*_users` counts (not of storage owners; their client IPs still count in `clients_total` / `<svc>_clients`, which come from the TCP table: add those IPs to `sessions.exclude_clients`) |
| Daemon accounts (Linux) | `"exclude_system_accounts": true` (default on Linux) | System UIDs left out of per-user metrics |
| Per-user CPU and RAM rows | `"user_metrics": "off"` | No `user_metrics` messages |
| Per-user GPU attribution | `"gpu_per_user": "off"` | GPUs reported per device only; no `gpus[].users`, no GPU fields per user |
| Storage owner names | `"storage_breakdown": {"owners": "none"}` | Cold data and file counts stay; no owners (`owner_source: "none"`) |
| The whole storage breakdown | `"storage_breakdown": {"enabled": false}` | No `storage_breakdown` message |
| Disk models and serials | `"storage_health": {"enabled": false}` | No `storage_health` message |

Notes:

- `exclude_users` patterns are case-insensitive globs and add to the built-in Windows defaults
  (`NT AUTHORITY\*`, `UMFD-*`, ...). Sessions also leave out Windows machine accounts (`*$`).
- Storage owners are file owners, not people logged in, so `exclude_users` does not apply to them.
  Use `storage_breakdown.owners: "none"` instead.
- On Windows, storage owners are never collected.
- `user_metrics: "off"` still sends per-user GPU rows in `system_metrics` `gpus[].users` unless
  `gpu_per_user` is `"off"` too.

To remove already stored data, the existing tools apply: `tools/reset_system.py` (moves a
collector's archives aside and deletes the system on the Manager), `DELETE /api/system/<name>`
(admin token), and deleting archive, backup and legacy files by hand. There is no per-user delete.

## Limits of the data

- Sessions are a 5-minute sample, not an audit log: a connection that starts and ends between two
  runs is never seen.
- Per-user GPU busy time is a sampling estimate; it is not suitable for billing.
- "Cold" data is data not modified for a year or three, by mtime; it says nothing about who reads it.

See [METRICS.md](../METRICS.md#accuracy).
