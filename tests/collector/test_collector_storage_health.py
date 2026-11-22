"""lib/storage_health.py: Tier A readers (md, sysfs, mounts, hwmon), Windows PowerShell, classify, sending."""
import json
import logging
import os
import shutil
import subprocess

import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32',
                                reason='needs a POSIX host (os.getuid / os.sysconf / /proc / POSIX paths)')


LOG = logging.getLogger('test')


@pytest.fixture
def health(collector):
    from lib import storage_health as module
    return module


class Tree:
    """A fake /sys and /proc under tmp_path for the storage-health readers."""

    def __init__(self, root):
        self.sys, self.proc = str(root / 'sys'), str(root / 'proc')
        os.makedirs(os.path.join(self.sys, 'block'))
        os.makedirs(self.proc)

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def disk(self, name, model='WD80EFZX', state='running', ioerr='0x0', rotational=1, size=15628053168,
             partitions=(), serial=None):
        d = os.path.join(self.sys, 'block', name)
        self.put(os.path.join(d, 'size'), f'{size}\n')
        self.put(os.path.join(d, 'queue', 'rotational'), f'{rotational}\n')
        os.makedirs(os.path.join(d, 'device'), exist_ok=True)
        for key, value in (('model', model), ('state', state), ('ioerr_cnt', ioerr), ('serial', serial)):
            if value is not None:
                self.put(os.path.join(d, 'device', key), f'{value}\n')
        for part in partitions:
            os.makedirs(os.path.join(d, part), exist_ok=True)
        return self

    def md(self, name, level, state='clean', raid_disks=4, degraded=0, action='idle', completed='none',
           speed='none', members=None, size=23437500000):
        d = os.path.join(self.sys, 'block', name)
        self.put(os.path.join(d, 'size'), f'{size}\n')
        md = os.path.join(d, 'md')
        for key, value in (('array_state', state), ('level', level), ('raid_disks', raid_disks),
                           ('degraded', degraded), ('sync_action', action), ('sync_completed', completed),
                           ('sync_speed', speed), ('mismatch_cnt', 0)):
            self.put(os.path.join(md, key), f'{value}\n')
        for member, flags in (members or {}).items():
            self.put(os.path.join(md, f'dev-{member}', 'state'), f'{flags}\n')
        return self

    def mounts(self, text):
        self.put(os.path.join(self.proc, 'mounts'), text)
        return self

    def hwmon(self, index, name, millideg, disk=None, nvme=None):
        h = os.path.join(self.sys, 'class', 'hwmon', f'hwmon{index}')
        self.put(os.path.join(h, 'name'), f'{name}\n')
        self.put(os.path.join(h, 'temp1_input'), f'{millideg}\n')
        if disk:
            os.makedirs(os.path.join(h, 'device', 'block', disk), exist_ok=True)
        if nvme:
            os.makedirs(os.path.join(h, 'device', nvme), exist_ok=True)
        return self


@pytest.fixture
def tree(tmp_path, health, monkeypatch):
    t = Tree(tmp_path)
    monkeypatch.setattr(health, 'SYS_ROOT', t.sys)
    monkeypatch.setattr(health, 'PROC_ROOT', t.proc)
    monkeypatch.setattr(health, 'FSTAB', str(tmp_path / 'fstab'))
    monkeypatch.setattr(health.shutil, 'which', lambda name: None)       # no zpool
    t.mounts('/dev/md2 /volume1 btrfs rw,relatime 0 0\n')
    return t


def _classify(health, arrays=(), disks=(), volumes=(), platform='linux', baselines=None, now=1000.0, **kw):
    arrays, disks, volumes = list(arrays), list(disks), list(volumes)
    temps = {'hdd': [50, 60], 'ssd': [70, 80]}
    overall, reasons, _ = health.classify(arrays, disks, volumes, platform, temps,
                                          {} if baselines is None else baselines, now, **kw)
    return overall, reasons, arrays, disks, volumes


# ---------------------------------------------------------------------------
# md
# ---------------------------------------------------------------------------

def _md(health, tree, **kw):
    tree.md('md2', **kw)
    status, arrays = health.read_md()
    assert status == 'ok'
    for a in arrays:
        a.setdefault('role', 'data')
    return arrays


def test_healthy_raid5(health, tree):
    for n in ('sata1', 'sata2', 'sata3', 'sata4'):
        tree.disk(n, partitions=(f'{n}p5',))
    arrays = _md(health, tree, level='raid5', members={f'sata{i}p5': 'in_sync' for i in range(1, 5)})
    a = arrays[0]
    assert (a['members'], a['members_active'], a['degraded'], a['size_bytes']) == (
        ['sata1', 'sata2', 'sata3', 'sata4'], 4, 0, 23437500000 * 512)      # partitions mapped to their disks
    assert _classify(health, arrays)[:2] == ('ok', [])


def test_raid1_with_one_member_left_is_critical(health, tree):
    arrays = _md(health, tree, level='raid1', raid_disks=2, degraded=1, members={'sda1': 'in_sync'})
    overall, reasons, *_ = _classify(health, arrays)
    assert overall == 'critical' and reasons == ['md2 degraded with no redundancy left: 1 of 2 members active']


def test_recovery_is_a_warning_with_progress_and_eta(health, tree):
    arrays = _md(health, tree, level='raid5', degraded=1, action='recover', completed='1464342528 / 3904999936',
                 speed='120000', members={'sda3': 'in_sync', 'sdb3': 'in_sync', 'sdc3': 'in_sync', 'sdd3': 'spare'})
    a = arrays[0]
    assert a['sync_percent'] == 37.5 and a['sync_speed_kbps'] == 120000 and a['members_active'] == 3
    assert a['sync_eta_seconds'] == 10169            # 2440657408 sectors left at 120000 KiB/s
    overall, reasons, *_ = _classify(health, arrays)
    assert overall == 'warning' and reasons == ['md2 rebuilding 37.5% (ETA 2h 49m)']
    assert health._duration(30) == '<1m' and health._duration(600) == '10m'


def test_resync_check_inactive_raid6_and_faulty(health, tree):
    assert _classify(health, _md(health, tree, level='raid1', raid_disks=2, action='resync', completed='1/4'))[0] == 'warning'
    overall, reasons, *_ = _classify(health, _md(health, tree, level='raid5', action='check', completed='1/2'))
    assert overall == 'ok' and reasons == ['md2 data scrub (check) 50.0%']
    assert _classify(health, _md(health, tree, level='raid5', state='inactive'))[0] == 'critical'
    assert _classify(health, _md(health, tree, level='raid6', degraded=1))[0] == 'warning'
    assert _classify(health, _md(health, tree, level='raid6', degraded=2))[0] == 'critical'
    arrays = _md(health, tree, level='raid5', members={'sda3': 'faulty', 'sdb3': 'in_sync', 'sdc3': 'in_sync'})
    overall, reasons, *_ = _classify(health, arrays)
    assert overall == 'warning' and arrays[0]['faulty_members'] == ['sda3'] and 'faulty' in reasons[0]


def test_imsm_container_is_not_an_array(health, tree):
    tree.md('md127', level='container', state='inactive')
    assert health.read_md() == ('ok', [])


MDSTAT = """Personalities : [raid1] [raid6] [raid5] [raid4]
md2 : active raid5 sata1p5[0] sata2p5[1] sata4p5[3] sata3p5[4]
      11995903803392 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/3] [UU_U]
      [=======>.............]  recovery = 37.5% (1464342528/3904999936) finish=312.0min speed=120000K/sec

md1 : active raid1 sata1p2[0] sata2p2[1] sata4p2[3]
      2097088 blocks [4/3] [UU_U]

md0 : active raid1 sata1p1[0] sata2p1[1] sata3p1[2](F) sata4p1[3]
      2490176 blocks [4/3] [UUU_]

md127 : inactive sdb[1](S) sda[0](S)
      5040 blocks super external:imsm

unused devices: <none>
"""


def test_mdstat_fallback_and_dsm_system_arrays(health, tree, monkeypatch):
    tree.put(os.path.join(tree.proc, 'mdstat'), MDSTAT)
    monkeypatch.setattr(health, 'SYS_ROOT', os.path.join(tree.sys, 'nothing'))     # sysfs unreadable
    status, arrays = health.read_md()
    assert status == 'ok' and [a['id'] for a in arrays] == ['md2', 'md1', 'md0']
    md2 = arrays[0]
    assert (md2['level'], md2['members_total'], md2['members_active'], md2['degraded'], md2['sync_action'],
            md2['sync_percent'], md2['sync_eta_seconds'], md2['sync_speed_kbps']) == (
        'raid5', 4, 3, 1, 'recover', 37.5, 18720, 120000)
    assert arrays[2]['faulty_members'] == ['sata3p1']
    for a in arrays:
        a['role'] = {'md0': 'system', 'md1': 'swap'}.get(a['id'], 'data')
    overall, reasons, arrays, *_ = _classify(health, arrays, platform='synology')
    # md0/md1 missing members are empty bays; a faulty member is a warning
    assert [a['status'] for a in arrays] == ['warning', 'ok', 'warning']
    assert reasons[0] == 'md2 rebuilding 37.5% (ETA 5h 12m)' and 'md0 (system) member sata3p1 faulty' in reasons


def test_no_md_driver_is_unavailable(health, tree):
    assert health.read_md() == ('unavailable', [])


# ---------------------------------------------------------------------------
# Disks, temperatures, volumes
# ---------------------------------------------------------------------------

def test_block_devices(health, tree):
    tree.disk('sata1', ioerr='0x1f', serial='WD-ABC').disk('nvme0n1', model='Samsung 990', rotational=0)
    tree.md('md2', level='raid1')
    os.makedirs(os.path.join(tree.sys, 'block', 'loop0'))                    # no device link: virtual
    status, disks = health.read_block_devices()
    assert status == 'ok' and [d['id'] for d in disks] == ['nvme0n1', 'sata1']
    sata = disks[1]
    assert (sata['model'], sata['serial'], sata['media'], sata['bus'], sata['io_errors'], sata['device_state'],
            sata['size_bytes'], sata['health_source']) == ('WD80EFZX', 'WD-ABC', 'hdd', 'sata', 31, 'running',
                                                         15628053168 * 512, 'sysfs')
    assert disks[0]['media'] == 'nvme' and disks[0]['bus'] == 'nvme'


def test_vpd_serial(health, tree):
    tree.disk('sda')
    with open(os.path.join(tree.sys, 'block', 'sda', 'device', 'vpd_pg80'), 'wb') as f:
        f.write(b'\x00\x80\x00\x08  ZL2ABC')
    assert health.read_block_devices()[1][0]['serial'] == 'ZL2ABC'


def test_disk_status_offline_temperature_and_io_errors(health):
    def disk(**kw):
        d = {f: None for f in health.DISK_FIELDS}
        d.update({'id': 'sda', 'media': 'hdd', 'health_source': 'sysfs', 'device_state': 'running', **kw})
        return d
    assert _classify(health, disks=[disk(device_state='offline')])[1] == ['sda offline']
    assert _classify(health, disks=[disk(temperature_c=55)])[:2] == ('warning', ['sda temperature 55 C (warning at 50)'])
    assert _classify(health, disks=[disk(temperature_c=61)])[0] == 'critical'
    assert _classify(health, disks=[disk(temperature_c=61, media='ssd')])[0] == 'ok'          # SSD: 70 / 80
    # ioerr_cnt is reported but never a status by itself: it also counts rejected probe commands
    baselines = {}
    for n in (5, 9, 2):
        assert _classify(health, disks=[disk(io_errors=n)], baselines=baselines)[0] == 'ok'
    nothing = {f: None for f in health.DISK_FIELDS}
    nothing.update(id='sdz', health_source='sysfs')
    assert _classify(health, disks=[nothing])[0] == 'unknown'


def test_hwmon(health, tree):
    tree.hwmon(0, 'coretemp', 50000).hwmon(1, 'drivetemp', 38000, disk='sda').hwmon(2, 'nvme', 45850, nvme='nvme0n1')
    assert health.read_hwmon() == ('ok', {'sda': 38.0, 'nvme0n1': 45.9})


def test_hwmon_without_drive_sensors(health, tree):
    assert health.read_hwmon() == ('unavailable', {})
    tree.hwmon(0, 'coretemp', 50000)
    assert health.read_hwmon() == ('unavailable', {})


@posix_only
def test_read_only_volumes(health, tree, tmp_path):
    tree.mounts('/dev/md2 /volume1 btrfs ro,relatime 0 0\n/dev/md3 /volume2 ext4 rw 0 0\n'
                '/dev/sdq1 /volumeUSB1/usbshare vfat rw 0 0\n')
    status, volumes = health.read_volumes({}, 'synology')
    assert status == 'ok' and [v['id'] for v in volumes] == ['/volume1', '/volume2']
    overall, reasons, *_ = _classify(health, volumes=volumes, platform='synology')
    assert overall == 'critical' and reasons == ['/volume1 mounted read-only']
    # Configured, but not a mount point on DSM: the volume isn't mounted
    status, volumes = health.read_volumes({'volumes': ['/volume3']}, 'synology')
    assert _classify(health, volumes=volumes, platform='synology')[1] == ['/volume3 not mounted']
    # Linux: read-only on purpose (fstab says ro) is fine; a configured folder is judged by its filesystem
    (tmp_path / 'fstab').write_text('UUID=1 /srv/archive xfs ro 0 0\n')
    tree.mounts('/dev/sda2 / ext4 rw 0 0\n/dev/sdb1 /srv/archive xfs ro 0 0\n')
    status, volumes = health.read_volumes({'volumes': ['/srv/archive', '/data/projects']}, 'linux')
    assert [(v['id'], v['fs'], v['read_only']) for v in volumes] == [('/srv/archive', 'xfs', True),
                                                                      ('/data/projects', 'ext4', False)]
    assert _classify(health, volumes=volumes)[:2] == ('ok', [])


def test_unreadable_mounts(health, tree):
    os.unlink(os.path.join(tree.proc, 'mounts'))
    assert health.read_volumes({}, 'linux') == ('unavailable', [])


def test_zpool(health):
    def run(args, **kw):
        assert args[1:] == ['list', '-H', '-o', 'name,health'] and kw['timeout'] == health.ZPOOL_TIMEOUT
        return subprocess.CompletedProcess(args, 0, stdout='tank\tONLINE\nbackup\tDEGRADED\nold\tFAULTED\n')
    status, pools = health.read_zpool(run, lambda n: '/sbin/zpool', LOG)
    assert status == 'ok' and [p['id'] for p in pools] == ['tank', 'backup', 'old']
    overall, reasons, pools, *_ = _classify(health, pools)
    assert [p['status'] for p in pools] == ['ok', 'warning', 'critical'] and overall == 'critical'
    assert health.read_zpool(run, lambda n: None, LOG) == (None, [])


def test_zpool_ids_are_cut_to_the_contract_limit(health):
    name = 'tank_' + 'x' * 95
    run = lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=f'{name}\tONLINE\n')
    status, pools = health.read_zpool(run, lambda n: '/sbin/zpool', LOG)
    assert pools[0]['id'] == name[:health.ID_MAX_CHARS] and len(pools[0]['id']) == 64      # R10
    assert pools[0]['name'] == name                                                         # <= 128


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

WIN_NAMES = {
    'disks_status': 'ok', 'spaces_status': 'ok', 'volumes_status': 'ok', 'events_status': 'ok',
    'disks': [
        {'DeviceId': '0', 'FriendlyName': 'Samsung SSD', 'SerialNumber': 'S1', 'MediaType': 'SSD', 'BusType': 'NVMe',
         'Size': 2000398934016, 'HealthStatus': 'Healthy', 'OperationalStatus': ['OK'], 'Temperature': 41,
         'TemperatureMax': 0, 'ReadErrorsUncorrected': 0, 'WriteErrorsUncorrected': 0, 'Wear': 3, 'PowerOnHours': 900},
        {'DeviceId': '1', 'FriendlyName': 'ST8000', 'SerialNumber': 'Z1', 'MediaType': 'HDD', 'BusType': 'SATA',
         'Size': 8001563222016, 'HealthStatus': 'Warning', 'OperationalStatus': ['Predictive Failure'],
         'Temperature': 47, 'TemperatureMax': 50, 'ReadErrorsUncorrected': 4, 'WriteErrorsUncorrected': 0,
         'Wear': None, 'PowerOnHours': 65535},
        {'DeviceId': '2', 'FriendlyName': 'PERC H730', 'SerialNumber': '', 'MediaType': 'Unspecified',
         'BusType': 'RAID', 'Size': 4000000000000, 'HealthStatus': 'Healthy', 'OperationalStatus': ['OK'],
         'Temperature': None, 'TemperatureMax': None},
    ],
    'virtual_disks': [{'FriendlyName': 'Pool-Mirror', 'HealthStatus': 'Warning', 'OperationalStatus': ['Degraded'],
                       'ResiliencySettingName': 'Mirror', 'Size': 4000000000000}],
    'jobs': [{'Name': 'Repair', 'JobState': 'Running', 'PercentComplete': 42}],
    'volumes': [{'DriveLetter': 'E', 'FileSystemType': 'NTFS', 'HealthStatus': 'Healthy', 'OperationalStatus': ['OK']},
                {'DriveLetter': 'F', 'FileSystemType': 'ReFS', 'HealthStatus': 'Unhealthy'}],
    'events': {'disk/7': 2, 'disk/153': 3, 'storahci/129': 40, 'ntfs/55': 0},
}
# The same as PowerShell prints the enums when [string] gives their numbers
WIN_NUMBERS = json.loads(json.dumps(WIN_NAMES).replace('"SSD"', '"4"').replace('"HDD"', '"3"').replace(
    '"NVMe"', '"17"').replace('"SATA"', '"11"').replace('"RAID"', '"8"').replace('"Healthy"', '"0"').replace(
    '"Warning"', '"1"').replace('"Unhealthy"', '"2"').replace('"Predictive Failure"', '"5"').replace(
    '"Running"', '"4"').replace('"Degraded"', '"3"'))


@pytest.mark.parametrize('obj', [WIN_NAMES, WIN_NUMBERS], ids=['names', 'numbers'])
def test_windows_components(health, obj):
    win = health.windows_components(obj)
    assert win['sources'] == {'windows_storage': 'ok', 'events': 'ok'}
    disks, arrays, volumes = win['disks'], win['arrays'], win['volumes']
    assert [(d['id'], d['media'], d['bus']) for d in disks] == [('pd0', 'nvme', 'nvme'), ('pd1', 'hdd', 'sata'),
                                                                ('pd2', None, 'raid')]
    assert disks[1]['smart_passed'] is False and disks[1]['power_on_hours'] == 65535 and disks[2]['temperature_c'] is None
    assert arrays[0]['sync_action'] == 'repair' and arrays[0]['sync_percent'] == 42 and arrays[0]['degraded'] == 1
    overall, reasons, arrays, disks, volumes = _classify(health, arrays, disks, volumes, platform='windows',
                                                         events=win['events'], event_hours=1.0)
    assert [d['status'] for d in disks] == ['ok', 'critical', 'ok']
    assert [v['status'] for v in volumes] == ['ok', 'critical'] and volumes[0]['id'] == 'E:'
    assert arrays[0]['status'] == 'warning' and overall == 'critical'
    assert reasons[0] in ('pd1 predictive failure', 'pd1 5') and 'F: health Unhealthy' in reasons
    assert 'disk event 7 x2' in reasons and 'storahci event 129 x40 (40/h)' in reasons
    assert not [r for r in reasons if 'event 153' in r]           # 3 an hour: below 10/h
    # TemperatureMax 50: warning from 45, critical at 50
    assert 'pd1 temperature 47 C (warning at 45)' in reasons


def test_windows_uncorrected_errors_rising(health):
    baselines = {}
    win = health.windows_components(WIN_NAMES)
    disk = dict(win['disks'][0])
    for value, now, expected in ((0, 0, 'ok'), (3, 300, 'warning'), (3, 600, 'warning'),
                                 (3, 300 + health.RISE_HOLD_SECONDS, 'ok'), (1, 90000, 'ok'), (2, 90300, 'warning')):
        disk['read_errors_uncorrected'] = value
        assert _classify(health, disks=[dict(disk)], baselines=baselines, now=now)[0] == expected, (value, now)


def test_powershell_output_parsing_and_failures(health, monkeypatch):
    out = 'WARNING: something\r\n' + json.dumps(WIN_NAMES) + '\r\n'
    assert health.parse_powershell_output(out.encode('utf-8'), LOG)['disks_status'] == 'ok'
    assert health.parse_powershell_output('no json here', LOG) == {'error': 'error'}
    seen = {}

    def run(args, **kw):
        seen['args'], seen['env'], seen['timeout'] = args, kw['env'], kw['timeout']
        raise subprocess.TimeoutExpired(args, kw['timeout'])
    monkeypatch.setattr(health.subprocess, 'run', run)
    assert health.run_powershell_health({'volumes': ['E:', 'f:\\']}, 1_795_000_000, LOG) == {'error': 'timeout'}
    assert seen['args'][:4] == ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand']
    assert seen['timeout'] == 90 and seen['env']['LM_HEALTH_VOLUMES'] == 'E,F'
    assert seen['env']['LM_HEALTH_SINCE'] == '2026-11-18T11:06:40Z'
    import base64
    assert base64.b64decode(seen['args'][4]).decode('utf-16-le') == health.PS_HEALTH_SCRIPT
    assert health.windows_components({'error': 'timeout'})['sources'] == {'windows_storage': 'timeout',
                                                                        'events': 'timeout'}
    bom = '\ufeff' + json.dumps(WIN_NAMES) + '\r\n'
    assert health.parse_powershell_output(bom.encode('utf-8'), LOG)['disks_status'] == 'ok'


@pytest.mark.parametrize('configured', [30, 150])
def test_powershell_timeout_follows_storage_health_timeout_seconds(collector, health, monkeypatch, configured):
    seen = {}

    def run(args, **kw):
        seen['timeout'] = kw['timeout']
        raise subprocess.TimeoutExpired(args, kw['timeout'])
    monkeypatch.setattr(health.subprocess, 'run', run)
    config = {'storage_health': {'timeout_seconds': configured}}
    assert health.run_powershell_health(config, 1_795_000_000, LOG) == {'error': 'timeout'}
    # Killed before the collector abandons the thread, which would leave PowerShell running
    assert seen['timeout'] == configured < configured + collector.HEALTH_BUDGET_MARGIN_SECONDS


def test_collector_sources_have_no_invisible_characters(collector):
    import unicodedata
    root = os.path.dirname(collector.__file__)
    found = []
    for folder, _, files in os.walk(root):
        for name in files:
            if name.endswith('.py'):
                with open(os.path.join(folder, name), encoding='utf-8') as f:
                    for n, line in enumerate(f, 1):
                        found += [(name, n, hex(ord(c))) for c in line if unicodedata.category(c) == 'Cf']
    assert found == []          # a BOM, zero-width space, ... typed literally: write it as an escape


def test_powershell_script_is_ascii_and_balanced(health):
    script = health.PS_HEALTH_SCRIPT
    assert script.isascii()
    for open_, close in ('()', '{}', '[]'):
        assert script.count(open_) == script.count(close), open_
    for cmdlet in ('Get-PhysicalDisk', 'Get-StorageReliabilityCounter', 'Get-VirtualDisk', 'Get-StorageJob',
                   'Get-Volume', 'Get-WinEvent', 'ConvertTo-Json -Compress'):
        assert cmdlet in script
    assert '$env:LM_HEALTH_SINCE' in script and '$env:LM_HEALTH_VOLUMES' in script
    for provider in health.EVENT_PROVIDERS:
        assert f"'{provider}'" in script


# ---------------------------------------------------------------------------
# classify, flatten, sending
# ---------------------------------------------------------------------------

def test_overall_is_the_worst_and_unknown_only_when_nothing_was_assessed(health):
    assert _classify(health)[0] == 'unknown'
    unknown = {f: None for f in health.DISK_FIELDS}
    unknown.update(id='sdz', health_source='sysfs')
    ok = dict(unknown, id='sda', device_state='running')
    assert _classify(health, disks=[unknown])[0] == 'unknown'
    assert _classify(health, disks=[unknown, ok])[0] == 'ok'
    assert _classify(health, disks=[ok, dict(ok, id='sdb', device_state='offline')])[0] == 'critical'


def test_sid_and_flatten(health):
    assert [health.sid(i) for i in ('/volume1', 'E:', '/', 'sata1', 'Pool-Mirror', 'x' * 40)] == [
        'volume1', 'e', 'root', 'sata1', 'pool_mirror', 'x' * 32]
    disk = {f: None for f in health.DISK_FIELDS}
    disks = [dict(disk, id='sda', status='ok', temperature_c=40, io_errors=0, bus='sata'),
             dict(disk, id='sdb', status='unknown', bus='usb', temperature_c=30),          # USB: never flattened
             dict(disk, id='SDA', status='warning', bus='sata')]                          # same key as sda
    arrays = [{'id': 'md2', 'status': 'warning', 'degraded': 1, 'sync_percent': 37.5}]
    volumes = [{'id': '/volume1', 'status': 'ok'}, {'id': '/', 'status': 'unknown'}]
    flat, truncated = health.flatten(arrays, disks, volumes)
    assert flat == {'disk_sda_temperature_c': 40, 'disk_sda_status_code': 0, 'disk_sda_io_errors': 0,
                    'array_md2_status_code': 1, 'array_md2_degraded': 1, 'array_md2_sync_percent': 37.5,
                    'volume_volume1_status_code': 0}
    assert truncated
    many = [dict(disk, id=f'sd{i}', status='ok', bus='sata') for i in range(50)]
    flat, truncated = health.flatten([], many, [])
    assert truncated and len(flat) == 48


_RENDER_TEMPS_JS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = vm.createContext({ console, setTimeout, clearTimeout, AbortController, URLSearchParams });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const disks = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = ctx.buildHealthHtml({ overall_status: 'ok', timestamp: new Date().toISOString(), disks });
console.log(JSON.stringify([...out.matchAll(/data-metric="([^"]*)"/g)].map(m => m[1])));
"""


@posix_only
@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_dashboard_links_exactly_the_disk_temperatures_the_collector_keeps(health):
    """dashboard.js offers a temperature chart only for the keys flatten() stores: cap, collisions, sid rules."""
    disk = {f: None for f in health.DISK_FIELDS}
    disks = [dict(disk, id='usb0', bus='usb', temperature_c=30, status='ok'),          # never flattened
             dict(disk, id='__sda__', bus='sata', temperature_c=31, status='ok'),       # sid strips '_'
             dict(disk, id='sdb', bus='sata', temperature_c=32, status='ok'),
             dict(disk, id='SDB', bus='sata', temperature_c=33, status='ok'),           # same key: the first wins
             dict(disk, id='sdc', bus='sata', temperature_c=None, status='ok'),         # no temperature, no claim
             dict(disk, id='SDC', bus='sata', temperature_c=34, status='ok'),
             dict(disk, id='', bus='sata', temperature_c=35, status='ok'),              # 'root'
             dict(disk, id='a' * 31 + '-x', bus='nvme', temperature_c=36, status='ok'),
             dict(disk, id='d\U0001F600x', bus='sata', temperature_c=37, status='ok')]
    disks += [dict(disk, id=f'pd{i}', bus='sas', temperature_c=40, status='ok') for i in range(45)]   # 52 non-USB
    flat, truncated = health.flatten([], disks, [])
    kept = {k for k in flat if k.endswith('_temperature_c')}
    assert truncated and len(kept) == 46 and 'disk_pd43_temperature_c' not in kept
    js = os.path.join(os.path.dirname(__file__), '..', '..', 'dashboard', 'static', 'js', 'dashboard.js')
    result = subprocess.run(['node', '-e', _RENDER_TEMPS_JS, js], input=json.dumps(disks),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    linked = json.loads(result.stdout)
    assert len(linked) == len(set(linked)) and set(linked) == kept


@pytest.fixture
def run(health, tree, tmp_path, monkeypatch):
    """maybe_collect() on the fake tree with a settable clock; the state is written as the collector would."""
    clock = {'now': 1_795_000_000.0}
    monkeypatch.setattr(health, '_wall_time', lambda: clock['now'])
    data_dir = tmp_path / 'data'
    (data_dir / 'box').mkdir(parents=True)
    state_file = data_dir / 'box' / health.STATE_FILE
    tree.disk('sda').mounts('/dev/sda2 / ext4 rw 0 0\n')
    calls = {'hwmon': 0}
    real_hwmon = health.read_hwmon

    def counted(sys_root=None):
        calls['hwmon'] += 1
        return real_hwmon(sys_root)
    monkeypatch.setattr(health, 'read_hwmon', counted)

    def collect(force=False, **block):
        payload, state = health.maybe_collect({'platform': 'linux', 'storage_health': block}, str(data_dir), 'box',
                                              force=force, logger=LOG)
        if state is not None:
            state_file.write_text(json.dumps(state))
        return payload
    collect.clock, collect.calls, collect.tree, collect.state_file = clock, calls, tree, state_file
    return collect


@posix_only
def test_sending_rules(run):
    first = run()
    assert first['trigger'] == 'change' and first['overall_status'] == 'ok' and first['overall_status_code'] == 0
    assert first['sources'] == {'md': 'unavailable', 'sysfs': 'ok', 'mounts': 'ok', 'hwmon': 'unavailable'}
    assert first['_schema'] == 1 and first['events'] is None and first['volumes'][0]['id'] == '/'
    run.clock['now'] += 300
    assert run() is None                                          # same signature, within the hour
    run.tree.disk('sda', state='offline')
    run.clock['now'] += 300
    changed = run()
    assert changed['trigger'] == 'change' and changed['overall_status'] == 'critical'
    assert changed['status_reasons'] == ['sda offline'] and changed['disk_sda_status_code'] == 2
    run.clock['now'] += 3600
    assert run()['trigger'] == 'interval'
    assert run(force=True)['trigger'] == 'manual'
    run.state_file.write_text('{not json')
    assert run()['trigger'] == 'change'                           # a corrupt state file: start afresh


def test_md_clean_active_flips_are_not_a_change(run):
    # The kernel marks a working array 'active' on its first write and 'clean'
    # ~200 ms after the last one: a 5-minute sample sees either
    triggers, states = [], []
    for state in ('clean', 'active', 'clean', 'write-pending', 'active-idle', 'active', 'clean'):
        run.tree.md('md2', 'raid1', state=state, raid_disks=2, members={'sda1': 'in_sync', 'sdb1': 'in_sync'})
        payload = run()
        triggers.append(payload and payload['trigger'])
        states.append(payload and payload['arrays'][0]['state'])
        run.clock['now'] += 300
    assert triggers == ['change'] + [None] * 6 and states[0] == 'clean'
    run.tree.md('md2', 'raid1', state='readonly', raid_disks=2, members={'sda1': 'in_sync', 'sdb1': 'in_sync'})
    changed = run()
    assert changed['trigger'] == 'change' and changed['arrays'][0]['state'] == 'readonly'


def test_hwmon_only_on_the_hourly_cadence(run):
    run.tree.hwmon(1, 'drivetemp', 41000, disk='sda')
    assert run()['disks'][0]['temperature_c'] == 41.0 and run.calls['hwmon'] == 1
    run.tree.hwmon(1, 'drivetemp', 65000, disk='sda')
    for _ in range(3):
        run.clock['now'] += 300
        run()
    assert run.calls['hwmon'] == 1                                # cached in the state file meanwhile
    run.clock['now'] += 3600
    hot = run()
    assert run.calls['hwmon'] == 2 and hot['disks'][0]['temperature_c'] == 65.0 and hot['overall_status'] == 'critical'


@posix_only
def test_ignore_and_no_smart_or_snmp(run):
    payload = run(ignore=['/'])
    assert payload['volumes'] == [] and 'smart' not in payload['sources'] and 'snmp' not in payload['sources']


def test_maybe_collect_writes_no_files(health, tree, tmp_path):
    before = sorted(tmp_path.rglob('*'))
    payload, state = health.maybe_collect({'platform': 'linux'}, str(tmp_path / 'data'), 'box')
    assert payload is not None and state['signature'] and sorted(tmp_path.rglob('*')) == before


def test_windows_runs_powershell_hourly_and_reuses_it(health, tmp_path, monkeypatch):
    clock = {'now': 1_795_000_000.0}
    monkeypatch.setattr(health, '_wall_time', lambda: clock['now'])
    runs = []

    def powershell(config, since, log=None):
        runs.append(since)
        return json.loads(json.dumps(WIN_NAMES))
    monkeypatch.setattr(health, 'run_powershell_health', powershell)
    state = None
    for step in range(3):
        if state is not None:
            os.makedirs(tmp_path / 'box', exist_ok=True)
            (tmp_path / 'box' / health.STATE_FILE).write_text(json.dumps(state))
        payload, state = health.maybe_collect({'platform': 'windows'}, str(tmp_path), 'box', logger=LOG)
        if step == 0:
            assert payload['events'] == WIN_NAMES['events'] and payload['overall_status'] == 'critical'
            assert set(payload['sources']) == {'windows_storage', 'events'}
        clock['now'] += 600
    assert len(runs) == 1 and runs[0] == 1_795_000_000.0 - health.FIRST_EVENT_LOOKBACK
    clock['now'] += 3600
    os.makedirs(tmp_path / 'box', exist_ok=True)
    (tmp_path / 'box' / health.STATE_FILE).write_text(json.dumps(state))
    health.maybe_collect({'platform': 'windows'}, str(tmp_path), 'box', logger=LOG)
    assert len(runs) == 2 and runs[1] == 1_795_000_000.0           # events since the previous full run
