"""
Local queue management for pending usage reports.
Append-only JSONL file of reports waiting to be posted to Manager.
"""
import os
import json
import logging
from typing import List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def ensure_queue_dir(queue_path: str) -> None:
    """Create queue directory if it doesn't exist"""
    queue_dir = os.path.dirname(queue_path)
    if queue_dir and not os.path.exists(queue_dir):
        os.makedirs(queue_dir, exist_ok=True)
        logger.info(f"Created queue directory: {queue_dir}")


def enqueue_report(report_dict: dict, queue_path: str) -> bool:
    """
    Append a usage report to the local queue.
    
    Args:
        report_dict: Report JSON dict
        queue_path: Path to queue.jsonl
    
    Returns: True if enqueued successfully
    """
    try:
        ensure_queue_dir(queue_path)
        
        # Append as single line JSON
        with open(queue_path, 'a') as f:
            f.write(json.dumps(report_dict) + '\n')
        
        logger.info(f"Enqueued report for {report_dict.get('nas_name')} "
                   f"({len(report_dict.get('folders', []))} folders)")
        return True
    
    except Exception as e:
        logger.error(f"Failed to enqueue report: {e}")
        return False


def read_queue(queue_path: str) -> List[dict]:
    """
    Read all pending reports from queue.
    
    Returns: List of report dicts
    """
    if not os.path.exists(queue_path):
        return []
    
    reports = []
    try:
        with open(queue_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        reports.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"Skipping malformed queue line: {e}")
    
    except Exception as e:
        logger.error(f"Error reading queue: {e}")
    
    return reports


def clear_queue(queue_path: str) -> bool:
    """
    Delete the queue file (after successful flush).
    
    Returns: True if cleared successfully
    """
    try:
        if os.path.exists(queue_path):
            os.remove(queue_path)
            logger.info("Queue cleared (all reports successfully posted)")
        return True
    
    except Exception as e:
        logger.error(f"Failed to clear queue: {e}")
        return False


def get_queue_size(queue_path: str) -> int:
    """
    Get number of pending reports in queue.
    
    Returns: Count of reports
    """
    return len(read_queue(queue_path))


def backup_queue(queue_path: str, backup_dir: str = None) -> Optional[str]:
    """
    Create a backup of queue before clearing (optional safety measure).
    
    Returns: Path to backup file, or None if no backup created
    """
    if not os.path.exists(queue_path):
        return None
    
    if backup_dir is None:
        backup_dir = os.path.dirname(queue_path)
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"queue_backup_{timestamp}.jsonl")
        
        with open(queue_path, 'r') as src:
            with open(backup_path, 'w') as dst:
                dst.write(src.read())
        
        logger.info(f"Queue backed up to {backup_path}")
        return backup_path
    
    except Exception as e:
        logger.error(f"Failed to backup queue: {e}")
        return None
