"""
Disk usage measurement (cross-platform).

Measures folder trees and volume capacity for the collector's disk mode.

For a volume or folder like /volume1 or E:/Users, discover_folders() lists the
immediate subdirectories; scan_folders() measures each one's total size
(including all nested contents).

Example:
  Folder:   /volume1
  Discovers: /volume1/JeffMoffitt, /volume1/OtherUser, ...
  Measures: Total size of each (all contents within)
  Reports:  {'sizes': {'/volume1/JeffMoffitt': 5000000, ...},
             'timed_out': [...], 'errors': [...]}

Cross-platform approach:
- Windows: PowerShell Get-ChildItem (native Win32 batch enumeration, faster than os.scandir)
- Linux/Synology: Python native os.scandir (iterative, no recursion limit)
- Handles permission errors gracefully
- Per-folder timeout plus an optional overall deadline for large trees (100TB+)
- A folder that times out or fails is reported as such, never as 0 bytes
"""
import os
import shutil
import logging
import stat
import subprocess
import time
import re
import sys
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# stat.FILE_ATTRIBUTE_REPARSE_POINT only exists on Windows builds of Python
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)

# How many directory entries to process between deadline checks
_DEADLINE_CHECK_EVERY = 1000

# PowerShell reads the folder from this environment variable, so the path is
# never spliced into the script text (no quoting/injection issues).
PS_PATH_ENV_VAR = 'LM_SCAN_PATH'
PS_MEASURE_SCRIPT = (
    "$r = Get-ChildItem -LiteralPath $env:" + PS_PATH_ENV_VAR + " "
    "-Recurse -Force -File -ErrorAction SilentlyContinue "
    "| Measure-Object -Property Length -Sum; "
    "Write-Output ('{0} {1}' -f [int64]$r.Sum, [int64]$r.Count)"
)


def format_bytes(b: int) -> str:
    """Human-readable byte string (B … PB), e.g. '1.5 GB'."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def measure_folder_powershell(path: str, timeout: float = 3600) -> Tuple[int, int]:
    """
    Measure folder size on Windows using PowerShell Get-ChildItem.

    Uses native Win32 batch enumeration (FindFirstFileEx with LARGE_FETCH),
    which is significantly faster than Python's per-entry os.scandir on
    Windows filesystems with large file counts.

    Args:
        path: Folder path to measure (passed via $env:LM_SCAN_PATH)
        timeout: Max seconds to wait for PowerShell process

    Returns: Tuple of (total_bytes, files_counted)

    Raises: TimeoutError if PowerShell exceeds the timeout (the process is
            killed); FileNotFoundError / PermissionError if the folder itself
            is missing or can't be listed; RuntimeError if PowerShell is
            unavailable or its output can't be parsed.
    """
    env = dict(os.environ)
    env[PS_PATH_ENV_VAR] = path
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_MEASURE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.001),
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"PowerShell measurement of {path} exceeded {timeout:.0f}s")
    except OSError as e:
        raise RuntimeError(f"Could not run PowerShell: {e}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"PowerShell returned empty output for {path}")
    try:
        parts = output.split()
        total_bytes = int(parts[0])
        files_counted = int(parts[1]) if len(parts) > 1 else -1
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"Could not parse PowerShell output for {path}: {e}")
    if files_counted == 0:
        # -ErrorAction SilentlyContinue makes Get-ChildItem print "0 0" for an
        # empty folder, a missing one and one it can't list (access denied)
        # alike. Listing it here tells them apart: the last two raise, so the
        # folder goes in _errors instead of being reported as 0 bytes.
        if not os.path.isdir(path):
            raise FileNotFoundError(f"Not a directory: {path}")
        with os.scandir(path):
            pass
    return total_bytes, files_counted


def _is_reparse_dir(entry: os.DirEntry) -> bool:
    """
    True for Windows directory reparse points (junctions, volume mount points,
    directory symlinks). Following them double-counts data or loops forever.
    Reparse-point *files* (e.g. Data Deduplication, cloud placeholders) are
    still counted by the caller; file symlinks are skipped separately.
    """
    is_junction = getattr(entry, 'is_junction', None)   # Python 3.12+
    if is_junction is not None and is_junction():
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    

def measure_folder_recursive(path: str, timeout: float = 3600,
                             deadline: Optional[float] = None) -> Tuple[int, int]:
    """
    Measure folder size by walking the whole tree (cross-platform).

    Iterative (explicit stack) so arbitrarily deep trees can't hit Python's
    recursion limit. Hard-linked files are counted once: (st_dev, st_ino) is
    remembered only for files with st_nlink > 1, so memory stays proportional
    to the number of multiply-linked files, not the size of the tree.
    (On Windows os.scandir reports st_nlink = 0, so no dedup happens there.)
    
    Args:
        path: Folder path to measure
        timeout: Max seconds to spend measuring this folder
        deadline: Optional absolute time.monotonic() value that also ends the scan
    
    Returns: Tuple of (total_bytes, files_counted)

    Raises:
        TimeoutError if the time budget runs out (never a partial total)
        OSError (FileNotFoundError, PermissionError, ...) if path itself
        can't be read. Unreadable subdirectories are skipped and logged.
    """
    limit = time.monotonic() + timeout
    if deadline is not None:
        limit = min(limit, deadline)

    if not os.path.isdir(path):
        raise FileNotFoundError(f"Not a directory: {path}")

    total_bytes = 0
    files_counted = 0
    unreadable_dirs = 0
    entries_seen = 0
    linked_inodes = set()     # (dev, ino) of files with st_nlink > 1
    skip_reparse = sys.platform == 'win32'
    stack = [path]

    while stack:
        if time.monotonic() > limit:
            raise TimeoutError(f"Measurement of {path} exceeded {timeout:.0f}s")
        dir_path = stack.pop()
        try:
            it = os.scandir(dir_path)
        except OSError as e:
            if dir_path == path:
                raise   # the folder itself is unreadable: an error, not 0 bytes
            unreadable_dirs += 1
            logger.debug(f"Cannot access {dir_path}: {e}")  # Permission denied on system folders is expected
            continue

        with it:
            for entry in it:
                entries_seen += 1
                if entries_seen % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() > limit:
                    raise TimeoutError(f"Measurement of {path} exceeded {timeout:.0f}s")
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
                        st = entry.stat(follow_symlinks=False)
                        if st.st_nlink > 1:
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key in linked_inodes:
                                continue   # hard link already counted
                            linked_inodes.add(inode_key)
                        total_bytes += st.st_size
                        files_counted += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if skip_reparse and _is_reparse_dir(entry):
                            continue
                        stack.append(entry.path)
                except OSError:
                    continue

    if unreadable_dirs:
        logger.info(f"{path}: skipped {unreadable_dirs} unreadable subfolder(s)")
    return total_bytes, files_counted


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


def discover_folders(volume_path: str, nas_type: str = "synology",
                     validate_volume: bool = True, raise_errors: bool = False) -> List[str]:
    """
    Discover immediate subdirectories on a volume (one level only).
    
    For /volume1, returns: [/volume1/user1, /volume1/user2, ...]
    For E:/Users, returns: [E:/Users/Admin, E:/Users/Guest, ...]
    
    Does NOT recurse deeper - you measure each subdirectory's full tree,
    but you only report top-level subdirectory sizes.
    
    Args:
        volume_path: Root volume path (e.g., /volume1 or E:)
        nas_type: 'synology' or 'windows'
        validate_volume: require a volume-style path (/volume*, /mnt*, X:).
            Pass False for an explicitly configured scan_paths glob root,
            which may legitimately live anywhere (Linux /data, UNC paths).
        raise_errors: re-raise the OSError if volume_path itself can't be
            listed, so the caller can report it (in _errors) instead of
            getting an empty list that looks like "no subfolders".
    
    Returns: List of immediate subdirectory paths (one level only)
    """
    folders = []
    
    if validate_volume and not is_valid_volume_path(volume_path, system=nas_type):
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
    except OSError as e:
        if raise_errors:
            raise
        logger.debug(f"Cannot read {volume_path}: {e}")
    
    return folders


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


def _measure_one_folder(folder_path: str, timeout: float, use_powershell: bool) -> Tuple[int, int]:
    """
    Measure a single folder, using PowerShell on Windows when requested.

    Falls back to the Python scanner (with whatever is left of the budget) if
    PowerShell fails — but not if it timed out, which would only spend the
    same budget again in the slower scanner, nor if the folder itself is
    missing or unreadable.

    Returns: (bytes_used, files_counted)
    Raises: TimeoutError, or OSError/RuntimeError on failure
    """
    deadline = time.monotonic() + timeout
    if use_powershell:
        try:
            return measure_folder_powershell(folder_path, timeout=timeout)
        except OSError:
            raise   # TimeoutError, or the folder itself is missing / can't be listed
        except Exception as ps_err:
            logger.warning(
                f"PowerShell measurement failed for {folder_path} ({ps_err}); "
                f"falling back to Python scanner"
            )
    return measure_folder_recursive(folder_path, timeout=max(0.0, deadline - time.monotonic()),
                                    deadline=deadline)


def scan_folders(folders: List[str], timeout: float = 3600,
                 deadline: Optional[float] = None) -> Dict:
    """
    Measure each folder in a pre-discovered list.

    On Windows, uses PowerShell Get-ChildItem for faster native enumeration.
    Falls back to the Python os.scandir walker if PowerShell is unavailable
    or returns an error.

    Args:
        folders:  Folder paths to measure (e.g. from discover_folders)
        timeout:  Max seconds per folder
        deadline: Optional overall time.monotonic() deadline shared by all
                  folders. Folders not reached before it are reported as
                  timed out rather than silently dropped.

    Returns: {'sizes': {path: bytes}, 'timed_out': [paths], 'errors': [paths]}
             A timed-out or failed folder is never reported with a size.
    """
    sizes: Dict[str, int] = {}
    timed_out: List[str] = []
    errors: List[str] = []
    use_powershell = sys.platform == 'win32'

    if use_powershell and folders:
        logger.info("Windows detected — using PowerShell for folder size measurement")

    for i, folder_path in enumerate(folders):
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            remaining = list(folders[i:])
            logger.warning(
                f"Overall disk-scan time budget exhausted: {len(remaining)} folder(s) not "
                f"measured, reported as timed out: {remaining}"
            )
            timed_out.extend(remaining)
            break
        folder_timeout = timeout if deadline is None else min(timeout, deadline - now)

        logger.info(f"Measuring ... {folder_path}")
        folder_start = time.monotonic()
        try:
            bytes_used, files_counted = _measure_one_folder(folder_path, folder_timeout, use_powershell)
        except TimeoutError as e:
            logger.warning(f"Timed out measuring {folder_path}: {e} (reported as timed out, not 0)")
            timed_out.append(folder_path)
            continue
        except Exception as e:
            logger.error(f"Failed to measure {folder_path}: {e}")
            errors.append(folder_path)
            continue

        sizes[folder_path] = bytes_used
        folder_elapsed = time.monotonic() - folder_start
        count_str = f"{files_counted:,} files" if files_counted >= 0 else "file count n/a"
        logger.info(f"[{folder_elapsed:.1f}s] {folder_path}: {format_bytes(bytes_used)} ({count_str})")

    return {'sizes': sizes, 'timed_out': timed_out, 'errors': errors}
