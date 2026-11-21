"""
Which block devices are physical disks (Linux and Synology).

Shared by the disk I/O counters (perf_counters) and storage health, so both
see the same disks. /proc/diskstats lists every block device and partition;
only whole physical disks are summed, so traffic is never counted twice:

  - disk:  /sys/block/<name>/device exists (sda, sata1 on DSM, nvme0n1, vda,
           xvda, mmcblk0), minus optical drives (sr*), the DSM boot device
           (synoboot*), eMMC boot and RPMB areas (mmcblk0boot0, mmcblk0rpmb)
           and anything matching config "disk_io_exclude" (globs);
  - array: md RAID arrays (md0, md2, ...). Their I/O is already counted on
           the member disks, so they are classified only to be left out;
  - None:  everything else - partitions (not in /sys/block at all), loop,
           ram, zram, dm-*, nbd: virtual devices with no 'device' link.

When /sys/block can't be read the name alone decides, by the usual disk
naming patterns.
"""
import fnmatch
import os
import re
from typing import Iterable, List, Optional

SYS_BLOCK = '/sys/block'

_ARRAY_RE = re.compile(r'^md\d+$')
# Used only when /sys/block can't be read
_FALLBACK_DISK_RE = re.compile(r'^(sd[a-z]+|sata\d+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$')
# Have a 'device' link but are not data disks
_NOT_DISKS = ('sr*', 'synoboot*', '*boot[0-9]*', '*rpmb')

SECTOR_BYTES = 512     # /sys/block/<n>/size and /proc/diskstats count 512-byte sectors, whatever the disk's


def _excluded(name: str, exclude: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in tuple(_NOT_DISKS) + tuple(exclude or ()))


def classify_block_device(name: str, sys_block: Optional[str] = None,
                          exclude: Iterable[str] = ()) -> Optional[str]:
    """'disk', 'array' or None (not a physical disk: partition, loop, zram, dm-*, ...)."""
    sys_block = sys_block or SYS_BLOCK
    if _ARRAY_RE.match(name):
        return 'array'
    if _excluded(name, exclude):
        return None
    entry = os.path.join(sys_block, name)
    try:
        if os.path.lexists(entry):
            return 'disk' if os.path.exists(os.path.join(entry, 'device')) else None
        if os.path.isdir(sys_block):
            return None              # not a whole device here: a partition
    except (OSError, ValueError):
        pass
    return 'disk' if _FALLBACK_DISK_RE.match(name) else None


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def list_block_devices(sys_block: Optional[str] = None, exclude: Iterable[str] = ()) -> List[dict]:
    """
    [{name, kind ('disk' or 'array'), rotational (bool or None), size_bytes
    (int or None)}] for the devices in /sys/block, sorted by name; [] when it
    can't be listed.
    """
    sys_block = sys_block or SYS_BLOCK
    try:
        names = sorted(os.listdir(sys_block))
    except OSError:
        return []
    devices = []
    for name in names:
        kind = classify_block_device(name, sys_block, exclude)
        if kind is None:
            continue
        rotational = _read_int(os.path.join(sys_block, name, 'queue', 'rotational'))
        sectors = _read_int(os.path.join(sys_block, name, 'size'))
        devices.append({
            'name':       name,
            'kind':       kind,
            'rotational': None if rotational is None else rotational == 1,
            'size_bytes': None if sectors is None else sectors * SECTOR_BYTES,
        })
    return devices
