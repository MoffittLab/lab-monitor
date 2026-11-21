"""Unmeasured values are None/null (never 0): network, CPU, RAM, uptime, GPU."""
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

NEW_SYSTEM_FIELDS = ('interval_s', 'cpu_iowait_percent', 'cpu_steal_percent', 'proc_cpu_coverage_percent',
                     'disk_read_bytes_per_s', 'disk_write_bytes_per_s', 'disk_read_iops', 'disk_write_iops',
                     'disk_await_ms', 'disk_busy_max_percent', 'disk_busy_max_name')


def _no_gpu(collector):
    return lambda *a, **k: collector.gpu.GpuSample([], None, None)


def test_metrics_message_sends_null_not_zero(collector, config, monkeypatch):
    for getter in ('get_cpu_percent', 'get_ram_percent', 'get_total_ram_bytes', 'get_uptime_seconds'):
        monkeypatch.setattr(collector, getter, lambda: None)
    monkeypatch.setattr(collector, 'get_network_stats', lambda c, n: {
        'bytes_in': None, 'bytes_out': None, 'bandwidth_in_mbps': None, 'bandwidth_out_mbps': None})
    monkeypatch.setattr(collector.gpu, 'collect', lambda *a, **k: collector.gpu.GpuSample(
        [{'index': 0, 'name': 'GPU', 'gpu_percent': None, 'vram_used_bytes': None}], None, 300.0))
    # Nothing measurable: no counters, and per-process data without a window
    monkeypatch.setattr(collector, 'measure_counters', lambda *a, **k: {'status': 'none', 'users': {
        'alice': {'cpu_seconds': None, 'cpu_cores': None, 'cpu_percent': None, 'ram_bytes': 5,
                  'process_count': 1}}})
    captured = []
    monkeypatch.setattr(collector, '_record', lambda cfg, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)

    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    for key in ('cpu_percent', 'ram_percent', 'total_ram_bytes', 'uptime_seconds', 'uptime_formatted',
                'network_bytes_in', 'network_bytes_out',
                'network_bandwidth_in_mbps', 'network_bandwidth_out_mbps',
                'gpu_0_percent', 'gpu_0_vram_used') + NEW_SYSTEM_FIELDS:
        assert data[key] is None, key
    for key in NEW_SYSTEM_FIELDS:
        if key != 'disk_busy_max_name':
            assert key + '_unit' in data, key                       # units are sent with null values too
    assert data['gpu_sample_seconds'] == 300.0 and data['gpu_sample_seconds_unit'] == 's'
    assert json.loads(json.dumps(data))['cpu_percent'] is None     # serialises as JSON null
    assert len(captured[0]['header']['msg_id']) == 32
    user = captured[1]['data']
    assert user == {'data_type': 'user_metrics', 'username': 'alice', 'cpu_percent': None, 'ram_bytes': 5,
                    'ram_formatted': '5.0 B', 'cpu_seconds': None, 'cpu_cores': None, 'interval_s': None,
                    'process_count': 1}


def test_no_gpu_data_no_gpu_sample_seconds(collector, config, monkeypatch):
    monkeypatch.setattr(collector.gpu, 'collect', _no_gpu(collector))
    monkeypatch.setattr(collector, 'measure_counters', lambda *a, **k: {})
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 5.0)
    captured = []
    monkeypatch.setattr(collector, '_record', lambda cfg, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    assert 'gpu_sample_seconds' not in captured[0]['data'] and captured[0]['data']['gpu_count'] == 0


# ---------------------------------------------------------------------------
# user_metrics: merge, GPU-only users, user_metrics modes
# ---------------------------------------------------------------------------

def _cpu_user(cores=None, ram=0, procs=1, interval=300.0):
    seconds = None if cores is None else round(cores * interval, 2)
    return {'cpu_seconds': seconds, 'cpu_cores': cores, 'cpu_percent': None if cores is None else cores * 100 / 8,
            'ram_bytes': ram, 'process_count': procs}


GPU_USE = {'gpu_vram_bytes': 1024, 'gpu_util_percent': 0.0, 'gpu_busy_seconds': 0.0, 'gpus_held': 1,
           'gpu_held_seconds': 300.0, 'gpu_processes': 1}


def test_gpu_only_user_gets_a_message_with_null_cpu_and_ram(collector, config):
    rows = collector._user_metrics_data(config, {'alice': _cpu_user(1.0, ram=10)}, {'bob': GPU_USE}, 300.0)
    by_name = {r['username']: r for r in rows}
    assert by_name['bob'] == {'data_type': 'user_metrics', 'username': 'bob', 'cpu_percent': None,
                              'ram_bytes': None, 'ram_formatted': None, **GPU_USE}
    # On a host where per-user GPU worked, a user without GPU use has measured zeros
    assert by_name['alice']['gpus_held'] == 0 and by_name['alice']['gpu_vram_bytes'] == 0
    assert by_name['alice']['cpu_seconds'] == 300.0 and by_name['alice']['interval_s'] == 300.0


def test_without_per_user_gpu_data_the_gpu_fields_are_omitted(collector, config):
    rows = collector._user_metrics_data(config, {'alice': _cpu_user(1.0)}, None, 300.0)
    assert 'gpus_held' not in rows[0] and 'gpu_vram_bytes' not in rows[0]


def test_user_metrics_off_sends_no_user_messages(collector, config):
    config['user_metrics'] = 'off'
    assert collector._user_metrics_data(config, {'alice': _cpu_user(1.0)}, {'bob': GPU_USE}, 300.0) == []


def test_synology_auto_sends_only_active_users(collector, config):
    users = {'admin': _cpu_user(0.001, ram=50 * 2 ** 20),           # idle daemon account
             'busy': _cpu_user(0.5, ram=10 * 2 ** 20),              # CPU
             'big': _cpu_user(0.0, ram=2 * 2 ** 30)}                # RAM
    config['platform'] = 'synology'
    assert {r['username'] for r in collector._user_metrics_data(config, users, None, 300.0)} == {'busy', 'big'}
    # Without a window only the RAM (and GPU) tests apply
    no_window = {name: _cpu_user(None, ram=u['ram_bytes']) for name, u in users.items()}
    assert {r['username'] for r in collector._user_metrics_data(config, no_window, None, None)} == {'big'}
    config['platform'] = 'linux'                                     # auto = all elsewhere
    assert len(collector._user_metrics_data(config, users, None, 300.0)) == 3
    config.update(platform='linux', user_metrics='active', user_metrics_min_cores=0.0001)
    assert len(collector._user_metrics_data(config, users, None, 300.0)) == 3


def test_active_mode_keeps_gpu_holders(collector, config):
    config['user_metrics'] = 'active'
    rows = collector._user_metrics_data(config, {'carol': _cpu_user(0.0, ram=1)}, {'carol': GPU_USE}, 300.0)
    assert [r['username'] for r in rows] == ['carol']


# ---------------------------------------------------------------------------
# The metrics run: order, bounded optional collectors, state committed last
# ---------------------------------------------------------------------------

@pytest.fixture
def metrics_host(collector, config, monkeypatch, fake_manager):
    """collect_metrics with fixed values, the real _record/_sync and a fake Manager."""
    monkeypatch.setattr(collector, 'HEALTH_BUDGET_MARGIN_SECONDS', 0)
    monkeypatch.setattr(collector, 'measure_counters', lambda *a, **k: {})
    for getter, value in (('get_cpu_percent', 5.0), ('get_ram_percent', 20.0),
                          ('get_total_ram_bytes', 1000), ('get_uptime_seconds', 60)):
        monkeypatch.setattr(collector, getter, lambda value=value: value)
    monkeypatch.setattr(collector, 'get_network_stats', lambda c, n, persist=True: {})
    monkeypatch.setattr(collector.gpu, 'collect', _no_gpu(collector))
    return fake_manager


class _Optional:
    """Fake lib.sessions / lib.storage_health modules; `behaviour(kind)` runs inside the collector thread."""

    def __init__(self, monkeypatch, behaviour):
        import threading
        self.release = threading.Event()
        self.calls = []
        self.threads = []
        for kind in ('sessions', 'storage_health'):
            module = types.ModuleType(f'lib.{kind}')
            module.STATE_FILE = f'{kind}_state.json'

            def run(config, data_dir, name, force=False, logger=None, kind=kind):
                import threading as t
                self.threads.append(t.current_thread())
                self.calls.append(kind)
                return behaviour(kind)
            setattr(module, 'collect' if kind == 'sessions' else 'maybe_collect', run)
            module.failure_payload = lambda config, status: {'data_type': 'sessions', '_sources': {'tcp': status},
                                                             'clients_total': None}
            monkeypatch.setitem(sys.modules, f'lib.{kind}', module)


def _queued_types(collector, config):
    path = collector.get_queue_path(config, 'testbox')
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [m['data']['data_type'] for m in json.load(f)]


def _state(collector, config, kind):
    path = os.path.join(config['data_dir'], 'testbox', f'{kind}_state.json')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def test_system_metrics_are_queued_before_the_optional_collectors_run(collector, config, metrics_host, monkeypatch):
    seen = {}

    def behaviour(kind):
        seen[kind] = _queued_types(collector, config)
        return ({'data_type': kind, 'n': 1}, {'kind': kind})
    opt = _Optional(monkeypatch, behaviour)
    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    assert seen == {'sessions': ['system_metrics'], 'storage_health': ['system_metrics']}
    sent = [m['data']['data_type'] for call in metrics_host.calls for m in call['payload']['messages']]
    assert sent == ['system_metrics', 'sessions', 'storage_health'] and len(metrics_host.calls) == 1   # one sync
    assert _state(collector, config, 'sessions') == {'kind': 'sessions'}
    assert _state(collector, config, 'storage_health') == {'kind': 'storage_health'}
    import threading
    assert all(t is not threading.main_thread() for t in opt.threads)


@pytest.mark.parametrize('failure', ['hang', 'exception'])
def test_a_failing_optional_collector_costs_only_itself(collector, config, metrics_host, monkeypatch, failure):
    config['sessions'] = {'timeout_seconds': 1}
    config['storage_health'] = {'timeout_seconds': 1}

    def behaviour(kind):
        if failure == 'hang':
            opt.release.wait(30)
        else:
            raise RuntimeError(f'{kind} broke')
        return ({'data_type': kind}, {'sent': True})
    opt = _Optional(monkeypatch, behaviour)
    try:
        assert collector.collect_metrics(config, logging.getLogger('test')) is True
    finally:
        opt.release.set()
    sent = [m['data'] for call in metrics_host.calls for m in call['payload']['messages']]
    assert [d['data_type'] for d in sent] == ['system_metrics', 'sessions'] and len(metrics_host.calls) == 1
    status = 'timeout' if failure == 'hang' else 'error'
    assert sent[1] == {'data_type': 'sessions', '_sources': {'tcp': status}, 'clients_total': None}
    for t in opt.threads:
        t.join(5)
    # Never the failed thread's state; sessions' is reset, so the next list is sent in full
    assert _state(collector, config, 'sessions') == {'last_full_utc': None, 'hash': None}
    assert _state(collector, config, 'storage_health') is None


@pytest.mark.parametrize('failure', ['hang', 'exception'])
def test_after_a_failed_sessions_run_the_list_is_sent_in_full(collector, config, metrics_host, monkeypatch, failure):
    # The failed run's message says _list_state 'none' and a Manager clears
    # its list on that; 'unchanged' next would leave the list empty for an hour
    import threading
    from lib import sessions
    config.update({'platform': 'linux', 'storage_health': {'enabled': False}, 'sessions': {'timeout_seconds': 1}})
    login = {'service': 'ssh', 'username': 'alice', 'client': '10.0.0.5', 'source': 'utmp',
             'since': '2026-11-21T10:00:00Z'}
    monkeypatch.setattr(sessions, 'utmp_exists', lambda paths=None: True)
    monkeypatch.setattr(sessions, 'read_utmp', lambda paths=None, log=None: ('ok', [dict(login)]))
    monkeypatch.setattr(sessions, 'read_proc_net_tcp',
                        lambda proc_net=None, log=None: ('ok', {22}, [(22, '10.0.0.5', '10.0.0.2')]))
    real_collect, release = sessions.collect, threading.Event()

    def broken(*a, **k):
        if failure == 'hang':
            release.wait(30)
        raise RuntimeError('sessions broke')
    log = logging.getLogger('test')
    try:
        assert collector.collect_metrics(config, log) is True
        monkeypatch.setattr(sessions, 'collect', broken)
        collector.collect_metrics(config, log)
        monkeypatch.setattr(sessions, 'collect', real_collect)
        assert collector.collect_metrics(config, log) is True
    finally:
        release.set()
    sent = [m['data'] for call in metrics_host.calls for m in call['payload']['messages']
            if m['data']['data_type'] == 'sessions']
    assert [d['_list_state'] for d in sent] == ['full', 'none', 'full']
    assert sent[2]['sessions'] == sent[0]['sessions'] and sent[2]['_list_hash'] == sent[0]['_list_hash']


def test_abandoned_thread_finishing_later_commits_no_state(collector, config, metrics_host, monkeypatch):
    config['storage_health'] = {'timeout_seconds': 1}

    def behaviour(kind):
        if kind == 'storage_health':
            opt.release.wait(30)
            return ({'data_type': kind, 'late': True}, {'sent': 'late'})
        return ({'data_type': kind}, {'sent': kind})
    opt = _Optional(monkeypatch, behaviour)
    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    opt.release.set()
    for t in opt.threads:
        t.join(5)
    assert _state(collector, config, 'storage_health') is None
    assert _state(collector, config, 'sessions') == {'sent': 'sessions'}
    assert 'storage_health' not in _queued_types(collector, config) + [
        m['data']['data_type'] for call in metrics_host.calls for m in call['payload']['messages']]


def test_failed_record_of_the_optional_messages_commits_no_state(collector, config, metrics_host, monkeypatch):
    real_lock = collector.local_queue.QueueLock
    blocked = {'on': False}

    class Lock(real_lock):
        def __enter__(self):
            if blocked['on'] and self.lock_path.endswith('queue.json.lock'):
                raise collector.local_queue.QueueLockTimeout('forced')
            return super().__enter__()

    def behaviour(kind):
        blocked['on'] = True             # the queue lock times out from now on
        return ({'data_type': kind}, {'sent': kind})
    _Optional(monkeypatch, behaviour)
    monkeypatch.setattr(collector.local_queue, 'QueueLock', Lock)
    assert collector.collect_metrics(config, logging.getLogger('test')) is False
    assert _state(collector, config, 'sessions') is None and _state(collector, config, 'storage_health') is None
    assert _queued_types(collector, config) == ['system_metrics']


def test_disabled_or_missing_optional_collectors_are_skipped(collector, config, metrics_host, monkeypatch):
    calls = []
    _Optional(monkeypatch, lambda kind: calls.append(kind) or (None, None))
    config['sessions'] = {'enabled': False}
    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    assert calls == ['storage_health']
    monkeypatch.setitem(sys.modules, 'lib.storage_health', None)   # as if the file didn't exist
    calls.clear()
    assert collector.collect_metrics(config, logging.getLogger('test')) is True
    assert calls == []


def test_dry_run_prints_and_writes_nothing(collector, config, procfs, monkeypatch, capsys, fake_manager):
    procfs.cpu = dict(user=100, nice=0, system=50, idle=850, iowait=0, irq=0, softirq=0, steal=0)
    procfs.passwd = {1000: 'alice'}
    procfs.proc_entry(2001, 1000, cpu=50, rss=10)
    procfs.write()
    monkeypatch.setattr(collector.gpu, 'collect', _no_gpu(collector))
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 7.0)
    _Optional(monkeypatch, lambda kind: ({'data_type': kind, 'dry': True}, {'must': 'not be written'}))
    config['platform'] = 'linux'
    assert collector.collect_metrics(config, logging.getLogger('test'), dry_run=True) is True
    printed = json.loads(capsys.readouterr().out)
    assert [m['data']['data_type'] for m in printed] == ['system_metrics', 'user_metrics', 'sessions',
                                                         'storage_health']
    assert printed[1]['data']['username'] == 'alice' and printed[1]['data']['process_count'] == 1
    assert not os.path.exists(config['data_dir'])                  # no archive, queue, snapshot or state
    assert fake_manager.calls == []


def test_health_mode_forces_a_manual_check(collector, config, metrics_host, monkeypatch):
    seen = []

    def behaviour(kind):
        seen.append(kind)
        return ({'data_type': kind, 'trigger': 'manual'}, {'last_full': 1})
    _Optional(monkeypatch, behaviour)
    config['storage_health'] = {'enabled': False}                  # an explicit check runs anyway
    assert collector.collect_health(config, logging.getLogger('test')) is True
    assert seen == ['storage_health']
    sent = [m['data'] for call in metrics_host.calls for m in call['payload']['messages']]
    assert sent == [{'data_type': 'storage_health', 'trigger': 'manual'}]
    assert _state(collector, config, 'storage_health') == {'last_full': 1}


def test_health_mode_without_the_module_fails_cleanly(collector, config, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'lib.storage_health', None)
    with caplog.at_level(logging.ERROR):
        assert collector.collect_health(config, logging.getLogger('test')) is False
    assert 'not available' in caplog.text


def test_run_bounded(collector):
    import threading
    import time as _time
    assert collector._run_bounded(lambda a, b=0: a + b, 5, 2, b=3) == ('ok', 5)
    status, error = collector._run_bounded(lambda: 1 / 0, 5)
    assert status == 'error' and isinstance(error, ZeroDivisionError)
    release = threading.Event()
    started = _time.monotonic()
    try:
        assert collector._run_bounded(release.wait, 0.2, 30) == ('timeout', None)
        assert _time.monotonic() - started < 2
    finally:
        release.set()


def test_cli_accepts_health_and_dry_run(collector, config, monkeypatch, tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    called = []
    monkeypatch.setattr(collector, 'setup_logging', lambda *a: logging.getLogger('test'))
    monkeypatch.setattr(collector, 'collect_health', lambda c, l, dry_run=False: called.append(('health', dry_run)) or True)
    monkeypatch.setattr(collector, 'collect_metrics', lambda c, l, dry_run=False: called.append(('metrics', dry_run)) or True)
    monkeypatch.setattr(collector, 'collect_disk_usage', lambda c, l, dry_run=False: called.append(('disk', dry_run)) or True)
    for argv in (['--mode', 'health'], ['--mode', 'metrics', '--dry-run'], ['--dry-run']):
        monkeypatch.setattr(sys, 'argv', ['collector.py', '--config', str(path)] + argv)
        with pytest.raises(SystemExit) as exit_info:
            collector.main()
        assert exit_info.value.code == 0
    assert called == [('health', False), ('metrics', True), ('disk', True)]
