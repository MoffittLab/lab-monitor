"""
NVIDIA GPU stats through NVML (nvidia-ml-py's pynvml), per device and per user.

collect() runs inside one nvmlInit()/nvmlShutdown() and returns
GpuSample(devices, per_user, sample_seconds):

devices: one dict per GPU, the system_metrics 'gpus' list. The device keys
  (index, name, gpu_percent, vram_used_bytes, vram_total_bytes,
  vram_used_formatted, vram_total_formatted, temperature_c, power_watts) are
  what Windows and Linux GPU servers have always sent; per-process keys
  follow: uuid, process_source ('nvml', or None when there is no per-process
  data for that GPU), util_split, users ([{username, vram_bytes, util_percent,
  processes}], at most 20, by VRAM), users_truncated, unattributed_vram_bytes,
  unattributed_processes. Without per-process data for a GPU (process_source
  None) users and the three counts are None. With gpu_per_user "off" only
  uuid, process_source (None) and util_split (None) are added.

per_user: {username: {gpu_vram_bytes, gpu_util_percent, gpu_busy_seconds,
  gpus_held, gpu_held_seconds, gpu_processes}} for the user_metrics
  messages; None unless the process list worked on every GPU (never partial).

sample_seconds: the dt behind the GPU-seconds (system_metrics
  gpu_sample_seconds): interval_s when 0 < interval_s <= 900, else config
  metrics_interval_seconds (default 300). None without GPUs.

How the busy % of a GPU (gpu_percent, U) is split between users, in order:
U unknown -> None ('util_split' null); U == 0 -> 0 for everyone ('idle');
exactly one owner (a user or the unattributed bucket) with compute processes
-> that owner gets U ('single_user'); per-process SM samples of the holders
summing to > 0 -> U x their share of all samples ('process_samples');
otherwise None for the holders ('ambiguous'). Users who hold no compute
process on a GPU get 0 there.

Processes: the compute, MPS and graphics lists merged by pid; compute (or MPS)
processes "hold" the GPU, graphics-only ones count for VRAM only. Under WDDM
(Windows desktop driver model) a pid in both the compute and graphics lists
(C+G: explorer.exe, browsers) counts as graphics. Per-process VRAM is unknown
under WDDM (None), and implausible values (negative or above the GPU's
total) are dropped. Owners come from the counters' process walk (owner_of),
then /proc/<pid>/status (Linux), then psutil; excluded users (user_filter)
and unknown owners go in the unattributed bucket. Process names and PIDs
never leave the host.
"""
import logging
import os
import sys
from collections import namedtuple
from typing import Callable, Optional

from . import proc_accounting
from . import settings
from .disk_usage import format_bytes

logger = logging.getLogger(__name__)

GpuSample = namedtuple('GpuSample', 'devices per_user sample_seconds')

# NVML return codes (nvml.h) that get a specific explanation in the log
NVML_SUCCESS                 = 0     # pynvml 12.535 raises NVMLError(0) for "no samples"
NVML_ERROR_NOT_SUPPORTED     = 3
NVML_ERROR_NO_PERMISSION     = 4
NVML_ERROR_NOT_FOUND         = 6
NVML_ERROR_DRIVER_NOT_LOADED = 9
NVML_ERROR_LIBRARY_NOT_FOUND = 12
NVML_ERROR_FUNCTION_NOT_FOUND = 13
NVML_ERROR_GPU_IS_LOST       = 15
NVML_ERROR_VERSION_MISMATCH  = 18   # NVML_ERROR_LIB_RM_VERSION_MISMATCH
NVML_DRIVER_WDDM             = 0    # nvmlDeviceGetDriverModel: 0 WDDM, 1 WDM (TCC)

NVML_INIT_HINTS = {
    NVML_ERROR_LIBRARY_NOT_FOUND:
        "The NVIDIA driver's NVML library (libnvidia-ml.so.1 / nvml.dll) was not found: "
        "is the NVIDIA driver installed?",
    NVML_ERROR_DRIVER_NOT_LOADED:
        "The NVIDIA kernel driver is not loaded, or /dev/nvidiactl cannot be opened "
        "(device permissions, or a service sandbox: see 'GPU servers' in the collector README).",
    NVML_ERROR_NO_PERMISSION:
        "No permission to open the NVIDIA devices (/dev/nvidia*): see 'GPU servers' in the "
        "collector README.",
    NVML_ERROR_VERSION_MISMATCH:
        "The NVIDIA kernel module and the NVML library are different versions, usually after "
        "a driver upgrade without a reboot: reboot.",
}

MAX_USERS_PER_GPU = 20
USERNAME_MAX_CHARS = 256     # contract R10
MAX_SAMPLE_SECONDS = 900
UUID_MAX_CHARS = 96
# Windows drivers in this range list every GPU's processes for each GPU [forum]
BAD_WINDOWS_DRIVERS = (500.0, 526.0)

_UNATTRIBUTED = object()     # owner key of excluded / unknown processes

# The per-process keys of a GPU without per-process data: not measured, so null (never 0)
_NO_PROCESS_DATA = {'process_source': None, 'util_split': None, 'users': None, 'users_truncated': None,
                    'unattributed_vram_bytes': None, 'unattributed_processes': None}


def nvml_error_text(e: Exception) -> str:
    """'Insufficient Permissions (NVML error 4)' for an NVMLError, str(e) otherwise."""
    try:
        text = str(e)
    except Exception:
        text = type(e).__name__
    code = getattr(e, 'value', None)
    return f"{text} (NVML error {code})" if isinstance(code, int) else text


def _code(e: Exception):
    return getattr(e, 'value', None)


def nvml_or_none(fn, *args, problems: Optional[list] = None, what: str = '', log=None):
    """
    Call an NVML query; None if this GPU/driver can't answer it. A failure
    other than "not supported" (normal for e.g. power on some GPUs) is added
    to `problems` as "what: error" so the caller can log it.
    """
    try:
        return fn(*args)
    except Exception as e:
        if _code(e) == NVML_ERROR_NOT_SUPPORTED:
            (log or logger).debug(f"NVML {what or getattr(fn, '__name__', fn)}: not supported")
        elif problems is not None:
            problems.append(f"{what}: {nvml_error_text(e)}")
        return None


def _text(value) -> Optional[str]:
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value


def sample_seconds(config: dict, interval_s: Optional[float]) -> float:
    """interval_s when 0 < interval_s <= 900 s, otherwise metrics_interval_seconds (an unknown gap is never extrapolated)."""
    if isinstance(interval_s, (int, float)) and 0 < interval_s <= MAX_SAMPLE_SECONDS:
        return float(interval_s)
    return float(settings.value(config, 'metrics_interval_seconds'))


def collect(config: dict, owner_of: Optional[dict], interval_s: Optional[float],
            logger: Optional[logging.Logger] = None, user_filter: Optional[Callable] = None) -> GpuSample:
    """
    Device and per-user GPU stats (see the module docstring). owner_of:
    {pid: username} from the process walk (excluded users included), or
    None. user_filter(pid, username, uid) -> False for processes that must
    not be attributed to their user (exclude_users, system accounts, the
    collector itself); None attributes every known owner.

    nvidia-ml-py is only installed where an NVIDIA driver was found, so an NVML
    that can't start is logged as a warning, with the likely cause, and so is
    a query that fails for another reason than "not supported". A failure in
    the per-user part is logged once and leaves the device stats intact.
    """
    log = logger or logging.getLogger(__name__)
    try:
        import pynvml
    except ImportError:
        return GpuSample([], None, None)   # nvidia-ml-py not installed: not a GPU host

    try:
        pynvml.nvmlInit()
    except Exception as e:
        hint = NVML_INIT_HINTS.get(_code(e), '')
        log.warning(f"NVIDIA GPU stats unavailable: NVML could not start "
                    f"({nvml_error_text(e)}). {hint}".rstrip())
        return GpuSample([], None, None)

    devices, handles, problems = [], [], []
    per_user = None
    try:
        try:
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                except Exception as e:
                    problems.append(f"GPU {i}: {nvml_error_text(e)}; GPU left out")
                    continue   # skip individual GPU on error
                devices.append(_device_stats(pynvml, i, handle, problems, log))
                handles.append(handle)
        except Exception as e:
            problems.append(f"listing the GPUs: {nvml_error_text(e)}")

        seconds = sample_seconds(config, interval_s) if devices else None
        if devices:
            for dev, handle in zip(devices, handles):
                dev['uuid'] = _uuid(pynvml, handle, dev['index'], problems, log)
            if settings.value(config, 'gpu_per_user') == 'off':
                for dev in devices:
                    dev.update(process_source=None, util_split=None)
            else:
                try:
                    per_user = _per_user(pynvml, devices, handles, owner_of, user_filter, seconds, log)
                except Exception as e:
                    log.warning(f"Per-user GPU data unavailable this run ({type(e).__name__}: {e}); "
                                f"the GPU device stats are still sent")
                    per_user = None
                    for dev in devices:
                        dev.update(_NO_PROCESS_DATA)
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    if problems:
        hint = (" A GPU that is lost needs a reset or reboot." if any(
            f"(NVML error {NVML_ERROR_GPU_IS_LOST})" in p for p in problems) else "")
        log.warning(f"NVML errors (values sent as null): {'; '.join(problems)}.{hint}")
    return GpuSample(devices, per_user, seconds)


def _device_stats(pynvml, i: int, handle, problems: list, log) -> dict:
    def query(fn, *args, what):
        return nvml_or_none(fn, handle, *args, problems=problems, what=f"GPU {i} {what}", log=log)

    name = _text(query(pynvml.nvmlDeviceGetName, what='name'))
    util = query(pynvml.nvmlDeviceGetUtilizationRates, what='utilization')
    mem  = query(pynvml.nvmlDeviceGetMemoryInfo, what='memory')
    temp = query(pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU, what='temperature')
    power_mw = query(pynvml.nvmlDeviceGetPowerUsage, what='power')

    vram_used  = mem.used if mem is not None else None
    vram_total = mem.total if mem is not None else None
    return {
        'index':                i,
        'name':                 name,
        'gpu_percent':          util.gpu if util is not None else None,
        'vram_used_bytes':      vram_used,
        'vram_total_bytes':     vram_total,
        'vram_used_formatted':  format_bytes(vram_used) if vram_used is not None else None,
        'vram_total_formatted': format_bytes(vram_total) if vram_total is not None else None,
        'temperature_c':        temp,
        'power_watts':          round(power_mw / 1000.0, 1) if power_mw is not None else None,
    }


def _uuid(pynvml, handle, index: int, problems: list, log) -> Optional[str]:
    fn = getattr(pynvml, 'nvmlDeviceGetUUID', None)
    if fn is None:
        return None
    value = _text(nvml_or_none(fn, handle, problems=problems, what=f"GPU {index} uuid", log=log))
    return value[:UUID_MAX_CHARS] if isinstance(value, str) else None


def _driver_version(pynvml) -> Optional[str]:
    try:
        return _text(pynvml.nvmlSystemGetDriverVersion())
    except Exception:
        return None


def _bad_windows_driver(version: Optional[str]) -> bool:
    try:
        number = float('.'.join(str(version).split('.')[:2]))
    except (TypeError, ValueError):
        return False
    return BAD_WINDOWS_DRIVERS[0] <= number < BAD_WINDOWS_DRIVERS[1]


def _is_wddm(pynvml, handle) -> bool:
    """Whether this GPU runs the WDDM driver model (Windows only). Unknown counts as WDDM: the C+G rule is harmless under TCC."""
    try:
        current = pynvml.nvmlDeviceGetDriverModel(handle)
        if isinstance(current, (list, tuple)):
            current = current[0]
        return current == NVML_DRIVER_WDDM
    except Exception:
        return True


class _OldDriver(Exception):
    """nvmlDeviceGetComputeRunningProcesses_v3 is missing from this driver's NVML."""


def _process_lists(pynvml, handle, vram_total, wddm: bool) -> Optional[dict]:
    """{pid: {'compute': bool, 'vram': int | None}} for one GPU, or None if the compute list failed."""
    try:
        compute = pynvml.nvmlDeviceGetComputeRunningProcesses(handle) or []
    except Exception as e:
        if _code(e) == NVML_ERROR_FUNCTION_NOT_FOUND:
            raise _OldDriver()
        return None

    def optional(fn_name):
        fn = getattr(pynvml, fn_name, None)
        if fn is None:
            return []
        try:
            return fn(handle) or []
        except Exception:
            return []           # not supported here (MPS off, TCC has no graphics), or failed

    procs = {}
    for kind, items in (('c', compute), ('c', optional('nvmlDeviceGetMPSComputeRunningProcesses')),
                        ('g', optional('nvmlDeviceGetGraphicsRunningProcesses'))):
        for item in items:
            pid = getattr(item, 'pid', None)
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            entry = procs.setdefault(pid, {'c': False, 'g': False, 'vram': None})
            entry[kind] = True
            used = getattr(item, 'usedGpuMemory', None)    # None when N/A (always under WDDM)
            if (isinstance(used, int) and not isinstance(used, bool) and used >= 0
                    and (vram_total is None or used <= vram_total)):
                entry['vram'] = used if entry['vram'] is None else max(entry['vram'], used)
    return {pid: {'compute': e['c'] and not (wddm and e['g']), 'vram': e['vram']} for pid, e in procs.items()}


def _process_samples(pynvml, handle) -> Optional[dict]:
    """{pid: summed smUtil over every buffered sample} ({} = all idle), or None if unavailable."""
    fn = getattr(pynvml, 'nvmlDeviceGetProcessUtilization', None)
    if fn is None:
        return None
    try:
        samples = fn(handle, 0) or []
    except Exception as e:
        return {} if _code(e) in (NVML_ERROR_NOT_FOUND, NVML_SUCCESS) else None
    sums = {}
    for s in samples:
        pid, sm = getattr(s, 'pid', None), getattr(s, 'smUtil', None)
        if isinstance(pid, int) and isinstance(sm, (int, float)) and 0 <= sm <= 100:
            sums[pid] = sums.get(pid, 0) + sm
    return sums


def _owner(pid: int, owner_of: Optional[dict], cache: dict):
    """
    (username, uid) of a process: the process walk's map (uid None: the
    filter looks it up in uid_of), then /proc/<pid>/status on Linux, then
    psutil. With the uid the filter's system-account and own-account rules
    apply to pids the walk didn't list too, and /proc names an account-less
    uid 'uid:<n>' like the walk does (psutil says '<n>').
    """
    if owner_of and pid in owner_of:
        return owner_of[pid], None
    if sys.platform.startswith('linux'):
        try:
            with open(os.path.join(proc_accounting.PROC, str(pid), 'status'), 'rb') as f:
                uid = proc_accounting.status_uid(f.read())
            if uid is not None:
                return proc_accounting.user_name(uid, cache), uid
        except OSError:
            pass
    try:
        import psutil
        proc = psutil.Process(pid)
        name = proc.username() or None
    except Exception:
        return None, None
    uid = None
    if sys.platform != 'win32':
        try:
            real = getattr(proc.uids(), 'real', None)
            uid = real if isinstance(real, int) and not isinstance(real, bool) else None
        except Exception:
            pass
    return name, uid


def _per_user(pynvml, devices: list, handles: list, owner_of, user_filter, seconds: float, log):
    windows = sys.platform == 'win32'
    driver = _driver_version(pynvml)
    if windows and _bad_windows_driver(driver):
        log.warning(f"NVIDIA driver {driver} lists every GPU's processes for each GPU: per-user GPU data "
                    f"is off with this driver (500.xx-525.xx); update the driver to get it")
        for dev in devices:
            dev.update(_NO_PROCESS_DATA)
        return None

    names = {}
    totals = {}
    complete = True
    old_driver_logged = False
    for dev, handle in zip(devices, handles):
        try:
            procs = _process_lists(pynvml, handle, dev.get('vram_total_bytes'), windows and _is_wddm(pynvml, handle))
        except _OldDriver:
            procs = None
            if not old_driver_logged:
                old_driver_logged = True
                log.warning(f"NVIDIA driver {driver or '(version unknown)'} is too old for the per-process "
                            f"query nvidia-ml-py uses (nvmlDeviceGetComputeRunningProcesses_v3): no per-user "
                            f"GPU data; device stats are unaffected")
        if procs is None:
            dev.update(_NO_PROCESS_DATA)
            complete = False
            continue
        vram_used, vram_total = dev.get('vram_used_bytes'), dev.get('vram_total_bytes')
        if not procs and vram_used and vram_total and vram_used > vram_total / 2:
            log.info(f"GPU {dev['index']}: {format_bytes(vram_used)} of VRAM in use but no processes are "
                     f"visible to this account")

        owners = {}
        for pid, p in procs.items():
            name, uid = _owner(pid, owner_of, names)
            key = (name[:USERNAME_MAX_CHARS] if name and (user_filter is None or user_filter(pid, name, uid))
                   else _UNATTRIBUTED)
            o = owners.get(key)
            if o is None:
                o = owners[key] = {'vram': 0, 'vram_unknown': False, 'processes': 0, 'compute': []}
            o['processes'] += 1
            if p['vram'] is None:
                o['vram_unknown'] = True
            else:
                o['vram'] += p['vram']
            if p['compute']:
                o['compute'].append(pid)

        util_split, util = _split(dev.get('gpu_percent'), owners, _process_samples(pynvml, handle))
        rows = []
        for key, o in owners.items():
            if key is _UNATTRIBUTED:
                continue
            vram = None if o['vram_unknown'] else o['vram']
            share = util[key]
            rows.append({'username': key, 'vram_bytes': vram,
                         'util_percent': None if share is None else round(share, 1),
                         'processes': o['processes']})
            t = totals.get(key)
            if t is None:
                t = totals[key] = {'vram': 0, 'vram_unknown': False, 'held': 0, 'util': 0.0,
                                   'util_unknown': False, 'busy': 0.0, 'compute': 0}
            if vram is None:
                t['vram_unknown'] = True
            else:
                t['vram'] += vram
            if o['compute']:
                t['held'] += 1
                t['compute'] += len(o['compute'])
                if share is None:
                    t['util_unknown'] = True
                else:
                    t['util'] += share
                    t['busy'] += share / 100.0 * seconds
        rows.sort(key=lambda r: (r['vram_bytes'] is None, -(r['vram_bytes'] or 0), -r['processes'], r['username']))
        other = owners.get(_UNATTRIBUTED)
        dev.update({
            'process_source':          'nvml',
            'util_split':              util_split,
            'users':                   rows[:MAX_USERS_PER_GPU],
            'users_truncated':         max(0, len(rows) - MAX_USERS_PER_GPU),
            'unattributed_vram_bytes': (0 if other is None else None if other['vram_unknown'] else other['vram']),
            'unattributed_processes':  0 if other is None else other['processes'],
        })

    if not complete:
        return None
    per_user = {}
    for name, t in totals.items():
        per_user[name] = {
            'gpu_vram_bytes':   None if t['vram_unknown'] else t['vram'],
            'gpu_util_percent': (None if t['util_unknown']
                                 else round(t['util'] / t['held'], 1) if t['held'] else 0.0),
            'gpu_busy_seconds': None if t['util_unknown'] else round(t['busy'], 1),
            'gpus_held':        t['held'],
            'gpu_held_seconds': round(t['held'] * seconds, 1),
            'gpu_processes':    t['compute'],
        }
    return per_user


def _split(busy, owners: dict, samples: Optional[dict]):
    """(util_split, {owner key: attributed busy % or None}) for one GPU (see the module docstring)."""
    holders = [key for key, o in owners.items() if o['compute']]
    if busy is None:
        return None, {key: None for key in owners}
    util = {key: 0.0 for key in owners}
    if busy == 0:
        return 'idle', util
    if len(holders) == 1:
        util[holders[0]] = float(busy)
        return 'single_user', util
    if samples:
        total = sum(samples.values())
        weights = {key: sum(samples.get(pid, 0) for pid in owners[key]['compute']) for key in holders}
        if total > 0 and sum(weights.values()) > 0:
            for key in holders:
                util[key] = busy * weights[key] / total
            return 'process_samples', util
    for key in holders:
        util[key] = None
    return 'ambiguous', util
