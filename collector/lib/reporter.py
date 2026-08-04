"""
API client for posting reports to Manager.
Handles authentication, retries, and handshake validation.
"""
import requests
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


def is_manager_reachable(manager_url: str, timeout: int = 5) -> bool:
    """
    Quick health check to see if Manager is accessible.
    
    Returns: True if Manager responds
    """
    try:
        # Try to reach a health endpoint (or just hit the root)
        response = requests.get(
            f"{manager_url}/health",
            timeout=timeout
        )
        return response.status_code in [200, 404]  # 404 is ok, 200 is better
    
    except Exception as e:
        logger.debug(f"Manager not reachable at {manager_url}: {e}")
        return False


def flush_queue(reports: List[dict], manager_url: str, auth_token: str, 
                retry_attempts: int = 3, retry_delay: int = 10) -> bool:
    """
    Try to POST all pending reports to Manager.
    
    Implements handshake:
    - POST /api/usage/report with all reports
    - Manager validates and stores
    - If Manager returns 200, flush succeeded
    - If any error, no reports are deleted from queue
    
    Args:
        reports: List of usage report dicts
        manager_url: Base URL of Manager service
        auth_token: Bearer token for authentication
        retry_attempts: Number of retry attempts
        retry_delay: Seconds between retries
    
    Returns: True if flush successful (all reports posted)
    """
    if not reports:
        logger.debug("No reports to flush")
        return True
    
    endpoint = f"{manager_url}/api/usage/report"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    
    # Try multiple times before giving up
    for attempt in range(1, retry_attempts + 1):
        try:
            logger.info(f"Flushing {len(reports)} report(s) to {endpoint} "
                       f"(attempt {attempt}/{retry_attempts})")
            
            # POST all reports as batch
            response = requests.post(
                endpoint,
                json={"reports": reports},
                headers=headers,
                timeout=30
            )
            
            # Check for success
            if response.status_code == 200:
                logger.info(f"Successfully flushed {len(reports)} report(s)")
                return True
            
            # Log error response
            logger.warning(f"Manager returned {response.status_code}: {response.text}")
            
            # If last attempt, give up
            if attempt == retry_attempts:
                logger.error(f"Failed to flush after {retry_attempts} attempts")
                return False
            
            # Wait before retrying
            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
        
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout posting to Manager (attempt {attempt}/{retry_attempts})")
            if attempt < retry_attempts:
                time.sleep(retry_delay)
        
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error to Manager (attempt {attempt}/{retry_attempts})")
            if attempt < retry_attempts:
                time.sleep(retry_delay)
        
        except Exception as e:
            logger.error(f"Unexpected error posting to Manager: {e}")
            if attempt < retry_attempts:
                time.sleep(retry_delay)
    
    return False


def post_single_report(report: dict, manager_url: str, auth_token: str) -> bool:
    """
    Post a single report (convenience method).
    
    Returns: True if successful
    """
    return flush_queue([report], manager_url, auth_token)
