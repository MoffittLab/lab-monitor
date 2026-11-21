"""
Shared fixtures for the collector tests.

collector/collector.py is loaded by file path (it is a script, not a
package). It imports its own lib/ as the 'lib' package, appending the
collector directory to sys.path itself — the tests deliberately don't put it
first, so they also exercise that import path.
"""
import builtins
import importlib.util
import io
import json
import os
import shutil
import sys
import types

import pytest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COLLECTOR_DIR = os.path.join(REPO_DIR, 'collector')


def _load_collector():
    name = 'labmon_collector_main'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(COLLECTOR_DIR, 'collector.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector_module = _load_collector()


@pytest.fixture
def collector(monkeypatch, tmp_path):
    # build_header() would otherwise probe the network for the local IP
    monkeypatch.setattr(collector_module, 'get_local_ip', lambda: '10.0.0.1')
    # Not this host's interfaces (fake NIC names could match a real virtual
    # one, e.g. a veth eth0 inside a container) or account layout
    monkeypatch.setattr(collector_module, 'SYS_CLASS_NET', str(tmp_path / 'no-sys-class-net'))
    monkeypatch.setattr(collector_module, 'LOGIN_DEFS', str(tmp_path / 'no-login.defs'))
    return collector_module


@pytest.fixture
def local_queue():
    return collector_module.local_queue


@pytest.fixture
def disk_usage():
    from lib import disk_usage as module
    return module


@pytest.fixture
def config(tmp_path):
    return {
        'name': 'testbox',
        'id': 'linux-testbox',
        'device_type': 'Server',
        'manager_url': 'http://manager.invalid:5000',
        'manager_token': 'collector-token-0123456789',
        'data_dir': str(tmp_path / 'data'),
        'request_timeout_seconds': 5,
    }


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else '')

    def json(self):
        if self._body is None:
            raise ValueError('no JSON body')
        return self._body


class FakeManager:
    """
    Stand-in for requests.post. `handler(payload) -> FakeResponse` decides the
    reply; every decoded payload is kept in .calls.
    """

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or self.ack_all

    def __call__(self, url, data=None, json=None, headers=None, timeout=None):
        import json as _json
        payload = _json.loads(data) if data is not None else json
        self.calls.append({'url': url, 'payload': payload, 'headers': headers, 'timeout': timeout})
        return self.handler(payload)

    @staticmethod
    def ack(payload, statuses=None):
        """New-style ACK. statuses: {msg_id: status} (default: all 'stored')."""
        statuses = statuses or {}
        results = []
        for m in payload['messages']:
            mid = m['header']['msg_id']
            results.append({'msg_id': mid, 'status': statuses.get(mid, 'stored')})
        counts = {s: sum(1 for r in results if r['status'] == s)
                  for s in ('stored', 'duplicate', 'rejected', 'error')}
        return FakeResponse(200, {
            'status': 'ok', 'queue_id': payload['queue_id'],
            'received': len(results),
            'stored': counts['stored'] + counts['duplicate'] + counts['rejected'],
            'counts': counts, 'results': results,
        })

    @classmethod
    def ack_all(cls, payload):
        return cls.ack(payload)


@pytest.fixture
def fake_manager(collector, monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(collector.requests, 'post', manager)
    return manager


def make_entries(collector, n, name='testbox'):
    return [
        {'header': collector.build_header(name, 'linux-testbox', 'Server'),
         'data': {'data_type': 'system_metrics', 'cpu_percent': float(i)}}
        for i in range(n)
    ]


def read_queue_file(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


@pytest.fixture
def helpers():
    """Helper callables/classes (conftest can't be imported directly under importlib mode)."""
    import types
    return types.SimpleNamespace(
        FakeResponse=FakeResponse,
        FakeManager=FakeManager,
        make_entries=lambda n, name='testbox': make_entries(collector_module, n, name),
        read_queue_file=read_queue_file,
    )


# ---------------------------------------------------------------------------
# Fake OS sources
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_proc(collector, monkeypatch):
    """
    Serve fake /proc files to collector.py only (shadows builtins.open in its
    module globals). Map a path to a string, a list of strings (one per open),
    or an exception instance.
    """
    files = {}
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path in files:
            content = files[path]
            if isinstance(content, BaseException):
                raise content
            if isinstance(content, list):
                content = content.pop(0)
            return io.StringIO(content)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(collector, 'open', fake_open, raising=False)
    return files


@pytest.fixture
def no_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, 'psutil', None)   # makes `import psutil` raise ImportError


CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
PAGE_SIZE = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096


class FakeProcfs:
    """
    A /proc and /sys/block tree under `root` for lib/perf_counters,
    lib/proc_accounting and lib/blockdev (install() points their PROC /
    SYS_BLOCK at it and makes the process behave as Linux). Set the
    attributes, then write() lays the files out (again after every change):

      uptime (s), boot_id (None: no file), btime, cpu {field: ticks},
      disks {name: [diskstats fields after the name]}, block {name: {'device':
      bool, 'size': sectors, 'rotational': 0/1}} (/sys/block), mounts (text),
      procs {pid: {'comm', 'state', 'ppid', 'cpu', 'child', 'start', 'rss',
      'uid'}} with cpu / child / start in clock ticks and rss in pages,
      unreadable {pid, ...} (EACCES: written with mode 0), passwd {uid: name}.
    """

    def __init__(self, root):
        self.root = root
        self.proc = os.path.join(str(root), 'proc')
        self.sys_block = os.path.join(str(root), 'sys', 'block')
        self.uptime = 1000.0
        self.boot_id = 'b3b3b3b3-0000-4000-8000-000000000001'
        self.btime = 1_790_000_000
        self.cpu = dict(user=0, nice=0, system=0, idle=0, iowait=0, irq=0, softirq=0, steal=0)
        self.disks = {}
        self.block = {}
        self.mounts = 'proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n'
        self.procs = {}
        self.unreadable = set()
        self.passwd = {}

    def proc_entry(self, pid, uid, cpu=0, child=0, start=1, ppid=1, rss=0, state='S', comm='work'):
        self.procs[pid] = dict(comm=comm, state=state, ppid=ppid, cpu=cpu, child=child,
                               start=start, rss=rss, uid=uid)

    def write(self):
        for path in (self.proc, os.path.dirname(self.sys_block)):
            if os.path.exists(path):
                for dirpath, dirnames, _ in os.walk(path):
                    for d in dirnames:
                        os.chmod(os.path.join(dirpath, d), 0o755)
                shutil.rmtree(path)
        os.makedirs(os.path.join(self.proc, 'sys', 'kernel', 'random'))
        os.makedirs(self.sys_block)

        def put(path, text):
            with open(path, 'w') as f:
                f.write(text)

        put(os.path.join(self.proc, 'uptime'), f'{self.uptime:.2f} 12345.67\n')
        if self.boot_id is not None:
            put(os.path.join(self.proc, 'sys', 'kernel', 'random', 'boot_id'), self.boot_id + '\n')
        c = self.cpu
        line = ' '.join(str(c[k]) for k in ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal'))
        put(os.path.join(self.proc, 'stat'),
            f'cpu  {line} 0 0\ncpu0 {line} 0 0\nintr 1 2 3\nctxt 99\nbtime {self.btime}\nprocesses 5\n')
        put(os.path.join(self.proc, 'diskstats'), ''.join(
            f'   8       {i} {name} {" ".join(str(v) for v in fields)}\n'
            for i, (name, fields) in enumerate(self.disks.items())))
        put(os.path.join(self.proc, 'mounts'), self.mounts)
        for pid, p in self.procs.items():
            d = os.path.join(self.proc, str(pid))
            os.makedirs(d)
            utime, stime = p['cpu'] - p['cpu'] // 3, p['cpu'] // 3
            cutime, cstime = p['child'] - p['child'] // 2, p['child'] // 2
            put(os.path.join(d, 'stat'),
                f"{pid} ({p['comm']}) {p['state']} {p['ppid']} {pid} {pid} 0 -1 4194304 0 0 0 0 "
                f"{utime} {stime} {cutime} {cstime} 20 0 1 0 {p['start']} 1000000 {p['rss']} "
                f"18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0\n")
            uid = p['uid']
            put(os.path.join(d, 'status'),
                f"Name:\t{p['comm']}\nUmask:\t0022\nState:\t{p['state']}\nTgid:\t{pid}\nPid:\t{pid}\n"
                f"PPid:\t{p['ppid']}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            if pid in self.unreadable:
                for name in ('stat', 'status'):
                    os.chmod(os.path.join(d, name), 0)
        for name, info in self.block.items():
            d = os.path.join(self.sys_block, name)
            os.makedirs(os.path.join(d, 'queue'))
            if info.get('device', True):
                os.makedirs(os.path.join(d, 'device'))
            put(os.path.join(d, 'size'), f"{info.get('size', 0)}\n")
            put(os.path.join(d, 'queue', 'rotational'), f"{info.get('rotational', 1)}\n")
        return self

    def install(self, monkeypatch):
        from lib import blockdev, perf_counters, proc_accounting
        monkeypatch.setattr(perf_counters, 'PROC', self.proc)
        monkeypatch.setattr(proc_accounting, 'PROC', self.proc)
        monkeypatch.setattr(blockdev, 'SYS_BLOCK', self.sys_block)
        monkeypatch.setattr(proc_accounting, '_passwd_name', lambda uid: self.passwd.get(uid))
        monkeypatch.setattr(sys, 'platform', 'linux')
        return self


@pytest.fixture
def procfs(tmp_path, monkeypatch, collector):
    """A FakeProcfs installed for this test (the process behaves as Linux)."""
    fake = FakeProcfs(tmp_path / 'host')
    fake.install(monkeypatch)
    return fake


class FakeWindowsCounters:
    """
    The Windows counter sources behaving as a Windows host: GetTickCount64
    (tick_ms), GetSystemTimes (idle, kernel, user in 100 ns), and the
    psutil the counters use (process_iter rows, disk_io_counters).
    procs: {pid: dict(username, create_time, cpu (s), rss, ppid)}; a None
    cpu stands for an AccessDenied process.
    """

    def __init__(self):
        self.tick_ms = 1_000_000
        self.times = [0, 0, 0]
        self.procs = {}
        self.disks = {}

    def install(self, monkeypatch, psutil_module=None):
        from lib import perf_counters
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setattr(perf_counters, '_win_tick_count64', lambda kernel32=None: self.tick_ms)
        monkeypatch.setattr(perf_counters, '_win_system_times', lambda kernel32=None: tuple(self.times))
        ps = psutil_module or types.ModuleType('psutil')
        ps.process_iter = self.process_iter
        ps.disk_io_counters = self.disk_io_counters
        monkeypatch.setitem(sys.modules, 'psutil', ps)
        return self

    def process_iter(self, attrs=None):
        ns = types.SimpleNamespace
        rows = []
        for pid, p in self.procs.items():
            denied = p.get('cpu') is None
            rows.append(ns(pid=pid, info={
                'pid': pid, 'ppid': p.get('ppid', 4), 'username': None if denied else p.get('username'),
                'create_time': None if denied else p['create_time'],
                'cpu_times': None if denied else ns(user=p['cpu'] * 2 / 3, system=p['cpu'] / 3),
                'memory_info': None if denied else ns(rss=p.get('rss', 0))}))
        return rows

    def disk_io_counters(self, perdisk=False):
        assert perdisk
        ns = types.SimpleNamespace
        return {name: ns(read_count=d[0], write_count=d[1], read_bytes=d[2], write_bytes=d[3],
                         read_time=d[4], write_time=d[5]) for name, d in self.disks.items()}


@pytest.fixture
def windows_counters(monkeypatch, collector):
    return FakeWindowsCounters()


@pytest.fixture
def fakes():
    """The fake-source classes (conftest can't be imported directly under importlib mode)."""
    return types.SimpleNamespace(FakeProcfs=FakeProcfs, FakeWindowsCounters=FakeWindowsCounters,
                                 CLK_TCK=CLK_TCK, PAGE_SIZE=PAGE_SIZE)
