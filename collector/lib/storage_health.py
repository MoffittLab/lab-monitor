"""
Storage health: the 'storage_health' message (contract item 14), built in
the metrics run and sent when the health signature changes (trigger
'change'), every storage_health.interval_seconds ('interval', default 1 h),
or by `collector.py --mode health` ('manual').

Linux and DSM, without root (Tier A), every run (milliseconds):
  md      /sys/block/md*/md: array_state, level, raid_disks, degraded,
          sync_action, sync_completed, sync_speed, mismatch_cnt and each
          member's dev-*/state; /proc/mdstat when sysfs can't be read
  sysfs   /sys/block/<disk>: size, rotational, device/{model, state,
          ioerr_cnt}, the serial (device/serial, or the vpd_pg80 page)
  mounts  /proc/mounts: whether a volume root is mounted read-only
  zpool   `zpool list -H -o name,health` (15 s timeout), where zpool exists
and on the hourly cadence of the expensive sources:
  hwmon   drivetemp / nvme temperatures. drivetemp sends the drive a
          command, which on some drives resets the spin-down timer, so it is
          never read every run.
Windows, hourly (whatever the SYSTEM account can read):
  windows_storage, events   one PowerShell run (PS_HEALTH_SCRIPT, killed after
          storage_health.timeout_seconds, default 90 s):
          Get-PhysicalDisk with Get-StorageReliabilityCounter, Storage
          Spaces virtual disks and jobs, Get-Volume, and the System log's
          disk / Ntfs / storport-miniport events since the previous run.
Between expensive runs their last results come from
storage_health_state.json.

Not collected in this release: SMART on Linux and DSM (it needs root; a
privileged helper writing smartctl output is a future option) and DSM's
SNMP disk, RAID and SMART tables (a future opt-in). Their sources ('smart',
'snmp') are left out of the message.

Every component gets a status (ok / warning / critical / unknown, see
classify()); overall_status is the worst of them, 'unknown' only when
nothing could be assessed. For history and rollups each component's
numbers are also flattened into top-level keys <kind>_<sid>_<metric>
(flatten()). maybe_collect() never writes a file: it returns the new state,
which the collector writes once the message is recorded.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from . import blockdev, disk_usage, platform_info, settings

logger = logging.getLogger(__name__)

STATE_FILE = 'storage_health_state.json'
SCHEMA_VERSION = 1

SYS_ROOT = '/sys'
PROC_ROOT = '/proc'
FSTAB = '/etc/fstab'
ZPOOL_TIMEOUT = 15
FIRST_EVENT_LOOKBACK = 86400          # Windows events read on the first run: the last day
RISE_HOLD_SECONDS = 86400             # a rising error counter keeps its warning this long

STATUS_ORDER = {'ok': 0, 'warning': 1, 'critical': 2}
STATUS_CODES = STATUS_ORDER           # overall_status_code / <kind>_<sid>_status_code; unknown = null

ID_MAX_CHARS = 64                     # contract R10: disk, array and volume ids
TEXT_MAX_CHARS = 128                  # model, serial, name
REASON_MAX_CHARS, MAX_REASONS = 200, 10
LIST_CAPS = {'arrays': 128, 'disks': 512, 'volumes': 64}
FLAT_CAPS = {'disk': 48, 'array': 32, 'volume': 32}
FLAT_METRICS = {
    'disk':   ('temperature_c', 'status_code', 'power_on_hours', 'reallocated_sectors', 'pending_sectors',
               'uncorrectable_sectors', 'wear_used_percent', 'io_errors'),
    'array':  ('status_code', 'degraded', 'sync_percent'),
    'volume': ('status_code',),
}
SID_MAX_CHARS = 32

DISK_FIELDS = ('id', 'model', 'serial', 'size_bytes', 'media', 'bus', 'status', 'health_source', 'smart_passed',
               'temperature_c', 'power_on_hours', 'reallocated_sectors', 'pending_sectors',
               'uncorrectable_sectors', 'read_errors_uncorrected', 'write_errors_uncorrected',
               'wear_used_percent', 'remaining_life_percent', 'bad_sectors', 'io_errors', 'device_state',
               'in_standby', 'arrays')
ARRAY_FIELDS = ('id', 'kind', 'name', 'level', 'role', 'state', 'status', 'members_total', 'members_active',
                'degraded', 'sync_action', 'sync_percent', 'sync_speed_kbps', 'sync_eta_seconds',
                'mismatch_cnt', 'size_bytes', 'faulty_members', 'members')

# md levels: how many missing members still leave the data readable
_MD_REDUNDANCY = {'raid1': None, 'raid4': 1, 'raid5': 1, 'raid6': 2, 'raid10': 2}
_MD_REBUILD = ('recover', 'resync', 'reshape')
_MD_SCRUB = ('check', 'repair')
# array_state values of a working md array that it moves between by itself:
# 'active' on the first write, 'clean' again once writes stop for
# safe_mode_delay (~200 ms). The signature treats them as one state, or any
# array that is written to now and then would be a 'change' on most runs.
# ('auto-read-only' is /proc/mdstat's name for read-auto.)
_MD_RUNNING_STATES = ('clean', 'active', 'active-idle', 'write-pending', 'read-auto', 'auto-read-only')
_DSM_SYSTEM_ARRAYS = {'md0': 'system', 'md1': 'swap'}

_MDSTAT_HEAD = re.compile(r'^(md\d+)\s*:\s*(\S+)\s*(.*)$')
_MDSTAT_MEMBER = re.compile(r'^([A-Za-z0-9_-]+)\[(\d+)\]((?:\([A-Z]\))*)$')
_MDSTAT_COUNTS = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
_MDSTAT_PROGRESS = re.compile(r'(recovery|resync|check|repair|reshape)\s*=\s*([\d.]+)%'
                              r'(?:.*?finish=([\d.]+)min)?(?:.*?speed=(\d+)K/sec)?')
_MDSTAT_BLOCKS = re.compile(r'^\s*(\d+)\s+blocks')


def _wall_time() -> float:
    return time.time()


def _read(path: str) -> Optional[str]:
    """A small sysfs/procfs file's text, stripped, or None if it can't be read."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str, base: int = 10) -> Optional[int]:
    text = _read(path)
    try:
        return int(text, base) if text not in (None, '', 'none') else None
    except ValueError:
        return None


def _text(value, limit: int = TEXT_MAX_CHARS) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return '<1m'
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 60}m"


# ---------------------------------------------------------------------------
# md RAID
# ---------------------------------------------------------------------------

def _member_disk(sys_block: str, member: str) -> str:
    """The whole disk a member partition belongs to ('sata1p5' -> 'sata1', 'sda3' -> 'sda'); itself otherwise."""
    try:
        for disk in os.listdir(sys_block):
            if disk != member and os.path.isdir(os.path.join(sys_block, disk, member)):
                return disk
    except OSError:
        pass
    return member


def read_md(sys_root: Optional[str] = None, proc_root: Optional[str] = None):
    """
    (status, arrays) of the md arrays from sysfs, else from /proc/mdstat:
    'ok' (also with no arrays), or 'unavailable' when neither can be read
    (no md driver). Each array is the raw state classify() judges.
    """
    sys_block = os.path.join(sys_root or SYS_ROOT, 'block')
    mdstat = _read(os.path.join(proc_root or PROC_ROOT, 'mdstat'))
    try:
        names = sorted(n for n in os.listdir(sys_block) if re.match(r'^md\d+$', n))
    except OSError:
        names = None
    arrays = []
    for name in names or []:
        md = os.path.join(sys_block, name, 'md')
        state = _read(os.path.join(md, 'array_state'))
        if state is None:
            arrays = None           # sysfs not readable: use /proc/mdstat
            break
        if _read(os.path.join(md, 'level')) == 'container':
            continue                # IMSM / DDF metadata container: always 'inactive', holds no data itself
        members, faulty, active, replace = [], [], 0, False
        try:
            entries = sorted(e for e in os.listdir(md) if e.startswith('dev-'))
        except OSError:
            entries = []
        for entry in entries:
            flags = (_read(os.path.join(md, entry, 'state')) or '').split(',')
            disk = _member_disk(sys_block, entry[4:])
            members.append(disk)
            if 'faulty' in flags:
                faulty.append(disk)
            elif 'in_sync' in flags:
                active += 1
            replace = replace or 'want_replacement' in flags
        done = total = None
        completed = _read(os.path.join(md, 'sync_completed')) or ''
        if '/' in completed:
            try:
                done, total = (int(v) for v in completed.split('/'))
            except ValueError:
                done = total = None
        sectors = _read_int(os.path.join(sys_block, name, 'size'))
        arrays.append({
            'id': name, 'kind': 'md', 'name': None, 'level': _read(os.path.join(md, 'level')),
            'state': state, 'members_total': _read_int(os.path.join(md, 'raid_disks')),
            'members_active': active, 'degraded': _read_int(os.path.join(md, 'degraded')),
            'sync_action': _read(os.path.join(md, 'sync_action')),
            '_done': done, '_total': total, 'sync_speed_kbps': _read_int(os.path.join(md, 'sync_speed')),
            'mismatch_cnt': _read_int(os.path.join(md, 'mismatch_cnt')),
            'size_bytes': None if sectors is None else sectors * blockdev.SECTOR_BYTES,
            'faulty_members': faulty, 'members': members, '_want_replacement': replace,
        })
    if arrays is not None and names is not None and (names or mdstat is not None):
        return 'ok', [_md_progress(a) for a in arrays]
    if mdstat is None:
        return 'unavailable', []
    return 'ok', parse_mdstat(mdstat)


def _md_progress(array: dict) -> dict:
    """sync_percent and sync_eta_seconds from sync_completed (sectors) and sync_speed (K/s)."""
    done, total = array.pop('_done', None), array.pop('_total', None)
    array['sync_percent'] = array['sync_eta_seconds'] = None
    if done is not None and total:
        array['sync_percent'] = round(100.0 * done / total, 1)
        speed = array.get('sync_speed_kbps')
        if speed:
            array['sync_eta_seconds'] = int((total - done) * blockdev.SECTOR_BYTES / (speed * 1024))
    return array


def parse_mdstat(text: str) -> List[dict]:
    """The md arrays from /proc/mdstat (fallback when sysfs can't be read)."""
    arrays, current = [], None
    for line in text.splitlines():
        head = _MDSTAT_HEAD.match(line)
        if head:
            state, rest = head.group(2), head.group(3).split()
            while rest and rest[0].startswith('('):          # (read-only), (auto-read-only)
                state = rest.pop(0).strip('()')
            level = rest.pop(0) if rest and not _MDSTAT_MEMBER.match(rest[0]) else None
            members, faulty = [], []
            for token in rest:
                m = _MDSTAT_MEMBER.match(token)
                if m:
                    members.append(m.group(1))
                    if '(F)' in m.group(3):
                        faulty.append(m.group(1))
            current = {'id': head.group(1), 'kind': 'md', 'name': None, 'level': level, 'state': state,
                       'members_total': None, 'members_active': None, 'degraded': None,
                       'sync_action': 'idle' if state == 'active' else None, 'sync_percent': None,
                       'sync_speed_kbps': None, 'sync_eta_seconds': None, 'mismatch_cnt': None,
                       'size_bytes': None, 'faulty_members': faulty, 'members': members,
                       '_want_replacement': False}
            arrays.append(current)
            continue
        if current is None:
            continue
        blocks = _MDSTAT_BLOCKS.match(line)
        if blocks:
            current['size_bytes'] = int(blocks.group(1)) * 1024
            if 'super external:' in line and current['level'] is None:
                arrays.remove(current)          # IMSM / DDF metadata container
                current = None
                continue
        counts = _MDSTAT_COUNTS.search(line)
        if counts:
            total, active = int(counts.group(1)), int(counts.group(2))
            current.update(members_total=total, members_active=active, degraded=max(0, total - active))
        progress = _MDSTAT_PROGRESS.search(line)
        if progress:
            action = progress.group(1)
            current['sync_action'] = 'recover' if action == 'recovery' else action
            current['sync_percent'] = float(progress.group(2))
            if progress.group(3):
                current['sync_eta_seconds'] = int(float(progress.group(3)) * 60)
            if progress.group(4):
                current['sync_speed_kbps'] = int(progress.group(4))
    return arrays


def read_zpool(run=None, which=None, log: Optional[logging.Logger] = None):
    """(status, pools) from `zpool list`, or (None, []) when zpool isn't installed."""
    log = log or logger
    exe = (which or shutil.which)('zpool')
    if not exe:
        return None, []
    try:
        proc = (run or subprocess.run)([exe, 'list', '-H', '-o', 'name,health'], capture_output=True,
                                       text=True, timeout=ZPOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning(f"Storage health: zpool list took longer than {ZPOOL_TIMEOUT}s")
        return 'timeout', []
    except OSError as e:
        log.warning(f"Storage health: cannot run zpool ({e})")
        return 'error', []
    if proc.returncode != 0:
        log.warning(f"Storage health: zpool list exited with {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return 'error', []
    pools = []
    for line in (proc.stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 2 and parts[0].strip():
            pool = parts[0].strip()
            pools.append({'id': _text(pool, ID_MAX_CHARS), 'kind': 'zpool', 'name': _text(pool), 'level': None,
                          'state': parts[1].strip().lower(), 'members_total': None, 'members_active': None,
                          'degraded': None, 'sync_action': None, 'sync_percent': None, 'sync_speed_kbps': None,
                          'sync_eta_seconds': None, 'mismatch_cnt': None, 'size_bytes': None,
                          'faulty_members': [], 'members': []})
    return 'ok', pools


# ---------------------------------------------------------------------------
# Disks, temperatures, volumes (Linux, DSM)
# ---------------------------------------------------------------------------

def _serial(device_dir: str) -> Optional[str]:
    """device/serial (NVMe), else the unit serial number page (SCSI VPD 0x80) when readable."""
    serial = _read(os.path.join(device_dir, 'serial'))
    if serial:
        return _text(serial)
    try:
        with open(os.path.join(device_dir, 'vpd_pg80'), 'rb') as f:
            page = f.read(260)
    except OSError:
        return None
    if len(page) > 4 and page[1] == 0x80:
        return _text(page[4:4 + page[3]].decode('ascii', 'replace'))
    return None


def _bus(sys_block: str, name: str) -> Optional[str]:
    try:
        path = os.path.realpath(os.path.join(sys_block, name))
    except OSError:
        path = ''
    if name.startswith('nvme'):
        return 'nvme'
    if '/usb' in path:
        return 'usb'
    if '/ata' in path or name.startswith('sata'):
        return 'sata'
    if '/virtio' in path or name.startswith('vd'):
        return 'virtio'
    if name.startswith('mmcblk'):
        return 'mmc'
    return None


def read_block_devices(sys_root: Optional[str] = None):
    """(status, disks) for the physical disks in /sys/block (the same disks the I/O counters count)."""
    sys_block = os.path.join(sys_root or SYS_ROOT, 'block')
    if not os.path.isdir(sys_block):
        return 'unavailable', []
    disks = []
    for dev in blockdev.list_block_devices(sys_block):
        if dev['kind'] != 'disk':
            continue
        name = dev['name']
        device = os.path.join(sys_block, name, 'device')
        media = 'nvme' if name.startswith('nvme') else (
            None if dev['rotational'] is None else ('hdd' if dev['rotational'] else 'ssd'))
        disk = {field: None for field in DISK_FIELDS}
        disk.update(id=name, model=_text(_read(os.path.join(device, 'model'))), serial=_serial(device),
                    size_bytes=dev['size_bytes'], media=media, bus=_bus(sys_block, name),
                    health_source='sysfs', device_state=_read(os.path.join(device, 'state')),
                    io_errors=_read_int(os.path.join(device, 'ioerr_cnt'), 0), arrays=[])
        disks.append(disk)
    return 'ok', disks


def read_hwmon(sys_root: Optional[str] = None) -> Tuple[str, Dict[str, float]]:
    """(status, {disk: degrees C}) from drivetemp and nvme hwmon sensors; 'unavailable' without any."""
    base = os.path.join(sys_root or SYS_ROOT, 'class', 'hwmon')
    try:
        sensors = sorted(os.listdir(base))
    except OSError:
        return 'unavailable', {}
    temps = {}
    for sensor in sensors:
        path = os.path.join(base, sensor)
        kind = _read(os.path.join(path, 'name'))
        if kind not in ('drivetemp', 'nvme'):
            continue
        millideg = _read_int(os.path.join(path, 'temp1_input'))
        if millideg is None:
            continue
        for disk in _hwmon_disks(os.path.join(path, 'device'), kind):
            temps[disk] = round(millideg / 1000.0, 1)
    return ('ok' if temps else 'unavailable'), temps


def _hwmon_disks(device: str, kind: str) -> List[str]:
    """The block devices behind a hwmon sensor: device/block/<sd*> (drivetemp), nvme namespaces (nvme)."""
    def listdir(path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []
    if kind == 'drivetemp':
        return listdir(os.path.join(device, 'block'))
    names = [n for n in listdir(device) if re.match(r'^nvme\d+n\d+$', n)]
    for ctrl in listdir(os.path.join(device, 'nvme')):
        names += [n for n in listdir(os.path.join(device, 'nvme', ctrl)) if re.match(r'^nvme\d+n\d+$', n)]
    return names


def _mount_table(text: str) -> List[Tuple[str, str, str, List[str]]]:
    """(source, mount point, fstype, options) per line of /proc/mounts or /etc/fstab."""
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[0].startswith('#'):
            unescape = disk_usage._unescape_mount_field
            rows.append((unescape(fields[0]), unescape(fields[1]), fields[2], fields[3].split(',')))
    return rows


def _superblock_options(text: str) -> dict:
    """
    {mount point: the FILESYSTEM's own options} from /proc/self/mountinfo
    (the fields after " - ": fstype, source, super options). The last entry at
    a mount point is the one in use.

    /proc/mounts shows the options of the mount as this process sees it, and a
    systemd sandbox (ProtectSystem=strict, as in the collector's units) remounts
    every filesystem read-only for the service alone. The superblock options
    stay 'rw' then; they turn 'ro' when the filesystem itself is read-only,
    e.g. after the kernel remounted it because of errors (errors=remount-ro).
    """
    options = {}
    for line in text.splitlines():
        left, sep, right = line.partition(' - ')
        fields, tail = left.split(), right.split()
        if sep and len(fields) >= 5 and len(tail) >= 3:
            options[disk_usage._unescape_mount_field(fields[4])] = tail[2].split(',')
    return options


def read_volumes(config: dict, platform: str, proc_root: Optional[str] = None,
                 fstab: Optional[str] = None):
    """
    (status, volumes) for the volume roots: config "volumes", else the
    platform's (DSM /volumeN mounts; Linux real local filesystems, as the
    disk run discovers them). read_only is the filesystem's own state
    (/proc/self/mountinfo superblock options, see _superblock_options), or
    the /proc/mounts option where mountinfo can't be read; a root that fstab
    mounts 'ro' is expected to be read-only.
    """
    text = _read(os.path.join(proc_root or PROC_ROOT, 'mounts'))
    if text is None:
        return 'unavailable', []
    table = _mount_table(text)
    top = {}
    for row in table:                     # the last entry at a mount point is the one in use
        top[row[1]] = row
    superblock = _superblock_options(_read(os.path.join(proc_root or PROC_ROOT, 'self', 'mountinfo')) or '')
    expected_ro = {os.path.normpath(r[1]) for r in _mount_table(_read(fstab or FSTAB) or '') if 'ro' in r[3]}
    roots = [v for v in config.get('volumes') or [] if isinstance(v, str)]
    if not roots:
        if platform == 'synology':
            roots = sorted((mp for mp in top if re.match(r'^/volume\d+$', mp)), key=lambda p: int(p[7:]))
        else:
            roots = disk_usage.discover_linux_volumes(
                [disk_usage.Mount(src, mp, fs) for src, mp, fs, _ in table])
    volumes = []
    for root in roots:
        root = os.path.normpath(root)
        mount = top.get(root)
        if mount is None and platform != 'synology':
            # A configured folder: judged by the filesystem it is on
            parents = [mp for mp in top if root == mp or root.startswith(mp.rstrip('/') + '/')]
            mount = top[max(parents, key=len)] if parents else None
        volume = {'id': root[:ID_MAX_CHARS], 'fs': None, 'read_only': None, 'health': None, 'status': None,
                  '_mounted': mount is not None}
        if mount is not None:
            volume.update(fs=mount[2], read_only='ro' in superblock.get(mount[1], mount[3]),
                          _expected_ro=os.path.normpath(mount[1]) in expected_ro)
        volumes.append(volume)
    return 'ok', volumes


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# Read by PowerShell from the environment, never spliced into the script
PS_SINCE_ENV_VAR = 'LM_HEALTH_SINCE'          # ISO-8601 UTC start of the event window
PS_VOLUMES_ENV_VAR = 'LM_HEALTH_VOLUMES'      # drive letters, comma separated ('' = all)
EVENT_PROVIDERS = ('disk', 'Ntfs', 'Microsoft-Windows-Ntfs', 'storahci', 'stornvme', 'iaStorA', 'iaStorAVC')

PS_HEALTH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$out = [ordered]@{}
function Get-Names($values) { @($values | ForEach-Object { [string]$_ }) }
try {
    $disks = @()
    foreach ($d in @(Get-PhysicalDisk)) {
        $r = $null
        try { $r = $d | Get-StorageReliabilityCounter } catch { $r = $null }
        $disks += [ordered]@{
            DeviceId = [string]$d.DeviceId; FriendlyName = [string]$d.FriendlyName
            SerialNumber = [string]$d.SerialNumber; MediaType = [string]$d.MediaType
            BusType = [string]$d.BusType; Size = $d.Size; HealthStatus = [string]$d.HealthStatus
            OperationalStatus = Get-Names $d.OperationalStatus
            Temperature = $r.Temperature; TemperatureMax = $r.TemperatureMax
            ReadErrorsUncorrected = $r.ReadErrorsUncorrected; WriteErrorsUncorrected = $r.WriteErrorsUncorrected
            Wear = $r.Wear; PowerOnHours = $r.PowerOnHours
        }
    }
    $out.disks = $disks; $out.disks_status = 'ok'
} catch { $out.disks_status = 'error'; $out.disks_error = [string]$_ }
try {
    $vds = @()
    foreach ($v in @(Get-VirtualDisk)) {
        $vds += [ordered]@{
            FriendlyName = [string]$v.FriendlyName; HealthStatus = [string]$v.HealthStatus
            OperationalStatus = Get-Names $v.OperationalStatus
            ResiliencySettingName = [string]$v.ResiliencySettingName; Size = $v.Size
        }
    }
    $jobs = @()
    foreach ($j in @(Get-StorageJob)) {
        $jobs += [ordered]@{ Name = [string]$j.Name; JobState = [string]$j.JobState; PercentComplete = $j.PercentComplete }
    }
    $out.virtual_disks = $vds; $out.jobs = $jobs; $out.spaces_status = 'ok'
} catch { $out.spaces_status = 'error'; $out.spaces_error = [string]$_ }
try {
    $letters = @(([string]$env:LM_HEALTH_VOLUMES).Split(',') | Where-Object { $_ -ne '' })
    $vols = @()
    foreach ($v in @(Get-Volume)) {
        $letter = ([string]$v.DriveLetter).Trim([char]0)
        if ($letter.Length -eq 0) { continue }
        if ($letters.Count -gt 0 -and $letters -notcontains $letter) { continue }
        $vols += [ordered]@{
            DriveLetter = $letter; FileSystemType = [string]$v.FileSystemType
            HealthStatus = [string]$v.HealthStatus; OperationalStatus = Get-Names $v.OperationalStatus
        }
    }
    $out.volumes = $vols; $out.volumes_status = 'ok'
} catch { $out.volumes_status = 'error'; $out.volumes_error = [string]$_ }
$events = @{}
$failed = 0
$since = [DateTime]::Parse([string]$env:LM_HEALTH_SINCE, [Globalization.CultureInfo]::InvariantCulture)
foreach ($p in @('disk', 'Ntfs', 'Microsoft-Windows-Ntfs', 'storahci', 'stornvme', 'iaStorA', 'iaStorAVC')) {
    try {
        $filter = @{ LogName = 'System'; ProviderName = $p; StartTime = $since }
        foreach ($e in @(Get-WinEvent -FilterHashtable $filter -ErrorAction Stop)) {
            $key = (([string]$e.ProviderName).ToLower() -replace '^microsoft-windows-', '') + '/' + [string]$e.Id
            if ($events.ContainsKey($key)) { $events[$key] += 1 } else { $events[$key] = 1 }
        }
    } catch {
        if ([string]$_.FullyQualifiedErrorId -notlike 'NoMatching*') { $failed += 1 }
    }
}
$out.events = $events
if ($failed -eq 0) { $out.events_status = 'ok' } elseif ($failed -lt 7) { $out.events_status = 'partial' } else { $out.events_status = 'error' }
Write-Output ($out | ConvertTo-Json -Compress -Depth 5)
"""

# Enum values as PowerShell prints them ([string] casts) or as their numbers
_WIN_HEALTH = {'healthy': 'ok', '0': 'ok', 'warning': 'warning', '1': 'warning',
               'unhealthy': 'critical', '2': 'critical', 'unknown': 'unknown', '5': 'unknown'}
_WIN_MEDIA = {'hdd': 'hdd', '3': 'hdd', 'ssd': 'ssd', '4': 'ssd', 'scm': 'ssd', '5': 'ssd'}
_WIN_BUS = {'1': 'scsi', '2': 'atapi', '3': 'ata', '4': '1394', '5': 'ssa', '6': 'fibre_channel', '7': 'usb',
            '8': 'raid', '9': 'iscsi', '10': 'sas', '11': 'sata', '12': 'sd', '13': 'mmc', '14': 'virtual',
            '15': 'file_backed_virtual', '16': 'spaces', '17': 'nvme', '18': 'scm', '19': 'ufs'}
_WIN_FAILING = {'predictive failure', '5', 'lost communication', '13', 'failed media'}
_WIN_JOB_RUNNING = {'running', '4'}
_WIN_MINIPORTS = ('storahci', 'stornvme', 'iastora', 'iastoravc')


def _win_key(value) -> str:
    return re.sub(r'\s+', ' ', str(value if value is not None else '')).strip().lower()


def _win_list(value) -> List[str]:
    if value is None:
        return []
    return [_win_key(v) for v in (value if isinstance(value, list) else [value])]


def _win_number(value) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def run_powershell_health(config: dict, since: float, log: Optional[logging.Logger] = None) -> dict:
    """
    The parsed JSON of one PS_HEALTH_SCRIPT run, or {'error': status}
    ('timeout' / 'error' / 'unavailable'). PowerShell is killed after
    storage_health.timeout_seconds, before the collector's budget for this
    thread (that plus collector.HEALTH_BUDGET_MARGIN_SECONDS) runs out.
    """
    import base64
    log = log or logger
    timeout = settings.value(config, 'storage_health.timeout_seconds', log)
    env = dict(os.environ)
    env[PS_SINCE_ENV_VAR] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since))
    letters = [str(v).strip().rstrip(':\\/')[:1].upper() for v in config.get('volumes') or [] if str(v).strip()]
    env[PS_VOLUMES_ENV_VAR] = ','.join(sorted(set(letters)))
    encoded = base64.b64encode(PS_HEALTH_SCRIPT.encode('utf-16-le')).decode('ascii')
    try:
        proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                              capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        log.warning(f"Storage health: PowerShell took longer than {timeout:.0f}s (storage_health.timeout_seconds)")
        return {'error': 'timeout'}
    except OSError as e:
        log.warning(f"Storage health: cannot run PowerShell ({e})")
        return {'error': 'unavailable'}
    except Exception as e:
        log.warning(f"Storage health: PowerShell failed ({e})")
        return {'error': 'error'}
    return parse_powershell_output(proc.stdout, log)


def parse_powershell_output(stdout, log: Optional[logging.Logger] = None) -> dict:
    """The JSON object on the last stdout line that starts with '{', or {'error': 'error'}."""
    log = log or logger
    text = stdout.decode('utf-8', 'replace') if isinstance(stdout, bytes) else (stdout or '')
    for line in reversed(text.splitlines()):
        line = line.strip().lstrip('\ufeff')     # a UTF-8 BOM before the JSON
        if line.startswith('{'):
            try:
                obj = json.loads(line)
            except ValueError:
                break
            if isinstance(obj, dict):
                return obj
            break
    log.warning(f"Storage health: no result from PowerShell: {text.strip()[:200]!r}")
    return {'error': 'error'}


def windows_components(obj: dict) -> dict:
    """{sources, disks, arrays, volumes, events} from the PowerShell result (enums as names or numbers)."""
    if 'error' in obj and len(obj) == 1:
        status = obj['error']
        return {'sources': {'windows_storage': status, 'events': status},
                'disks': [], 'arrays': [], 'volumes': [], 'events': None}
    blocks = [obj.get(k) for k in ('disks_status', 'spaces_status', 'volumes_status')]
    ok = sum(1 for b in blocks if b == 'ok')
    storage = 'ok' if ok == len(blocks) else ('partial' if ok else 'error')
    disks = []
    for d in obj.get('disks') or []:
        if not isinstance(d, dict):
            continue
        bus = _win_key(d.get('BusType'))
        bus = _WIN_BUS.get(bus, bus.replace(' ', '_') or None)
        operational = _win_list(d.get('OperationalStatus'))
        temp = _win_number(d.get('Temperature'))
        temp_max = _win_number(d.get('TemperatureMax'))
        disk = {field: None for field in DISK_FIELDS}
        disk.update(
            id=f"pd{_text(d.get('DeviceId'), 32) or '?'}", model=_text(d.get('FriendlyName')),
            serial=_text(d.get('SerialNumber')), size_bytes=_win_number(d.get('Size')),
            media='nvme' if bus == 'nvme' else _WIN_MEDIA.get(_win_key(d.get('MediaType'))),
            bus=bus, health_source='windows',
            smart_passed=False if 'predictive failure' in operational or '5' in operational else None,
            temperature_c=temp if temp else None,
            power_on_hours=_win_number(d.get('PowerOnHours')),
            read_errors_uncorrected=_win_number(d.get('ReadErrorsUncorrected')),
            write_errors_uncorrected=_win_number(d.get('WriteErrorsUncorrected')),
            wear_used_percent=_win_number(d.get('Wear')), device_state=', '.join(operational) or None,
            arrays=[], _win_health=_WIN_HEALTH.get(_win_key(d.get('HealthStatus')), 'unknown'),
            _win_failing=sorted(set(operational) & _WIN_FAILING),
            _temp_max=temp_max if temp_max else None)
        disks.append(disk)
    jobs = [j for j in obj.get('jobs') or [] if isinstance(j, dict)
            and _win_key(j.get('JobState')) in _WIN_JOB_RUNNING]
    arrays = []
    for v in obj.get('virtual_disks') or []:
        if not isinstance(v, dict):
            continue
        operational = _win_list(v.get('OperationalStatus'))
        array = {field: None for field in ARRAY_FIELDS}
        array.update(id=_text(v.get('FriendlyName'), ID_MAX_CHARS) or '?', kind='storage_space',
                     name=_text(v.get('FriendlyName')), level=_text(v.get('ResiliencySettingName')), role='data',
                     state=', '.join(operational) or None, size_bytes=_win_number(v.get('Size')),
                     degraded=1 if 'degraded' in operational or '3' in operational else 0,
                     faulty_members=[], members=[],
                     _win_health=_WIN_HEALTH.get(_win_key(v.get('HealthStatus')), 'unknown'))
        arrays.append(array)
    unhealthy = [a for a in arrays if a['_win_health'] != 'ok']
    if len(jobs) == 1 and len(unhealthy) == 1:
        # One repair job and one unhealthy virtual disk: the job is its rebuild
        unhealthy[0].update(sync_action='repair', sync_percent=_win_number(jobs[0].get('PercentComplete')))
    volumes = []
    for v in obj.get('volumes') or []:
        if not isinstance(v, dict) or not v.get('DriveLetter'):
            continue
        volumes.append({'id': f"{str(v['DriveLetter'])[:1].upper()}:", 'fs': _text(v.get('FileSystemType')),
                        'read_only': None, 'health': _win_key(v.get('HealthStatus')) or None, 'status': None,
                        '_mounted': True,
                        '_win_health': _WIN_HEALTH.get(_win_key(v.get('HealthStatus')), 'unknown')})
    events = obj.get('events') if isinstance(obj.get('events'), dict) else None
    if events is not None:
        events = {str(k)[:64]: int(n) for k, n in events.items() if _win_number(n) is not None}
    return {'sources': {'windows_storage': storage, 'events': obj.get('events_status') or 'error'},
            'disks': disks, 'arrays': arrays, 'volumes': volumes, 'events': events}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _worse(a: str, b: str) -> str:
    """The worse of two statuses; unknown only beats nothing."""
    if a == 'unknown':
        return b
    if b == 'unknown':
        return a
    return a if STATUS_ORDER[a] >= STATUS_ORDER[b] else b


class _Judge:
    """Collects each component's status and the reasons, worst first."""

    def __init__(self):
        self.reasons = []        # (severity, order, text)

    def note(self, status: str, text: str):
        self.reasons.append((STATUS_ORDER.get(status, -1), len(self.reasons), text[:REASON_MAX_CHARS]))

    def sorted_reasons(self) -> List[str]:
        return [text for _, _, text in sorted(self.reasons, key=lambda r: (-r[0], r[1]))][:MAX_REASONS]


def _classify_md(array: dict, platform: str, judge: _Judge) -> str:
    aid, state = array['id'], (array.get('state') or '').lower()
    level = (array.get('level') or '').lower()
    degraded = array.get('degraded') or 0
    action = array.get('sync_action') or 'idle'
    progress = '' if array.get('sync_percent') is None else f" {array['sync_percent']:.1f}%"
    eta = '' if not array.get('sync_eta_seconds') else f" (ETA {_duration(array['sync_eta_seconds'])})"
    role = array.get('role') or 'data'
    if role != 'data':
        # DSM's system and swap arrays span every bay: missing members are empty bays
        if array.get('faulty_members'):
            judge.note('warning', f"{aid} ({role}) member {', '.join(array['faulty_members'])} faulty")
            return 'warning'
        return 'ok'
    if state in ('inactive', 'clear', 'broken', 'failed') or (level in ('raid0', 'linear') and degraded):
        judge.note('critical', f"{aid} {state or 'failed'}")
        return 'critical'
    status = 'ok'
    if degraded:
        total = array.get('members_total')
        redundancy = _MD_REDUNDANCY.get(level, 1)
        if redundancy is None:              # raid1: n-1 copies can go
            redundancy = (total or 2) - 1
        active = array.get('members_active')
        counts = f": {active} of {total} members active" if active is not None and total else ''
        if action in _MD_REBUILD:
            judge.note('warning', f"{aid} rebuilding{progress}{eta}")
            status = 'warning'
        elif degraded >= redundancy:
            judge.note('critical', f"{aid} degraded with no redundancy left{counts}")
            return 'critical'
        else:
            judge.note('warning', f"{aid} degraded{counts}")
            status = 'warning'
    elif action in _MD_REBUILD:
        judge.note('warning', f"{aid} {action}{progress}{eta}")
        status = 'warning'
    if array.get('faulty_members'):
        judge.note('warning', f"{aid} member {', '.join(array['faulty_members'])} faulty")
        status = _worse(status, 'warning')
    if array.get('_want_replacement'):
        judge.note('warning', f"{aid} has a member marked for replacement")
        status = _worse(status, 'warning')
    if action in _MD_SCRUB and status == 'ok':
        judge.note('ok', f"{aid} data scrub ({action}){progress}")
    return status


def _classify_zpool(pool: dict, judge: _Judge) -> str:
    health = (pool.get('state') or '').upper()
    if health == 'ONLINE':
        return 'ok'
    status = 'warning' if health == 'DEGRADED' else 'critical'
    judge.note(status, f"zpool {pool['id']} {health or 'unknown'}")
    return status


def _thresholds(disk: dict, temperature_c: dict) -> Optional[List[float]]:
    if disk.get('_temp_max'):
        return [disk['_temp_max'] - 5, disk['_temp_max']]
    media = disk.get('media')
    if media is None:
        return None
    return temperature_c['hdd'] if media == 'hdd' else temperature_c['ssd']


def _rising(baselines: dict, key: str, value, now: float) -> bool:
    """Whether an error counter rose within RISE_HOLD_SECONDS (a decrease starts a new baseline)."""
    if value is None:
        return False
    base = baselines.get(key)
    if not isinstance(base, dict) or not isinstance(base.get('v'), (int, float)) or value < base['v']:
        baselines[key] = {'v': value, 'rose': None}
        return False
    if value > base['v']:
        base.update(v=value, rose=now)
    rose = base.get('rose')
    return isinstance(rose, (int, float)) and 0 <= now - rose < RISE_HOLD_SECONDS


def _classify_disk(disk: dict, temperature_c: dict, baselines: dict, now: float, judge: _Judge) -> str:
    did, status, assessed = disk['id'], 'ok', False
    state = (disk.get('device_state') or '').lower()
    if disk.get('health_source') == 'sysfs' and state:
        assessed = True
        if state in ('offline', 'transport-offline', 'dead'):
            judge.note('critical', f"{did} {state}")
            status = 'critical'
    if disk.get('io_errors') is not None:
        assessed = True     # reported, but no status of its own: probes of unsupported commands count too
    win = disk.get('_win_health')
    if win is not None:
        assessed = assessed or win != 'unknown'
        if disk.get('_win_failing'):
            judge.note('critical', f"{did} {', '.join(disk['_win_failing'])}")
            status = 'critical'
        elif win in ('warning', 'critical'):
            judge.note(win, f"{did} health {'Unhealthy' if win == 'critical' else 'Warning'}")
            status = _worse(status, win)
        for field, what in (('read_errors_uncorrected', 'uncorrected read'),
                            ('write_errors_uncorrected', 'uncorrected write')):
            if _rising(baselines, f"{did}:{field}", disk.get(field), now):
                judge.note('warning', f"{did} {what} errors rising ({disk[field]:g})")
                status = _worse(status, 'warning')
    wear = disk.get('wear_used_percent')
    if wear is not None and wear >= 90:
        judge.note('warning', f"{did} wear {wear:g}% used")
        status = _worse(status, 'warning')
    temp, limits = disk.get('temperature_c'), _thresholds(disk, temperature_c)
    if temp is not None and limits:
        assessed = True
        if temp >= limits[1]:
            judge.note('critical', f"{did} temperature {temp:g} C (critical at {limits[1]:g})")
            status = 'critical'
        elif temp >= limits[0]:
            judge.note('warning', f"{did} temperature {temp:g} C (warning at {limits[0]:g})")
            status = _worse(status, 'warning')
    return status if assessed or status != 'ok' else 'unknown'


def _classify_volume(volume: dict, platform: str, judge: _Judge) -> str:
    vid = volume['id']
    win = volume.get('_win_health')
    if win is not None:
        if win in ('warning', 'critical'):
            judge.note(win, f"{vid} health {'Unhealthy' if win == 'critical' else 'Warning'}")
        return win
    if not volume.get('_mounted'):
        judge.note('critical', f"{vid} not mounted")
        return 'critical'
    if volume.get('read_only') and not volume.get('_expected_ro'):
        judge.note('critical', f"{vid} mounted read-only")
        return 'critical'
    return 'ok' if volume.get('read_only') is not None else 'unknown'


def _classify_events(events: Optional[dict], hours: float, judge: _Judge) -> str:
    """Windows System-log disk events counted since the previous full run."""
    if not events:
        return 'ok'
    status = 'ok'

    def count(provider, *ids):
        return sum(events.get(f"{provider}/{i}", 0) for i in ids)
    if count('disk', 52):
        judge.note('critical', f"disk event 52 (imminent failure) x{count('disk', 52)}")
        status = 'critical'
    for provider, ids in (('disk', (7, 11, 51, 154)), ('ntfs', (50, 55, 98))):
        for i in ids:
            n = count(provider, i)
            if n:
                judge.note('warning', f"{provider} event {i} x{n}")
                status = _worse(status, 'warning')
    hours = max(hours, 1.0)
    for provider, i in [('disk', 153)] + [(m, 129) for m in _WIN_MINIPORTS]:
        n = count(provider, i)
        if n and n / hours >= 10:
            judge.note('warning', f"{provider} event {i} x{n} ({n / hours:.0f}/h)")
            status = _worse(status, 'warning')
    return status


def classify(arrays: List[dict], disks: List[dict], volumes: List[dict], platform: str,
             temperature_c: dict, baselines: dict, now: float, events: Optional[dict] = None,
             event_hours: float = 1.0):
    """
    Set every component's 'status'; return (overall_status, status_reasons,
    events_status or None). baselines ({key: {v, rose}}) is updated in place.
    """
    judge = _Judge()
    overall, assessed = 'unknown', False
    for array in arrays:
        if array.get('kind') == 'md':
            array['status'] = _classify_md(array, platform, judge)
        elif array.get('kind') == 'zpool':
            array['status'] = _classify_zpool(array, judge)
        else:
            win = array.get('_win_health') or 'unknown'
            if win in ('warning', 'critical'):
                what = 'rebuilding' if array.get('sync_action') else ('degraded' if array.get('degraded')
                                                                    else f"health {win}")
                pct = '' if array.get('sync_percent') is None else f" {array['sync_percent']:g}%"
                judge.note(win, f"{array['id']} {what}{pct}")
            array['status'] = win
    for disk in disks:
        disk['status'] = _classify_disk(disk, temperature_c, baselines, now, judge)
    for volume in volumes:
        volume['status'] = _classify_volume(volume, platform, judge)
    events_status = None
    if events is not None:
        events_status = _classify_events(events, event_hours, judge)
    for status in [c['status'] for c in arrays + disks + volumes] + ([events_status] if events_status else []):
        if status != 'unknown':
            assessed = True
            overall = _worse(overall, status)
    return (overall if assessed else 'unknown'), judge.sorted_reasons(), events_status


# ---------------------------------------------------------------------------
# Flattening, signature, the message
# ---------------------------------------------------------------------------

def sid(component_id: str) -> str:
    """Key form of an id: lowercase, [a-z0-9] kept, the rest '_' ('/volume1' -> 'volume1', 'E:' -> 'e', '/' -> 'root')."""
    text = re.sub(r'[^a-z0-9]', '_', str(component_id).lower()).strip('_')[:SID_MAX_CHARS]
    return text or 'root'


def flatten(arrays: List[dict], disks: List[dict], volumes: List[dict]) -> Tuple[dict, bool]:
    """
    ({<kind>_<sid>_<metric>: number}, truncated): per component kind the
    first FLAT_CAPS components (USB disks never), non-null values only. A
    key two components would share, or a component over the cap, sets
    truncated.
    """
    flat, truncated = {}, False
    for kind, items in (('disk', [d for d in disks if d.get('bus') != 'usb']), ('array', arrays),
                        ('volume', volumes)):
        if len(items) > FLAT_CAPS[kind]:
            truncated = True
        for item in items[:FLAT_CAPS[kind]]:
            values = dict(item, status_code=STATUS_CODES.get(item.get('status')))
            for metric in FLAT_METRICS[kind]:
                value = values.get(metric)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                key = f"{kind}_{sid(item['id'])}_{metric}"
                if key in flat:
                    truncated = True
                    continue
                flat[key] = value
    return flat, truncated


def _signature_state(kind: str, component: dict) -> str:
    state = str(component.get('state'))
    if kind == 'array' and component.get('kind') == 'md' and state.lower() in _MD_RUNNING_STATES:
        return 'running'
    return state


def signature(overall: str, components: List[Tuple[str, dict]], sources: dict, events_status) -> str:
    """
    sha1 over overall_status, the sorted (kind, id, status, state,
    sync_action) and the source statuses. A working md array's clean /
    active / ... states count as one (_MD_RUNNING_STATES).
    """
    items = sorted((kind, str(c.get('id')), str(c.get('status')), _signature_state(kind, c),
                    str(c.get('sync_action')))
                   for kind, c in components)
    blob = json.dumps([overall, items, sorted(sources.items()), events_status], separators=(',', ':'))
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()


def _public(component: dict, fields=None) -> dict:
    """The wire form of a component: internal '_' keys dropped, the given fields in order."""
    if fields is None:
        return {k: v for k, v in component.items() if not k.startswith('_')}
    return {f: component.get(f) for f in fields}


def _load_state(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _number(value) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _linux_sources(config: dict, platform: str, full_due: bool, cached: dict, log: logging.Logger):
    """(sources, arrays, disks, volumes, cached) from the Tier A readers (and hwmon when due)."""
    sources = {}
    sources['md'], arrays = read_md()
    zpool_status, pools = read_zpool(log=log)
    if zpool_status is not None:
        sources['zpool'] = zpool_status
        arrays += pools
    sources['sysfs'], disks = read_block_devices()
    sources['mounts'], volumes = read_volumes(config, platform)
    if full_due:
        hwmon_status, temps = read_hwmon()
        cached = {'hwmon': {'status': hwmon_status, 'temps': temps}}
    else:
        hwmon = cached.get('hwmon') if isinstance(cached.get('hwmon'), dict) else {}
        hwmon_status, temps = hwmon.get('status') or 'unavailable', hwmon.get('temps') or {}
    sources['hwmon'] = hwmon_status
    swaps = _read(os.path.join(PROC_ROOT, 'swaps')) or ''
    for array in arrays:
        if array['kind'] != 'md':
            array.setdefault('role', 'data')
            continue
        if platform == 'synology' and array['id'] in _DSM_SYSTEM_ARRAYS:
            array['role'] = _DSM_SYSTEM_ARRAYS[array['id']]
        else:
            array['role'] = 'swap' if re.search(rf"^/dev/{array['id']}\s", swaps, re.M) else 'data'
    by_disk = {}
    for array in arrays:
        for member in array.get('members') or []:
            by_disk.setdefault(member, []).append(array['id'])
    for disk in disks:
        disk['arrays'] = by_disk.get(disk['id'], [])
        if disk['id'] in temps:
            disk['temperature_c'] = _number(temps[disk['id']])
    return sources, arrays, disks, volumes, cached


def maybe_collect(config: dict, data_dir: str, name: str, force: bool = False,
                  logger: Optional[logging.Logger] = None):
    """
    (payload, state): the 'storage_health' message data when one is due
    (signature changed: 'change'; interval elapsed: 'interval'; force:
    'manual'), else None; and the new storage_health_state.json
    ({last_sent, last_full, signature, cached, baselines, last_event_time}).
    Reads that state file but never writes a file.
    """
    log = logger or logging.getLogger(__name__)
    platform = platform_info.platform_kind(config)
    interval = settings.value(config, 'storage_health.interval_seconds', log)
    temperature_c = settings.value(config, 'storage_health.temperature_c', log)
    ignore = set(settings.value(config, 'storage_health.ignore', log))
    now = _wall_time()
    previous = _load_state(os.path.join(data_dir, name, STATE_FILE))
    last_full = _number(previous.get('last_full'))
    full_due = force or last_full is None or not 0 <= now - last_full < interval
    cached = previous.get('cached') if isinstance(previous.get('cached'), dict) else {}
    baselines = previous.get('baselines') if isinstance(previous.get('baselines'), dict) else {}
    last_event_time = _number(previous.get('last_event_time'))

    events, event_hours = None, 1.0
    if platform == 'windows':
        if full_due:
            since = last_event_time if last_event_time and last_event_time < now else now - FIRST_EVENT_LOOKBACK
            cached = {'windows': run_powershell_health(config, since, log), 'since': since, 'until': now}
            last_event_time = now
        win = windows_components(cached.get('windows') or {'error': 'error'})
        sources, arrays, disks, volumes = win['sources'], win['arrays'], win['disks'], win['volumes']
        events = win['events']
        since, until = _number(cached.get('since')), _number(cached.get('until'))
        event_hours = (until - since) / 3600.0 if since and until and until > since else 1.0
    else:
        sources, arrays, disks, volumes, cached = _linux_sources(config, platform, full_due, cached, log)

    arrays = [a for a in arrays if a['id'] not in ignore][:LIST_CAPS['arrays']]
    disks = [d for d in disks if d['id'] not in ignore][:LIST_CAPS['disks']]
    volumes = [v for v in volumes if v['id'] not in ignore][:LIST_CAPS['volumes']]
    overall, reasons, events_status = classify(arrays, disks, volumes, platform, temperature_c, baselines,
                                               now, events, event_hours)
    components = [('array', a) for a in arrays] + [('disk', d) for d in disks] + [('volume', v) for v in volumes]
    digest = signature(overall, components, sources, events_status)

    if force:
        trigger = 'manual'
    elif digest != previous.get('signature'):
        trigger = 'change'
    elif not 0 <= now - (_number(previous.get('last_sent')) or 0) < interval:
        trigger = 'interval'
    else:
        trigger = None

    state = {'last_sent': now if trigger else _number(previous.get('last_sent')),
             'last_full': now if full_due else last_full, 'signature': digest, 'cached': cached,
             'baselines': baselines, 'last_event_time': last_event_time}
    summary = (f"Storage health: {overall} ({len(disks)} disk(s), {len(arrays)} array(s), "
               f"{len(volumes)} volume(s); {', '.join(f'{k} {v}' for k, v in sources.items())})")
    if trigger is None:
        log.info(f"{summary}; unchanged, not sent")
        return None, state
    log.info(f"{summary}; sent ({trigger})" + (f": {reasons[0]}" if reasons else ''))

    flat, truncated = flatten(arrays, disks, volumes)
    payload = {
        '_schema': SCHEMA_VERSION,
        'trigger': trigger,
        'overall_status': overall,
        'overall_status_code': STATUS_CODES.get(overall),
        'status_reasons': reasons,
        'sources': sources,
        'arrays': [_public(a, ARRAY_FIELDS) for a in arrays],
        'disks': [_public(d, DISK_FIELDS) for d in disks],
        'volumes': [_public(v, ('id', 'fs', 'read_only', 'health', 'status')) for v in volumes],
        'events': events if platform == 'windows' else None,
        '_flat_truncated': truncated,
        **flat,
    }
    return payload, state
