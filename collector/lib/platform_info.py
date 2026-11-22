"""
Facts about the host the collector runs on.

platform_kind() is the one answer to "which platform is this": config
"platform" when it is set and valid, otherwise disk_usage.detect_platform()
(Windows; Synology when a DSM marker file exists; any other host is
'linux'). collector.get_platform() delegates here, so the metrics-mode
modules (counters, GPU, sessions, storage health) and the disk scan always
agree. device_type ("NAS", "Server", ...) is only the Dashboard's grouping
label and is never used to tell platforms apart.
"""
import logging
import os
import struct
import sys
from typing import Optional

from . import disk_usage

logger = logging.getLogger(__name__)

# DSM's version file: majorversion="7" / minorversion="1" / buildnumber="42661" ...
DSM_VERSION_FILE = '/etc.defaults/VERSION'


def platform_kind(config: Optional[dict] = None) -> str:
    """
    'synology', 'linux' or 'windows': config "platform" when set, otherwise
    detected (see disk_usage.detect_platform). An invalid "platform" value
    is logged and detection is used instead.
    """
    override = (config or {}).get('platform')
    if override is not None:
        value = str(override).strip().lower()
        if value in disk_usage.PLATFORMS:
            return value
        logger.warning(f"Invalid platform {override!r} in config (expected one of "
                       f"{', '.join(disk_usage.PLATFORMS)}); detecting it instead")
    return disk_usage.detect_platform()


def is_root() -> bool:
    """True when the effective user is root (POSIX). Always False on Windows."""
    geteuid = getattr(os, 'geteuid', None)
    return geteuid is not None and sys.platform != 'win32' and geteuid() == 0


def is_32bit() -> bool:
    """True for a 32-bit Python (armv7 DSM units): kernel counters there wrap at 2**32."""
    return struct.calcsize('P') == 4


def dsm_version(path: str = DSM_VERSION_FILE) -> Optional[dict]:
    """
    {'majorversion', 'minorversion', 'buildnumber'} from DSM's VERSION file
    (ints where the value is a number), or None when the file is missing,
    unreadable or has no majorversion (not a DSM host).
    """
    wanted = ('majorversion', 'minorversion', 'buildnumber')
    found = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                key, sep, value = line.partition('=')
                key = key.strip()
                if sep and key in wanted:
                    value = value.strip().strip('"')
                    found[key] = int(value) if value.isdigit() else value
    except OSError:
        return None
    if 'majorversion' not in found:
        return None
    return {key: found.get(key) for key in wanted}
