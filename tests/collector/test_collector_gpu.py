"""Per-user GPU (lib/gpu.py) against a fake pynvml that fails the way nvidia-ml-py 12.535 does."""
import logging
import sys
import types

import pytest

GiB = 1024 ** 3
LOG = logging.getLogger('test')
ns = types.SimpleNamespace

NOT_SUPPORTED, NOT_FOUND, FUNCTION_NOT_FOUND = 3, 6, 13


class NVMLError(Exception):
    """Like pynvml.NVMLError: .value is the return code (12.535 also raises NVMLError(0) for 'no samples')."""

    def __init__(self, value):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return {3: 'Not Supported', 6: 'Not Found', 13: 'Function Not Found'}.get(self.value, f'code {self.value}')


def gpu(util=50, used=10 * GiB, total=48 * GiB, compute=(), graphics=(), mps=(), samples=None,
        uuid='GPU-0000', model=1, compute_error=None):
    """One fake GPU. compute/graphics/mps: [(pid, usedGpuMemory or None)]; samples: [(pid, smUtil)] or an error."""
    return dict(util=util, used=used, total=total, compute=compute, graphics=graphics, mps=mps,
                samples=samples, uuid=uuid, model=model, compute_error=compute_error)


def make_nvml(gpus, driver='550.54.15'):
    nvml = types.ModuleType('pynvml')
    nvml.NVMLError = NVMLError
    nvml.NVML_TEMPERATURE_GPU = 0
    nvml.calls = []

    def procs(items):
        return [ns(pid=pid, usedGpuMemory=mem) for pid, mem in items]

    def compute(h):
        nvml.calls.append(('compute', h['uuid']))
        if h['compute_error'] is not None:
            raise h['compute_error']
        return procs(h['compute'])

    def samples(h, last_seen):
        assert last_seen == 0                  # every buffered sample
        if isinstance(h['samples'], Exception):
            raise h['samples']
        if h['samples'] is None:
            raise NVMLError(NOT_FOUND)
        return [ns(pid=pid, smUtil=sm, memUtil=0, timeStamp=i) for i, (pid, sm) in enumerate(h['samples'])]

    nvml.nvmlInit = lambda: None
    nvml.nvmlShutdown = lambda: None
    nvml.nvmlSystemGetDriverVersion = lambda: driver
    nvml.nvmlDeviceGetCount = lambda: len(gpus)
    nvml.nvmlDeviceGetHandleByIndex = lambda i: gpus[i]
    nvml.nvmlDeviceGetName = lambda h: 'NVIDIA RTX A6000'
    nvml.nvmlDeviceGetUUID = lambda h: h['uuid']
    nvml.nvmlDeviceGetUtilizationRates = lambda h: ns(gpu=h['util'], memory=0)
    nvml.nvmlDeviceGetMemoryInfo = lambda h: ns(used=h['used'], total=h['total'], free=h['total'] - h['used'])
    nvml.nvmlDeviceGetTemperature = lambda h, sensor: 60
    nvml.nvmlDeviceGetPowerUsage = lambda h: 200_000
    nvml.nvmlDeviceGetComputeRunningProcesses = compute
    nvml.nvmlDeviceGetMPSComputeRunningProcesses = lambda h: procs(h['mps'])
    nvml.nvmlDeviceGetGraphicsRunningProcesses = lambda h: procs(h['graphics'])
    nvml.nvmlDeviceGetProcessUtilization = samples
    nvml.nvmlDeviceGetDriverModel = lambda h: [h['model'], h['model']]
    return nvml


@pytest.fixture
def gpus(collector, monkeypatch):
    """collect(gpus, owner_of, ...) with a fake pynvml installed; returns the GpuSample."""
    from lib import gpu as module

    def run(gpu_list, owner_of=None, interval_s=300.0, config=None, user_filter=None, driver='550.54.15'):
        nvml = make_nvml(gpu_list, driver)
        monkeypatch.setitem(sys.modules, 'pynvml', nvml)
        return module.collect(config or {}, owner_of, interval_s, LOG, user_filter=user_filter)
    run.module = module
    return run


OWNERS = {101: 'alice', 102: 'alice', 201: 'bob', 301: 'carol', 666: 'svc'}


def _users(dev):
    return {u['username']: u for u in dev['users']}


# ---------------------------------------------------------------------------
# Splitting the busy %
# ---------------------------------------------------------------------------

def test_single_user_gets_the_whole_busy_percent(gpus):
    s = gpus([gpu(util=87, compute=[(101, 20 * GiB), (102, GiB)], graphics=[(301, GiB)])], OWNERS)
    dev = s.devices[0]
    assert dev['util_split'] == 'single_user' and dev['process_source'] == 'nvml'
    assert dev['uuid'] == 'GPU-0000'
    assert dev['users'] == [{'username': 'alice', 'vram_bytes': 21 * GiB, 'util_percent': 87.0, 'processes': 2},
                            {'username': 'carol', 'vram_bytes': GiB, 'util_percent': 0.0, 'processes': 1}]
    assert dev['users_truncated'] == 0 and dev['unattributed_vram_bytes'] == 0 and dev['unattributed_processes'] == 0
    assert s.per_user['alice'] == {'gpu_vram_bytes': 21 * GiB, 'gpu_util_percent': 87.0, 'gpu_busy_seconds': 261.0,
                                   'gpus_held': 1, 'gpu_held_seconds': 300.0, 'gpu_processes': 2}
    # carol only has a graphics process: VRAM, but she holds no GPU
    assert s.per_user['carol'] == {'gpu_vram_bytes': GiB, 'gpu_util_percent': 0.0, 'gpu_busy_seconds': 0.0,
                                   'gpus_held': 0, 'gpu_held_seconds': 0.0, 'gpu_processes': 0}
    assert s.sample_seconds == 300.0


def test_60_20_split_between_two_users(gpus):
    s = gpus([gpu(util=80, compute=[(101, GiB), (201, GiB)], samples=[(101, 60), (201, 20)])], OWNERS)
    dev = s.devices[0]
    assert dev['util_split'] == 'process_samples'
    assert {n: u['util_percent'] for n, u in _users(dev).items()} == {'alice': 60.0, 'bob': 20.0}
    assert s.per_user['alice']['gpu_busy_seconds'] == 180.0 and s.per_user['bob']['gpu_busy_seconds'] == 60.0


def test_split_weights_sum_every_sample_of_a_pid(gpus):
    # alice busy through the whole buffer, bob in one sample only: 3:1, not the newest sample's 1:1
    samples = [(101, 50), (201, 50), (101, 50), (101, 50)]
    s = gpus([gpu(util=80, compute=[(101, GiB), (201, GiB)], samples=samples)], OWNERS)
    assert {n: u['util_percent'] for n, u in _users(s.devices[0]).items()} == {'alice': 60.0, 'bob': 20.0}


@pytest.mark.parametrize('samples', [None, NVMLError(0), NVMLError(999), []])
def test_no_usable_samples_is_ambiguous(gpus, samples):
    s = gpus([gpu(util=80, compute=[(101, GiB), (201, GiB)], samples=samples)], OWNERS)
    dev = s.devices[0]
    assert dev['util_split'] == 'ambiguous'
    assert all(u['util_percent'] is None for u in dev['users'])
    assert s.per_user['alice']['gpu_util_percent'] is None and s.per_user['alice']['gpu_busy_seconds'] is None
    assert s.per_user['alice']['gpus_held'] == 1 and s.per_user['alice']['gpu_held_seconds'] == 300.0


def test_idle_gpu_gives_zero(gpus):
    s = gpus([gpu(util=0, compute=[(101, GiB), (201, GiB)])], OWNERS)
    assert s.devices[0]['util_split'] == 'idle'
    assert s.per_user['alice']['gpu_util_percent'] == 0.0 and s.per_user['bob']['gpu_busy_seconds'] == 0.0


def test_unknown_busy_percent_is_null(gpus):
    nvml = make_nvml([gpu(compute=[(101, GiB)])])
    nvml.nvmlDeviceGetUtilizationRates = lambda h: (_ for _ in ()).throw(NVMLError(999))
    sys.modules['pynvml'] = nvml
    try:
        s = gpus.module.collect({}, OWNERS, 300.0, LOG)
    finally:
        del sys.modules['pynvml']
    assert s.devices[0]['gpu_percent'] is None and s.devices[0]['util_split'] is None
    assert s.per_user['alice']['gpu_util_percent'] is None


def test_invariants_util_and_busy_seconds_never_exceed_the_gpu(gpus):
    s = gpus([gpu(util=90, compute=[(101, GiB), (201, GiB), (301, GiB)],
                  samples=[(101, 40), (201, 30), (301, 20), (999, 60)])], OWNERS, interval_s=120.0)
    dev = s.devices[0]
    assert sum(u['util_percent'] for u in dev['users']) <= 90 + 0.1
    assert sum(u['gpu_busy_seconds'] for u in s.per_user.values()) <= 0.9 * 120 + 0.1


# ---------------------------------------------------------------------------
# VRAM, WDDM, old drivers
# ---------------------------------------------------------------------------

def test_wddm_vram_is_null_but_counts_are_kept(gpus, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    s = gpus([gpu(util=40, compute=[(101, None)], model=0)], OWNERS)
    assert _users(s.devices[0])['alice'] == {'username': 'alice', 'vram_bytes': None, 'util_percent': 40.0,
                                             'processes': 1}
    assert s.per_user['alice']['gpu_vram_bytes'] is None and s.per_user['alice']['gpus_held'] == 1


@pytest.mark.parametrize('mem', [-5, 100 * GiB])
def test_implausible_vram_is_null(gpus, mem):
    s = gpus([gpu(compute=[(101, mem)])], OWNERS)
    assert _users(s.devices[0])['alice']['vram_bytes'] is None


def test_wddm_c_plus_g_counts_as_graphics(gpus, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    # bob's explorer.exe is in both lists (C+G); alice's CUDA job is compute only
    s = gpus([gpu(util=70, compute=[(101, None), (201, None)], graphics=[(201, None)], model=0)], OWNERS)
    dev = s.devices[0]
    assert dev['util_split'] == 'single_user'
    assert {n: u['util_percent'] for n, u in _users(dev).items()} == {'alice': 70.0, 'bob': 0.0}
    assert s.per_user['bob']['gpus_held'] == 0 and s.per_user['alice']['gpus_held'] == 1


def test_tcc_on_windows_keeps_compute_and_vram(gpus, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    s = gpus([gpu(util=70, compute=[(101, 4 * GiB), (201, GiB)], graphics=[(201, GiB)], model=1,
                  samples=[(101, 50), (201, 20)])], OWNERS)
    assert s.devices[0]['util_split'] == 'process_samples'        # under TCC, C+G is still compute
    assert s.per_user['bob']['gpus_held'] == 1 and s.per_user['alice']['gpu_vram_bytes'] == 4 * GiB


def test_function_not_found_means_no_per_process_data_for_that_gpu(gpus, caplog):
    with caplog.at_level(logging.WARNING):
        s = gpus([gpu(compute=[(101, GiB)]), gpu(uuid='GPU-1', compute_error=NVMLError(FUNCTION_NOT_FOUND)),
                  gpu(uuid='GPU-2', compute_error=NVMLError(FUNCTION_NOT_FOUND))], OWNERS, driver='450.80.02')
    assert s.devices[0]['process_source'] == 'nvml'
    for dev in s.devices[1:]:
        assert dev['process_source'] is None and dev['users'] is None and dev['util_split'] is None
        # Not measured: null, never 0 (contract R4)
        assert dev['users_truncated'] is None and dev['unattributed_processes'] is None
        assert dev['unattributed_vram_bytes'] is None
        assert dev['gpu_percent'] == 50 and dev['vram_used_bytes'] == 10 * GiB      # device stats unaffected
    warnings = [r.getMessage() for r in caplog.records if 'too old' in r.getMessage()]
    assert len(warnings) == 1 and '450.80.02' in warnings[0]
    assert s.per_user is None                                     # never partial


def test_mps_processes_hold_the_gpu(gpus):
    s = gpus([gpu(util=30, mps=[(201, GiB)])], OWNERS)
    assert s.devices[0]['util_split'] == 'single_user' and s.per_user['bob']['gpus_held'] == 1


def test_windows_driver_500_to_525_turns_per_process_off(gpus, monkeypatch, caplog):
    monkeypatch.setattr(sys, 'platform', 'win32')
    with caplog.at_level(logging.WARNING):
        s = gpus([gpu(compute=[(101, None)], model=0)], OWNERS, driver='512.95')
    assert s.devices[0]['process_source'] is None and s.per_user is None
    assert s.devices[0]['users_truncated'] is None and s.devices[0]['unattributed_processes'] is None
    assert '512.95' in caplog.text
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert gpus([gpu(compute=[(101, GiB)])], OWNERS, driver='515.65.01').per_user is not None   # Linux is fine


def test_gpu_per_user_off(gpus):
    s = gpus([gpu(compute=[(101, GiB)])], OWNERS, config={'gpu_per_user': 'off'})
    dev = s.devices[0]
    assert dev['uuid'] == 'GPU-0000' and dev['process_source'] is None and dev['util_split'] is None
    assert 'users' not in dev and 'unattributed_processes' not in dev and s.per_user is None


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------

def test_owner_map_then_direct_lookup_then_unattributed(gpus, monkeypatch, procfs):
    procfs.passwd = {1002: 'dave'}
    procfs.proc_entry(401, 1002)
    procfs.write()
    psutil = types.ModuleType('psutil')

    def process(pid):
        if pid == 501:
            return ns(username=lambda: 'erin')
        raise RuntimeError('no such process')
    psutil.Process = process
    monkeypatch.setitem(sys.modules, 'psutil', psutil)
    keep = lambda pid, name, uid: name != 'svc'
    s = gpus([gpu(util=60, compute=[(101, GiB), (501, GiB), (401, GiB), (666, 2 * GiB), (777, 3 * GiB)])],
             OWNERS, user_filter=keep)
    dev = s.devices[0]
    assert set(_users(dev)) == {'alice', 'erin', 'dave'}           # owner_of, psutil, /proc/<pid>/status
    assert dev['unattributed_processes'] == 2 and dev['unattributed_vram_bytes'] == 5 * GiB   # excluded + unknown
    assert 'svc' not in s.per_user


def test_filter_gets_the_uid_from_proc(gpus, procfs):
    procfs.passwd = {120: 'gdm'}
    procfs.proc_entry(401, 120)
    procfs.write()
    seen = []
    s = gpus([gpu(util=0, graphics=[(401, GiB)])], {}, user_filter=lambda pid, name, uid: seen.append(uid) or False)
    assert seen == [120] and s.devices[0]['unattributed_processes'] == 1


@pytest.mark.parametrize('owner_of,uid_of', [({500: 'ollama', 600: 'alice'}, {500: 998, 600: 1001}),
                                              (None, {})], ids=['walked', 'not-walked'])
def test_unwalked_pids_get_the_same_account_rules(collector, gpus, procfs, monkeypatch, owner_of, uid_of):
    # A Linux GPU server: 'ollama' is a daemon account (uid 998 < UID_MIN),
    # alice a user. Without the counters' walk (perf_counters false, a lock
    # timeout, a process started since) the owners come from /proc, uids included
    procfs.passwd = {998: 'ollama', 1001: 'alice'}
    procfs.proc_entry(500, 998)
    procfs.proc_entry(600, 1001)
    procfs.write()
    psutil = types.ModuleType('psutil')
    psutil.Process = lambda pid: ns(username=lambda: {500: 'ollama', 600: 'alice'}[pid])
    monkeypatch.setitem(sys.modules, 'psutil', psutil)
    config = {'platform': 'linux'}
    s = gpus([gpu(util=90, compute=[(500, 8 * GiB), (600, 2 * GiB)], samples=[(500, 70), (600, 20)])], owner_of,
             config=config, user_filter=collector._user_filter(config, uid_of))
    assert sorted(s.per_user) == ['alice'] and s.devices[0]['unattributed_processes'] == 1


def test_unresolvable_uid_is_named_as_the_walk_names_it(gpus, procfs, monkeypatch):
    procfs.proc_entry(700, 4242)             # a container's user: no account on the host
    procfs.write()
    psutil = types.ModuleType('psutil')
    psutil.Process = lambda pid: ns(username=lambda: '4242')
    monkeypatch.setitem(sys.modules, 'psutil', psutil)
    s = gpus([gpu(util=40, compute=[(700, GiB)])], None)
    assert list(s.per_user) == ['uid:4242']          # as proc_accounting names it in the CPU rows


def test_psutil_owner_lookup_passes_the_real_uid(gpus, monkeypatch, tmp_path):
    from lib import proc_accounting
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(proc_accounting, 'PROC', str(tmp_path / 'no-proc'))     # /proc unreadable
    psutil = types.ModuleType('psutil')
    psutil.Process = lambda pid: ns(username=lambda: 'ollama', uids=lambda: ns(real=998, effective=998, saved=998))
    monkeypatch.setitem(sys.modules, 'psutil', psutil)
    seen = []
    s = gpus([gpu(util=40, compute=[(500, GiB)])], None, user_filter=lambda pid, name, uid: seen.append(uid) or False)
    assert seen == [998] and s.devices[0]['unattributed_processes'] == 1


def test_twenty_users_per_gpu(gpus):
    owners = {1000 + i: f'user{i:02d}' for i in range(25)}
    s = gpus([gpu(util=0, compute=[(1000 + i, (i + 1) * GiB) for i in range(25)])], owners)
    dev = s.devices[0]
    assert len(dev['users']) == 20 and dev['users_truncated'] == 5
    assert dev['users'][0]['username'] == 'user24'                 # largest VRAM first
    assert len(s.per_user) == 25                                   # the per-user totals keep everyone


def test_vram_in_use_without_visible_processes_is_logged(gpus, caplog):
    with caplog.at_level(logging.INFO):
        gpus([gpu(used=40 * GiB, total=48 * GiB)], OWNERS)
    assert 'no processes are visible' in caplog.text


# ---------------------------------------------------------------------------
# sample_seconds, failures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('interval,config,expected', [
    (300.2, {}, 300.2), (900.0, {}, 900.0), (901.0, {}, 300.0), (None, {}, 300.0), (0.0, {}, 300.0),
    (None, {'metrics_interval_seconds': 600}, 600.0), (-5.0, {}, 300.0),
])
def test_sample_seconds(gpus, interval, config, expected):
    assert gpus.module.sample_seconds(config, interval) == expected
    s = gpus([gpu(util=50, compute=[(101, GiB)])], OWNERS, interval_s=interval, config=config)
    assert s.sample_seconds == expected and s.per_user['alice']['gpu_held_seconds'] == expected


def test_no_gpus_no_sample_seconds(gpus):
    s = gpus([])
    assert s == gpus.module.GpuSample([], None, None)


def test_per_user_failure_still_sends_device_stats(collector, config, monkeypatch, caplog):
    from lib import gpu as module
    monkeypatch.setitem(sys.modules, 'pynvml', make_nvml([gpu(compute=[(101, GiB)])]))
    monkeypatch.setattr(module, '_split', lambda *a: 1 / 0)
    monkeypatch.setattr(collector, 'measure_counters', lambda *a, **k: {})
    monkeypatch.setattr(collector, 'get_cpu_percent', lambda: 10.0)
    captured = []
    monkeypatch.setattr(collector, '_record', lambda c, n, entries, log: captured.extend(entries) or True)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    with caplog.at_level(logging.WARNING):
        assert collector.collect_metrics(config, LOG) is True
    data = captured[0]['data']
    assert data['gpu_count'] == 1 and data['gpu_0_percent'] == 50 and data['gpus'][0]['vram_used_bytes'] == 10 * GiB
    assert data['gpus'][0]['process_source'] is None and data['gpus'][0]['users'] is None
    assert 'Per-user GPU data unavailable' in caplog.text
    assert not [e for e in captured if e['data']['data_type'] == 'user_metrics' and 'gpus_held' in e['data']]
