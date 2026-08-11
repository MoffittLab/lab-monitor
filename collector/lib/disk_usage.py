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
- Windows: PowerShell Get-ChildItem (native Win32 batch enumeration, faster than os.scandir)
- Linux/Synology: Python native os.scandir
- Handles permission errors gracefully
- Timeout support for large folders (100TB+)
- Works on Windows, Linux, and Synology NAS
"""
import os
import shutil
import logging
import subprocess
import time
import re
import sys
from typing import List, Dict, Tuple, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def measure_folder_powershell(path: str, timeout: int = 3600) -> Tuple[int, int]:
    """
    Measure folder size on Windows using PowerShell Get-ChildItem.

    Uses native Win32 batch enumeration (FindFirstFileEx with LARGE_FETCH),
    which is significantly faster than Python's per-entry os.scandir on
    Windows filesystems with large file counts.

    Args:
        path: Folder path to measure
        timeout: Max seconds to wait for PowerShell process

    Returns: Tuple of (total_bytes, files_counted)
             files_counted is -1 when PowerShell returns a result (count
             not easily available without a second pipeline step).

    Raises: RuntimeError if PowerShell is unavailable or returns an error.
    """
    # Escape single quotes in path for PowerShell string literal
    ps_path = path.replace("'", "''")
    ps_script = (
        "$r = Get-ChildItem -LiteralPath '" + ps_path + "' "
        "-Recurse -Force -ErrorAction SilentlyContinue "
        "| Measure-Object -Property Length -Sum; "
        "Write-Output \"$($r.Sum) $($r.Count)\""
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError(f"PowerShell returned empty output for {path}")
        parts = output.split()
        total_bytes   = int(float(parts[0])) if parts[0] not in ('', 'None', None) else 0
        files_counted = int(parts[1]) if len(parts) > 1 and parts[1] not in ('', 'None', None) else -1
        return total_bytes, files_counted
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"PowerShell measurement of {path} exceeded {timeout}s")
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"Could not parse PowerShell output for {path}: {e}")


def measure_folder_recursive(path: str, timeout: int = 3600) -> int:
    """
    Measure folder size recursively (cross-platform).
    
    Uses Python's native os.scandir for fast, reliable measurement.
    Works on Windows, Linux, and Synology.
    
    Args:
        path: Folder path to measure
        timeout: Max seconds to spend measuring this folder
    
    Returns: Tuple of (total_bytes, files_counted)

    Raises: TimeoutError if measurement exceeds timeout
    """
    total_bytes = 0
    start_time = time.time()
    files_counted = 0
    visited_inodes = set()   # track (dev, ino) to skip hard-link duplicates

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
                    # Skip symlinks to avoid loops
                    if entry.is_symlink():
                        continue

                    # Skip Synology system directories and backup vaults at every level
                    name = entry.name
                    if name.startswith('@') or name == '#recycle' or name.startswith('.@'):
                        continue
                    # Skip Hyper Backup vault packages (.hbk are directories, not files)
                    if name.endswith('.hbk'):
                        continue

                    if entry.is_file(follow_symlinks=False):
                        try:
                            st = entry.stat(follow_symlinks=False)
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key in visited_inodes:
                                continue   # hard-link already counted
                            visited_inodes.add(inode_key)
                            dir_size += st.st_size
                            files_counted += 1
                        except OSError:
                            pass
                    elif entry.is_dir(follow_symlinks=False):
                        dir_size += _scan(entry.path)
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError) as e:
            logger.debug(f"Cannot access {dir_path}: {e}")  # Permission denied on system folders is expected

        return dir_size
    
    try:
        if not os.path.exists(path):
            logger.debug(f"Path does not exist: {path}")  # Non-existent scan paths are handled gracefully
            return 0
        
        total_bytes = _scan(path)
        return total_bytes, files_counted

    except TimeoutError:
        raise
    except Exception as e:
        logger.error(f"Error measuring {path}: {e}")
        return 0, 0


def should_skip_folder(folder_name: str, nas_type: str = "synology") -> bool:
    """
    Check if folder should be skipped based on NAS type and naming conventions.
    
    Always skips:
    - Folders starting with '.' (hidden/system folders like .git, .pids, .venv)
    
    Synology NAS additionally skips:
    - @* system directories
    - #recycle (trash)
    
    Windows additionally skips:
    - $RECYCLE.BIN (Windows recycle bin)
    - ServiceProfiles (Windows service account directories)
    
    Args:
        folder_name: Name of the folder to check
        nas_type: 'synology' or 'windows'
    
    Returns: True if folder should be skipped, False otherwise
    """
    # Skip hidden/system folders (start with '.')
    if folder_name.startswith('.'):
        return True
    
    if nas_type.lower() == "synology":
        # Skip Synology system directories
        if folder_name.startswith('@'):
            return True
        # Skip trash
        if folder_name == '#recycle':
            return True
    
    if nas_type.lower() == "windows" or sys.platform == "win32":
        # Skip Windows recycle bin
        if folder_name == '$RECYCLE.BIN':
            return True
        # Skip Windows service profile directories
        if 'ServiceProfiles' in folder_name:
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


def discover_at_depth(root: str, depth: int, nas_type: str = None) -> List[str]:
    """
    Discover folders at exactly 'depth' levels below root.

    depth=1: immediate subdirectories of root  (e.g. /volume1/SharedFolder)
    depth=2: subdirectories of those           (e.g. /volume1/SharedFolder/Project)

    Args:
        root:     Starting path (e.g. /volume1)
        depth:    How many levels to descend
        nas_type: 'synology', 'windows', or None (auto-detect)

    Returns: List of leaf folder paths at the requested depth
    """
    if nas_type is None:
        nas_type = "windows" if sys.platform == "win32" else "synology"

    if depth == 1:
        return discover_folders(root, nas_type)

    leaf_folders = []
    for folder in discover_folders(root, nas_type):
        leaf_folders.extend(discover_at_depth(folder, depth - 1, nas_type))
    return leaf_folders


def measure_volume_capacity(volume_path: str) -> dict:
    """
    Get total and free bytes for a volume (cross-platform).

    Uses shutil.disk_usage which works on Synology (/volume1) and
    Windows (E:) with no platform branching required.

    Args:
        volume_path: Volume root path (e.g. /volume1 or E:)

    Returns: dict with 'total_bytes' and 'free_bytes', or empty dict on error
    """
    try:
        usage = shutil.disk_usage(volume_path)
        return {
            'total_bytes': usage.total,
            'free_bytes':  usage.free,
        }
    except Exception as e:
        logger.warning(f"Could not get capacity for {volume_path}: {e}")
        return {}


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


def _format_bytes(b: int) -> str:
    """Human-readable byte string for log messages."""
    if b > 1024 ** 3:
        return f"{b / (1024 ** 3):.2f} GB"
    if b > 1024 ** 2:
        return f"{b / (1024 ** 2):.2f} MB"
    return f"{b / 1024:.2f} KB"


def _measure_one_folder(folder_path: str, timeout: int, use_powershell: bool) -> Tuple[int, int]:
    """
    Measure a single folder, using PowerShell on Windows when requested.

    Falls back to the Python scanner if PowerShell fails.

    Returns: (bytes_used, files_counted)
    """
    if use_powershell:
        try:
            return measure_folder_powershell(folder_path, timeout=timeout)
        except Exception as ps_err:
            logger.warning(
                f"PowerShell measurement failed for {folder_path} ({ps_err}); "
                f"falling back to Python scanner"
            )
    return measure_folder_recursive(folder_path, timeout=timeout)


def measure_leaf_folders(leaf_folders: List[str], timeout: int = 3600) -> List[Dict]:
    """
    Measure usage for a pre-discovered list of leaf folders.

    On Windows, uses PowerShell Get-ChildItem for faster native enumeration.
    Falls back to the Python os.scandir walker if PowerShell is unavailable
    or returns an error.

    Does NOT discover subdirectories — measures each folder as-is.
    Use when you have already discovered the exact folders to measure via discover_at_depth.

    Args:
        leaf_folders: List of folder paths already discovered (e.g., from discover_at_depth)
        timeout: Max seconds per folder measurement

    Returns: List of folder reports with path and usage_bytes
    """
    results = []
    start_time = time.time()
    use_powershell = sys.platform == 'win32'

    if use_powershell:
        logger.info("Windows detected — using PowerShell for folder size measurement")

    for folder_path in leaf_folders:
        if not os.path.exists(folder_path):
            logger.warning(f"Folder does not exist: {folder_path}")
            continue

        try:
            # Check overall timeout
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning(f"Overall timeout ({timeout}s) reached, stopping measurements")
                break

            logger.info(f"Measuring ... {folder_path}")
            folder_start = time.time()

            bytes_used, files_counted = _measure_one_folder(folder_path, timeout, use_powershell)

            results.append({
                'path': folder_path,
                'usage_bytes': bytes_used
            })

            folder_elapsed = time.time() - folder_start
            count_str = f"{files_counted:,} files" if files_counted >= 0 else "file count n/a"
            logger.info(f"[{folder_elapsed:.1f}s] {folder_path}: {_format_bytes(bytes_used)} ({count_str})")

        except Exception as e:
            logger.error(f"Failed to measure {folder_path}: {e}")
            continue

    return results


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
    use_powershell = sys.platform == 'win32'

    if use_powershell:
        logger.info("Windows detected — using PowerShell for folder size measurement")

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
                
                # Announce before starting — lets you see what's in progress
                logger.info(f"Measuring ... {folder_path}")
                folder_start = time.time()

                # Measure this subdirectory (including all nested contents)
                bytes_used, files_counted = _measure_one_folder(folder_path, timeout, use_powershell)

                results.append({
                    'path': folder_path,
                    'usage_bytes': bytes_used
                })

                folder_elapsed = time.time() - folder_start
                count_str = f"{files_counted:,} files" if files_counted >= 0 else "file count n/a"
                logger.info(f"[{folder_elapsed:.1f}s] {folder_path}: {_format_bytes(bytes_used)} ({count_str})")
            
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
    logger.info(f"Measurement complete: {len(results)} subdirectories, {_format_bytes(total_bytes)} total, {total_time:.1f}s")
    
    return results
