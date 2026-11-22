# Lab Monitor

Distributed system monitoring for tracking disk usage, CPU, RAM, GPU, per-user activity and network
traffic across NAS systems and Windows servers.

> **Upgrading an installation that already holds data?** Start with
> [docs/UPGRADE.md](docs/UPGRADE.md): back up, verify against your real databases, then upgrade.

---

## What It Does

**Collectors** run on each system (Synology NAS, Windows server, Linux):
- Every 5 minutes: CPU %, RAM, uptime, network counters and bandwidth, GPUs → `system_metrics`, plus
  per-user CPU/RAM → `user_metrics`
- Daily at 02:00: folder sizes and volume capacity → `folder_usage`
- Write each message to a local archive (permanent) and a locked local queue
- POST the queue to the Manager in batches; remove each message once the Manager has acknowledged it

**Manager** runs centrally (Windows Server):
- Receives `POST /api/data/queue`, acknowledges every message individually and ignores duplicates
- Stores full history in per-system SQLite databases
- Keeps a central database with the device registry, latest snapshots and running totals
- Serves a REST API for the Dashboard and the tools

**Dashboard** is a web UI:
- Its server polls the Manager (every 30 seconds by default) with a read-only token
- Shows a card per system with current metrics, disk state and users, and history charts
- Shows when the Manager or a system is unreachable or stale instead of silently showing old data

---

## Architecture

```
Systems (Collectors)
  ├── Every 5 min:  CPU/RAM/network/GPU/users  →  system_metrics + user_metrics
  ├── Daily 02:00:  disk by folder and volume  →  folder_usage
  └── Archive + queue locally → POST batches → drop each message once acknowledged

Manager (Windows Server, port 5000)
  ├── POST /api/data/queue  (per-message ACK, de-duplication by msg_id)
  ├── Per-system history:  data/<name>/data.db
  └── Central summary:     metrics.db  (devices, snapshots, totals, per-user, dead letters)

Dashboard (Windows Server, port 5001)
  └── Server reads the Manager API (read token) → page renders cards and charts
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full detail.

---

## Quick Start

| Role | Start Here |
|------|-----------|
| Install everything | [INSTALLATION.md](INSTALLATION.md) |
| Upgrade an existing installation | [docs/UPGRADE.md](docs/UPGRADE.md) |
| Understand the system | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Manager API reference | [manager/README.md](manager/README.md) |
| Collector reference | [collector/README.md](collector/README.md) |
| Fleet tools | [tools/README.md](tools/README.md) |

**Install order:**
1. Manager + Dashboard on the Windows server → `install-manager-dashboard-taskscheduler.ps1`
2. Collector on each Synology NAS → `install-collector-synology.sh`
3. Collector on each Windows server → `install-collector-windows.ps1`
4. Collector on each Linux (Ubuntu/Debian) server → `install-collector-linux.sh`

---

## Features

- **Cross-platform collectors**: Synology NAS, Windows Server, Linux; pure-Python fallbacks when
  `psutil` is unavailable
- **Reliable delivery**: messages survive outages in a locked local queue; each message has an id,
  the Manager acknowledges each one and ignores re-sent duplicates
- **Permanent local archive**: every message is written to monthly JSONL before sending; never
  deleted, and can be replayed (`tools/replay_archives.py`)
- **Honest values**: anything that couldn't be measured is `null` and shown as `—`, never `0`
- **Typed storage**: one table per `data_type`; folder history stored one row per folder, so large
  shares fit; new fields are added automatically
- **Running totals**: lifetime network bytes, with reboots detected from uptime
- **Safe upgrades**: databases from older versions are backed up before their first migration;
  nothing is deleted; history is kept forever unless retention is configured
- **Scoped tokens**: admin, read (Dashboard) and per-device collector tokens; placeholder tokens
  stop the Manager from starting
- **Dashboard sign-in** (optional: one password, remembered per browser); CORS off by default
- **Fleet tools** over SSH with pinned host keys: update, pause, run, archive, reset, replay, restart,
  verify and roll back an upgrade

The Manager and the Dashboard serve plain HTTP. On an untrusted network, put them behind a TLS
reverse proxy (see [CONFIG.md](CONFIG.md#other-security-settings)).

---

## Repository

<https://github.com/MoffittLab/lab-monitor.git>

Tests: `python -m pytest` (see `pytest.ini`).

---

## Reference

- [INSTALLATION.md](INSTALLATION.md) — Installation scripts and setup
- [docs/UPGRADE.md](docs/UPGRADE.md) — Upgrading an existing installation, verification and rollback
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design and data flow
- [CONFIG.md](CONFIG.md) — Configuration and security
- [METRICS.md](METRICS.md) — Metrics collection and storage
- [manager/README.md](manager/README.md) — Manager API reference
- [collector/README.md](collector/README.md) — Collector reference
- [dashboard/README.md](dashboard/README.md) — Dashboard reference
- [tools/README.md](tools/README.md) — Fleet management tools
- [docs/REMOTE_NAS_STORAGE.md](docs/REMOTE_NAS_STORAGE.md) — Manager data on a network share (superseded: keep `data_dir` on a local disk and snapshot it to the NAS)
- [docs/ATLANTIS_DUAL_ROLE.md](docs/ATLANTIS_DUAL_ROLE.md) — Manager and collector on one server
