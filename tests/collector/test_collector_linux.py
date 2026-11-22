"""Linux (Ubuntu/Debian) collector: platform detection, mount table, scans, installer, systemd units."""
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import types

import pytest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COLLECTOR_DIR = os.path.join(REPO_DIR, 'collector')
INSTALLER = os.path.join(REPO_DIR, 'install-collector-linux.sh')
UNITS_DIR = os.path.join(COLLECTOR_DIR, 'systemd')

posix_only = pytest.mark.skipif(sys.platform == 'win32', reason='Linux collector')
linux_only = pytest.mark.skipif(not sys.platform.startswith('linux') or not os.path.isdir('/proc/self'),
                                reason='needs a Linux /proc')


def _bash_works():
    """A bash that runs (on Windows, `bash` may be the WSL launcher with no distribution)."""
    if shutil.which('bash') is None:
        return False
    try:
        return subprocess.run(['bash', '-c', 'true'], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_bash = pytest.mark.skipif(not _bash_works(), reason='needs a working bash')
_IS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0


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
# Platform detection
# ---------------------------------------------------------------------------

@pytest.fixture
def host(disk_usage, monkeypatch, tmp_path):
    """Pretend to be a POSIX host whose Synology marker files live under tmp_path."""
    markers = (str(tmp_path / 'etc' / 'synoinfo.conf'), str(tmp_path / 'usr' / 'syno'))
    monkeypatch.setattr(disk_usage, 'SYNOLOGY_MARKERS', markers)
    monkeypatch.setattr(disk_usage, 'sys', types.SimpleNamespace(platform='linux'))
    return types.SimpleNamespace(markers=markers, tmp=tmp_path)


def test_plain_linux_host_is_linux(disk_usage, host):
    assert disk_usage.detect_platform() == 'linux'


@pytest.mark.parametrize('marker', [0, 1])
def test_synology_markers_mean_synology(disk_usage, host, marker):
    path = host.markers[marker]
    if marker == 0:
        _write(path, 10)                  # /etc/synoinfo.conf
    else:
        os.makedirs(path)                 # /usr/syno
    assert disk_usage.detect_platform() == 'synology'


def test_windows_is_detected_first(disk_usage, host, monkeypatch):
    _write(host.markers[0], 1)
    monkeypatch.setattr(disk_usage, 'sys', types.SimpleNamespace(platform='win32'))
    assert disk_usage.detect_platform() == 'windows'


def test_config_platform_overrides_detection(collector, host, caplog):
    assert collector.get_platform({}) == 'linux'
    assert collector.get_platform({'platform': ' Synology '}) == 'synology'
    assert collector.get_platform({'platform': 'windows'}) == 'windows'
    with caplog.at_level(logging.WARNING):
        assert collector.get_platform({'platform': 'freebsd'}) == 'linux'
    assert "Invalid platform 'freebsd'" in caplog.text


def test_device_type_defaults_to_server_on_linux(collector, host):
    log = logging.getLogger('test')
    assert collector._identity({'name': 'box'}, log)[2] == 'Server'
    assert collector._identity({'name': 'box', 'platform': 'synology'}, log)[2] == 'unknown'
    assert collector._identity({'name': 'box', 'device_type': 'NAS'}, log)[2] == 'NAS'


@posix_only
def test_linux_default_data_dir(collector, host, monkeypatch, tmp_path):
    monkeypatch.setattr(collector, 'DEFAULT_LINUX_DATA_DIR', str(tmp_path / 'varlib'))
    assert collector.get_data_dir({'name': 'box'}) == str(tmp_path / 'varlib')
    assert collector.get_data_dir({'data_dir': str(tmp_path / 'x')}) == str(tmp_path / 'x')


def test_linux_volume_paths_may_be_any_absolute_path(disk_usage, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert disk_usage.is_valid_volume_path(str(tmp_path), system='linux') is True
        assert disk_usage.is_valid_volume_path('relative/dir', system='linux') is False
        assert disk_usage.is_valid_volume_path(str(tmp_path), system='synology') is False
    assert 'should be /volume* or /mnt' in caplog.text      # Synology keeps its rule


# ---------------------------------------------------------------------------
# /proc/self/mounts
# ---------------------------------------------------------------------------

UBUNTU_MOUNTS = r"""sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
udev /dev devtmpfs rw,nosuid,relatime,size=16334968k,nr_inodes=4083742,mode=755,inode64 0 0
devpts /dev/pts devpts rw,nosuid,noexec,relatime,gid=5,mode=620,ptmxmode=000 0 0
tmpfs /run tmpfs rw,nosuid,nodev,noexec,relatime,size=3270584k,mode=755,inode64 0 0
/dev/nvme0n1p2 / btrfs rw,relatime,ssd,space_cache=v2,subvolid=256,subvol=/@ 0 0
securityfs /sys/kernel/security securityfs rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /dev/shm tmpfs rw,nosuid,nodev,inode64 0 0
cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev,noexec,relatime,nsdelegate,memory_recursiveprot 0 0
efivarfs /sys/firmware/efi/efivars efivarfs rw,nosuid,nodev,noexec,relatime 0 0
bpf /sys/fs/bpf bpf rw,nosuid,nodev,noexec,relatime,mode=700 0 0
systemd-1 /proc/sys/fs/binfmt_misc autofs rw,relatime,fd=29,pgrp=1,timeout=0,minproto=5,maxproto=5,direct 0 0
/dev/loop0 /snap/core22/1612 squashfs ro,nodev,relatime,errors=continue,threads=single 0 0
/dev/loop1 /snap/snapd/21759 squashfs ro,nodev,relatime,errors=continue,threads=single 0 0
/dev/nvme0n1p2 /home btrfs rw,relatime,ssd,space_cache=v2,subvolid=257,subvol=/@home 0 0
/dev/nvme0n1p1 /boot/efi vfat rw,relatime,fmask=0077,dmask=0077,codepage=437,errors=remount-ro 0 0
/dev/sdb1 /data xfs rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,noquota 0 0
/dev/sdb1 /srv/data xfs rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,noquota 0 0
/dev/mapper/vg0-scratch /mnt/scratch\040space ext4 rw,relatime 0 0
/dev/dm-0 /mnt/scratch-again ext4 rw,relatime 0 0
tank/projects /tank/projects zfs rw,xattr,noacl 0 0
nas.lab:/export/archive /mnt/archive nfs4 rw,relatime,vers=4.2,rsize=1048576,wsize=1048576 0 0
//fs.lab/share /mnt/share cifs rw,relatime,vers=3.1.1,cache=strict 0 0
/dev/sda3 /var/lib/docker ext4 rw,relatime 0 0
overlay /var/lib/docker/overlay2/3f1c/merged overlay rw,relatime,lowerdir=/var/lib/docker/overlay2/l/A 0 0
tmpfs /run/user/1000 tmpfs rw,nosuid,nodev,relatime,size=3270580k,mode=700,uid=1000,gid=1000,inode64 0 0
gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0
/dev/sdd1 /media/alice/USB\040Stick vfat rw,nosuid,nodev,relatime,uid=1000 0 0
"""


@pytest.fixture
def ubuntu_mounts(disk_usage, monkeypatch):
    mounts = disk_usage.parse_mounts(UBUNTU_MOUNTS)
    # The same ext4 filesystem appears under two device names (dm-0 = vg0-scratch)
    devices = {'/': 1, '/data': 2, '/mnt/scratch space': 3, '/mnt/scratch-again': 3, '/tank/projects': 4}
    monkeypatch.setattr(disk_usage, '_device_of', lambda path: devices.get(path))
    return mounts


def test_parse_mounts_unescapes_paths(disk_usage):
    mounts = disk_usage.parse_mounts(UBUNTU_MOUNTS + 'garbage-line\n')
    assert len(mounts) == UBUNTU_MOUNTS.count('\n')
    scratch = [m for m in mounts if m.source == '/dev/mapper/vg0-scratch'][0]
    assert scratch.mountpoint == '/mnt/scratch space' and scratch.fstype == 'ext4'
    assert disk_usage.parse_mounts(r'a /x\134y\011z ext4 rw 0 0')[0].mountpoint == '/x\\y\tz'


def test_linux_volumes_are_real_filesystems_once_each(disk_usage, ubuntu_mounts):
    assert disk_usage.discover_linux_volumes(ubuntu_mounts) == [
        '/',                      # btrfs @ ; @home (/home) is the same filesystem
        '/data',                  # xfs; its bind mount /srv/data is left out
        '/mnt/scratch space',     # ext4; /mnt/scratch-again is the same device
        '/tank/projects',         # zfs dataset
    ]   # no proc/sysfs/tmpfs/devtmpfs/squashfs snaps/cgroup2/efivarfs/vfat ESP/autofs/fuse/overlay/docker


def test_network_mounts_only_when_included(disk_usage, ubuntu_mounts):
    volumes = disk_usage.discover_linux_volumes(ubuntu_mounts, include_network=True)
    assert volumes == ['/', '/data', '/mnt/scratch space', '/tank/projects', '/mnt/archive', '/mnt/share']


def test_network_mounts_are_never_stated(disk_usage, monkeypatch):
    # A dead NFS server would hang stat() for minutes
    def no_stat(path):
        raise AssertionError(f'stat({path}) on a network mount')
    monkeypatch.setattr(disk_usage, '_device_of', no_stat)
    mounts = disk_usage.parse_mounts('srv:/x /mnt/x nfs rw 0 0\n//s/y /mnt/y cifs rw 0 0\n')
    assert disk_usage.discover_linux_volumes(mounts, include_network=True) == ['/mnt/x', '/mnt/y']


def test_discover_volumes_linux_branch(disk_usage, ubuntu_mounts):
    assert disk_usage.discover_volumes('linux', mounts=ubuntu_mounts)[0] == '/'


def test_unreadable_mount_table_gives_no_volumes(disk_usage, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert disk_usage.read_mounts(str(tmp_path / 'missing')) == []
    assert 'Cannot read the mount table' in caplog.text


@linux_only
def test_this_hosts_mount_table_parses(disk_usage):
    mounts = disk_usage.read_mounts()
    assert any(m.mountpoint == '/' for m in mounts)
    assert all(disk_usage.is_pseudo_fs(m.fstype) for m in mounts if m.mountpoint == '/proc')


def test_pseudo_filesystem_types(disk_usage):
    for fstype in ('proc', 'sysfs', 'tmpfs', 'devtmpfs', 'overlay', 'squashfs', 'cgroup', 'cgroup2',
                   'efivarfs', 'autofs', 'ramfs', 'fuse.gvfsd-fuse', 'fuse.sshfs', 'fuse'):
        assert disk_usage.is_pseudo_fs(fstype), fstype
    for fstype in ('ext4', 'xfs', 'btrfs', 'zfs', 'nfs4', 'vfat', 'fuseblk'):
        assert not disk_usage.is_pseudo_fs(fstype), fstype


def test_linux_policy_never_enters_pseudo_or_network_mounts(disk_usage, ubuntu_mounts):
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=ubuntu_mounts)
    assert {'/proc', '/sys', '/dev', '/run', '/snap/core22/1612', '/run/user/1000/gvfs',
            '/var/lib/docker/overlay2/3f1c/merged', '/mnt/archive', '/mnt/share'} <= policy.never_enter
    assert not {'/', '/home', '/data', '/srv/data', '/tank/projects'} & policy.never_enter
    included = disk_usage.ScanPolicy('linux', mounts=ubuntu_mounts, include_network=True)
    assert '/mnt/archive' not in included.never_enter


def test_legacy_policy_is_the_default(disk_usage):
    policy = disk_usage.current_policy()
    assert policy.platform == 'synology' and policy.skip_synology_names
    assert not policy.mount_aware and not policy.one_filesystem and not policy.never_enter


# ---------------------------------------------------------------------------
# Scanning: names and one-filesystem semantics
# ---------------------------------------------------------------------------

class _St:
    def __init__(self, size=0, dev=0, nlink=1, ino=0):
        self.st_size, self.st_dev, self.st_nlink, self.st_ino = size, dev, nlink, ino
        self.st_file_attributes = 0


class _Entry:
    def __init__(self, parent, name, is_dir=False, size=0, dev=0):
        self.name, self.path = name, parent + '/' + name
        self._is_dir, self._st = is_dir, _St(size, dev)

    def is_symlink(self):
        return False

    def is_file(self, follow_symlinks=True):
        return not self._is_dir

    def is_dir(self, follow_symlinks=True):
        return self._is_dir

    def is_junction(self):
        return False

    def stat(self, follow_symlinks=True):
        return self._st


class _Scandir:
    def __init__(self, tree):
        self.tree = tree

    def __call__(self, path):
        class _It(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _It(self.tree[path])


@pytest.fixture
def mounted_tree(tmp_path, disk_usage, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Linux mount tables and POSIX paths')
    """
    A real root directory whose (faked) contents include another filesystem
    (different st_dev), a bind mount of the same filesystem, a /proc-like
    pseudo mount, and names that are special on Synology or Linux.
    """
    root = os.path.realpath(str(tmp_path))
    dev = os.stat(root).st_dev
    d = lambda name, size_dev=dev: _Entry(root, name, is_dir=True, dev=size_dev)   # noqa: E731
    tree = {
        root: [_Entry(root, 'f', size=1, dev=dev), d('same'), d('other-fs', dev + 1), d('bind'),
               d('subvol', dev + 2), d('proc'), d('@eaDir'), d('#recycle'), d('vault.hbk'),
               d('lost+found'), d('.snapshots')],
        root + '/same': [_Entry(root + '/same', 'a', size=100, dev=dev)],
        root + '/other-fs': [_Entry(root + '/other-fs', 'big', size=10**9, dev=dev + 1)],
        # Another st_dev but not in the mount table: e.g. a nested btrfs subvolume
        root + '/subvol': [_Entry(root + '/subvol', 'nested', size=10**8, dev=dev + 2)],
        root + '/bind': [_Entry(root + '/bind', 'dup', size=10**6, dev=dev)],
        root + '/proc': [_Entry(root + '/proc', 'kcore', size=10**12, dev=dev)],
        root + '/@eaDir': [_Entry(root + '/@eaDir', 'thumb', size=7, dev=dev)],
        root + '/#recycle': [_Entry(root + '/#recycle', 'old', size=20, dev=dev)],
        root + '/vault.hbk': [_Entry(root + '/vault.hbk', 'v', size=300, dev=dev)],
        root + '/lost+found': [_Entry(root + '/lost+found', 'orphan', size=4000, dev=dev)],
        root + '/.snapshots': [_Entry(root + '/.snapshots', 'snap', size=50000, dev=dev)],
    }
    monkeypatch.setattr(disk_usage.os, 'scandir', _Scandir(tree))
    mounts = [disk_usage.Mount('/dev/sdb1', root + '/bind', 'xfs'),
              disk_usage.Mount('proc', root + '/proc', 'proc'),
              disk_usage.Mount('/dev/sdc1', root + '/other-fs', 'ext4')]
    return types.SimpleNamespace(root=root, mounts=mounts)


def test_linux_scan_stays_on_one_filesystem(disk_usage, mounted_tree, caplog):
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounted_tree.mounts)
    with caplog.at_level(logging.INFO):
        total, files = disk_usage.measure_folder_recursive(mounted_tree.root, policy=policy)
    # f + same + Synology-special names (ordinary on Linux); not other-fs, bind,
    # subvol, proc, lost+found or .snapshots
    assert (total, files) == (1 + 100 + 7 + 20 + 300, 5)
    assert 'did not enter 4 other filesystem(s)' in caplog.text


def test_linux_scan_can_cross_filesystems_but_not_pseudo_ones(disk_usage, mounted_tree):
    policy = disk_usage.ScanPolicy('linux', one_filesystem=False, mounts=mounted_tree.mounts)
    total, _ = disk_usage.measure_folder_recursive(mounted_tree.root, policy=policy)
    assert total == 1 + 100 + 7 + 20 + 300 + 10**9 + 10**6 + 10**8     # still not proc


def test_policy_comes_from_the_active_context(disk_usage, mounted_tree):
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounted_tree.mounts)
    with disk_usage.scan_policy(policy):
        inside = disk_usage.measure_folder_recursive(mounted_tree.root)[0]
    outside = disk_usage.measure_folder_recursive(mounted_tree.root)[0]
    assert inside == 428
    # Historical (Synology) rules outside: '@'/'#recycle'/'.hbk' skipped, everything entered
    assert outside == 1 + 100 + 10**9 + 10**6 + 10**8 + 10**12 + 4000 + 50000


def test_synology_can_opt_into_one_filesystem(disk_usage, mounted_tree):
    policy = disk_usage.ScanPolicy('synology', one_filesystem=True, mounts=mounted_tree.mounts)
    assert disk_usage.measure_folder_recursive(mounted_tree.root, policy=policy)[0] == \
        1 + 100 + 4000 + 50000                     # Synology names still skipped; mounts not entered


@posix_only
def test_linux_glob_skips_lost_found_and_pseudo_mounts(disk_usage, tmp_path):
    for name in ('alice', 'bob', 'lost+found', '.cache', 'proc-like', '@shared', 'nfs'):
        (tmp_path / name).mkdir()
    root = os.path.realpath(str(tmp_path))
    mounts = [disk_usage.Mount('proc', root + '/proc-like', 'proc'),
              disk_usage.Mount('srv:/e', root + '/nfs', 'nfs4')]
    with disk_usage.scan_policy(disk_usage.ScanPolicy('linux', mounts=mounts)):
        found = disk_usage.discover_folders(str(tmp_path), 'linux', validate_volume=False)
    assert sorted(os.path.basename(f) for f in found) == ['@shared', 'alice', 'bob']
    # Synology rules: '@' skipped, the rest are plain folders there
    synology = disk_usage.discover_folders(str(tmp_path), 'synology', validate_volume=False)
    assert sorted(os.path.basename(f) for f in synology) == ['alice', 'bob', 'lost+found', 'nfs', 'proc-like']


@linux_only
def test_kernel_trees_are_never_measured_even_when_configured(tmp_path, collector, config, monkeypatch):
    config.update({'platform': 'linux', 'volumes': [], 'scan_paths': ['/proc/*', '/sys', '/run/*']})
    captured = _capture_disk_message(collector, monkeypatch)
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    assert sorted(data['_errors']) == ['/proc', '/run', '/sys']
    assert not [k for k in data if k.startswith(('/proc', '/sys', '/run'))]


@linux_only
def test_root_glob_never_lists_kernel_trees(disk_usage):
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True)
    with disk_usage.scan_policy(policy):
        found = set(disk_usage.discover_folders('/', 'linux', validate_volume=False))
    assert not found & {'/proc', '/sys', '/dev', '/run', '/lost+found'}


@posix_only
def test_linux_disk_run_end_to_end(tmp_path, collector, config, monkeypatch):
    t = tmp_path / 'tree'
    _write(str(t / 'alice' / 'a'), 1000)
    os.link(t / 'alice' / 'a', t / 'alice' / 'a-link')         # counted once
    _write(str(t / '@shared' / 's'), 500)
    _write(str(t / 'bob' / '#recycle' / 'r'), 300)
    _write(str(t / 'carol' / 'backup.hbk' / 'v'), 400)
    _write(str(t / 'carol' / '.snapshots' / 'old'), 9999)
    _write(str(t / 'lost+found' / 'orphan'), 9999)
    config.update({'platform': 'linux', 'volumes': [str(tmp_path)], 'scan_paths': [str(t) + '/*']})
    captured = _capture_disk_message(collector, monkeypatch)
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    folders = {k[len(str(t)) + 1:]: v for k, v in data.items() if k.startswith(str(t) + '/')}
    assert folders == {'alice': 1000, '@shared': 500, 'bob': 300, 'carol': 400}
    assert data['_errors'] == [] and data['_timed_out'] == []
    assert data[f'{tmp_path}_total_bytes'] > 0
    assert captured[0]['header']['device_type'] == 'Server'


def test_linux_without_scan_paths_reports_capacity_only(tmp_path, collector, config, monkeypatch, caplog):
    _write(str(tmp_path / 'vol' / 'usr' / 'big'), 1000)
    config.update({'platform': 'linux', 'volumes': [str(tmp_path / 'vol')]})
    monkeypatch.setattr(collector, 'scan_folders', lambda *a, **k: pytest.fail('must not scan'))
    captured = _capture_disk_message(collector, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    vol = str(tmp_path / 'vol')
    assert set(k for k in data if not k.startswith('_')) == {
        'data_type', vol, f'{vol}_total_bytes', f'{vol}_free_bytes', 'total_usage'}
    assert 'reporting volume capacity only' in caplog.text and '"/home/*"' in caplog.text


def test_linux_volumes_are_discovered_from_the_mount_table(tmp_path, collector, config, monkeypatch):
    seen = {}

    def fake_discover(nas_type, mounts=None, include_network=False):
        seen.update(nas_type=nas_type, include_network=include_network)
        return [str(tmp_path)]
    monkeypatch.setattr(collector, 'discover_volumes', fake_discover)
    config.update({'platform': 'linux', 'scan_paths': [], 'include_network_mounts': True})
    captured = _capture_disk_message(collector, monkeypatch)
    collector.collect_disk_usage(config, logging.getLogger('test'))
    assert seen == {'nas_type': 'linux', 'include_network': True}
    assert f'{tmp_path}_total_bytes' in captured[0]['data']


def test_bad_boolean_config_falls_back(collector, caplog):
    with caplog.at_level(logging.WARNING):
        policy = collector._scan_policy({'cross_filesystems': 'no'}, 'linux')
    assert policy.one_filesystem is True                      # default kept
    assert "Invalid cross_filesystems='no'" in caplog.text
    assert collector._scan_policy({'cross_filesystems': True}, 'linux').one_filesystem is False
    assert collector._scan_policy({}, 'synology').one_filesystem is False


# ---------------------------------------------------------------------------
# Metrics on Linux with and without psutil
# ---------------------------------------------------------------------------

def test_per_user_stats_without_psutil_is_empty_not_an_error(collector, config, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'psutil', None)
    with caplog.at_level(logging.INFO):
        assert collector.get_per_user_stats(config, logging.getLogger('test'), seeded_procs=[]) == []
    assert 'psutil is not installed' in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class _UserProcs:
    """
    Fake psutil for per-user metrics: process_iter() over (pid, username, uid,
    rss, per-core cpu%) rows; pid None stands for the collector's own process.
    uid None: psutil couldn't read it (no 'uids' value).
    """

    def __init__(self, monkeypatch, rows, cpus=8):
        self.attrs = []
        ps = types.ModuleType('psutil')
        ps.NoSuchProcess = type('NoSuchProcess', (Exception,), {})
        ps.AccessDenied = type('AccessDenied', (Exception,), {})
        ps.cpu_count = lambda logical=True: cpus

        def process_iter(attrs=None):
            self.attrs.append(list(attrs))
            return [_UserProc(os.getpid() if pid is None else pid, user, uid, rss, cpu, 'uids' in attrs)
                    for pid, user, uid, rss, cpu in rows]
        ps.process_iter = process_iter
        monkeypatch.setitem(sys.modules, 'psutil', ps)


class _UserProc:
    def __init__(self, pid, username, uid, rss, cpu, with_uids):
        self.pid = pid
        self.info = {'pid': pid, 'username': username, 'memory_info': types.SimpleNamespace(rss=rss)}
        if with_uids:
            self.info['uids'] = None if uid is None else types.SimpleNamespace(real=uid, effective=uid, saved=uid)
        self._cpu, self._samples = cpu, 0

    def cpu_percent(self, interval=None):
        self._samples += 1
        return 0.0 if self._samples == 1 else self._cpu


MiB = 1024 ** 2
LABMON_UID = 998
# A Ubuntu GPU server during the nightly disk scan
GPU_SERVER_PROCS = [
    (None, 'labmon',              LABMON_UID, 40 * MiB,    3.0),   # this metrics run
    (5000, 'labmon',              LABMON_UID, 300 * MiB,  95.0),   # lab-monitor-disk.service
    (1,    'root',                0,          12 * MiB,    0.1),   # systemd, kernel threads, dockerd ...
    (3001, 'root',                0,          6 * 1024 * MiB, 640.0),   # a docker container's job
    (610,  'systemd-resolve',     101,        20 * MiB,    0.2),
    (620,  'messagebus',          102,        5 * MiB,     0.1),
    (630,  'nvidia-persistenced', 120,        3 * MiB,     0.0),
    (640,  'nobody',              65534,      2 * MiB,     0.0),
    (650,  '999',                 999,        512 * MiB,  80.0),   # container user without a host account
    (2001, 'alice',               1000,       8 * 1024 * MiB, 400.0),
    (2002, 'bob',                 1001,       2 * 1024 * MiB, 64.0),
]


@pytest.fixture
def ubuntu_accounts(collector, monkeypatch, tmp_path):
    """/etc/login.defs as on Ubuntu, the collector running as labmon (a system account)."""
    if sys.platform == 'win32':
        pytest.skip('POSIX user IDs (os.getuid)')
    login_defs = tmp_path / 'login.defs'
    login_defs.write_text('# comment\nUID_MIN\t\t\t 1000\nUID_MAX\t\t\t60000\nSYS_UID_MIN\t\t  100\n')
    monkeypatch.setattr(collector, 'LOGIN_DEFS', str(login_defs))
    monkeypatch.setattr(os, 'getuid', lambda: LABMON_UID)
    monkeypatch.setattr(sys, 'platform', 'linux')
    return login_defs


def _users(collector, config, monkeypatch, rows=GPU_SERVER_PROCS):
    fake = _UserProcs(monkeypatch, rows)
    seeded = collector._seed_user_processes(config)
    stats = collector.get_per_user_stats(config, logging.getLogger('test'), seeded_procs=seeded)
    return {u['username']: u for u in stats}, fake


def test_linux_per_user_metrics_leave_out_the_collector_and_daemon_accounts(
        collector, config, monkeypatch, ubuntu_accounts):
    config['platform'] = 'linux'
    users, fake = _users(collector, config, monkeypatch)
    # Neither the metrics run nor the concurrent disk scan (both labmon), nor
    # systemd-resolve / messagebus / nvidia-persistenced / nobody
    assert set(users) == {'alice', 'bob', 'root', '999'}
    assert users['root']['cpu_percent'] == round(640.1 / 8, 2)
    assert users['999']['ram_bytes'] == 512 * MiB          # no account here: a container's user, shown
    assert fake.attrs == [['pid', 'username', 'memory_info', 'uids']]


def test_linux_system_accounts_can_be_shown_but_never_the_collector(
        collector, config, monkeypatch, ubuntu_accounts):
    config.update(platform='linux', exclude_system_accounts=False)
    users, _ = _users(collector, config, monkeypatch)
    assert set(users) == {'alice', 'bob', 'root', '999', 'systemd-resolve', 'messagebus',
                          'nvidia-persistenced', 'nobody'}      # labmon still hidden


def test_exclude_users_still_applies(collector, config, monkeypatch, ubuntu_accounts):
    config.update(platform='linux', exclude_users=['root', 'b*'])
    users, _ = _users(collector, config, monkeypatch)
    assert set(users) == {'alice', '999'}


def test_collector_run_by_hand_as_a_regular_user_counts_that_users_other_processes(
        collector, config, monkeypatch, ubuntu_accounts):
    monkeypatch.setattr(os, 'getuid', lambda: 1000)        # alice runs it
    config['platform'] = 'linux'
    rows = [(None, 'alice', 1000, 40 * MiB, 3.0), (2001, 'alice', 1000, 1024 * MiB, 400.0),
            (5000, 'labmon', LABMON_UID, 300 * MiB, 95.0)]
    users, _ = _users(collector, config, monkeypatch, rows)
    assert set(users) == {'alice'} and users['alice']['ram_bytes'] == 1024 * MiB   # own process only


@pytest.mark.parametrize('content,uid_min', [
    ('UID_MIN 500\n', 500),
    ('#UID_MIN 500\nUID_MIN\t2000 # site policy\n', 2000),
    ('UID_MIN\n', 1000), ('UID_MIN abc\n', 1000), ('', 1000), (None, 1000),
])
def test_uid_min_comes_from_login_defs(collector, monkeypatch, tmp_path, content, uid_min):
    path = tmp_path / 'login.defs'
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(collector, 'LOGIN_DEFS', str(path))
    assert collector._uid_min() == uid_min


def test_system_account_range_follows_uid_min(collector, config, monkeypatch, ubuntu_accounts):
    ubuntu_accounts.write_text('UID_MIN 500\n')            # an old RHEL-style layout
    config['platform'] = 'linux'
    rows = [(2001, 'olduser', 600, MiB, 8.0), (610, 'daemon', 2, MiB, 8.0)]
    users, _ = _users(collector, config, monkeypatch, rows)
    assert set(users) == {'olduser'}


def test_synology_keeps_showing_system_accounts_by_default(collector, config, monkeypatch, ubuntu_accounts):
    monkeypatch.setattr(os, 'getuid', lambda: 0)            # DSM Task Scheduler runs it as root
    config['platform'] = 'synology'
    rows = [(1, 'root', 0, MiB, 1.0), (700, 'postgres', 55, MiB, 1.0), (2001, 'alice', 1026, MiB, 8.0)]
    users, _ = _users(collector, config, monkeypatch, rows)
    assert set(users) == {'root', 'postgres', 'alice'}


def test_unreadable_uid_falls_back_to_the_name(collector, config, monkeypatch, ubuntu_accounts):
    config['platform'] = 'linux'
    rows = [(610, 'systemd-resolve', None, MiB, 1.0), (2001, 'alice', 1000, MiB, 8.0)]
    users, _ = _users(collector, config, monkeypatch, rows)
    assert set(users) == {'systemd-resolve', 'alice'}       # can't tell: counted, as before


def test_windows_does_not_ask_psutil_for_uids(collector, config, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')           # 'uids' would raise ValueError there
    rows = [(None, 'NT AUTHORITY\\SYSTEM', None, MiB, 1.0), (2001, 'LAB\\alice', None, MiB, 8.0)]
    users, fake = _users(collector, config, monkeypatch, rows)
    assert set(users) == {'LAB\\alice'}
    assert fake.attrs == [['pid', 'username', 'memory_info']]


@linux_only
def test_metrics_from_proc_without_psutil(collector, config, monkeypatch):
    # This host's real /proc: interval counters, disk I/O and per-user CPU
    # all work without psutil (armv7 DSM units have none)
    monkeypatch.setitem(sys.modules, 'psutil', None)
    monkeypatch.setitem(sys.modules, 'pynvml', None)
    captured = []

    def record(cfg, n, entries, log):
        captured.append(entries)
        return True
    monkeypatch.setattr(collector, '_record', record)
    monkeypatch.setattr(collector, '_sync', lambda *a: True)
    config['platform'] = 'linux'
    for run in range(2):
        assert collector.collect_metrics(config, logging.getLogger('test')) is True
        if run == 0:
            # second run more than 5 s later: move the baseline back instead of sleeping
            path = collector.get_perf_snapshot_path(config, 'testbox')
            with open(path) as f:
                snap = json.load(f)
            snap['boot']['mono'] -= 10
            snap['boot']['wall'] -= 10
            with open(path, 'w') as f:
                json.dump(snap, f)
    # Each run records twice: the metrics, then the optional collectors' messages
    runs = [batch for batch in captured if batch[0]['data']['data_type'] == 'system_metrics']
    first, second = runs[0][0]['data'], runs[1][0]['data']
    for data in (first, second):
        assert 0 <= data['cpu_percent'] <= 100 and 0 < data['ram_percent'] < 100
        assert data['total_ram_bytes'] > 0 and data['uptime_seconds'] > 0
        assert data['network_bytes_in'] is not None and data['network_bytes_out'] is not None
    assert first['interval_s'] is None and 9 <= second['interval_s'] <= 30
    assert second['cpu_iowait_percent'] is not None and second['proc_cpu_coverage_percent'] is not None
    users = [m['data'] for m in runs[1][1:]]
    assert users and all(m['data_type'] == 'user_metrics' for m in users)
    assert all(isinstance(u['cpu_seconds'], float) and u['process_count'] >= 0 for u in users)


def test_metrics_unit_lets_the_collector_read_its_sources():
    # Everything metrics mode reads as labmon, without root or capabilities:
    # counters, every user's processes, sessions, storage health Tier A, NVML
    svc = _unit('lab-monitor-metrics.service')['Service']
    needed = ('/proc/stat', '/proc/uptime', '/proc/diskstats', '/proc/mounts', '/proc/1/stat', '/proc/1/status',
              '/proc/sys/kernel/random/boot_id', '/proc/net/tcp', '/proc/net/tcp6', '/proc/mdstat',
              '/sys/block', '/sys/class/hwmon', '/run/utmp', '/run/systemd/sessions', '/etc/passwd',
              '/etc/login.defs', '/dev/nvidiactl', '/proc/driver/nvidia')
    hidden = []
    for key in ('InaccessiblePaths', 'TemporaryFileSystem', 'BindPaths', 'BindReadOnlyPaths'):
        for value in ' '.join(svc.get(key, [])).split():
            path = value.lstrip('-+').split(':')[0].rstrip('/')
            hidden += [(key, p) for p in needed if p == path or p.startswith(path + '/')]
    assert hidden == []
    # Not ProtectProc=invisible / ProcSubset=pid / PrivateUsers (other users' /proc entries),
    # nor PrivateNetwork (/proc/net/tcp would show only the sandbox's own sockets)
    for key in ('ProtectProc', 'ProcSubset', 'PrivateUsers', 'PrivateNetwork', 'PrivateDevices'):
        assert svc.get(key, ['no'])[-1] in ('no', 'default'), key
    # ProtectKernelTunables / ProtectControlGroups only make /proc/sys and /sys read-only
    assert svc['ProtectSystem'] == ['strict'] and svc['ReadWritePaths'] == ['/var/lib/lab-monitor /var/log/lab-monitor']
    assert svc.get('AmbientCapabilities', ['']) == [''] and svc['CapabilityBoundingSet'] == ['']


# ---------------------------------------------------------------------------
# install-collector-linux.sh
# ---------------------------------------------------------------------------

TOKEN = 'collector-token-0123456789abcdef'


def run_installer(*args, env=None, check=False):
    return subprocess.run(['bash', INSTALLER, *args], capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})), stdin=subprocess.DEVNULL,
                          timeout=120, check=check)


def run_sourced(script, env=None):
    """Source the installer (its main() doesn't run) and run `script` with its functions."""
    return subprocess.run(['bash', '-c', f'source "{INSTALLER}"\n{script}'], capture_output=True,
                          text=True, env=dict(os.environ, **(env or {})), stdin=subprocess.DEVNULL,
                          timeout=120)


@needs_bash
def test_installer_syntax_and_help():
    subprocess.run(['bash', '-n', INSTALLER], check=True)
    out = run_installer('--help', check=True).stdout
    for flag in ('--manager-url', '--token', '--generate-token', '--name', '--scan-paths', '--volumes',
                 '--non-interactive', '--uninstall', '--purge', '--dry-run', '--repo', '--ref'):
        assert flag in out
    with open(INSTALLER) as f:
        text = f.read()
    assert 'set -euo pipefail' in text
    assert 'read -p' not in text                    # always read -r
    assert not re.search(r'(requests|psutil)==\d', text)   # versions come from requirements.txt
    assert re.search(r'^\s*sudo\b', text, re.M) is None    # it is run with sudo, never calls it


@needs_bash
@pytest.mark.skipif(shutil.which('shellcheck') is None, reason='shellcheck not installed')
def test_installer_is_shellcheck_clean():
    subprocess.run(['shellcheck', '-x', INSTALLER], check=True)


def _print_config(*args, env=None):
    proc = run_installer('--print-config', '--manager-url', 'http://mgr.lab:5000/', *args, env=env)
    return proc, (json.loads(proc.stdout) if proc.returncode == 0 else None)


@needs_bash
def test_print_config_writes_valid_json_from_hostile_values(tmp_path):
    nasty = 'tok"en\\with$(touch${IFS}' + str(tmp_path / 'pwned') + ')`id`-0123456789'
    proc, config = _print_config('--token', nasty, '--name', 'Lab-A.1_x', '--device-type', 'NAS',
                                 '--scan-paths', " /srv/Bob's Data , \"/data/My Stuff\" ,, /home/*",
                                 '--volumes', '/,/data')
    assert proc.returncode == 0, proc.stderr
    assert config['manager_token'] == nasty and not (tmp_path / 'pwned').exists()
    assert config['scan_paths'] == ["/srv/Bob's Data", '/data/My Stuff', '/home/*']
    assert config['volumes'] == ['/', '/data']
    assert config['name'] == 'Lab-A.1_x' and config['id'] == 'linux-lab-a.1_x'
    assert config['device_type'] == 'NAS' and config['platform'] == 'linux'
    assert config['manager_url'] == 'http://mgr.lab:5000'
    assert config['data_dir'] == '/var/lib/lab-monitor'
    assert config['log_file'] == '/var/log/lab-monitor/collector.log'
    assert config['cross_filesystems'] is False and config['include_network_mounts'] is False
    assert config['total_timeout_seconds'] == 10800 and config['max_batch_messages'] == 100


@needs_bash
def test_print_config_defaults(monkeypatch):
    proc, config = _print_config('--token', TOKEN)
    assert proc.returncode == 0, proc.stderr
    assert 'volumes' not in config                          # auto-discovered
    assert config['scan_paths'][0] == '/home/*'
    assert re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', config['name'])
    assert config['device_type'] == 'Server'
    assert config['exclude_system_accounts'] is True and config['exclude_users'] == []
    proc, config = _print_config('--token', TOKEN, '--scan-paths', 'none', '--volumes', 'auto')
    assert config['scan_paths'] == [] and 'volumes' not in config


@needs_bash
def test_print_config_spells_out_the_new_keys_like_the_example(collector, tmp_path):
    # Counters, per-user GPU, sessions, storage health and breakdown: the
    # same values as config.linux.example.json, and the collector's defaults
    from lib import settings
    proc, config = _print_config('--token', TOKEN)
    assert proc.returncode == 0, proc.stderr
    with open(os.path.join(REPO_DIR, 'collector', 'config.linux.example.json')) as f:
        example = json.load(f)
    for key in settings.SCHEMA:
        assert settings.raw_value(config, key) == settings.raw_value(example, key), key
        assert settings.value(config, key) == settings.SCHEMA[key][1], key
    path = tmp_path / 'printed.json'
    path.write_text(json.dumps(config))
    check = subprocess.run([sys.executable, os.path.join(REPO_DIR, 'collector', 'config_tool.py'),
                            '--config', str(path), 'check'], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stdout


@needs_bash
def test_generate_token_prints_the_manager_entry():
    proc, config = _print_config('--generate-token', '--name', 'box1')
    assert proc.returncode == 0, proc.stderr
    token = config['manager_token']
    assert re.fullmatch(r'[A-Za-z0-9_-]{43}', token)       # 32 random bytes, urlsafe
    line = next(l.strip() for l in proc.stderr.splitlines() if l.strip().startswith('"collector_tokens"'))
    assert json.loads('{' + line + '}') == {'collector_tokens': {'box1': token}}
    assert _print_config('--generate-token')[1]['manager_token'] != token


@needs_bash
@pytest.mark.parametrize('name,ok', [
    ('box1', True), ('Lab-A.1_x', True), ('a' * 64, True),
    ('bad name', False), ('a' * 65, False), ('a/b', False), ('naïve', False), ('x;id', False),
])
def test_installer_validates_device_name(name, ok):
    proc, _ = _print_config('--token', TOKEN, '--name', name)
    assert (proc.returncode == 0) is ok, proc.stderr
    if not ok:
        assert proc.returncode == 2 and '1-64 characters' in proc.stderr


@needs_bash
@pytest.mark.parametrize('args,message', [
    (['--token', 'short'], 'shorter than 16'),
    (['--token', 'YOUR-COLLECTOR-TOKEN-FOR-THIS-DEVICE'], 'placeholder'),
    (['--token', TOKEN, '--generate-token'], 'not both'),
    (['--token', TOKEN, '--scan-paths', 'relative/dir'], 'absolute path'),
    ([], 'token is required'),
])
def test_installer_rejects_bad_settings(args, message):
    proc, _ = _print_config(*args)
    assert proc.returncode == 2 and message in proc.stderr, proc.stderr


@needs_bash
def test_installer_name_check_matches_the_manager():
    names = ['box1', 'Lab-A.1_x', 'a' * 64, 'bad name', 'a' * 65, 'a/b', '', 'x;id', 'naïve']
    script = '\n'.join(f"valid_name '{n}' && echo yes || echo no" for n in names)
    proc = run_sourced(script)
    assert proc.stdout.split() == ['yes', 'yes', 'yes', 'no', 'no', 'no', 'no', 'no', 'no'], proc.stderr


@needs_bash
def test_installer_rejects_bad_url_and_flags():
    proc = run_installer('--print-config', '--manager-url', 'ftp://x', '--token', TOKEN)
    assert proc.returncode == 2 and 'http:// or https://' in proc.stderr
    assert run_installer('--purge').returncode == 2
    assert run_installer('--bogus').returncode == 2


@needs_bash
def test_rerun_keeps_config_and_changes_only_flagged_keys(tmp_path):
    existing = {'name': 'box1', 'id': 'my-own-id', 'device_type': 'NAS', 'platform': 'linux',
                'manager_url': 'http://old:5000', 'manager_token': TOKEN,
                'scan_paths': ['/data/*'], 'volumes': ['/data'], 'custom_key': [1, 2],
                'data_dir': '/var/lib/lab-monitor', 'log_file': '/var/log/lab-monitor/collector.log'}
    cfg = tmp_path / 'collector.json'
    cfg.write_text(json.dumps(existing))
    proc = run_sourced(f'CONFIG_FILE="{cfg}"\nmain --print-config --manager-url http://new:5000')
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {**existing, 'manager_url': 'http://new:5000'}

    proc = run_sourced(f'CONFIG_FILE="{cfg}"\nmain --print-config --volumes auto --scan-paths "/home/*"')
    merged = json.loads(proc.stdout)
    assert 'volumes' not in merged and merged['scan_paths'] == ['/home/*'] and merged['custom_key'] == [1, 2]


@needs_bash
@posix_only
def test_config_file_is_written_atomically_with_mode_0640(tmp_path):
    out = tmp_path / 'collector.json'
    env = {'LM_NAME': 'box1', 'LM_MANAGER_URL': 'https://mgr', 'LM_MANAGER_TOKEN': TOKEN,
           'LM_SCAN_PATHS': '/home/*', 'LM_SCAN_PATHS_SET': '1'}
    proc = run_sourced(f'write_config "{out}"', env=env)
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o640
    assert json.loads(out.read_text())['manager_token'] == TOKEN
    assert os.listdir(tmp_path) == ['collector.json']      # no temp file left behind


@needs_bash
@pytest.mark.parametrize('config', [
    {},
    {'timeout_seconds': 100, 'scan_paths': ['/a', '/b', '/c', '/d', '/e']},
    {'timeout_seconds': 100, 'volumes': ['/a']},
    {'total_timeout_seconds': 7200, 'scan_paths': ['/a'] * 9},
    {'timeout_seconds': 'bad', 'total_timeout_seconds': True},
])
def test_disk_unit_timeout_matches_the_collector_budget(collector, tmp_path, config):
    cfg = tmp_path / 'c.json'
    cfg.write_text(json.dumps(config))
    proc = run_sourced(f'disk_timeout_seconds "{cfg}"')
    assert proc.returncode == 0, proc.stderr
    timeout = collector._cfg_number(config, 'timeout_seconds', collector.DEFAULT_TIMEOUT_SECONDS, minimum=1)
    entries = len(config.get('scan_paths') or config.get('volumes') or [])
    budget = collector._cfg_number(config, 'total_timeout_seconds',
                                   collector._default_total_timeout(timeout, entries, windows=False),
                                   minimum=1)
    assert int(proc.stdout) == int(budget) + 3600


@needs_bash
@pytest.mark.skipif(not os.path.isdir('/run/systemd/system') or _IS_ROOT
                    or os.path.exists('/etc/lab-monitor/collector.json'),
                    reason='needs a systemd host without the collector installed, and a non-root user')
def test_dry_run_changes_nothing(tmp_path):
    proc = run_installer('--dry-run', '--non-interactive', '--manager-url', 'http://127.0.0.1:9',
                         '--generate-token', '--scan-paths', '/home/*')
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert '[dry-run] systemctl enable --now lab-monitor-metrics.timer lab-monitor-disk.timer' in out
    assert '[dry-run] systemctl start lab-monitor-metrics.service' in out
    assert '"manager_token": "<redacted>"' in out
    assert '"collector_tokens":' in out
    proc = run_installer('--uninstall', '--purge', '--dry-run', '--non-interactive')
    assert proc.returncode == 0 and '[dry-run] rm -rf -- /opt/lab-monitor' in proc.stdout


@posix_only
@needs_bash
@pytest.mark.skipif(_IS_ROOT, reason='checks the non-root refusal')
def test_installer_needs_root_unless_dry_run():
    proc = run_installer('--non-interactive', '--manager-url', 'http://m:5000', '--token', TOKEN)
    assert proc.returncode == 1 and 'sudo' in proc.stderr


# ---------------------------------------------------------------------------
# systemd units (collector/systemd/)
# ---------------------------------------------------------------------------

def _unit(name):
    """{section: {key: [values]}} of a unit file."""
    sections, current = {}, None
    with open(os.path.join(UNITS_DIR, name)) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if line.startswith('['):
                current = sections.setdefault(line.strip('[]'), {})
                continue
            key, _, value = line.partition('=')
            current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _seconds(value):
    units = {'s': 1, 'sec': 1, 'min': 60, 'm': 60, 'h': 3600, 'hr': 3600}
    m = re.fullmatch(r'(\d+)\s*([a-z]*)', value)
    return int(m.group(1)) * units.get(m.group(2) or 's')


HARDENING = {
    'NoNewPrivileges': 'yes', 'ProtectSystem': 'strict', 'PrivateTmp': 'yes',
    'ProtectKernelTunables': 'yes', 'ProtectKernelModules': 'yes', 'LockPersonality': 'yes',
    'SystemCallFilter': '@system-service',
    'ReadWritePaths': '/var/lib/lab-monitor /var/log/lab-monitor', 'User': 'labmon', 'Type': 'oneshot',
}
# The metrics unit loads the NVIDIA driver's libnvidia-ml, which isn't known to
# be W^X-safe (see the comment in the unit); the disk unit loads nothing native.
MEMORY_DENY_WRITE_EXECUTE = {'metrics': 'no', 'disk': 'yes'}
# The disk unit has no network at all; the metrics unit only IP to the Manager
ADDRESS_FAMILIES = {'metrics': 'AF_INET AF_INET6 AF_UNIX', 'disk': 'AF_UNIX'}
EXTRA_ARGS = {'metrics': '', 'disk': ' --no-sync'}


@pytest.mark.parametrize('mode', ['metrics', 'disk'])
def test_service_units_are_hardened(mode):
    svc = _unit(f'lab-monitor-{mode}.service')['Service']
    for key, value in HARDENING.items():
        assert svc.get(key) == [value], key
    assert svc.get('MemoryDenyWriteExecute') == [MEMORY_DENY_WRITE_EXECUTE[mode]]
    assert svc.get('RestrictAddressFamilies') == [ADDRESS_FAMILIES[mode]]
    assert svc.get('IPAddressDeny') == ['any']
    # -s: no user site-packages from labmon's writable HOME (see test_user_site_is_off_in_units)
    assert svc['ExecStart'] == [
        '/opt/lab-monitor/venv/bin/python -s /opt/lab-monitor/collector/collector.py '
        f'--config /etc/lab-monitor/collector.json --mode {mode}{EXTRA_ARGS[mode]}']
    assert 'PYTHONNOUSERSITE=1' in svc['Environment'][0].split()
    # Per-user metrics read every user's /proc entries; PrivateUsers would turn them into 'nobody'
    assert 'ProtectProc' not in svc and 'ProcSubset' not in svc and 'PrivateUsers' not in svc
    assert svc.get('User') != ['root']


def test_metrics_unit_has_no_capabilities_and_a_short_timeout():
    svc = _unit('lab-monitor-metrics.service')['Service']
    assert svc['CapabilityBoundingSet'] == [''] and svc.get('AmbientCapabilities', ['']) == ['']
    # Loopback only in the unit itself; the installer's drop-in adds the Manager
    assert svc['IPAddressAllow'] == ['localhost'] and 'PrivateNetwork' not in svc
    assert svc['ProtectHome'] == ['yes']
    assert _seconds(svc['TimeoutStartSec'][0]) < 5 * 60


_TRUE = ('yes', 'true', 'on', '1')


def nvml_blockers(svc):
    """
    Settings of a [Service] section ({key: [values]}) that keep NVML from the
    GPUs: hidden or blocked /dev/nvidia* nodes, or a hidden /proc/driver/nvidia.
    """
    def last(key):
        values = svc.get(key)
        return values[-1].strip().lower() if values else None

    problems = []
    if last('PrivateDevices') in _TRUE:
        problems.append('PrivateDevices=yes hides /dev/nvidia*')
    if last('ProcSubset') == 'pid':
        problems.append('ProcSubset=pid hides /proc/driver/nvidia')
    allows = [v for v in svc.get('DeviceAllow', []) if v.strip()]
    # Any DeviceAllow= entry, DevicePolicy=closed/strict, and on systemd < 252
    # ProtectClock=yes (an implicit DeviceAllow=char-rtc r) block every device not listed
    if last('DevicePolicy') in ('closed', 'strict') or allows or last('ProtectClock') in _TRUE:
        if not any(re.match(r'(char-nvidia|/dev/nvidia)', a.split()[0]) for a in allows):
            problems.append('devices are restricted without a DeviceAllow= for the NVIDIA devices '
                            '(e.g. DeviceAllow=char-nvidia* rw)')
    for key in ('InaccessiblePaths', 'TemporaryFileSystem', 'NoExecPaths'):
        for value in ' '.join(svc.get(key, [])).split():
            path = value.lstrip('-+').split(':')[0].rstrip('/')
            if any(p == path or p.startswith(path + '/') for p in
                   ('/dev/nvidiactl', '/proc/driver/nvidia', '/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1')):
                problems.append(f'{key}={value} covers what NVML needs')
    return problems


def test_metrics_unit_lets_nvml_reach_the_gpus():
    svc = _unit('lab-monitor-metrics.service')['Service']
    assert nvml_blockers(svc) == []
    # NVML talks to nvidia-persistenced over AF_UNIX
    assert 'AF_UNIX' in svc['RestrictAddressFamilies'][0].split()


@pytest.mark.parametrize('extra,blocked', [
    ({'PrivateDevices': ['yes']}, True),
    ({'DevicePolicy': ['closed']}, True),
    ({'DevicePolicy': ['strict'], 'DeviceAllow': ['/dev/null rw']}, True),
    ({'DeviceAllow': ['char-rtc r']}, True),                     # policy "auto" + any allow-list
    ({'ProtectClock': ['yes']}, True),                           # implicit char-rtc on systemd < 252
    ({'ProcSubset': ['pid']}, True),
    ({'InaccessiblePaths': ['/proc/driver']}, True),
    ({'DevicePolicy': ['closed'], 'DeviceAllow': ['char-nvidia* rw']}, False),
    ({'ProtectClock': ['yes'], 'DeviceAllow': ['char-nvidia* rw', 'char-rtc r']}, False),
    ({'DevicePolicy': ['auto']}, False),
    ({'PrivateDevices': ['no']}, False),
])
def test_nvml_blockers_are_recognised(extra, blocked):
    svc = {**_unit('lab-monitor-metrics.service')['Service'], **extra}
    assert bool(nvml_blockers(svc)) is blocked, nvml_blockers(svc)


def test_disk_unit_reads_everything_without_root_but_has_no_network():
    unit = _unit('lab-monitor-disk.service')
    svc = unit['Service']
    assert svc['AmbientCapabilities'] == ['CAP_DAC_READ_SEARCH']
    assert svc['CapabilityBoundingSet'] == ['CAP_DAC_READ_SEARCH']
    # The storage breakdown runs in the same walk: st_uid / st_mtime from the
    # lstat it already makes, and owner names through NSS (/etc/passwd, or
    # sssd / nscd / winbind over a unix socket) within its own budget. Those
    # sockets are files, which PrivateNetwork leaves reachable; PrivateUsers
    # would turn every owner into 'nobody'
    assert 'AF_UNIX' in svc['RestrictAddressFamilies'][0].split()
    assert svc.get('PrivateUsers', ['no'])[-1] in ('no', 'default')
    # The capability holder can't send anything anywhere: the next metrics run sends its result
    assert svc['PrivateNetwork'] == ['yes'] and svc['RestrictAddressFamilies'] == ['AF_UNIX']
    assert svc['IPAddressDeny'] == ['any'] and 'IPAddressAllow' not in svc
    assert svc['ExecStart'][0].endswith('--mode disk --no-sync')
    assert 'network-online.target' not in ' '.join(sum(unit['Unit'].values(), []))
    hidden = svc['InaccessiblePaths'][0].split()
    for path in ('/root', '/etc/shadow', '/etc/gshadow', '/etc/ssh', '/etc/sudoers', '/etc/sudoers.d'):
        assert '-' + path in hidden
    assert svc['ProtectHome'] == ['read-only']
    assert svc['Nice'] == ['19'] and svc['IOSchedulingClass'] == ['idle']
    # Default budget written by the installer (total_timeout_seconds 10800) + 1 h
    assert _seconds(svc['TimeoutStartSec'][0]) == 10800 + 3600


def test_timers():
    metrics = _unit('lab-monitor-metrics.timer')
    assert metrics['Timer'] == {'OnCalendar': ['*:0/5'], 'AccuracySec': ['10s'], 'Persistent': ['true'],
                                'Unit': ['lab-monitor-metrics.service']}
    disk = _unit('lab-monitor-disk.timer')
    assert disk['Timer'] == {'OnCalendar': ['*-*-* 02:00:00'], 'RandomizedDelaySec': ['15min'],
                             'Persistent': ['true'], 'Unit': ['lab-monitor-disk.service']}
    for timer in (metrics, disk):
        assert timer['Install'] == {'WantedBy': ['timers.target']}


def test_installer_installs_exactly_the_shipped_units():
    with open(INSTALLER) as f:
        text = f.read()
    shipped = sorted(os.listdir(UNITS_DIR))
    listed = re.search(r'^UNITS=\(([^)]*)\)', text, re.M).group(1).split()
    assert sorted(listed) == shipped
    for path in ('PREFIX=/opt/lab-monitor', 'CONFIG_DIR=/etc/lab-monitor', 'DATA_DIR=/var/lib/lab-monitor',
                 'LOG_DIR=/var/log/lab-monitor', 'SERVICE_USER=labmon'):
        assert path in text


@pytest.mark.skipif(shutil.which('systemd-analyze') is None, reason='needs systemd-analyze')
def test_units_pass_systemd_analyze_verify(tmp_path):
    for name in os.listdir(UNITS_DIR):
        with open(os.path.join(UNITS_DIR, name)) as f:
            text = f.read()
        # The venv doesn't exist here: point ExecStart at this interpreter and checkout
        text = text.replace('/opt/lab-monitor/venv/bin/python', sys.executable)
        text = text.replace('/opt/lab-monitor/collector/', COLLECTOR_DIR + '/')
        (tmp_path / name).write_text(text)
    proc = subprocess.run(['systemd-analyze', 'verify', *sorted(str(p) for p in tmp_path.iterdir())],
                          capture_output=True, text=True, timeout=120)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 and 'lab-monitor' not in output:
        pytest.skip(f'systemd-analyze unusable here: {output[:200]}')
    assert proc.returncode == 0 and 'lab-monitor' not in output, output


# ---------------------------------------------------------------------------
# Review fixes: scan roots on never-entered filesystems, automounts,
# volume metadata, queue files, installer details
# ---------------------------------------------------------------------------

def _nfs_home(tmp_path, include_network=False):
    """A tree whose <tmp>/home is an NFS mount (as with an fstab nas:/export/home /home nfs4)."""
    root = os.path.realpath(str(tmp_path))
    _write(os.path.join(root, 'home', 'alice', 'a'), 4000)
    _write(os.path.join(root, 'home', 'bob', 'b'), 10)
    _write(os.path.join(root, 'local', 'c'), 7)
    mounts = [_mount('/dev/sda2', '/', 'ext4'), _mount('nas:/export/home', root + '/home', 'nfs4')]
    return root, mounts


def _mount(source, mountpoint, fstype):
    from lib.disk_usage import Mount
    return Mount(source, mountpoint, fstype)


@posix_only
@pytest.mark.parametrize('one_filesystem', [True, False])
def test_scan_root_on_a_network_mount_is_refused(disk_usage, tmp_path, one_filesystem):
    root, mounts = _nfs_home(tmp_path)
    policy = disk_usage.ScanPolicy('linux', one_filesystem=one_filesystem, mounts=mounts)
    with disk_usage.scan_policy(policy):
        with pytest.raises(OSError, match='include_network_mounts'):
            disk_usage.discover_folders(root + '/home', 'linux', validate_volume=False, raise_errors=True)
        assert disk_usage.discover_folders(root + '/home', 'linux', validate_volume=False) == []
        for path in (root + '/home', root + '/home/alice'):
            with pytest.raises(OSError, match=r'network filesystem \(nfs4\)'):
                disk_usage.measure_folder_recursive(path)
        # The parent is measured, without entering the mount
        assert disk_usage.measure_folder_recursive(root)[0] == 7
    included = disk_usage.ScanPolicy('linux', one_filesystem=one_filesystem, mounts=mounts,
                                     include_network=True)
    with disk_usage.scan_policy(included):
        assert disk_usage.measure_folder_recursive(root + '/home')[0] == 4010


@posix_only
def test_symlink_into_a_network_mount_is_refused(disk_usage, tmp_path):
    root, mounts = _nfs_home(tmp_path)
    os.symlink(root + '/home', root + '/local/homes')
    with disk_usage.scan_policy(disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounts)):
        with pytest.raises(OSError, match='nfs4'):
            disk_usage.discover_folders(root + '/local/homes', 'linux', validate_volume=False,
                                        raise_errors=True)
        with pytest.raises(OSError, match='nfs4'):
            disk_usage.measure_folder_recursive(root + '/local/homes')


@posix_only
def test_collector_reports_network_scan_roots_in_errors_without_touching_them(
        tmp_path, collector, config, disk_usage, monkeypatch):
    root, mounts = _nfs_home(tmp_path)
    monkeypatch.setattr(disk_usage, 'read_mounts', lambda path='/proc/self/mounts': mounts)
    real_isdir = os.path.isdir

    def isdir(path):   # a dead NFS server would hang this stat()
        assert not str(path).startswith(root + '/home'), f'stat({path}) on a network mount'
        return real_isdir(path)
    monkeypatch.setattr(collector.os.path, 'isdir', isdir)
    config.update({'platform': 'linux', 'volumes': [root],
                   'scan_paths': [root + '/home/*', root + '/home/alice', root + '/local']})
    captured = _capture_disk_message(collector, monkeypatch)
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    data = captured[0]['data']
    assert data['_errors'] == [root + '/home', root + '/home/alice']
    assert data[root + '/local'] == 7
    assert not [k for k in data if k.startswith(root + '/home')]


@posix_only
def test_installer_default_scan_paths_on_an_nfs_home_are_refused(disk_usage):
    mounts = [_mount('/dev/sda2', '/', 'ext4'), _mount('nas:/export/home', '/home', 'nfs4'),
              _mount('/dev/sdb1', '/data', 'xfs')]
    with disk_usage.scan_policy(disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounts)):
        assert 'include_network_mounts' in disk_usage.scan_root_refusal('/home')
        assert disk_usage.scan_root_refusal('/data') is None
        assert disk_usage.scan_root_refusal('/') is None
    assert disk_usage.scan_root_refusal('/home') is None     # legacy (Synology) policy: unchanged


def test_mount_point_lookup_is_longest_prefix(disk_usage):
    mounts = [_mount('a', '/', 'ext4'), _mount('b', '/srv', 'xfs'), _mount('c', '/srv/nfs', 'nfs'),
              _mount('d', '/srv/nfs/local', 'ext4')]
    policy = disk_usage.ScanPolicy('linux', mounts=mounts)
    assert policy.mount_point_of('/srv/nfsx') == '/srv'
    assert policy.mount_point_of('/srv/nfs/a/b') == '/srv/nfs'
    assert policy.mount_point_of('/srv/nfs/local/x') == '/srv/nfs/local'
    assert policy.mount_point_of('/') == '/'
    assert policy.refusal_for('/srv/nfs/local/x') is None and policy.refusal_for('/srv/nfs/a')


def test_cluster_filesystems_count_as_network(disk_usage):
    for fstype in ('lustre', 'gpfs', 'beegfs', 'glusterfs'):
        policy = disk_usage.ScanPolicy('linux', mounts=[_mount('x', '/scratch', fstype)])
        assert '/scratch' in policy.never_enter, fstype


# --- automounts -------------------------------------------------------------

@posix_only
def test_triggered_automount_is_the_filesystem_on_top(disk_usage, tmp_path, monkeypatch):
    root = os.path.realpath(str(tmp_path))
    _write(root + '/srv/data/x', 50)
    _write(root + '/srv/other/y', 5)
    mounts = [_mount('/dev/sda2', '/', 'ext4'), _mount('systemd-1', root + '/srv/data', 'autofs'),
              _mount('/dev/sdb1', root + '/srv/data', 'ext4')]
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounts, fstab=[])
    assert root + '/srv/data' not in policy.never_enter
    with disk_usage.scan_policy(policy):
        found = disk_usage.discover_folders(root + '/srv', 'linux', validate_volume=False)
        assert sorted(os.path.basename(f) for f in found) == ['data', 'other']
        assert disk_usage.measure_folder_recursive(root + '/srv/data')[0] == 50
    crossing = disk_usage.ScanPolicy('linux', one_filesystem=False, mounts=mounts, fstab=[])
    assert disk_usage.measure_folder_recursive(root + '/srv', policy=crossing)[0] == 55
    # Volume discovery agrees: the ext4 on top is a volume
    monkeypatch.setattr(disk_usage, '_device_of', {'/': 1, root + '/srv/data': 2}.get)
    assert disk_usage.discover_linux_volumes(mounts) == ['/', root + '/srv/data']


def test_hidden_mount_is_judged_by_the_one_on_top(disk_usage, monkeypatch):
    mounts = [_mount('/dev/sdb1', '/mnt/x', 'ext4'), _mount('tmpfs', '/mnt/x', 'tmpfs')]
    monkeypatch.setattr(disk_usage, '_device_of', lambda path: 9)
    assert disk_usage.discover_linux_volumes(mounts) == []
    assert '/mnt/x' in disk_usage.ScanPolicy('linux', mounts=mounts).never_enter


@posix_only
def test_untriggered_automount_follows_fstab(disk_usage, tmp_path, monkeypatch, caplog):
    root = os.path.realpath(str(tmp_path))
    for name in ('usb', 'nfs', 'mystery', 'plain'):
        _write(f'{root}/mnt/{name}/f', 10)
    mounts = [_mount('/dev/sda2', '/', 'ext4')] + [
        _mount('systemd-1', f'{root}/mnt/{name}', 'autofs') for name in ('usb', 'nfs', 'mystery')]
    fstab = disk_usage.parse_mounts(
        f'# comment line\nUUID=1 {root}/mnt/usb ext4 nofail,x-systemd.automount 0 2\n'
        f'nas:/e {root}/mnt/nfs nfs4 x-systemd.automount 0 0\n')
    triggered = []
    monkeypatch.setattr(disk_usage, '_trigger_automount', triggered.append)
    policy = disk_usage.ScanPolicy('linux', one_filesystem=True, mounts=mounts, fstab=fstab)
    assert policy.automounts == {f'{root}/mnt/usb'}
    with disk_usage.scan_policy(policy), caplog.at_level(logging.INFO):
        found = disk_usage.discover_folders(root + '/mnt', 'linux', validate_volume=False)
        assert sorted(os.path.basename(f) for f in found) == ['plain', 'usb']
        assert disk_usage.measure_folder_recursive(f'{root}/mnt/usb')[0] == 10
        with pytest.raises(OSError, match='no /etc/fstab entry'):
            disk_usage.measure_folder_recursive(f'{root}/mnt/mystery')
    assert triggered == [f'{root}/mnt/usb']           # mounted before its st_dev is read
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('mystery' in w and 'automount' in w for w in warnings)
    assert any('nfs' in w and 'include_network_mounts' in w for w in warnings)
    with_net = disk_usage.ScanPolicy('linux', mounts=mounts, fstab=fstab, include_network=True)
    assert with_net.automounts == {f'{root}/mnt/usb', f'{root}/mnt/nfs'}


def test_fstab_is_only_read_for_automounts(disk_usage, monkeypatch):
    monkeypatch.setattr(disk_usage, 'read_fstab', lambda path='/etc/fstab': pytest.fail('read fstab'))
    disk_usage.ScanPolicy('linux', mounts=[_mount('a', '/', 'ext4')])


# --- volume metadata ----------------------------------------------------------

@posix_only
def test_disk_message_lists_its_volumes(tmp_path, collector, config, monkeypatch):
    deep = tmp_path / 'mnt' / 'data'          # a volume two levels deep, as /mnt/data
    _write(str(deep / 'x'), 10)
    config.update({'platform': 'linux', 'volumes': [str(deep), str(tmp_path / 'missing')],
                   'scan_paths': []})
    captured = _capture_disk_message(collector, monkeypatch)
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    assert captured[0]['data']['_volumes'] == [str(deep)]     # only measured volumes


# --- queue files ----------------------------------------------------------------

@posix_only
def test_symlinked_queue_is_moved_aside_unread(local_queue, tmp_path, caplog):
    secret_dir = tmp_path / 'private'
    secret_dir.mkdir()
    secret = secret_dir / 'secret.json'
    secret.write_text(json.dumps([{'header': {'msg_id': 'x'}, 'api_key': 'TOP-SECRET-1'}, 'TOP-SECRET-2']))
    qdir = tmp_path / 'data' / 'box'
    qdir.mkdir(parents=True)
    queue = qdir / 'queue.json'
    os.symlink(secret, queue)
    with caplog.at_level(logging.ERROR):
        assert local_queue.enqueue(str(queue), [{'header': {'msg_id': 'new'}, 'data': {}}]) == 1
    assert not queue.is_symlink() and json.loads(queue.read_text())[0]['header']['msg_id'] == 'new'
    for path in qdir.iterdir():
        if path.is_file() and not path.is_symlink():
            assert 'TOP-SECRET' not in path.read_text(), path
    aside = [p for p in qdir.iterdir() if p.name.startswith('queue.untrusted-')]
    assert len(aside) == 1 and aside[0].is_symlink()       # the link itself, not a copy
    assert 'symbolic link' in caplog.text
    assert json.loads(secret.read_text())[1] == 'TOP-SECRET-2'     # target untouched


@posix_only
def test_capability_holder_reads_only_its_own_queue(local_queue, tmp_path, monkeypatch):
    queue = tmp_path / 'queue.json'
    queue.write_text(json.dumps([{'header': {'msg_id': 'a'}, 'data': {}}]))
    st = os.stat(queue)
    monkeypatch.setattr(local_queue, '_holds_capabilities', lambda: True)
    assert len(local_queue.snapshot(str(queue))) == 1                 # owned by this user: read
    monkeypatch.setattr(local_queue.os, 'geteuid', lambda: st.st_uid + 1)
    assert local_queue.snapshot(str(queue)) == []                     # someone else's: refused
    assert not queue.exists()
    assert len([p for p in tmp_path.iterdir() if p.name.startswith('queue.untrusted-')]) == 1


@posix_only
def test_fifo_in_place_of_the_queue_does_not_block(local_queue, tmp_path):
    queue = tmp_path / 'queue.json'
    os.mkfifo(queue)
    assert local_queue.snapshot(str(queue)) == []
    assert not queue.exists()


@linux_only
def test_holds_capabilities_is_false_for_a_plain_user(local_queue):
    if os.geteuid() == 0:
        pytest.skip('root')
    with open('/proc/self/status') as f:
        cap_eff = int(next(l for l in f if l.startswith('CapEff:')).split()[1], 16)
    assert local_queue._holds_capabilities() is (cap_eff != 0)


@posix_only
def test_lock_and_archive_never_follow_symlinks(local_queue, collector, tmp_path, caplog):
    target = tmp_path / 'elsewhere'
    target.write_text('keep')
    queue = tmp_path / 'queue.json'
    os.symlink(target, str(queue) + '.lock')
    local_queue.enqueue(str(queue), [{'header': {'msg_id': 'a'}, 'data': {}}])
    assert not os.path.islink(str(queue) + '.lock') and target.read_text() == 'keep'
    archive = tmp_path / '2026-11.jsonl'
    os.symlink(target, archive)
    assert collector.append_to_archive(str(archive), {'a': 1}, logging.getLogger('test')) is False
    assert target.read_text() == 'keep'
    plain = tmp_path / 'plain.jsonl'
    assert collector.append_to_archive(str(plain), {'a': 1}, logging.getLogger('test')) is True
    assert collector.append_to_archive(str(plain), {'b': 2}, logging.getLogger('test')) is True
    assert [json.loads(l) for l in plain.read_text().splitlines()] == [{'a': 1}, {'b': 2}]


# --- units: user site -----------------------------------------------------------

@posix_only
def test_user_site_is_off_in_units(tmp_path):
    """What -s in ExecStart prevents: a .pth under $HOME/.local running in a system-site-packages venv."""
    home = tmp_path / 'home'
    site_dir = home / '.local' / 'lib' / f'python{sys.version_info[0]}.{sys.version_info[1]}' / 'site-packages'
    site_dir.mkdir(parents=True)
    (site_dir / 'evil.pth').write_text('import sys; sys.stderr.write("PTH-RAN\\n")\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
    env['HOME'] = str(home)
    code = 'import site; print(site.ENABLE_USER_SITE)'
    plain = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)
    if 'PTH-RAN' not in plain.stderr:
        pytest.skip('user site disabled for this interpreter anyway (a venv without system site-packages)')
    hardened = subprocess.run([sys.executable, '-s', '-c', code], capture_output=True, text=True, env=env)
    assert 'PTH-RAN' not in hardened.stderr and hardened.stdout.strip() == 'False'
    for mode in ('metrics', 'disk'):
        assert ' -s ' in _unit(f'lab-monitor-{mode}.service')['Service']['ExecStart'][0]


# --- installer ------------------------------------------------------------------

@needs_bash
@pytest.mark.parametrize('config', [
    {'timeout_seconds': 3600, 'total_timeout_seconds': '36000', 'scan_paths': ['/a/*', '/b/*']},
    {'timeout_seconds': ' 600 ', 'scan_paths': ['/a'] * 5},
    {'timeout_seconds': '0.5', 'total_timeout_seconds': [1]},
    {'total_timeout_seconds': 'nan'},
    {'total_timeout_seconds': 1e400},
])
def test_disk_unit_timeout_reads_numbers_like_the_collector(collector, tmp_path, config):
    cfg = tmp_path / 'c.json'
    cfg.write_text(json.dumps(config))
    proc = run_sourced(f'disk_timeout_seconds "{cfg}"')
    assert proc.returncode == 0, proc.stderr
    timeout = collector._cfg_number(config, 'timeout_seconds', collector.DEFAULT_TIMEOUT_SECONDS, minimum=1)
    entries = len(config.get('scan_paths') or config.get('volumes') or [])
    default = collector._default_total_timeout(timeout, entries, windows=False)
    budget = collector._cfg_number(config, 'total_timeout_seconds', default, minimum=1)
    if budget != budget or budget == float('inf'):
        budget = default          # the collector takes nan/inf; the unit gets the default
    assert int(proc.stdout) == int(budget) + 3600


APT_STUB = r'''
calls=0
apt-get() {
    calls=$((calls + 1))
    echo "apt-get $*" >>"$LOG"
    if (( calls <= FAILS )); then
        echo "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 42 (apt-get)" >&2
        return 100
    fi
    [[ $FINAL == ok ]] && return 0
    echo "E: Unable to locate package nope" >&2
    return 100
}
sleep() { echo "sleep $*" >>"$LOG"; }
APT_RETRY_INTERVAL=7
'''


@needs_bash
@pytest.mark.parametrize('fails,final,wait,rc,calls', [
    (2, 'ok', 300, 0, 3),          # waits out the lock
    (0, 'bad', 300, 100, 1),       # a real error is not retried
    (5, 'ok', 0, 100, 1),          # no time left: gives up with the lock error
])
def test_apt_get_waits_for_the_package_lock(tmp_path, fails, final, wait, rc, calls):
    log = tmp_path / 'log'
    proc = run_sourced(APT_STUB + f'APT_LOCK_WAIT={wait}\nrc=0; apt_get update || rc=$?; echo "rc=$rc"',
                       env={'LOG': str(log), 'FAILS': str(fails), 'FINAL': final})
    assert f'rc={rc}' in proc.stdout, proc.stdout + proc.stderr
    lines = log.read_text().splitlines()
    assert [l for l in lines if l.startswith('apt-get')] == [
        f'apt-get -o DPkg::Lock::Timeout={wait} update'] * calls
    assert lines.count('sleep 7') == calls - 1
    if fails and calls > 1:
        assert 'waiting up to' in proc.stdout


def test_install_lock_is_in_a_root_only_directory():
    with open(INSTALLER) as f:
        text = f.read()
    lock = re.search(r'^INSTALL_LOCK=(\S+)$', text, re.M).group(1)
    assert os.path.dirname(lock) == '/run'
    assert '/run/lock' not in re.sub(r'#.*', '', text)
    # install and uninstall both take it
    assert text.count('take_install_lock\n') >= 2


# --- installer: NVIDIA GPU servers --------------------------------------------

PROC_NVIDIA_VERSION = ('NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  535.183.01  Release Build  '
                       '(dvs-builder@U16-I3-B03-4-3)  Tue May 14 13:28:42 UTC 2024\nGCC version:  gcc 12.3.0\n')
LDCONFIG_CACHE = ('1234 libs found in cache `/etc/ld.so.cache\'\n'
                  '\tlibnvidia-ml.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libnvidia-ml.so.1\n'
                  '\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6\n')


def _gpu_host(tmp_path, proc_version=False, ldconfig_nvml=False, nvidia_smi=False):
    """
    Shell lines that make the installer see a host with the given signs of an
    NVIDIA driver: a PATH with only the tools it needs (a fake ldconfig, and a
    fake nvidia-smi if asked), and NVIDIA_PROC_VERSION in tmp_path.
    """
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    for tool in ('head', 'awk', 'grep', 'tail', 'sed', 'stat', 'cat', 'mktemp', 'rm', 'dirname'):
        target = shutil.which(tool)
        if target and not (bindir / tool).exists():
            os.symlink(target, bindir / tool)
    cache = tmp_path / 'ld.so.cache.txt'
    cache.write_text(LDCONFIG_CACHE if ldconfig_nvml else LDCONFIG_CACHE.replace('libnvidia-ml', 'libother'))
    (bindir / 'ldconfig').write_text(f'#!/bin/sh\n[ "$1" = -p ] && cat "{cache}"\n')
    (bindir / 'ldconfig').chmod(0o755)
    if nvidia_smi:
        (bindir / 'nvidia-smi').write_text('#!/bin/sh\nexit 0\n')
        (bindir / 'nvidia-smi').chmod(0o755)
    version = tmp_path / 'nvidia-version'
    if proc_version:
        version.write_text(PROC_NVIDIA_VERSION)
    return f'PATH="{bindir}"\nNVIDIA_PROC_VERSION="{version}"\n'


@needs_bash
@pytest.mark.parametrize('signs,expected', [
    ({}, []),
    ({'proc_version': True}, ['kernel driver 535.183.01 loaded (']),
    ({'ldconfig_nvml': True}, ['NVML library /lib/x86_64-linux-gnu/libnvidia-ml.so.1 (ldconfig -p)']),
    ({'nvidia_smi': True}, ['nvidia-smi (']),
    ({'proc_version': True, 'ldconfig_nvml': True, 'nvidia_smi': True},
     ['kernel driver 535.183.01 loaded (', 'NVML library ', 'nvidia-smi (']),
])
def test_installer_detects_an_nvidia_driver_by_any_sign(tmp_path, signs, expected):
    proc = run_sourced(_gpu_host(tmp_path, **signs) + 'decide_gpu_support 0\necho "wanted=$GPU_WANTED"')
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f'wanted={int(bool(expected))}' in out
    found = [l.strip()[2:] for l in out.splitlines() if l.startswith('  - ')]
    assert len(found) == len(expected) and all(f.startswith(e) for f, e in zip(found, expected)), out
    if not expected:
        assert 'No NVIDIA driver detected' in out


@needs_bash
def test_installer_keeps_gpu_support_on_upgrade_without_a_visible_driver(tmp_path):
    proc = run_sourced(_gpu_host(tmp_path) + 'decide_gpu_support 1\necho "wanted=$GPU_WANTED"')
    assert 'wanted=1' in proc.stdout and 'installed by an earlier run' in proc.stdout, proc.stdout


FAKE_VENV_PYTHON = '''#!/bin/bash
echo "$*" >>"$LOG"
case "$*" in
    *"import pynvml"*) exit "$HAD_PYNVML" ;;
esac
exit 0
'''


@needs_bash
@pytest.mark.parametrize('had_pynvml,signs,gpu_installed', [
    (False, {}, False),                          # no GPU: nothing extra
    (True, {}, True),                            # upgrade: kept although no driver is visible now
    (False, {'proc_version': True}, True),       # kernel module loaded, no nvidia-smi on PATH
    (False, {'ldconfig_nvml': True}, True),
])
def test_setup_venv_installs_requirements_gpu(tmp_path, had_pynvml, signs, gpu_installed):
    venv, code, log = tmp_path / 'venv', tmp_path / 'code', tmp_path / 'log'
    (venv / 'bin').mkdir(parents=True)
    code.mkdir()
    (venv / 'bin' / 'python').write_text(FAKE_VENV_PYTHON)
    (venv / 'bin' / 'python').chmod(0o755)
    script = (_gpu_host(tmp_path, **signs) +
              f'VENV_DIR="{venv}"\nCODE_DIR="{code}"\n'
              'venv_healthy() { return 0; }\n'
              'check_nvml_access() { echo "nvml-check wanted=$GPU_WANTED"; }\n'
              'setup_venv')
    proc = run_sourced(script, env={'LOG': str(log), 'HAD_PYNVML': '0' if had_pynvml else '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = log.read_text()
    assert f'-r {code}/requirements.txt' in calls
    assert (f'-r {code}/requirements-gpu.txt' in calls) is gpu_installed, calls
    assert f'nvml-check wanted={int(gpu_installed)}' in proc.stdout


FAKE_PYNVML = '''
import os
class NVMLError(Exception):
    pass
def nvmlInit():
    if os.environ.get("NVML_FAIL"):
        raise NVMLError(os.environ["NVML_FAIL"])
def nvmlShutdown():
    pass
def nvmlDeviceGetCount():
    return 2
def nvmlDeviceGetHandleByIndex(i):
    return i
def nvmlDeviceGetName(h):
    return ["NVIDIA RTX A6000", b"Tesla T4"][h]
'''


@needs_bash
@pytest.mark.parametrize('fail,expected', [
    (None, (0, '2 GPU(s): NVIDIA RTX A6000, Tesla T4')),
    ('Driver Not Loaded', (4, 'nvmlInit failed: Driver Not Loaded')),
])
def test_nvml_probe_is_what_the_collector_does(tmp_path, fail, expected):
    probe = run_sourced('printf "%s" "$NVML_PROBE"').stdout
    mod = tmp_path / 'mods'
    mod.mkdir()
    (mod / 'pynvml.py').write_text(FAKE_PYNVML)
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
    if fail:
        env['NVML_FAIL'] = fail
    proc = subprocess.run([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(mod)!r})\n' + probe],
                          capture_output=True, text=True, env=env)
    assert (proc.returncode, proc.stdout.strip()) == expected, proc.stderr


@needs_bash
@pytest.mark.parametrize('message,device,expected', [
    ('nvmlInit failed: RM has detected an NVML/RM version mismatch.', None, 'without a reboot'),
    ('nvmlInit failed: Driver/library version mismatch', None, 'without a reboot'),
    ('nvmlInit failed: NVML Shared Library Not Found', None, 'libnvidia-compute-<driver version>'),
    ('nvmlInit failed: Insufficient Permissions', 0o660, 'NVreg_DeviceFileMode'),
    ('nvmlInit failed: Driver Not Loaded', None, 'nvidia-persistenced'),
    ('import pynvml failed: No module named pynvml', None, 'PyPI'),
])
def test_nvml_failure_hints(tmp_path, message, device, expected):
    dev = tmp_path / 'dev'
    dev.mkdir()
    if device is not None:
        (dev / 'nvidiactl').touch()
        (dev / 'nvidiactl').chmod(device)
        (dev / 'nvidia0').touch()
    proc = run_sourced(f'NVIDIA_DEV_DIR="{dev}"\nnvml_failure_hint "{message}"')
    assert proc.returncode == 0 and expected in proc.stdout, proc.stdout + proc.stderr
    if device is not None:
        assert f'-rw-rw---- ' in proc.stdout and f'{dev}/nvidia0' in proc.stdout


@needs_bash
@pytest.mark.parametrize('probe_out,rc,expected', [
    ('2 GPU(s): NVIDIA RTX A6000, Tesla T4', 0, '[OK] NVML works as labmon: 2 GPU(s)'),
    ('nvmlInit failed: Insufficient Permissions', 4, 'GPU stats will be missing: as labmon, nvmlInit failed'),
])
def test_check_nvml_access_reports_the_result(tmp_path, probe_out, rc, expected):
    script = ('GPU_WANTED=1\n'
              f'run_as_service_user() {{ echo "{probe_out}"; return {rc}; }}\n'
              f'NVIDIA_DEV_DIR="{tmp_path}"\ncheck_nvml_access')
    proc = run_sourced(script)
    assert proc.returncode == 0
    assert expected in proc.stdout + proc.stderr, proc.stdout + proc.stderr


@needs_bash
@pytest.mark.parametrize('log,expected', [
    ('2026-11-21 10:00:01 - INFO - CPU: 4.0% | RAM: 20% | Uptime: 1d 1h | Network: {} | '
     'GPUs: 2 (NVIDIA RTX A6000 37%, Tesla T4 n/a)\nmore', 'GPUs: 2 (NVIDIA RTX A6000 37%, Tesla T4 n/a)'),
    ('... | GPUs: none/unavailable', 'GPUs: none/unavailable'),
    ('no summary line', ''),
])
def test_test_run_reads_the_gpu_summary_from_the_journal(log, expected):
    proc = run_sourced(f"gpu_summary_of {shlex.quote(log)}")
    assert proc.returncode == 0 and proc.stdout.strip() == expected, proc.stdout + proc.stderr


@needs_bash
@pytest.mark.parametrize('journal,expected', [
    ('CPU: 4.0% | RAM: 20% | GPUs: 2 (NVIDIA RTX A6000 37%, Tesla T4 n/a)',
     '[OK] GPU stats collected by the service (GPUs: 2 (NVIDIA RTX A6000 37%, Tesla T4 n/a))'),
    ('WARNING - NVIDIA GPU stats unavailable: NVML could not start (Driver Not Loaded (NVML error 9)).\n'
     'CPU: 4.0% | RAM: 20% | GPUs: none/unavailable',
     'reported no GPUs (GPUs: none/unavailable)'),
])
def test_test_run_checks_that_the_service_saw_the_gpus(tmp_path, journal, expected):
    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin' / 'python').write_text('#!/bin/sh\nexit 0\n')
    (venv / 'bin' / 'python').chmod(0o755)
    script = (f'VENV_DIR="{venv}"\nGPU_WANTED=1\n'
              'config_get() { echo http://manager.invalid:5000; }\n'
              'queue_length() { echo 0; }\n'
              'systemctl() { [[ $1 == show ]] && echo success; return 0; }\n'
              f'journalctl() {{ printf "%s\\n" {shlex.quote(journal)}; }}\n'
              'test_run')
    proc = run_sourced(script)
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout + proc.stderr, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Network limits: disk results are only queued; metrics may reach the Manager only
# ---------------------------------------------------------------------------

def test_disk_no_sync_queues_without_sending(tmp_path, collector, config, monkeypatch):
    def must_not_send(*a, **k):
        raise AssertionError('--no-sync must not contact the Manager')
    monkeypatch.setattr(collector, 'sync_queue_to_manager', must_not_send)
    _write(str(tmp_path / 'tree' / 'alice' / 'a'), 1000)
    config.update({'platform': 'linux', 'volumes': [str(tmp_path)],
                   'scan_paths': [str(tmp_path / 'tree') + '/*']})
    assert collector.collect_disk_usage(config, logging.getLogger('test'), sync=False) is True
    queue = tmp_path / 'data' / 'testbox' / 'queue.json'
    entries = json.loads(queue.read_text())
    # Folder sizes, plus the storage breakdown (owners) when that is on: all queued, none sent
    types = [e['data']['data_type'] for e in entries]
    assert 'folder_usage' in types and set(types) <= {'folder_usage', 'storage_breakdown'}


def test_disk_sync_is_the_default(tmp_path, collector, config, monkeypatch):
    sent = []
    monkeypatch.setattr(collector, 'sync_queue_to_manager', lambda *a: sent.append(a) or True)
    config.update({'platform': 'linux', 'volumes': [str(tmp_path)], 'scan_paths': []})
    assert collector.collect_disk_usage(config, logging.getLogger('test')) is True
    assert len(sent) == 1


def test_no_sync_flag_reaches_disk_mode(collector, monkeypatch, tmp_path, config):
    cfg = tmp_path / 'c.json'
    cfg.write_text(json.dumps(config))
    seen = {}
    monkeypatch.setattr(collector, 'collect_disk_usage',
                        lambda c, log, dry_run=False, sync=True: seen.setdefault('sync', sync))
    monkeypatch.setattr(sys, 'argv', ['collector.py', '--config', str(cfg), '--mode', 'disk', '--no-sync'])
    with pytest.raises(SystemExit):
        collector.main()
    assert seen == {'sync': False}


@needs_bash
def test_name_servers_from_resolv_conf(tmp_path):
    conf = tmp_path / 'resolv.conf'
    conf.write_text('# comment\nnameserver 127.0.0.53\nnameserver fe80::1%eth0\noptions edns0\n'
                    'search example.org\nnameserver 10.0.0.2\n')
    proc = run_sourced(f'name_servers "{conf.as_posix()}"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ['10.0.0.2', '127.0.0.53', 'fe80::1']


@needs_bash
@pytest.mark.parametrize('url,expected', [
    ('http://127.0.0.1:5000', ['127.0.0.1']),
    ('https://10.1.2.3/', ['10.1.2.3']),
    ('http://name.invalid:5000', []),
    ('', []),
])
def test_manager_addresses(url, expected):
    if shutil.which('getent') is None and expected:
        pytest.skip('needs getent')
    proc = run_sourced(f'manager_addresses {shlex.quote(url)}')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == expected


@needs_bash
def test_metrics_network_dropin_allows_the_manager_and_name_servers():
    if shutil.which('getent') is None:
        pytest.skip('needs getent')
    proc = run_sourced('name_servers() { echo 10.0.0.2; }\nmetrics_network_dropin http://127.0.0.1:5000')
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l and not l.startswith('#')]
    assert lines == ['[Service]', 'IPAddressAllow=127.0.0.1', 'IPAddressAllow=10.0.0.2']


@needs_bash
def test_metrics_network_dropin_fails_when_the_manager_does_not_resolve():
    proc = run_sourced('metrics_network_dropin http://name.invalid:5000; echo "rc=$?"')
    assert 'rc=1' in proc.stdout and 'IPAddressAllow' not in proc.stdout


@needs_bash
def test_no_network_limit_resets_the_deny_list():
    proc = run_sourced('OPT_NO_NETWORK_LIMIT=1\nmetrics_network_dropin http://name.invalid:5000')
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l and not l.startswith('#')]
    assert lines == ['[Service]', 'IPAddressDeny=']


@needs_bash
def test_installer_has_no_network_limit_flag_and_uninstall_removes_the_dropin():
    assert '--no-network-limit' in run_installer('--help', check=True).stdout
    with open(INSTALLER) as f:
        text = f.read()
    assert 'METRICS_NET_DROPIN="$METRICS_DROPIN_DIR/50-network.conf"' in text
    assert 'rm -f -- "$DISK_DROPIN" "$METRICS_NET_DROPIN"' in text
