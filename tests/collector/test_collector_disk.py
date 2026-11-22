"""Disk scanning: timeouts/errors reported (not 0), hardlinks, deep trees, PowerShell, junctions."""
import logging
import os
import subprocess
import sys
import types

import pytest


class FakeClock:
    """Replacement for the `time` module inside disk_usage (monotonic only advances when told)."""

    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)


def _capture_disk_message(collector, monkeypatch):
    captured = []
    monkeypatch.setattr(collector, '_record_and_sync',
                        lambda cfg, n, i, entries, log, sync=True: captured.extend(entries) or True)
    return captured


# ---------------------------------------------------------------------------
# measure_folder_recursive
# ---------------------------------------------------------------------------

def test_hardlinks_counted_once(tmp_path, disk_usage):
    _write(str(tmp_path / 'a'), 1000)
    os.link(tmp_path / 'a', tmp_path / 'b')
    _write(str(tmp_path / 'sub' / 'c'), 500)
    os.link(tmp_path / 'a', tmp_path / 'sub' / 'd')
    assert disk_usage.measure_folder_recursive(str(tmp_path)) == (1500, 2)


class _FakeStat:
    def __init__(self, size, nlink, ino=0, dev=0, attrs=0):
        self.st_size, self.st_nlink, self.st_ino, self.st_dev = size, nlink, ino, dev
        self.st_file_attributes = attrs


class _FakeEntry:
    def __init__(self, name, parent, is_dir=False, st=None, junction=False):
        self.name = name
        self.path = parent + '/' + name
        self._is_dir = is_dir
        self._st = st or _FakeStat(0, 1)
        self._junction = junction

    def is_symlink(self):
        return False

    def is_file(self, follow_symlinks=True):
        return not self._is_dir

    def is_dir(self, follow_symlinks=True):
        return self._is_dir

    def is_junction(self):
        return self._junction

    def stat(self, follow_symlinks=True):
        return self._st


class _FakeScandir:
    def __init__(self, tree):
        self.tree = tree

    def __call__(self, path):
        entries = self.tree[path]

        class _It(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _It(entries)


def test_single_link_files_are_not_deduplicated(tmp_path, disk_usage, monkeypatch):
    # Windows' os.scandir reports st_ino = st_nlink = 0 for every file. The old
    # scanner keyed hard-link dedup on (dev, ino) for all files, so on Windows it
    # counted only the first file of each tree. Only nlink > 1 files are tracked now.
    root = str(tmp_path)
    tree = {root: [_FakeEntry(f'f{i}', root, st=_FakeStat(100, nlink=0)) for i in range(5)]}
    monkeypatch.setattr(disk_usage.os, 'scandir', _FakeScandir(tree))
    assert disk_usage.measure_folder_recursive(root) == (500, 5)


def test_windows_junctions_are_not_followed(tmp_path, disk_usage, monkeypatch):
    root = str(tmp_path)
    reparse = disk_usage.FILE_ATTRIBUTE_REPARSE_POINT
    tree = {
        root: [
            _FakeEntry('real', root, is_dir=True),
            _FakeEntry('junction', root, is_dir=True, junction=True),
            _FakeEntry('mountpoint', root, is_dir=True, st=_FakeStat(0, 1, attrs=reparse)),
            _FakeEntry('dedup-file', root, st=_FakeStat(70, 1, attrs=reparse)),  # still counted
        ],
        root + '/real': [_FakeEntry('f', root + '/real', st=_FakeStat(30, 1))],
        root + '/junction': [_FakeEntry('f', root + '/junction', st=_FakeStat(10**9, 1))],
        root + '/mountpoint': [_FakeEntry('f', root + '/mountpoint', st=_FakeStat(10**9, 1))],
    }
    monkeypatch.setattr(disk_usage.os, 'scandir', _FakeScandir(tree))
    monkeypatch.setattr(disk_usage, 'sys', types.SimpleNamespace(platform='win32'))
    assert disk_usage.measure_folder_recursive(root) == (100, 2)


def test_deep_tree_does_not_hit_recursion_limit(tmp_path, disk_usage):
    depth = sys.getrecursionlimit() + 100
    base = str(tmp_path)
    if sys.platform == 'win32':
        # A path of this depth is far past MAX_PATH (260): use an extended-length path
        base = '\\\\?\\' + os.path.abspath(base)
    created = [os.path.join(base, 'deep')]
    os.mkdir(created[0])
    try:
        for _ in range(depth):
            created.append(os.path.join(created[-1], 'd'))
            os.mkdir(created[-1])
        leaf = os.path.join(created[-1], 'leaf')
        _write(leaf, 42)
        assert disk_usage.measure_folder_recursive(created[0]) == (42, 1)
    finally:
        # shutil.rmtree (used by pytest's temp cleanup) is itself recursive
        if os.path.exists(os.path.join(created[-1], 'leaf')):
            os.remove(os.path.join(created[-1], 'leaf'))
        for path in reversed(created):
            os.rmdir(path)


def test_missing_or_unreadable_root_raises_instead_of_returning_zero(tmp_path, disk_usage):
    with pytest.raises(FileNotFoundError):
        disk_usage.measure_folder_recursive(str(tmp_path / 'nope'))
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('root can read anything')
    if sys.platform == 'win32':
        pytest.skip('needs POSIX permissions (chmod 0 only sets read-only on Windows)')
    locked = tmp_path / 'locked'
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        with pytest.raises(PermissionError):
            disk_usage.measure_folder_recursive(str(locked))
    finally:
        os.chmod(locked, 0o700)


def test_timeout_in_a_subfolder_propagates(tmp_path, disk_usage, monkeypatch):
    # The old recursive scanner swallowed TimeoutError raised in nested folders
    # (TimeoutError is an OSError) and returned a silently truncated total.
    for i in range(1500):
        (tmp_path / 'sub').mkdir(exist_ok=True)
        (tmp_path / 'sub' / f'f{i}').touch()

    class Clock(FakeClock):
        calls = 0

        def monotonic(self):
            self.calls += 1
            return 0.0 if self.calls <= 3 else 100.0
    monkeypatch.setattr(disk_usage, 'time', Clock())
    with pytest.raises(TimeoutError):
        disk_usage.measure_folder_recursive(str(tmp_path), timeout=5)


# ---------------------------------------------------------------------------
# scan_folders budgets
# ---------------------------------------------------------------------------

def test_overall_budget_reports_unreached_folders_as_timed_out(disk_usage, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(disk_usage, 'time', clock)

    def fake_measure(path, timeout, use_powershell):
        if timeout < 100:                      # would need 100 s
            clock.now += timeout
            raise TimeoutError(f'{path} exceeded {timeout}s')
        clock.now += 100
        return 1000, 10
    monkeypatch.setattr(disk_usage, '_measure_one_folder', fake_measure)

    result = disk_usage.scan_folders(['a', 'b', 'c', 'd'], timeout=1000, deadline=250)
    assert result == {'sizes': {'a': 1000, 'b': 1000}, 'timed_out': ['c', 'd'], 'errors': []}


def test_failed_folder_is_an_error_not_zero(disk_usage, monkeypatch):
    def fake_measure(path, timeout, use_powershell):
        if path == 'bad':
            raise PermissionError('denied')
        return 5, 1
    monkeypatch.setattr(disk_usage, '_measure_one_folder', fake_measure)
    result = disk_usage.scan_folders(['good', 'bad'], timeout=10)
    assert result == {'sizes': {'good': 5}, 'timed_out': [], 'errors': ['bad']}


def test_disk_message_reports_timeouts_errors_and_keeps_capacity(
        tmp_path, collector, config, disk_usage, monkeypatch):
    vol = tmp_path / 'vol'
    _write(str(vol / 'A' / 'f'), 100)
    _write(str(vol / 'B' / 'f'), 200)
    _write(str(tmp_path / 'single' / 'f'), 300)
    _write(str(tmp_path / 'slow' / 'f'), 400)
    config.update({
        'volumes': [str(vol)],
        'scan_paths': [str(tmp_path / 'slow'), str(vol) + '/*', str(tmp_path / 'single'),
                       str(tmp_path / 'missing')],
    })
    real = disk_usage._measure_one_folder

    def fake_measure(path, timeout, use_powershell):
        if path.endswith('slow'):
            raise TimeoutError('too slow')
        return real(path, timeout, use_powershell)
    monkeypatch.setattr(disk_usage, '_measure_one_folder', fake_measure)
    captured = _capture_disk_message(collector, monkeypatch)

    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    assert data['_timed_out'] == [str(tmp_path / 'slow')]
    assert data['_errors'] == [str(tmp_path / 'missing')]
    assert str(tmp_path / 'slow') not in data                  # not reported as 0
    assert data[str(vol / 'A')] == 100 and data[str(vol / 'B')] == 200  # later entries still measured
    assert data[str(tmp_path / 'single')] == 300
    assert data[f'{vol}_total_bytes'] > 0 and f'{vol}_free_bytes' in data
    assert data['total_usage'] == data[str(vol)]
    assert isinstance(data['_scan_seconds'], float)


def test_total_timeout_defaults_to_three_times_per_folder(collector, config, disk_usage, monkeypatch):
    seen = {}

    def fake_scan(folders, timeout, deadline=None, breakdown=None):
        seen['timeout'] = timeout
        seen['budget'] = deadline - collector.time.monotonic()
        return {'sizes': {}, 'timed_out': [], 'errors': []}
    monkeypatch.setattr(collector, 'scan_folders', fake_scan)
    _capture_disk_message(collector, monkeypatch)
    config.update({'volumes': [], 'scan_paths': [config['data_dir']], 'timeout_seconds': 100})
    os.makedirs(config['data_dir'], exist_ok=True)
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert seen['timeout'] == 100
    assert 295 < seen['budget'] <= 300

    config['total_timeout_seconds'] = 50
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert seen['budget'] <= 50


def test_scan_path_equal_to_volume_root_keeps_volume_value(tmp_path, collector, config, monkeypatch, caplog):
    vol = tmp_path / 'vol'
    _write(str(vol / 'A' / 'f'), 100)
    config.update({'volumes': [str(vol) + '/'], 'scan_paths': [str(vol), str(vol) + '/*']})
    captured = _capture_disk_message(collector, monkeypatch)
    with caplog.at_level(logging.WARNING):
        collector.collect_disk_usage(config, logging.getLogger('test'))
    data = captured[0]['data']
    vol_key = str(vol) + '/'
    assert data[vol_key] == data[f'{vol_key}_total_bytes'] - data[f'{vol_key}_free_bytes']
    assert str(vol) not in data            # the full-volume scan was skipped, not reported twice
    assert data[str(vol / 'A')] == 100
    assert 'volume root' in caplog.text


def test_no_measurable_volume_gives_null_total(tmp_path, collector, config, monkeypatch):
    config.update({'volumes': [str(tmp_path / 'gone')], 'scan_paths': []})
    captured = _capture_disk_message(collector, monkeypatch)
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert captured[0]['data']['total_usage'] is None


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------

def test_powershell_gets_path_via_environment_not_script_text(disk_usage, monkeypatch):
    evil = "E:\\Users\\x'; Remove-Item -Recurse C:\\ ; '"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='12345 67\n', stderr='')
    monkeypatch.setattr(disk_usage.subprocess, 'run', fake_run)

    assert disk_usage.measure_folder_powershell(evil, timeout=30) == (12345, 67)
    args, kwargs = calls[0]
    assert all(evil not in a and 'Remove-Item' not in a for a in args)
    assert '$env:LM_SCAN_PATH' in args[-1]
    assert kwargs['env']['LM_SCAN_PATH'] == evil
    assert kwargs['timeout'] == 30


def test_powershell_timeout_does_not_rescan_with_python(disk_usage, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    monkeypatch.setattr(disk_usage.subprocess, 'run', fake_run)
    monkeypatch.setattr(disk_usage, 'measure_folder_recursive',
                        lambda *a, **k: pytest.fail('must not fall back after a timeout'))
    with pytest.raises(TimeoutError):
        disk_usage._measure_one_folder('E:\\big', 10, use_powershell=True)


def test_powershell_failure_falls_back_to_python(tmp_path, disk_usage, monkeypatch):
    _write(str(tmp_path / 'f'), 10)
    monkeypatch.setattr(disk_usage.subprocess, 'run',
                        lambda args, **k: (_ for _ in ()).throw(OSError('no powershell')))
    assert disk_usage._measure_one_folder(str(tmp_path), 10, use_powershell=True) == (10, 1)


# ---------------------------------------------------------------------------
# Unreadable folders/roots are errors, never 0 bytes or silently missing
# ---------------------------------------------------------------------------

needs_non_root = pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0,
                                    reason='needs POSIX permissions enforced (non-root)')


@pytest.fixture
def denied_dir(tmp_path):
    """A directory nobody but root can list."""
    made = []

    def make(name):
        path = tmp_path / name
        _write(str(path / 'sub' / 'f'), 10)
        os.chmod(path, 0)
        made.append(path)
        return path
    yield make
    for path in made:
        os.chmod(path, 0o700)


@needs_non_root
def test_unreadable_glob_root_is_reported_in_errors(tmp_path, collector, config, denied_dir, monkeypatch):
    denied_root = denied_dir('denied_root')
    denied_single = denied_dir('denied_single')
    _write(str(tmp_path / 'ok' / 'A' / 'f'), 100)
    config.update({'volumes': [], 'scan_paths': [str(denied_root) + '/*', str(denied_single),
                                                 str(tmp_path / 'ok') + '/*']})
    captured = _capture_disk_message(collector, monkeypatch)
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    assert sorted(data['_errors']) == sorted([str(denied_root), str(denied_single)])
    assert data[str(tmp_path / 'ok' / 'A')] == 100


@needs_non_root
def test_unreadable_volume_in_legacy_mode_is_reported_in_errors(
        tmp_path, collector, config, disk_usage, denied_dir, monkeypatch):
    vol = denied_dir('vol')
    # tmp paths aren't /volumeN or /mnt paths; let them pass the volume check
    monkeypatch.setattr(disk_usage, 'is_valid_volume_path', lambda path, system=None: True)
    # The one-level-deep fallback is Synology's (Linux reports capacity only)
    config.update({'platform': 'synology', 'volumes': [str(vol)], 'scan_paths': []})
    captured = _capture_disk_message(collector, monkeypatch)
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert captured[0]['data']['_errors'] == [str(vol)]


def _powershell_prints(disk_usage, monkeypatch, stdout):
    monkeypatch.setattr(disk_usage.subprocess, 'run',
                        lambda args, **k: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=''))


@needs_non_root
def test_powershell_zero_for_unreadable_folder_is_an_error(disk_usage, denied_dir, monkeypatch):
    # Get-ChildItem -ErrorAction SilentlyContinue prints "0 0" for a folder it
    # can't list; that used to be reported as a 0-byte folder.
    locked = str(denied_dir('locked'))
    _powershell_prints(disk_usage, monkeypatch, '0 0\n')
    monkeypatch.setattr(disk_usage, 'measure_folder_recursive',
                        lambda *a, **k: pytest.fail('must not re-scan an unreadable folder'))
    with pytest.raises(PermissionError):
        disk_usage._measure_one_folder(locked, 10, use_powershell=True)
    monkeypatch.setattr(disk_usage, 'sys', types.SimpleNamespace(platform='win32'))
    assert disk_usage.scan_folders([locked], timeout=10)['errors'] == [locked]


def test_powershell_zero_for_empty_or_missing_folder(tmp_path, disk_usage, monkeypatch):
    _powershell_prints(disk_usage, monkeypatch, '0 0\n')
    (tmp_path / 'empty').mkdir()
    assert disk_usage.measure_folder_powershell(str(tmp_path / 'empty')) == (0, 0)
    with pytest.raises(FileNotFoundError):
        disk_usage.measure_folder_powershell(str(tmp_path / 'missing'))


def test_total_timeout_default_keeps_per_entry_time_off_windows(collector):
    # Older collectors gave each scan_paths entry its own timeout_seconds
    assert collector._default_total_timeout(100, 5, windows=False) == 500
    assert collector._default_total_timeout(100, 1, windows=False) == 300
    assert collector._default_total_timeout(100, 0, windows=False) == 300
    # Windows: fixed 3x, which the Disk Collection task limit (+1 h) is sized for
    assert collector._default_total_timeout(100, 5, windows=True) == 300


def test_total_timeout_default_counts_scan_paths(tmp_path, collector, config, monkeypatch):
    seen = {}

    def fake_scan(folders, timeout, deadline=None, breakdown=None):
        seen['budget'] = deadline - collector.time.monotonic()
        return {'sizes': {}, 'timed_out': [], 'errors': []}
    monkeypatch.setattr(collector, 'scan_folders', fake_scan)
    _capture_disk_message(collector, monkeypatch)
    paths = []
    for i in range(5):
        (tmp_path / f'p{i}').mkdir()
        paths.append(str(tmp_path / f'p{i}'))
    config.update({'volumes': [], 'scan_paths': paths, 'timeout_seconds': 100})
    monkeypatch.setattr(collector.sys, 'platform', 'linux')
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert 495 < seen['budget'] <= 500
