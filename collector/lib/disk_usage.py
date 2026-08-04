"""
Disk usage measurement (cross-platform).

Measures immediate subdirectories one level deep.

For volumes like /volume1 or E:/Users, discovers immediate subdirectories
and measures their total size (including all nested contents).

Example:
  Volume: /volume1
  Discovers: /volume1/JeffMoffitt, /volume1/OtherUser, ...
  Measures: Total size of each (all contents within)
  Reports: [{'path': '/volume1/JeffMoffitt', 'usage_bytes': 5000000}, ...]

Cross-platform approach:
- Python native os.scandir (faster and more reliable than subprocess du)
- Handles permission errors gracefully
- Timeout support for large folders (100TB+)
- Works on Windows, Linux, and Synology NAS
"""
import os
import logging
import time
import re
import sys
from typing import List, Dict
from pathlib import Path

logger = logging.getLogger(__name__)


def measure_folder_recursive(path: str, timeout: int = 3600) -> int:
    """
    Measure folder size recursively (cross-platform).
    
    Uses Python's native os.scandir for fast, reliable measurement.
    Works on Windows, Linux, and Synology.
    
    Args:
        path: Folder path to measure
        timeout: Max seconds to spend measuring this folder
    
    Returns: Total bytes used by folder and contents
    
    Raises: TimeoutError if measurement exceeds timeout
    """
    total_bytes = 0
    start_time = time.time()
    files_counted = 0
    
    def _scan(dir_path: str) -> int:
        """Recursive scan helper"""
        nonlocal total_bytes, files_counted
        
        # Check timeout
        if time.time() - start_time > timeout:
            raise TimeoutError(f"Measurement of {path} exceeded {timeout}s")
        
        dir_size = 0
        
        try:
            for entry in os.scandir(dir_path):
                try:
                    if entry.is_symlink():
                        # Skip symlinks to avoid loops
                        continue
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                            dir_size += size
                            files_counted += 1
                        except OSError:
                            # File may have been deleted or access denied
                            pass
                    elif entry.is_dir(follow_symlinks=False):
                        # Recurse into subdirectory
                        dir_size += _scan(entry.path)
                except (OSError, PermissionError):
                    # Permission denied, skip this entry
                    continue
        except (OSError, PermissionError) as e:
            logger.warning(f"Cannot access {dir_path}: {e}")
        
        return dir_size
    
    try:
        if not os.path.exists(path):
            logger.warning(f"Path does not exist: {path}")
            return 0
        
        total_bytes = _scan(path)
        logger.info(f"Measured {path}: {total_bytes} bytes ({files_counted} files)")
        return total_bytes
    
    except TimeoutError:
        raise
    except Exception as e:
        logger.error(f"Error measuring {path}: {e}")
        return 0


def should_skip_folder(folder_name: str, nas_type: str = "synology") -> bool:
    """
    Check if folder should be skipped based on NAS type.
    
    Synology NAS: Skip @* system directories
    Windows: No system exclusions (all user-accessible paths count)
    """
    if nas_type.lower() == "synology":
        # Skip Synology system directories
        if folder_name.startswith('@'):
            return True
        # Skip trash
        if folder_name == '#recycle':
            return True
    
    return False


def is_valid_volume_path(path: str, system: str = None) -> bool:
    """
    Validate that path is a proper volume/mount point.
    
    Args:
        path: Path to validate
        system: 'windows', 'synology', or None (auto-detect)
    
    Returns: True if valid
    """
    if system is None:
        # Auto-detect
        system = "windows" if sys.platform == "win32" else "synology"
    
    if system == "windows":
        # Windows: Should be drive letter (D:, E:, etc.)
        if not re.match(r'^[A-Z]:', path):
            logger.warning(f"Windows path should start with drive letter: {path}")
            return False
    else:
        # Synology/Linux: Should be /volume* or mount point
        if not (path.startswith('/volume') or path.startswith('/mnt')):
            logger.warning(f"Linux path should be /volume* or /mnt: {path}")
            return False
    
    # Check it exists
    if not os.path.exists(path):
        logger.warning(f"Path does not exist: {path}")
        return False
    
    # Check it's a directory
    if not os.path.isdir(path):
        logger.warning(f"Path is not a directory: {path}")
        return False
    
    return True


def discover_folders(volume_path: str, nas_type: str = "synology") -> List[str]:
    """
    Discover immediate subdirectories on a volume (one level only).
    
    For /volume1, returns: [/volume1/user1, /volume1/user2, ...]
    For E:/Users, returns: [E:/Users/Admin, E:/Users/Guest, ...]
    
    Does NOT recurse deeper - you measure each subdirectory's full tree,
    but you only report top-level subdirectory sizes.
    
    Args:
        volume_path: Root volume path (e.g., /volume1 or E:)
        nas_type: 'synology' or 'windows'
    
    Returns: List of immediate subdirectory paths (one level only)
    """
    folders = []
    
    if not is_valid_volume_path(volume_path, system=nas_type):
        return folders
    
    try:
        for entry in os.scandir(volume_path):
            if entry.is_dir(follow_symlinks=False):
                folder_name = entry.name
                
                # Skip system folders on Synology
                if should_skip_folder(folder_name, nas_type):
                    logger.debug(f"Skipping system folder: {entry.path}")
                    continue
                
                # Add this immediate subdirectory only
                folders.append(entry.path)
    except (OSError, PermissionError) as e:
        logger.debug(f"Cannot read {volume_path}: {e}")
    
    return folders


def discover_volumes(nas_type: str = None) -> List[str]:
    """
    Auto-discover storage volumes.
    
    Args:
        nas_type: 'synology', 'windows', or None (auto-detect)
    
    Returns: List of volume paths
    """
    if nas_type is None:
        nas_type = "windows" if sys.platform == "win32" else "synology"
    
    volumes = []
    
    if nas_type == "windows":
        # Windows: Find all drive letters
        import string
        for drive in string.ascii_uppercase:
            path = f"{drive}:"
            if os.path.exists(path):
                volumes.append(path)
        logger.info(f"Discovered Windows drives: {volumes}")
    
    else:
        # Synology/Linux: Look for /volume* directories
        for i in range(1, 20):  # Check /volume1 through /volume19
            path = f"/volume{i}"
            if os.path.exists(path) and os.path.isdir(path):
                volumes.append(path)
        logger.info(f"Discovered Synology volumes: {volumes}")
    
    return volumes


def measure_all_folders(volumes: List[str], timeout: int = 3600) -> List[Dict]:
    """
    Measure usage for all immediate subdirectories on given volumes.
    
    For each volume, discovers immediate subdirectories (one level only),
    then measures the size of each subdirectory including all nested contents.
    
    Args:
        volumes: List of volume paths (e.g., ['/volume1', '/volume2'])
        timeout: Max seconds per folder measurement
    
    Returns: List of folder reports with path and usage_bytes
        Example: [{'path': '/volume1/JeffMoffitt', 'usage_bytes': 5000000},
                  {'path': '/volume1/OtherUser', 'usage_bytes': 3000000}]
    """
    results = []
    start_time = time.time()
    
    for volume in volumes:
        if not os.path.exists(volume):
            logger.warning(f"Volume does not exist: {volume}")
            continue
        
        # Log progress
        elapsed = time.time() - start_time
        logger.info(f"Measuring volume {volume} (elapsed {elapsed:.1f}s)")
        
        # Discover immediate subdirectories on this volume (one level only)
        try:
            folders = discover_folders(volume, nas_type="synology" if not sys.platform.startswith('win') else "windows")
        except Exception as e:
            logger.error(f"Failed to discover folders on {volume}: {e}")
            continue
        
        logger.info(f"Found {len(folders)} subdirectories on {volume}")
        
        # Measure each folder
        for folder_path in folders:
            try:
                # Check overall timeout
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    logger.warning(f"Overall timeout ({timeout}s) reached, stopping measurements")
                    break
                
                # Measure this subdirectory (including all nested contents)
                bytes_used = measure_folder_recursive(folder_path, timeout=timeout)
                
                results.append({
                    'path': folder_path,
                    'usage_bytes': bytes_used
                })
                
                # Format bytes nicely for logging
                if bytes_used > 1024**3:
                    size_str = f"{bytes_used / (1024**3):.2f} GB"
                elif bytes_used > 1024**2:
                    size_str = f"{bytes_used / (1024**2):.2f} MB"
                else:
                    size_str = f"{bytes_used / 1024:.2f} KB"
                
                # Log progress with readable size
                elapsed = time.time() - start_time
                logger.info(f"[{elapsed:.1f}s] {folder_path}: {size_str}")
            
            except TimeoutError as e:
                logger.warning(f"Timeout measuring {folder_path}: {e}")
                results.append({
                    'path': folder_path,
                    'usage_bytes': 0,
                    'timeout': True
                })
            except Exception as e:
                logger.error(f"Failed to measure {folder_path}: {e}")
    
    # Log summary
    total_time = time.time() - start_time
    total_bytes = sum(r.get('usage_bytes', 0) for r in results)
    if total_bytes > 1024**3:
        total_str = f"{total_bytes / (1024**3):.2f} GB"
    elif total_bytes > 1024**2:
        total_str = f"{total_bytes / (1024**2):.2f} MB"
    else:
        total_str = f"{total_bytes / 1024:.2f} KB"
    logger.info(f"Measurement complete: {len(results)} subdirectories, {total_str} total, {total_time:.1f}s")
    
    return results
