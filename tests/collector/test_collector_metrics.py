"""Unmeasured values are None/null (never 0): network, CPU, RAM, uptime, GPU."""
import builtins
import io
import json
import logging
import os
import sys
import types
from datetime import datetime

import pytest

PROC_NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 9999999     100    0    0    0     0          0         0  9999999     100    0    0    0     0       0          0
  eth0: 1000        10    0    0    0     0          0         0  2000        20    0    0    0     0       0          0
  eth1: 300          3    0    0    0     0          0         0  400          4    0    0    0     0       0          0
"""


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


def _fake_psutil(monkeypatch, **attrs):
    module = types.SimpleNamespace(**attrs)
    monkeypatch.setitem(sys.modules, 'psutil', module)
    return module


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def test_proc_net_dev_fallback_excludes_loopback(collector, fake_proc, no_psutil):
    fake_proc['/proc/net/dev'] = PROC_NET_DEV
    assert collector.get_network_counters() == (1300, 2400)


def test_psutil_path_also_excludes_loopback(collector, monkeypatch):
    nic = types.SimpleNamespace
    _fake_psutil(monkeypatch, net_io_counters=lambda pernic=False: {
        'lo': nic(bytes_recv=10**9, bytes_sent=10**9),
        'Loopback Pseudo-Interface 1': nic(bytes_recv=10**9, bytes_sent=10**9),
        'eth0': nic(bytes_recv=1000, bytes_sent=2000),
        'Ethernet 2': nic(bytes_recv=300, bytes_sent=400),
    })
    assert collector.get_network_counters() == (1300, 2400)


@pytest.mark.parametrize('name,expected', [
    ('lo', True), ('lo0', True), ('Loopback Pseudo-Interface 1', True),
    ('eth0', False), ('local-bridge', False), ('Ethernet', False), ('bond0', False),
])
def test_loopback_detection(collector, name, expected):
    assert collector._is_loopback(name) is expected


def test_network_unmeasurable_is_null_and_snapshot_untouched(collector, config, fake_proc, no_psutil):
    fake_proc['/proc/net/dev'] = FileNotFoundError('/proc/net/dev')
    snapshot = collector.get_network_snapshot_path(config, 'testbox')
    stats = collector.get_network_stats(config, 'testbox')
    assert stats == {'bytes_in': None, 'bytes_out': None,
                     'bandwidth_in_mbps': None, 'bandwidth_out_mbps': None}
    assert not os.path.exists(snapshot)


def test_psutil_counter_failure_is_null(collector, config, monkeypatch):
    def broken(pernic=False):
        raise OSError('no counters')
    _fake_psutil(monkeypatch, net_io_counters=broken)
    stats = collector.get_network_stats(config, 'testbox')
    assert set(stats.values()) == {None}


def test_bandwidth_null_on_first_run_and_after_reset(collector, config, fake_proc, no_psutil):
    snapshot = collector.get_network_snapshot_path(config, 'testbox')

    fake_proc['/proc/net/dev'] = PROC_NET_DEV
    first = collector.get_network_stats(config, 'testbox')
    assert first['bytes_in'] == 1300 and first['bandwidth_in_mbps'] is None

    # 60 s later, 7.5 MB more received → 1 Mbps
    with open(snapshot) as f:
        snap = json.load(f)
    snap['timestamp'] = '2000-01-01T00:00:00Z'
    with open(snapshot, 'w') as f:
        json.dump(snap, f)
    collector_time = collector._utcnow

    try:
        from datetime import datetime
        collector._utcnow = lambda: datetime(2000, 1, 1, 0, 1, 0)
        fake_proc['/proc/net/dev'] = PROC_NET_DEV.replace('eth0: 1000 ', 'eth0: 7501000 ')
        second = collector.get_network_stats(config, 'testbox')
        assert second['bandwidth_in_mbps'] == pytest.approx(1.0)
        assert second['bandwidth_out_mbps'] == 0.0

        # Counter went backwards (reboot): bandwidth unknown, not 0
        collector._utcnow = lambda: datetime(2000, 1, 1, 0, 2, 0)
        fake_proc['/proc/net/dev'] = PROC_NET_DEV.replace('eth0: 1000 ', 'eth0: 10 ')
        third = collector.get_network_stats(config, 'testbox')
        assert third['bytes_in'] == 310
        assert third['bandwidth_in_mbps'] is None
    finally:
        collector._utcnow = collector_time


class _Nics:
    """Fake psutil.net_io_counters(pernic=True) with adjustable counters."""

    def __init__(self, monkeypatch, lo, eth):
        self.lo, self.eth = lo, eth
        _fake_psutil(monkeypatch, net_io_counters=self)

    def __call__(self, pernic=False):
        nic = types.SimpleNamespace
        assert pernic
        return {'lo': nic(bytes_recv=self.lo, bytes_sent=self.lo),
                'eth0': nic(bytes_recv=self.eth, bytes_sent=self.eth)}


def _write_snapshot(collector, config, data):
    path = collector.get_network_snapshot_path(config, 'testbox')
    with open(path, 'w') as f:
        json.dump(data, f)
    return path


def _read_snapshot(path):
    with open(path) as f:
        return json.load(f)


def test_upgraded_psutil_host_keeps_loopback_until_counters_reset(collector, config, monkeypatch):
    # An older collector summed every interface (loopback included) on the
    # psutil path. Dropping loopback at once would make the counters fall, and
    # the Manager would take that as a reboot and re-add all traffic since boot
    # to the lifetime totals. The switch waits for a real reset instead.
    nics = _Nics(monkeypatch, lo=50_000, eth=5_000_000)
    path = _write_snapshot(collector, config, {      # as written by the old collector
        'timestamp': '2000-01-01T00:00:00Z', 'bytes_in': 5_040_000, 'bytes_out': 5_040_000})

    stats = collector.get_network_stats(config, 'testbox')
    assert stats['bytes_in'] == 5_050_000                     # still includes loopback: no drop
    assert _read_snapshot(path)['loopback'] == 'included'

    nics.lo, nics.eth = 60_000, 5_100_000
    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == 5_160_000

    nics.lo, nics.eth = 10, 2_000                             # reboot: counters reset
    stats = collector.get_network_stats(config, 'testbox')
    assert stats['bytes_in'] == 2_000                         # loopback dropped at the reset
    assert stats['bandwidth_in_mbps'] is None
    assert _read_snapshot(path)['loopback'] == 'excluded'

    nics.lo, nics.eth = 10**9, 3_000                          # and stays excluded
    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == 3_000


def test_new_install_excludes_loopback_from_the_first_run(collector, config, monkeypatch):
    _Nics(monkeypatch, lo=50_000, eth=7_000)
    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == 7_000
    path = collector.get_network_snapshot_path(config, 'testbox')
    assert _read_snapshot(path)['loopback'] == 'excluded'


def test_old_snapshot_without_psutil_needs_no_transition(collector, config, fake_proc, no_psutil):
    # The old /proc/net/dev fallback already skipped 'lo'
    _write_snapshot(collector, config, {
        'timestamp': '2000-01-01T00:00:00Z', 'bytes_in': 1000, 'bytes_out': 2000})
    fake_proc['/proc/net/dev'] = PROC_NET_DEV
    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == 1300


# Linux virtual interfaces (docker0, veths, bridges, bonds, tunnels)

@pytest.fixture
def sysfs(collector, monkeypatch, tmp_path):
    """
    A fake /sys/class/net: nics(name, ...) adds interfaces, physical ones with
    a 'device' link. The process behaves as Linux.
    """
    root = tmp_path / 'sys-class-net'
    root.mkdir()
    monkeypatch.setattr(collector, 'SYS_CLASS_NET', str(root))
    monkeypatch.setattr(sys, 'platform', 'linux')

    def add(*names, physical=False):
        for name in names:
            (root / name).mkdir()
            if physical:
                (root / name / 'device').mkdir()
    return add


class _DockerHost:
    """Fake psutil.net_io_counters on a docker host: eno1 plus docker0 and one veth per running container."""

    def __init__(self, monkeypatch, sysfs):
        sysfs('lo', 'docker0', 'veth1a2b3c', 'veth4d5e6f', 'tailscale0')
        sysfs('eno1', physical=True)
        self.nics = {'lo': 10 ** 9, 'eno1': 10 ** 12, 'docker0': 10 ** 11,
                     'veth1a2b3c': 5 * 10 ** 10, 'veth4d5e6f': 10 ** 10, 'tailscale0': 10 ** 9}
        _fake_psutil(monkeypatch, net_io_counters=self)

    def __call__(self, pernic=False):
        assert pernic
        return {n: types.SimpleNamespace(bytes_recv=v, bytes_sent=v // 2) for n, v in self.nics.items()}


def test_virtual_interfaces_are_not_counted_on_linux(collector, monkeypatch, sysfs):
    _DockerHost(monkeypatch, sysfs)
    assert collector.get_network_counters() == (10 ** 12, 10 ** 12 // 2)       # eno1 only


def test_a_stopped_container_does_not_make_the_counters_drop(collector, config, monkeypatch, sysfs):
    # Summing docker0 + veths, a container's veth disappearing made the totals
    # fall: bandwidth null, and the Manager re-added all traffic since boot
    host = _DockerHost(monkeypatch, sysfs)
    first = collector.get_network_stats(config, 'testbox')
    assert first['bytes_in'] == 10 ** 12

    path = collector.get_network_snapshot_path(config, 'testbox')
    snap = _read_snapshot(path)
    assert snap['virtual'] == 'excluded' and snap['loopback'] == 'excluded'
    snap['timestamp'] = '2000-01-01T00:00:00Z'
    with open(path, 'w') as f:
        json.dump(snap, f)
    monkeypatch.setattr(collector, '_utcnow', lambda: datetime(2000, 1, 1, 0, 1, 0))
    del host.nics['veth1a2b3c']
    host.nics['eno1'] += 7_500_000
    second = collector.get_network_stats(config, 'testbox')
    assert second['bytes_in'] == 10 ** 12 + 7_500_000
    assert second['bandwidth_in_mbps'] == pytest.approx(1.0)


def test_proc_net_dev_fallback_leaves_out_virtual_interfaces(collector, fake_proc, no_psutil, sysfs):
    sysfs('lo', 'eth1')                                       # eth1: a bridge, say
    sysfs('eth0', physical=True)
    fake_proc['/proc/net/dev'] = PROC_NET_DEV
    assert collector.get_network_counters() == (1000, 2000)
    assert collector.get_network_counters(include_virtual=True) == (1300, 2400)


def test_bond_is_counted_through_its_physical_ports(collector, monkeypatch, sysfs):
    sysfs('bond0', 'bond0.100')
    sysfs('eno1', 'eno2', physical=True)
    nic = types.SimpleNamespace
    _fake_psutil(monkeypatch, net_io_counters=lambda pernic=False: {
        'bond0': nic(bytes_recv=300, bytes_sent=30), 'bond0.100': nic(bytes_recv=100, bytes_sent=10),
        'eno1': nic(bytes_recv=200, bytes_sent=20), 'eno2': nic(bytes_recv=100, bytes_sent=10)})
    assert collector.get_network_counters() == (300, 30)       # not 700: no double counting


def test_inside_a_container_every_interface_is_virtual_and_counted(collector, monkeypatch, sysfs):
    sysfs('lo', 'eth0')                                       # eth0 is the container's veth
    nic = types.SimpleNamespace
    _fake_psutil(monkeypatch, net_io_counters=lambda pernic=False: {
        'lo': nic(bytes_recv=10 ** 6, bytes_sent=10 ** 6), 'eth0': nic(bytes_recv=1000, bytes_sent=2000)})
    assert collector.get_network_counters() == (1000, 2000)


def test_interfaces_unknown_to_sysfs_are_counted(collector, monkeypatch, sysfs):
    sysfs('docker0')
    nic = types.SimpleNamespace
    _fake_psutil(monkeypatch, net_io_counters=lambda pernic=False: {
        'docker0': nic(bytes_recv=50, bytes_sent=50), 'eth0': nic(bytes_recv=1000, bytes_sent=2000)})
    assert collector.get_network_counters() == (1000, 2000)


def test_other_platforms_count_every_non_loopback_interface(collector, monkeypatch, sysfs):
    sysfs('vEthernet (WSL)')                                  # even if a sysfs entry existed
    monkeypatch.setattr(sys, 'platform', 'win32')
    nic = types.SimpleNamespace
    _fake_psutil(monkeypatch, net_io_counters=lambda pernic=False: {
        'Loopback Pseudo-Interface 1': nic(bytes_recv=10 ** 6, bytes_sent=10 ** 6),
        'vEthernet (WSL)': nic(bytes_recv=50, bytes_sent=50), 'Ethernet': nic(bytes_recv=1000, bytes_sent=2000)})
    assert collector.get_network_counters() == (1050, 2050)


def test_upgraded_host_keeps_virtual_interfaces_until_counters_go_backwards(
        collector, config, monkeypatch, sysfs):
    # Like loopback: dropping docker0/veths at once would make the counters
    # fall, which the Manager reads as a reboot
    host = _DockerHost(monkeypatch, sysfs)
    everything = sum(v for n, v in host.nics.items() if n != 'lo')
    path = _write_snapshot(collector, config, {              # written by the previous collector
        'timestamp': '2000-01-01T00:00:00Z', 'bytes_in': everything - 1000,
        'bytes_out': everything // 2, 'loopback': 'excluded'})

    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == everything
    assert _read_snapshot(path)['virtual'] == 'included'

    del host.nics['veth4d5e6f']                              # a container stops: counters go backwards
    stats = collector.get_network_stats(config, 'testbox')
    assert stats['bytes_in'] == 10 ** 12 and stats['bandwidth_in_mbps'] is None
    assert _read_snapshot(path)['virtual'] == 'excluded'

    host.nics['veth7a8b9c'] = 10 ** 11                       # and stays excluded
    sysfs('veth7a8b9c')
    assert collector.get_network_stats(config, 'testbox')['bytes_in'] == 10 ** 12


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def _stat(user, idle):
    return f"cpu  {user} 0 0 {idle} 0 0 0 0 0 0\ncpu0 {user} 0 0 {idle} 0 0 0 0 0 0\nintr 1 2 3\n"


def test_cpu_fallback_uses_two_proc_stat_samples(collector, fake_proc, no_psutil):
    fake_proc['/proc/stat'] = [_stat(100, 900), _stat(130, 970)]   # 30 busy of 100 → 30%
    assert collector.fallback_cpu_percent(interval=0) == 30.0


def test_cpu_counts_iowait_as_idle(collector, fake_proc):
    fake_proc['/proc/stat'] = ["cpu  10 0 0 80 10 0 0 0\n", "cpu  20 0 0 160 20 0 0 0\n"]
    assert collector.fallback_cpu_percent(interval=0) == 10.0


def test_cpu_fallback_failure_is_null(collector, fake_proc, no_psutil):
    fake_proc['/proc/stat'] = FileNotFoundError('/proc/stat')
    assert collector.get_cpu_percent() is None


def test_cpu_psutil_failure_is_null(collector, monkeypatch):
    def broken(interval=None):
        raise RuntimeError('boom')
    _fake_psutil(monkeypatch, cpu_percent=broken)
    assert collector.get_cpu_percent() is None


def test_cpu_no_longer_uses_loadavg(collector, fake_proc, no_psutil, monkeypatch):
    monkeypatch.setattr(os, 'getloadavg', lambda: pytest.fail('loadavg must not be used'))
    fake_proc['/proc/stat'] = [_stat(0, 100), _stat(50, 150)]
    monkeypatch.setattr(collector.time, 'sleep', lambda s: None)
    assert collector.get_cpu_percent() == 50.0


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------

MEMINFO_SHUFFLED = """\
MemFree:          100000 kB
SwapTotal:       2000000 kB
MemAvailable:     250000 kB
Buffers:           50000 kB
MemTotal:        1000000 kB
Cached:           100000 kB
"""


def test_ram_fallback_parses_meminfo_by_key(collector, fake_proc, no_psutil):
    fake_proc['/proc/meminfo'] = [MEMINFO_SHUFFLED, MEMINFO_SHUFFLED]
    assert collector.get_ram_percent() == 75.0
    assert collector.get_total_ram_bytes() == 1000000 * 1024


def test_ram_fallback_without_memavailable(collector, fake_proc, no_psutil):
    old_kernel = '\n'.join(l for l in MEMINFO_SHUFFLED.splitlines() if 'MemAvailable' not in l)
    fake_proc['/proc/meminfo'] = old_kernel
    # available = MemFree + Buffers + Cached = 250000 → 75 % used
    assert collector.get_ram_percent() == 75.0


def test_ram_unmeasurable_is_null(collector, fake_proc, no_psutil):
    fake_proc['/proc/meminfo'] = [PermissionError('nope'), PermissionError('nope')]
    assert collector.get_ram_percent() is None
    assert collector.get_total_ram_bytes() is None


def test_uptime_unmeasurable_is_null(collector, fake_proc, no_psutil):
    fake_proc['/proc/uptime'] = FileNotFoundError('/proc/uptime')
    assert collector.get_uptime_seconds() is None
    assert collector.format_uptime(None) is None


# ---------------------------------------------------------------------------
# The message itself
# ---------------------------------------------------------------------------

def test_metrics_message_sends_null_not_zero(collector, config, monkeypatch):
    for getter in ('get_cpu_percent', 'get_ram_percent', 'get_total_ram_bytes', 'get_uptime_seconds'):
        monkeypatch.setattr(collector, getter, lambda: None)
    monkeypatch.setattr(collector, 'get_network_stats', lambda c, n: {
        'bytes_in': None, 'bytes_out': None, 'bandwidth_in_mbps': None, 'bandwidth_out_mbps': None})
    monkeypatch.setattr(collector, 'get_gpu_stats', lambda: [
        {'index': 0, 'name': 'GPU', 'gpu_percent': None, 'vram_used_bytes': None}])
    monkeypatch.setattr(collector, '_seed_user_processes', lambda c: [])
    monkeypatch.setattr(collector, 'get_per_user_stats', lambda c, l, seeded_procs=None: [])
    captured = []
    monkeypatch.setattr(collector, '_record_and_sync',
                        lambda cfg, n, i, entries, log: captured.extend(entries) or True)

    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    for key in ('cpu_percent', 'ram_percent', 'total_ram_bytes', 'uptime_seconds', 'uptime_formatted',
                'network_bytes_in', 'network_bytes_out',
                'network_bandwidth_in_mbps', 'network_bandwidth_out_mbps',
                'gpu_0_percent', 'gpu_0_vram_used'):
        assert data[key] is None, key
    assert json.loads(json.dumps(data))['cpu_percent'] is None     # serialises as JSON null
    assert len(captured[0]['header']['msg_id']) == 32
