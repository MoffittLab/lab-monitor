# lab-monitor

Lab monitoring system. Collectors (Synology DSM, Windows, Linux) send metrics and disk usage to a
Flask + SQLite Manager, and a Flask + Plotly Dashboard reads the Manager. Architecture:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Current work:** see [docs/HANDOFF.md](docs/HANDOFF.md) before doing anything. The upgrade
procedure is [docs/UPGRADE.md](docs/UPGRADE.md).

## Data safety (owner's hard requirement)

The Manager's existing databases (`data_dir`: per-system `<name>/data.db` and the central
`metrics.db`) must stay fully readable across every change. Never write into a live `data_dir`,
delete or overwrite `.bak` / `folder_usage_legacy` / backup copies, or start a new Manager on live data
without the owner's explicit go-ahead. Schema changes add tables or columns only, after a
`storage_util.backup_database` copy.

## Working in this repo

- Tests: `python -m pytest -p no:cacheprovider` (config in `pytest.ini`; `tests/compat` runs the old
  `f086e7e` Manager through `git archive`, so git must be on PATH).
- Collector code must stay Python 3.8-compatible, stdlib-only, with psutil and nvidia-ml-py optional.
- The wire protocol and API are described in [docs/PROTOCOL.md](docs/PROTOCOL.md) (canonical),
  [METRICS.md](METRICS.md) and [manager/README.md](manager/README.md). Unmeasured values are `null`,
  never 0.
- The GitHub repo is public: never commit tokens, configs with tokens, logs, or real data.
- Ask before pushing; work on branches, never commit to `main` directly.
