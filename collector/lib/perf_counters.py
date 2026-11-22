"""
Interval counters: the clock, CPU and disk I/O, averaged over the whole time
since the previous metrics run instead of a 1-second sample.

Each run reads cumulative OS counters and compares them with the previous
run's, kept in data/<name>/perf_snapshot.json (written by
collector.measure_counters, which also adds the per-process table from
proc_accounting). window() decides whether the two readings make a valid
window:

  ok      same boot, 5 s .. counter_max_window_seconds apart
  short   under 5 s (a run started by hand right after the timer's): nothing
          is reported and the previous snapshot is kept as the baseline
  stale   longer than counter_max_window_seconds (default 24 h): re-baseline
  reboot  the host rebooted since: a zero baseline, i.e. averages since boot,
          when the uptime is within the maximum window (stale otherwise).
          Without a boot id (Windows, the btime fallback) only a monotonic
          clock that went backwards counts as a reboot
  none    no usable previous snapshot (missing, corrupt, another version or
          counter source, or a counter that went backwards), or, without a
          boot id, a boot time that moved while the monotonic clock went on
          (a wall-clock step or a reboot: they can't be told apart)

Sources:
  Linux/DSM: /proc/uptime (mono, the clock process start times use),
             /proc/sys/kernel/random/boot_id (else btime from /proc/stat),
             the aggregate 'cpu' line of /proc/stat, and /proc/diskstats for
             the physical disks blockdev picks (md arrays, partitions and
             virtual devices are never summed).
  Windows:   GetTickCount64 (one system-wide clock for both the window and
             the boot time) and GetSystemTimes via ctypes, so the CPU average
             needs no psutil; disk counters need psutil (PhysicalDriveN).
  Elsewhere: nothing (callers fall back to the 1-second sample).

Units of the snapshot's counters: CPU in the source's ticks (cpu.hz per
second: SC_CLK_TCK on Linux, 10**7 on Windows), disk 'rs'/'ws' in the
source's units (512-byte sectors from procfs, bytes from psutil), 'rt'/'wt'/
'io' in milliseconds. /proc/diskstats prints the three time fields as 32-bit
unsigned ints on every kernel (the others are unsigned long, 32-bit only on a
32-bit kernel), so they wrap at 2**32 ms; disk_metrics undoes that.
"""
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from . import blockdev
from . import local_queue

logger = logging.getLogger(__name__)

PROC = '/proc'

SNAPSHOT_VERSION = 1
SNAPSHOT_FILE = 'perf_snapshot.json'

MIN_WINDOW_SECONDS = 5.0
DEFAULT_MAX_WINDOW_SECONDS = 86400
WALL_CLOCK_JUMP_SECONDS = 120        # |Δwall - Δmono| above this is logged
SAME_BOOT_TOLERANCE_SECONDS = 600    # boot times compared without a boot id (Windows, btime)
MAX_DISK_BYTES_PER_SECOND = 20e9     # plausibility bound for a counter that wrapped at 2**32
_WRAP32 = 2 ** 32

CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')
WIN_CPU_FIELDS = ('idle', 'kernel', 'user')
WIN_TICKS_PER_SECOND = 10 ** 7       # FILETIME: 100 ns units

# Bytes per unit of the 'rs'/'ws' disk counters, by source
DISK_UNIT_BYTES = {'procfs': blockdev.SECTOR_BYTES, 'psutil': 1}
DISK_COUNTERS = ('r', 'w', 'rs', 'ws', 'rt', 'wt', 'io')
# The /proc/diskstats fields that are 32-bit on every kernel: ms reading, ms
# writing and io_ticks ('%u' in the kernel's diskstats_show)
PROCFS_32BIT_COUNTERS = ('rt', 'wt', 'io')

DISK_FIELDS = ('disk_read_bytes_per_s', 'disk_write_bytes_per_s', 'disk_read_iops', 'disk_write_iops',
               'disk_await_ms', 'disk_busy_max_percent', 'disk_busy_max_name')


def _wall_time() -> float:
    return time.time()


def _proc(proc_root: Optional[str], *parts: str) -> str:
    return os.path.join(proc_root or PROC, *parts)


def _is_linux() -> bool:
    return sys.platform.startswith('linux')


# ---------------------------------------------------------------------------
# Windows (ctypes; no pywin32, no psutil)
# ---------------------------------------------------------------------------

def _kernel32():
    import ctypes
    return ctypes.windll.kernel32    # AttributeError off Windows


def _filetime_ticks(ft) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _win_tick_count64(kernel32=None) -> Optional[int]:
    """Milliseconds since boot (GetTickCount64), or None if it can't be read."""
    try:
        import ctypes
        k32 = kernel32 or _kernel32()
        fn = k32.GetTickCount64
        fn.restype = ctypes.c_ulonglong
        return int(fn())
    except Exception as e:
        logger.debug(f"GetTickCount64 failed: {e}")
        return None


def _win_system_times(kernel32=None) -> Optional[Tuple[int, int, int]]:
    """(idle, kernel, user) in 100 ns ticks summed over the CPUs (GetSystemTimes), or None."""
    try:
        import ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        k32 = kernel32 or _kernel32()
        if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return _filetime_ticks(idle), _filetime_ticks(kernel), _filetime_ticks(user)
    except Exception as e:
        logger.debug(f"GetSystemTimes failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _clock_ticks() -> int:
    try:
        return int(os.sysconf('SC_CLK_TCK'))
    except (AttributeError, ValueError, OSError):
        return 100


def _read_btime(proc_root: Optional[str]) -> Optional[int]:
    try:
        with open(_proc(proc_root, 'stat'), 'r') as f:
            for line in f:
                if line.startswith('btime '):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def clock_now(proc_root: Optional[str] = None) -> Optional[dict]:
    """
    {wall, mono, boot_id, boot_time}: wall-clock seconds, seconds since boot,
    an id of this boot (None on Windows; 'btime:<n>' where boot_id is
    unreadable), and the boot time in epoch seconds. None where neither the
    Linux nor the Windows sources exist.
    """
    wall = _wall_time()
    if sys.platform == 'win32':
        ticks = _win_tick_count64()
        if ticks is None:
            return None
        mono = ticks / 1000.0
        return {'wall': wall, 'mono': mono, 'boot_id': None, 'boot_time': wall - mono}
    if not _is_linux():
        return None
    try:
        with open(_proc(proc_root, 'uptime'), 'r') as f:
            mono = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    btime = _read_btime(proc_root)
    boot_id = None
    try:
        with open(_proc(proc_root, 'sys', 'kernel', 'random', 'boot_id'), 'r') as f:
            boot_id = f.read().strip() or None
    except OSError:
        pass
    if boot_id is None and btime is not None:
        boot_id = f'btime:{btime}'
    return {'wall': wall, 'mono': mono, 'boot_id': boot_id,
            'boot_time': float(btime) if btime is not None else wall - mono}


def read_cpu_counters(proc_root: Optional[str] = None) -> Optional[dict]:
    """
    {'source': 'procfs'|'win32', 'hz': ticks per second, 'f': {field: ticks}},
    or None. Linux: user, nice, system, idle, iowait, irq, softirq, steal from
    the aggregate 'cpu' line (guest time is already inside user). Windows:
    idle, kernel (which includes idle) and user; above 64 logical CPUs only the
    primary processor group is covered.
    """
    if sys.platform == 'win32':
        times = _win_system_times()
        if times is None:
            return None
        return {'source': 'win32', 'hz': WIN_TICKS_PER_SECOND, 'f': dict(zip(WIN_CPU_FIELDS, times))}
    if not _is_linux():
        return None
    try:
        with open(_proc(proc_root, 'stat'), 'r') as f:
            for line in f:
                if line.startswith('cpu '):
                    values = [int(x) for x in line.split()[1:]]
                    break
            else:
                return None
    except (OSError, ValueError):
        return None
    if len(values) < 4:
        return None
    fields = {name: (values[i] if i < len(values) else 0) for i, name in enumerate(CPU_FIELDS)}
    return {'source': 'procfs', 'hz': _clock_ticks(), 'f': fields}


def _disk_exclude(config: dict) -> list:
    patterns = (config or {}).get('disk_io_exclude') or []
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        logger.warning(f"Invalid disk_io_exclude={patterns!r} in config (expected a list of names or "
                       f"globs); excluding nothing")
        return []
    return patterns


def read_disk_counters(config: dict, proc_root: Optional[str] = None,
                       sys_block: Optional[str] = None) -> Optional[dict]:
    """
    {'source': 'procfs'|'psutil', 'disks': {name: {'k': 'disk', 'r', 'w', 'rs',
    'ws', 'rt', 'wt', 'io'}}} for the physical disks, or None when there are
    no disk counters (Windows without psutil, other platforms, unreadable
    /proc/diskstats). 'io' (time busy) is None on Windows.
    """
    exclude = _disk_exclude(config)
    if sys.platform == 'win32':
        return _read_psutil_disks(exclude)
    if not _is_linux():
        return None
    try:
        with open(_proc(proc_root, 'diskstats'), 'r') as f:
            lines = f.readlines()
    except OSError as e:
        logger.debug(f"/proc/diskstats unreadable: {e}")
        return None
    disks = {}
    for line in lines:
        cols = line.split()
        # major minor name + 11 fields (kernel 4.4, DSM), 15 (4.18+) or 17 (5.5+)
        if len(cols) < 14:
            continue
        name = cols[2]
        if blockdev.classify_block_device(name, sys_block, exclude) != 'disk':
            continue    # md arrays are counted on their member disks
        try:
            f = [int(x) for x in cols[3:14]]
        except ValueError:
            continue
        disks[name] = {'k': 'disk', 'r': f[0], 'rs': f[2], 'rt': f[3],
                       'w': f[4], 'ws': f[6], 'wt': f[7], 'io': f[9]}
    return {'source': 'procfs', 'disks': disks}


def _read_psutil_disks(exclude: list) -> Optional[dict]:
    try:
        import psutil
    except ImportError:
        return None
    try:
        counters = psutil.disk_io_counters(perdisk=True) or {}
    except Exception as e:
        logger.debug(f"psutil.disk_io_counters failed: {e}")
        return None
    import fnmatch
    disks = {}
    for name, c in counters.items():
        if any(fnmatch.fnmatch(name, p) for p in exclude):
            continue
        disks[name] = {'k': 'disk', 'r': c.read_count, 'w': c.write_count,
                       'rs': c.read_bytes, 'ws': c.write_bytes,
                       'rt': c.read_time, 'wt': c.write_time, 'io': None}
    return {'source': 'psutil', 'disks': disks}


# ---------------------------------------------------------------------------
# The window and the derived values
# ---------------------------------------------------------------------------

def _boot_relation(prev: dict, now: dict) -> str:
    """'same', 'reboot' or 'unknown': whether two clock readings are from the same boot."""
    prev_id, now_id = prev.get('boot_id'), now.get('boot_id')
    if prev_id and now_id and not str(prev_id).startswith('btime:') and not str(now_id).startswith('btime:'):
        return 'same' if prev_id == now_id else 'reboot'
    # Windows, or no boot_id: the boot time (wall - mono, or btime) moves with
    # wall-clock corrections. Only mono going backwards proves a reboot; a
    # boot time that moved while mono went on is a clock step (an unsynced
    # RTC corrected after boot) or a reboot, and a since-boot window would
    # count the CPU of windows already sent again
    if now['mono'] < prev['mono']:
        return 'reboot'
    if abs(float(now['boot_time']) - float(prev['boot_time'])) < SAME_BOOT_TOLERANCE_SECONDS:
        return 'same'
    return 'unknown'


def window(prev: Optional[dict], now: Optional[dict],
           max_window: float = DEFAULT_MAX_WINDOW_SECONDS) -> Tuple[str, Optional[float]]:
    """
    (status, interval_s) for the previous and the current clock_now() (see
    the module docstring). interval_s is Δmono ('ok', 'short', 'stale') or the
    uptime ('reboot'); None for 'none', and for 'stale' after a reboot.
    """
    if not prev or not now:
        return 'none', None
    try:
        prev = {**prev, 'mono': float(prev['mono']), 'wall': float(prev['wall']),
                'boot_time': float(prev['boot_time'])}
    except (KeyError, TypeError, ValueError):
        return 'none', None
    relation = _boot_relation(prev, now)
    if relation == 'same':
        interval = now['mono'] - prev['mono']
        if interval < 0:
            return 'none', None
        if abs((now['wall'] - prev['wall']) - interval) > WALL_CLOCK_JUMP_SECONDS:
            logger.info(f"Wall clock adjusted by {now['wall'] - prev['wall'] - interval:+.0f}s since the "
                        f"previous run; averages use the monotonic clock ({interval:.0f}s)")
        if interval < MIN_WINDOW_SECONDS:
            return 'short', interval
        if interval > max_window:
            return 'stale', interval
        return 'ok', interval
    if relation == 'unknown':
        logger.info(f"The boot time moved by {float(now['boot_time']) - float(prev['boot_time']):+.0f}s "
                    f"since the previous run (a wall-clock step or a reboot; there is no boot id to tell): "
                    f"interval counters start from a new baseline")
        return 'none', None
    uptime = now['mono']
    if uptime <= max_window:
        return 'reboot', uptime
    return 'stale', None


def zero_cpu(cur: dict) -> dict:
    """The baseline at boot for read_cpu_counters() values: every counter 0."""
    return {**cur, 'f': {name: 0 for name in cur['f']}}


def zero_disks(cur: Optional[dict]) -> Optional[dict]:
    """The baseline at boot for read_disk_counters() values (procfs only: psutil isn't since boot)."""
    if not cur or cur.get('source') != 'procfs':
        return None
    return {**cur, 'disks': {name: {**d, **{k: 0 for k in DISK_COUNTERS}} for name, d in cur['disks'].items()}}


def cpu_metrics(prev: Optional[dict], cur: Optional[dict]) -> Optional[dict]:
    """
    {'cpu_percent', 'cpu_iowait_percent', 'cpu_steal_percent', 'attributable_s'}
    over the window between two read_cpu_counters() readings, or None (no
    readings, another source, or a counter other than iowait went backwards,
    so the window isn't valid). attributable_s: CPU-seconds, all cores, that
    processes could have used (Linux user+nice+system, Windows user+kernel-
    idle) - the denominator of proc_cpu_coverage_percent. iowait and steal are
    None on Windows.
    """
    if not prev or not cur or prev.get('source') != cur.get('source'):
        return None
    pf, cf, hz = prev['f'], cur['f'], float(cur['hz'])
    try:
        deltas = {name: cf[name] - pf[name] for name in cf}
    except (KeyError, TypeError):
        return None
    if cur['source'] == 'win32':
        if any(d < 0 for d in deltas.values()):
            return None
        total = deltas['kernel'] + deltas['user']     # kernel time includes idle
        if total <= 0:
            return None
        busy = max(0, total - deltas['idle'])
        return {'cpu_percent': round(min(100.0, busy * 100.0 / total), 1),
                'cpu_iowait_percent': None, 'cpu_steal_percent': None,
                'attributable_s': busy / hz}
    for name, d in deltas.items():
        if d < 0:
            if name != 'iowait':
                return None
            deltas[name] = 0       # iowait can decrease (proc(5))
    total = sum(deltas[name] for name in CPU_FIELDS)
    if total <= 0:
        return None
    busy = total - deltas['idle'] - deltas['iowait']
    return {
        'cpu_percent':        round(min(100.0, max(0.0, busy * 100.0 / total)), 1),
        'cpu_iowait_percent': round(deltas['iowait'] * 100.0 / total, 1),
        'cpu_steal_percent':  round(deltas['steal'] * 100.0 / total, 1),
        'attributable_s':     (deltas['user'] + deltas['nice'] + deltas['system']) / hz,
    }


def _unwrap(delta: int, key: str, interval: float, unit: int, wrap32: bool) -> Optional[int]:
    """A counter delta, with a 32-bit wrap undone (wrap32) when that gives a plausible value; None = reset."""
    if delta >= 0:
        return delta
    if not wrap32:
        return None
    delta += _WRAP32
    if key in ('rs', 'ws'):
        plausible = delta * unit < MAX_DISK_BYTES_PER_SECOND * interval
    elif key in ('r', 'w'):
        plausible = delta * blockdev.SECTOR_BYTES < MAX_DISK_BYTES_PER_SECOND * interval
    elif key == 'io':
        plausible = delta <= interval * 1000 + 1000
    else:   # rt / wt: summed over requests in flight
        plausible = delta <= interval * 1000 * 1024
    return delta if plausible else None


def disk_metrics(prev: Optional[dict], cur: Optional[dict], interval: Optional[float],
                 wrap32: bool = False) -> dict:
    """
    The disk_* fields of system_metrics (DISK_FIELDS) over the window: sums
    over the physical disks, and the busiest one. Every value is None when
    there are no disk counters or no baseline; a disk without a baseline
    (just plugged in) or whose counters went backwards (reset) is left out
    for this run; await is None when no I/O happened; busy is None on Windows.
    wrap32: a 32-bit kernel, whose procfs counters all wrap at 2**32; the
    time fields (PROCFS_32BIT_COUNTERS) wrap on every kernel.
    """
    out = dict.fromkeys(DISK_FIELDS)
    if (not prev or not cur or not interval or interval <= 0
            or prev.get('source') != cur.get('source')):
        return out
    unit = DISK_UNIT_BYTES.get(cur['source'], 1)
    procfs = cur['source'] == 'procfs'
    sums = dict.fromkeys(('r', 'w', 'rs', 'ws', 'rt', 'wt'), 0)
    measured = 0
    busiest = None
    for name, c in sorted(cur['disks'].items()):
        p = prev['disks'].get(name)
        if not isinstance(p, dict):
            continue                 # hot-plugged: no baseline yet
        deltas = {}
        for key in DISK_COUNTERS:
            cv, pv = c.get(key), p.get(key)
            if cv is None or pv is None:
                deltas[key] = None
                continue
            deltas[key] = _unwrap(cv - pv, key, interval, unit,
                                  wrap32 or (procfs and key in PROCFS_32BIT_COUNTERS))
            if deltas[key] is None:
                break
        else:
            measured += 1
            for key in sums:
                sums[key] += deltas[key] or 0
            if deltas['io'] is not None:
                busy = min(100.0, deltas['io'] * 100.0 / (interval * 1000.0))
                if busiest is None or busy > busiest[0]:
                    busiest = (busy, name)
            continue
        logger.info(f"Disk {name}: counters went backwards (device reset); left out of this run's totals")
    if not measured:
        return out
    ops = sums['r'] + sums['w']
    out.update({
        'disk_read_bytes_per_s':  int(round(sums['rs'] * unit / interval)),
        'disk_write_bytes_per_s': int(round(sums['ws'] * unit / interval)),
        'disk_read_iops':         round(sums['r'] / interval, 1),
        'disk_write_iops':        round(sums['w'] / interval, 1),
        'disk_await_ms':          round((sums['rt'] + sums['wt']) / ops, 1) if ops else None,
    })
    if busiest is not None:
        out['disk_busy_max_percent'] = round(busiest[0], 1)
        out['disk_busy_max_name'] = busiest[1][:32]
    return out


# ---------------------------------------------------------------------------
# perf_snapshot.json
# ---------------------------------------------------------------------------

def build_snapshot(clock: dict, cpu: Optional[dict], disks: Optional[dict], procs=None) -> dict:
    """
    The snapshot for the next run (version 1). procs: a proc_accounting
    ProcessTable or None. It holds PIDs and counters, never usernames.
    """
    return {
        'v': SNAPSHOT_VERSION,
        'written_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'boot': {'id': clock.get('boot_id'), 'time': clock.get('boot_time'),
                 'mono': clock.get('mono'), 'wall': clock.get('wall')},
        'source': {'cpu': cpu['source'] if cpu else None,
                   'disk': disks['source'] if disks else None,
                   'proc': procs.source if procs is not None else None},
        'cpu': {'hz': cpu['hz'], 'f': cpu['f']} if cpu else None,
        'disks': disks['disks'] if disks else {},
        'proc_hz': procs.hz if procs is not None else None,
        'procs': ({str(pid): [p[0], p[1], p[2], p[3]] for pid, p in procs.procs.items()}
                  if procs is not None else {}),
    }


def load_snapshot(path: str) -> Optional[dict]:
    """The previous snapshot, or None when it is missing, unreadable, corrupt or another version."""
    try:
        import json
        snap = json.loads(local_queue.read_trusted_file(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring unreadable {path} ({e}); counters start from a new baseline")
        return None
    if not isinstance(snap, dict) or snap.get('v') != SNAPSHOT_VERSION:
        logger.info(f"{path} is from another collector version; counters start from a new baseline")
        return None
    boot = snap.get('boot')
    if not isinstance(boot, dict) or not isinstance(snap.get('source'), dict):
        return None
    return snap


def snapshot_clock(snap: dict) -> dict:
    """The clock_now()-shaped clock of a loaded snapshot."""
    boot = snap['boot']
    return {'wall': boot.get('wall'), 'mono': boot.get('mono'),
            'boot_id': boot.get('id'), 'boot_time': boot.get('time')}


def snapshot_cpu(snap: dict) -> Optional[dict]:
    cpu = snap.get('cpu')
    if not isinstance(cpu, dict) or not isinstance(cpu.get('f'), dict):
        return None
    return {'source': snap['source'].get('cpu'), 'hz': cpu.get('hz'), 'f': cpu['f']}


def snapshot_disks(snap: dict) -> Optional[dict]:
    disks = snap.get('disks')
    if not snap['source'].get('disk') or not isinstance(disks, dict):
        return None
    return {'source': snap['source']['disk'], 'disks': disks}


def save_snapshot(path: str, snap: dict) -> bool:
    """Write the snapshot atomically; False (logged) if that failed."""
    try:
        local_queue.atomic_write_json(path, snap)
        return True
    except Exception as e:
        logger.warning(f"Could not write {path} ({e}); this run's interval counters are sent as null")
        return False
