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

## Development

- Python 3.8+
- Each service manages its own dependencies (see `requirements.txt`)
- Shared code in `shared/` package

## Deployment

See [scripts/](scripts/) for deployment helpers and [docs/](docs/) for architecture details.
