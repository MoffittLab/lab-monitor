"""
Per-process and per-user CPU-seconds over the metrics interval.

read_processes() takes one pass over the process table:
  Linux/DSM  /proc/<pid>/stat and /proc/<pid>/status, with or without psutil
             (so armv7 DSM units without psutil get per-user data too)
  Windows    one psutil.process_iter() pass; without psutil there is none
Each process is (start, cpu, child_cpu, ppid, owner, rss_bytes, zombie): start
and the CPU times in the table's ticks (Linux: SC_CLK_TCK per second, start
since boot; Windows: seconds, start = create_time), owner the uid (Linux) or
the account name (Windows). The table's owner_of maps every pid to a user
name, excluded users included (per-user GPU attribution uses it); a uid
without an account is 'uid:<n>', as in storage_breakdown.

user_cpu() compares the table with the previous run's (perf_snapshot.json,
which keeps PIDs and counters but no names) and attributes CPU to users:
  - a process seen last time (same pid and start time): its CPU since then;
  - one started since the previous run: all of its CPU;
  - anything else (missed last time): nothing.
On Linux the CPU of processes that exited in between is recovered from their
parents' child times (cutime/cstime: the kernel adds a reaped child's whole
lifetime to its parent), less what those processes had already used at the
previous run; recovered time is capped so the total never exceeds what the
CPUs actually spent on processes, so errors only under-count.
proc_cpu_coverage_percent = attributed / attributable shows what is left
over (mostly short-lived processes on Windows, which has no child times).

Privacy: names, command lines and PIDs never leave the host; the snapshot
holds PIDs and counters only.
"""
import errno
import logging
import os
import sys
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

PROC = '/proc'

EACCES_LIMIT = 0.2       # more than this share of unreadable PIDs: no per-user data this run
HIDEPID_OPEN = ('0', 'off')

# Fields of a process tuple
START, CPU, CHILD, PPID, OWNER, RSS, ZOMBIE = range(7)


class ProcessTable:
    """
    One pass over the processes.

    source:   'procfs' or 'psutil'
    hz:       ticks per second of start / cpu / child (1.0 for psutil: seconds)
    procs:    {pid: (start, cpu, child, ppid, owner, rss_bytes, zombie)}
    owner_of: {pid: username} for every process whose owner is known
    per_user: False when per-user data must be skipped this run (hidepid)
    denied:   processes that couldn't be read (EACCES / AccessDenied)
    """
    __slots__ = ('source', 'hz', 'procs', 'owner_of', 'per_user', 'denied')

    def __init__(self, source: str, hz: float, procs: dict, owner_of: dict,
                 per_user: bool = True, denied: int = 0):
        self.source, self.hz, self.procs, self.owner_of = source, hz, procs, owner_of
        self.per_user, self.denied = per_user, denied

    def uid_of(self) -> dict:
        """{pid: uid} where the owner is a uid (procfs)."""
        return {pid: p[OWNER] for pid, p in self.procs.items() if isinstance(p[OWNER], int)}


def _passwd_name(uid: int) -> Optional[str]:
    """The account name of a uid, or None if it has none (pwd.getpwuid)."""
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return None


def user_name(uid: int, cache: dict) -> str:
    """uid -> account name ('uid:<n>' when it has none), cached for the run."""
    name = cache.get(uid)
    if name is None:
        try:
            name = _passwd_name(uid)
        except Exception:
            name = None
        name = cache[uid] = name or f'uid:{uid}'
    return name


def parse_stat(raw: bytes):
    """
    (state, ppid, utime+stime, cutime+cstime, starttime, rss_pages) from the
    text of /proc/<pid>/stat, or None. comm (field 2) may contain spaces and
    ')', so the fields are split after the last ')'.
    """
    end = raw.rfind(b')')
    if end < 0:
        return None
    rest = raw[end + 1:].split()
    try:
        return (rest[0].decode('ascii', 'replace'), int(rest[1]),
                int(rest[11]) + int(rest[12]), int(rest[13]) + int(rest[14]),
                int(rest[19]), int(rest[21]))
    except (IndexError, ValueError):
        return None


def status_uid(raw: bytes) -> Optional[int]:
    """The real uid (first value of the 'Uid:' line) from /proc/<pid>/status."""
    for line in raw.splitlines():
        if line.startswith(b'Uid:'):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _read_file(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


def hidepid_hides(mounts_text: str) -> bool:
    """
    True when the /proc mount uses hidepid (other users' processes hidden or
    unreadable) and this process can't see past it: not root, and not in the
    mount's gid= group.
    """
    options = None
    for line in mounts_text.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[1] == '/proc' and cols[2] == 'proc':
            options = cols[3].split(',')     # the last /proc mount is the one in use
    if not options:
        return False
    values = dict(o.split('=', 1) for o in options if '=' in o)
    hidepid = values.get('hidepid')
    if hidepid is None or hidepid in HIDEPID_OPEN:
        return False
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return False
    try:
        gid = int(values['gid'])
        if gid == os.getegid() or gid in os.getgroups():
            return False
    except (KeyError, ValueError, OSError, AttributeError):
        pass
    return True


def _read_procfs(proc_root: str, log: logging.Logger) -> Optional[ProcessTable]:
    try:
        names = os.listdir(proc_root)
    except OSError as e:
        log.warning(f"Cannot list {proc_root} ({e}): no per-process data this run")
        return None
    try:
        page_size = os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, ValueError, OSError):
        page_size = 4096
    try:
        hz = float(os.sysconf('SC_CLK_TCK'))
    except (AttributeError, ValueError, OSError):
        hz = 100.0
    procs, owner_of, names_cache = {}, {}, {}
    seen = denied = 0
    for entry in names:
        if not entry.isdigit():
            continue
        seen += 1
        pid = int(entry)
        try:
            parsed = parse_stat(_read_file(os.path.join(proc_root, entry, 'stat')))
            uid = status_uid(_read_file(os.path.join(proc_root, entry, 'status')))
        except PermissionError:
            denied += 1
            continue
        except (FileNotFoundError, ProcessLookupError):
            continue            # it exited while we looked
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.ESRCH):
                denied += 1
            continue
        if parsed is None:
            continue
        state, ppid, cpu, child, start, rss_pages = parsed
        procs[pid] = (start, cpu, child, ppid, uid, max(0, rss_pages) * page_size, state in ('Z', 'X'))
        if uid is not None:
            owner_of[pid] = user_name(uid, names_cache)
    per_user = True
    if seen and denied > EACCES_LIMIT * seen:
        log.warning(f"{denied} of {seen} processes in {proc_root} could not be read (hidepid or a "
                    f"sandbox?): per-user metrics are skipped this run")
        per_user = False
    else:
        try:
            with open(os.path.join(proc_root, 'mounts'), 'r') as f:
                hides = hidepid_hides(f.read())
        except OSError:
            hides = False
        if hides:
            log.warning(f"{proc_root} is mounted with hidepid, so other users' processes are hidden from "
                        f"this account: per-user metrics are skipped this run (run the collector as "
                        f"root, or as a member of the mount's gid= group)")
            per_user = False
    return ProcessTable('procfs', hz, procs, owner_of, per_user, denied)


def _read_psutil(log: logging.Logger) -> Optional[ProcessTable]:
    try:
        import psutil
    except ImportError:
        return None
    procs, owner_of = {}, {}
    denied = 0
    try:
        for proc in psutil.process_iter(['pid', 'ppid', 'create_time', 'cpu_times', 'memory_info', 'username']):
            info = proc.info
            pid = info.get('pid')
            if not pid:
                continue        # pid 0: the System Idle Process (its "CPU" is idle time)
            created, times = info.get('create_time'), info.get('cpu_times')
            if created is None or times is None:
                denied += 1     # AccessDenied (protected processes): not counted, lowers coverage
                continue
            mem = info.get('memory_info')
            username = info.get('username') or None
            procs[pid] = (float(created), float(times.user + times.system), 0.0, info.get('ppid') or 0,
                          username, getattr(mem, 'rss', 0) if mem else 0, False)
            if username:
                owner_of[pid] = username
    except Exception as e:
        log.warning(f"psutil process listing failed ({e}): no per-process data this run")
        return None
    return ProcessTable('psutil', 1.0, procs, owner_of, True, denied)


def read_processes(config: Optional[dict] = None, proc_root: Optional[str] = None,
                   log: Optional[logging.Logger] = None) -> Optional[ProcessTable]:
    """
    The process table (see ProcessTable), or None when per-process data is
    unavailable (Windows without psutil, other platforms, unreadable /proc).
    """
    log = log or logger
    if sys.platform == 'win32':
        return _read_psutil(log)
    if sys.platform.startswith('linux'):
        return _read_procfs(proc_root or PROC, log)
    return None


def prev_table(snap: Optional[dict]) -> Optional[dict]:
    """
    The previous run's processes from perf_snapshot.json:
    {'hz', 'mono', 'wall', 'procs': {pid: (start, cpu, child, ppid)}}, or None.
    """
    if not isinstance(snap, dict) or not isinstance(snap.get('procs'), dict):
        return None
    procs = {}
    for key, value in snap['procs'].items():
        try:
            procs[int(key)] = (value[0], value[1], value[2], int(value[3]))
        except (TypeError, ValueError, IndexError):
            continue
    boot = snap.get('boot') or {}
    return {'hz': snap.get('proc_hz'), 'mono': boot.get('mono'), 'wall': boot.get('wall'), 'procs': procs}


def zero_baseline(clock: dict, hz: float) -> dict:
    """The previous-run table at boot: no processes, so every process counts from its start."""
    return {'hz': hz, 'mono': 0.0, 'wall': clock['boot_time'], 'procs': {}}


def _same_process(prev_start, start, procfs: bool) -> bool:
    if procfs:
        return prev_start == start
    try:
        return abs(float(prev_start) - float(start)) <= 1.0     # create_time jitter on Windows
    except (TypeError, ValueError):
        return False


def _born_since(start, hz: float, prev: dict, procfs: bool) -> bool:
    try:
        if procfs:
            return start / hz > float(prev['mono']) - 1
        return float(start) >= float(prev['wall']) - 2
    except (TypeError, ValueError, KeyError):
        return False


def _reaper(pid: int, prev_procs: dict, matched: set) -> int:
    """The live (matched) ancestor whose child times absorb a dead process: up its old ppid chain, else 1."""
    seen = set()
    parent = prev_procs[pid][PPID]
    while parent not in matched and parent in prev_procs and parent not in seen:
        seen.add(parent)
        parent = prev_procs[parent][PPID]
    return parent if parent in matched else 1


def user_cpu(prev: Optional[dict], cur: Optional[ProcessTable], interval_s: Optional[float],
             attributable_s: Optional[float], ncpu: Optional[int], count_children: bool = True,
             keep: Optional[Callable] = None):
    """
    (users, coverage) for the window since `prev` (prev_table() or
    zero_baseline(); None = no baseline: CPU values None).

    users: {username: {'cpu_seconds', 'cpu_cores', 'cpu_percent', 'ram_bytes',
    'process_count'}} over the processes keep(pid, username, uid) accepts
    (all when keep is None); None when per-process data is unavailable or
    must be skipped (hidepid). cpu_percent = cpu_cores / ncpu x 100, the
    system's 0-100 % scale. process_count leaves out zombies.
    coverage: attributed / attributable CPU in % (0-100; every process
    counts, excluded users included), or None.
    """
    if cur is None or not cur.per_user:
        return None, None
    procfs = cur.source == 'procfs'
    hz = float(cur.hz)
    attributed = None
    if prev is not None and interval_s and interval_s > 0 and prev.get('hz') in (None, cur.hz):
        prev_procs = prev['procs']
        own, child, matched = {}, {}, set()
        for pid, p in cur.procs.items():
            old = prev_procs.get(pid)
            if old is not None and _same_process(old[START], p[START], procfs):
                own[pid] = max(0, p[CPU] - old[CPU])
                child[pid] = p[CHILD] - old[CHILD]
                matched.add(pid)
            elif _born_since(p[START], hz, prev, procfs):
                own[pid], child[pid] = p[CPU], p[CHILD]
            else:
                own[pid], child[pid] = 0, 0
        if procfs and count_children:
            for pid, old in prev_procs.items():
                if pid in matched:
                    continue                 # still running
                reaper = _reaper(pid, prev_procs, matched)
                if reaper in child:
                    child[reaper] -= old[CPU] + old[CHILD]
            for pid in child:
                child[pid] = max(0, child[pid])
            room = max(0.0, (attributable_s or 0) * hz - sum(own.values()))
            total_child = sum(child.values())
            if total_child > room:
                scale = room / total_child
                child = {pid: c * scale for pid, c in child.items()}
        else:
            child = {}
        per_pid = {pid: own[pid] + child.get(pid, 0) for pid in own}
        attributed = sum(per_pid.values())
    else:
        per_pid = None

    coverage = None
    if attributed is not None and attributable_s and attributable_s > 0:
        coverage = round(min(100.0, max(0.0, attributed / hz * 100.0 / attributable_s)), 1)

    users: Dict[str, dict] = {}
    for pid, p in cur.procs.items():
        name = cur.owner_of.get(pid)
        if not name:
            continue
        if keep is not None and not keep(pid, name, p[OWNER] if isinstance(p[OWNER], int) else None):
            continue
        u = users.get(name)
        if u is None:
            u = users[name] = {'ticks': 0.0, 'ram_bytes': 0, 'process_count': 0}
        if per_pid is not None:
            u['ticks'] += per_pid[pid]
        u['ram_bytes'] += p[RSS]
        if not p[ZOMBIE]:
            u['process_count'] += 1
    result = {}
    for name, u in users.items():
        row = {'cpu_seconds': None, 'cpu_cores': None, 'cpu_percent': None,
               'ram_bytes': int(u['ram_bytes']), 'process_count': u['process_count']}
        if per_pid is not None:
            seconds = u['ticks'] / hz
            cores = seconds / interval_s
            row.update(cpu_seconds=round(seconds, 2), cpu_cores=round(cores, 3),
                       cpu_percent=round(cores * 100.0 / ncpu, 2) if ncpu else None)
        result[name] = row
    return result, coverage
