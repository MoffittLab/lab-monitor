"""
Windows / Linux parity of what a GPU server reports.

An Ubuntu GPU server must send the Manager the same data as the Windows GPU
servers. The collector code is shared, so these tests run the real collection
paths -- collect_metrics() and collect_disk_usage(), through the archive and
the queue to the POST the (fake) Manager receives -- once with the process
behaving as Windows and once as Linux, with the same fake NVML (two GPUs; the
second can't report utilisation, temperature or power; alice and bob run
compute jobs on them) and the same workload seen through each OS's sources.
The messages on the wire are compared: key sets (*_unit keys included),
value types, units, and the values -- system_metrics with the interval
counters (CPU, disk I/O), user_metrics with per-user CPU-seconds and per-user
GPU, and the per-GPU 'users' lists.

The workload's sources: on Linux a fake /proc (stat, uptime, boot_id,
diskstats, <pid>/stat and status) and /sys/block, since the counters and the
per-process walk read procfs with or without psutil; on Windows
GetTickCount64 / GetSystemTimes (ctypes, faked) and psutil's process_iter,
disk_io_counters. Two metrics runs, 300 s apart on both clocks: the first has
no baseline (1-s CPU sample, CPU-seconds null), the second reports averages.

"Behaving as Windows" means the Windows *platform branches*: sys.platform
'win32' and no "platform" key in the config (so detection, the data dir, the
PowerShell folder scan, the Windows skip rules and the per-user code without
psutil's 'uids' all take their Windows paths), Windows account and NIC names.
As Linux: sys.platform 'linux', the config the Linux installer writes, the
collector running as the system account labmon, /etc/login.defs, and a
/sys/class/net with docker interfaces. os.path stays this host's, so
test_folder_usage_has_the_same_shape_and_sizes feeds POSIX paths to the
Windows run; Windows *path handling* (backslash and forward-slash scan_paths,
ntpath normalisation, case-insensitive duplicate and volume-root checks,
drive-letter volume keys such as 'E:\\_total_bytes') is covered by
test_folder_usage_with_windows_paths, which runs the scan-path logic with
ntpath. Windows GPU servers run TCC or WDDM: the TCC run must equal Linux;
under WDDM NVML has no per-process VRAM (KNOWN_DIFFERENCES). Not covered: the
msvcrt queue lock (chosen when lib.local_queue is imported), real ctypes
calls, and real PowerShell / wmic output.

The expected differences are listed in KNOWN_DIFFERENCES, with the reason;
any other difference fails.
"""
import contextlib
import json
import logging
import ntpath
import os
import re
import subprocess
import sys
import time
import types
from collections import Counter
from datetime import datetime, timedelta

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32', reason='the Linux folder scanner needs POSIX paths')

GiB = 1024 ** 3
MiB = 1024 ** 2

# Expected differences between the Windows and the Linux messages. Keys and
# units are identical in system_metrics, user_metrics and folder_usage;
# the only type differences are values Windows can't measure (null).
KNOWN_DIFFERENCES = {
    'user_metrics: Linux-only accounts': (
        {'root'},
        "Windows' default excludes hide its OS service accounts (NT AUTHORITY\\*, NT SERVICE\\*, "
        "DWM-*, UMFD-*); Linux hides their counterparts, daemon accounts (UID below UID_MIN, such "
        "as systemd-resolve, nvidia-persistenced and the collector's own labmon), by default "
        "(exclude_system_accounts). root stays visible on purpose: docker containers and sudo jobs -- "
        "real GPU workloads -- run as root, while on Windows an elevated job keeps its user's name. "
        "So the root row also carries system services; exclude_users [\"root\"] hides it."),
    'user_metrics: username format': (
        None, "Windows names are DOMAIN\\user (psutil), Linux names are plain user names."),
    'folder_usage: path keys': (
        None, "Folder and volume keys are native paths (E:\\Data\\alice on Windows, /data/alice on Linux)."),
    'system_metrics: null on Windows': (
        {'cpu_iowait_percent', 'cpu_steal_percent', 'disk_busy_max_percent', 'disk_busy_max_name'},
        "GetSystemTimes has no iowait or steal time, and psutil's Windows disk counters have no busy "
        "time (v1): null on Windows, never 0. The keys and units are the same."),
    'system_metrics: CPU coverage': (
        {'proc_cpu_coverage_percent'},
        "The share of CPU time attributed to processes depends on each OS's process list (kernel "
        "threads, protected processes, the System Idle Process): a float on both, not the same value."),
    'per-user GPU: WDDM': (
        {'vram_bytes', 'gpu_vram_bytes', 'unattributed_vram_bytes'},
        "Under the WDDM driver model NVML reports no per-process VRAM: null (TCC and Linux report it). "
        "Held GPUs, process counts and the busy split are the same."),
    'storage_breakdown: owners null on Windows': (
        {'folder_owner', 'owners', 'other'},
        "The PowerShell scanner yields no file owners: owner_source 'none' and the owner fields null "
        "(owners not collected). Bytes, file counts and the cold buckets (by LastWriteTimeUtc) are the "
        "same as Linux's (by st_mtime)."),
    'sessions: user-level sources': (
        None,
        "Named users come from each OS's own sources: utmp/logind (ssh, console) on Linux; WTS (console, "
        "RDP) and NetSessionEnum (SMB) on Windows, so the *_users keys differ. The client counts come "
        "from the TCP table on both and are the same."),
}
WINDOWS_NULL = KNOWN_DIFFERENCES['system_metrics: null on Windows'][0]
WDDM_NULL = KNOWN_DIFFERENCES['per-user GPU: WDDM'][0]


# ---------------------------------------------------------------------------
# Fake NVML (nvidia-ml-py's pynvml module)
# ---------------------------------------------------------------------------

_NVML_STRINGS = {3: 'Not Supported', 4: 'Insufficient Permissions', 6: 'Not Found', 9: 'Driver Not Loaded',
                 12: 'NVML Shared Library Not Found', 18: 'RM has detected an NVML/RM version mismatch.',
                 999: 'Unknown Error'}


class FakeNVMLError(Exception):
    """Like pynvml.NVMLError: .value is the NVML return code, str() its description."""

    def __init__(self, value):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return _NVML_STRINGS.get(self.value, f'NVML error code {self.value}')


ns = types.SimpleNamespace

# GPU 0 reports everything. GPU 1 (an older nvidia-ml-py returns the name as
# bytes) has a utilisation query that fails and no temperature/power sensors.
# Compute processes (pid, VRAM): alice's training job and bob's job share GPU
# 0, whose SM samples weigh them 3:1; alice's notebook is on GPU 1.
GPUS = [
    {'Name': 'NVIDIA RTX A6000',
     'UUID': 'GPU-8f2c0000-1111-2222-3333-444455556666',
     'UtilizationRates': ns(gpu=37, memory=21),
     'MemoryInfo': ns(total=48 * GiB, used=12 * GiB, free=36 * GiB),
     'Temperature': 61,
     'PowerUsage': 215_340,                       # milliwatts
     'Compute': [(2001, 10 * GiB), (2003, 1 * GiB)],
     'Samples': [(2001, 30), (2003, 10), (2001, 30), (2003, 10)]},
    {'Name': b'Tesla T4',
     'UUID': b'GPU-1d4e0000-7777-8888-9999-000011112222',
     'UtilizationRates': FakeNVMLError(999),
     'MemoryInfo': ns(total=16 * GiB, used=1 * GiB, free=15 * GiB),
     'Temperature': FakeNVMLError(3),
     'PowerUsage': FakeNVMLError(3),
     'Compute': [(2002, 512 * MiB)],
     'Samples': FakeNVMLError(6)},
]


def make_fake_pynvml(gpus=GPUS, init_error=None, driver_model='tcc'):
    """driver_model: 'tcc' or 'wddm' (Windows: nvmlDeviceGetDriverModel, and no per-process VRAM)."""
    nvml = types.ModuleType('pynvml')
    nvml.NVMLError = FakeNVMLError
    nvml.NVML_TEMPERATURE_GPU = 0
    nvml.NVML_ERROR_NOT_SUPPORTED = 3
    nvml.calls = Counter()

    def nvmlInit():
        nvml.calls['init'] += 1
        if init_error is not None:
            raise init_error

    def nvmlShutdown():
        nvml.calls['shutdown'] += 1

    def query(field):
        def fn(handle, *args):
            if field == 'Temperature':
                assert args == (nvml.NVML_TEMPERATURE_GPU,)
            value = handle[field]
            if isinstance(value, Exception):
                raise value
            return value
        fn.__name__ = f'nvmlDeviceGet{field}'
        return fn

    def processes(handle):
        wddm = driver_model == 'wddm' and sys.platform == 'win32'
        return [ns(pid=pid, usedGpuMemory=None if wddm else mem) for pid, mem in handle['Compute']]

    def samples(handle, last_seen):
        if isinstance(handle['Samples'], Exception):
            raise handle['Samples']
        return [ns(pid=pid, smUtil=sm, memUtil=0, timeStamp=i) for i, (pid, sm) in enumerate(handle['Samples'])]

    def driver_model_of(handle):
        if sys.platform != 'win32':
            raise FakeNVMLError(3)
        value = 0 if driver_model == 'wddm' else 1          # NVML_DRIVER_WDDM / NVML_DRIVER_WDM (TCC)
        return [value, value]

    nvml.nvmlInit = nvmlInit
    nvml.nvmlShutdown = nvmlShutdown
    nvml.nvmlSystemGetDriverVersion = lambda: '551.61' if sys.platform == 'win32' else '550.54.15'
    nvml.nvmlDeviceGetCount = lambda: len(gpus)
    nvml.nvmlDeviceGetHandleByIndex = lambda i: gpus[i]
    for field in ('Name', 'UUID', 'UtilizationRates', 'MemoryInfo', 'Temperature', 'PowerUsage'):
        setattr(nvml, f'nvmlDeviceGet{field}', query(field))
    nvml.nvmlDeviceGetComputeRunningProcesses = processes
    nvml.nvmlDeviceGetMPSComputeRunningProcesses = lambda handle: []
    nvml.nvmlDeviceGetGraphicsRunningProcesses = lambda handle: []
    nvml.nvmlDeviceGetProcessUtilization = samples
    nvml.nvmlDeviceGetDriverModel = driver_model_of
    return nvml


# ---------------------------------------------------------------------------
# The workload: the same machine state as seen on each OS
# ---------------------------------------------------------------------------

OWN = object()      # stands for the collector's own pid
ABSENT = object()   # no such process on this OS
LABMON_UID = 998    # useradd --system: below UID_MIN
NCPU = 64
INTERVAL = 300.0    # between the two metrics runs, on every clock
UPTIME = 90061.0    # at the first run
STARTED = 1000.0    # seconds after boot every process started
BASE_CPU = 500.0    # CPU-seconds each process had used at the first run

# (pid, username on Windows, (username, uid) on Linux, rss, per-core cpu%, exits before the 2nd run).
# A username of None is one psutil couldn't read (AccessDenied on Windows).
PROCESSES = [
    (4,    'NT AUTHORITY\\SYSTEM',          ('root', 0),                 2 * GiB,   1.0, False),  # kernel, services
    (OWN,  'NT AUTHORITY\\SYSTEM',          ('labmon', LABMON_UID),      40 * MiB,  3.0, False),  # this collector
    (5000, 'NT AUTHORITY\\SYSTEM',          ('labmon', LABMON_UID),     300 * MiB, 95.0, False),  # the disk scan
    (812,  'NT AUTHORITY\\NETWORK SERVICE', ('systemd-resolve', 101),    30 * MiB,  0.5, False),  # daemon accounts
    (813,  'NT AUTHORITY\\LOCAL SERVICE',   ('messagebus', 102),          5 * MiB,  0.1, False),
    (814,  'NT AUTHORITY\\SYSTEM',          ('nvidia-persistenced', 120), 3 * MiB,  0.0, False),
    (815,  ABSENT,                          ('nobody', 65534),            2 * MiB,  0.0, False),
    (1500, 'Window Manager\\DWM-1',         ABSENT,                      90 * MiB,  0.3, False),
    (1600, 'Font Driver Host\\UMFD-0',      ABSENT,                       5 * MiB,  0.0, False),
    (2001, 'LAB\\alice',                    ('alice', 1000),              8 * GiB, 400.0, False),  # training job
    (2002, 'LAB\\alice',                    ('alice', 1000),              1 * GiB,  32.0, False),  # notebook
    (2003, 'LAB\\bob',                      ('bob', 1001),                2 * GiB,  64.0, False),
    # Exits before the second run. Its parent never wait()s for it (it was
    # reaped by a subreaper), so Linux recovers no child time either: on both
    # OSes its CPU since the first run is lost and only lowers the coverage.
    (2004, 'LAB\\bob',                      ('bob', 1001),                3 * GiB,  50.0, True),
    (2005, None,                            ABSENT,                       1 * GiB,  10.0, False),  # owner unreadable
    (3001, ABSENT,                          ('root', 0),                  6 * GiB, 640.0, False),  # docker container
]

# Physical NIC traffic is the same on both. The Linux host also runs docker:
# docker0 and one veth per container (counted on eno1 as well), and the
# container behind veth3f2a stops before the second run.
NICS = {'windows': ('Loopback Pseudo-Interface 1', 'Ethernet'), 'linux': ('lo', 'eno1')}
LINUX_VIRTUAL_NICS = ('docker0', 'veth3f2a', 'veth9c1d')

# The system CPU: 30 % busy from boot to the first run, 42.5 % in the window between the runs
CPU_BUSY = 0.425


def cpu_at(run, ticks_per_second):
    """Cumulative (busy, idle) CPU time over all 64 CPUs at metrics run `run`, in the given ticks."""
    before = NCPU * UPTIME * ticks_per_second
    window = NCPU * INTERVAL * run * ticks_per_second
    busy = before * 0.3 + window * CPU_BUSY
    return busy, before + window - busy
# One physical disk, cumulative at run n: (reads, writes, read bytes, write bytes, read ms, write ms, busy ms)
DISK_RATE = (20.0, 50.0, 4096 * 512.0, 8192 * 512.0, 60.0, 90.0, 120.0)    # per second


def processes_of(platform, run):
    """[(pid, user, rss, cpu_seconds)] alive at metrics run `run` (0, 1) on that OS."""
    column = 1 if platform == 'windows' else 2
    rows = []
    for p in PROCESSES:
        if p[column] is ABSENT or (run and p[5]):
            continue
        pid = os.getpid() if p[0] is OWN else p[0]
        rows.append((pid, p[column], p[3], BASE_CPU + p[4] / 100.0 * INTERVAL * run))
    return rows


def disk_at(run):
    return [int(rate * (100000 + INTERVAL * run)) for rate in DISK_RATE]


# The TCP table both OSes show: sshd, SMB and RDP listening; two SMB
# connections from one client, an ssh and an RDP client; the collector's own
# outbound connection to the Manager and a loopback one (neither counted)
TCP_TABLE = [
    ('0.0.0.0', 22, None, 'LISTEN'), ('0.0.0.0', 445, None, 'LISTEN'), ('0.0.0.0', 3389, None, 'LISTEN'),
    ('10.11.2.5', 445, ('10.11.2.17', 50001), 'ESTABLISHED'),
    ('10.11.2.5', 445, ('10.11.2.17', 50002), 'ESTABLISHED'),
    ('10.11.2.5', 22, ('10.11.2.40', 50100), 'ESTABLISHED'),
    ('10.11.2.5', 3389, ('10.11.2.50', 50200), 'ESTABLISHED'),
    ('10.11.2.5', 51000, ('10.0.0.9', 5000), 'ESTABLISHED'),
    ('127.0.0.1', 22, ('127.0.0.1', 40000), 'ESTABLISHED'),
]
LOGIN_TIME = 1_795_000_000


def proc_net_tcp():
    """TCP_TABLE as /proc/net/tcp shows it (native-endian hex words)."""
    import ipaddress

    def addr(ip, port):
        return f"{int.from_bytes(ipaddress.IPv4Address(ip).packed, sys.byteorder):08X}:{port:04X}"
    lines = ['  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode']
    for i, (local, port, remote, status) in enumerate(TCP_TABLE):
        rem = addr(*remote) if remote else '00000000:0000'
        lines.append(f"   {i}: {addr(local, port)} {rem} {'0A' if status == 'LISTEN' else '01'} "
                     f"00000000:00000000 00:00000000 00000000  1000        0 1 1 0 100 0 0 10 0")
    return '\n'.join(lines) + '\n'


def utmp_record(user, line, host, addr, pid):
    """One struct utmp (glibc, 384 bytes) USER_PROCESS record."""
    import ipaddress
    import struct
    return struct.pack('=h2xi32s4s32s256shhiii16s20x', 7, pid, line.encode(), b'ts/0', user.encode(),
                       host.encode(), 0, 0, 0, LOGIN_TIME, 0, ipaddress.IPv4Address(addr).packed + b'\0' * 12)


# What WTS and NetSessionEnum report on the Windows server
WINDOWS_SESSIONS = {
    'wts': ('ok', [{'service': 'rdp', 'username': 'LAB\\alice', 'client': '10.11.2.50', 'client_host': 'ALICE-PC',
                    'state': 'active', 'source': 'wts', 'since': '2026-11-18T20:13:20Z'}]),
    'netsession': ('ok', [{'service': 'smb', 'username': 'bob', 'client': '10.11.2.17', 'source': 'netsession',
                           'since': '2026-11-18T20:13:20Z'}]),
}

# What the storage-health PowerShell prints on the Windows server: one NVMe
# disk and the E: volume, healthy
WINDOWS_HEALTH = {
    'disks_status': 'ok', 'spaces_status': 'ok', 'volumes_status': 'ok', 'events_status': 'ok',
    'disks': [{'DeviceId': '0', 'FriendlyName': 'Samsung SSD 990 PRO 2TB', 'SerialNumber': 'S7KHNJ0X000000',
               'MediaType': 'SSD', 'BusType': 'NVMe', 'Size': 2 * 10 ** 12, 'HealthStatus': 'Healthy',
               'OperationalStatus': ['OK'], 'Temperature': 41, 'TemperatureMax': 0,
               'ReadErrorsUncorrected': 0, 'WriteErrorsUncorrected': 0, 'Wear': 2, 'PowerOnHours': 1234}],
    'virtual_disks': [], 'jobs': [],
    'volumes': [{'DriveLetter': 'E', 'FileSystemType': 'NTFS', 'HealthStatus': 'Healthy',
                 'OperationalStatus': ['OK']}],
    'events': {},
}


def make_fake_psutil(platform, clock):
    """psutil as each OS has it, for the workload at run clock['run']."""
    ps = types.ModuleType('psutil')

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class Process:
        def __init__(self, pid, user, rss, cpu, attrs):
            username, uid = user if isinstance(user, tuple) else (user, None)
            self.pid = pid
            self.info = {'pid': pid, 'username': username, 'memory_info': ns(rss=rss)}
            if 'uids' in attrs:
                assert platform == 'linux'         # an invalid attribute name for psutil on Windows
                self.info['uids'] = ns(real=uid, effective=uid, saved=uid)
            if 'cpu_times' in attrs:               # the counters' process walk (Windows only)
                assert platform == 'windows'
                denied = username is None
                self.info.update(ppid=4, create_time=clock['boot_wall'] + STARTED,
                                 cpu_times=ns(user=cpu * 0.75, system=cpu * 0.25))
                if denied:                         # AccessDenied: psutil fills in None
                    self.info.update(create_time=None, cpu_times=None, memory_info=None)

    loopback, physical = NICS[platform]
    reads = Counter()

    def net_io_counters(pernic=False):
        assert pernic
        reads['n'] += 1
        n = reads['n']
        nics = {loopback: ns(bytes_recv=10 ** 9 * n, bytes_sent=10 ** 9 * n),
                physical: ns(bytes_recv=5_000_000 + 7_500_000 * n, bytes_sent=2_000_000 + 1_500_000 * n)}
        if platform == 'linux':
            nics['docker0'] = ns(bytes_recv=10 ** 8 * n, bytes_sent=10 ** 8 * n)
            nics['veth9c1d'] = ns(bytes_recv=4 * 10 ** 7 * n, bytes_sent=4 * 10 ** 7 * n)
            if n == 1:
                nics['veth3f2a'] = ns(bytes_recv=6 * 10 ** 10, bytes_sent=6 * 10 ** 10)
        return nics

    def disk_io_counters(perdisk=False):
        assert perdisk and platform == 'windows'   # Linux reads /proc/diskstats
        r, w, rb, wb, rt, wt, _busy = disk_at(clock['run'])
        return {'PhysicalDrive0': ns(read_count=r, write_count=w, read_bytes=rb, write_bytes=wb,
                                     read_time=rt, write_time=wt)}

    def net_connections(kind='inet'):
        assert platform == 'windows'               # Linux reads /proc/net/tcp
        return [ns(type=1, laddr=(local, port), raddr=raddr, status=status)
                for local, port, raddr, status in TCP_TABLE]

    ps.NoSuchProcess, ps.AccessDenied = NoSuchProcess, AccessDenied
    ps.net_connections = net_connections
    ps.cpu_percent = lambda interval=None: 42.5
    ps.cpu_count = lambda logical=True: NCPU
    ps.virtual_memory = lambda: ns(percent=63.2, total=256 * GiB, available=94 * GiB)
    ps.boot_time = lambda: time.time() - 90061
    ps.net_io_counters = net_io_counters
    ps.disk_io_counters = disk_io_counters
    ps.process_iter = lambda attrs=None: [Process(pid, user, rss, cpu, attrs)
                                          for pid, user, rss, cpu in processes_of(platform, clock['run'])]
    return ps


def write_linux_host(procfs, run):
    """The workload at run `run` as /proc and /sys/block show it."""
    hz = procfs_hz()
    busy, idle = cpu_at(run, hz)
    procfs.uptime = UPTIME + INTERVAL * run
    procfs.cpu = dict(user=int(busy * 0.75), nice=0, system=int(busy * 0.25), idle=int(idle),
                      iowait=0, irq=0, softirq=0, steal=0)
    r, w, rb, wb, rt, wt, io = disk_at(run)
    #                reads merged sectors ms  writes merged sectors ms  in-flight io_ticks weighted
    sda = [r, 0, rb // 512, rt, w, 0, wb // 512, wt, 0, io, rt + wt, 0, 0, 0, 0]
    procfs.disks = {'sda': sda, 'sda1': sda, 'sda2': [0] * 15, 'md0': [10 ** 7] * 15, 'loop0': [10 ** 6] * 15}
    procfs.block = {'sda': {'size': 10 ** 9}, 'md0': {'device': False}, 'loop0': {'device': False}}
    procfs.passwd = {0: 'root', LABMON_UID: 'labmon', 101: 'systemd-resolve', 102: 'messagebus',
                     120: 'nvidia-persistenced', 65534: 'nobody', 1000: 'alice', 1001: 'bob'}
    procfs.procs = {}
    for pid, (username, uid), rss, cpu in processes_of('linux', run):
        procfs.proc_entry(pid, uid, cpu=int(round(cpu * hz)), start=int(STARTED * hz), ppid=1,
                          rss=rss // os.sysconf('SC_PAGE_SIZE'))
    procfs.write()


def procfs_hz():
    return os.sysconf('SC_CLK_TCK')


# ---------------------------------------------------------------------------
# Running the collector as Windows / as Linux
# ---------------------------------------------------------------------------

class Subprocesses:
    """
    Stand-ins for subprocess.run / check_output. PowerShell folder scans are
    answered like Get-ChildItem -Recurse -Force -File | Measure-Object (sum
    and count of every file, hidden ones included); anything else (wmic, ...)
    is recorded as unexpected.
    """

    def __init__(self, breakdown_script=None):
        self.powershell = []
        self.health = []
        self.unexpected = []
        self.breakdown_script = breakdown_script

    def run(self, args, *a, env=None, **kw):
        if args and args[0] == 'powershell' and sys.platform == 'win32' and '-EncodedCommand' in args:
            self.health.append(env['LM_HEALTH_SINCE'])       # storage health (lib/storage_health.py)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(WINDOWS_HEALTH) + '\r\n', stderr='')
        if args and args[0] == 'powershell' and sys.platform == 'win32':
            path = env['LM_SCAN_PATH']
            self.powershell.append(path)
            total = count = cold1 = cold3 = 0
            for dirpath, _dirs, files in os.walk(path):
                for name in files:
                    st = os.lstat(os.path.join(dirpath, name))
                    total += st.st_size
                    count += 1
                    ticks = int(st.st_mtime * 10 ** 7) + 621355968000000000   # LastWriteTimeUtc.Ticks
                    if 'LM_COLD1_TICKS' in env and ticks < int(env['LM_COLD1_TICKS']):
                        cold1 += st.st_size
                        if ticks < int(env['LM_COLD3_TICKS']):
                            cold3 += st.st_size
            if 'LM_COLD1_TICKS' in env:                     # the storage breakdown's scanner
                assert args[-1] == self.breakdown_script
                out = f'{total} {count} {cold1} {cold3}'
            else:
                out = f'{total} {count}'
            return subprocess.CompletedProcess(args, 0, stdout=out + '\r\n', stderr='')
        self.unexpected.append((sys.platform, args))
        raise RuntimeError(f'unexpected subprocess.run({args!r})')

    def check_output(self, args, *a, **kw):
        self.unexpected.append((sys.platform, args))
        raise RuntimeError(f'unexpected subprocess.check_output({args!r})')


@pytest.fixture
def hosts(collector, config, disk_usage, fake_manager, monkeypatch, tmp_path, fakes):
    """
    run(platform, fn) runs fn(config, logger) with the process behaving as that
    OS and returns the messages POSTed to the fake Manager. nvml: a fake pynvml
    module (default: make_fake_pynvml()), or 'absent' for no nvidia-ml-py.
    Every run on a platform is the workload INTERVAL seconds later.
    """
    from lib import perf_counters
    from lib import sessions, sessions_win, storage_health
    procs = Subprocesses(disk_usage.PS_BREAKDOWN_SCRIPT)
    clock = {'t': datetime(2026, 11, 21, 10, 0, 0)}

    def utcnow():
        clock['t'] += timedelta(seconds=1)
        return clock['t']
    monkeypatch.setattr(collector, '_utcnow', utcnow)

    configs = {
        # As install-collector-windows.ps1 writes it: no "platform" (detected)
        'windows': {**config, 'name': 'gpu-win', 'id': 'windows-gpu-win',
                    'data_dir': str(tmp_path / 'state-windows'),
                    'exclude_users': ['UMFD-*', 'NT AUTHORITY\\*', 'NT SERVICE\\*']},
        # As install-collector-linux.sh writes it
        'linux': {**config, 'name': 'gpu-linux', 'id': 'linux-gpu-linux', 'platform': 'linux',
                  'data_dir': str(tmp_path / 'state-linux'), 'exclude_users': [],
                  'exclude_system_accounts': True,
                  'cross_filesystems': False, 'include_network_mounts': False},
    }
    # Each OS's workload clock: run index, wall time, boot time
    clocks = {p: {'run': 0, 'boot_wall': 1_795_000_000.0 - UPTIME} for p in configs}
    psutils = {p: make_fake_psutil(p, clocks[p]) for p in configs}
    linux_host = fakes.FakeProcfs(tmp_path / 'linux-host')
    runs = Counter()

    # The Linux host: Ubuntu's /etc/login.defs, and /sys/class/net with eno1
    # (a PCI device) next to docker0 and the veths
    login_defs = tmp_path / 'login.defs'
    login_defs.write_text('UID_MIN\t\t\t 1000\nUID_MAX\t\t\t60000\n')
    sys_class_net = tmp_path / 'sys-class-net'
    for nic in ('lo', 'eno1') + LINUX_VIRTUAL_NICS:
        (sys_class_net / nic).mkdir(parents=True)
    (sys_class_net / 'eno1' / 'device').mkdir()

    @contextlib.contextmanager
    def behave_as(platform, nvml):
        run = clocks[platform]['run'] = runs[platform]
        runs[platform] += 1
        with monkeypatch.context() as m:
            m.setattr(sys, 'platform', 'win32' if platform == 'windows' else 'linux')
            m.setattr(os, 'cpu_count', lambda: NCPU)
            m.setattr(perf_counters, '_wall_time', lambda: clocks[platform]['boot_wall'] + UPTIME + INTERVAL * run)
            # Platform detection: not Synology, whatever this test host is
            m.setattr(disk_usage, 'SYNOLOGY_MARKERS', (str(tmp_path / 'no-synoinfo.conf'),))
            # Win32-only helpers (PowerShell, wmic) never really run
            m.setattr(subprocess, 'run', procs.run)
            m.setattr(subprocess, 'check_output', procs.check_output)
            m.setitem(sys.modules, 'pynvml', None if nvml == 'absent' else nvml)   # None: ImportError
            m.setitem(sys.modules, 'psutil', psutils[platform])
            if platform == 'windows':
                m.setattr(sessions_win, 'read_all', lambda now, log=None: WINDOWS_SESSIONS)
                busy, idle = cpu_at(run, 10 ** 7)
                m.setattr(perf_counters, '_win_tick_count64',
                          lambda kernel32=None: int((UPTIME + INTERVAL * run) * 1000))
                # kernel time includes idle
                m.setattr(perf_counters, '_win_system_times',
                          lambda kernel32=None: (int(idle), int(idle + busy * 0.25), int(busy * 0.75)))
            else:
                write_linux_host(linux_host, run)
                linux_host.install(m)
                # Sessions and storage health read the same fake host
                os.makedirs(os.path.join(linux_host.proc, 'net'))
                with open(os.path.join(linux_host.proc, 'net', 'tcp'), 'w') as f:
                    f.write(proc_net_tcp())
                utmp = tmp_path / 'utmp'
                utmp.write_bytes(utmp_record('alice', 'pts/0', '10.11.2.40', '10.11.2.40', os.getpid()))
                m.setattr(sessions, 'PROC_NET', os.path.join(linux_host.proc, 'net'))
                m.setattr(sessions, 'UTMP_PATHS', (str(utmp),))
                m.setattr(storage_health, 'SYS_ROOT', os.path.dirname(linux_host.sys_block))
                m.setattr(storage_health, 'PROC_ROOT', linux_host.proc)
                m.setattr(storage_health, 'FSTAB', str(tmp_path / 'no-fstab'))
                # The scanned tree is on a local filesystem even if this host's /tmp is tmpfs
                m.setattr(disk_usage, 'read_mounts',
                          lambda path='/proc/self/mounts': [disk_usage.Mount('/dev/sda2', '/', 'ext4')])
                m.setattr(collector, 'LOGIN_DEFS', str(login_defs))
                m.setattr(collector, 'SYS_CLASS_NET', str(sys_class_net))
                m.setattr(os, 'getuid', lambda: LABMON_UID, raising=False)   # the systemd unit's User=labmon
            yield

    def run(platform, fn, nvml=None, **config_changes):
        nvml = nvml or make_fake_pynvml()
        cfg = {**configs[platform], **config_changes}
        sent_before = len(fake_manager.calls)
        with behave_as(platform, nvml):
            assert collector.get_platform(cfg) == platform
            assert fn(cfg, logging.getLogger(f'test-{platform}')) is True
        return [m for call in fake_manager.calls[sent_before:] for m in call['payload']['messages']]

    return types.SimpleNamespace(run=run, procs=procs, configs=configs)


def shape(value):
    """The JSON structure of a value with every leaf replaced by its type name."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v) for v in value]
    return type(value).__name__


def _by_type(messages, data_type):
    return [m for m in messages if m['data']['data_type'] == data_type]


def _plain_user(name):
    return name.rsplit('\\', 1)[-1]


def _plain_users(gpus):
    """The gpus list with DOMAIN\\ stripped from the per-GPU usernames."""
    return [{**g, 'users': [{**u, 'username': _plain_user(u['username'])} for u in g['users']]}
            if g.get('users') is not None else g for g in gpus]


def _null(data, keys):
    """data with `keys` (at any depth) set to None."""
    if isinstance(data, dict):
        return {k: None if k in keys else _null(v, keys) for k, v in data.items()}
    if isinstance(data, list):
        return [_null(v, keys) for v in data]
    return data


# ---------------------------------------------------------------------------
# system_metrics / user_metrics
# ---------------------------------------------------------------------------

def _two_runs(hosts, collector, nvml=None):
    return {p: [hosts.run(p, collector.collect_metrics, nvml=nvml and nvml()) for _ in range(2)]
            for p in ('windows', 'linux')}


@pytest.fixture
def metrics(hosts, collector):
    """Two metrics runs per OS (the second has interval averages and a bandwidth): {platform: [run1, run2]}."""
    runs = _two_runs(hosts, collector)
    assert hosts.procs.unexpected == []             # psutil present: no wmic, no PowerShell
    return runs


@posix_only
@pytest.mark.parametrize('run', [0, 1], ids=['first-run', 'second-run'])
def test_system_metrics_have_the_same_keys_types_and_units(metrics, run):
    win = _by_type(metrics['windows'][run], 'system_metrics')
    lin = _by_type(metrics['linux'][run], 'system_metrics')
    assert len(win) == len(lin) == 1
    win, lin = win[0], lin[0]

    assert set(win['data']) == set(lin['data'])                     # *_unit keys included
    # Value types, gpus list included; values Windows can't measure are null there
    assert shape(_null(win['data'], WINDOWS_NULL)) == shape(_null(lin['data'], WINDOWS_NULL))
    for key in WINDOWS_NULL:
        assert win['data'][key] is None, key
        if run and key in ('cpu_iowait_percent', 'cpu_steal_percent'):
            assert isinstance(lin['data'][key], float), key
    assert shape(win['header']) == shape(lin['header'])
    assert win['header']['device_type'] == lin['header']['device_type'] == 'Server'
    units = {k: v for k, v in win['data'].items() if k.endswith('_unit')}
    assert units == {k: v for k, v in lin['data'].items() if k.endswith('_unit')}
    # Every numeric field carries its unit (counts and names have none)
    for key, value in win['data'].items():
        if key.endswith('_unit') or key in ('data_type', 'gpus', 'gpu_count', 'cpu_count', 'uptime_formatted',
                                            'disk_busy_max_name'):
            continue
        assert f'{key}_unit' in win['data'], key
    assert units['interval_s_unit'] == 's' and units['disk_read_bytes_per_s_unit'] == 'B/s'
    assert units['disk_read_iops_unit'] == 'ops/s' and units['gpu_sample_seconds_unit'] == 's'


GPU0_USERS = [{'username': 'alice', 'vram_bytes': 10 * GiB, 'util_percent': 27.8, 'processes': 1},
              {'username': 'bob', 'vram_bytes': 1 * GiB, 'util_percent': 9.2, 'processes': 1}]


@posix_only
def test_gpu_fields_are_identical_on_windows_and_linux(metrics):
    def gpu_fields(platform):
        data = _by_type(metrics[platform][1], 'system_metrics')[0]['data']
        fields = {k: v for k, v in data.items() if k.startswith('gpu')}
        fields['gpus'] = _plain_users(fields['gpus'])
        return fields

    win, lin = gpu_fields('windows'), gpu_fields('linux')
    assert win == lin
    assert lin['gpu_count'] == 2
    assert lin['gpus'] == [
        {'index': 0, 'name': 'NVIDIA RTX A6000', 'gpu_percent': 37,
         'vram_used_bytes': 12 * GiB, 'vram_total_bytes': 48 * GiB,
         'vram_used_formatted': '12.0 GB', 'vram_total_formatted': '48.0 GB',
         'temperature_c': 61, 'power_watts': 215.3,
         # Per-process data: alice and bob split GPU 0 3:1 by their SM samples
         'uuid': 'GPU-8f2c0000-1111-2222-3333-444455556666', 'process_source': 'nvml',
         'util_split': 'process_samples', 'users': GPU0_USERS, 'users_truncated': 0,
         'unattributed_vram_bytes': 0, 'unattributed_processes': 0},
        # Unmeasurable values are null, never 0 (contract 4)
        {'index': 1, 'name': 'Tesla T4', 'gpu_percent': None,
         'vram_used_bytes': 1 * GiB, 'vram_total_bytes': 16 * GiB,
         'vram_used_formatted': '1.0 GB', 'vram_total_formatted': '16.0 GB',
         'temperature_c': None, 'power_watts': None,
         'uuid': 'GPU-1d4e0000-7777-8888-9999-000011112222', 'process_source': 'nvml',
         'util_split': None,       # no utilisation reading: nothing to split
         'users': [{'username': 'alice', 'vram_bytes': 512 * MiB, 'util_percent': None, 'processes': 1}],
         'users_truncated': 0, 'unattributed_vram_bytes': 0, 'unattributed_processes': 0},
    ]
    flat = {k: v for k, v in lin.items() if k not in ('gpus', 'gpu_count')}
    assert flat == {
        'gpu_0_percent': 37, 'gpu_0_percent_unit': '%',
        'gpu_0_vram_used': 12 * GiB, 'gpu_0_vram_used_unit': 'bytes',
        'gpu_0_temp_c': 61, 'gpu_0_temp_c_unit': '\u00b0C',
        'gpu_0_power_w': 215.3, 'gpu_0_power_w_unit': 'W',
        # GPU 1: utilisation unmeasured (null); no temperature/power sensor (omitted)
        'gpu_1_percent': None, 'gpu_1_percent_unit': '%',
        'gpu_1_vram_used': 1 * GiB, 'gpu_1_vram_used_unit': 'bytes',
        # The dt of the per-user GPU-seconds: the interval
        'gpu_sample_seconds': INTERVAL, 'gpu_sample_seconds_unit': 's',
    }


@posix_only
def test_system_values_match_for_the_same_machine_state(metrics):
    def data(platform, run=1):
        return _by_type(metrics[platform][run], 'system_metrics')[0]['data']

    win, lin = data('windows'), data('linux')
    for key in ('cpu_percent', 'ram_percent', 'total_ram_bytes', 'network_bytes_in', 'network_bytes_out',
                'interval_s', 'cpu_count', 'disk_read_bytes_per_s', 'disk_write_bytes_per_s',
                'disk_read_iops', 'disk_write_iops', 'disk_await_ms', 'gpu_sample_seconds'):
        assert win[key] == lin[key], key
    # The CPU average over the interval from each OS's own counters, not a 1-s sample
    assert win['cpu_percent'] == 42.5 and win['interval_s'] == INTERVAL and win['cpu_count'] == NCPU
    assert lin['cpu_iowait_percent'] == 0.0 and lin['cpu_steal_percent'] == 0.0
    # Disk I/O of the physical disk only: not its partition, the md array or loop devices
    assert win['disk_read_bytes_per_s'] == int(DISK_RATE[2]) and win['disk_read_iops'] == DISK_RATE[0]
    assert win['disk_await_ms'] == round((DISK_RATE[4] + DISK_RATE[5]) / (DISK_RATE[0] + DISK_RATE[1]), 1)
    assert lin['disk_busy_max_percent'] == round(DISK_RATE[6] / 10, 1) and lin['disk_busy_max_name'] == 'sda'
    for platform in ('windows', 'linux'):
        assert 0 < data(platform)['proc_cpu_coverage_percent'] <= 100
    # Loopback not counted on either OS, nor docker0 / the veths on Linux. A
    # container stopping between the runs (its veth gone) doesn't make the
    # Linux counters drop, which the Manager would take as a reboot.
    assert win['network_bytes_in'] == 5_000_000 + 7_500_000 * 2
    assert abs(win['uptime_seconds'] - lin['uptime_seconds']) <= 5
    for platform in ('windows', 'linux'):
        first = data(platform, 0)
        assert first['network_bandwidth_in_mbps'] is None            # no previous sample yet
        assert first['interval_s'] is None and first['cpu_percent'] == 42.5   # first run: the 1-s sample
        assert first['disk_read_bytes_per_s'] is None
        assert isinstance(data(platform)['network_bandwidth_in_mbps'], float)


USER_KEYS = {'data_type', 'username', 'cpu_percent', 'ram_bytes', 'ram_formatted',
             'cpu_seconds', 'cpu_cores', 'interval_s', 'process_count',
             'gpu_vram_bytes', 'gpu_util_percent', 'gpu_busy_seconds', 'gpus_held', 'gpu_held_seconds',
             'gpu_processes'}


@posix_only
def test_user_metrics_have_the_same_shape_and_values(metrics):
    for run in (0, 1):
        win = _by_type(metrics['windows'][run], 'user_metrics')
        lin = _by_type(metrics['linux'][run], 'user_metrics')
        assert win and lin
        assert all(set(m['data']) == USER_KEYS for m in win + lin)       # one key set for every message
        assert shape(win[0]['header']) == shape(lin[0]['header'])
        by_user = {}
        for m in win + lin:
            by_user.setdefault(_plain_user(m['data']['username']), set()).add(repr(shape(m['data'])))
        assert all(len(s) == 1 for s in by_user.values()), by_user       # same types for a user on both

    win = {_plain_user(m['data']['username']): m['data'] for m in _by_type(metrics['windows'][1], 'user_metrics')}
    lin = {m['data']['username']: m['data'] for m in _by_type(metrics['linux'][1], 'user_metrics')}
    assert {m['data']['username'] for m in _by_type(metrics['windows'][1], 'user_metrics')} == {'LAB\\alice', 'LAB\\bob'}

    linux_only, _reason = KNOWN_DIFFERENCES['user_metrics: Linux-only accounts']
    assert set(lin) - set(win) == linux_only
    assert set(win) - set(lin) == set()
    # The collector is never a user's workload, nor is the disk scan running
    # at the same time: on Windows both run as SYSTEM (excluded), on Linux as
    # labmon. Daemon accounts are hidden on both.
    assert not {'labmon', 'systemd-resolve', 'messagebus', 'nvidia-persistenced', 'nobody'} & set(lin)
    for user in ('alice', 'bob'):
        assert {k: v for k, v in win[user].items() if k != 'username'} == \
               {k: v for k, v in lin[user].items() if k != 'username'}, user
    # Per-user CPU over the 300 s interval, on the system's 0-100 % scale: (400 + 32) % of a core / 64 cores
    assert lin['alice']['cpu_seconds'] == 1296.0 and lin['alice']['cpu_cores'] == 4.32
    assert lin['alice']['cpu_percent'] == 6.75 and lin['alice']['ram_bytes'] == 9 * GiB
    assert lin['alice']['interval_s'] == INTERVAL and lin['alice']['process_count'] == 2
    # bob's process that exited before the second run isn't counted
    assert lin['bob'] == {**lin['bob'], 'cpu_percent': 1.0, 'cpu_seconds': 192.0, 'ram_bytes': 2 * GiB,
                          'process_count': 1}
    assert lin['root']['cpu_percent'] == 10.02            # (1 + 640) / 64: system + the container
    # Per-user GPU: alice holds both GPUs, GPU 1 has no utilisation reading
    assert {k: v for k, v in lin['alice'].items() if k.startswith('gpu')} == {
        'gpu_vram_bytes': 10 * GiB + 512 * MiB, 'gpu_util_percent': None, 'gpu_busy_seconds': None,
        'gpus_held': 2, 'gpu_held_seconds': 2 * INTERVAL, 'gpu_processes': 2}
    assert {k: v for k, v in lin['bob'].items() if k.startswith('gpu')} == {
        'gpu_vram_bytes': 1 * GiB, 'gpu_util_percent': 9.2, 'gpu_busy_seconds': 27.8,
        'gpus_held': 1, 'gpu_held_seconds': INTERVAL, 'gpu_processes': 1}
    # root runs nothing on the GPUs: measured zeros
    assert lin['root']['gpus_held'] == 0 and lin['root']['gpu_vram_bytes'] == 0
    # First run: no baseline, so no CPU-seconds yet, the same on both
    for platform in ('windows', 'linux'):
        for m in _by_type(metrics[platform][0], 'user_metrics'):
            assert m['data']['cpu_seconds'] is None and m['data']['cpu_percent'] is None
            assert m['data']['interval_s'] is None and m['data']['ram_bytes'] > 0


@posix_only
def test_windows_wddm_differs_only_in_per_process_vram(hosts, collector):
    runs = _two_runs(hosts, collector, nvml=lambda: make_fake_pynvml(driver_model='wddm'))

    def report(platform):
        system = _by_type(runs[platform][1], 'system_metrics')[0]['data']
        users = {_plain_user(m['data']['username']): m['data'] for m in _by_type(runs[platform][1], 'user_metrics')}
        return _plain_users(system['gpus']), {u: users[u] for u in ('alice', 'bob')}

    win_gpus, win_users = report('windows')
    lin_gpus, lin_users = report('linux')
    # Same keys; the per-process VRAM is null under WDDM, everything else is equal
    assert [set(g) for g in win_gpus] == [set(g) for g in lin_gpus]
    assert win_gpus == _null(lin_gpus, WDDM_NULL - {'unattributed_vram_bytes'})
    assert {u: {k: v for k, v in d.items() if k != 'username'} for u, d in win_users.items()} == \
           {u: {k: v for k, v in _null(d, WDDM_NULL).items() if k != 'username'} for u, d in lin_users.items()}
    assert win_users['alice']['gpu_vram_bytes'] is None and win_users['alice']['gpus_held'] == 2
    assert win_gpus[0]['util_split'] == 'process_samples'


@posix_only
def test_nvml_is_shut_down_after_every_run(hosts, collector):
    for platform in ('windows', 'linux'):
        nvml = make_fake_pynvml()
        hosts.run(platform, collector.collect_metrics, nvml=nvml)
        assert nvml.calls == Counter(init=1, shutdown=1)


@pytest.mark.parametrize('code,hint', [
    (9, 'kernel driver is not loaded, or /dev/nvidiactl cannot be opened'),
    (12, 'NVML library'),
    (18, 'reboot'),
])
@posix_only
def test_nvml_that_cannot_start_is_logged_the_same_on_both(hosts, collector, caplog, code, hint):
    for platform in ('windows', 'linux'):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            messages = hosts.run(platform, collector.collect_metrics,
                                 nvml=make_fake_pynvml(init_error=FakeNVMLError(code)))
        data = _by_type(messages, 'system_metrics')[0]['data']
        assert data['gpus'] == [] and data['gpu_count'] == 0
        assert not [k for k in data if re.match(r'gpu_\d+_', k)] and 'gpu_sample_seconds' not in data
        assert all('gpus_held' not in m['data'] for m in _by_type(messages, 'user_metrics'))
        warning = next(r.getMessage() for r in caplog.records if 'NVML could not start' in r.getMessage())
        assert f'(NVML error {code})' in warning and hint in warning, warning


@posix_only
def test_failing_nvml_queries_are_logged_but_not_supported_ones_are_not(hosts, collector, caplog):
    with caplog.at_level(logging.WARNING):
        hosts.run('linux', collector.collect_metrics)
    warnings = [r.getMessage() for r in caplog.records if 'NVML errors' in r.getMessage()]
    assert warnings == ['NVML errors (values sent as null): GPU 1 utilization: Unknown Error (NVML error 999).']


@posix_only
def test_without_nvidia_ml_py_there_are_no_gpu_fields_and_no_warning(hosts, collector, caplog):
    for platform in ('windows', 'linux'):
        with caplog.at_level(logging.WARNING):
            messages = hosts.run(platform, collector.collect_metrics, nvml='absent')
        data = _by_type(messages, 'system_metrics')[0]['data']
        assert data['gpus'] == [] and data['gpu_count'] == 0
        assert not [k for k in data if re.match(r'gpu_\d+_', k)] and 'gpu_sample_seconds' not in data
        assert all('gpus_held' not in m['data'] for m in _by_type(messages, 'user_metrics'))
    assert not [r for r in caplog.records if 'NVML' in r.getMessage()]


# ---------------------------------------------------------------------------
# folder_usage
# ---------------------------------------------------------------------------

def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)


def _folder_shape(data, base, pathmod=os.path):
    """folder_usage split into metadata (types), volumes (types, path-free keys) and folders (relative path -> bytes)."""
    volumes = data['_volumes']
    meta, vols, folders = {}, {}, {}
    for key, value in data.items():
        if key.startswith('_') or key in ('data_type', 'total_usage'):
            meta[key] = shape(value)
            continue
        vol = next((v for v in volumes if key in (v, f'{v}_total_bytes', f'{v}_free_bytes')), None)
        if vol is not None:
            vols['<volume>' + key[len(vol):]] = shape(value)
        else:
            folders[pathmod.relpath(key, base).replace(pathmod.sep, '/')] = value
    return meta, vols, folders


def _age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


@posix_only
def test_folder_usage_has_the_same_shape_and_sizes(hosts, collector, tmp_path):
    srv = tmp_path / 'srv'
    _write(str(srv / 'data' / 'alice' / 'results' / 'run1.bin'), 3000)
    _write(str(srv / 'data' / 'alice' / 'results' / '.ipynb_checkpoints' / 'x'), 200)   # hidden: counted
    _write(str(srv / 'data' / 'alice' / 'notes.txt'), 50)
    _write(str(srv / 'data' / 'bob' / 'model.pt'), 5000)
    _write(str(srv / 'data' / 'bob' / 'deep' / 'a' / 'b' / 'c.txt'), 7)
    os.makedirs(srv / 'data' / 'empty-project')
    _write(str(srv / 'data' / '.cache' / 'blob'), 999)                                   # hidden folder: skipped
    _write(str(srv / 'shared' / 'readme'), 123)
    _age(srv / 'data' / 'alice' / 'results' / 'run1.bin', 2 * 365)                      # cold for 1 year
    _age(srv / 'data' / 'bob' / 'model.pt', 4 * 365)                                     # ... and for 3
    changes = {'volumes': [str(srv)], 'scan_paths': [f'{srv}/data/*', str(srv / 'shared')]}

    # Two daily runs each: the first measures every folder without the
    # breakdown (no timings yet: the time guard), the second with it
    win = [hosts.run('windows', collector.collect_disk_usage, **changes) for _ in range(2)]
    assert len(hosts.procs.powershell) == 8                 # Windows measured through PowerShell
    lin = [hosts.run('linux', collector.collect_disk_usage, **changes) for _ in range(2)]
    assert len(hosts.procs.powershell) == 8                 # ... Linux with the Python scanner
    assert hosts.procs.unexpected == []

    for run in (0, 1):
        assert [m['data']['data_type'] for m in win[run]] == [m['data']['data_type'] for m in lin[run]] == [
            'folder_usage', 'storage_breakdown']
    win_fu, lin_fu = win[0][0], lin[0][0]
    assert shape(win_fu['header']) == shape(lin_fu['header'])
    win_meta, win_vols, win_folders = _folder_shape(win_fu['data'], str(srv))
    lin_meta, lin_vols, lin_folders = _folder_shape(lin_fu['data'], str(srv))
    assert win_meta == lin_meta
    assert win_vols == lin_vols == {'<volume>': 'int', '<volume>_total_bytes': 'int', '<volume>_free_bytes': 'int'}
    assert win_folders == lin_folders == {
        'data/alice': 3250, 'data/bob': 5007, 'data/empty-project': 0, 'shared': 123}
    for data in (win_fu['data'], lin_fu['data']):
        assert data['_unit'] == 'bytes' and data['_timed_out'] == [] and data['_errors'] == []
        assert data['_volumes'] == [str(srv)]
    # folder_usage is the same whether or not the breakdown ran
    assert win[1][0]['data'].keys() == win_fu['data'].keys() and lin[1][0]['data'].keys() == lin_fu['data'].keys()
    assert _folder_shape(lin[1][0]['data'], str(srv))[2] == lin_folders

    # storage_breakdown: one data_type and shape on both; Windows has no owners
    owner_fields, _reason = KNOWN_DIFFERENCES['storage_breakdown: owners null on Windows']
    for run in (0, 1):
        w, l = win[run][1]['data'], lin[run][1]['data']
        assert set(w) == set(l)
        assert shape(_null(w, owner_fields | {'reference_time'})) == shape(_null(l, owner_fields | {'reference_time'}))
        assert w['owner_source'] == 'none' and l['owner_source'] == 'posix_uid'
        assert all(f['owners'] is None and f['folder_owner'] is None for f in w['folders'])
    first = lin[0][1]['data']
    assert len(first['_breakdown_unavailable']) == 4 and all(f['cold_1y_bytes'] is None for f in first['folders'])
    w, l = win[1][1]['data'], lin[1][1]['data']
    assert w['_breakdown_unavailable'] == l['_breakdown_unavailable'] == []

    def numbers(data):
        return {os.path.relpath(f['path'], str(srv)): (f['bytes'], f['files'], f['cold_1y_bytes'], f['cold_3y_bytes'])
                for f in data['folders']}
    assert numbers(w) == numbers(l) == {
        'data/alice': (3250, 3, 3000, 0), 'data/bob': (5007, 2, 5000, 5000),
        'data/empty-project': (0, 0, 0, 0), 'shared': (123, 1, 0, 0)}
    assert w['device'] == {**_null(l['device'], owner_fields), 'owners': None, 'other': None}
    assert l['device']['owners'][0]['bytes'] == l['device']['bytes'] == 8380
    assert all(f['owners'] is not None and f['other']['owner_count'] == 0 for f in l['folders'])
    empty = next(f for f in l['folders'] if f['path'].endswith('empty-project'))
    assert empty['owners'] == [] and empty['folder_owner'] is not None      # collected: nobody owns a file


def _second_run(metrics, platform, data_type):
    found = _by_type(metrics[platform][1], data_type)
    assert len(found) == 1, (platform, data_type)
    return found[0]['data']


@posix_only
def test_sessions_count_clients_the_same_way(metrics):
    win, lin = (_by_type(metrics[p][0], 'sessions')[0]['data'] for p in ('windows', 'linux'))
    counts = {k: v for k, v in lin.items() if k == 'clients_total' or k.endswith('_clients')}
    assert counts == {k: v for k, v in win.items() if k == 'clients_total' or k.endswith('_clients')} == {
        'clients_total': 3, 'smb_clients': 1, 'ssh_clients': 1, 'rdp_clients': 1}    # nfs isn't listening
    common = {'_schema', '_unit', '_detail', '_sources', '_collect_seconds', '_list_state', '_list_hash',
              'clients_total', 'users_total', 'sessions_truncated', 'sessions', 'data_type'}
    assert common <= set(win) and common <= set(lin)
    assert shape({k: win[k] for k in common - {'_sources', 'sessions'}}) == \
        shape({k: lin[k] for k in common - {'_sources', 'sessions'}})
    assert lin['_sources'] == {'tcp': 'ok', 'utmp': 'ok', 'smb': 'not_permitted'}
    assert win['_sources'] == {'tcp': 'ok', 'wts': 'ok', 'netsession': 'ok'}
    assert lin['ssh_users'] == 1 and win['rdp_users'] == 1 and win['smb_users'] == 1
    # One row per user session, then the clients no named session covers
    assert [(r['service'], r.get('username'), r['client'], r['source']) for r in lin['sessions']] == [
        ('ssh', 'alice', '10.11.2.40', 'utmp'), ('rdp', None, '10.11.2.50', 'tcp'), ('smb', None, '10.11.2.17', 'tcp')]
    assert [(r['service'], r.get('username'), r['client'], r['source']) for r in win['sessions']] == [
        ('rdp', 'LAB\\alice', '10.11.2.50', 'wts'), ('smb', 'bob', '10.11.2.17', 'netsession'),
        ('ssh', None, '10.11.2.40', 'tcp')]
    assert lin['sessions'][2]['connections'] == 2
    # The list was sent with the first run; the second only confirms it
    assert lin['_list_state'] == win['_list_state'] == 'full'
    for platform, first in (('windows', win), ('linux', lin)):
        second = _second_run(metrics, platform, 'sessions')
        assert second['_list_state'] == 'unchanged' and 'sessions' not in second
        assert second['_list_hash'] == first['_list_hash'] and second['clients_total'] == 3


@posix_only
def test_storage_health_has_the_same_message_keys(metrics):
    first = {p: _by_type(metrics[p][0], 'storage_health') for p in ('windows', 'linux')}
    assert all(len(found) == 1 for found in first.values())
    win, lin = first['windows'][0]['data'], first['linux'][0]['data']
    flat = re.compile(r'(disk|array|volume)_')
    keys = {p: {k for k in d if not flat.match(k)} for p, d in (('windows', win), ('linux', lin))}
    assert keys['windows'] == keys['linux']
    assert win['overall_status'] == 'ok' and win['sources'] == {'windows_storage': 'ok', 'events': 'ok'}
    assert win['disks'][0]['id'] == 'pd0' and win['disks'][0]['media'] == 'nvme' and win['events'] == {}
    assert win['disk_pd0_temperature_c'] == 41 and win['volume_e_status_code'] == 0
    assert lin['disks'][0]['id'] == 'sda' and lin['sources']['sysfs'] == 'ok' and lin['events'] is None
    assert [d['id'] for d in lin['disks']] == ['sda']            # not the partitions, md0 or loop0
    assert set(win['disks'][0]) == set(lin['disks'][0])          # one disk row layout
    # Unchanged and within the hour: nothing sent the second time
    assert not _by_type(metrics['windows'][1], 'storage_health') and not _by_type(metrics['linux'][1], 'storage_health')


# A Windows GPU server's E: drive, as the collector sees it through ntpath
WIN_DIRS = {'E:\\': ['Data', 'Shared'], 'E:\\Data': ['alice', 'bob', 'empty-project'],
            'E:\\Data\\alice': [], 'E:\\Data\\bob': [], 'E:\\Data\\empty-project': [], 'E:\\Shared': []}
WIN_SIZES = {'E:\\Data\\alice': 3250, 'E:\\Data\\bob': 5007, 'E:\\Data\\empty-project': 0, 'E:\\Shared': 123}


def _ntkey(path):
    return ntpath.normcase(ntpath.normpath(path)).rstrip('\\') or path


@posix_only
def test_folder_usage_with_windows_paths(hosts, collector, monkeypatch, tmp_path):
    """
    The scan_paths / volume logic with Windows path semantics (ntpath, '\\'),
    against the same tree measured by the Linux scanner. Listing and measuring
    are stubbed (E: doesn't exist here); everything between scan_paths and the
    message -- glob parsing, normalisation, the volume-root and duplicate
    checks, key naming -- is the real code.
    """
    dirs = {_ntkey(d): d for d in WIN_DIRS}
    win_path = types.ModuleType('ntpath')
    win_path.__dict__.update(ntpath.__dict__)
    win_path.isdir = lambda p: _ntkey(p) in dirs
    win_os = types.ModuleType('os')
    win_os.__dict__.update(os.__dict__)
    win_os.path, win_os.sep, win_os.altsep = win_path, '\\', '/'

    listed, measured = [], []

    def discover_folders(root, nas_type, validate_volume=True, raise_errors=False):
        assert nas_type == 'windows'
        listed.append(root)
        return [ntpath.join(root, name) for name in WIN_DIRS[dirs[_ntkey(root)]]]

    def scan_folders(folders, timeout=None, deadline=None, breakdown=None):
        measured.extend(folders)
        return {'sizes': {f: WIN_SIZES[dirs[_ntkey(f)]] for f in folders}, 'timed_out': [], 'errors': []}

    def measure_volume_capacity(vol):
        return {'total_bytes': 4 * 10 ** 12, 'free_bytes': 3 * 10 ** 12} if _ntkey(vol) == 'e:' else {}

    real_measure = collector._measure_scan_paths

    def measure_with_ntpath(*args, **kwargs):
        with monkeypatch.context() as m:
            m.setattr(collector, 'os', win_os)
            return real_measure(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(collector, '_measure_scan_paths', measure_with_ntpath)
        m.setattr(collector, 'discover_folders', discover_folders)
        m.setattr(collector, 'scan_folders', scan_folders)
        m.setattr(collector, 'measure_volume_capacity', measure_volume_capacity)
        win = hosts.run('windows', collector.collect_disk_usage, volumes=['E:\\'], scan_paths=[
            'E:\\Data\\*',          # backslash glob
            'e:/Shared',            # forward slashes, other case
            'E:',                   # the volume root ('E:\\'): reported from capacity, not scanned
            'E:\\data\\ALICE\\',    # already measured through the glob (case-insensitive)
        ])
    assert listed == ['E:\\Data']
    assert measured == ['E:\\Data\\alice', 'E:\\Data\\bob', 'E:\\Data\\empty-project', 'e:\\Shared']
    assert hosts.procs.powershell == [] and hosts.procs.unexpected == []

    data = win[0]['data']
    assert {k: v for k, v in data.items() if not k.startswith('_') and k != 'data_type'} == {
        'E:\\Data\\alice': 3250, 'E:\\Data\\bob': 5007, 'E:\\Data\\empty-project': 0, 'e:\\Shared': 123,
        'E:\\': 10 ** 12, 'E:\\_total_bytes': 4 * 10 ** 12, 'E:\\_free_bytes': 3 * 10 ** 12,
        'total_usage': 10 ** 12}
    assert data['_volumes'] == ['E:\\'] and data['_errors'] == [] and data['_timed_out'] == []

    # The same tree on the Linux server
    srv = tmp_path / 'srv'
    for folder, size in WIN_SIZES.items():
        linux_folder = srv.joinpath(*folder[len('E:\\'):].split('\\'))
        if size:
            _write(str(linux_folder / 'f.bin'), size)
        else:
            os.makedirs(linux_folder)
    lin = hosts.run('linux', collector.collect_disk_usage, volumes=[str(srv)],
                    scan_paths=[f'{srv}/Data/*', f'{srv}/Shared', str(srv), f'{srv}/Data/alice/'])

    win_meta, win_vols, win_folders = _folder_shape(data, 'E:\\', ntpath)
    lin_meta, lin_vols, lin_folders = _folder_shape(lin[0]['data'], str(srv))
    assert win_meta == lin_meta
    assert win_vols == lin_vols == {'<volume>': 'int', '<volume>_total_bytes': 'int', '<volume>_free_bytes': 'int'}
    assert win_folders == lin_folders == {'Data/alice': 3250, 'Data/bob': 5007, 'Data/empty-project': 0, 'Shared': 123}
