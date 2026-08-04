# Lab Monitor

Distributed system monitoring platform for tracking disk usage, CPU, RAM, and network traffic across NAS systems and Windows servers.

---

## What It Does

**Collectors** run on each system (Synology NAS, Windows server):
- Every 5 minutes: measure CPU%, RAM%, uptime, network bandwidth → `system_metrics` message
- Daily at 2 AM: measure disk usage by folder and volume → `folder_usage` message
- Write each message to a local archive (permanent) and a local queue
- POST the queue to the Manager; delete it only after a verified acknowledgment

**Manager** runs centrally (Windows Server):
- Receives messages from all collectors via `POST /api/data/queue`
- Stores full history in per-system SQLite databases, one table per data type
- Maintains a fast-access central database: device registry, latest snapshots, running totals
- Serves a REST API for Dashboard queries

**Dashboard** is a web UI:
- Polls the Manager every 30 seconds
- Shows a card per system with current metrics and disk state
- Summary header shows total disk usage and lifetime network traffic across all systems

---

## Architecture

```
Systems (Collectors)
  ├── Every 5 min:  measure CPU/RAM/network  →  system_metrics message
  ├── Daily 2 AM:   measure disk by folder   →  folder_usage message
  └── Queue locally → POST to Manager → delete on ACK

Manager (Windows Server)
  ├── Receives POST /api/data/queue
  ├── Per-system history:  data/<name>/data.db  (one table per data_type)
  └── Central summary:     metrics.db  (devices, snapshots, running totals)

Dashboard (Web UI)
  └── Polls Manager REST API → renders cards
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full detail.

---

## Quick Start

| Role | Start Here |
|------|-----------|
| Install everything | [INSTALLATION.md](INSTALLATION.md) |
| Understand the system | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Manager API reference | [manager/README.md](manager/README.md) |
| Collector reference | [collector/README.md](collector/README.md) |

**Install order:**
1. Manager + Dashboard on Windows Server → `install-manager-dashboard-taskscheduler.ps1`
2. Collector on each Synology NAS → `install-collector-synology.sh`
3. Collector on each Windows server → `install-collector-windows.ps1`

---

## Features

- **Cross-platform collectors** — Synology NAS, Windows Server, Linux
- **Reliable delivery** — local queue survives network outages; queue deleted only after verified ACK
- **Permanent local archive** — every message written to JSONL before sending; never deleted
- **Typed storage** — each `data_type` gets its own table; schema evolves automatically as new fields appear
- **Running totals** — lifetime network bytes accumulated across reboots using delta tracking
- **Fast dashboard queries** — central summary DB always has latest state; full history in per-system DB
- **Bearer token auth** — all Manager API endpoints require authentication
- **CORS security** — configurable origin whitelist

---

## Repository

<https://github.com/MoffittLab/lab-monitor.git>

---

## Reference

- [INSTALLATION.md](INSTALLATION.md) — Installation scripts and setup
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design and data flow
- [CONFIG.md](CONFIG.md) — Configuration management and security
- [METRICS.md](METRICS.md) — Metrics collection details
- [manager/README.md](manager/README.md) — Manager API reference
- [collector/README.md](collector/README.md) — Collector reference
