"""Storage breakdown: the breakdown walker, the time guard, build_payload and the disk run's second message."""
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

LOG = logging.getLogger('test')
DAY = 86400
posix_only = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX owners and hard links')


@pytest.fixture
def breakdown(collector):
    from lib import storage_breakdown as module
    return module


def _write(path, size, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))


def _cuts(now=None):
    now = time.time() if now is None else now
    return now - 365 * DAY, now - 1095 * DAY


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / 'tree'
    _write(str(root / 'new.bin'), 100)
    _write(str(root / 'two_years.bin'), 200, 2 * 365)
    _write(str(root / 'four_years.bin'), 400, 4 * 365)
    _write(str(root / 'future.bin'), 50, -30)                       # a future mtime is never cold
    _write(str(root / 'sub' / 'deep.bin'), 10, 5 * 365)
    return root


# ---------------------------------------------------------------------------
# The walker
# ---------------------------------------------------------------------------

def test_cold_buckets_by_mtime(disk_usage, tree):
    cut1, cut3 = _cuts()
    total, files, acc = disk_usage.measure_folder_recursive_breakdown(str(tree), 60, None, cut1, cut3, owners=True)
    assert (total, files) == (760, 5)
    assert acc == {os.getuid(): [760, 5, 610, 410]}


def test_the_plain_walker_is_unchanged_and_agrees(disk_usage, tree):
    cut1, cut3 = _cuts()
    assert disk_usage.measure_folder_recursive(str(tree), 60) == (760, 5)
    assert disk_usage.measure_folder_recursive_breakdown(str(tree), 60, None, cut1, cut3, False)[:2] == (760, 5)


@posix_only
def test_hard_links_once_symlinks_and_synology_names_skipped(disk_usage, tree):
    cut1, cut3 = _cuts()
    os.link(str(tree / 'four_years.bin'), str(tree / 'sub' / 'link.bin'))
    os.symlink(str(tree / 'new.bin'), str(tree / 'symlink.bin'))
    _write(str(tree / '@eaDir' / 'thumb'), 9999)
    _write(str(tree / '#recycle' / 'old'), 9999)
    policy = disk_usage.ScanPolicy('synology')
    total, files, acc = disk_usage.measure_folder_recursive_breakdown(str(tree), 60, None, cut1, cut3, True, policy)
    assert (total, files) == (760, 5) == disk_usage.measure_folder_recursive(str(tree), 60, policy=policy)
    assert acc[os.getuid()] == [760, 5, 610, 410]


def test_owners_off_gives_one_record(disk_usage, tree):
    cut1, cut3 = _cuts()
    assert disk_usage.measure_folder_recursive_breakdown(str(tree), 60, None, cut1, cut3, False)[2] == {
        None: [760, 5, 610, 410]}


def test_per_owner_records(disk_usage, tree, monkeypatch):
    """Files of several owners (st_uid faked: tests run as one user)."""
    owners = {'new.bin': 1001, 'two_years.bin': 1001, 'four_years.bin': 1002, 'future.bin': 1001, 'deep.bin': 0}
    real_scandir = os.scandir

    class Entry:
        def __init__(self, entry):
            self._e = entry
            self.name, self.path = entry.name, entry.path

        def __getattr__(self, name):
            return getattr(self._e, name)

        def stat(self, follow_symlinks=True):
            st = self._e.stat(follow_symlinks=follow_symlinks)
            if self.name not in owners:
                return st
            fields = list(st)
            fields[4] = owners[self.name]                     # st_uid
            return type('St', (), dict(zip(('st_mode', 'st_ino', 'st_dev', 'st_nlink', 'st_uid', 'st_gid',
                                            'st_size', 'st_atime', 'st_mtime', 'st_ctime'), fields)))()

    class Scan:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __iter__(self):
            return (Entry(e) for e in self._it)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()
    monkeypatch.setattr(disk_usage.os, 'scandir', Scan)
    cut1, cut3 = _cuts()
    _, _, acc = disk_usage.measure_folder_recursive_breakdown(str(tree), 60, None, cut1, cut3, True)
    assert acc == {1001: [350, 3, 200, 0], 1002: [400, 1, 400, 400], 0: [10, 1, 10, 10]}


def test_a_timeout_gives_no_partial_result(disk_usage, tree, monkeypatch):
    monkeypatch.setattr(disk_usage, '_DEADLINE_CHECK_EVERY', 1)
    with pytest.raises(TimeoutError):
        disk_usage.measure_folder_recursive_breakdown(str(tree), 0, None, *_cuts(), owners=True)


# ---------------------------------------------------------------------------
# scan_folders and the time guard
# ---------------------------------------------------------------------------

def _options(disk_usage, previous, max_fraction=0.6):
    return disk_usage.BreakdownOptions(*_cuts(), owners=True, previous=previous, max_fraction=max_fraction)


def test_time_guard(disk_usage, tmp_path):
    folders = []
    for name in ('fast', 'slow', 'timed_out', 'new'):
        _write(str(tmp_path / name / 'f'), 10, 400)
        folders.append(str(tmp_path / name))
    fast, slow, timed_out, new = folders
    previous = {fast: 5.0, slow: 61.0}                     # timed_out and new: no successful measurement
    result = disk_usage.scan_folders(folders, timeout=100, breakdown=_options(disk_usage, previous))
    assert result['sizes'] == {f: 10 for f in folders}
    assert result['breakdown_unavailable'] == [slow, timed_out, new]
    assert result['breakdown'][fast]['acc'] == {os.getuid(): [10, 1, 10, 0]}
    assert all(result['breakdown'][f]['acc'] is None for f in (slow, timed_out, new))
    assert all(result['breakdown'][f]['folder_owner'] == os.getuid() for f in folders)
    assert set(result['seconds']) == set(folders) and result['files'] == {f: 1 for f in folders}
    # 0.6 x the timeout in force now: with a 200 s timeout slow (61 s) qualifies
    result = disk_usage.scan_folders(folders, timeout=200, breakdown=_options(disk_usage, previous))
    assert result['breakdown_unavailable'] == [timed_out, new]


def test_scan_folders_without_breakdown_is_unchanged(disk_usage, tmp_path):
    _write(str(tmp_path / 'a' / 'f'), 7)
    assert disk_usage.scan_folders([str(tmp_path / 'a')], timeout=10) == {
        'sizes': {str(tmp_path / 'a'): 7}, 'timed_out': [], 'errors': []}


def test_windows_breakdown_scanner(disk_usage, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='1000 4 600 200\r\n', stderr='')
    monkeypatch.setattr(disk_usage.subprocess, 'run', run)
    evil = "E:\\x'; Remove-Item C:\\ ; '"
    assert disk_usage.measure_folder_powershell_breakdown(evil, 30, 0.0, -1.0) == (1000, 4, {None: [1000, 4, 600, 200]})
    args, kwargs = calls[0]
    assert args[-1] == disk_usage.PS_BREAKDOWN_SCRIPT and all(evil not in a for a in args)
    env = kwargs['env']
    assert env['LM_SCAN_PATH'] == evil
    assert env['LM_COLD1_TICKS'] == str(621355968000000000) and env['LM_COLD3_TICKS'] == str(621355968000000000 - 10 ** 7)
    script = disk_usage.PS_BREAKDOWN_SCRIPT
    assert script.isascii() and all(script.count(a) == script.count(b) for a, b in ('()', '{}', '[]'))
    assert 'LastWriteTimeUtc' in script and script.count('Get-ChildItem') == 1      # one enumeration


def test_windows_scan_falls_back_to_python_without_owners(disk_usage, tree, monkeypatch):
    def broken(args, **kwargs):
        raise OSError('no powershell')
    monkeypatch.setattr(disk_usage.subprocess, 'run', broken)
    monkeypatch.setattr(disk_usage.sys, 'platform', 'win32')
    result = disk_usage.scan_folders([str(tree)], timeout=60, breakdown=_options(disk_usage, {str(tree): 1.0}))
    assert result['breakdown'][str(tree)] == {'acc': {None: [760, 5, 610, 410]}, 'folder_owner': None}


# ---------------------------------------------------------------------------
# Owner names
# ---------------------------------------------------------------------------

def test_unknown_uids_and_the_lookup_budget(breakdown, monkeypatch):
    names = {0: 'root', 1000: 'alice'}

    def lookup(uid):
        if uid == 4242:
            raise KeyError(uid)
        return names.get(uid)
    monkeypatch.setattr(breakdown, '_passwd_name', lambda uid: names.get(uid))
    assert breakdown.resolve_owner_names([0, 1000, 4242]) == {0: 'root', 1000: 'alice', 4242: 'uid:4242'}
    release = threading.Event()

    def slow(uid):
        if uid == 1000:
            release.wait(10)                       # a hanging LDAP lookup
        return names.get(uid)
    monkeypatch.setattr(breakdown, '_passwd_name', slow)
    started = time.monotonic()
    try:
        assert breakdown.resolve_owner_names([0, 1000, 2000], budget_s=0.2) == {
            0: 'root', 1000: 'uid:1000', 2000: 'uid:2000'}
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_real_getpwuid_keyerror(breakdown):
    assert breakdown.resolve_owner_names([2 ** 31 - 7])[2 ** 31 - 7] == f'uid:{2 ** 31 - 7}'


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

def _opts(breakdown, **block):
    return breakdown.Options({'storage_breakdown': block}, windows=False, log=LOG, now=1_795_000_000.0)


def _scan(folders):
    """A scan_folders() result: folders {path: acc or None}."""
    sizes = {p: (999 if acc is None else sum(r[0] for r in acc.values())) for p, acc in folders.items()}
    return {'sizes': sizes, 'timed_out': ['/v/slow'], 'errors': [],
            'files': {p: (9 if acc is None else sum(r[1] for r in acc.values())) for p, acc in folders.items()},
            'breakdown': {p: {'acc': acc, 'folder_owner': 1000} for p, acc in folders.items()},
            'breakdown_unavailable': [p for p, acc in folders.items() if acc is None]}


NAMES = {0: 'root', 1000: 'alice', 1001: 'bob', 1002: 'carol', 1003: 'dave'}


def test_top_owners_and_other(breakdown):
    acc = {1000: [500, 5, 100, 10], 1001: [300, 3, 0, 0], 1002: [100, 1, 100, 100], 1003: [50, 1, 50, 0]}
    payload = breakdown.build_payload(_scan({'/v/lab': acc}), ['/v/lab'], _opts(breakdown, top_owners=2), NAMES)
    folder = payload['folders'][0]
    assert folder == {
        'path': '/v/lab', 'folder_owner': 'alice', 'bytes': 950, 'files': 10, 'cold_1y_bytes': 250, 'cold_3y_bytes': 110,
        'owners': [{'owner': 'alice', 'bytes': 500, 'files': 5, 'cold_1y_bytes': 100, 'cold_3y_bytes': 10},
                   {'owner': 'bob', 'bytes': 300, 'files': 3, 'cold_1y_bytes': 0, 'cold_3y_bytes': 0}],
        'other': {'owner_count': 2, 'bytes': 150, 'files': 2, 'cold_1y_bytes': 150, 'cold_3y_bytes': 100}}
    assert payload['top_owners'] == payload['top_owners_effective'] == 2 and payload['owners_truncated'] is False
    assert payload['reference_time'] == '2026-11-18T11:06:40Z' and payload['cold_thresholds_days'] == [365, 1095]
    assert payload['owner_source'] == 'posix_uid' and payload['age_source'] == 'mtime'
    assert payload['volume_owners'] is None and payload['_timed_out'] == ['/v/slow']
    # The device gets the full owner list (device_top_owners), not the folder's cut one
    assert [o['owner'] for o in payload['device']['owners']] == ['alice', 'bob', 'carol', 'dave']
    for entry in payload['folders'] + [payload['device']]:
        assert entry['cold_3y_bytes'] <= entry['cold_1y_bytes'] <= entry['bytes']


def test_null_versus_empty_owners(breakdown):
    scan = _scan({'/v/empty': {}, '/v/slow_folder': None})
    payload = breakdown.build_payload(scan, ['/v/empty', '/v/slow_folder'], _opts(breakdown), NAMES)
    empty, plain = payload['folders']
    assert empty['owners'] == [] and empty['other']['owner_count'] == 0 and empty['cold_1y_bytes'] == 0
    assert plain['owners'] is None and plain['other'] is None and plain['cold_1y_bytes'] is None
    assert plain['bytes'] == 999 and plain['files'] == 9 and plain['folder_owner'] == 'alice'
    assert payload['_breakdown_unavailable'] == ['/v/slow_folder']
    # One top-level folder without cold data: the device's cold totals are null, not partial
    assert payload['device']['bytes'] == 999 and payload['device']['cold_1y_bytes'] is None
    assert payload['device']['owners'] is None
    no_owners = breakdown.build_payload(_scan({'/v/a': {None: [10, 1, 5, 0]}}), ['/v/a'], _opts(breakdown, owners='none'))
    assert no_owners['owner_source'] == 'none' and no_owners['folders'][0]['owners'] is None
    assert no_owners['folders'][0]['folder_owner'] is None and no_owners['folders'][0]['cold_1y_bytes'] == 5


def test_device_rollup_skips_nested_folders(breakdown):
    scan = _scan({'/v/lab': {1000: [100, 1, 0, 0]}, '/v/lab/alice': {1000: [60, 1, 0, 0]},
                  '/v/other': {1001: [40, 1, 40, 0]}, '/v/labx': {1002: [5, 1, 0, 0]}})
    payload = breakdown.build_payload(scan, list(scan['sizes']), _opts(breakdown), NAMES)
    assert payload['device']['bytes'] == 145 and payload['device']['cold_1y_bytes'] == 40
    assert {o['owner']: o['bytes'] for o in payload['device']['owners']} == {'alice': 100, 'bob': 40, 'carol': 5}


def _pairwise_top_level(paths):
    """The rule _top_level implements, checked pair by pair."""
    keys = {p: os.path.normcase(os.path.normpath(p)) for p in paths}

    def inside(child, parent):
        prefix = parent if parent.endswith(('/', '\\')) else parent + os.sep
        return child != parent and child.startswith(prefix)
    return [p for p in paths if not any(inside(keys[p], keys[q]) for q in paths if q != p)]


def test_top_level_folders(breakdown):
    paths = ['/volume1/homes', '/volume1/homes/alice', '/volume1/homes/alice/x', '/volume1/home', '/volume1/homesx',
             '/volume2', '/volume2/', '/volume1/homes/bob', '/data']
    assert breakdown._top_level(paths) == _pairwise_top_level(paths) == [
        '/volume1/homes', '/volume1/home', '/volume1/homesx', '/volume2', '/volume2/', '/data']
    assert breakdown._top_level(['/', '/volume1', '/volume1/a']) == ['/']
    assert breakdown._top_level([]) == []


def test_top_level_is_not_quadratic_in_the_folder_count(breakdown):
    # '/volume1/homes/*' on a large site: thousands of folders (pairwise this took ~9 s for 6000)
    paths = [f'/volume1/homes/user{i:05d}' for i in range(20000)] + ['/volume1/homes/user00007/sub']
    started = time.perf_counter()
    top = breakdown._top_level(paths)
    assert time.perf_counter() - started < 2.0
    assert len(top) == 20000 and '/volume1/homes/user00007/sub' not in top


def test_row_budget_lowers_n_then_drops_owners(breakdown):
    acc = {uid: [uid, 1, 0, 0] for uid in range(1000, 1050)}                 # 50 owners in each folder

    def payload(n_folders, max_rows):
        folders = {f'/v/f{i}': dict(acc) for i in range(n_folders)}
        return breakdown.build_payload(_scan(folders), list(folders), _opts(breakdown, max_rows=max_rows), NAMES)

    def rows(p):
        return sum(1 + len(f['owners'] or []) + (1 if f['other'] else 0) for f in p['folders'])
    roomy = payload(20, 1000)                                    # 20 x (1 + 10 + 1) = 240 rows
    assert roomy['owners_truncated'] is False and roomy['top_owners_effective'] == 10 and rows(roomy) == 240
    lowered = payload(20, 200)                                   # N = 200 // 20 - 2 = 8
    assert lowered['owners_truncated'] is True and lowered['top_owners_effective'] == 8 and rows(lowered) == 200
    assert all(len(f['owners']) == 8 and f['other']['owner_count'] == 42 for f in lowered['folders'])
    dropped = payload(60, 100)                                   # N = 1 still gives 180 rows
    assert dropped['owners_truncated'] is True and dropped['top_owners_effective'] == 0
    assert all(f['owners'] is None and f['other'] is None for f in dropped['folders']) and rows(dropped) == 60
    # Folder totals and the full device entry remain
    assert dropped['device']['bytes'] == sum(f['bytes'] for f in dropped['folders'])
    assert len(dropped['device']['owners']) == 25 and dropped['device']['other']['owner_count'] == 25


def test_more_folders_than_the_manager_accepts_are_cut_to_the_largest(breakdown, monkeypatch, caplog):
    """Over MAX_FOLDERS the smallest folders are left out and counted; the device still covers all of them."""
    monkeypatch.setattr(breakdown, 'MAX_FOLDERS', 3)
    folders = {f'/v/f{i}': {1000: [10 * (i + 1), 1, 0, 0]} for i in range(5)}
    folders['/v/f1/sub'] = {1001: [1, 1, 0, 0]}
    scan = _scan(folders)
    scan['breakdown']['/v/f3'] = {'acc': None, 'folder_owner': 1000}
    scan['breakdown_unavailable'] = ['/v/f0', '/v/f3']
    with caplog.at_level(logging.WARNING):
        payload = breakdown.build_payload(scan, list(folders), _opts(breakdown), NAMES)
    assert [f['path'] for f in payload['folders']] == ['/v/f2', '/v/f3', '/v/f4']      # order kept
    assert payload['_folders_omitted'] == 3 and payload['_breakdown_unavailable'] == ['/v/f3']
    assert all(f['owners'] is None for f in payload['folders']) and payload['owners_truncated'] is True
    assert payload['top_owners_effective'] == 0
    assert payload['device']['bytes'] == 150 and payload['device']['files'] == 5       # every top-level folder
    assert '3 of 6 folders left out' in caplog.text
    small = breakdown.build_payload(_scan({'/v/a': {1000: [5, 1, 0, 0]}}), ['/v/a'], _opts(breakdown), NAMES)
    assert '_folders_omitted' not in small and small['folders'][0]['owners'] is not None


def test_a_huge_disk_run_stays_within_the_manager_caps(breakdown):
    """10,001 measured folders (a '/volume1/homes/*' glob): at most 10,000 folders and 1 MiB are sent."""
    paths = [f'/volume1/homes/user{i:05d}' for i in range(10001)]
    folders = {p: {1000 + i % 50: [1000 + i, 10, 100, 10]} for i, p in enumerate(paths)}
    payload = breakdown.build_payload(_scan(folders), paths, _opts(breakdown), {1000 + k: f'u{k}' for k in range(50)})
    message = {'header': {'msg_id': 'f' * 32, 'device_name': 'nas', 'device_id': 'nas', 'device_type': 'synology',
                          'timestamp': '2026-11-21T02:00:00Z'},
               'data': dict(payload, data_type='storage_breakdown')}
    assert len(json.dumps(message)) <= breakdown.MAX_MESSAGE_BYTES
    sent = [f['path'] for f in payload['folders']]
    assert 0 < len(sent) <= 10000 and payload['_folders_omitted'] == 10001 - len(sent)
    assert sent == paths[-len(sent):]                     # the largest ones, in their order
    assert payload['device']['bytes'] == sum(1000 + i for i in range(10001))


def test_the_folder_cap_matches_the_manager(breakdown):
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', '..', 'manager', 'manager.py'), encoding='utf-8').read()
    m = re.search(r'^_MAX_BREAKDOWN_FOLDERS\s*=\s*(\d+)\s*$', src, re.M)
    assert m and int(m.group(1)) == breakdown.MAX_FOLDERS


def test_nothing_measured_gives_no_message(breakdown):
    assert breakdown.build_payload({'sizes': {}, 'timed_out': ['/v/a']}, [], _opts(breakdown), NAMES) is None


def test_options_and_state(breakdown, tmp_path):
    opts = _opts(breakdown, cold_days=[30, 90], max_timeout_fraction=0.5)
    assert opts.cut1 == 1_795_000_000.0 - 30 * DAY and opts.cut3 == 1_795_000_000.0 - 90 * DAY
    assert breakdown.Options({}, windows=True).owners is False
    bad = breakdown.Options({'storage_breakdown': {'cold_days': [900, 30], 'top_owners': 0}}, windows=False)
    assert bad.cold_days == [365, 1095] and bad.top_owners == 10
    path = tmp_path / 'state.json'
    assert breakdown.load_state(str(path)) == {}
    path.write_text('{"/v/a": 1.5, "/v/b": "x", "/v/c": true}')
    assert breakdown.load_state(str(path)) == {'/v/a': 1.5}
    path.write_text('[1, 2]')
    assert breakdown.load_state(str(path)) == {}


# ---------------------------------------------------------------------------
# The disk run
# ---------------------------------------------------------------------------

@pytest.fixture
def disk_run(collector, config, tree, monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(collector, '_record_and_sync', lambda cfg, n, i, entries, log: captured.extend(entries) or True)
    config.update({'platform': 'linux', 'volumes': [], 'scan_paths': [str(tree), str(tmp_path / 'missing')]})

    def run(**changes):
        captured.clear()
        assert collector.collect_disk_usage({**config, **changes}, LOG) is True
        return [dict(e['data']) for e in captured]
    run.state = os.path.join(config['data_dir'], 'testbox', 'storage_breakdown_state.json')
    return run


def test_disk_run_sends_breakdown_from_the_second_run(disk_run, tree):
    first = disk_run()
    assert [m['data_type'] for m in first] == ['folder_usage', 'storage_breakdown']
    assert first[1]['_breakdown_unavailable'] == [str(tree)] and first[1]['folders'][0]['cold_1y_bytes'] is None
    assert first[1]['_errors'] == first[0]['_errors']
    with open(disk_run.state) as f:
        assert list(json.load(f)) == [str(tree)]
    second = disk_run()
    folder = second[1]['folders'][0]
    assert folder['cold_1y_bytes'] == 610 and folder['cold_3y_bytes'] == 410 and folder['files'] == 5
    assert second[1]['_breakdown_unavailable'] == []
    # folder_usage is the same with and without the breakdown walker (the
    # auto-discovered volumes' free space aside: they are this host's disks)
    def folders(data):
        return {k: v for k, v in data.items() if k == str(tree) or k in ('data_type', '_unit', '_errors', '_timed_out')}
    assert set(first[0]) == set(second[0]) and folders(first[0]) == folders(second[0])
    off = disk_run(storage_breakdown={'enabled': False})
    assert [m['data_type'] for m in off] == ['folder_usage']
    assert set(off[0]) == set(second[0]) and folders(off[0]) == folders(second[0]) and off[0][str(tree)] == 760


def test_dry_run_writes_no_state(collector, config, tree, capsys):
    config.update({'platform': 'linux', 'volumes': [], 'scan_paths': [str(tree)]})
    assert collector.collect_disk_usage(config, LOG, dry_run=True) is True
    printed = json.loads(capsys.readouterr().out)
    assert [m['data']['data_type'] for m in printed] == ['folder_usage', 'storage_breakdown']
    assert not os.path.exists(config['data_dir'])


def test_a_breakdown_walk_that_times_out_is_redone_without(disk_usage, tree, monkeypatch, caplog):
    def too_slow(*args, **kwargs):
        raise TimeoutError('breakdown too slow')
    monkeypatch.setattr(disk_usage, '_measure_one_folder_breakdown', too_slow)
    with caplog.at_level(logging.WARNING):
        result = disk_usage.scan_folders([str(tree)], timeout=60, breakdown=_options(disk_usage, {str(tree): 1.0}))
    assert result['sizes'] == {str(tree): 760} and result['timed_out'] == []          # folder_usage keeps it
    assert result['breakdown_unavailable'] == [str(tree)] and result['breakdown'][str(tree)]['acc'] is None
    assert 'measuring it again without' in caplog.text
    # The attempt used up the overall budget too: timed out, as it would be without the breakdown
    deadline = time.monotonic() + 0.2

    def slow_to_the_deadline(*args, **kwargs):
        while time.monotonic() < deadline:
            time.sleep(0.01)
        raise TimeoutError('breakdown too slow')
    monkeypatch.setattr(disk_usage, '_measure_one_folder_breakdown', slow_to_the_deadline)
    result = disk_usage.scan_folders([str(tree)], timeout=60, deadline=deadline,
                                     breakdown=_options(disk_usage, {str(tree): 0.01}))
    assert result['sizes'] == {} and result['timed_out'] == [str(tree)]
