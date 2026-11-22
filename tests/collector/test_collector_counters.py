"""Interval counters: the clock window, CPU, disk I/O, the snapshot, block devices, platform facts."""
import ctypes
import json
import logging
import os
import struct
import sys
import types

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32',
                                reason='needs a POSIX host (os.getuid / os.sysconf / /proc / POSIX paths)')


LOG = logging.getLogger('test')


@pytest.fixture
def pc(collector):
    from lib import perf_counters
    return perf_counters


@pytest.fixture
def blockdev(collector):
    from lib import blockdev as module
    return module


@pytest.fixture
def wall(pc, monkeypatch):
    """The wall clock the counters read: wall['t'] (epoch seconds)."""
    clock = {'t': 1_795_000_000.0}
    monkeypatch.setattr(pc, '_wall_time', lambda: clock['t'])
    return clock


def _nas(procfs):
    """A small DSM-like host: two SATA disks, an md array, a partition, loop/zram/dm/synoboot."""
    procfs.block = {'sata1': {'size': 100}, 'sata2': {'size': 100, 'rotational': 0},
                    'md2': {'device': False}, 'loop0': {'device': False}, 'zram0': {'device': False},
                    'dm-0': {'device': False}, 'synoboot': {}, 'nvme0n1': {}}
    procfs.cpu = dict(user=1000, nice=0, system=500, idle=8000, iowait=100, irq=0, softirq=0, steal=0)
    procfs.disks = {name: [0] * 11 for name in ('sata1', 'sata1p1', 'sata2', 'md2', 'loop0', 'zram0',
                                                 'dm-0', 'synoboot', 'nvme0n1')}
    procfs.passwd = {0: 'root', 1000: 'alice'}
    procfs.proc_entry(1, 0, cpu=10, start=1, ppid=0, comm='init')
    procfs.proc_entry(200, 1000, cpu=100, start=50, rss=100)
    return procfs


def _advance(procfs, wall, seconds, user=0, system=0, idle=0, iowait=0, steal=0):
    procfs.uptime += seconds
    wall['t'] += seconds
    c = procfs.cpu
    c['user'] += user
    c['system'] += system
    c['idle'] += idle
    c['iowait'] += iowait
    c['steal'] += steal


def _measure(collector, config, procfs, dry_run=False):
    procfs.write()
    return collector.measure_counters(config, 'testbox', LOG, dry_run=dry_run)


def _snapshot(collector, config):
    with open(collector.get_perf_snapshot_path(config, 'testbox')) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CPU arithmetic
# ---------------------------------------------------------------------------

def _cpu(source='procfs', hz=100, **fields):
    names = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')
    if source == 'win32':
        names = ('idle', 'kernel', 'user')
    return {'source': source, 'hz': hz, 'f': {n: fields.get(n, 0) for n in names}}


def test_cpu_delta_with_iowait_and_steal(pc):
    prev = _cpu(user=100, system=50, idle=800, iowait=20, steal=5, nice=10, irq=5, softirq=10)
    cur = _cpu(user=300, system=100, idle=1300, iowait=70, steal=25, nice=30, irq=10, softirq=15)
    # deltas: user 200 nice 20 system 50 idle 500 iowait 50 irq 5 softirq 5 steal 20 = 850
    values = pc.cpu_metrics(prev, cur)
    busy = 850 - 500 - 50
    assert values['cpu_percent'] == round(busy * 100 / 850, 1)
    assert values['cpu_iowait_percent'] == round(50 * 100 / 850, 1)
    assert values['cpu_steal_percent'] == round(20 * 100 / 850, 1)
    assert values['attributable_s'] == pytest.approx((200 + 20 + 50) / 100)


def test_falling_iowait_is_clamped(pc):
    values = pc.cpu_metrics(_cpu(user=100, idle=100, iowait=50), _cpu(user=150, idle=150, iowait=40))
    assert values['cpu_iowait_percent'] == 0.0 and values['cpu_percent'] == 50.0


def test_other_counter_going_backwards_is_no_window(pc):
    assert pc.cpu_metrics(_cpu(user=100, idle=100), _cpu(user=90, idle=200)) is None
    assert pc.cpu_metrics(_cpu(user=1), _cpu('win32', user=2)) is None        # another source


def test_windows_cpu_kernel_time_includes_idle(pc):
    # kernel 600 of which idle 500: busy = 100 (kernel) + 300 (user) of 900 total
    values = pc.cpu_metrics(_cpu('win32', hz=10 ** 7), _cpu('win32', hz=10 ** 7, idle=500, kernel=600, user=300))
    assert values['cpu_percent'] == round(400 * 100 / 900, 1)
    assert values['cpu_iowait_percent'] is None and values['cpu_steal_percent'] is None
    assert values['attributable_s'] == pytest.approx(400 / 10 ** 7)


def test_getsystemtimes_arithmetic_with_mocked_ctypes(pc):
    def fill(p, value):
        p._obj.dwLowDateTime = value & 0xFFFFFFFF
        p._obj.dwHighDateTime = value >> 32

    class Kernel32:
        def GetSystemTimes(self, p_idle, p_kernel, p_user):
            fill(p_idle, 5 * 2 ** 32 + 7)
            fill(p_kernel, 9 * 2 ** 32 + 1)
            fill(p_user, 123)
            return 1

    k32 = Kernel32()
    k32.GetTickCount64 = lambda: 86_400_000          # a function: takes .restype like a ctypes one
    assert pc._win_system_times(k32) == (5 * 2 ** 32 + 7, 9 * 2 ** 32 + 1, 123)
    assert pc._win_tick_count64(k32) == 86_400_000
    assert k32.GetTickCount64.restype is ctypes.c_ulonglong      # 64-bit result, not the default int

    class Failing:
        def GetSystemTimes(self, *args):
            return 0
    assert pc._win_system_times(Failing()) is None


def test_windows_cpu_fallback_without_psutil(collector, windows_counters, monkeypatch, no_psutil):
    windows_counters.install(monkeypatch)
    samples = iter([(100, 200, 100), (180, 320, 180)])       # idle +80, kernel +120, user +80
    from lib import perf_counters
    monkeypatch.setattr(perf_counters, '_win_system_times', lambda kernel32=None: next(samples))
    monkeypatch.setitem(sys.modules, 'psutil', None)
    monkeypatch.setattr(collector.time, 'sleep', lambda s: None)
    assert collector.get_cpu_percent() == 60.0               # (200 - 80) / 200


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def test_first_run_uses_the_1s_sample_and_writes_the_snapshot(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    captured = []
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 33.3)
    monkeypatch.setattr(collector, '_record', lambda c, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    monkeypatch.setattr(collector.gpu, 'collect', lambda *a, **k: collector.gpu.GpuSample([], None, None))
    procfs.write()
    assert collector.collect_metrics(config, LOG) is True
    data = captured[0]['data']
    assert data['cpu_percent'] == 33.3 and data['interval_s'] is None
    assert data['cpu_iowait_percent'] is None and data['disk_read_bytes_per_s'] is None
    snap = _snapshot(collector, config)
    assert snap['v'] == 1 and snap['boot']['id'] == procfs.boot_id
    assert set(snap['disks']) == {'sata1', 'sata2', 'nvme0n1'}       # physical disks only
    assert snap['procs']['200'][:2] == [50, 100]
    assert 'alice' not in json.dumps(snap)                           # no usernames on disk


def test_second_run_averages_over_the_interval(collector, config, procfs, wall):
    _nas(procfs)
    first = _measure(collector, config, procfs)
    assert first['status'] == 'none' and 'interval_s' not in first
    _advance(procfs, wall, 300, user=6000, system=3000, idle=20000, iowait=1000)
    second = _measure(collector, config, procfs)
    assert second['status'] == 'ok' and second['interval_s'] == 300.0
    assert second['cpu_percent'] == 30.0 and second['cpu_iowait_percent'] == 3.3


def test_reboot_gives_since_boot_values(collector, config, procfs, wall):
    _nas(procfs)
    _measure(collector, config, procfs)
    procfs.boot_id = 'b3b3b3b3-0000-4000-8000-000000000002'
    procfs.uptime = 600.0
    procfs.cpu = dict(user=300, nice=0, system=300, idle=5400, iowait=0, irq=0, softirq=0, steal=0)
    wall['t'] += 3600
    result = _measure(collector, config, procfs)
    assert result['status'] == 'reboot' and result['interval_s'] == 600.0
    assert result['cpu_percent'] == 10.0


def test_reboot_longer_ago_than_the_max_window_rebaselines(collector, config, procfs, wall):
    _nas(procfs)
    _measure(collector, config, procfs)
    procfs.boot_id = 'other-boot'
    procfs.uptime = 2 * 86400.0
    result = _measure(collector, config, procfs)
    assert result['status'] == 'stale' and 'cpu_percent' not in result


def test_windows_monotonic_going_backwards_is_a_reboot(pc):
    prev = {'wall': 1000.0, 'mono': 500.0, 'boot_id': None, 'boot_time': 500.0}
    same = {'wall': 1300.0, 'mono': 800.0, 'boot_id': None, 'boot_time': 500.0}
    rebooted = {'wall': 1300.0, 'mono': 100.0, 'boot_id': None, 'boot_time': 1200.0}
    assert pc.window(prev, same) == ('ok', 300.0)
    assert pc.window(prev, rebooted) == ('reboot', 100.0)


def test_two_hour_gap_is_valid_and_a_day_and_more_rebaselines(collector, config, procfs, wall):
    _nas(procfs)
    _measure(collector, config, procfs)
    _advance(procfs, wall, 7200, user=100, idle=100)
    assert _measure(collector, config, procfs)['interval_s'] == 7200.0
    _advance(procfs, wall, 90000, user=100, idle=100)
    stale = _measure(collector, config, procfs)
    assert stale['status'] == 'stale' and 'cpu_percent' not in stale
    _advance(procfs, wall, 300, user=100, idle=100)
    assert _measure(collector, config, procfs)['interval_s'] == 300.0     # the new baseline counts


def test_short_window_keeps_the_previous_snapshot(collector, config, procfs, wall):
    _nas(procfs)
    _measure(collector, config, procfs)
    path = collector.get_perf_snapshot_path(config, 'testbox')
    before = open(path).read()
    _advance(procfs, wall, 3, user=10, idle=10)
    short = _measure(collector, config, procfs)
    assert short['status'] == 'short' and 'cpu_percent' not in short
    assert open(path).read() == before
    assert short['users']['alice']['cpu_seconds'] is None            # users still listed, no CPU
    _advance(procfs, wall, 297, user=10, idle=10)
    assert _measure(collector, config, procfs)['interval_s'] == 300.0  # measured from the kept baseline


def test_wall_clock_jump_does_not_change_the_interval(collector, config, procfs, wall, caplog):
    _nas(procfs)
    _measure(collector, config, procfs)
    _advance(procfs, wall, 300, user=10, idle=10)
    wall['t'] += 3600
    with caplog.at_level(logging.INFO):
        assert _measure(collector, config, procfs)['interval_s'] == 300.0
    assert 'Wall clock adjusted' in caplog.text


def test_without_boot_id_btime_identifies_the_boot(pc):
    prev = {'wall': 1000.0, 'mono': 500.0, 'boot_id': 'btime:500', 'boot_time': 500.0}
    now = {'wall': 1300.0, 'mono': 800.0, 'boot_id': 'btime:501', 'boot_time': 501.0}   # NTP moved btime
    assert pc.window(prev, now) == ('ok', 300.0)


@pytest.mark.parametrize('boot_id', [None, 'btime'])
def test_without_boot_id_a_clock_step_is_no_window_not_a_reboot(pc, boot_id, caplog):
    # Up 2 h, then the wall clock is stepped 700 s forward (an RTC corrected
    # after boot): the boot time moves, but mono went on, so this may not be
    # taken for a reboot, whose since-boot window would count 2 h of CPU again
    def clock(wall, mono):
        boot = wall - mono
        return {'wall': wall, 'mono': mono, 'boot_id': f'btime:{boot:.0f}' if boot_id else None, 'boot_time': boot}
    prev = clock(1_795_000_000.0, 7200.0)
    with caplog.at_level(logging.INFO):
        assert pc.window(prev, clock(1_795_000_000.0 + 300 + 700, 7500.0)) == ('none', None)
    assert 'wall-clock step or a reboot' in caplog.text
    assert pc.window(prev, clock(1_795_000_000.0 + 300 + 500, 7500.0)) == ('ok', 300.0)   # within 600 s
    assert pc.window(prev, clock(1_795_000_000.0 + 300, 120.0)) == ('reboot', 120.0)     # mono went back


def test_windows_clock_step_soon_after_boot_counts_no_cpu_twice(collector, config, windows_counters,
                                                                  monkeypatch, wall):
    w = windows_counters.install(monkeypatch)
    w.tick_ms = 2 * 3600 * 1000                                  # up 2 hours
    w.times = [10 ** 11, 2 * 10 ** 11, 10 ** 11]
    w.procs = {1000: {'username': 'LAB\\alice', 'create_time': wall['t'] - 5400, 'cpu': 4000.0}}
    assert collector.measure_counters(config, 'testbox', LOG)['status'] == 'none'
    w.tick_ms += 300_000
    wall['t'] += 300 + 700                                       # w32time stepped the clock 700 s
    w.times = [w.times[0] + 2000 * 10 ** 7, w.times[1] + 2500 * 10 ** 7, w.times[2] + 2300 * 10 ** 7]
    w.procs[1000]['cpu'] += 300.0
    stepped = collector.measure_counters(config, 'testbox', LOG)
    # Not a reboot (that gave interval 7500 s and alice 4300 CPU-s): a new baseline, nothing counted
    assert stepped['status'] == 'none' and 'interval_s' not in stepped
    assert stepped['users']['LAB\\alice']['cpu_seconds'] is None
    w.tick_ms += 300_000
    wall['t'] += 300
    w.times = [w.times[0] + 2000 * 10 ** 7, w.times[1] + 2500 * 10 ** 7, w.times[2] + 2300 * 10 ** 7]
    w.procs[1000]['cpu'] += 150.0
    after = collector.measure_counters(config, 'testbox', LOG)
    assert after['status'] == 'ok' and after['interval_s'] == 300.0
    assert after['users']['LAB\\alice']['cpu_seconds'] == 150.0


@pytest.mark.parametrize('content', ['{not json', '[]', json.dumps({'v': 2, 'boot': {}}),
                                     json.dumps({'v': 1, 'boot': 'x', 'source': {}})])
def test_corrupt_or_foreign_snapshot_is_a_new_baseline(collector, config, procfs, wall, content):
    _nas(procfs)
    path = collector.get_perf_snapshot_path(config, 'testbox')
    with open(path, 'w') as f:
        f.write(content)
    result = _measure(collector, config, procfs)
    assert result['status'] == 'none' and 'cpu_percent' not in result
    assert _snapshot(collector, config)['v'] == 1


def test_changed_cpu_source_is_no_window(collector, config, procfs, wall):
    _nas(procfs)
    _measure(collector, config, procfs)
    snap = _snapshot(collector, config)
    snap['source']['cpu'] = 'win32'
    with open(collector.get_perf_snapshot_path(config, 'testbox'), 'w') as f:
        json.dump(snap, f)
    _advance(procfs, wall, 300, user=10, idle=10)
    result = _measure(collector, config, procfs)
    assert result['status'] == 'none' and 'cpu_percent' not in result


def test_failed_snapshot_write_gives_null_counters_and_no_overlap(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    _measure(collector, config, procfs)
    _advance(procfs, wall, 300, user=3000, idle=27000)
    from lib import perf_counters
    real_save = perf_counters.save_snapshot
    monkeypatch.setattr(perf_counters, 'save_snapshot', lambda path, snap: False)
    failed = _measure(collector, config, procfs)
    assert 'cpu_percent' not in failed and failed['users']['alice']['cpu_seconds'] is None
    monkeypatch.setattr(perf_counters, 'save_snapshot', real_save)
    _advance(procfs, wall, 300, user=6000, idle=24000)
    # The next window starts at the last snapshot that was saved: 600 s, both
    # intervals' CPU, never a window that overlaps the one reported before
    result = _measure(collector, config, procfs)
    assert result['interval_s'] == 600.0 and result['cpu_percent'] == 15.0


def test_held_lock_gives_null_counters_and_leaves_the_snapshot(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    _measure(collector, config, procfs)
    path = collector.get_perf_snapshot_path(config, 'testbox')
    before = open(path).read()
    _advance(procfs, wall, 300, user=10, idle=10)
    monkeypatch.setattr(collector, 'COUNTER_LOCK_TIMEOUT', 0.2)
    # flock locks belong to the open file: a second QueueLock on the same
    # path waits even inside one process, like another collector run would
    with collector.local_queue.QueueLock(path):
        held = _measure(collector, config, procfs)
    assert held['status'] == 'locked' and 'cpu_percent' not in held and 'interval_s' not in held
    assert open(path).read() == before
    # The users are still listed without CPU, and the GPU part gets the owners
    alice = held['users']['alice']
    assert alice['cpu_seconds'] is None and alice['process_count'] == 1 and alice['ram_bytes'] > 0
    assert held['owner_of'][200] == 'alice' and held['uid_of'][200] == 1000


def test_users_are_reported_while_another_run_holds_the_lock(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    procfs.write()
    captured = []
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 12.0)
    monkeypatch.setattr(collector, '_record', lambda c, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    monkeypatch.setattr(collector, '_optional_collectors', lambda *a, **k: [])
    monkeypatch.setattr(collector.gpu, 'collect', lambda *a, **k: collector.gpu.GpuSample([], None, None))

    def user_rows():
        captured.clear()
        assert collector.collect_metrics(config, LOG) is True
        return {e['data']['username']: e['data'] for e in captured if e['data']['data_type'] == 'user_metrics'}
    normal = user_rows()
    monkeypatch.setattr(collector, 'COUNTER_LOCK_TIMEOUT', 0.2)
    with collector.local_queue.QueueLock(collector.get_perf_snapshot_path(config, 'testbox')):
        locked = user_rows()
    assert set(locked) == set(normal) == {'root', 'alice'}
    assert locked['alice']['ram_bytes'] == normal['alice']['ram_bytes'] and locked['alice']['process_count'] == 1
    assert locked['alice']['cpu_seconds'] is None and locked['alice']['cpu_percent'] is None


def test_failing_counters_still_report_the_users(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    from lib import perf_counters
    monkeypatch.setattr(perf_counters, 'read_cpu_counters', lambda: 1 / 0)
    result = _measure(collector, config, procfs)
    assert result['status'] == 'error' and 'cpu_percent' not in result
    assert result['users']['alice']['cpu_seconds'] is None and result['owner_of'][200] == 'alice'
    assert not os.path.exists(collector.get_perf_snapshot_path(config, 'testbox'))


def test_dry_run_reads_but_never_writes_the_snapshot(collector, config, procfs, wall):
    _nas(procfs)
    assert _measure(collector, config, procfs, dry_run=True)['status'] == 'none'
    assert not os.path.exists(config['data_dir'])
    _measure(collector, config, procfs)
    path = collector.get_perf_snapshot_path(config, 'testbox')
    before = sorted(os.listdir(os.path.dirname(path))), open(path).read()
    _advance(procfs, wall, 300, user=10, idle=10)
    assert _measure(collector, config, procfs, dry_run=True)['interval_s'] == 300.0
    assert (sorted(os.listdir(os.path.dirname(path))), open(path).read()) == before


def test_perf_counters_false_restores_the_old_path(collector, config, procfs, wall, monkeypatch):
    _nas(procfs)
    calls = []
    seeded = [{'proc': types.SimpleNamespace(pid=200), 'username': 'alice', 'memory_info': None}]
    monkeypatch.setattr(collector, 'measure_counters', lambda *a, **k: calls.append('counters') or {})
    monkeypatch.setattr(collector, '_seed_user_processes', lambda c: calls.append('seed') or seeded)
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: calls.append('1s') or 12.5)
    monkeypatch.setattr(collector, 'get_per_user_stats', lambda c, l, seeded_procs=None: [
        {'username': 'alice', 'cpu_percent': 3.0, 'ram_bytes': 10, 'ram_formatted': '10.0 B'}]
        if seeded_procs is seeded else pytest.fail('not seeded'))
    gpu_owners = []
    monkeypatch.setattr(collector.gpu, 'collect', lambda c, owner_of, *a, **k: gpu_owners.append(owner_of)
                        or collector.gpu.GpuSample([], None, None))
    captured = []
    monkeypatch.setattr(collector, '_record', lambda c, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    config['perf_counters'] = False
    assert collector.collect_metrics(config, LOG) is True
    assert calls == ['seed', '1s']
    assert gpu_owners == [{200: 'alice'}]        # per-user GPU names users as the CPU rows do
    system, user = captured[0]['data'], captured[1]['data']
    assert system['cpu_percent'] == 12.5 and system['interval_s'] is None
    assert user == {'data_type': 'user_metrics', 'username': 'alice', 'cpu_percent': 3.0,
                    'ram_bytes': 10, 'ram_formatted': '10.0 B'}


# ---------------------------------------------------------------------------
# Disks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('columns', [14, 18, 20])
def test_diskstats_with_14_18_and_20_columns(pc, procfs, columns):
    procfs.block = {'sda': {}}
    procfs.disks = {'sda': [10, 0, 80, 7, 20, 0, 160, 9, 0, 33, 40, 1, 2, 3, 4, 5, 6][:columns - 3]}
    procfs.write()
    disks = pc.read_disk_counters({})
    assert disks == {'source': 'procfs', 'disks': {
        'sda': {'k': 'disk', 'r': 10, 'rs': 80, 'rt': 7, 'w': 20, 'ws': 160, 'wt': 9, 'io': 33}}}


def test_short_diskstats_lines_are_skipped(pc, procfs):
    procfs.block = {'sda': {}}
    procfs.disks = {'sda': [1] * 10}
    procfs.write()
    assert pc.read_disk_counters({})['disks'] == {}


def test_block_device_classification(blockdev, procfs):
    _nas(procfs).block.update({'sr0': {}, 'mmcblk0': {}, 'mmcblk0boot0': {}, 'mmcblk0rpmb': {},
                               'nbd0': {'device': False}, 'ram0': {'device': False}})
    procfs.write()
    kinds = {name: blockdev.classify_block_device(name) for name in
             ('sata1', 'sata2', 'nvme0n1', 'mmcblk0', 'sata1p1', 'md2', 'loop0', 'zram0', 'dm-0',
              'synoboot', 'sr0', 'mmcblk0boot0', 'mmcblk0rpmb', 'nbd0', 'ram0')}
    assert kinds == {'sata1': 'disk', 'sata2': 'disk', 'nvme0n1': 'disk', 'mmcblk0': 'disk',
                     'sata1p1': None, 'md2': 'array', 'loop0': None, 'zram0': None, 'dm-0': None,
                     'synoboot': None, 'sr0': None, 'mmcblk0boot0': None, 'mmcblk0rpmb': None,
                     'nbd0': None, 'ram0': None}
    assert blockdev.classify_block_device('sata2', exclude=['sata2']) is None
    devices = {d['name']: d for d in blockdev.list_block_devices()}
    assert set(devices) == {'sata1', 'sata2', 'nvme0n1', 'mmcblk0', 'md2'}
    assert devices['sata2'] == {'name': 'sata2', 'kind': 'disk', 'rotational': False, 'size_bytes': 51200}


def test_classification_without_sysfs_uses_names(blockdev, tmp_path, monkeypatch):
    monkeypatch.setattr(blockdev, 'SYS_BLOCK', str(tmp_path / 'no-sys-block'))
    assert [blockdev.classify_block_device(n) for n in ('sda', 'sata3', 'nvme1n1', 'sda1', 'loop0', 'md0')] == \
        ['disk', 'disk', 'disk', None, None, 'array']
    assert blockdev.list_block_devices() == []


def _disk(r=0, w=0, rs=0, ws=0, rt=0, wt=0, io=0):
    return {'k': 'disk', 'r': r, 'w': w, 'rs': rs, 'ws': ws, 'rt': rt, 'wt': wt, 'io': io}


def test_disk_rates_md_left_out_and_busiest_disk(pc, procfs, wall, collector, config):
    _nas(procfs)
    _measure(collector, config, procfs)
    #            reads 1, sect-rd 2, ms-rd 3, writes 4, sect-wr 6, ms-wr 7, io_ticks 9
    procfs.disks['sata1'] = [300, 0, 6000, 900, 600, 0, 12000, 1800, 0, 30000, 0]
    procfs.disks['sata2'] = [0, 0, 0, 0, 300, 0, 600, 300, 0, 3000, 0]
    procfs.disks['md2'] = [10 ** 6] * 11                          # would double-count sata1/2
    _advance(procfs, wall, 300, user=1, idle=1)
    result = _measure(collector, config, procfs)
    assert result['disk_read_bytes_per_s'] == 6000 * 512 // 300
    assert result['disk_write_bytes_per_s'] == (12000 + 600) * 512 // 300
    assert result['disk_read_iops'] == 1.0 and result['disk_write_iops'] == 3.0
    assert result['disk_await_ms'] == round((900 + 1800 + 300) / 1200, 1)
    assert result['disk_busy_max_percent'] == 10.0 and result['disk_busy_max_name'] == 'sata1'


def test_busy_is_clamped_and_await_is_null_without_io(pc):
    prev = {'source': 'procfs', 'disks': {'sda': _disk()}}
    cur = {'source': 'procfs', 'disks': {'sda': _disk(io=400_000)}}
    out = pc.disk_metrics(prev, cur, 300.0)
    assert out['disk_busy_max_percent'] == 100.0
    assert out['disk_await_ms'] is None and out['disk_read_iops'] == 0.0 and out['disk_read_bytes_per_s'] == 0


def test_32bit_wrap_versus_64bit_reset(pc):
    prev = {'source': 'procfs', 'disks': {'sda': _disk(r=2 ** 32 - 100, rs=2 ** 32 - 1000)}}
    cur = {'source': 'procfs', 'disks': {'sda': _disk(r=200, rs=3000)}}
    wrapped = pc.disk_metrics(prev, cur, 100.0, wrap32=True)
    assert wrapped['disk_read_iops'] == 3.0 and wrapped['disk_read_bytes_per_s'] == 4000 * 512 // 100
    assert pc.disk_metrics(prev, cur, 100.0, wrap32=False)['disk_read_iops'] is None     # reset: skipped
    # A drop that a wrap can't explain (more than 20 GB/s) is a reset on 32-bit too
    big = {'source': 'procfs', 'disks': {'sda': _disk(rs=10 ** 9)}}
    small = {'source': 'procfs', 'disks': {'sda': _disk(rs=10)}}
    assert pc.disk_metrics(big, small, 1.0, wrap32=True)['disk_read_bytes_per_s'] is None


def test_procfs_time_counters_wrap_at_32_bits_on_every_kernel(pc):
    # /proc/diskstats prints ms reading, ms writing and io_ticks as 32-bit
    # unsigned ints even on 64-bit kernels; a busy disk's ms reading wraps
    # every few days and must not drop the disk from the totals
    prev = {'source': 'procfs', 'disks': {
        'sda': _disk(r=1000, rs=8000, rt=2 ** 32 - 1000, io=2 ** 32 - 500),
        'sdb': _disk(r=1000, rs=8000, rt=5000, io=100000)}}
    cur = {'source': 'procfs', 'disks': {
        'sda': _disk(r=31000, rs=8000 + 300 * 2 * 10 ** 6 // 512, rt=59000, io=279500),
        'sdb': _disk(r=1100, rs=8800, rt=5500, io=101000)}}
    out = pc.disk_metrics(prev, cur, 300.0, wrap32=False)
    assert out['disk_read_iops'] == 100.3 and out['disk_read_bytes_per_s'] == (300 * 2 * 10 ** 6 // 512 + 800) * 512 // 300
    assert out['disk_busy_max_name'] == 'sda' and out['disk_busy_max_percent'] == 93.3
    assert out['disk_await_ms'] == round((60000 + 500) / 30100, 1)
    # The 64-bit counters still count a drop as a reset, and psutil's times are 64-bit
    reset = {'source': 'procfs', 'disks': {'sda': _disk(r=10, rt=2 ** 32 - 1000)}}
    assert pc.disk_metrics(reset, {'source': 'procfs', 'disks': {'sda': _disk(r=5, rt=10)}}, 300.0) == \
        dict.fromkeys(pc.DISK_FIELDS)
    win = {'source': 'psutil', 'disks': {'d0': _disk(r=10, rt=2 ** 32 - 1000, io=None)}}
    assert pc.disk_metrics(win, {'source': 'psutil', 'disks': {'d0': _disk(r=20, rt=10, io=None)}},
                           300.0)['disk_read_iops'] is None


def test_hot_plugged_disk_is_skipped_for_one_run(pc):
    prev = {'source': 'procfs', 'disks': {'sda': _disk()}}
    cur = {'source': 'procfs', 'disks': {'sda': _disk(r=30), 'sdb': _disk(r=10 ** 6)}}
    assert pc.disk_metrics(prev, cur, 10.0)['disk_read_iops'] == 3.0
    only_new = {'source': 'procfs', 'disks': {'sdb': _disk(r=10)}}
    assert pc.disk_metrics(prev, only_new, 10.0) == dict.fromkeys(pc.DISK_FIELDS)


def test_windows_disks_from_psutil_busy_null_await_from_read_time(pc, windows_counters, monkeypatch):
    windows_counters.install(monkeypatch)
    #                     reads, writes, read B, write B, read ms, write ms
    windows_counters.disks = {'PhysicalDrive0': (100, 50, 10 ** 6, 2 * 10 ** 6, 400, 200),
                              'PhysicalDrive1': (0, 0, 0, 0, 0, 0)}
    first = pc.read_disk_counters({'disk_io_exclude': ['PhysicalDrive1']})
    assert first['source'] == 'psutil' and set(first['disks']) == {'PhysicalDrive0'}
    windows_counters.disks['PhysicalDrive0'] = (400, 150, 4 * 10 ** 6, 3 * 10 ** 6, 1600, 400)
    out = pc.disk_metrics(first, pc.read_disk_counters({'disk_io_exclude': ['PhysicalDrive1']}), 100.0)
    assert out['disk_read_bytes_per_s'] == 30000 and out['disk_write_bytes_per_s'] == 10000
    assert out['disk_await_ms'] == round((1200 + 200) / 400, 1)
    assert out['disk_busy_max_percent'] is None and out['disk_busy_max_name'] is None


def test_windows_without_psutil_has_no_disk_counters(pc, monkeypatch, no_psutil):
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert pc.read_disk_counters({}) is None


def test_disk_io_off(collector, config, procfs, wall):
    _nas(procfs)
    config['disk_io'] = False
    _measure(collector, config, procfs)
    _advance(procfs, wall, 300, user=1, idle=1)
    result = _measure(collector, config, procfs)
    assert result['interval_s'] == 300.0 and result['disk_read_bytes_per_s'] is None


def test_windows_counters_end_to_end(collector, config, windows_counters, monkeypatch, wall):
    w = windows_counters.install(monkeypatch)
    w.times = [10 ** 9, 2 * 10 ** 9, 10 ** 9]
    w.procs = {4: {'username': 'NT AUTHORITY\\SYSTEM', 'create_time': wall['t'] - 10 ** 5, 'cpu': 100.0}}
    first = collector.measure_counters(config, 'testbox', LOG)
    assert first['status'] == 'none'
    w.tick_ms += 300_000
    wall['t'] += 300
    w.times = [w.times[0] + 9 * 10 ** 9, w.times[1] + 10 * 10 ** 9, w.times[2] + 2 * 10 ** 9]
    result = collector.measure_counters(config, 'testbox', LOG)
    assert result['status'] == 'ok' and result['interval_s'] == 300.0
    assert result['cpu_percent'] == 25.0 and result['cpu_iowait_percent'] is None
    assert result['disk_read_bytes_per_s'] is None          # this fake psutil has no disks


# ---------------------------------------------------------------------------
# Platform facts and config values
# ---------------------------------------------------------------------------

@posix_only
def test_platform_info(collector, tmp_path, monkeypatch):
    from lib import platform_info, disk_usage
    version = tmp_path / 'VERSION'
    version.write_text('majorversion="7"\nminorversion="2"\nproductversion="7.2.1"\nbuildnumber="69057"\n')
    assert platform_info.dsm_version(str(version)) == {'majorversion': 7, 'minorversion': 2, 'buildnumber': 69057}
    assert platform_info.dsm_version(str(tmp_path / 'missing')) is None
    assert platform_info.is_32bit() is (struct.calcsize('P') == 4)
    assert platform_info.is_root() is (os.geteuid() == 0)
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert platform_info.is_root() is False
    assert platform_info.platform_kind({'platform': 'Synology'}) == 'synology'
    monkeypatch.setattr(disk_usage, 'detect_platform', lambda: 'linux')
    assert platform_info.platform_kind({}) == platform_info.platform_kind(None) == 'linux'
    assert collector.get_platform({}) == 'linux'                    # one source of truth


@pytest.mark.parametrize('key,raw,expected,warned', [
    ('perf_counters', None, True, False), ('perf_counters', 'no', True, True), ('perf_counters', False, False, False),
    ('counter_max_window_seconds', 3600, 3600, False), ('counter_max_window_seconds', 1, 86400, True),
    ('user_metrics', 'ACTIVE', 'active', False), ('user_metrics', 'some', 'auto', True),
    ('user_metrics_min_ram_bytes', 1.5, 1073741824, True), ('disk_io_exclude', ['sd*'], ['sd*'], False),
    ('disk_io_exclude', 'sd*', [], True), ('gpu_per_user', 'off', 'off', False),
    ('metrics_interval_seconds', True, 300, True), ('sessions.timeout_seconds', 5, 5, False),
])
def test_config_values_fall_back_to_defaults(collector, caplog, key, raw, expected, warned):
    from lib import settings
    config = {}
    if raw is not None:
        node = config
        parts = key.split('.')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = raw
    with caplog.at_level(logging.WARNING):
        assert settings.value(config, key) == expected
    assert (f'Invalid {key}=' in caplog.text) is warned
    assert settings.value({'sessions': 'nope'}, 'sessions.enabled') is True     # a block that isn't one
