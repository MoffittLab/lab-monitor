# Protocol Reference

The wire protocol between collectors and the Manager, and the contract of the Manager's read API.
This is the canonical reference: where another document and this one disagree, this one is meant to
be right, and where this one and the code disagree, the code is right and this file must be fixed.

- Items 1–9 are the contract of the review-fix release (`fb6498c`; `160da09` adds nightly snapshots, and `e5cd699`, the version that is deployed, the review's snapshot fixes: neither changes anything here).
- Items 10–18 are added by the usage-metrics release (`feature/usage-metrics`). They are additive:
  items 1–9 are unchanged.

Field-by-field descriptions for operators are in [METRICS.md](../METRICS.md); endpoint details and
examples in [manager/README.md](../manager/README.md); what is recorded about people in
[PRIVACY.md](PRIVACY.md).

**Baselines** used in the compatibility rules below:

| Name | What it is |
|------|------------|
| OLD Manager | Commit `f086e7e`, the version that ran before the review-fix release. No `msg_id` dedup; it answers `{"stored": n}`, and a collector keeps the whole batch queued unless `stored` equals the batch size. |
| CURRENT Manager | The review-fix release, `e5cd699` (`160da09` and `fb6498c` behave the same on the wire and in storage). Items 1–9 apply. None of items 10–18. |
| NEW | CURRENT plus the usage-metrics release. |

---

## Part 1: Contract items 1–9

### 1. Message IDs

Every message the collector creates gets `header.msg_id = uuid4().hex`, assigned at creation and
written to both the archive and the queue. On reading `queue.json`, entries lacking `msg_id` get one
assigned and the queue is rewritten (so retries keep the same id). The Manager dedups on `msg_id`;
for messages without `msg_id` (old collectors) it derives one deterministically:
`"legacy-" + sha1("name|data_type|timestamp|username")`.

### 2. Batching

The collector POSTs at most config `max_batch_messages` (default 100) messages per request to
`POST /api/data/queue` with body `{queue_id, name, id, messages}`.
`queue_id = f"{name}-{uuid4().hex[:12]}"`. Collector config `request_timeout_seconds` stays at a
default of 30.

### 3. ACK

The Manager returns HTTP 200:

```json
{"status": "ok", "queue_id": "<echo>", "received": N, "stored": H,
 "counts": {"stored": a, "duplicate": b, "rejected": c, "error": d},
 "results": [{"msg_id": "...", "status": "stored|duplicate|rejected|error", "error": "<short str, optional>"}]}
```

- `stored` = newly persisted; `duplicate` = `msg_id` already present (no side effects re-applied);
  `rejected` = permanently unstorable (bad shape, bad value, name mismatch), saved to a `dead_letter`
  table in the central metrics DB with the raw message and the reason; `error` = transient failure
  (DB locked, I/O), not persisted.
- `H` (top-level `stored`) = a + b + c, the messages durably handled. This keeps OLD collectors
  (which delete their queue only when `stored` equals the number sent) draining.
- A new collector, after a 200 ACK with a matching `queue_id`, removes from `queue.json` exactly the
  `msg_id`s whose status is `stored`, `duplicate` or `rejected`, and keeps the `error` ones. If
  `results` is absent (old Manager), it removes the whole batch only if `stored == len(batch)`.
  Removal re-reads the queue under the lock, so entries appended meanwhile are kept.
- Manager side effects (central snapshot, network running totals, user metrics, device registry)
  are applied only for newly stored messages (not duplicates, not rejected).
- HTTP 401 = bad or missing token; 403 = token not allowed for this device or endpoint; 413 = body
  too large (Manager config `max_request_bytes`, default 32 MiB); 400 = malformed body. Error bodies
  are `{"status": "error", "error": "<generic message>"}`, never `str(exception)`.

### 4. Unmeasured values are null

If the collector can't measure a metric (CPU, RAM, network counters, GPU, a folder), it sends JSON
`null` or omits the field, never `0`. The Manager stores null as `NULL`, does not touch running
totals when either network counter is null or missing, and the Dashboard renders null as `—`.

### 5. folder_usage wire format stays flat

Keys are folder paths → int bytes, volume path → used bytes, `"<vol>_total_bytes"` /
`"<vol>_free_bytes"`, `"total_usage"`, `"_unit"`. Keys starting with `_` are metadata, never
folders. Optional metadata keys:

| Key | Meaning |
|-----|---------|
| `_timed_out` | Folder paths that hit the scan timeout |
| `_errors` | Folder paths that failed to scan |
| `_scan_seconds` | float |
| `_volumes` | The keys that are volumes (added with the Linux collector; sent by the current collector on every platform). On Linux a volume can be `/mnt/data` and a folder `/data`, so path depth can't tell them apart. |

Timed-out and failed folders are omitted from the folder keys (not reported as 0). The Manager
stores `folder_usage` narrow, but its read API keeps returning the same flat dict per row, so the
Dashboard's parsing keeps working. The Dashboard skips `_`-prefixed keys when listing folders and
surfaces `_timed_out` / `_errors` as a warning on the card.

### 6. Network running totals (Manager)

Skip if either counter is null; skip if the message timestamp is not newer than
`device_totals.last_updated` (replay, out of order); if current < last (reboot or reset) the delta
is `current` (traffic since boot), else `current − last`. (The Manager refines the reboot test with
`uptime_seconds`: see [METRICS.md](../METRICS.md#central-summary-metricsdb).)

### 7. Auth scopes

All token comparisons use `hmac.compare_digest`.

- `collector_tokens`: `{"<device_name>": "<token>"}`. May only `POST /api/data/queue`, and only when
  the payload `name` (and each message header name) matches that device, compared
  case-insensitively after the Manager's device-name normalization. A key of `"*"` means any device
  (explicit opt-in, logged as a warning at startup).
- `read_tokens`: GET endpoints only (the Dashboard's token).
- `admin_tokens`: everything, including `DELETE /api/system/<name>`.
- Legacy `auth_tokens`: keep working with full (admin + any-device write) rights; a deprecation
  warning is logged at startup.
- The Manager refuses to start if any configured token is a known placeholder (contains
  `CHANGE-ME`, `YOUR-`, `GENERATE-RANDOM`, ...) or is shorter than 16 characters.
- The Dashboard config key stays `manager_token` (a read token). Tools that call DELETE use an admin
  token.
- Device names: ingest validates the header and payload name against `^[A-Za-z0-9_.-]{1,64}$`
  (reject otherwise), the same pattern the read endpoints use. Normalization for storage (directory
  name and central DB name) is one shared function, so `Lab-A` and `lab-a` can't diverge between
  the history store and the metrics DB. Read endpoints never create databases or directories for
  unknown systems (404).

### 8. Servers and dependencies

The Manager and the Dashboard run under waitress when it is installed (fallback: the Flask
development server with a loud warning). The root `requirements.txt` (Manager, Dashboard, tools)
pins every package, `requests>=2.32.3`. `collector/requirements.txt` is what the collector installers
install (`requests` and `psutil`; `nvidia-ml-py` is optional, `requirements-gpu.txt`).

### 9. History retention

No automatic deletion by default. Manager config `history_retention_days` (default `null` = keep
forever). The old `retention_days` keeps its old meaning (the legacy `usage.jsonl` only). With
rollups (item 17) a rolled-up table is pruned only once its rollups are complete.

---

## Part 2: Items 10–18 (usage-metrics release)

### 10. Compatibility rules for every new field and data_type

**R1 Key names.**
- New top-level payload keys match `^_?[a-z][a-z0-9_]{0,62}$` (lowercase only).
- Keys starting with `_` are metadata: never charted or rolled up.
- A key is never `id`, `timestamp`, `device_type`, `msg_id` or `data_type`, and never starts with
  `payload_`.
- Within one data_type no two keys differ only in case. (The OLD Manager's `store()` inserts
  `timestamp`, `device_type` + payload keys and compares column names case-sensitively; a colliding
  key makes `store()` fail and the collector re-send the batch every run.)

**R2 Detail versus chartable values.** Structured detail goes in nested lists or objects, which every
Manager's generic wide path stores as JSON text. Only top-level finite numbers (and booleans) can be
charted (`/api/history`) or rolled up.

**R3 Units.**
- New `system_metrics` numeric fields carry `<field>_unit` siblings, as before. `cpu_count` and
  `disk_busy_max_name` have none (like `gpu_count`).
- `sessions` uses `_unit: "count"` as the message default. `storage_health` has no unit keys: the
  Manager derives the unit from the field-name suffix (item 17).
- `user_metrics` carries no unit siblings; its units come from the Manager's rollup registry.

**R4 Null semantics.** Item 4 applies to every new field: an unmeasured value is null or omitted,
never 0; a measured zero is 0. The tables below say which.

**R5 Storable by every Manager.** Each new message is storable by the OLD Manager's generic
`store()` and by the CURRENT Manager's generic wide path (R1 guarantees it). At most 1000 distinct
top-level keys per data_type over a host's lifetime (SQLite allows 2000 columns; the Manager's
limit is 1900). A serialized message is at most 1 MiB.
`tests/compat/test_new_payloads_old_managers.py` checks this against the real OLD and CURRENT code.

**R6 Unknown types are stored, not interpreted.** A Manager that does not know a data_type stores it
in a wide table and applies no side effects. Collectors never need a particular Manager version.

**R7 Optional endpoints.** A Dashboard treats HTTP 404 from any endpoint added by item 16 as "feature
not available on this Manager". That is not an error and does not make the poll degraded.

**R8 NEW Manager validation.**
- A wrong JSON type in a field the Manager knows explicitly → `rejected` (item 3). The fields:
  - `system_metrics`: `interval_s`, `cpu_count`, `cpu_iowait_percent`, `cpu_steal_percent`,
    `proc_cpu_coverage_percent`, `disk_read_bytes_per_s`, `disk_write_bytes_per_s`,
    `disk_read_iops`, `disk_write_iops`, `disk_await_ms`, `disk_busy_max_percent`,
    `gpu_sample_seconds` (number or null); `disk_busy_max_name` (string or null); `gpus` (list,
    JSON text or null); `gpus[].users` (list of at most 64, or null);
  - `user_metrics`: `cpu_seconds`, `cpu_cores`, `interval_s`, `process_count`, `gpu_vram_bytes`,
    `gpu_util_percent`, `gpu_busy_seconds`, `gpus_held`, `gpu_held_seconds`, `gpu_processes`;
  - `sessions`: `clients_total`, `users_total`, `rdp_disconnected`; `sessions` (list of at most
    5000); `_list_state` (`full`, `unchanged`, `none`); `_list_hash` (string of at most 64); `_sources`
    (object);
  - `storage_health`: `overall_status_code`; `overall_status` (`ok`, `warning`, `critical`,
    `unknown`); `arrays`, `disks`, `volumes` (lists of at most 128, 512, 64); `status_reasons` (list);
  - `storage_breakdown`: `folders` (list of at most 10,000); `device` and each folder's `other`
    (object or null); at most 50,000 rows in total (per folder and for `device`: 1 + owners + 1 for
    `other`).
- A key that is numeric only because its name matches a pattern (`sessions` `<svc>_clients` /
  `<svc>_users`; `storage_health` `disk_*`, `array_*`, `volume_*`) is **dropped** when its value is
  not a number or null (booleans count as non-numbers here). Its name goes in `_dropped_fields`
  (list of str, added by the Manager) and the rest of the message is stored.
- A bad row inside a nested list is dropped and counted in `_dropped_rows` (integer, added by the
  Manager); the rest is stored. Bad means: not an object, a `status` outside its enum, a string over
  the R10 limits, a non-finite number, a negative `storage_breakdown` byte or file count, or (in
  `storage_breakdown`) a folder path or an owner already listed in the same entry.
- `status_reasons` is cut to 10 entries of at most 200 characters.
- `_dropped_fields` and `_dropped_rows` are stored as fields of the history row. A message from an
  older collector has none of these keys and is stored unchanged.

**R9 Privacy.** New data may carry: usernames as the OS reports them (`DOMAIN\user` on Windows,
`uid:<n>` when a uid has no account); client IP addresses; client host names the protocol itself
supplies (RDP ClientName, a utmp or logind host, an SMB machine name); folder paths that
`folder_usage` already reports; disk model and serial; GPU UUIDs. It never carries file or directory
names below a scanned folder, SMB share names or open-file names, process names, command lines or
PIDs, or anything obtained by DNS lookup. See [PRIVACY.md](PRIVACY.md).

**R10 String limits** (cut by the collector, re-checked by the Manager): usernames 256 characters;
paths 4096; disk, array and volume ids 64; client and client host 256 (Manager check); service names
`^[a-z][a-z0-9]{0,15}$`; status reasons 200 characters, at most 10. The Dashboard escapes every
string.

**R11 Source status vocabulary**, shared by `sessions._sources` and `storage_health.sources`:

| Status | Meaning |
|--------|---------|
| `ok` | Read |
| `partial` | Read, but some entries could not be (for example WTS sessions a user-logon install may not query) |
| `not_permitted` | Needs root or admin rights the collector account does not have |
| `unavailable` | The tool, file or hardware is absent |
| `timeout`, `error` | The source, or the whole sessions step, timed out or failed |
| `stale`, `unsafe`, `off` | Reserved. Not sent in this release (`stale` / `unsafe` were for a privileged helper's files, which is not implemented; a feature turned off in config sends no message instead of `off`). |

### 11. `system_metrics` additions

All optional. Every numeric field except `cpu_count` has a `<field>_unit` sibling, sent even when
the value is null.

**Meaning change.** `cpu_percent` keeps its name and becomes the true average since the previous run;
`interval_s` gives that window. When `interval_s` is null (every row stored before this release, a
first run, a run without a valid baseline, `perf_counters: false`), `cpu_percent` is the old 1-second
sample.

| Field | Type | Unit | Null / omit rule | Meaning |
|-------|------|------|------------------|---------|
| `interval_s` | float, 1 dp | `s` | null without a valid baseline | Window covered by every counter-derived field |
| `cpu_count` | int | – | null if unknown | `os.cpu_count()` (logical CPUs) |
| `cpu_iowait_percent` | float, 1 dp | `%` | null on Windows or without a baseline | Δiowait / Δtotal |
| `cpu_steal_percent` | float, 1 dp | `%` | as above | Δsteal / Δtotal |
| `proc_cpu_coverage_percent` | float 0–100 | `%` | null when per-process data is unavailable | CPU attributed to processes ÷ CPU processes could have used |
| `disk_read_bytes_per_s`, `disk_write_bytes_per_s` | int | `B/s` | null without disk counters or baseline | Sum over physical disks (md arrays, partitions and virtual devices excluded) |
| `disk_read_iops`, `disk_write_iops` | float, 1 dp | `ops/s` | as above | Sum over physical disks |
| `disk_await_ms` | float, 1 dp | `ms` | null when no I/O happened | Σ(Δread_ms + Δwrite_ms) / Σ(Δops) |
| `disk_busy_max_percent` | float, 1 dp, 0–100 | `%` | null on Windows | Busiest physical disk |
| `disk_busy_max_name` | str ≤ 32 | – | null with the field above | e.g. `sata3`, `sda` |
| `gpu_sample_seconds` | float | `s` | omitted on hosts without GPU data | dt used for GPU-seconds: `interval_s` when 0 < `interval_s` ≤ 900, else config `metrics_interval_seconds` (default 300) |

**New keys in each `gpus[i]`** (only where NVML works; the existing device keys and the flat
`gpu_N_*` columns are unchanged):

| Key | Type | Meaning |
|-----|------|---------|
| `uuid` | str ≤ 96 or null | NVML UUID |
| `process_source` | `"nvml"` or null | null = no per-process data for this GPU (driver without the `_v3` process call, Windows drivers 500–525, `gpu_per_user: "off"`, or the query failed) |
| `util_split` | `single_user`, `process_samples`, `idle`, `ambiguous` or null | How the GPU busy % was split between users |
| `users` | list ≤ 20, or null | `[{username, vram_bytes: int\|null, util_percent: float\|null, processes: int}]`, largest VRAM first; `processes` counts all of the user's processes on that GPU |
| `users_truncated` | int or null | Users beyond the 20 listed |
| `unattributed_vram_bytes` | int or null | VRAM of processes that are excluded or whose owner is unknown |
| `unattributed_processes` | int or null | Number of those processes |

When `process_source` is null, `users`, `users_truncated`, `unattributed_vram_bytes` and
`unattributed_processes` are all null. With `gpu_per_user: "off"` only `uuid`, `process_source`
(null) and `util_split` (null) are added.

### 12. `user_metrics` additions

Still one message per reported user per run. The existing fields (`username`, `cpu_percent`,
`ram_bytes`, `ram_formatted`) keep their names. Config `user_metrics` decides which users are
reported (`auto` = only active users on Synology, every non-excluded user elsewhere).

**Meaning change.** Per-user `cpu_percent` is `cpu_cores / cpu_count × 100` averaged over
`interval_s`; it used to be a 1-second psutil sample. It is null on a run without a baseline.

A user seen only on a GPU gets a message too, with `cpu_percent`, `ram_bytes` and `ram_formatted`
null.

**CPU fields**, present wherever per-process accounting works (Linux and DSM through `/proc`, with or
without psutil; Windows only with psutil):

| Field | Type | Unit | Null rule | Meaning |
|-------|------|------|-----------|---------|
| `cpu_seconds` | float, 2 dp | s | null without a baseline | CPU-seconds used in `interval_s`, all cores |
| `cpu_cores` | float, 3 dp | cores | as above | `cpu_seconds / interval_s` |
| `interval_s` | float | s | as above | Same window as `system_metrics.interval_s` |
| `process_count` | int | – | null when per-process data is unavailable | The user's processes at measurement time, zombies excluded |

**GPU fields**, present on every row of a host where the per-process GPU query worked on every GPU,
with measured zeros for users who use no GPU. On other hosts they are omitted, never partial.

| Field | Type | Unit | Meaning |
|-------|------|------|---------|
| `gpu_vram_bytes` | int or null | bytes | VRAM held now, summed over GPUs; null if any of the user's processes has unknown VRAM (WDDM) |
| `gpu_util_percent` | float or null | % | Average attributed busy % over the GPUs the user holds; 0 if none; null if any GPU the user holds is `ambiguous` or has no utilization reading |
| `gpu_busy_seconds` | float or null | s | Σ over GPUs of `user_util / 100 × gpu_sample_seconds` (a sampling estimate); null under the same condition |
| `gpus_held` | int | – | GPUs where the user has at least one compute process |
| `gpu_held_seconds` | float | s | `gpus_held × gpu_sample_seconds` |
| `gpu_processes` | int | – | The user's compute processes across all GPUs |

Usernames are cut to 256 characters; an unresolvable uid is `uid:<n>`. Process names, PIDs and
command lines never leave the host; the local `perf_snapshot.json` holds PIDs and counters but no
usernames.

### 13. New data_type `sessions` (who is connected)

One message per metrics run when `sessions.enabled` is true (the default). A service is counted only
while the host has a local LISTEN socket on one of its ports, so a missing `<svc>_*` key means the
service is not running and `0` means it runs with no clients.

**Top-level keys:**

| Key | Type | Rule |
|-----|------|------|
| `_schema` | int | `1` |
| `_unit` | `"count"` | `/api/history/<sys>/sessions/<field>` reports the unit `count` |
| `_detail` | `full`, `users` or `counts` | Config `sessions.detail` |
| `_sources` | object | `{source: status}` (R11). POSIX: `tcp`, `utmp` (or `logind` when no utmp file exists), `smb` (left out when the TCP table shows SMB isn't listening). Windows: `tcp`, `wts`, `netsession`. `snmp` is reserved. |
| `_collect_seconds` | float | Wall time of the sessions step |
| `_list_state` | `full`, `unchanged` or `none` | See below |
| `_list_hash` | 16 hex or null | Hash of the list; null when `_list_state` is `none` |
| `clients_total` | int or null | Distinct remote IPs with an ESTABLISHED connection to a monitored listening port; null if the TCP table can't be read |
| `<svc>_clients` | int | Present only while the service listens and the TCP table can be read; always counted from TCP |
| `users_total` | int or null | Distinct usernames across the user-level sources (DOMAIN\ prefix stripped, case-folded on Windows); null if none of them succeeded |
| `<svc>_users` | int | Users with a session that isn't disconnected, per service a succeeded source covers: utmp/logind → `ssh_users`, `console_users`; smb → `smb_users`; wts → `console_users`, `rdp_users`; netsession → `smb_users` |
| `rdp_disconnected` | int | Windows (wts): disconnected RDP sessions |
| `sessions_truncated` | bool | The list was cut at `sessions.max_entries` (default 200); the counts are complete. Absent with `_detail: "counts"`. |
| `sessions` | list ≤ 5000 | Present only when `_list_state` is `full` |

**List entries** (unmeasured keys omitted):

| Key | Meaning |
|-----|---------|
| `service` | Name from the port map, or `console` |
| `username` | As the source reports it. Omitted for TCP-only rows. |
| `client` | IP address (IPv4-mapped IPv6 as IPv4, zone id stripped). Omitted with `_detail: "users"`, and on a login from a loopback address (the row is kept). |
| `client_host` | A name the protocol supplied (RDP ClientName, a non-IP utmp/logind host, an SMB machine name); never from DNS. A utmp or logind host is sent only when it looks like a host name (letters, digits, `.`, `_`, `-`); other text is dropped. Omitted with `_detail: "users"`. |
| `connections` | TCP connections or protocol sessions folded into the row |
| `since` | ISO-8601 UTC: earliest login or connect time among them. Omitted for TCP-only rows. |
| `state` | `active` or `disconnected`; WTS rows only |
| `source` | `tcp`, `utmp`, `logind`, `smb`, `wts` or `netsession` |

utmp rows that terminal programs write through libutempter are not logins and are left out: tmux
panes (`tmux(<pid>).%<n>`), GNU screen windows (`<host>:S.<n>`) and a mosh-server with no client
attached (`mosh [<pid>]`). A mosh-server with a client (`<ip> via mosh [<pid>]`) is an `ssh` row
from `<ip>`, without the PID.

**How the list is sent.** The collector hashes the sorted `(service, username, client, client_host,
state)` tuples (`since` and `connections` don't count). It sends the full list (`_list_state:
"full"`) when the hash changed, when no state file exists, every `sessions.list_heartbeat_seconds`
(default 3600), or every run with `sessions.history: "every_run"`. Otherwise it sends `"unchanged"`,
the same `_list_hash`, and no `sessions` key; the counts are sent every run. With `_detail: "users"`
client and host fields and TCP-only rows are left out; with `_detail: "counts"` there is no list
(`_list_state: "none"`).

A sessions step that fails or times out still sends a message: null counts, every source of the
platform set to `error` or `timeout`, `_list_state: "none"`. The next good run then sends the full
list.

**Exclusions.** `exclude_users` plus the built-in defaults and the pattern `*$` (Windows machine
accounts) apply to the list and the `*_users` counts. `sessions.exclude_clients` (IPs or CIDRs)
applies to counts and list. Loopback and self-connections never count.

### 14. New data_type `storage_health`

Built inside the metrics run. Sent when the health signature changes (`trigger: "change"`), every
`storage_health.interval_seconds` (default 3600, `trigger: "interval"`), or by `--mode health`
(`trigger: "manual"`).

| Key | Type | Rule |
|-----|------|------|
| `_schema` | int | `1` |
| `trigger` | `interval`, `change` or `manual` | |
| `overall_status` | `ok`, `warning`, `critical` or `unknown` | Worst status of any component; `unknown` only when nothing could be assessed |
| `overall_status_code` | 0, 1, 2 or null | ok 0, warning 1, critical 2, unknown null (the chartable copy) |
| `status_reasons` | list of str | At most 10 of at most 200 characters, worst first, e.g. `"md2 rebuilding 37.5% (ETA 5h 12m)"` |
| `sources` | object | `{source: status}` (R11). Linux and DSM: `md`, `sysfs`, `mounts`, `hwmon`, and `zpool` where the `zpool` tool exists. Windows: `windows_storage`, `events`. `smart` and `snmp` are reserved and not sent in this release. |
| `arrays` | list ≤ 128 | `{id, kind: md\|storage_space\|zpool, name, level, role: data\|system\|swap, state, status, members_total, members_active, degraded, sync_action, sync_percent, sync_speed_kbps, sync_eta_seconds, mismatch_cnt, size_bytes, faulty_members: [ids], members: [ids]}` |
| `disks` | list ≤ 512 | `{id, model, serial, size_bytes, media: hdd\|ssd\|nvme\|null, bus, status, health_source: sysfs\|windows, smart_passed, temperature_c, power_on_hours, reallocated_sectors, pending_sectors, uncorrectable_sectors, read_errors_uncorrected, write_errors_uncorrected, wear_used_percent, remaining_life_percent, bad_sectors, io_errors, device_state, in_standby, arrays: [ids]}` |
| `volumes` | list ≤ 64 | `{id, fs, read_only, health, status}` |
| `events` | object or null | Windows only: System-log event counts since the previous full run, e.g. `{"disk/7": 2}` |
| `_flat_truncated` | bool | More components than the flattening caps allow |
| flattened numerics | number | See below |

Every `status` inside the lists is `ok`, `warning`, `critical` or `unknown`. Units: temperatures
°C, sizes bytes, power-on time hours, percentages 0–100. `wear_used_percent`: 0 = new, 100 = rated
life reached. `power_on_hours` on Windows is a UInt16, so 65535 means "at least 65535". SMART
attributes stay null on Linux and DSM in this release (no root).

**Ids:** disk on Linux and DSM = sysfs name (`sata1`, `sda`, `nvme0n1`); disk on Windows =
`pd<DeviceId>`; array = `md2`, a zpool name, or the Storage Spaces virtual disk FriendlyName; volume
= mount root (`/volume1`) or drive (`E:`). Ids are cut to 64 characters.

**Flattened numerics** (for history and rollups): for each component also top-level keys
`<kind>_<sid>_<metric>`, where `sid` is the id lowercased, every character outside `[a-z0-9]`
replaced by `_`, cut to 32 characters (`/volume1` → `volume1`, `E:` → `e`, `/` → `root`). A key is
emitted only when its value is not null; `status_code` is ok 0, warning 1, critical 2 (unknown
omitted). Disks with `bus: "usb"` are listed in `disks` but never flattened. If two components give
the same key, only the first is flattened and `_flat_truncated` is true.

| Kind | Metrics |
|------|---------|
| `disk` (first 48 disks) | `temperature_c`, `status_code`, `power_on_hours`, `reallocated_sectors`, `pending_sectors`, `uncorrectable_sectors`, `wear_used_percent`, `io_errors` |
| `array` (first 32) | `status_code`, `degraded`, `sync_percent` |
| `volume` (first 32) | `status_code` |

### 15. New data_type `storage_breakdown` (storage attribution)

The daily disk run sends it next to `folder_usage`, as its own message with its own `msg_id`, when
`storage_breakdown.enabled` is true (the default) and at least one folder was measured. It covers
the folders of `scan_paths`. `folder_usage` itself is unchanged (item 5).

Sent on Linux, DSM **and Windows**. On Windows (owner decision) it carries bytes, file counts and
cold data; `owner_source` is `none` and `folder_owner`, `owners`, `other` and the device entry's
`owners` / `other` are null. (This replaces the earlier plan of POSIX collectors only.)

| Key | Type | Rule |
|-----|------|------|
| `_schema` | int | `1` |
| `_unit` | `"bytes"` | |
| `reference_time` | ISO-8601 UTC | The instant the cold cut-offs are computed from |
| `cold_thresholds_days` | `[int, int]` | Config `storage_breakdown.cold_days`, default `[365, 1095]` |
| `owner_source` | `posix_uid` or `none` | `none` on Windows and with `storage_breakdown.owners: "none"`. `ntfs_owner_sid` is reserved. |
| `age_source` | `mtime` | (`LastWriteTimeUtc` on Windows) |
| `top_owners`, `top_owners_effective` | int | Configured N, and the N actually used (0 when per-folder owners were dropped) |
| `owners_truncated` | bool | The row budget (or the size limit) lowered N or dropped per-folder owners |
| `folders` | list | One entry per measured folder, at most 10,000 and within the 1 MiB message limit (see Limits) |
| `device` | object or null | Rollup of the top-level folders only (a folder inside another measured folder is not counted twice). A total that any of those folders lacks is null, not partial. |
| `volume_owners` | null | Reserved |
| `_timed_out`, `_errors`, `_breakdown_unavailable` | list | As in `folder_usage`, plus the folders measured without the breakdown (first run, or too close to their timeout) |
| `_folders_omitted` | int | Only when the size limit left folders out: how many (see Limits) |

- **Folder entry:** `path`, `folder_owner` (str or null), `bytes`, `files`, `cold_1y_bytes`,
  `cold_3y_bytes`, `owners`, `other`.
- **`owners` row:** `owner`, `bytes`, `files`, `cold_1y_bytes`, `cold_3y_bytes` (top N by bytes).
- **`other`:** `owner_count`, `bytes`, `files`, `cold_1y_bytes`, `cold_3y_bytes` (everyone else).

| Item | Rule |
|------|------|
| Ordering | `cold_3y_bytes ≤ cold_1y_bytes ≤ bytes` |
| `owners: null` | Owners were not collected |
| `owners: []` | Owners were collected and there were none |
| Cold fields | null when not measured, never 0 |
| `owner` | The account name, or `uid:<n>` (name lookups are capped at `storage_breakdown.name_lookup_budget_seconds`, default 30, in total) |
| "Cold" | Not modified within the window, by mtime; atime is never used; a future mtime is never cold |
| Sizes | Apparent sizes (`st_size`); hard links counted once, as in `folder_usage` |

**Time guard.** A folder gets the breakdown walk only when the previous disk run measured it in at
most `storage_breakdown.max_timeout_fraction` (0.6) of its `timeout_seconds`. The first disk run after
the upgrade therefore sends bytes and file counts only, with cold and owner fields null and every
folder in `_breakdown_unavailable`. A breakdown walk that still runs out of time is redone without
the breakdown in the same run, so `folder_usage` never loses a folder to it.

**Limits.** Each folder costs `1 + min(N, owners) + 1` rows, at most `storage_breakdown.max_rows`
(default 5000) in total. Over that, N drops to `max(1, max_rows // folders − 2)`; if still over,
per-folder owners are left out (folder totals and `device` remain). Either way `owners_truncated` is
true. The device entry (at most `device_top_owners` + 2 rows) is outside this budget. The Manager
rejects more than 10,000 folders or 50,000 rows, so the collector also keeps a message within
10,000 folders and 1 MiB of JSON (R5): over either, per-folder owners are dropped first, then the
smallest folders by bytes. Those are counted in `_folders_omitted` (sent only then; `folder_usage`
still reports them) and left out of `_breakdown_unavailable`; `device` still covers every measured
folder. A disk run reaches this only past about 6,000 measured folders.

### 16. New Manager read endpoints

Rules for every endpoint here: read scope (item 7); `<name>` validated against the device-name
pattern (bad name 400; unknown system, or nothing reported yet, 404; nothing is created); error
bodies as in item 3; an older Manager answers 404 (hence R7). The static `/all` routes shadow a
device named `all`, and `/api/user_metrics/usage` shadows a device named `usage` in
`/api/user_metrics/<device_name>`.

| Endpoint | Response |
|----------|----------|
| `GET /api/sessions/all` | `{name: {timestamp, list_timestamp, list_confirmed_at, summary: {every non-list key of the latest message except data_type}, sessions: [...]}}` |
| `GET /api/sessions/<name>` | One entry |
| `GET /api/storage/health/all` | `{name: {timestamp, overall_status, status_reasons, sources, arrays, disks, volumes, events}}`; the flattened keys are not included |
| `GET /api/storage/health/<name>` | One entry |
| `GET /api/storage/breakdown/all` | `{name: {timestamp, owner_source, reference_time, cold_thresholds_days, device: {bytes, files, cold_1y_bytes, cold_3y_bytes, owners (top 25), other}, folders: [{path, folder_owner, bytes, files, cold_1y_bytes, cold_3y_bytes, top_owner, top_owner_bytes}], folders_truncated, volume_owners}}`. At most 50 KB per device: folders sorted by bytes descending and cut. |
| `GET /api/storage/breakdown/<name>[?folder=<path>]` | The latest run from the per-system database, as sent (every owner row), plus `system`, `id`, `msg_id`, `timestamp`, `device_type`. `folder` (1–4096 characters, no NUL; else 400) limits it to that folder (404 if the run doesn't list it). |
| `GET /api/user_metrics/usage?window=24h\|7d\|30d[&device=<name>][&top=N]` | Per-user totals (below) |
| `GET /api/rollups/status` | `{system directory name: {table: {pending_ids, done_ids, frozen_before, series, complete, last_live_id}}}`; `pending_ids` / `done_ids` are id spans, not row counts; `{}` when no database has rollups yet |

**Sessions snapshot semantics.** Only a message at least as new as the stored one updates it.
`full`: the list is replaced and `list_timestamp` = `list_confirmed_at` = that message's time.
`unchanged` with the stored list's hash: the list is kept and `list_confirmed_at` moves.
`unchanged` with another hash: list and `list_confirmed_at` are kept, so the list's real age shows.
`none`: the list is cleared and `list_hash`, `list_timestamp`, `list_confirmed_at` become null. The
counts in `summary` always come from the latest message. Central snapshots are filled only by live
messages; history stored earlier is not replayed into them.

**`/api/user_metrics/usage`.**

```
{"window": "24h", "from": ISO, "to": ISO,
 "devices": {"<registered name>": {"source": "rollups"|"raw", "complete": bool,
   "users": [{"username", "samples", "cpu_core_hours", "avg_cores", "peak_cores",
              "gpu_busy_hours", "gpu_held_hours", "peak_gpu_vram_bytes"}]}}}
```

- `window` default `24h` (ending now); `top` default 50, below 1 → 400, above 200 capped at 200;
  `device` of an unknown system → 404, an invalid name → 400; a bad window → 400.
- `cpu_core_hours` = Σ`cpu_seconds`/3600; `avg_cores` = Σ`cpu_seconds` / window seconds;
  `peak_cores` = max `cpu_cores`; `gpu_busy_hours` = Σ`gpu_busy_seconds`/3600; `gpu_held_hours` =
  Σ`gpu_held_seconds`/3600; `peak_gpu_vram_bytes` = max `gpu_vram_bytes`. An aggregate over no
  measured value is null, never 0.
- Sorted by `gpu_busy_hours`, then `cpu_core_hours`, descending, nulls last.
- `source: "rollups"` (hourly) when the system's `user_metrics` rollups are complete; these include
  the hour the window starts in, so a total can hold up to one extra hour. Otherwise `raw` (the
  per-system `user_metrics` rows). `complete` is false only while the rollup backfill is pending; the
  raw totals are exact then. `samples`: raw = row count; rollups = the user's largest per-field count.
- Every system with a history database is listed (`users: []` when it has no per-user data).
- Answers are cached for Manager config `usage_cache_seconds` (default 300; 0 = no cache) per
  (window, device, top). `DELETE /api/system/<name>` clears the cache.

### 17. History API extension and rollup semantics

`GET /api/history/<sys>/<type>/<field>` gains two optional query parameters:

| Parameter | Values | Meaning |
|-----------|--------|---------|
| `resolution` | `auto` (default; empty = auto), `raw`, `hour`, `day` | Which data to answer from; anything else → 400 |
| `entity` | 1–256 printable characters | One user of `user_metrics`; any other data type → 400. Without it the series mixes all users, as before. |

**Response.** The old keys are unchanged; added: `"entity": null | "<user>"`, `"resolution": "raw" |
"hour" | "day"` (what was actually used), `"bucket_seconds": int | null` (width of an aggregated
point), `"downsampled_from": int | null` (values averaged). A point that aggregates several values
(downsampled raw data or rollups) also carries `min`, `max` and `count`; `downsampled` is true
whenever the values are averages. Clients that read only `timestamp` and `value` are unaffected.

**Planner:**
- A tail query (no `from`) and `resolution=raw` always use raw rows.
- `user_metrics` without `entity` always uses raw rows (every user mixed, as before), also with an
  explicit `hour` or `day` (the response then says `resolution: "raw"`).
- Explicit `hour` / `day` is honoured when the field has a rollup series in the window and the table
  has that level; otherwise raw.
- `auto`:
  - raw while that table's rollup backfill is pending;
  - otherwise by the point width w = (span from the field's first to its last value in the window) /
    `max_points` (`max_points` defaults to `limit` for a window query): raw below 3600 s, hourly
    below 86400 s, daily above;
  - but raw whenever the window holds at most `max_points` values of that field (and user), so small
    sets keep their exact points;
  - where retention pruned raw rows of the window, at least hourly (rollups are kept forever);
  - `storage_health` has daily rollups only: raw where it would be hourly, if raw covers the window,
    else daily.
- A rollup point merges consecutive buckets k at a time (at most `max_points` points). Its timestamp
  is the count-weighted centre of the buckets it merges, kept within the window's first and last
  value (the bucket edge where raw rows are pruned).
- After retention pruned every raw row of a table, window queries are still answered from the rollups;
  a tail query and `resolution=raw` return an empty `data` list, as before.
- On data up to about 30 days every Dashboard query stays on raw rows; with about 83 days or more the
  Dashboard's "All Available" (2000 points) comes from hourly rollups.

**Units.** Raw answers: the rollup registry's default unit (`user_metrics`: `cpu_percent` %,
`ram_bytes` bytes, `cpu_seconds` s, `cpu_cores` cores, `gpu_vram_bytes` bytes, `gpu_util_percent` %,
`gpu_busy_seconds` s, `gpu_held_seconds` s), else the newest row's `<field>_unit` / `_unit`, else for
`storage_health` the name suffix (`_temperature_c` °C, `_percent` %, `_hours` h,
`_sectors` / `_errors` / `_code` / `_degraded` count), else null. Rollup answers use the rollup
series' unit after the registry. `storage_breakdown` fields: `files` count, byte metrics bytes.

**storage_breakdown history fields:** `owner/<owner>/<metric>` (the device rollup of one owner) and
`folder/<path>/<metric>` (a folder total), metric = `bytes`, `files`, `cold_1y_bytes` or
`cold_3y_bytes`; everything between the first and the last `/` is the owner or path, and a leading
`/` lost in the URL is restored. A numeric top-level key (for example `top_owners_effective`) can be
charted too. `storage_breakdown` is not rolled up.

**Rollup semantics:**
- **What:** `system_metrics` hourly and daily (`cpu_count` excluded); `user_metrics` hourly and daily,
  one series per username (`interval_s` excluded); `sessions` hourly and daily
  (`sessions_truncated` excluded); `storage_health` daily only. Only numeric columns (declared REAL:
  the Manager declares a column REAL when it first sees a number, boolean or null in it) are rolled
  up, never `id`, `timestamp`, `device_type`, `msg_id`, `*_unit`, `payload_*` or `_*` columns.
  `folder_usage` and `storage_breakdown` are not rolled up.
- **Buckets:** UTC hours and days, computed in SQL as
  `(CAST(strftime('%s', timestamp) AS INTEGER) / W) * W`. Each holds `n`, `vsum`, `vmin`, `vmax`;
  `avg = vsum / n`. A timestamp SQLite can't parse gets no bucket (logged).
- **No p95:** a percentile can't be merged across buckets.
- **Gaps:** NULL, NaN and text values are skipped, so a gap never becomes 0. Rows stored before this
  release (`interval_s` NULL) add their 1-s `cpu_percent` to the same series and nothing to the new
  fields.
- **Booleans:** 0/1, so their average is the fraction of runs that were true.
- **Retention:** rollups are kept forever. Raw rows are pruned by `history_retention_days` (item 9)
  only after the table's rollups are complete; the table's `frozen_before` is then set to the cutoff,
  and a row older than that is never rolled up again (a pruned message replayed later is stored but
  not counted twice).

### 18. Compatibility matrix and data already stored

| Sender → receiver | Behaviour |
|-------------------|-----------|
| NEW collector → OLD Manager | New data types go into wide tables with no side effects (R1 and R5 keep `store()` working). New `user_metrics` / `system_metrics` keys become columns, which the OLD Manager backfills with one full-table UPDATE each; on a large table that can exceed the collector's 30 s timeout, and since the OLD Manager has no dedup, the retried batch is stored twice. Its central code reads `null` as 0, so GPU-only users show 0 CPU. **Never point new collectors at an OLD Manager.** |
| NEW collector → CURRENT Manager | New data types go into wide tables (`sessions`, `storage_health`, `storage_breakdown`, lists as JSON text); no central snapshots, no rollups. New keys become columns; earlier rows read NULL with no UPDATE. A NEW Manager later migrates the wide `storage_breakdown` rows and rolls up the rest. |
| OLD or CURRENT collector → NEW Manager | No new fields arrive. The new central tables stay empty, the Dashboard hides the new sections, `system_metrics` and `user_metrics` are still rolled up. |
| NEW Dashboard → OLD or CURRENT Manager | The new endpoints answer 404 (the OLD Manager answers `/api/user_metrics/usage` with 200 `[]` through its `<device_name>` route, which the Dashboard also reads as absent); the sections are hidden (R7). `resolution` and `entity` are ignored by those Managers, so charts behave as before, with no band; a per-user chart answers 404 "Per-user history needs a newer Manager" instead of a mixed series. |
| OLD or CURRENT Dashboard → NEW Manager | New endpoints are unused; new keys in `/api/metrics/all`, `/api/user_metrics/*` and `/api/history` are ignored. Per-user endpoints leave a NULL usage field out, and `/api/metrics/all` passes the item 11 keys through only when the latest message has them, so old data reads back exactly as before. |
| NEW Manager → CURRENT Manager (downgrade) | Safe: CURRENT reserves the `_` and `storage_breakdown_` table prefixes, so it lists none of NEW's tables, takes no backup and ingests normally. `storage_breakdown` messages it receives go into a new wide table, which the next NEW start migrates (to `storage_breakdown_legacy_1`, ...) after its own backup, skipping runs NEW already holds (same msg_id, or same timestamp as a run stored without one); rows CURRENT stored are rolled up as a gap. While CURRENT runs, it does not show the storage-breakdown runs NEW stored (narrow tables); they are served again after the next upgrade. CURRENT updates per-user snapshot rows without the usage columns, so NEW serves those only once a new reading has them (`usage_timestamp`). Verified over three cycles by `tests/compat/test_new_payloads_old_managers.py`. Never downgrade below the review-fix release (CURRENT: `e5cd699`; `160da09` and `fb6498c` write the same databases). Going back to CURRENT needs no rollback tool: start the CURRENT code on the same `data_dir`. |

**Data already stored:**

| Data | What happens |
|------|--------------|
| `system_metrics` and `user_metrics` rows (per-system and central) | New columns read NULL. Old rows' `cpu_percent` is a 1-s sample (`interval_s` IS NULL). Central `device_user_snapshot` and `user_metrics_history` gain 10 columns (`device_user_snapshot` also `usage_timestamp`), added once after a pre-upgrade backup of `metrics.db` (from OLD and from CURRENT). |
| Wide `sessions` and `storage_health` tables written by OLD or CURRENT | Used as they are; NEW keeps both types wide. Central snapshots are filled by the next live message (no backfill from history); rollups backfill their counts and flattened keys. |
| Wide `storage_breakdown` table | Migrated to the narrow layout after a pre-upgrade backup of that `data.db`, then renamed to `storage_breakdown_legacy` and kept. msg_ids are kept; ids are kept by the first migration (later ones, after a downgrade, give new ids). |
| Rollup tables (`_rollup_*`) | Added to each `data.db`; one pre-upgrade backup first when it already has a table, even one without rows (taken with the migration when there is one; none for a `data.db` with no table). |
| `folder_usage` | Untouched |
| Rollups | Backfilled in the background from the full raw history, OLD-format rows included |

Nothing stored is rewritten or deleted by the upgrade. New tables and columns only; any one-time
migration takes a `.pre-upgrade-<stamp>.bak` copy first (`backup_before_migration`).

---

## Appendix: Example payloads

Each block is one message's `data` object; headers follow item 1. Names and addresses are examples.

**`system_metrics`** (new keys only; the existing keys are unchanged and left out):

```json
{"data_type":"system_metrics","cpu_percent":23.4,"cpu_percent_unit":"%","interval_s":300.2,"interval_s_unit":"s",
 "cpu_count":32,"cpu_iowait_percent":1.2,"cpu_iowait_percent_unit":"%","cpu_steal_percent":0.0,"cpu_steal_percent_unit":"%",
 "proc_cpu_coverage_percent":96.5,"proc_cpu_coverage_percent_unit":"%",
 "disk_read_bytes_per_s":1250000,"disk_read_bytes_per_s_unit":"B/s","disk_write_bytes_per_s":310000,"disk_write_bytes_per_s_unit":"B/s",
 "disk_read_iops":41.2,"disk_read_iops_unit":"ops/s","disk_write_iops":12.0,"disk_write_iops_unit":"ops/s",
 "disk_await_ms":6.3,"disk_await_ms_unit":"ms","disk_busy_max_percent":17.0,"disk_busy_max_percent_unit":"%","disk_busy_max_name":"sda",
 "gpu_sample_seconds":300.2,"gpu_sample_seconds_unit":"s","gpu_count":1,
 "gpus":[{"index":0,"name":"NVIDIA RTX A6000","gpu_percent":87,"vram_used_bytes":22548578304,"vram_total_bytes":51527024640,
          "vram_used_formatted":"21.0 GB","vram_total_formatted":"48.0 GB","temperature_c":71,"power_watts":250.1,
          "uuid":"GPU-8f2c0000-1111-2222-3333-444455556666","process_source":"nvml","util_split":"single_user",
          "users":[{"username":"LAB\\alice","vram_bytes":21474836480,"util_percent":87.0,"processes":2}],
          "users_truncated":0,"unattributed_vram_bytes":1073741824,"unattributed_processes":1}]}
```

**`user_metrics`:** a normal user, and a user seen only on a GPU:

```json
{"data_type":"user_metrics","username":"LAB\\alice","cpu_percent":12.5,"ram_bytes":8589934592,"ram_formatted":"8.0 GB",
 "cpu_seconds":1200.4,"cpu_cores":4.0,"interval_s":300.2,"process_count":17,
 "gpu_vram_bytes":21474836480,"gpu_util_percent":87.0,"gpu_busy_seconds":261.2,"gpus_held":1,"gpu_held_seconds":300.2,"gpu_processes":2}
```

```json
{"data_type":"user_metrics","username":"LAB\\bob","cpu_percent":null,"ram_bytes":null,"ram_formatted":null,
 "gpu_vram_bytes":1073741824,"gpu_util_percent":0.0,"gpu_busy_seconds":0.0,"gpus_held":1,"gpu_held_seconds":300.2,"gpu_processes":1}
```

**`sessions`** on a NAS (collector not root): a full list, then the next run with the list unchanged:

```json
{"data_type":"sessions","_schema":1,"_unit":"count","_detail":"full",
 "_sources":{"tcp":"ok","utmp":"ok","smb":"not_permitted"},"_collect_seconds":0.04,
 "_list_state":"full","_list_hash":"3f9a1c2b7d4e5f60",
 "clients_total":9,"users_total":1,"smb_clients":7,"nfs_clients":1,"ssh_clients":1,"ssh_users":1,"console_users":0,
 "dsm_clients":2,"rsync_clients":0,"sessions_truncated":false,
 "sessions":[{"service":"ssh","username":"alice","client":"10.0.0.40","connections":1,"since":"2026-11-21T12:35:00Z","source":"utmp"},
             {"service":"smb","client":"10.0.0.17","connections":2,"source":"tcp"}]}
```

```json
{"data_type":"sessions","_schema":1,"_unit":"count","_detail":"full",
 "_sources":{"tcp":"ok","utmp":"ok","smb":"not_permitted"},"_collect_seconds":0.03,
 "_list_state":"unchanged","_list_hash":"3f9a1c2b7d4e5f60",
 "clients_total":9,"users_total":1,"smb_clients":7,"nfs_clients":1,"ssh_clients":1,"ssh_users":1,"console_users":0,
 "dsm_clients":2,"rsync_clients":0,"sessions_truncated":false}
```

**`storage_health`** on a NAS with an array rebuilding:

```json
{"data_type":"storage_health","_schema":1,"trigger":"change","overall_status":"warning","overall_status_code":1,
 "status_reasons":["md2 rebuilding 37.5% (ETA 5h 12m)"],
 "sources":{"md":"ok","sysfs":"ok","mounts":"ok","hwmon":"unavailable"},
 "arrays":[{"id":"md2","kind":"md","name":null,"level":"raid5","role":"data","state":"active","status":"warning",
            "members_total":4,"members_active":3,"degraded":1,"sync_action":"recover","sync_percent":37.5,
            "sync_speed_kbps":120000,"sync_eta_seconds":18720,"mismatch_cnt":0,"size_bytes":11995903803392,
            "faulty_members":[],"members":["sata1","sata2","sata4","sata3"]}],
 "disks":[{"id":"sata3","model":"WD80EFZX","serial":null,"size_bytes":8001563222016,"media":"hdd","bus":"sata",
           "status":"ok","health_source":"sysfs","smart_passed":null,"temperature_c":null,"power_on_hours":null,
           "reallocated_sectors":null,"pending_sectors":null,"uncorrectable_sectors":null,
           "read_errors_uncorrected":null,"write_errors_uncorrected":null,"wear_used_percent":null,
           "remaining_life_percent":null,"bad_sectors":null,"io_errors":0,"device_state":"running",
           "in_standby":null,"arrays":["md2"]}],
 "volumes":[{"id":"/volume1","fs":"btrfs","read_only":false,"health":null,"status":"ok"}],
 "events":null,"_flat_truncated":false,
 "disk_sata3_status_code":0,"disk_sata3_io_errors":0,
 "array_md2_status_code":1,"array_md2_degraded":1,"array_md2_sync_percent":37.5,
 "volume_volume1_status_code":0}
```

**`storage_breakdown`** on Linux or DSM:

```json
{"data_type":"storage_breakdown","_schema":1,"_unit":"bytes","reference_time":"2026-11-21T02:00:03Z",
 "cold_thresholds_days":[365,1095],"owner_source":"posix_uid","age_source":"mtime",
 "top_owners":10,"top_owners_effective":10,"owners_truncated":false,
 "folders":[{"path":"/volume1/Lab","folder_owner":"alice","bytes":5000,"files":120,"cold_1y_bytes":3000,"cold_3y_bytes":1000,
             "owners":[{"owner":"alice","bytes":4000,"files":100,"cold_1y_bytes":2500,"cold_3y_bytes":900}],
             "other":{"owner_count":3,"bytes":1000,"files":20,"cold_1y_bytes":500,"cold_3y_bytes":100}}],
 "device":{"bytes":5000,"files":120,"cold_1y_bytes":3000,"cold_3y_bytes":1000,
           "owners":[{"owner":"alice","bytes":4000,"files":100,"cold_1y_bytes":2500,"cold_3y_bytes":900}],
           "other":{"owner_count":3,"bytes":1000,"files":20,"cold_1y_bytes":500,"cold_3y_bytes":100}},
 "volume_owners":null,"_timed_out":[],"_errors":[],"_breakdown_unavailable":[]}
```

**`storage_breakdown`** on Windows (no owners):

```json
{"data_type":"storage_breakdown","_schema":1,"_unit":"bytes","reference_time":"2026-11-21T02:00:05Z",
 "cold_thresholds_days":[365,1095],"owner_source":"none","age_source":"mtime",
 "top_owners":10,"top_owners_effective":10,"owners_truncated":false,
 "folders":[{"path":"E:\\Users\\alice","folder_owner":null,"bytes":7300,"files":4,"cold_1y_bytes":6000,"cold_3y_bytes":4000,
             "owners":null,"other":null}],
 "device":{"bytes":7300,"files":4,"cold_1y_bytes":6000,"cold_3y_bytes":4000,"owners":null,"other":null},
 "volume_owners":null,"_timed_out":[],"_errors":[],"_breakdown_unavailable":[]}
```
