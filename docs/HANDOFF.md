# Handoff: verify and roll out `fix/review-findings`

Written 2026-11-21 by the Claude session on the Linux development box. It is for the next session, on
the Windows server that runs the Manager, the Dashboard and a local collector, and has the real
databases. If you are on a different machine, find out which machine holds the Manager's `data_dir`
before doing anything else.

## Status 2026-11-22 (Windows server session)

- **Running commit on the server is `1a2171b`**, not `f086e7e` (six commits older). Step 1 was run with
  `--old-ref 1a2171b`.
- **Test suite on Windows** (Windows Server 2019, Python 3.11): 544 passed, 125 skipped (Linux-only), after
  the fixes in `fix/windows-tests` (merged here). One real bug (hard links counted once per link by the
  Python scanner on Windows) and several tests that assumed POSIX or hung (see that branch's commits).
- **Smoke test (step 5 below)** passed on scratch data: metrics, GPU, PowerShell folder scan, Dashboard,
  one snapshot (`ok`, `snapshot.py verify` OK). The snapshot went to a local folder, not yet to a NAS share.
- **Step 1 against copies of the real databases**: every check PASS except `dashboard` (2 differences, one
  system's current network counters). Cause: the old Manager's save of that system's latest sample failed
  half-way (row and `device_totals` written, `device_snapshot` not), so the old card mixed two samples and
  the new one shows the consistent, one-sample-older message. Clears with the first new sample. First start
  95.5 s for 2.8 GB. The owner decided to go ahead.
- Not done from that session (the lab's rules there allow no admin work, NAS writes or SSH): Step 0, Step 3,
  Step 4, Step 5. The owner runs them.

## What this branch is

`fix/review-findings` starts from `f086e7e`, the commit deployed today (`origin/main`). It adds a
reliability and security overhaul, plus a new Linux collector:

| Area | What changed |
|------|--------------|
| Collector | Locked, atomic `queue.json`; every message gets a `msg_id`; per-message ACK; batches of 100; `null` (not 0) for unmeasured values; iterative disk scanner |
| Manager | Duplicates detected by `msg_id` (and by natural key for old messages); narrow `folder_usage` storage (the old wide table is kept as `folder_usage_legacy`); scoped tokens (`collector_tokens`, `read_tokens`, `admin_tokens`; old `auth_tokens` still work); a `.pre-upgrade-<time>.bak` copy of each database before its first migration; thread-safe SQLite; waitress |
| Storage layout | The databases in a local folder of their own (`E:\Users\lab-monitor\manager-data`, not the collector's `data` and not a share), and nightly snapshots of every database to a NAS folder (`snapshot` in the Manager config; consistent, verified copies; a damaged database makes the run `partial` instead of stopping it). Startup warnings when the layout differs. Nothing is moved automatically: UPGRADE.md Step 3b moves it by hand before the installer |
| Dashboard | Outage and degraded states, XSS escaping, optional login, caching |
| Tools | `verify_upgrade.py` (old vs new Manager on copies of your data), `rollback_upgrade.py`, `replay_archives.py`, SSH host-key pinning, queue-safe archive and reset |
| Docs | [UPGRADE.md](UPGRADE.md) is the procedure. Most other docs were brought up to date |
| Linux | `install-collector-linux.sh` + systemd units for Ubuntu/Debian servers, with NVIDIA GPU stats identical to Windows. Not relevant to this server |

`git log --oneline f086e7e..origin/fix/review-findings` lists the commits.

**Tests:** 667 passed, 1 skipped (`shellcheck` missing), on Ubuntu 24.04 with Python 3.12.
**The suite has never run on Windows.**

**Not on GitHub:** a second branch, `feature/usage-metrics` (per-user GPU and CPU, who is connected,
storage health, rollups, storage owners). It is still being built on the development box. Do not start
or recreate it here.

## The owner's hard requirement

The data already in the Manager's databases must stay fully usable: device history, folder history,
per-user history, lifetime network totals and the device list. Never write into the live `data_dir`,
delete or overwrite backups, or start the new Manager on live data without the owner's explicit
go-ahead.

## Never executed: the Windows-only parts

Everything below was checked by reading only, because the development box has no Windows or
PowerShell. These are the most likely places for a bug, so test them here first, on scratch data:

- `install-manager-dashboard-taskscheduler.ps1` and `install-collector-windows.ps1`
- The collector's PowerShell folder scan (`collector/lib/disk_usage.py`, path passed in `$env:LM_SCAN_PATH`)
- Queue locking with `msvcrt` (`collector/lib/local_queue.py`) and file replacement on Windows
- The Windows script that `tools/archive_all_collectors.py` sends (`FileStream.Lock`)
- SQLite opening a database by URI on a UNC path (`file:////server/share/...`, an empty URI
  authority): `verify_upgrade.py`, `rollback_upgrade.py`, `snapshot.py verify` and a snapshot's
  staging, the Manager's backup check. Tested here only by emulating Windows' path rules
  (`tests/manager/test_manager_unc_paths.py`)
- The Manager and Dashboard under waitress on Windows; the single-instance lock in `data_dir\manager.lock`
- Task Scheduler behaviour, for example an S4U task reaching a UNC `data_dir`
- Windows NVIDIA GPU stats (WDDM and TCC driver models)
- Nightly snapshots on Windows: the installer's `-SnapshotDir`, a UNC `snapshot.dest_dir` reached by a
  stored-password task, the copy to and the `.partial` → `snapshot-<stamp>` rename on an SMB share, pruning
  (deleting folders) there, two Managers claiming a `.partial` folder at once there (`os.mkdir`), and
  `snapshot.py verify` on a UNC path
- `is_network_path` on Windows (`GetDriveTypeW` for mapped drives) behind the startup layout warnings
- The PowerShell and robocopy commands of UPGRADE.md Step 3b (moving the databases to `manager-data`,
  editing `config.json` with `ConvertTo-Json`)

## Your job, in order

1. **Read** [UPGRADE.md](UPGRADE.md) in full, especially its last section, "For the next Claude
   session". Its rules apply to you. Then read [tools/README.md](../tools/README.md) sections 8 and 9.
2. **Write down the facts** listed in that section: hostname, checkout path, running commit,
   `data_dir` (local, a share, or shared with the local collector), sizes and free space (also on the
   drive that will hold `manager-data`), the scheduled tasks, the NAS folder the owner wants for the
   snapshots (`snapshot.dest_dir`), and the Python environment. Do not print any config file: they
   hold tokens.
3. **Get the new code into a separate folder** (UPGRADE.md Step 1a). Never change the production
   checkout's branch.
4. **Run the test suite on Windows** from that folder, in a **separate** Python environment so the
   Manager's environment stays untouched:
   ```powershell
   conda create -n lab-monitor-test python=<same version as the Manager's env> -y
   conda activate lab-monitor-test
   pip install -r requirements.txt -r collector\requirements.txt pytest
   python -m pytest -p no:cacheprovider
   ```
   Some tests are POSIX-only and skip themselves. Sort each failure into one of three kinds: a test
   that assumes POSIX but doesn't skip; a real Windows bug; or an environment problem. Report them.
   Fix only with the owner's agreement, on a new branch (see "Git" below).
5. **Windows smoke test, scratch data only.** Start the new Manager from the new folder with a scratch
   config: `data_dir` in a new empty folder, `host` `127.0.0.1`, an unused port, and freshly generated
   tokens. Then run the new collector against it with a scratch config (`--mode metrics`, then
   `--mode disk` on a small test folder). Finally point the new Dashboard at it (a second unused port) and
   check that the card shows CPU, RAM, network, GPUs and the folders. With a `snapshot` section in that
   scratch config (`dest_dir` a scratch folder, on the NAS share if the owner agrees), take one snapshot
   (`POST /api/snapshot` with the admin token, then `GET /api/snapshot` until `running` is false and
   `last_result` is `ok`) and check it with `python manager\snapshot.py verify <its folder>`. This is the
   first time the PowerShell scan, `msvcrt` locking, waitress and the snapshots run on Windows. Stop both
   servers afterwards and delete the scratch folders.
6. **Step 0 (backup), then Step 1 (`verify_upgrade.py` against the real data)**, exactly as UPGRADE.md
   says. A local collector writing into the same `data_dir` is the "dual role" case in Step 1c: the
   layout before Step 3b, which moves the databases to a local `manager-data` folder. Note whether
   `data_dir` is on a share or shared with the collector, and which NAS folder the owner wants for the
   snapshots (`snapshot.dest_dir`).
7. **Report to the owner** and wait. Give PASS or FAIL, every non-PASS item with its cause, the
   Windows test and smoke-test results, the measured first-start time, and the free space. Do not
   upgrade anything without the owner's go-ahead.
8. **With the go-ahead:**
   - Step 2: merging into `main` is the owner's decision.
   - Step 3: move the databases to `manager-data` and turn on snapshots (3b), then upgrade Dashboard and
     Manager together with the installer (3c).
   - Step 4: check the result, including a first snapshot (`snapshot.py verify`).
   - Step 5: upgrade the collectors. Do the local one first, then one NAS, then the rest.
   - Watch the first day: metrics every 5 minutes, the 02:00 disk run, no errors in the logs.

## Rules

- **Tokens are secrets.** Don't print `manager\config.json`, `dashboard\config.json` or collector
  configs. The verifier's `config` check tells you whether the Manager accepts its tokens.
- **The GitHub repository is public.** Never commit or push anything that contains real data: usernames,
  folder names, hostnames, IP addresses, report files, logs, tokens. Reports go to the owner directly.
- **Git.** Ask before every push. Commit fixes on a new branch made from `fix/review-findings` (for
  example `fix/windows-<topic>`). Never force-push, and never commit to `main` directly.
- **Don't make a difference go away.** Never widen `verify_upgrade.py`'s `INTENDED` list or loosen a
  test to get a PASS. Find the cause.
- **Clean up only what you created.**

## Owner decisions already made (context for later work)

These are for the feature branch, not this one: per-user data is visible to everyone with Dashboard
access; the Synology NAS units get nothing that needs root (no SMART, no SNMP); Windows storage
breakdown is cold data and file counts only; session lists are kept in history; days use UTC
boundaries.
