# Lab Monitor

A lightweight distributed system for tracking file usage across Synology NAS systems and displaying real-time analytics.

## Architecture

**Three-service design:**
- **Collector** (runs on each NAS) → Daily disk usage snapshots
- **Manager** (central service) → Receives, stores, and serves usage data
- **Dashboard** (web interface) → Real-time visualization of usage trends

Data flow: NAS Collector → Manager API → Dashboard UI

## Quick Start

See service-specific README files:
- [Collector](collector/README.md) - Deploy to Synology systems
- [Manager](manager/README.md) - Central data service
- [Dashboard](dashboard/README.md) - Web interface

## Project Structure

```
collector/      # NAS-side collection script
manager/        # Central manager service
dashboard/      # Web dashboard
shared/         # Shared Python utilities (models, config, helpers)
scripts/        # Deployment & setup helpers
docs/           # Architecture & API documentation
```

## Configuration

Each service has a `config.example.json` file. Copy to `config.json` and customize for your environment.

### DNS Examples
- **NAS Collector**: `t1.hms.harvard.edu`
- **Windows Server Manager**: `a1.med.harvard.edu`

## Architecture

```
Collector (NAS)                Manager (Windows)              Dashboard (Web)
─────────────────              ──────────────────             ───────────────
E.g., triton1.hms...    ──→   E.g., atlantis.med...    ──→   Display data
Measure folders               Receive reports                Poll API (30s)
Queue locally                 Store JSONL files              Real-time UI
POST to Manager               Serve REST API
Delete on success
```

## Quick Start

**Windows Server (Manager + Dashboard):**
- See [docs/WINDOWS-DEPLOYMENT.md](docs/WINDOWS-DEPLOYMENT.md)

**Synology NAS (Collector):**
- See [collector/README.md](collector/README.md)

## Development

- Python 3.8+
- Each service manages its own dependencies (see `requirements.txt`)
- Shared code in `shared/` package

## Services

### Collector
- Runs on each NAS (Synology, Windows, etc.)
- Daily: Measures disk usage → Queues locally → Posts to Manager
- Offline-resilient: Keeps queue if Manager unavailable
- See: `collector/` and `collector/README.md`

### Manager
- Flask API server (Windows Server recommended)
- Receives reports from collectors (with auth token)
- Stores in append-only JSONL files
- Serves REST API for Dashboard
- See: `manager/` and `docs/WINDOWS-DEPLOYMENT.md`

### Dashboard
- Flask web interface
- Polls Manager every 30 seconds
- Displays real-time NAS usage
- Accessible from intranet
- See: `dashboard/` and `docs/WINDOWS-DEPLOYMENT.md`

## Deployment

- **Windows Server**: [docs/WINDOWS-DEPLOYMENT.md](docs/WINDOWS-DEPLOYMENT.md)
- **NAS Collectors**: [collector/README.md](collector/README.md)
- **Architecture**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
