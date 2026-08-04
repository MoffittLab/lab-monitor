#!/usr/bin/env python3
r"""
Archive Metrics - Monthly Archival Job

Runs once per month (1st at 2 AM via Windows Task Scheduler).

Moves metrics older than 90 days from SQLite to monthly JSONL files for archival.
Keeps recent metrics (last 90 days) in SQLite for fast queries.

Usage:
    python archive_metrics.py --config E:\Users\lab-monitor\scripts\lab-monitor\manager\config.json

Or from Task Scheduler:
    python.exe E:\Users\lab-monitor\scripts\lab-monitor\manager\archive_metrics.py
"""
import os
import sys
import json
import logging
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(__file__))

from metrics import MetricsDB


def setup_logging(log_file: str = None):
    """Setup logging"""
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


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


def archive_old_metrics(db: MetricsDB, data_dir: str, logger: logging.Logger, days: int = 90):
    """
    Archive metrics older than N days from SQLite to monthly JSONL files.
    
    Args:
        db: MetricsDB instance
        data_dir: Base data directory
        logger: Logger instance
        days: Retention days (default 90)
    """
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    logger.info(f"Starting archival of metrics older than {cutoff_date}")
    
    try:
        # Get all systems with old data
        cursor = db.db.execute('''
            SELECT DISTINCT name FROM metrics
            WHERE timestamp < ?
            ORDER BY name
        ''', (cutoff_date,))
        
        systems = [row[0] for row in cursor.fetchall()]
        
        if not systems:
            logger.info("No metrics older than retention period to archive")
            return True
        
        logger.info(f"Found {len(systems)} systems with old data")
        
        # Archive each system separately, grouped by month
        total_archived = 0
        
        for name in systems:
            # Get old metrics for this system, grouped by month
            cursor = db.db.execute('''
                SELECT timestamp, cpu_percent, ram_percent, network_bytes_in, network_bytes_out
                FROM metrics
                WHERE name = ? AND timestamp < ?
                ORDER BY timestamp
            ''', (name, cutoff_date))
            
            metrics = cursor.fetchall()
            
            if not metrics:
                continue
            
            # Group by month
            months = {}
            for row in metrics:
                timestamp = row[0]  # ISO format: YYYY-MM-DDTHH:MM:SSZ
                year_month = timestamp[:7]  # Extract YYYY-MM
                
                if year_month not in months:
                    months[year_month] = []
                
                months[year_month].append({
                    'timestamp': timestamp,
                    'cpu_percent': row[1],
                    'ram_percent': row[2],
                    'network_bytes_in': row[3],
                    'network_bytes_out': row[4]
                })
            
            # Write to JSONL monthly files
            system_dir = os.path.join(data_dir, name)
            os.makedirs(system_dir, exist_ok=True)
            
            for year_month, entries in sorted(months.items()):
                archive_file = os.path.join(system_dir, f"{year_month}-metrics.jsonl")
                
                try:
                    with open(archive_file, 'a') as f:
                        for entry in entries:
                            f.write(json.dumps(entry) + '\n')
                    
                    logger.info(f"Archived {len(entries)} metrics to {archive_file}")
                    total_archived += len(entries)
                    
                except Exception as e:
                    logger.error(f"Failed to write {archive_file}: {e}")
                    return False
        
        # Delete archived metrics from SQLite
        try:
            cursor = db.db.execute(
                'SELECT COUNT(*) FROM metrics WHERE timestamp < ?',
                (cutoff_date,)
            )
            old_count = cursor.fetchone()[0]
            
            db.db.execute(
                'DELETE FROM metrics WHERE timestamp < ?',
                (cutoff_date,)
            )
            db.db.commit()
            logger.info(f"Deleted {old_count} old metrics from SQLite")
        except Exception as e:
            logger.error(f"Failed to delete old metrics: {e}")
            return False
        
        logger.info(f"Archival complete: {total_archived} metrics archived, retention window reset")
        return True
    
    except Exception as e:
        logger.error(f"Archival failed: {e}", exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Archive old metrics from SQLite to JSONL'
    )
    parser.add_argument('--config', help='Config file path')
    
    args = parser.parse_args()
    
    # If no config provided, look for standard location
    if not args.config:
        args.config = os.path.join(
            os.path.dirname(__file__),
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
    
    # Initialize metrics database
    data_dir = config.get('data_dir', 'data')
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, 'metrics.db')
    
    try:
        db = MetricsDB(db_path)
        
        # Archive metrics older than retention period
        retention_days = config.get('retention_days', 90)
        success = archive_old_metrics(db, data_dir, logger, days=retention_days)
        
        db.close()
        
        if success:
            logger.info("Archive job completed successfully")
            sys.exit(0)
        else:
            logger.error("Archive job failed")
            sys.exit(1)
    
    except Exception as e:
        logger.error(f"Archive job crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
