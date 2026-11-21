# Handoff: verify and roll out `fix/review-findings`

Written 2026-11-21 by the Claude session on the Linux development box. It is for the next session, on
the Windows server that runs the Manager, the Dashboard and a local collector, and has the real
databases. If you are on a different machine, find out which machine holds the Manager's `data_dir`
before doing anything else.

## What this branch is

`fix/review-findings` starts from `f086e7e`, the commit deployed today (`origin/main`). It adds a
reliability and security overhaul, plus a new Linux collector:

| Area | What changed |
|------|--------------|
| Collector | Locked, atomic `queue.json`; every message gets a `msg_id`; per-message ACK; batches of 100; `null` (not 0) for unmeasured values; iterative disk scanner |
| Manager | Duplicates detected by `msg_id` (and by natural key for old messages); narrow `folder_usage` storage (the old wide table is kept as `folder_usage_legacy`); scoped tokens (`collector_tokens`, `read_tokens`, `admin_tokens`; old `auth_tokens` still work); a `.pre-upgrade-<time>.bak` copy of each database before its first migration; thread-safe SQLite; waitress |
| Dashboard | Outage and degraded states, XSS escaping, optional login, caching |
| Tools | `verify_upgrade.py` (old vs new Manager on copies of your data), `rollback_upgrade.py`, `replay_archives.py`, SSH host-key pinning, queue-safe archive and reset |
| Docs | [UPGRADE.md](UPGRADE.md) is the procedure. Most other docs were brought up to date |
| Linux | `install-collector-linux.sh` + systemd units for Ubuntu/Debian servers, with NVIDIA GPU stats identical to Windows. Not relevant to this server |

`git log --oneline f086e7e..origin/fix/review-findings` lists the commits.

**Tests:** 595 passed, 1 skipped (`shellcheck` missing), on Ubuntu 24.04 with Python 3.12.
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
- `verify_upgrade.py` opening databases read-only on a UNC path (`file:////server/share/...`)
- The Manager and Dashboard under waitress on Windows; the single-instance lock in `data_dir\manager.lock`
- Task Scheduler behaviour, for example an S4U task reaching a UNC `data_dir`
- Windows NVIDIA GPU stats (WDDM and TCC driver models)

## Your job, in order

1. **Read** [UPGRADE.md](UPGRADE.md) in full, especially its last section, "For the next Claude
   session". Its rules apply to you. Then read [tools/README.md](../tools/README.md) sections 8 and 9.
2. **Write down the facts** listed in that section: hostname, checkout path, running commit,
   `data_dir`, sizes and free space, the scheduled tasks, whether the local collector shares the
   `data_dir`, and the Python environment. Do not print any config file: they hold tokens.
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
   check that the card shows CPU, RAM, network, GPUs and the folders. This is the first time the
   PowerShell scan, `msvcrt` locking and waitress run on Windows. Stop both servers afterwards and delete
   the scratch folders.
6. **Step 0 (backup), then Step 1 (`verify_upgrade.py` against the real data)**, exactly as UPGRADE.md
   says. A local collector writing into the same `data_dir` is the "dual role" case in Step 1c.
7. **Report to the owner** and wait. Give PASS or FAIL, every non-PASS item with its cause, the
   Windows test and smoke-test results, the measured first-start time, and the free space. Do not
   upgrade anything without the owner's go-ahead.
8. **With the go-ahead:**
   - Step 2: merging into `main` is the owner's decision.
   - Step 3: upgrade Dashboard and Manager together with the installer.
   - Step 4: check the result.
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

## Feature release (feature/usage-metrics)

A second release, built on top of `fix/review-findings` at `fb6498c`. It adds, without changing what the
review-fix release stores:

- **Collectors:** CPU %, iowait and disk I/O averaged over the whole interval from kernel counters;
  per-user CPU-seconds (also on armv7 NAS units without psutil); per-user GPU memory, busy time and GPUs
  held (Linux and Windows, TCC and WDDM); who is connected (`sessions`); RAID, disk and volume state
  (`storage_health`, nothing that needs root on a NAS, no SMART, no SNMP); file counts, cold data and
  storage per owner in the daily disk run (`storage_breakdown`; on Windows without owners).
- **Manager:** hourly and daily rollups kept forever with a background backfill, the new snapshot tables
  and per-user columns, eight new read endpoints, `resolution` / `entity` on `/api/history`.
- **Dashboard:** Disk I/O, per-user cores and GPU, "Last 24 h" totals, Connected, storage health and
  storage owners; min–max bands on long charts. It works with the OLD, CURRENT and new Managers.
- **Docs:** [PROTOCOL.md](PROTOCOL.md), [PRIVACY.md](PRIVACY.md), [FIELD_CHECKS.md](FIELD_CHECKS.md), and
  the "usage-metrics release" sections of [UPGRADE.md](UPGRADE.md#the-usage-metrics-release) and the
  component READMEs.

**It comes after the review-fix release:** only once this handoff's job is done (the review-fix release
verified against the real databases and deployed) does it get its turn, with the owner's go-ahead. Going
back from it to the review-fix release means starting the CURRENT code on the same `data_dir`, never
`rollback_upgrade.py` (UPGRADE.md explains why).

**[FIELD_CHECKS.md](FIELD_CHECKS.md) must be done on the real machines before the new defaults are
trusted.** The feature code was tested only with fakes on the Linux development box (no Synology,
Windows, PowerShell or GPU there), so each platform's checks gate that platform's collector upgrade; the
atlantis checks (27–30) gate the Manager. Owner decisions for this release are in the section above.

