# Field checks for the usage-metrics release

The usage-metrics release (`feature/usage-metrics`) was built and tested on a Linux development box
with fake `/proc`, fake NVML, fake Windows APIs and fake PowerShell output. Several things it relies
on can only be checked on the real machines: what a non-root DSM account can read, how the PowerShell
scripts behave as SYSTEM, how NVML reports real GPU processes, what the storage breakdown costs on an
armv7 NAS, and how the Manager performs on the Windows server. **Do these checks before trusting the
new defaults on a platform.** Each check gives the commands, what to compare, the pass criterion, and
which setting to turn off if it fails.

- Pass criteria marked *(spec)* come from the release's design; the others are proposals, chosen so
  that a failure means a real problem rather than noise.
- Turning a setting off is done in that collector's config (`collector/local/config.json`;
  `/etc/lab-monitor/collector.json` on Linux) with `config_tool.py set ...` or an editor, and takes
  effect on the next run. Keys and defaults: [collector/README.md](../collector/README.md#usage-metrics-settings).
  If an armv7 or Windows default has to change for every unit, tell the developer as well: the default
  lives in `collector/lib/settings.py`.
- **The outputs hold real user names, client IPs, folder names and serial numbers.** Keep them in a
  private folder, give them to the owner directly, delete them afterwards, and never commit or paste
  them anywhere public (the repository is public).
- `--dry-run` prints the messages and sends and saves nothing (it still writes to the log file). It
  shows interval averages only when a real run has written a baseline before, which the scheduled
  runs do.
- Order of the rollout (collectors only after the Manager and Dashboard): Linux GPU server, x86 DSM,
  armv7 DSM, Windows servers, everything else. Each platform's collector upgrade waits for its
  checks below.

Not part of this release, so not checked here: SNMP, a privileged helper, SMART on Linux and DSM.

**Contents:** [DSM (x86 and armv7)](#synology-dsm-x86-and-armv7) ·
[armv7 extras](#synology-dsm-armv7-extras) · [Windows Server](#windows-server-2016-2019-2022) ·
[Linux GPU server](#linux-gpu-server-ubuntu) · [Atlantis](#atlantis-manager-and-dashboard-server) ·
[Recording results](#recording-results)

---

## Synology DSM (x86 and armv7)

Do this on one x86 unit and one armv7 unit, on each DSM version in the fleet (7.1 runs kernel 4.4; 7.2
runs 4.4.302 or 5.10 depending on the platform). SSH in as **the account the DSM tasks run as** (the
owner of `local/config.json`), not root, unless a step says so.

```bash
uname -m; uname -r; cat /etc.defaults/VERSION                # record: architecture, kernel, DSM build
PY=/volume1/lab-monitor/lab-monitor-env/bin/python3          # the interpreter the DSM tasks use
cd /volume1/lab-monitor/scripts/lab-monitor/collector
mkdir -p ~/lm-check && chmod 700 ~/lm-check
# show FILE TYPE: print the data of the messages of one data_type from a --dry-run output
show() { "$PY" -c 'import json,sys; [print(json.dumps(m["data"], indent=1)) for m in json.load(open(sys.argv[1])) if m["data"]["data_type"]==sys.argv[2]]' "$1" "$2"; }
```

### 1. What the task account can read

```bash
cat /proc/sys/kernel/random/boot_id
cat /proc/mdstat
head -3 /proc/net/tcp; head -3 /proc/net/tcp6
ls -l /sys/block/*/device
for d in /sys/block/*/device; do echo "$d: model=$(cat $d/model 2>&1) state=$(cat $d/state 2>&1) ioerr=$(cat $d/ioerr_cnt 2>&1)"; done
head -c 100 /proc/1/stat; echo            # a process of another user (root)
grep -w proc /proc/mounts                   # look for hidepid=
```

- **Compare:** every command prints values; none says `Permission denied`.
- **Pass:** all readable, and the `/proc` mount has no `hidepid=1` or `hidepid=2` (`hidepid=0`, or no
  `hidepid` option, is fine).
- **If it fails:**
  - `boot_id` unreadable: nothing to turn off. The collector falls back to the boot time, which only
    makes reboot detection less exact.
  - `/proc/1/stat` unreadable or `hidepid` set: the collector skips per-user data with a WARNING.
    Nothing to turn off; `"user_metrics": "off"` stops the messages if you prefer.
  - `/proc/net/tcp` unreadable: session counts are null. `"sessions": {"enabled": false}` if nothing
    else (utmp) is readable either.
  - `/proc/mdstat`, `/sys/block` unreadable: storage health is `unknown`. `"storage_health":
    {"enabled": false}`.

### 2. SSH logins by name (utmp)

```bash
ls -l /var/run/utmp /run/utmp
# from another machine, log in over SSH as a second user and stay logged in; then here:
who
"$PY" collector.py --config local/config.json --mode metrics --dry-run 2>/dev/null > ~/lm-check/metrics.json
show ~/lm-check/metrics.json sessions
```

- **Compare:** `_sources` (`utmp` ok?), `ssh_users`, and the `ssh` rows in `sessions` against `who`.
- **Pass:** utmp exists and is readable, and every SSH login `who` shows is a row with that user name.
- **If it fails:** nothing to turn off. Without a readable utmp written by DSM's sshd there are no
  `ssh_users` and no named SSH rows; SSH clients are still counted by IP.

### 3. Owner names of domain users

Only where scanned folders hold files of directory (AD/LDAP) users.

```bash
ls -ln /volume1/<share> | head             # numeric owner uids of files
"$PY" -c 'import pwd,sys; print(pwd.getpwuid(int(sys.argv[1])).pw_name)' <a domain uid from above>
```

- **Pass:** the uid resolves to the account name (the collector uses the same `pwd` lookup).
- **If it fails:** storage owners show as `uid:<n>`. Acceptable, or `"storage_breakdown": {"owners":
  "none"}` if those numbers are of no use. If lookups are just slow, the log says so
  (`owner name lookups took longer than 30s`): raise `storage_breakdown.name_lookup_budget_seconds`.

### 4. Cost of the metrics run

Compare the run with and without the interval counters, in a private copy of the config:

```bash
cp local/config.json ~/lm-check/off.json && chmod 600 ~/lm-check/off.json
"$PY" config_tool.py --config ~/lm-check/off.json set perf_counters false
time "$PY" collector.py --config local/config.json      --mode metrics --dry-run >/dev/null 2>&1
time "$PY" collector.py --config ~/lm-check/off.json    --mode metrics --dry-run >/dev/null 2>&1
```

- **Compare:** `user` + `sys` of the two runs (`real` differs anyway: the old path sleeps 1 s).
- **Pass (proposed):** the new run costs at most 0.2 s (x86) / 0.5 s (armv7) more `user + sys` CPU
  than the old one. The design estimate for the process walk is 20–50 ms (x86) and 80–200 ms (armv7).
- **If it fails:** `"perf_counters": false` (back to the 1-s CPU sample; no per-user CPU-seconds, no
  disk I/O on that unit).

### 5. CPU and disk I/O against Resource Monitor

Under a steady load for at least 30 minutes (for example a large copy to the NAS), compare the
Dashboard's history charts (CPU, Disk I/O tiles) with DSM's Resource Monitor > Performance over the
same time.

- **Compare:** CPU % (5-minute averages on the Dashboard against Resource Monitor's CPU graph); disk
  read and write throughput (the Dashboard sums all physical disks, so add up the disks in Resource
  Monitor's Disk tab); the busiest disk %.
- **Pass (proposed):** CPU within 5 percentage points; disk throughput within 15 %.
- **If it fails:** CPU wrong: `"perf_counters": false`. Disk wrong: `"disk_io": false`, or
  `disk_io_exclude` for a device that should not be summed.

### 6. Sector size of `/proc/diskstats`

The collector counts `/proc/diskstats` sectors as 512 bytes. Write a known amount and compare. Find
the md array behind the volume first (`cat /proc/mdstat`; `/volume1` is usually `md2`). Use random
data, because a compressed Btrfs share would shrink zeros to nothing.

```bash
MD=md2; F=/volume1/<a share>/lm-ddtest
awk -v d=$MD '$3==d {print $10}' /proc/diskstats      # sectors written, before
dd if=/dev/urandom of=$F bs=1M count=256 conv=fsync
awk -v d=$MD '$3==d {print $10}' /proc/diskstats      # after
rm -f $F
```

- **Compare:** (after − before) × 512 against 268435456 bytes (256 MiB).
- **Pass (proposed):** between 268435456 and 335544320 bytes (up to 25 % more for file-system metadata and
  other writes meanwhile). The check is for a wrong unit, which would be off by a factor of 2 or 8.
- **If it fails** (off by a factor, e.g. 8): `"disk_io": false` on that model, and report it.

### 7. Storage health against Storage Manager

```bash
"$PY" collector.py --config local/config.json --mode health --dry-run 2>/dev/null > ~/lm-check/health.json
show ~/lm-check/health.json storage_health
ls /sys/class/hwmon/*/name 2>/dev/null && cat /sys/class/hwmon/*/name
```

- **Compare:** `overall_status` and `status_reasons` against Storage Manager's pool and volume state;
  each `arrays` entry (state, members, a resync's progress) against Storage Pool; each disk (`model`,
  `status`, `temperature_c` if hwmon gives one) against HDD/SSD. `sources` shows what was readable.
- **Pass:** a healthy NAS is `ok` with no reasons; a degraded or rebuilding pool, if there is one, is
  `warning` or `critical` with a matching reason; every data disk is listed; temperatures, where
  present, are within 2 °C of Storage Manager.
- **If it fails:** a false warning from one component: add its id to `storage_health.ignore` (e.g.
  `["sata5"]`) and report it. Wrong overall: `"storage_health": {"enabled": false}`.

### 8. Storage health message rate

After the upgraded collector has run for a day:

```bash
cat /volume1/lab-monitor/logs/collector.log* | grep 'Storage health:' | grep -c 'sent (change)'
cat /volume1/lab-monitor/logs/collector.log* | grep 'Storage health:' | grep -c 'sent (interval)'
```

- **Pass:** about 24 `interval` sends a day, and `change` sends only when something really changed
  (on a healthy, quiet NAS: almost none). md arrays move between `clean` and `active` by themselves;
  that must not count as a change.
- **If it fails** (dozens of `change` sends a day): report it with the `status_reasons` / array states
  of a few of those messages; until fixed, `"storage_health": {"enabled": false}`.

### 9. The Connected list against Resource Monitor

With a few clients connected over SMB, NFS or AFP, compare the Dashboard's Connected section (or
`show ~/lm-check/metrics.json sessions` from a fresh dry run) with Resource Monitor > Connected Users.

- **Compare:** client IPs and per-service counts. Without root, SMB, NFS and AFP clients appear by IP
  only, with no user name; that is expected.
- **Pass:** every client IP Resource Monitor lists appears, under the right service; `clients_total`
  matches the number of distinct client IPs.
- **If it fails:** `"sessions": {"detail": "counts"}` (counts only) or `"sessions": {"enabled":
  false}`, and report it.

### 10. Optional: `smbstatus -b` as root

Only relevant if a DSM task runs the collector **as root**: then, and only then, it reads
`smbstatus -b` for SMB user names. The parser was written from documentation, not from DSM output.

```bash
sudo smbstatus -b > ~/lm-check/smbstatus-b.txt
"$PY" -c 'import sys; sys.path.insert(0, "."); from lib import sessions; [print(r) for r in sessions.parse_smbstatus_brief(open(sys.argv[1]).read())]' ~/lm-check/smbstatus-b.txt
```

- **Pass:** one parsed row per line of the `smbstatus -b` output, with the same user name and client IP.
- **If it fails:** report it with an anonymized copy of the output (replace names and addresses
  before it goes anywhere near the repository; it becomes the test fixture). Counts by IP are not
  affected.

---

## Synology DSM armv7 extras

On the slowest armv7 unit, in addition to the checks above.

### 11. Disk-scan benchmark of the storage breakdown *(spec gate)*

Measure the daily disk run with and without the breakdown on one folder of about 1 million files, in a
scratch collector that never reaches the Manager (`manager_url` points at a closed local port, so its
messages stay in its own scratch queue).

```bash
FOLDER="/volume1/<share>/<folder with about 1M files>"
B=/volume1/lm-bench; mkdir -p $B && chmod 700 $B
cp local/config.json $B/on.json && chmod 600 $B/on.json
for k in "name lm-bench" "manager_url http://127.0.0.1:9" "manager_token none" \
         "data_dir $B/data" "log_file $B/collector.log" "storage_breakdown.enabled true"; do
    "$PY" config_tool.py --config $B/on.json set $k
done
"$PY" config_tool.py --config $B/on.json set scan_paths "[\"$FOLDER\"]"
cp $B/on.json $B/off.json
"$PY" config_tool.py --config $B/off.json set storage_breakdown.enabled false

find "$FOLDER" -type f | wc -l                                              # file count; also warms the cache
time "$PY" collector.py --config $B/off.json --mode disk >/dev/null 2>&1    # off 1
time "$PY" collector.py --config $B/on.json  --mode disk >/dev/null 2>&1    # on 1: records the folder's time (plain walk)
time "$PY" collector.py --config $B/on.json  --mode disk >/dev/null 2>&1    # on 2: with the breakdown
time "$PY" collector.py --config $B/off.json --mode disk >/dev/null 2>&1    # off 2
grep -E 'Storage breakdown:|Disk scan incomplete' $B/collector.log
```

- **Check first:** the log's `Storage breakdown:` line of run "on 2" says `0 measured without it`.
  Otherwise the time guard kept the folder on the plain walker (its first scan took more than 0.6 ×
  `timeout_seconds`) and "on 2" measured nothing new: use a smaller folder or a larger
  `timeout_seconds` in both scratch configs and start again.
- **Compare:** `real` of "on 2" against the mean of "off 1" and "off 2".
- **Pass *(spec)*:** "on 2" is at most 15 % slower, and no folder times out (no `Disk scan
  incomplete` line from an "on" run that the "off" runs don't have).
- **If it fails:** `"storage_breakdown": {"enabled": false}` on every armv7 unit, and tell the developer
  that the armv7 default must ship as `false`. (Measured on an x86 test box: +7.6 % for 966k files with
  a warm cache.)
- **Clean up:** `rm -rf /volume1/lm-bench` (it holds a copy of the token and the scratch messages).

### 12. Process-walk cost

Check 4 above, with the armv7 limit (0.5 s more `user + sys`). armv7 units usually have no psutil, so
this is the first release that walks `/proc` for per-user data there.

---

## Windows Server (2016, 2019, 2022)

Do this on one server of each version in the fleet, plus one GPU server with the **TCC** and one with
the **WDDM** driver model if both exist. Run the checks as **the account the collector tasks use**:

```powershell
(Get-ScheduledTask "Lab Monitor - Metrics Collection").Principal | Select-Object UserId, RunLevel
```

Usually that is SYSTEM. A one-off task runs the script below as SYSTEM and writes its results to a
folder only Administrators can read (Anaconda PowerShell Prompt, *Run as Administrator*):

```powershell
$Chk = 'C:\lm-check'
New-Item -ItemType Directory $Chk -Force | Out-Null
icacls $Chk /inheritance:r /grant:r 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' | Out-Null
# The python.exe the collector tasks use: the installer wrote it into the wrapper .bat files
Select-String -Path E:\Users\lab-monitor\bin\run-metrics-collector.bat -Pattern 'python'
```

Save as `C:\lm-check\check.ps1`, with `$Py` set to that `python.exe`:

```powershell
$Out = 'C:\lm-check'
$Py  = 'C:\ProgramData\miniconda3\envs\lab-monitor\python.exe'     # <- the tasks' python.exe
$Col = 'E:\Users\lab-monitor\scripts\lab-monitor\collector'
"whoami: $(whoami)" | Out-File "$Out\check.txt"
"LanguageMode: $($ExecutionContext.SessionState.LanguageMode)" | Out-File "$Out\check.txt" -Append
$t = Measure-Command { Get-PhysicalDisk | Get-StorageReliabilityCounter | Format-List * | Out-File "$Out\reliability.txt" }
"Get-StorageReliabilityCounter: $([int]$t.TotalSeconds) s" | Out-File "$Out\check.txt" -Append
Get-PhysicalDisk | Format-Table DeviceId, FriendlyName, BusType, MediaType, HealthStatus, OperationalStatus -AutoSize |
    Out-File "$Out\disks.txt"
Get-VirtualDisk -ErrorAction SilentlyContinue | Format-Table FriendlyName, HealthStatus, OperationalStatus -AutoSize |
    Out-File "$Out\virtualdisks.txt"
Get-WinEvent -FilterHashtable @{LogName = 'System'; StartTime = (Get-Date).AddDays(-30)} -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -match '^(disk|Ntfs|Microsoft-Windows-Ntfs|storahci|stornvme|iaStorA|iaStorAVC)$' } |
    Group-Object ProviderName, Id | Sort-Object Count -Descending | Format-Table Count, Name -AutoSize |
    Out-File "$Out\events.txt"
Set-Location $Col
$t = Measure-Command { & $Py collector.py --config local\config.json --mode metrics --dry-run 2> "$Out\metrics.log" > "$Out\metrics.json" }
"metrics dry run: $([math]::Round($t.TotalSeconds, 1)) s" | Out-File "$Out\check.txt" -Append
& $Py collector.py --config local\config.json --mode health --dry-run 2> "$Out\health.log" > "$Out\health.json"
& $Py -c "import psutil; print(sorted(psutil.disk_io_counters(perdisk=True)))" > "$Out\perdisk.txt" 2>&1
Get-SmbSession -ErrorAction SilentlyContinue | Format-Table ClientComputerName, ClientUserName, NumOpens -AutoSize |
    Out-File "$Out\smbsessions.txt"
quser 2>&1 | Out-File "$Out\quser.txt"
```

Run it as SYSTEM, then remove the task:

```powershell
$A = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\lm-check\check.ps1'
Register-ScheduledTask -TaskName 'LM field check' -Action $A -User 'NT AUTHORITY\SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask 'LM field check'
do { Start-Sleep 5 } while ((Get-ScheduledTask 'LM field check').State -eq 'Running')
Unregister-ScheduledTask 'LM field check' -Confirm:$false
Get-Content C:\lm-check\check.txt
# Print one data_type from a dry-run output:
function Show($File, $Type) { (Get-Content $File -Raw | ConvertFrom-Json) | Where-Object { $_.data.data_type -eq $Type } |
    ForEach-Object { $_.data | ConvertTo-Json -Depth 8 } }
```

For a user-logon install (tasks under an administrator account), run `check.ps1` from an elevated
prompt as that account instead.

### 13. PowerShell language mode

- **Compare:** `LanguageMode` in `check.txt`.
- **Pass:** `FullLanguage`.
- **If it fails** (`ConstrainedLanguage`, e.g. under AppLocker or WDAC): the health script and the
  PowerShell folder scan can't run. The folder scan falls back to the Python walker by itself;
  set `"storage_health": {"enabled": false}` and report it.

### 14. Reliability counters: time and output

- **Compare:** the time in `check.txt`; `reliability.txt` and `disks.txt` against Server Manager or
  `Get-PhysicalDisk`. Disks behind a hardware RAID controller show `BusType RAID` and blank counters;
  the collector reports them with bus `raid` and null counters, which is expected.
- **Pass (proposed):** under 60 s (the collector kills the script after `storage_health.timeout_seconds`,
  90 s).
- **If it fails:** over 60 s but under 90 s: raise `storage_health.timeout_seconds` (e.g. 150). Errors
  or over 150 s: `"storage_health": {"enabled": false}`.

### 15. Storage health against the server's own view

```powershell
Show C:\lm-check\health.json storage_health
```

- **Compare:** `sources.windows_storage` and `sources.events` (`ok` or `partial`); every physical disk
  (`pd<DeviceId>`) with its `status` against `HealthStatus` in `disks.txt`; Storage Spaces arrays
  against `virtualdisks.txt`; volumes against `Get-Volume`.
- **Pass:** a healthy server is `ok`, every physical disk is listed, and no warning comes from normal
  event noise.
- **If it fails:** a false warning from one disk or volume: add its id to `storage_health.ignore` (e.g.
  `["pd3"]`, `["E:"]`). From events, or wrong overall: `"storage_health": {"enabled": false}` and
  report it with `events.txt`.

### 16. Disk event IDs

- **Compare:** `events.txt` against the rules: disk 52 is critical; disk 7, 11, 51, 154 or Ntfs 50, 55,
  98 are warnings; disk 153 or a storport miniport's 129 at 10 or more per hour is a warning.
- **Pass:** the IDs a healthy server logs regularly don't match a warning rule, or match only below
  its rate.
- **If it fails:** report which ID fires on a healthy server, and how often. Until the rule is fixed,
  `"storage_health": {"enabled": false}` if the warning is constant.

### 17. Sessions: NetSessionEnum and WTS

```powershell
Show C:\lm-check\metrics.json sessions
Get-Content C:\lm-check\smbsessions.txt, C:\lm-check\quser.txt
```

- **Compare:** `_sources.netsession` and `_sources.wts` (`ok`); SMB rows (user, client IP) against
  `Get-SmbSession`; RDP and console rows (user, `state` active / disconnected) against `quser`.
- **Pass:** the same users and client addresses; `rdp_disconnected` equals the disconnected sessions
  `quser` shows. For a user-logon install also check from that account: `netsession not_permitted`
  there means the task needs to run elevated.
- **If it fails:** `"sessions": {"detail": "counts"}` or `"sessions": {"enabled": false}`, and
  report it.

### 18. CPU and disk bytes against typeperf

Start this right after a scheduled metrics run, and compare with the next run's values (Dashboard
history, or the last `system_metrics` line of `E:\Users\lab-monitor\data\<name>\<YYYY-MM>.jsonl`):

```powershell
typeperf "\Processor(_Total)\% Processor Time" "\PhysicalDisk(_Total)\Disk Read Bytes/sec" `
         "\PhysicalDisk(_Total)\Disk Write Bytes/sec" -si 10 -sc 30 -f CSV -o C:\lm-check\typeperf.csv
```

- **Compare:** the averages of the three columns against `cpu_percent`, `disk_read_bytes_per_s` and
  `disk_write_bytes_per_s` of the run that covers those 5 minutes.
- **Pass (proposed):** CPU within 5 percentage points; disk bytes within 15 % under a steady load.
- **If it fails:** CPU: `"perf_counters": false`. Disk: see check 19, else `"disk_io": false`.

### 19. `PhysicalDriveN` disks and Storage Spaces

- **Compare:** `perdisk.txt` (the disk names psutil reports) against `disks.txt` and
  `virtualdisks.txt`.
- **Pass:** only physical disks are listed, or a Storage Spaces virtual disk that also appears as a
  `PhysicalDriveN` has been excluded.
- **If it fails** (a virtual disk is listed, so its traffic counts twice): `"disk_io_exclude":
  ["PhysicalDrive<N>"]` for the virtual one; check 18 again.

### 20. Storage breakdown on Windows

Cold data and file counts come from the same PowerShell pass as the folder sizes; its extra cost was
never measured. Repeat check 11 from an elevated prompt with a Windows scratch config (`data_dir`,
`log_file` under `C:\lm-check\bench`, `scan_paths` one folder of about 1 million files that
Administrators can read), using `python.exe` and `Measure-Command` instead of `time`. Then compare the
cold bytes of that folder:

```powershell
$Folder = 'E:\<a folder>'
$Cut = (Get-Date).ToUniversalTime().AddDays(-365)
(Get-ChildItem -LiteralPath $Folder -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTimeUtc -lt $Cut } | Measure-Object Length -Sum).Sum
```

- **Compare:** run "on 2" against the "off" runs; the sum above against that folder's
  `cold_1y_bytes` in the scratch queue (`C:\lm-check\bench\data\lm-bench\queue.json`).
- **Pass (proposed):** at most 15 % slower with no new timeouts (the owner's requirement: no
  measurable extra cost), and the cold bytes within 1 % (files changing meanwhile).
- **If it fails:** `"storage_breakdown": {"enabled": false}` on the Windows servers, and report it.
- **Clean up:** delete `C:\lm-check` when all Windows checks are done.

### 21. GPUs: TCC and WDDM

```powershell
nvidia-smi --query-gpu=index,name,driver_model.current,driver_version --format=csv
nvidia-smi          # the process table: Type C, G or C+G
```

Start a CUDA job as a test user, wait for the next metrics run, then run `check.ps1` again (or a
metrics dry run) and look at `gpus` in `system_metrics` and that user's `user_metrics`.

- **Compare:** `gpus[i].users` (user, `vram_bytes`, `util_percent`, `processes`) against `nvidia-smi`'s
  process table; the user's `gpus_held` (1) and `gpu_held_seconds` (about the interval).
- **Pass:** the job's user is listed and holds the GPU. On TCC, `vram_bytes` matches `nvidia-smi`'s
  process memory (to the MiB). On WDDM, `vram_bytes` is null and the job is listed as `C` (compute only),
  not `C+G`, so the WDDM rule still counts it as held. Desktop programs (`C+G`) don't hold the GPU.
- **Cost *(spec)*:** the metrics dry run (time in `check.txt`) with `"gpu_per_user": "off"` in a copy
  of the config, against the default: under 0.2 s more.
- **If it fails:** `"gpu_per_user": "off"` (device stats only) and report the driver version.

---

## Linux GPU server (Ubuntu)

As an administrator with `sudo`. The service runs as `labmon` in the systemd sandbox; check 22 uses the
real service run, the others a dry run as `labmon` (same account, same permissions).

```bash
uname -r; lsb_release -ds; nvidia-smi --query-gpu=index,name,driver_version --format=csv,noheader
NAME=<this collector's name>                       # as in /etc/lab-monitor/collector.json
PY=/opt/lab-monitor/venv/bin/python
mkdir -p ~/lm-check && chmod 700 ~/lm-check
dry() { sudo -u labmon $PY -s /opt/lab-monitor/collector/collector.py --config /etc/lab-monitor/collector.json --mode "$1" --dry-run 2>/dev/null; }
show() { python3 -c 'import json,sys; [print(json.dumps(m["data"], indent=1)) for m in json.load(open(sys.argv[1])) if m["data"]["data_type"]==sys.argv[2]]' "$1" "$2"; }
```

### 22. The new features inside the sandbox

```bash
sudo systemctl start lab-monitor-metrics.service
journalctl -u lab-monitor-metrics.service -n 30 --no-pager | grep -E 'CPU:|Sessions:|Storage health:|WARNING'
```

- **Pass:** the `CPU:` line says `(avg ...s)` and `Users: N (coverage ..%)`; `Sessions:` shows `tcp
  ok` and `utmp ok` (or `logind ok`); `Storage health:` shows `md`, `sysfs` and `mounts` ok; no WARNING
  about `hidepid`, interval counters or NVML.
- **If it fails:** a drop-in that adds `ProtectProc=`, `ProcSubset=` or `PrivateUsers=` hides other users'
  processes: remove it (`systemctl cat lab-monitor-metrics.service` shows drop-ins). Nothing new needs
  root; SMART is not collected.

### 23. Per-user GPU: two users on one GPU, and on separate GPUs

Run a GPU job as each of two test users, first both on GPU 0, then one on GPU 0 and one on GPU 1
(`CUDA_VISIBLE_DEVICES`). After the next scheduled run (so the averages have a window):

```bash
dry metrics > ~/lm-check/metrics.json
show ~/lm-check/metrics.json system_metrics | less          # gpus[i].users, util_split
show ~/lm-check/metrics.json user_metrics | less            # gpu_* per user
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv
for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader); do ps -o pid=,user= -p $p; done
nvidia-smi pmon -c 1
sudo -u labmon nvidia-smi --query-compute-apps=pid,used_memory --format=csv    # what labmon sees
```

- **Compare:** each user's `vram_bytes` against the sum of their processes' `used_memory`; the
  `util_split` and each user's `util_percent` against `pmon`'s `sm` column; `gpus_held` per user.
- **Pass:** labmon sees every user's processes; VRAM per user matches (to the MiB per process); one user
  on a GPU gives `single_user` with that user at the GPU's `gpu_percent`; two users on one GPU give
  `process_samples` with shares summing to at most `gpu_percent` and in the order `pmon` shows;
  `gpus_held` is 1 per user per GPU used.
- **Cost *(spec)*:** under 0.2 s more than with per-user GPU off:
  ```bash
  OFF=/var/lib/lab-monitor/lm-gpu-off.json
  sudo install -m 600 -o labmon -g labmon /etc/lab-monitor/collector.json $OFF
  sudo -u labmon $PY /opt/lab-monitor/collector/config_tool.py --config $OFF set gpu_per_user off
  time dry metrics >/dev/null
  time sudo -u labmon $PY -s /opt/lab-monitor/collector/collector.py --config $OFF --mode metrics --dry-run >/dev/null 2>&1
  sudo rm -f $OFF
  ```
- **If it fails:** `"gpu_per_user": "off"` and report it.

### 24. Per-user CPU-seconds and coverage

As a test user, right after a scheduled run, for 10 minutes (two full collector windows):

```bash
# A: steady load on two cores
stress-ng --cpu 2 --timeout 600s
# B (afterwards, separately): many short-lived processes; bash's time adds up their CPU
time bash -c 'end=$((SECONDS+600)); while [ $SECONDS -lt $end ]; do sh -c "i=0; while [ \$i -lt 20000 ]; do i=\$((i+1)); done"; done'
```

Read that user's values for the runs in each window:

```bash
sudo grep '"user_metrics"' /var/lib/lab-monitor/$NAME/$(date -u +%Y-%m).jsonl | grep '"<test user>"' | tail -n 4
sudo grep '"system_metrics"' /var/lib/lab-monitor/$NAME/$(date -u +%Y-%m).jsonl | tail -n 4 | grep -o '"proc_cpu_coverage_percent": [0-9.]*'
```

- **Compare:** A: `cpu_cores` of the two runs. B: the sum of `cpu_seconds` of the runs covering the
  10 minutes against `user` + `sys` from `time`. Repeat B with `"user_cpu_count_children": false`.
- **Pass (proposed):** A: `cpu_cores` between 1.9 and 2.1, coverage at least 90 %. B: with children
  recovery on, the sum within 10 % of `time`'s figure (and not above it by more than that); with it off,
  lower and coverage lower.
- **If it fails:** over-counting with recovery on: `"user_cpu_count_children": false`. Wrong in A:
  `"perf_counters": false`, and report it.

### 25. Logins without `/run/utmp` (Ubuntu 25.10 or later)

```bash
ls -l /run/utmp            # "No such file" on these releases
# log in over SSH as a test user from another machine, then:
loginctl list-sessions
dry metrics > ~/lm-check/metrics.json && show ~/lm-check/metrics.json sessions
```

- **Pass:** `_sources` has `logind: ok`, and the SSH login is an `ssh` row with the user name (on
  releases with utmp: `utmp: ok` and the same row, compared with `who`).
- **If it fails:** nothing to turn off (SSH clients are still counted by IP); report it.

### 26. Storage health

```bash
dry health > ~/lm-check/health.json && show ~/lm-check/health.json storage_health
cat /proc/mdstat; lsblk -d -o NAME,MODEL,SERIAL,SIZE,ROTA,TRAN
sensors 2>/dev/null | grep -iE 'drivetemp|nvme|composite|temp1' ; sudo nvme smart-log /dev/nvme0 2>/dev/null | grep -i temperature
```

- **Compare:** arrays against `/proc/mdstat`; disks against `lsblk` (USB disks are listed but have no
  history keys); temperatures against `sensors` / `nvme smart-log`. `io_errors` above 0 on a healthy disk
  is normal (it also counts failed probe commands) and sets no status.
- **Pass:** every array and non-USB disk listed with the right state; temperatures within 2 °C;
  `overall_status` `ok` on a healthy server.
- **If it fails:** `storage_health.ignore` for one component, else `"storage_health": {"enabled":
  false}`.

Clean up: `rm -rf ~/lm-check`.

---

## Atlantis (Manager and Dashboard server)

On the Windows server, in an Anaconda PowerShell Prompt (Administrator), with the variables of
[UPGRADE.md Step 0](UPGRADE.md#step-0-back-up) set. The live `data_dir` is only read.

### 27. SQLite version

```powershell
conda activate lab-monitor
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

- **Pass:** 3.24.0 or later (rollups use UPSERT; the review-fix Manager needs it too).
- **If it fails:** don't install this release; update the environment's Python first.

### 28. OLD data through the new code (`verify_upgrade.py`)

The checker compares the OLD (`f086e7e`) Manager and Dashboard with the new ones on copies of
OLD-format data. Use the Step 0 copy of the data taken before the review-fix upgrade (`$Backup\data`),
and a separate checkout of this release (once it is on GitHub, which is the owner's decision):

```powershell
git -C $Repo fetch origin
git -C $Repo worktree add E:\Users\lab-monitor\scripts\lab-monitor-usage origin/feature/usage-metrics
cd E:\Users\lab-monitor\scripts\lab-monitor-usage
python tools\verify_upgrade.py --manager-config $Repo\manager\config.json --data-dir $Backup\data `
       --work-dir F:\verify-usage --with-dashboard --report F:\verify-usage\report.json
```

- **Compare:** `result`, `failed_checks` and every difference in the report (see
  [UPGRADE.md, "For the next Claude session"](UPGRADE.md#for-the-next-claude-session), steps 6–7).
- **Pass:** PASS. Expected intended differences: N17 ("All Available" charts from hourly rollups when
  there are about 83 days or more of data), N18 (units of per-user and storage-health history), N19
  (the new Dashboard sections), N20 (the new Dashboard hides users who logged off: a user row more than
  120 s older than its system's latest metrics; any other missing user row is still reported). N21 only
  if a usage-metrics collector already reported to the old Manager (a test device): its newer metrics
  keys, its decoded storage-breakdown rows and the `storage_breakdown_legacy` table. The run waits for
  the rollup backfill before comparing (`timings_seconds.rollup_backfill`).
- **If it fails:** don't install; reproduce with `--keep-work` and report.

### 29. First start on the CURRENT data

After the review-fix release runs in production, the real `data_dir` (`E:\Users\lab-monitor\manager-data`
after UPGRADE.md Step 3b) is in the CURRENT (`e5cd699`; `160da09` and `fb6498c` write the same databases) layout. Measure
the new Manager's first start on a copy of it:

This is the run of [UPGRADE.md, "Verifying CURRENT → usage-metrics"](UPGRADE.md#verifying-current--usage-metrics)
with `--keep-work` added, from the `lab-monitor-usage` worktree made there:

```powershell
$Current = (git -C $Repo rev-parse HEAD).Trim()             # the commit the server runs (e5cd699 or its merge)
python tools\verify_upgrade.py --manager-config $Repo\manager\config.json --old-ref $Current `
       --work-dir F:\verify-current --with-dashboard --keep-work --report F:\verify-current\report.json
Select-String -Path F:\verify-current\*\logs\new-manager.log -Pattern 'Backed up|backfill complete|migrated'
```

- **Compare:** the `OLD side: current (the review-fix release ...)` line; `result` and `failed_checks`;
  `timings_seconds.new_manager_first_start` in the report; the `Backed up` lines; the
  `backfill complete (... s)` line.
- **Pass:** the run is PASS (what that proves is listed under "PASS means" in UPGRADE.md; the `backups`
  check enforces exactly one new backup of `metrics.db` and one of each `data.db` that has a table (even
  one without rows), identical to the original); `backfill complete` present. Proposed: the first start
  (blocking) under 60 s, plus the time to copy the databases. Delete `F:\verify-current` afterwards (it
  holds full copies of the data).
- **If it fails** (first start far over a minute): plan the upgrade for a quiet hour and tell the owner
  how long the Manager will be down; there is no setting that shortens the migration.

### 30. Ingest with rollups on

A scratch Manager on an **empty** `data_dir`, fed one month of a collector's archive twice, once with
rollups and once without. Any collector's `YYYY-MM.jsonl` works; on atlantis the local collector's
archive is in that collector's own `data_dir` (`data_dir` in `collector\local\config.json`, by default
`E:\Users\lab-monitor\data`, not the Manager's `manager-data`; before UPGRADE.md Step 3b the two could be
one folder, [ATLANTIS_DUAL_ROLE.md](ATLANTIS_DUAL_ROLE.md)). Pick a free port (5099 below) and generate a
scratch token:

```powershell
$S = 'E:\lm-ingest'; New-Item -ItemType Directory "$S\on", "$S\off" -Force | Out-Null
$CollectorData = (Get-Content "$Repo\collector\local\config.json" -Raw | ConvertFrom-Json).data_dir
Copy-Item "$CollectorData\<the collector's name>\<YYYY-MM>.jsonl" "$S\month.jsonl"
$Tok = python -c "import secrets; print(secrets.token_hex(32))"
foreach ($m in 'on', 'off') {
  @{ host = '127.0.0.1'; port = 5099; data_dir = "$S\$m\data"; admin_tokens = @($Tok); read_tokens = @();
     collector_tokens = @{}; log_file = "$S\$m\manager.log"; rollups = @{ enabled = ($m -eq 'on') } } |
    ConvertTo-Json | Set-Content "$S\$m.json"
}
# In a second window: cd E:\Users\lab-monitor\scripts\lab-monitor-usage\manager; python manager.py --config E:\lm-ingest\on.json
Measure-Command { python tools\replay_archives.py --manager-url http://127.0.0.1:5099 --token $Tok `
                  --source "$S\month.jsonl" --include-legacy }
# Stop that Manager (Ctrl+C), start it with off.json, and run the same replay again
```

- **Compare:** the two times, divided by the number of messages `replay_archives.py` reports.
- **Pass (proposed):** rollups add less than 5 ms per message (0.6–0.9 ms was measured on a Linux NVMe
  box), so a batch of 100 stays far below the collectors' 30 s request timeout.
- **If it fails:** `"rollups": {"enabled": false}` in the Manager config: charts stay on raw rows,
  per-user totals come from raw rows, and the rows stored meanwhile are rolled up as a gap once rollups
  are enabled again. Report it.
- **Clean up:** stop the scratch Manager and delete `E:\lm-ingest` (it holds a copy of real data).

### 31. After the real upgrade

```powershell
cd $Repo\manager
python rollups.py --config config.json --status      # read-only; every table "complete": true once the backfill is done
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 200 | Select-String 'backfill|Rollup|SQLite'
```

- **Pass:** the backfill finishes (`complete: true` for every system and table) and the Dashboard shows
  the new sections once upgraded collectors report.

**And the snapshots** (larger now, with the rollup tables): after the next nightly run, or take one with
`POST /api/snapshot` (admin token) as in [UPGRADE.md, Step 4](UPGRADE.md#step-4-check-the-upgraded-server):

```powershell
$S = Invoke-RestMethod http://localhost:5000/api/snapshot -Headers $H           # read token
$S | Select-Object last_result, last_success, last_snapshot, last_bytes, last_seconds, last_failed, last_error
python $Repo\manager\snapshot.py verify $S.last_snapshot
```

- **Pass:** `last_result` `ok`, `last_success` after the upgrade, `last_bytes` about the size of the
  databases, `verify` prints `OK`, and no ingest errors in `manager.log` while it ran (`last_seconds` long).
- **If it fails:** `partial` names the database it could not copy (`last_failed`); `error` with "Not
  enough free space" means the NAS folder (or the local staging disk) needs room for one more copy of the
  larger databases.

---

## Recording results

One row per check and machine. Give the table to the owner directly; it is not for the repository.

| Machine | Platform | Check | Result | Numbers | Setting changed |
|---------|----------|-------|--------|---------|-----------------|
| *triton* | DSM 7.2 armv7 | 11 breakdown benchmark | pass / fail | off 1 … s, on 2 … s, off 2 … s | — / `storage_breakdown.enabled: false` |

**Settings at a glance** (what each failure turns off):

| Setting | Turns off | Checks |
|---------|-----------|--------|
| `"perf_counters": false` | Interval averages, per-user CPU-seconds, disk I/O (back to the 1-s CPU sample) | 4, 5, 12, 18, 24 |
| `"disk_io": false` / `disk_io_exclude` | Disk throughput, IOPS, latency, busiest disk / one device | 5, 6, 18, 19 |
| `"user_metrics": "off"` | Per-user messages | 1 |
| `"user_cpu_count_children": false` | CPU recovery from exited children (Linux, DSM) | 24 |
| `"gpu_per_user": "off"` | Per-user GPU attribution | 21, 23 |
| `"sessions": {"detail": "counts"}` / `{"enabled": false}` | The connected list / all of sessions | 1, 9, 17 |
| `storage_health.ignore` / `"storage_health": {"enabled": false}` | One component / all of storage health | 1, 7, 8, 13–16, 26 |
| `storage_health.timeout_seconds` | (raise it) the Windows PowerShell time limit | 14 |
| `"storage_breakdown": {"owners": "none"}` / `{"enabled": false}` | Owner names / the whole breakdown | 3, 11, 20 |
| Manager `"rollups": {"enabled": false}` | Rollups (charts and totals from raw rows) | 30 |
