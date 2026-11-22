# Installation Guide

Four installation scripts set everything up. **Upgrading an installation that already has data?**
Follow [docs/UPGRADE.md](docs/UPGRADE.md) instead: it backs up and verifies your data first.

## Quick Start

**Installation order:**

1. **Manager + Dashboard** on the Windows server (atlantis): `install-manager-dashboard-taskscheduler.ps1`
2. **Collector** on each Synology NAS: `install-collector-synology.sh`
3. **Collector** on each Windows server: `install-collector-windows.ps1`
4. **Collector** on each Linux server: `install-collector-linux.sh`

Each collector needs its own token from the Manager config (`collector_tokens`), so install the
Manager first.

**Network:** the Manager (port 5000) and the Dashboard (port 5001) serve plain **HTTP**. Tokens and
the Dashboard password cross the network unencrypted. Keep them on a trusted network (the installer
opens both ports in Windows Firewall; restrict them to the collectors' and users' addresses if you
can), or put a TLS reverse proxy in front and use `https://` URLs (collectors verify the proxy's
certificate).

---

## 1. Manager + Dashboard (Windows Server)

**Prerequisites:**
- Windows Server 2019 or later, Administrator access
- Miniconda (https://docs.conda.io/projects/miniconda/en/latest/)
- Git

**Install:**

1. Open **Anaconda PowerShell Prompt** (or Anaconda Prompt) with *Run as Administrator*.
2. Get the installer and run it:
   ```powershell
   mkdir E:\Temp -Force; cd E:\Temp
   git clone https://github.com/MoffittLab/lab-monitor.git
   cd lab-monitor
   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
   .\install-manager-dashboard-taskscheduler.ps1 -CollectorNames "Triton,Atlas"
   ```
   `-CollectorNames` is optional: it generates one collector token per name and prints them. Names
   must match `^[A-Za-z0-9_.-]{1,64}$` and should be each collector's `name` (the Synology installer
   uses the NAS host name in lowercase, the Windows installer the computer name in lowercase).

**What it does** (every check comes before anything that changes a running service):
1. Creates `E:\Users\lab-monitor\manager-data` (the Manager's `data_dir` in a new config: a local folder
   used by nothing else), `logs` and `scripts`. `manager-data` is created, and left empty, even when an
   existing config keeps another `data_dir`; the local collector's `E:\Users\lab-monitor\data` is the
   collector installer's.
2. Checks conda and creates the `lab-monitor` environment (Python 3.11) if missing.
3. Clones the repository to `E:\Users\lab-monitor\scripts\lab-monitor`, or runs `git pull origin main`
   there. A failed pull stops the install (`-SkipGitPull` skips it).
4. Prepares `manager\config.json` with random admin and read tokens, or **keeps** an existing one.
5. Prepares `dashboard\config.json` with the Manager **read** token as `manager_token`, or keeps an
   existing one. If it creates the Dashboard config next to a kept Manager config that has no read
   token, it adds one to the Manager config. Nothing is written yet.
6. Checks `data_dir`, the log paths and an enabled `snapshot.dest_dir`, and chooses the task account
   (see [Network paths](#data_dir-on-a-network-share)). A mapped drive letter stops the install here,
   before anything is installed or written. For a kept config whose `data_dir` is on a share or is the
   local collector's folder it prints a `[NOTE]` with the recommended layout; it never moves anything
   ([manager/README.md, "Moving `data_dir` off the NAS"](manager/README.md#moving-data_dir-off-the-nas);
   during the upgrade from the previous Manager: [docs/UPGRADE.md, Step 3b](docs/UPGRADE.md#step-3-upgrade-the-manager-and-dashboard)).
7. Installs `requirements.txt` (Flask, waitress, requests, paramiko; all pinned).
8. Validates the Manager config it is about to use (`manager.py --check-config`; touches no
   database and no log file) and the Python packages. **If the Manager would refuse the config,
   the install stops here**, with any running Manager and Dashboard untouched and no config
   written (`-IgnoreConfigCheck` overrides this).
9. Writes the prepared configs (old files backed up as `config.json.bak-<date>`).
10. Stops a Manager/Dashboard started by an earlier install (its tasks and the processes in
    `scripts\lab-monitor\.pids\`; the time is appended to `logs\installer-stop-times.txt`). If a
    `manager.py` process is still running after that, it offers to stop it and otherwise stops
    the install.
11. Writes the launchers `E:\Users\lab-monitor\start-manager.bat` and `start-dashboard.bat`. Each
    runs its service, writes console output to `logs\manager-console.log` /
    `logs\dashboard-console.log`, and restarts it 10 s after it exits. The Manager launcher stops on
    exit code 2 (config refused), since retrying can't help, and waits 60 s on exit code 3
    (another Manager already runs on the `data_dir`).
12. Registers the tasks **"Lab Monitor Manager"** and **"Lab Monitor Dashboard"**: at startup, whether
    or not anyone is logged on, restart on failure, no time limit. Only then it removes the old
    "Lab Monitor Startup" task (if a registration fails, the old task is kept and started again).
13. Opens ports 5000 and 5001 in Windows Firewall, starts both tasks and checks them.
14. Optionally installs OpenSSH Server (for the fleet tools).

At the end it prints the config paths, whether the Dashboard login is enabled (and the commands to
enable it), and any generated collector tokens.

**Installer parameters:**

| Parameter | Meaning |
|-----------|---------|
| `-CollectorNames "A,B"` | Generate one collector token per device (new config only) |
| `-AdminToken`, `-ReadToken` | Use your own tokens (at least 16 characters, not a placeholder) instead of random ones |
| `-DashboardToken` | Same as `-ReadToken` |
| `-ManagerToken` | Deprecated: stored as a collector token allowed to write for **any** device (`"*"`) |
| `-ManagerPort`, `-DashboardPort` | Ports (default 5000 / 5001) |
| `-ForceNewConfig` | Regenerate the **tokens only** in both configs (old files backed up as `config.json.bak-<date>`). `data_dir`, log paths, ports, retention and the Dashboard login are kept. **All** existing tokens go, including `auth_tokens` and `collector_tokens` (it asks first): every collector gets HTTP 401 until it has a new token (`-CollectorNames` generates them). To fix one bad token, edit the config instead ([docs/UPGRADE.md](docs/UPGRADE.md#fix-a-refused-token-without-downtime)) |
| `-IgnoreConfigCheck` | Install even if the new Manager refuses the config (it then stays down until the config is fixed) |
| `-SnapshotDir \\server\share\...` | New config only: turn on the nightly snapshots of `manager-data` to that NAS folder (`snapshot.enabled`, `snapshot.dest_dir`; 03:30, 14 daily and 12 monthly kept). An existing config keeps its own `snapshot` section: edit it there ([manager/README.md](manager/README.md#snapshots-to-a-nas)). A UNC folder makes the tasks run with a stored password |
| `-TaskCredential (Get-Credential)` | Account for the tasks when `data_dir`, a log file or an enabled `snapshot.dest_dir` is on a network share |
| `-SkipGitPull` | Install the code that is already checked out |

Re-running the installer is safe: both configs and their tokens are kept unless you pass
`-ForceNewConfig`, and a config the Manager would refuse (placeholder or short tokens) stops the
installer before it stops or changes anything.

**After installation:**
- Manager: `http://<server>:5000` (collectors post here). Dashboard: `http://<server>:5001`.
- Both start at boot without a login and restart if they crash.
- Logs: `E:\Users\lab-monitor\logs\manager.log` (rotating, 10 MiB × 5), `dashboard.log`, and the
  `*-console.log` files (startup errors, exit codes).

**Verify:**
```powershell
Get-ScheduledTask -TaskName "Lab Monitor*" | Select-Object TaskName, State
Invoke-RestMethod http://localhost:5000/health
Invoke-RestMethod http://localhost:5001/health
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 20
Get-Content E:\Users\lab-monitor\logs\manager-console.log -Tail 20
```

**Dashboard login** (recommended when port 5001 is reachable from the network):
```powershell
conda activate lab-monitor
cd E:\Users\lab-monitor\scripts\lab-monitor\dashboard
python app.py --hash-password
# paste the printed "auth" block into dashboard\config.json, then:
Stop-ScheduledTask "Lab Monitor Dashboard"; Start-ScheduledTask "Lab Monitor Dashboard"
```

**Restart a service** (ending the task stops the launcher loop; the PID check makes sure the Python
process is gone too):
```powershell
$Repo = "E:\Users\lab-monitor\scripts\lab-monitor"
Stop-ScheduledTask -TaskName "Lab Monitor Manager"
$MPid = Get-Content "$Repo\.pids\manager.pid"
Get-CimInstance Win32_Process -Filter "ProcessId = $MPid" | Where-Object CommandLine -match 'manager\.py' |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName "Lab Monitor Manager"
```
For the Dashboard use "Lab Monitor Dashboard", `dashboard.pid` and `app\.py`. From another machine,
`tools/restart_manager_dashboard.py` does the same over SSH (see
[tools/RESTART_MANAGER.md](tools/RESTART_MANAGER.md)).

### data_dir on a network share

**Don't put `data_dir` itself on a share.** SQLite is not reliable there (`disk I/O error`, failed
transactions); keep it on a local disk and let the Manager copy it to the NAS each night
([snapshots](manager/README.md#snapshots-to-a-nas)). The Manager logs a `WARNING` at every start
while `data_dir` is on a share.

The tasks normally run as the installing user through **S4U** (no stored password). An S4U task has
**no network access**:
- If `data_dir`, a log file or an enabled `snapshot.dest_dir` (the usual case: snapshots go to the
  NAS) is a UNC path (`\\server\share\...`), the installer asks for that account's password (or
  takes `-TaskCredential`) and registers the tasks with a stored password. The account needs access
  to the share. Re-run the installer after the password changes, or the tasks stop starting.
- **Mapped drive letters (`Z:\...`) are not supported**: scheduled tasks can't see drive mappings.
  The installer stops before changing anything; use the UNC path instead.

See [docs/REMOTE_NAS_STORAGE.md](docs/REMOTE_NAS_STORAGE.md).

### Adding a collector later

1. Generate a token: `conda run -n lab-monitor python -c "import secrets; print(secrets.token_hex(32))"`
2. Add it to `manager\config.json` under `collector_tokens`, keyed by the collector's `name`:
   `"collector_tokens": {"Triton": "<token>", ...}`
3. Restart the Manager (above).
4. Use the same token as the collector's Manager token when you install it.

### Troubleshooting

- **The Manager doesn't start:** read `logs\manager-console.log`. Exit code 2 = config refused,
  usually a placeholder or short token; run
  `python manager.py --config config.json --check-config` in `scripts\lab-monitor\manager`. Exit
  code 1 with "Cannot open data_dir" = the folder or share isn't reachable (see above). Exit code 3
  = another Manager already runs on this `data_dir` (it holds `data_dir\manager.lock`); the
  launcher retries every 60 s.
- **`/health` doesn't answer right after an upgrade:** the first start backs up and migrates the
  existing databases before it listens. Follow `logs\manager.log` and wait
  ([docs/UPGRADE.md](docs/UPGRADE.md)).
- **Dashboard banner "Manager unreachable" with HTTP 401/403:** the Dashboard's `manager_token` is
  not one of the Manager's `read_tokens` (or legacy `auth_tokens`).
- **Log says "using the Flask DEVELOPMENT server":** waitress is missing:
  `conda run -n lab-monitor python -m pip install -r requirements.txt`.

---

## 2. Collector on Synology NAS

**Prerequisites:**
- SSH access to the NAS as an admin user (not root)
- Git (Package Center -> Git Server) and Python 3.8 or later (Package Center -> Python)
- The admin user can write to `/volume1`
- This device's collector token from the Manager config

**Install:**
```bash
ssh your-admin-user@nas.local
cd /tmp
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor
./install-collector-synology.sh
```

**What it does:**
- Creates `/volume1/lab-monitor/data`, `logs` and `scripts`, and clones (or `git pull`s) the repository
  to `/volume1/lab-monitor/scripts/lab-monitor`.
- Creates a virtual environment (`/volume1/lab-monitor/lab-monitor-env`), or falls back to a `--user`
  install when DSM's Python has no working `venv`.
- Installs `collector/requirements.txt`: `requests` is required; `psutil` is best-effort (there is no
  wheel for armv7l; the collector then reads `/proc`).
- Asks for the Manager URL (plain `http://atlantis.med.harvard.edu:5000`; `https://` only behind a
  TLS proxy), this device's **collector token**, the device type and the scan paths. The `name` is
  the NAS host name in lowercase.
- Writes `collector/local/config.json` with mode 600 (it holds the token). On a re-run it keeps the
  existing config and asks before restricting a more open one.
- Runs one metrics collection as a test, then prints the two DSM tasks to create.

**Schedule jobs** (Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined
script). Run both as the owner of `config.json` (the installing user) or root; with mode 600 no
other user can read it. The installer prints the exact interpreter path to use.

| Task | Schedule | Script |
|------|----------|--------|
| Lab Monitor - Disk Collection | Daily 02:00 | `cd /volume1/lab-monitor/scripts/lab-monitor/collector && /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json --mode disk` |
| Lab Monitor - Metrics Collection | Daily 00:00, repeat every 5 minutes | `cd /volume1/lab-monitor/scripts/lab-monitor/collector && /volume1/lab-monitor/lab-monitor-env/bin/python3 collector.py --config local/config.json --mode metrics` |

**Verify:**
```bash
tail -f /volume1/lab-monitor/logs/collector.log
ls -la /volume1/lab-monitor/data/<name>/        # YYYY-MM.jsonl archive; queue.json only while unsent
curl http://atlantis.med.harvard.edu:5000/health
```

**Edit the config later:** `nano /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json`

---

## 3. Collector on Windows Server

**Prerequisites:**
- Windows Server 2019 or later, Administrator access
- Miniconda and Git
- This server's collector token from the Manager config

**Install:**

1. Open **Anaconda PowerShell Prompt** with *Run as Administrator*.
2. Optionally create the environment first: `conda create -n lab-monitor python=3.11`
3. Run the installer:
   ```powershell
   mkdir E:\Temp -Force; cd E:\Temp
   git clone https://github.com/MoffittLab/lab-monitor.git
   cd lab-monitor
   Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
   .\install-collector-windows.ps1
   ```

**What it does:**
- Asks for the installation drive and creates `<drive>:\Users\lab-monitor` (`data`, `logs`,
  `scripts`), and clones or updates the repository there.
- Restricts the install folder: only SYSTEM and Administrators can write to it, and only they can
  read `collector\local\` (the config holds the token). On a re-run this resets the permissions of
  everything below the install folder.
- Installs `collector\requirements.txt`, plus `requirements-gpu.txt` (`nvidia-ml-py`) when an NVIDIA
  GPU is found.
- Asks for the Manager URL, this server's **collector token**, the device type, volumes and scan
  paths. The `name` is the computer name in lowercase. An existing config is kept.
- Writes wrapper batch files with the full path to `python.exe` and registers two tasks:
  "Lab Monitor - Disk Collection" (daily 02:00; time limit `total_timeout_seconds` + 1 hour, 4 hours
  by default) and "Lab Monitor - Metrics Collection" (every 5 minutes). They run as SYSTEM when
  Python is at a system-wide path, otherwise as your account (it asks for the password).

**Verify:**
```powershell
Get-ScheduledTask -TaskName "Lab Monitor*" | Select-Object TaskName, State
Get-Content E:\Users\lab-monitor\logs\collector.log -Tail 20 -Wait
```

**Edit the config later** (as Administrator):
`notepad E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`

On the Manager server itself (atlantis), the collector uses the same checkout but its own data
folder: the collector writes to `E:\Users\lab-monitor\data`, the Manager keeps its databases in
`E:\Users\lab-monitor\manager-data` and snapshots them to the NAS each night (installer option
`-SnapshotDir \\server\share\...`); see [docs/ATLANTIS_DUAL_ROLE.md](docs/ATLANTIS_DUAL_ROLE.md).

---

## 4. Collector on Linux Server (Ubuntu / Debian)

**Prerequisites:** Ubuntu 22.04 / 24.04 or Debian 12 with systemd, `sudo`, and network access to the Manager
(and to PyPI for the pinned packages; offline, the installer falls back to the distribution's
`python3-requests` / `python3-psutil`).

```bash
git clone https://github.com/MoffittLab/lab-monitor.git
cd lab-monitor
sudo ./install-collector-linux.sh            # asks for the Manager URL, token, name, folders
```

Non-interactive, generating this device's token:

```bash
sudo ./install-collector-linux.sh --non-interactive \
     --manager-url http://atlantis.med.harvard.edu:5000 --generate-token \
     --scan-paths '/home/*,/data/*'
```

`--generate-token` prints the exact `"collector_tokens": {"<name>": "<token>"}` entry to add to the Manager
config (then restart the Manager). `--dry-run` prints every action without changing anything;
`--print-config` prints the config it would write. `--help` lists all flags (`--name`, `--volumes`,
`--repo/--ref`, ...).

**What it does:** installs `python3-venv` if missing; creates the system user `labmon`; copies the collector to
`/opt/lab-monitor` and builds `/opt/lab-monitor/venv`; writes `/etc/lab-monitor/collector.json`
(`0640 root:labmon`); installs and enables `lab-monitor-metrics.timer` (every 5 minutes) and
`lab-monitor-disk.timer` (daily 02:00); runs one metrics collection and shows whether the Manager
acknowledged it. On a server with an NVIDIA driver it also installs nvidia-ml-py and checks that NVML works for
`labmon` ([GPU servers](collector/README.md#gpu-servers)). Re-running it upgrades code, venv and units and keeps the config (flags change only the keys
they name). Details: [collector/README.md](collector/README.md#linux--ubuntu).

**Network and file access:** the metrics service may only reach the Manager (the addresses `manager_url`
resolves to when the installer runs, and the name servers): re-run the installer if the Manager's address
changes. The disk service can read every folder it measures but has no network at all; the next metrics run
sends its results. Details: [collector/README.md](collector/README.md#linux--ubuntu) ("Sandbox").

**Shared storage:** folders on NFS/SMB or cluster filesystems (Lustre, GPFS, ...) are not scanned by
default, so a server whose `/home` is an NFS mount reports `/home` in the disk scan's errors instead of
walking the shared tree every night. Measure shared storage on its file server, or set
`"include_network_mounts": true` on one node.

**Verify (first-install checklist):**
```bash
systemctl list-timers 'lab-monitor-*'
journalctl -u lab-monitor-metrics.service -n 30
sudo systemctl start lab-monitor-metrics.service   # run once more now
```

On a GPU server, also:
```bash
# NVML works for the service user: prints the number of GPUs
sudo -u labmon /opt/lab-monitor/venv/bin/python -s -c "import pynvml; pynvml.nvmlInit(); print(pynvml.nvmlDeviceGetCount())"
# The service found them ("GPUs: N (...)"), and logged no NVML errors
journalctl -u lab-monitor-metrics.service --since '15 min ago' | grep -E 'GPUs:|NVML'
# Compare with the Dashboard card / the collector's values
nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv
```
Temperature should match within a degree or two and power within a few watts. Utilization is a sample, and
VRAM used can differ by the driver-reserved amount. Causes and fixes:
[collector/README.md](collector/README.md#gpu-servers).

**Fleet tools:** to update, run, pause or archive this collector from `tools/` like the others, add it to
`collectors.csv` with `platform` = `linux` and add the sudoers rule in
[tools/README.md](tools/README.md#linux-collectors-platform--linux).

**Uninstall:** `sudo /opt/lab-monitor/install-collector-linux.sh --uninstall` (keeps config, data and logs;
add `--purge` to delete them and the `labmon` user).

---

## Collector configuration

The installers create `collector/local/config.json`. The main fields:

```json
{
  "name":          "triton",
  "id":            "synology-triton",
  "device_type":   "NAS",
  "manager_url":   "http://atlantis.med.harvard.edu:5000",
  "manager_token": "<this device's collector token>",
  "volumes":       ["/volume1", "/volume2"],
  "scan_paths":    ["/volume1/*", "/volume2/Data"],
  "data_dir":      "/volume1/lab-monitor/data",
  "log_file":      "/volume1/lab-monitor/logs/collector.log"
}
```

- `name`: shown on the Dashboard; 1-64 characters from `A-Z a-z 0-9 _ . -`. It must match the
  device's key in the Manager's `collector_tokens` (compared case-insensitively).
- `manager_token`: this device's `collector_tokens` entry (a legacy `auth_tokens` token also works).
- `volumes`: drives/volumes whose capacity (used, free, total) is reported.
- `scan_paths`: folders measured for the usage breakdown. `"/volume1/*"` measures each subfolder of
  `/volume1`; `"/volume2/Data"` measures that folder as one total. Without `scan_paths`, each
  volume is scanned one level deep.

All fields, time budgets and the queue are described in [collector/README.md](collector/README.md).

---

## Troubleshooting

### Windows: "This script must be run from an Anaconda Prompt"
Start an Anaconda (PowerShell) Prompt with *Run as Administrator*, or run `conda init powershell`
and open a new PowerShell window.

### Synology: could not clone the repository
Install Git Server from Package Center.

### Collector: Manager connection failed
```bash
curl http://atlantis.med.harvard.edu:5000/health
cat /volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json
```
Check `manager_url`, and that the Manager's firewall port is open.

### Collector: HTTP 401 or 403
- **401:** `manager_token` matches no Manager token (check for spaces).
- **403:** the token belongs to another device: its `collector_tokens` key must be this collector's
  `name`. A read token can't send data either.

### Collector: HTTP 400 "Invalid device name"
`name` must match `^[A-Za-z0-9_.-]{1,64}$` (no spaces). Names the previous Manager already
registered keep working.

---

## Next steps

1. **Watch the first day:** metrics every 5 minutes, the disk run at 02:00, no errors in the logs,
   every system on the Dashboard.
2. **Data growth:** history is kept forever by default (`history_retention_days: null`). To archive
   and prune old rows, set `history_retention_days` in `manager\config.json` (see
   [manager/README.md](manager/README.md#retention)). Collector archives (`YYYY-MM.jsonl`) are never
   deleted by the collector.
3. **Fleet tools:** [tools/README.md](tools/README.md) (update, pause, run, archive, replay, restart).

---

## See Also

- [README.md](README.md) — Project overview
- [docs/UPGRADE.md](docs/UPGRADE.md) — Upgrading an existing installation
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design
- [CONFIG.md](CONFIG.md) — Configuration and security
- [METRICS.md](METRICS.md) — Metrics reference
- [manager/README.md](manager/README.md) — Manager API reference
- [collector/README.md](collector/README.md) — Collector reference
- [dashboard/README.md](dashboard/README.md) — Dashboard reference
