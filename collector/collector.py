#!/usr/bin/env python3
"""
Lab Monitor Collector

Runs on each NAS/Windows system. Daily:
1. Measures disk usage of configured folders
2. Enqueues locally
3. Attempts to POST to Manager
4. Archives queue to monthly files on success

Usage:
    python3 collector.py --config config.json
"""
import sys
import os
import json
import logging
import argparse
import socket
import subprocess
from datetime import datetime

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

from disk_usage import measure_all_folders, discover_folders, discover_volumes
from queue_manager import enqueue_report, read_queue, clear_queue, archive_queue
from reporter import flush_queue, is_manager_reachable


def setup_logging(log_file: str = None, log_level: str = "INFO"):
    """Configure logging to console and file"""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level))
    
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Console
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    # File
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def load_config(config_path: str) -> dict:
    """Load configuration from JSON file"""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in config: {e}")
        sys.exit(1)


def get_git_version() -> tuple:
    """
    Get collector version tag and commit hash from git.
    Returns: (tag, commit) or (None, None) if not in git repo
    """
    try:
        # Get current git tag (or closest tag)
        tag = subprocess.check_output(
            ['git', 'describe', '--tags', '--always'],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL
        ).decode().strip()
        
        # Get short commit hash
        commit = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL
        ).decode().strip()
        
        return tag, commit
    except Exception:
        return None, None


def build_report(nas_name: str, nas_id: str, folders_usage: dict, execution_time: float = None) -> dict:
    """
    Build a usage report dict with summary statistics.
    
    Args:
        nas_name: Human-readable NAS name
        nas_id: Unique identifier
        folders_usage: Dict of {path: size_bytes}
        execution_time: Total execution time in seconds (optional)
    
    Returns: Report dict with folder details and summary stats
    """
    # Calculate volume totals
    volume_totals = {}
    total_usage = 0
    
    for path, size in folders_usage.items():
        # Extract volume from path (e.g., '/volume1' from '/volume1/shared')
        parts = path.split('/')
        if len(parts) >= 2 and parts[1]:  # Handle paths like '/volume1/shared'
            volume = '/' + parts[1]  # e.g., '/volume1'
            volume_totals[volume] = volume_totals.get(volume, 0) + size
        
        total_usage += size
    
    report = {
        "nas_name": nas_name,
        "nas_id": nas_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "folders": [
            {"path": path, "usage_bytes": size}
            for path, size in folders_usage.items()
        ],
        "volume_totals": volume_totals,
        "total_usage_bytes": total_usage
    }
    
    # Add execution time if provided
    if execution_time is not None:
        report["execution_time_seconds"] = round(execution_time, 2)
    
    # Add collector version info if available
    tag, commit = get_git_version()
    if tag:
        report["collector_version"] = tag
    if commit:
        report["collector_commit"] = commit
    
    return report


def main():
    # Track execution time
    execution_start = time.time()
    
    # Parse arguments
    parser = argparse.ArgumentParser(description="Lab Monitor Collector")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (default: config.json)"
    )
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Setup logging
    logger = setup_logging(
        config.get("log_file"),
        config.get("log_level", "INFO")
    )
    
    logger.info("=" * 60)
    logger.info("Lab Monitor Collector starting")
    logger.info("=" * 60)
    
    # Extract config (with autodiscovery)
    # If nas_name not in config, use system hostname
    nas_name = config.get("nas_name")
    if not nas_name:
        nas_name = socket.gethostname()
        logger.info(f"Auto-discovered NAS name from hostname: {nas_name}")
    
    # If nas_id not in config, use nas_name
    nas_id = config.get("nas_id", nas_name)
    volumes = config.get("volumes")
    exclude_folders = config.get("exclude_folders", [])
    manager_url = config.get("manager_url")
    manager_token = config.get("manager_token")
    queue_path = config.get("queue_path", "/volume1/lab-monitor/data/queue.jsonl")
    archive_dir = config.get("archive_dir")  # If None, archive_queue will use queue parent + /archive
    timeout_seconds = config.get("timeout_seconds", 3600)  # Default: 1 hour per folder
    retry_attempts = config.get("retry_attempts", 3)
    retry_delay = config.get("retry_delay_seconds", 10)
    
    # Validation (nas_name should now always be set via config or autodiscovery)
    if not nas_name or not isinstance(nas_name, str) or not nas_name.strip():
        logger.error("Failed to determine NAS name (config or hostname)")
        return False
    if not manager_url or not manager_token:
        logger.error("Config missing: manager_url or manager_token")
        return False
    
    # Auto-discover volumes if not in config
    if not volumes:
        logger.info("Volumes not configured, auto-discovering...")
        volumes = discover_volumes()
        if not volumes:
            logger.error("Failed to auto-discover volumes and none provided in config")
            return False
    
    logger.info(f"NAS: {nas_name} ({nas_id})")
    logger.info(f"Scanning volumes: {volumes}")
    if exclude_folders:
        logger.info(f"Excluding: {exclude_folders}")
    
    # Step 1: Discover and measure folders
    logger.info("Step 1: Discovering and measuring folder sizes...")
    folders_usage = discover_folders(volumes, exclude=exclude_folders, timeout=timeout_seconds)
    
    if not folders_usage:
        logger.warning("No folders measured successfully")
        return False
    
    logger.info(f"Measured {len(folders_usage)} folder(s)")
    
    # Step 2: Build and enqueue report
    logger.info("Step 2: Enqueuing report...")
    execution_time = time.time() - execution_start
    report = build_report(nas_name, nas_id, folders_usage, execution_time=execution_time)
    
    if not enqueue_report(report, queue_path):
        logger.error("Failed to enqueue report")
        return False
    
    # Step 3: Check if Manager is reachable
    logger.info("Step 3: Checking Manager availability...")
    manager_timeout = config.get("manager_timeout_seconds", 5)
    
    if not is_manager_reachable(manager_url, timeout=manager_timeout):
        logger.warning(f"Manager not reachable at {manager_url}")
        logger.warning("Report queued locally, will retry next run")
        logger.info("Collector exiting (normal offline scenario)")
        return True
    
    # Step 4: Try to flush queue
    logger.info("Step 4: Flushing queue to Manager...")
    pending_reports = read_queue(queue_path)
    logger.info(f"Queue has {len(pending_reports)} pending report(s)")
    
    if not pending_reports:
        logger.info("No reports to flush")
        return True
    
    success = flush_queue(
        pending_reports,
        manager_url,
        manager_token,
        retry_attempts=retry_attempts,
        retry_delay=retry_delay
    )
    
    if success:
        # Step 5: Archive queue on success
        logger.info("Step 5: Archiving queue...")
        if archive_queue(queue_path, archive_dir):
            logger.info("Collector completed successfully")
            return True
        else:
            logger.error("Failed to archive queue after successful flush")
            return False
    else:
        logger.warning("Failed to flush queue, keeping reports for next run")
        logger.info("Collector exiting (will retry next run)")
        return True  # Still exit cleanly, retry tomorrow


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
