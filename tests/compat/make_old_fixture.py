#!/usr/bin/env python3
"""
Build a realistic OLD-format Manager data_dir for upgrade tests.

The data is written by the OLD code itself (an extracted checkout of the base
commit, e.g. f086e7e): the old collector's own collect_metrics() /
collect_disk_usage() / sync_queue_to_manager() build and send every message,
and the old Manager stores it through its real Flask routes (test client).
Only the measurements are simulated (a fake psutil, a fake clock, fake folder
sizes), so payload shapes, header timestamps, queue handling and the old
Manager's schema evolution (NaN/'' backfill, wide folder_usage table) are
authentic.

Contents (default --days 3, starting 2026-08-01T00:00:00Z):
  Atlantis     Synology NAS. Metrics every 5 min; users admin, jeff, postgres.
               Reboot on day 1 (network counters and uptime reset).
               folder_usage daily: /volume1/homes/* (140), /volume1/Data/* (90),
               /volume2/Archive, plus "volume2/Archive" (a scan path configured
               without the leading slash: both keys in every row); one folder
               appears on day 1, one disappears on day 2; two volumes with
               *_total_bytes / *_free_bytes.  The day-0 scan's timestamp has no
               microseconds.  On the last day a scan reports
               "/volume1/data/project_00" next to "/volume1/Data/project_00":
               SQLite column names are case-insensitive, so the old Manager's
               ALTER TABLE failed and that scan exists only in the central
               snapshot (posted once, the way a pre-Sept-2026 collector that
               dropped its queue on any 200 sent it).
  ATLANTIS     The same NAS briefly sending under an upper-case name (a
               collector config typo): its own central device / snapshot /
               totals rows; its history shares data/atlantis/data.db.
  Vault-2      Synology NAS. One v1 "entries" batch before the v2 collector
               (legacy metrics + disk entries, one with a "+00:00" timestamp);
               two legacy POST /api/usage/report batches (data/Vault-2/usage.jsonl);
               a 90 min Manager outage (queue backlog drained in one batch);
               two "stored but the response timed out" retries, which make the
               old Manager store those messages twice (exact duplicates).
               /volume1/Shares/* (60 folders), some of them empty (0 bytes).
  win_srv.lab  Windows server ("." in the name). E:/Users/* (110 users, keys like
               E:\\Users\\jdoe), C:\\Data, volumes "C:" and "E:". Two GPUs appear
               at day 0 12:00 (driver installed: new columns, earlier rows
               backfilled). Users LAB\\jeff, LAB\\svc_backup, Administrator.
  Lab NAS      Space in the name (the old Manager accepted it). /volume1/* (50),
               users root, labuser, plus one hand-written message with no
               data_type (stored in the old "not_specified" table) and three
               from a sensor script with data_type "1wire_sensors" (a label
               the new ingest rules refuse: it starts with a digit).
  ws-07        Workstation with metrics for the first 6 hours only, no disk data.
  Vault-2 also receives two messages from a NEW collector (mixed-version
  rollout, collector upgraded before the Manager): a system_metrics message
  with null cpu / network counters (the old Manager stored NULL and set the
  network baseline to 0, so the next old message added the whole counter to
  the lifetime totals) and a folder_usage message with "_timed_out",
  "_errors" (TEXT columns: every earlier row backfilled with '') and
  "_scan_seconds" (REAL: backfilled with NaN, which SQLite stores as NULL).

--variant orphans adds the leftovers an old Manager could produce:
  retired-nas  history in data/retired_nas/data.db, but its central rows were
               removed by the old DELETE /api/system/<name> while Windows kept
               data.db locked (os.remove failed; the old code went on), so: a
               device directory with no central-DB entry, plus per-user rows
               (the old DELETE never removed those).
  gone-ws      deleted with the old DELETE: an empty data/gone_ws directory and
               orphaned per-user snapshot / history rows.
  ghost-pc     central device / snapshot / totals / per-user rows, but no
               data/ghost_pc directory (its history database was lost).
  typo-host    an empty data/typo_host/data.db (no tables) that the old
               Manager created when someone read /api/data/typo-host.

Usage:
    python make_old_fixture.py --old-code OLD_CHECKOUT --out DATA_DIR [--days 3]
                               [--variant default|orphans]
                               [--summary summary.json] [--leave-wal]

--leave-wal exits without closing the databases (like a Manager killed by Task
Scheduler), leaving -wal/-shm files next to them.
"""
import argparse
import importlib.util
import json
import logging
import math
import os
import posixpath
import random
import shutil
import sqlite3
import sys
import types
import zlib
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

TOKEN = 'fixture-old-auth-token-0123456789abcdef'
START = datetime(2026, 8, 1, 0, 0, 0)
GIB = 1024 ** 3
TIB = 1024 ** 4


# --------------------------------------------------------------------------- #
# Fake clock / fake psutil                                                    #
# --------------------------------------------------------------------------- #

class Clock:
    now = START


class FakeDatetime(datetime):
    """datetime whose utcnow()/now() follow Clock.now (naive UTC)."""

    @classmethod
    def utcnow(cls):
        n = Clock.now
        return cls(n.year, n.month, n.day, n.hour, n.minute, n.second, n.microsecond)

    @classmethod
    def now(cls, tz=None):
        n = cls.utcnow()
        if tz is None:
            return n
        return n.replace(tzinfo=timezone.utc).astimezone(tz)


def _epoch(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp()


fake_time = types.SimpleNamespace(
    time=lambda: _epoch(Clock.now),
    sleep=lambda s: None,
    monotonic=lambda: _epoch(Clock.now),
    perf_counter=lambda: _epoch(Clock.now),
)

VM = namedtuple('svmem', 'total available percent used free')
NIO = namedtuple('snetio', 'bytes_sent bytes_recv packets_sent packets_recv errin errout dropin dropout')


class Sim:
    """The device whose collector is currently running."""
    current = None


def _make_fake_psutil():
    m = types.ModuleType('psutil')

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    m.NoSuchProcess = NoSuchProcess
    m.AccessDenied = AccessDenied
    m.cpu_percent = lambda interval=None, percpu=False: Sim.current.cpu()
    m.cpu_count = lambda logical=True: 8

    def virtual_memory():
        d = Sim.current
        pct = d.ram()
        used = int(d.total_ram * pct / 100)
        return VM(d.total_ram, d.total_ram - used, pct, used, d.total_ram - used)

    m.virtual_memory = virtual_memory
    m.boot_time = lambda: Sim.current.boot_epoch

    def net_io_counters(pernic=False):
        d = Sim.current
        return NIO(d.bytes_out, d.bytes_in, 0, 0, 0, 0, 0, 0)

    m.net_io_counters = net_io_counters
    m.process_iter = lambda attrs=None: iter(())
    return m


# --------------------------------------------------------------------------- #
# Device models                                                               #
# --------------------------------------------------------------------------- #

class Device:
    def __init__(self, rnd, name, system_id, device_type, *, total_ram, volumes,
                 scan_paths=(), roots=None, singles=None, users=(), gpu_from=None,
                 reboot_at=None, outage=None, timeout_after_store=(), active_until=None,
                 offset_s=0, disk=True):
        self.rnd = rnd
        self.name, self.system_id, self.device_type = name, system_id, device_type
        self.total_ram = total_ram
        self.volumes = dict(volumes)            # vol -> total bytes
        self.scan_paths = list(scan_paths)
        self.roots = roots or {}                # glob root -> {subfolder path: (vol, base size)}
        self.singles = singles or {}            # single folder path -> (vol, base size)
        self.users = list(users)
        self.gpu_from = gpu_from
        self.reboot_at = reboot_at
        self.outage = outage
        self.timeout_after_store = set(timeout_after_store)
        self.active_until = active_until
        self.offset_s = offset_s
        self.disk = disk
        self.boot_epoch = _epoch(START) - rnd.randint(3, 40) * 86400
        self.bytes_in = rnd.randint(10, 900) * GIB
        self.bytes_out = rnd.randint(10, 400) * GIB
        self.rebooted = False
        self.appear = {}                        # path -> first day it exists
        self.disappear = {}                     # path -> first day it is gone
        self.day = 0

    # ---- metrics ---- #
    def cpu(self):
        t = (Clock.now - START).total_seconds() / 3600.0
        return round(max(0.0, min(100.0, 18 + 15 * math.sin(t / 3.0) + self.rnd.uniform(-4, 4))), 1)

    def ram(self):
        return round(self.rnd.uniform(35, 70), 1)

    def advance_network(self):
        if self.reboot_at and not self.rebooted and Clock.now >= self.reboot_at:
            self.rebooted = True
            self.boot_epoch = _epoch(Clock.now) - 95
            self.bytes_in = self.rnd.randint(1, 9) * 1000 * 1000
            self.bytes_out = self.rnd.randint(1, 9) * 1000 * 1000
            return
        self.bytes_in += self.rnd.randint(50, 600) * 1000 * 1000
        self.bytes_out += self.rnd.randint(20, 250) * 1000 * 1000

    def gpu_stats(self, fmt):
        if not self.gpu_from or Clock.now < self.gpu_from:
            return []
        gpus = []
        for i, (model, total) in enumerate((('NVIDIA RTX A4000', 16 * GIB), ('NVIDIA RTX A2000', 12 * GIB))):
            used = int(total * self.rnd.uniform(0.05, 0.9))
            gpus.append({
                'index': i, 'name': model, 'gpu_percent': self.rnd.randint(0, 100),
                'vram_used_bytes': used, 'vram_total_bytes': total,
                'vram_used_formatted': fmt(used), 'vram_total_formatted': fmt(total),
                'temperature_c': self.rnd.randint(35, 82),
                'power_watts': round(self.rnd.uniform(20, 140), 1) if i == 0 else None,
            })
        return gpus

    def user_stats(self, fmt):
        out = []
        for u in self.users:
            ram_b = self.rnd.randint(50, 4000) * 1024 * 1024
            out.append({'username': u, 'cpu_percent': round(self.rnd.uniform(0, 25), 2),
                        'ram_bytes': ram_b, 'ram_formatted': fmt(ram_b)})
        return sorted(out, key=lambda x: x['cpu_percent'], reverse=True)

    # ---- disk ---- #
    def exists(self, path):
        if path in self.appear and self.day < self.appear[path]:
            return False
        if path in self.disappear and self.day >= self.disappear[path]:
            return False
        return True

    def folder_size(self, path):
        for sub in self.roots.values():
            if path in sub:
                base = sub[path][1]
                break
        else:
            base = self.singles[path][1]
        if base == 0:
            return 0
        return int(base * (1 + 0.01 * self.day) + (zlib.crc32(path.encode('utf-8')) % 997) * 4096 * self.day)

    def volume_used(self, vol):
        used = 0
        for sub in self.roots.values():
            used += sum(self.folder_size(p) for p, (v, _) in sub.items() if v == vol and self.exists(p))
        used += sum(self.folder_size(p) for p, (v, _) in self.singles.items() if v == vol)
        return int(used * 1.07) + 3 * GIB


def build_devices(seed):
    rnd = random.Random(seed)
    dev = []

    def folders(prefix, sep, names, vol, lo=1, hi=900, zero_every=0):
        out = {}
        for i, n in enumerate(names):
            size = 0 if zero_every and i % zero_every == 0 else rnd.randint(lo, hi) * GIB // 10
            out[f"{prefix}{sep}{n}"] = (vol, size)
        return out

    homes = folders('/volume1/homes', '/', [f"user{i:03d}" for i in range(140)], '/volume1')
    data = folders('/volume1/Data', '/', [f"project_{i:02d}" for i in range(90)] + ['new_project'], '/volume1', 10, 9000)
    atl = Device(random.Random(seed + 1), 'Atlantis', 'synology-atlantis', 'NAS',
                 total_ram=32 * GIB, volumes={'/volume1': 40 * TIB, '/volume2': 16 * TIB},
                 scan_paths=['/volume1/homes/*', '/volume1/Data/*', '/volume2/Archive', 'volume2/Archive'],
                 roots={'/volume1/homes': homes, '/volume1/Data': data},
                 singles={'/volume2/Archive': ('/volume2', 7 * TIB), 'volume2/Archive': ('/volume2', 5 * GIB)},
                 users=['admin', 'jeff', 'postgres'],
                 reboot_at=START + timedelta(days=1, hours=13, minutes=7), offset_s=3)
    atl.appear['/volume1/Data/new_project'] = 1
    atl.disappear['/volume1/homes/user007'] = 2
    dev.append(atl)

    shares = folders('/volume1/Shares', '/', [f"share-{i:02d}" for i in range(60)], '/volume1', zero_every=17)
    vault = Device(random.Random(seed + 2), 'Vault-2', 'synology-vault2', 'NAS',
                   total_ram=8 * GIB, volumes={'/volume1': 24 * TIB},
                   scan_paths=['/volume1/Shares/*'], roots={'/volume1/Shares': shares},
                   users=['admin'],
                   outage=(START + timedelta(days=1, hours=8), START + timedelta(days=1, hours=9, minutes=30)),
                   timeout_after_store=[START + timedelta(days=2, hours=4), START + timedelta(days=2, hours=16, minutes=35)],
                   offset_s=11)
    dev.append(vault)

    users = folders('E:\\Users', '\\', [f"jdoe{i:03d}" for i in range(110)], 'E:', 1, 3000)
    win = Device(random.Random(seed + 3), 'win_srv.lab', 'windows-win_srv', 'Server',
                 total_ram=128 * GIB, volumes={'C:': 1 * TIB, 'E:': 60 * TIB},
                 scan_paths=['E:/Users/*', 'C:\\Data'],
                 roots={'E:/Users': users}, singles={'C:\\Data': ('C:', 210 * GIB)},
                 users=['LAB\\jeff', 'LAB\\svc_backup', 'Administrator'],
                 gpu_from=START + timedelta(hours=12), offset_s=27)
    dev.append(win)

    top = folders('/volume1', '/', [f"share{i:02d}" for i in range(50)], '/volume1', zero_every=23)
    labnas = Device(random.Random(seed + 4), 'Lab NAS', 'lab-nas-01', 'NAS',
                    total_ram=4 * GIB, volumes={'/volume1': 12 * TIB},
                    scan_paths=['/volume1/*'], roots={'/volume1': top},
                    users=['root', 'labuser'], offset_s=41)
    dev.append(labnas)

    ws = Device(random.Random(seed + 5), 'ws-07', 'windows-ws07', 'Workstation',
                total_ram=16 * GIB, volumes={}, active_until=START + timedelta(hours=6),
                offset_s=53, disk=False)
    dev.append(ws)
    return dev


# --------------------------------------------------------------------------- #
# Wiring the old code                                                         #
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--old-code', required=True, help='extracted OLD checkout (contains manager/ and collector/)')
    ap.add_argument('--out', required=True, help='Manager data_dir to create (must not exist or be empty)')
    ap.add_argument('--collector-state', help='scratch dir for collector queue/archive files (default: <out>-collectors)')
    ap.add_argument('--days', type=int, default=3)
    ap.add_argument('--variant', choices=('default', 'orphans'), default='default',
                    help='orphans: also leave the orphaned directories / rows an old Manager could produce')
    ap.add_argument('--seed', type=int, default=20260801)
    ap.add_argument('--summary', help='write a JSON summary of what was generated')
    ap.add_argument('--leave-wal', action='store_true', help='exit without closing the databases')
    args = ap.parse_args(argv)

    old = Path(args.old_code).resolve()
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    state = Path(args.collector_state or (str(out) + '-collectors')).resolve()
    state.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.ERROR)

    # Faster commits; the file format is unchanged.
    real_connect = sqlite3.connect

    def fast_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.execute('PRAGMA synchronous=OFF')
        return conn

    sqlite3.connect = fast_connect

    sys.modules['psutil'] = _make_fake_psutil()

    # ---- OLD manager (its routes, through the Flask test client) ---- #
    sys.path.insert(0, str(old / 'manager'))
    old_manager = _load_module('old_manager', old / 'manager' / 'manager.py')
    mlog = logging.getLogger('old-manager')
    mlog.setLevel(logging.ERROR)
    old_manager.init_app({'data_dir': str(out), 'auth_tokens': [TOKEN]}, mlog)
    client = old_manager.app.test_client()

    # ---- OLD collector ---- #
    collector = _load_module('old_collector', old / 'collector' / 'collector.py')
    reporter = _load_module('old_reporter', old / 'collector' / 'lib' / 'reporter.py')
    collector.datetime = FakeDatetime
    collector.time = fake_time
    collector.get_local_ip = lambda: '10.20.0.%d' % (zlib.crc32(Sim.current.name.encode('utf-8')) % 200 + 10)
    collector._seed_user_processes = lambda config: []
    collector.get_per_user_stats = lambda config, logger, seeded_procs=None: Sim.current.user_stats(collector._format_bytes)
    collector.get_gpu_stats = lambda: Sim.current.gpu_stats(collector._format_bytes)
    reporter.time = fake_time

    # Disk measurement against the device model
    def is_known_dir(p):
        d = Sim.current
        return p in d.roots or p in d.singles or p in d.volumes

    # POSIX path rules on every platform, as on the NAS the old collector ran on:
    # on Windows, ntpath.normpath would turn the simulated '/volume1/homes/*' into
    # '\\volume1\\homes', which the device model doesn't know, and the old data_dir
    # would come out without its folders.  posixpath handles the Windows paths of
    # the fixture's own state files too (join with '/', dirname, exists).
    fake_path = types.ModuleType('os.path')
    fake_path.__dict__.update(posixpath.__dict__)
    fake_path.isdir = lambda p: is_known_dir(p) or os.path.isdir(p)
    fake_os = types.ModuleType('os')
    fake_os.__dict__.update(os.__dict__)
    fake_os.path = fake_path
    fake_os.sep = '/'
    collector.os = fake_os
    collector.discover_volumes = lambda nas_type=None: list(Sim.current.volumes)
    collector.measure_volume_capacity = lambda vol: (
        {'total_bytes': Sim.current.volumes[vol],
         'free_bytes': Sim.current.volumes[vol] - Sim.current.volume_used(vol)}
        if vol in Sim.current.volumes else {})
    collector.discover_folders = lambda root, nas_type='synology': [
        p for p in Sim.current.roots.get(root, {}) if Sim.current.exists(p)]
    collector.measure_leaf_folders = lambda paths, timeout=3600: [
        {'path': p, 'usage_bytes': Sim.current.folder_size(p)} for p in paths]
    collector.measure_folder_recursive = lambda path, timeout=3600: (
        Sim.current.folder_size(path), 1000)

    import requests
    stats = {'posts': 0, 'timeouts_after_store': 0, 'outage_failures': 0, 'reports': 0}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        d = Sim.current
        if d is not None and d.outage and d.outage[0] <= Clock.now < d.outage[1]:
            stats['outage_failures'] += 1
            raise requests.exceptions.ConnectionError('simulated Manager outage')
        r = client.post(urlsplit(url).path, json=json, headers=headers)
        stats['posts'] += 1
        if d is not None and Clock.now.replace(microsecond=0) - timedelta(seconds=d.offset_s) in d.timeout_after_store:
            stats['timeouts_after_store'] += 1
            raise requests.exceptions.ReadTimeout('simulated: stored, but the response timed out')
        return FakeResponse(r.status_code, r.get_data(as_text=True))

    requests.post = fake_post

    devices = build_devices(args.seed)
    clog = logging.getLogger('old-collector')
    clog.setLevel(logging.CRITICAL)
    configs = {}
    for d in devices:
        configs[d.name] = {
            'name': d.name, 'id': d.system_id, 'device_type': d.device_type,
            'manager_url': 'http://old-manager.invalid:5000', 'manager_token': TOKEN,
            'data_dir': str(state / d.system_id), 'volumes': list(d.volumes),
            'scan_paths': d.scan_paths, 'timeout_seconds': 3600, 'request_timeout_seconds': 30,
        }

    def post_raw(body):
        r = client.post('/api/data/queue', json=body, headers={'Authorization': f'Bearer {TOKEN}'})
        if r.status_code != 200:
            raise SystemExit(f'fixture: raw post failed: {r.status_code} {r.get_data(as_text=True)}')

    vault = next(d for d in devices if d.name == 'Vault-2')
    labnas = next(d for d in devices if d.name == 'Lab NAS')
    atlantis = next(d for d in devices if d.name == 'Atlantis')
    auth = {'Authorization': f'Bearer {TOKEN}'}
    quirk_day = min(1, args.days - 1)
    msg_seq = iter(range(1, 10 ** 6))

    def ts_at(day, h, m, s, us=0):
        t = START + timedelta(days=day, hours=h, minutes=m, seconds=s, microseconds=us)
        return t.isoformat() + 'Z'          # like the collector: no fraction when us == 0

    def old_msg(name, system_id, data_type, ts, data, device_type='NAS', msg_id=None):
        header = {'device_name': name, 'device_id': system_id, 'device_type': device_type,
                  'ip_address': '10.20.0.99', 'timestamp': ts}
        if msg_id:
            header['msg_id'] = msg_id
        return {'header': header, 'data': dict(data, data_type=data_type)}

    def post_msgs(name, system_id, msgs):
        post_raw({'queue_id': f'{name}-fixture-{next(msg_seq)}', 'name': name, 'id': system_id,
                  'messages': msgs})

    def mixed_rollout():
        """Two messages of a NEW collector (msg_id, null for unmeasured) received by the OLD Manager."""
        Sim.current = vault
        vault.day = quirk_day
        ids = random.Random(args.seed + 99)
        new_id = lambda: '%032x' % ids.getrandbits(128)    # noqa: E731
        up = int(_epoch(Clock.now) - vault.boot_epoch)
        metrics = {
            'cpu_percent': None, 'cpu_percent_unit': '%', 'ram_percent': 44.1, 'ram_percent_unit': '%',
            'total_ram_bytes': vault.total_ram, 'total_ram_bytes_unit': 'bytes',
            'uptime_seconds': up, 'uptime_seconds_unit': 's', 'uptime_formatted': '%dd' % (up // 86400),
            'network_bytes_in': None, 'network_bytes_in_unit': 'bytes',
            'network_bytes_out': None, 'network_bytes_out_unit': 'bytes',
            'network_bandwidth_in_mbps': None, 'network_bandwidth_in_mbps_unit': 'Mbps',
            'network_bandwidth_out_mbps': None, 'network_bandwidth_out_mbps_unit': 'Mbps',
            'gpus': [], 'gpu_count': 0,
        }
        timed_out = '/volume1/Shares/share-05'
        folders = {p: vault.folder_size(p) for p in vault.roots['/volume1/Shares']
                   if vault.exists(p) and p != timed_out}
        used, total = vault.volume_used('/volume1'), vault.volumes['/volume1']
        disk = dict(folders, **{'_unit': 'bytes', '/volume1': used, '/volume1_total_bytes': total,
                                '/volume1_free_bytes': total - used, 'total_usage': used,
                                '_timed_out': [timed_out], '_errors': [], '_scan_seconds': 812.4})
        post_msgs('Vault-2', vault.system_id, [
            old_msg('Vault-2', vault.system_id, 'system_metrics', ts_at(quirk_day, 6, 0, 11), metrics,
                    msg_id=new_id()),
            old_msg('Vault-2', vault.system_id, 'folder_usage', ts_at(quirk_day, 6, 0, 41, 500000), disk,
                    msg_id=new_id()),
        ])

    def upper_case_atlantis():
        """The Atlantis collector briefly configured as "ATLANTIS" (its own central rows)."""
        base_in, base_out = 5 * GIB, 2 * GIB
        msgs = []
        for i, (m, us) in enumerate(((0, 0), (5, 250000), (10, 0))):
            ts = ts_at(quirk_day, 10, m, 1, us)
            msgs.append(old_msg('ATLANTIS', atlantis.system_id, 'system_metrics', ts, {
                'cpu_percent': 21.5 + i, 'cpu_percent_unit': '%', 'ram_percent': 52.0, 'ram_percent_unit': '%',
                'total_ram_bytes': atlantis.total_ram, 'total_ram_bytes_unit': 'bytes',
                'uptime_seconds': 3600 + 300 * i, 'uptime_seconds_unit': 's', 'uptime_formatted': '1h',
                'network_bytes_in': base_in + i * 400 * 1000 * 1000, 'network_bytes_in_unit': 'bytes',
                'network_bytes_out': base_out + i * 100 * 1000 * 1000, 'network_bytes_out_unit': 'bytes',
                'network_bandwidth_in_mbps': 10.5, 'network_bandwidth_in_mbps_unit': 'Mbps',
                'network_bandwidth_out_mbps': 2.5, 'network_bandwidth_out_mbps_unit': 'Mbps',
                'gpus': [], 'gpu_count': 0}))
            msgs.append(old_msg('ATLANTIS', atlantis.system_id, 'user_metrics', ts, {
                'username': 'jeff', 'cpu_percent': 3.25 + i, 'ram_bytes': (300 + i) * 1024 * 1024,
                'ram_formatted': '%d MB' % (300 + i)}))
        post_msgs('ATLANTIS', atlantis.system_id, msgs)

    def case_colliding_scan():
        """
        A scan reporting "/volume1/data/project_00" next to the existing
        "/volume1/Data/project_00" column: the old ALTER TABLE fails ("duplicate
        column name"), so the history row is lost but the central snapshot and
        disk total are updated.
        """
        snap = old_manager._metrics_db.get_snapshot('Atlantis')
        data = dict(snap['folders'])
        data['/volume1/data/project_00'] = 4096
        post_msgs('Atlantis', atlantis.system_id, [
            old_msg('Atlantis', atlantis.system_id, 'folder_usage', ts_at(args.days - 1, 3, 0, 33), data)])

    def orphans():
        """Leftovers of the old DELETE /api/system/<name> and of a lost history database."""
        def device_msgs(name, sid, day, users, disk=True):
            msgs = []
            for i in range(3):
                ts = ts_at(day, 4, 5 * i, 7, 0 if i == 1 else 123456 * (i + 1))
                msgs.append(old_msg(name, sid, 'system_metrics', ts, {
                    'cpu_percent': 10.0 + i, 'cpu_percent_unit': '%', 'ram_percent': 30.0 + i,
                    'ram_percent_unit': '%', 'total_ram_bytes': 8 * GIB, 'total_ram_bytes_unit': 'bytes',
                    'uptime_seconds': 86400 + 300 * i, 'uptime_seconds_unit': 's', 'uptime_formatted': '1d',
                    'network_bytes_in': (40 + i) * GIB, 'network_bytes_in_unit': 'bytes',
                    'network_bytes_out': (20 + i) * GIB, 'network_bytes_out_unit': 'bytes',
                    'network_bandwidth_in_mbps': 1.5, 'network_bandwidth_in_mbps_unit': 'Mbps',
                    'network_bandwidth_out_mbps': 0.5, 'network_bandwidth_out_mbps_unit': 'Mbps',
                    'gpus': [], 'gpu_count': 0}))
                for j, u in enumerate(users):
                    msgs.append(old_msg(name, sid, 'user_metrics', ts, {
                        'username': u, 'cpu_percent': 1.5 * (i + j + 1), 'ram_bytes': (100 + 10 * j + i) * 1024 * 1024,
                        'ram_formatted': '%d MB' % (100 + 10 * j + i)}))
            if disk:
                msgs.append(old_msg(name, sid, 'folder_usage', ts_at(day, 4, 30, 9), {
                    '_unit': 'bytes', '/volume1/a': 11 * GIB, '/volume1/b': 0, '/volume1': 12 * GIB,
                    '/volume1_total_bytes': 100 * GIB, '/volume1_free_bytes': 88 * GIB, 'total_usage': 12 * GIB}))
            return msgs

        post_msgs('retired-nas', 'synology-retired', device_msgs('retired-nas', 'synology-retired', 0, ['admin', 'bob']))
        post_msgs('gone-ws', 'windows-gone', device_msgs('gone-ws', 'windows-gone', 0, ['LAB\\carol'], disk=False))
        post_msgs('ghost-pc', 'windows-ghost', device_msgs('ghost-pc', 'windows-ghost', 0, ['dave']))

        # Old DELETE while Windows holds data.db open (antivirus, a backup agent):
        # os.remove fails, the old code logs it and still clears the central rows.
        locked = os.path.normcase(str(out / 'retired_nas' / 'data.db'))
        real_remove = os.remove

        def locked_remove(p, *a, **kw):
            if os.path.normcase(os.path.abspath(str(p))) == locked:
                raise PermissionError(13, 'The process cannot access the file because it is being '
                                          'used by another process', str(p))
            return real_remove(p, *a, **kw)

        os.remove = locked_remove
        try:
            r = client.delete('/api/system/retired-nas', headers=auth)
        finally:
            os.remove = real_remove
        if r.status_code != 200:
            raise SystemExit(f'fixture: old DELETE retired-nas failed: {r.status_code}')
        r = client.delete('/api/system/gone-ws', headers=auth)
        if r.status_code != 200 or not (r.get_json() or {}).get('per_system_db_deleted'):
            raise SystemExit(f'fixture: old DELETE gone-ws failed: {r.status_code} {r.get_data(as_text=True)}')
        # A lost history database: central rows stay
        db = old_manager._data_store._dbs.pop('ghost-pc', None)
        if db is not None:
            db.close()
        shutil.rmtree(str(out / 'ghost_pc'))
        # The old read path created data/<name>/data.db for any name it was asked about
        r = client.get('/api/data/typo-host', headers=auth)
        if r.status_code != 200:
            raise SystemExit(f'fixture: old GET /api/data/typo-host failed: {r.status_code}')

    # ---- v1 collector "entries" batch before the v2 collector (Vault-2) ---- #
    t_legacy = START - timedelta(hours=1)
    post_raw({
        'queue_id': 'Vault-2-2026-07-31-23-00-00', 'name': 'Vault-2', 'id': vault.system_id,
        'entries': [
            {'type': 'metrics', 'timestamp': t_legacy.strftime('%Y-%m-%dT%H:%M:%SZ'), 'device_type': 'NAS',
             'cpu_percent': 3.5, 'ram_percent': 41.0,
             'network_bytes_in': vault.bytes_in - 7 * GIB, 'network_bytes_out': vault.bytes_out - 3 * GIB},
            {'type': 'disk', 'timestamp': (t_legacy + timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S+00:00'),
             'device_type': 'NAS', 'total_usage': 9 * TIB,
             'folders': [{'path': '/volume1/Shares/share-01', 'usage_bytes': 123 * GIB}]},
        ],
    })

    # ---- legacy POST /api/usage/report (usage.jsonl) ---- #
    def legacy_reports(day):
        Clock.now = START + timedelta(days=day, hours=1)
        ts = Clock.now.strftime('%Y-%m-%dT%H:%M:%SZ')
        Sim.current = vault
        folders_ = [{'path': p.lstrip('/'), 'usage_bytes': vault.folder_size(p)}
                    for p in list(vault.roots['/volume1/Shares'])[:5]]
        ok = reporter.flush_queue([{'nas_name': 'Vault-2', 'nas_id': vault.system_id,
                                    'timestamp': ts, 'folders': folders_}],
                                  'http://old-manager.invalid:5000', TOKEN, retry_attempts=1)
        if not ok:
            raise SystemExit('fixture: legacy /api/usage/report failed')
        stats['reports'] += 1

    # ---- main timeline ---- #
    rnd = random.Random(args.seed)
    end = START + timedelta(days=args.days)
    tick = START
    counts = {d.name: {'metrics_runs': 0, 'disk_runs': 0} for d in devices}
    reports_done = set()
    while tick < end:
        day = (tick - START).days
        if day not in reports_done and day < 2 and tick >= START + timedelta(days=day, hours=1):
            legacy_reports(day)
            reports_done.add(day)
        for d in devices:
            if d.active_until and tick >= d.active_until:
                continue
            us = 0 if rnd.random() < 0.15 else rnd.randint(1, 999999)
            Clock.now = tick + timedelta(seconds=d.offset_s, microseconds=us)
            Sim.current = d
            d.day = day
            d.advance_network()
            collector.collect_metrics(configs[d.name], clog)
            counts[d.name]['metrics_runs'] += 1
            if d.disk and tick.hour == 2 and tick.minute == 0:
                us_disk = 0 if (d is atlantis and day == 0) else us
                Clock.now = tick + timedelta(seconds=d.offset_s + 30, microseconds=us_disk)
                collector.collect_disk_usage(configs[d.name], clog)
                counts[d.name]['disk_runs'] += 1
        if tick == START + timedelta(days=quirk_day, hours=6):
            Clock.now = tick + timedelta(seconds=11)
            mixed_rollout()
        if tick == START + timedelta(days=quirk_day, hours=10, minutes=10):
            upper_case_atlantis()
        if tick == START + timedelta(days=args.days - 1, hours=3):
            case_colliding_scan()
        tick += timedelta(minutes=5)

    # A hand-written message without data_type (old "not_specified" table)
    Clock.now = START + timedelta(days=1, hours=12)
    post_raw({
        'queue_id': 'Lab NAS-manual-1', 'name': 'Lab NAS', 'id': labnas.system_id,
        'messages': [{'header': {'device_name': 'Lab NAS', 'device_id': labnas.system_id,
                                 'device_type': 'NAS', 'timestamp': '2026-08-02T12:00:00.000000Z'},
                      'data': {'note': 'manual test message', 'value': 42}}],
    })
    # A home-made sensor script posting a data_type the old Manager took as is
    # (table "1wire_sensors"); the new ingest rules refuse a label starting
    # with a digit, the old table must stay readable
    post_msgs('Lab NAS', labnas.system_id, [
        old_msg('Lab NAS', labnas.system_id, '1wire_sensors', ts_at(min(1, args.days - 1), 12, m, 5),
                {'temp_c': 21.5 + m / 10, 'temp_c_unit': 'C', 'sensor': '28-0000072a1b3c'})
        for m in (0, 5, 10)])

    # Flush whatever is still queued (e.g. after the simulated timeouts)
    for d in devices:
        Sim.current = d
        qp = collector.get_queue_path(configs[d.name], d.name)
        collector.sync_queue_to_manager(configs[d.name], d.name, d.system_id, qp, clog)

    if args.variant == 'orphans':
        orphans()

    summary = {
        'data_dir': str(out), 'collector_state': str(state), 'token': TOKEN, 'variant': args.variant,
        'devices': [d.name for d in devices] + ['ATLANTIS'], 'runs': counts, 'stats': stats,
        'start': START.isoformat() + 'Z', 'end': end.isoformat() + 'Z',
    }

    if args.leave_wal:
        if args.summary:
            Path(args.summary).write_text(json.dumps(summary, indent=2), encoding='utf-8')
        sys.stdout.write(json.dumps(summary) + '\n')
        sys.stdout.flush()
        os._exit(0)

    old_manager._data_store.close()
    old_manager._metrics_db.close()
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
