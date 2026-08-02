"""
Disk usage measurement for monitored folders
"""
import os
import subprocess
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def format_bytes(size_bytes: int) -> str:
    """
    Format bytes as human-readable size (B, KB, MB, GB, TB).
    
    Args:
        size_bytes: Size in bytes
    
    Returns: Formatted string (e.g., "5.4 GB", "2.1 TB")
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} EB"


def format_duration(seconds: float) -> str:
    """
    Format duration as human-readable (s, m, h).
    
    Args:
        seconds: Duration in seconds
    
    Returns: Formatted string (e.g., "23s", "5m 32s", "2h 14m")
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def get_folder_size(path: str, timeout: int = 300, folder_num: int = None, total_folders: int = None) -> Optional[int]:
    """
    Get total size of a folder in bytes.
    
    Tries platform-specific methods:
    - Windows: dir command with /s flag (if du.exe not available)
    - Linux/Synology: du -sb
    
    Args:
        path: Folder path to measure
        timeout: Timeout in seconds
        folder_num: Progress indicator (current folder number)
        total_folders: Progress indicator (total folders)
    
    Returns: Size in bytes, or None if measurement fails
    """
    if not os.path.exists(path):
        logger.warning(f"Path does not exist: {path}")
        return None
    
    # Log progress header
    progress_str = ""
    if folder_num and total_folders:
        progress_str = f" [{folder_num}/{total_folders}]"
    logger.info(f"Measuring {path}{progress_str}...")
    
    start_time = time.time()
    
    try:
        # Try du command (Linux/Synology/Git Bash on Windows)
        if os.name != 'nt':
            # Linux/Synology
            result = subprocess.run(
                ['du', '-sb', path],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if result.returncode == 0:
                size_bytes = int(result.stdout.split()[0])
                elapsed = time.time() - start_time
                size_str = format_bytes(size_bytes)
                duration_str = format_duration(elapsed)
                logger.info(f"  ✓ {path}: {size_str} ({duration_str})")
                return size_bytes
        else:
            # Windows: try du.exe first, fall back to Python recursion
            try:
                result = subprocess.run(
                    ['du.exe', '-sb', path],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                if result.returncode == 0:
                    size_bytes = int(result.stdout.split()[0])
                    elapsed = time.time() - start_time
                    size_str = format_bytes(size_bytes)
                    duration_str = format_duration(elapsed)
                    logger.info(f"  ✓ {path}: {size_str} ({duration_str})")
                    return size_bytes
            except FileNotFoundError:
                logger.debug("du.exe not found, falling back to Python recursion")
                return _get_folder_size_python(path, start_time)
    
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        duration_str = format_duration(elapsed)
        logger.error(f"  ✗ Timeout measuring {path} (>{timeout}s, elapsed {duration_str})")
        return None
    except Exception as e:
        elapsed = time.time() - start_time
        duration_str = format_duration(elapsed)
        logger.error(f"  ✗ Error measuring {path}: {e} (elapsed {duration_str})")
        return None
    
    # Fallback: Python-based recursion (slower but reliable)
    return _get_folder_size_python(path, start_time)


def _get_folder_size_python(path: str, start_time: float = None) -> Optional[int]:
    """
    Fallback: Measure folder size using Python os.walk.
    Slower than du but works on any system.
    
    Args:
        path: Folder path to measure
        start_time: Start time for duration calculation
    """
    if start_time is None:
        start_time = time.time()
    
    try:
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                try:
                    total_size += os.path.getsize(filepath)
                except (OSError, FileNotFoundError):
                    # Skip files we can't access
                    pass
        
        elapsed = time.time() - start_time
        size_str = format_bytes(total_size)
        duration_str = format_duration(elapsed)
        logger.info(f"  ✓ {path}: {size_str} ({duration_str}, Python fallback)")
        return total_size
    
    except Exception as e:
        elapsed = time.time() - start_time
        duration_str = format_duration(elapsed)
        logger.error(f"  ✗ Error measuring {path} (Python fallback): {e} (elapsed {duration_str})")
        return None


def discover_folders(volumes: List[str], exclude: List[str] = None, timeout: int = 300) -> Dict[str, int]:
    """
    Auto-discover and measure folders one level deep from volumes.
    
    Args:
        volumes: List of volume/root paths (e.g., ['/volume1', '/volume2'])
        exclude: List of folder names to exclude (case-insensitive)
        timeout: Timeout for measurement
    
    Returns: Dict of {path: size_bytes}
    """
    if exclude is None:
        exclude = []
    
    # Normalize exclude list to lowercase for comparison
    exclude_lower = [e.lower() for e in exclude]
    
    results = {}
    discovered = []
    
    for volume in volumes:
        if not os.path.exists(volume):
            logger.warning(f"Volume does not exist: {volume}")
            continue
        
        try:
            # List one level deep
            entries = os.listdir(volume)
            
            for entry in entries:
                full_path = os.path.join(volume, entry)
                
                # Skip if not a directory
                if not os.path.isdir(full_path):
                    continue
                
                # Skip Synology system directories (start with @)
                if entry.startswith('@'):
                    logger.debug(f"Excluding {full_path} (Synology system directory)")
                    continue
                
                # Skip if in exclude list (case-insensitive)
                if entry.lower() in exclude_lower:
                    logger.debug(f"Excluding {full_path} (in exclude list)")
                    continue
                
                discovered.append(full_path)
        
        except PermissionError:
            logger.warning(f"Permission denied reading {volume}")
        except Exception as e:
            logger.error(f"Error listing {volume}: {e}")
    
    logger.info(f"Discovered {len(discovered)} folder(s) to measure")
    
    # Measure all discovered folders with progress tracking
    for idx, folder in enumerate(discovered, 1):
        size = get_folder_size(folder, timeout=timeout, folder_num=idx, total_folders=len(discovered))
        if size is not None:
            results[folder] = size
        else:
            logger.warning(f"Skipping {folder} (measurement failed)")
    
    return results


def measure_all_folders(folders: List[str], timeout: int = 300) -> Dict[str, int]:
    """
    Measure all configured folders.
    
    Returns: Dict of {path: size_bytes}
             Includes only successfully measured folders.
    """
    results = {}
    
    for folder in folders:
        size = get_folder_size(folder, timeout=timeout)
        if size is not None:
            results[folder] = size
        else:
            logger.warning(f"Skipping {folder} (measurement failed)")
    
    return results


def discover_volumes() -> List[str]:
    """
    Auto-discover mounted volumes on the system.
    Only returns volumes starting with 'volume' (e.g., /volume1, /volume2).
    
    Returns: List of volume paths matching pattern (e.g., ['/volume1', '/volume2'])
    """
    volumes = []
    
    try:
        if os.name != 'nt':
            # Linux/Synology: use df command
            result = subprocess.run(
                ['df', '-h'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                # Parse df output: skip header, extract mount points
                lines = result.stdout.strip().split('\n')[1:]  # Skip header
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 6:
                        mount_point = parts[-1]
                        # Only include volumes starting with 'volume' (e.g., /volume1, /volume2)
                        basename = os.path.basename(mount_point)
                        if basename.startswith('volume'):
                            if os.path.exists(mount_point) and os.path.isdir(mount_point):
                                volumes.append(mount_point)
        else:
            # Windows: use diskpart or wmic to get volumes
            try:
                result = subprocess.run(
                    ['wmic', 'logicaldisk', 'get', 'name'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')[1:]  # Skip header
                    for line in lines:
                        drive = line.strip()
                        # Only include volumes starting with 'volume' (e.g., C:, V:, etc. if labeled)
                        if drive and drive[0].lower() in ['v']:
                            volumes.append(drive + '\\')
            except FileNotFoundError:
                logger.warning("wmic not found, unable to auto-discover Windows volumes")
    
    except Exception as e:
        logger.error(f"Error discovering volumes: {e}")
    
    logger.info(f"Auto-discovered {len(volumes)} 'volume*' volume(s): {volumes}")
    return volumes
