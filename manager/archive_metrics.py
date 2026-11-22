#!/usr/bin/env python3
r"""
Archive Metrics — history retention job.

History is kept forever by default.  Retention only runs when the Manager
config sets "history_retention_days" (or --days is passed): rows older than
that many days are appended to monthly JSONL files and then deleted.

What is archived:
  - every per-system history table in data/<system>/data.db
    (system_metrics, folder_usage, user_metrics, not_specified, ...)
      → data/<system>/archive/<YYYY-MM>-<data_type>.jsonl
    (folder_usage and storage_breakdown runs as the read API returns them:
    one line per run, a storage_breakdown run with its nested folders)
  - user_metrics_history in the central metrics.db
      → data/<system>/archive/<YYYY-MM>-user_metrics_history.jsonl

Rows are deleted only after their archive lines have been written and
fsync'd.  The legacy "retention_days" key is NOT used here (it only ever
applied to the old usage.jsonl files).  folder_usage_legacy (the pre-upgrade
wide table) is left untouched.  A folder_usage / storage_breakdown table an
older Manager stored wide, whose one-time migration has not succeeded yet,
is skipped (WARNING) until it has.

Rollups (rollups.py) are kept forever and never archived.  A rolled-up
table (system_metrics, user_metrics, sessions, storage_health) is pruned
only once every row of it is rolled up: pending backfill ranges are worked
off first, and if that does not finish the table is skipped for this run
(WARNING); so is a table whose rollups have reached
rollups.max_series_per_table (fields / users beyond it are not rolled up, so
their raw rows are all there is of them).  Before pruning, the table's rollup cutoff frozen_before is set to
the retention cutoff, so a pruned message replayed later is stored again but
not rolled up twice.  The central user_metrics_history has no rollups and is
pruned without this guard.

The Manager runs this daily itself when history_retention_days is set; it
can also be run by hand or from Task Scheduler (safe while the Manager is
running — SQLite busy_timeout handles the locking):

Usage:
    python archive_metrics.py --config E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json
    python archive_metrics.py --config config.json --days 365 --dry-run
"""
import os
import sys
import json
import logging
import argparse
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

# Add this directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rollups
from metrics import MetricsDB
from data_store import TypedDataStore
from storage_util import MigrationBlockedError, format_timestamp, normalize_device_name

BATCH = 5000
# Runs per batch where one run is many rows (a storage_breakdown run is
# rebuilt with every folder and owner: up to 50,000 rows)
BATCH_BY_TYPE = {'storage_breakdown': 20}
# Longest a retention run works off pending rollups of one table before it
# skips pruning that table (retried at the next run)
ROLLUP_CATCHUP_SECONDS = 3600


def setup_logging(log_file: str = None):
    """Setup logging on the root logger so data_store/metrics logs are captured too."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logging.getLogger('archive_metrics')


def load_config(config_path: str) -> dict:
    """Load configuration from JSON"""
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: Config not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in config: {e}")
        sys.exit(1)


def _append_archive(data_dir: str, system_key: str, label: str, rows: List[dict]) -> int:
    """
    Append rows to data_dir/<system_key>/archive/<YYYY-MM>-<label>.jsonl,
    grouped by the month of each row's timestamp.  fsyncs before returning
    so the caller can delete the rows safely.
    """
    archive_dir = os.path.join(data_dir, normalize_device_name(system_key), 'archive')
    os.makedirs(archive_dir, exist_ok=True)

    months: Dict[str, List[dict]] = {}
    for row in rows:
        ts = str(row.get('timestamp') or '')
        months.setdefault(ts[:7] if len(ts) >= 7 else 'unknown', []).append(row)

    for year_month, entries in sorted(months.items()):
        path = os.path.join(archive_dir, f"{year_month}-{label}.jsonl")
        with open(path, 'a', encoding='utf-8') as f:
            for entry in entries:
                f.write(json.dumps(entry, default=str) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return len(rows)


def _rollups_ready_for_pruning(data_store: TypedDataStore, system_key: str, data_type: str,
                               cutoff: str, logger: logging.Logger, dry_run: bool) -> bool:
    """
    Before pruning a rolled-up table: finish its pending rollups (bounded
    by ROLLUP_CATCHUP_SECONDS), then freeze them at the cutoff.  False =
    skip pruning this table for this run (also while it is at
    max_series_per_table: what is beyond it exists only in raw rows).
    """
    if data_type not in rollups.REGISTRY:
        return True
    if not data_store.rows_older_than(system_key, data_type, cutoff, limit=1):
        return True             # nothing to prune
    if not data_store.rollups_complete(system_key, data_type):
        if dry_run:
            logger.info(f"  {system_key}/{data_type}: rollups still pending - a real run finishes "
                        f"them before pruning")
            return True
        logger.info(f"  {system_key}/{data_type}: completing pending rollups before pruning ...")
        try:
            data_store.run_rollup_backfill(system_key, [data_type],
                                           deadline=time.monotonic() + ROLLUP_CATCHUP_SECONDS)
        except Exception as e:
            logger.error(f"  {system_key}/{data_type}: rollup backfill failed: {e}", exc_info=True)
        if not data_store.rollups_complete(system_key, data_type):
            logger.warning(f"  {system_key}/{data_type}: skipped - its rollups are not complete yet "
                           f"(raw rows are pruned only once they are rolled up; retried next run)")
            return False
    if data_store.rollups_at_series_cap(system_key, data_type):
        logger.warning(f"  {system_key}/{data_type}: skipped - its rollups have reached "
                       f"rollups.max_series_per_table ({data_store.rollups.max_series}): fields / users "
                       f"beyond it are not rolled up, so its raw rows are kept. Raise the setting and run "
                       f"'rollups.py --rebuild --data-type {data_type}'")
        return False
    if not dry_run:
        data_store.freeze_rollups(system_key, data_type, cutoff)
    return True


def run_history_retention(data_store: TypedDataStore, metrics_db, data_dir: str,
                          days: int, logger: logging.Logger, dry_run: bool = False) -> dict:
    """
    Archive and delete history rows older than `days` days.
    metrics_db may be None (central user_metrics_history is then skipped).
    Returns {"cutoff", "archived": {label: n}, "dry_run"}.
    """
    cutoff = format_timestamp(datetime.now(timezone.utc) - timedelta(days=days))
    logger.info(f"History retention: archiving rows older than {cutoff} ({days} days)"
                + (" [dry run]" if dry_run else ""))
    archived: Dict[str, int] = {}

    # ---- per-system history tables -------------------------------------- #
    for system_key in data_store.list_systems():
        pending = data_store.upgrade_pending(system_key)
        if pending:
            logger.warning(f"  {system_key}: skipped - its database migration is held back "
                           f"(pre-upgrade backup failed: {pending})")
            continue
        for data_type in data_store.list_data_types(system_key):
            # A wide folder_usage / storage_breakdown table whose migration
            # failed: its rows can't be deleted, so they are not archived
            # either (each run would append them again)
            blocked = data_store.type_migration_pending(system_key, data_type, retry=not dry_run)
            if blocked:
                logger.warning(f"  {system_key}/{data_type}: skipped - {blocked}; pruned once it is migrated")
                continue
            if not _rollups_ready_for_pruning(data_store, system_key, data_type, cutoff, logger, dry_run):
                continue
            total = 0
            batch = BATCH_BY_TYPE.get(data_type, BATCH)
            try:
                while True:
                    rows = data_store.rows_older_than(system_key, data_type, cutoff, limit=batch)
                    if not rows:
                        break
                    if dry_run:
                        total += len(rows)
                        if len(rows) == batch:
                            logger.info(f"  {system_key}/{data_type}: at least {total} rows (dry run stops counting)")
                        break
                    _append_archive(data_dir, system_key, data_type, rows)
                    deleted = data_store.delete_rows(system_key, data_type, [r['id'] for r in rows])
                    total += deleted
                    if deleted == 0:
                        break
            except MigrationBlockedError as e:
                # Never stops the run: the other tables and systems are pruned
                logger.error(f"  {system_key}/{data_type}: stopped - {e}")
            if total:
                archived[f"{system_key}/{data_type}"] = total
                logger.info(f"  {system_key}/{data_type}: {'would archive' if dry_run else 'archived'} {total} rows")

    # ---- central per-user history ---------------------------------------- #
    if metrics_db is not None and metrics_db.upgrade_blocked:
        logger.warning("  user_metrics_history: skipped - the metrics.db migration is held back "
                       f"(pre-upgrade backup failed: {metrics_db.upgrade_blocked})")
    elif metrics_db is not None:
        total = 0
        while True:
            rows = metrics_db.users.history_older_than(cutoff, limit=BATCH)
            if not rows:
                break
            if dry_run:
                total += len(rows)
                break
            by_device: Dict[str, List[dict]] = {}
            for row in rows:
                by_device.setdefault(row['device_name'], []).append(row)
            for device, device_rows in by_device.items():
                _append_archive(data_dir, device, 'user_metrics_history', device_rows)
            deleted = metrics_db.users.delete_history_ids([r['id'] for r in rows])
            total += deleted
            if deleted == 0:
                break
        if total:
            archived['user_metrics_history'] = total
            logger.info(f"  user_metrics_history: {'would archive' if dry_run else 'archived'} {total} rows")

    if not archived:
        logger.info("History retention: nothing older than the cutoff")
    return {"cutoff": cutoff, "archived": archived, "dry_run": dry_run}


def main():
    parser = argparse.ArgumentParser(
        description='Archive history older than history_retention_days to JSONL, then delete it'
    )
    parser.add_argument('--config', help='Config file path')
    parser.add_argument('--days', type=int, help='Override history_retention_days')
    parser.add_argument('--dry-run', action='store_true', help='Only report what would be archived')

    args = parser.parse_args()

    # If no config provided, look for standard location
    if not args.config:
        args.config = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'config.json'
        )

    if not os.path.exists(args.config):
        print(f"ERROR: Config not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    # Setup logging
    log_file = os.path.join(
        config.get('data_dir', 'data'),
        '..',  # Go up to parent
        'logs',
        'archive-metrics.log'
    )
    logger = setup_logging(log_file)

    logger.info("=" * 60)
    logger.info("Archive Metrics Job Starting")
    logger.info("=" * 60)

    days = args.days if args.days is not None else config.get('history_retention_days')
    if not days:
        logger.info("History retention is disabled (history_retention_days is not set) — nothing to do")
        sys.exit(0)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        logger.error(f"history_retention_days must be a positive integer, got {days!r}")
        sys.exit(1)

    data_dir = config.get('data_dir', 'data')
    if not os.path.isdir(data_dir):
        logger.error(f"data_dir does not exist: {data_dir}")
        sys.exit(1)

    try:
        backup = config.get('backup_before_migration', True)
        settings = rollups.settings_from_config(config, logger)
        data_store = TypedDataStore(data_dir, backup_before_migration=backup,
                                    rollups=rollups.RollupEngine(settings) if settings['enabled'] else None)
        db_path = os.path.join(data_dir, 'metrics.db')
        metrics_db = MetricsDB(db_path, data_dir=data_dir, backup_before_migration=backup) \
            if os.path.exists(db_path) else None

        run_history_retention(data_store, metrics_db, data_dir, days, logger, dry_run=args.dry_run)

        data_store.close()
        if metrics_db is not None:
            metrics_db.close()
        logger.info("Archive job completed successfully")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Archive job crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
