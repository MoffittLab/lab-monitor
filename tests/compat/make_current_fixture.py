#!/usr/bin/env python3
"""
Build a realistic CURRENT-format Manager data_dir: what the review-fix
release (e5cd699 as deployed, or 160da09 / fb6498c) leaves on a server after it upgraded an OLD installation
and ran for a while.  It is the data_dir the next upgrade (CURRENT -> the
usage-metrics release) starts from.

It starts from an OLD-format data_dir written by make_old_fixture.py (copied;
the original is not changed) and lets the CURRENT code itself do the rest:
  1. the CURRENT Manager opens it (manager.init_app, as its first start does):
     every database is backed up to *.pre-upgrade-<stamp>.bak and migrated
     (narrow folder_usage_msgs / _items with the wide table kept as
     folder_usage_legacy, msg_id columns, dead_letter, network_last_updated);
  2. the CURRENT collector's own collect_metrics() / collect_disk_usage() /
     sync_queue_to_manager() build, archive, queue and send every message of
     the next --days days (same device models, a fake clock and fake
     measurements), and the CURRENT Manager stores them through its Flask
     routes (test client) with per-device collector_tokens:
       Atlantis     metrics every 5 min, daily scans (_volumes, _scan_seconds;
                    on the second day one folder times out: _timed_out)
       Vault-2      a 1 h Manager outage (backlog drained in one batch) and a
                    batch whose answer timed out after it was stored: the
                    collector re-sends it, the Manager answers "duplicate"
       win_srv.lab  Windows server with two GPUs
       Lab NAS      still runs the OLD collector on the first day (no msg_id:
                    the Manager derives "legacy-<sha1>" ids; its legacy
                    auth_tokens token) plus its sensor script ("1wire_sensors",
                    a table the CURRENT rules keep only because it exists),
                    then the CURRENT collector
       nas-3        a NAS first seen by CURRENT (its data.db never had a
                    pre-upgrade backup)
       ws-07, ATLANTIS  nothing new (retired / a spelling no longer used)
     plus messages the Manager rejects (dead_letter): a broken script's
     string cpu_percent and a message without a timestamp;
  3. --extra-messages FILE: queue bodies posted after that (e.g. a NEW
     collector that reported to CURRENT, which the rollout order forbids);
  4. --real-collector TREE (Linux): the CURRENT manager.py as a real process
     on 127.0.0.1 (it takes data_dir/manager.lock, as on the server) and the
     CURRENT collector.py run twice in metrics mode and once in disk mode on
     this host for one more device, "cur-linux", scanning TREE;
  5. when the CURRENT code has nightly snapshots (manager/snapshot.py, 160da09
     and later): one snapshot run by its own code to <out>-snapshots, which
     leaves snapshot-status.json and an empty .snapshot-staging/ in the
     data_dir, then what a snapshot killed while it staged atlantis/data.db
     leaves (.snapshot-staging/<stamp>/atlantis/data.db with a hot -journal).

Usage:
    python make_current_fixture.py --current-code CURRENT_CHECKOUT --old-data OLD_DATA_DIR
                                   --out DATA_DIR [--old-collector-state DIR] [--days 2]
                                   [--extra-messages FILE] [--real-collector TREE]
                                   [--summary summary.json]

Prints a one-line JSON summary (tokens included: test data only).
"""
import argparse
import importlib.util
import json
import logging
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import types
import urllib.request
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import make_old_fixture as old_fx  # noqa: E402  (device models, fake clock)

OLD_TOKEN = old_fx.TOKEN                         # the OLD collectors' token, kept as auth_tokens
ADMIN_TOKEN = 'fixture-current-admin-token-0123456789abcdef'
READ_TOKEN = 'fixture-current-read-token-0123456789abcdef'
GIB = old_fx.GIB


def collector_token(name):
    return 'fixture-current-collector-%08x-0123456789abcdef' % zlib.crc32(name.encode('utf-8'))


def token_key(name):
    """collector_tokens key of a device (names are matched after normalization)."""
    return ''.join(c if c.isalnum() or c in '_.-' else '_' for c in name)[:64]


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


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _old_state(data_dir):
    """{name: {'last_seen', 'bytes_in', 'bytes_out', 'uptime', 'metrics_ts'}} from the OLD central DB."""
    conn = sqlite3.connect('file:%s?mode=ro' % (Path(data_dir) / 'metrics.db'), uri=True)
    try:
        out = {}
        for name, last_seen in conn.execute('SELECT name, last_seen FROM devices'):
            out[name] = {'last_seen': last_seen}
        for name, lin, lout in conn.execute('SELECT name, last_bytes_in, last_bytes_out FROM device_totals'):
            out.setdefault(name, {}).update(bytes_in=lin, bytes_out=lout)
        for name, raw, ts in conn.execute('SELECT name, metrics_json, metrics_timestamp FROM device_snapshot'):
            try:
                m = json.loads(raw or '{}')
            except ValueError:
                m = {}
            out.setdefault(name, {}).update(uptime=m.get('uptime_seconds'), metrics_ts=ts)
        return out
    finally:
        conn.close()


def _parse(ts):
    return datetime.strptime(ts.replace('Z', '')[:19], '%Y-%m-%dT%H:%M:%S')


def real_collector_phase(cur, out, state, tree, summary):
    """The CURRENT manager.py (real process, takes manager.lock) and collector.py on this host."""
    work = Path(str(out) + '-real')
    work.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    name = 'cur-linux'
    cfg = {'data_dir': str(out), 'host': '127.0.0.1', 'port': port, 'log_file': str(work / 'manager.log'),
           'log_level': 'INFO', 'admin_tokens': [ADMIN_TOKEN], 'read_tokens': [READ_TOKEN],
           'auth_tokens': [OLD_TOKEN], 'collector_tokens': {name: collector_token(name)},
           'backup_before_migration': True, 'history_retention_days': None}
    (work / 'manager.json').write_text(json.dumps(cfg), encoding='utf-8')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    with open(work / 'manager.out', 'wb') as log:
        proc = subprocess.Popen([sys.executable, str(cur / 'manager' / 'manager.py'), '--config',
                                 str(work / 'manager.json')], cwd=str(work), stdout=log,
                                stderr=subprocess.STDOUT, env=env)
    try:
        deadline = time.time() + 120
        while True:
            if proc.poll() is not None:
                raise SystemExit('fixture: CURRENT manager.py exited: %s'
                                 % (work / 'manager.out').read_text(errors='replace')[-3000:])
            try:
                urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=2)
                break
            except OSError:
                if time.time() > deadline:
                    raise SystemExit('fixture: CURRENT manager.py did not start')
                time.sleep(0.1)
        ccfg = {'name': name, 'id': 'linux-' + name, 'device_type': 'Server', 'platform': 'linux',
                'manager_url': 'http://127.0.0.1:%d' % port, 'manager_token': collector_token(name),
                'scan_paths': [str(tree) + '/*'], 'volumes': [str(tree)], 'data_dir': str(state / ('linux-' + name)),
                'log_file': str(work / 'collector.log'), 'log_level': 'INFO', 'timeout_seconds': 300}
        (work / 'collector.json').write_text(json.dumps(ccfg), encoding='utf-8')
        runs = []
        for mode in ('metrics', 'metrics', 'disk'):
            r = subprocess.run([sys.executable, str(cur / 'collector' / 'collector.py'), '--config',
                                str(work / 'collector.json'), '--mode', mode], capture_output=True, env=env,
                               timeout=600)
            if r.returncode != 0:
                raise SystemExit('fixture: CURRENT collector --mode %s failed: %s'
                                 % (mode, r.stderr.decode('utf-8', 'replace')[-3000:]))
            runs.append(mode)
            time.sleep(1.5)
        text = (work / 'collector.log').read_text(encoding='utf-8', errors='replace')
        if text.count('duplicate 0, rejected 0, error 0') != 3:
            raise SystemExit('fixture: the real CURRENT collector runs were not all stored:\n' + text[-3000:])
        summary['real_collector'] = {'device': name, 'runs': runs}
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(30)


def snapshot_phase(cur, out, summary):
    """Step 5 of the module docstring (nothing for a CURRENT without snapshots, such as fb6498c)."""
    if not (cur / 'manager' / 'snapshot.py').is_file():
        return
    from compat_helpers import leave_killed_staging
    snap = _load_module('current_snapshot', cur / 'manager' / 'snapshot.py')
    dest = Path(str(out) + '-snapshots')
    settings = snap.settings_from_config({'data_dir': str(out),
                                          'snapshot': {'enabled': True, 'dest_dir': str(dest)}})
    res = snap.SnapshotRunner(str(out), settings, logging.getLogger('current-snapshot')).run('fixture')
    if res.get('status') != 'ok':
        raise SystemExit('fixture: the CURRENT snapshot failed: %s' % res)
    staged = out / snap.STAGING_DIR / '20261122T033000Z' / 'atlantis' / 'data.db'
    leave_killed_staging(out / 'atlantis' / 'data.db', staged)
    summary['snapshot'] = {'path': res['path'], 'files': res['files'],
                           'killed_staging': staged.relative_to(out).as_posix()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--current-code', required=True, help='extracted CURRENT checkout (manager/ and collector/)')
    ap.add_argument('--old-data', required=True, help='OLD-format data_dir from make_old_fixture.py (only read)')
    ap.add_argument('--old-collector-state', help="that fixture's collector state (copied: the collectors "
                                                  "are upgraded in place)")
    ap.add_argument('--out', required=True, help='data_dir to create (must not exist or be empty)')
    ap.add_argument('--collector-state', help='collector state dir (default: <out>-collectors)')
    ap.add_argument('--days', type=int, default=2, help='days the CURRENT release runs')
    ap.add_argument('--seed', type=int, default=20260803)
    ap.add_argument('--extra-messages', help='JSON list of queue bodies posted afterwards (admin token)')
    ap.add_argument('--real-collector', metavar='TREE',
                    help='also run the CURRENT manager.py and collector.py as real processes (Linux)')
    ap.add_argument('--summary', help='write the JSON summary here too')
    args = ap.parse_args(argv)

    cur = Path(args.current_code).resolve()
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f'{out} is not empty')
    if out.exists():
        out.rmdir()
    shutil.copytree(str(Path(args.old_data).resolve()), str(out))
    state = Path(args.collector_state or (str(out) + '-collectors')).resolve()
    if args.old_collector_state:
        shutil.copytree(str(Path(args.old_collector_state).resolve()), str(state), dirs_exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.ERROR)

    # Faster commits; the file format is unchanged.
    real_connect = sqlite3.connect

    def fast_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.execute('PRAGMA synchronous=OFF')
        return conn

    sqlite3.connect = fast_connect
    os.fsync = lambda fd: None              # the collectors' queue.json writes (test data only)
    old = _old_state(out)
    devices = old_fx.build_devices(args.seed)
    by_name = {d.name: d for d in devices}
    rnd = random.Random(args.seed)

    # ---- the CURRENT Manager's first start on the OLD data (its routes, test client) ---- #
    sys.path.insert(0, str(cur / 'manager'))
    cur_manager = _load_module('current_manager', cur / 'manager' / 'manager.py')
    mlog = logging.getLogger('current-manager')
    mlog.setLevel(logging.ERROR)
    nas3 = old_fx.Device(random.Random(args.seed + 7), 'nas-3', 'synology-nas3', 'NAS',
                         total_ram=16 * GIB, volumes={'/volume1': 8 * old_fx.TIB},
                         scan_paths=['/volume1/*'],
                         roots={'/volume1': {'/volume1/share%02d' % i: ('/volume1', (i + 1) * 3 * GIB)
                                             for i in range(12)}},
                         users=['admin', 'lab'], offset_s=17)
    fleet = [by_name[n] for n in ('Atlantis', 'Vault-2', 'win_srv.lab', 'Lab NAS')] + [nas3]
    config = {'data_dir': str(out), 'admin_tokens': [ADMIN_TOKEN], 'read_tokens': [READ_TOKEN],
              'auth_tokens': [OLD_TOKEN], 'backup_before_migration': True, 'history_retention_days': None,
              'collector_tokens': {token_key(d.name): collector_token(d.name) for d in fleet}}
    cur_manager.init_app(config, mlog)
    client = cur_manager.app.test_client()
    backups = sorted(str(p.relative_to(out)) for p in out.rglob('*.pre-upgrade-*.bak'))
    if not backups:
        raise SystemExit('fixture: the CURRENT Manager took no pre-upgrade backup of the OLD data')

    # ---- the CURRENT collector, upgraded in place on each device ---- #
    clock = old_fx.Clock
    fake_time = types.SimpleNamespace(time=lambda: old_fx._epoch(clock.now), sleep=lambda s: None,
                                      monotonic=lambda: old_fx._epoch(clock.now),
                                      perf_counter=lambda: old_fx._epoch(clock.now))
    sys.path.insert(0, str(cur / 'collector'))
    collector = _load_module('current_collector', cur / 'collector' / 'collector.py')
    ids = random.Random(args.seed + 1)
    collector.local_queue.new_msg_id = lambda: '%032x' % ids.getrandbits(128)
    collector._utcnow = lambda: clock.now
    collector.time = fake_time
    Sim = old_fx.Sim
    collector.get_local_ip = lambda: '10.20.0.%d' % (zlib.crc32(Sim.current.name.encode('utf-8')) % 200 + 10)
    collector.get_cpu_percent = lambda: Sim.current.cpu()
    collector.get_ram_percent = lambda: Sim.current.ram()
    collector.get_total_ram_bytes = lambda: Sim.current.total_ram
    collector.get_uptime_seconds = lambda: int(old_fx._epoch(clock.now) - Sim.current.boot_epoch)
    collector.get_network_counters = lambda include_loopback=False, include_virtual=False: (
        Sim.current.bytes_in, Sim.current.bytes_out)
    collector.get_gpu_stats = lambda: Sim.current.gpu_stats(collector.format_bytes)
    collector._seed_user_processes = lambda config: []
    collector.get_per_user_stats = lambda config, logger, seeded_procs=None: Sim.current.user_stats(
        collector.format_bytes)

    def measure_volumes(volumes, logger):
        d = Sim.current
        used = {v: d.volume_used(v) for v in volumes if v in d.volumes}
        cap = {}
        for v, u in used.items():
            cap['%s_total_bytes' % v] = d.volumes[v]
            cap['%s_free_bytes' % v] = d.volumes[v] - u
        return used, cap

    def measure_scan_paths(scan_paths, volumes, volume_used, nas_type, timeout, total_timeout, logger):
        d = Sim.current
        sizes, timed_out = {}, []
        for sub in d.roots.values():
            for p in sub:
                if d.exists(p):
                    sizes[p] = d.folder_size(p)
        for p in d.singles:
            sizes[p] = d.folder_size(p)
        for p in getattr(d, 'timed_out_today', ()):
            sizes.pop(p, None)
            timed_out.append(p)
        return {'sizes': sizes, 'timed_out': timed_out, 'errors': [],
                'seconds': round(30 + (zlib.crc32(d.name.encode('utf-8')) % 600) + d.day * 1.5, 1)}

    collector._measure_volumes = measure_volumes
    collector._measure_scan_paths = measure_scan_paths
    collector.discover_volumes = lambda *a, **kw: list(Sim.current.volumes)

    import requests
    stats = {'posts': 0, 'timeouts_after_store': 0, 'outage_failures': 0, 'old_collector_posts': 0}

    def fake_post(url, data=None, json=None, headers=None, timeout=None, **kw):
        d = Sim.current
        if d is not None and getattr(d, 'cur_outage', None) and d.cur_outage[0] <= clock.now < d.cur_outage[1]:
            stats['outage_failures'] += 1
            raise requests.exceptions.ConnectionError('simulated Manager outage')
        r = client.post(urlsplit(url).path, data=data, json=json, headers=headers)
        stats['posts'] += 1
        if d is not None and clock.now.replace(microsecond=0) in getattr(d, 'cur_timeouts', ()):
            stats['timeouts_after_store'] += 1
            raise requests.exceptions.ReadTimeout('simulated: stored, but the response timed out')
        return FakeResponse(r.status_code, r.get_data(as_text=True))

    requests.post = fake_post

    # The timeline continues a few hours after the last OLD message (the upgrade)
    last = max(_parse(v['last_seen']) for v in old.values() if v.get('last_seen'))
    begin = last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    end = begin + timedelta(days=args.days)
    for d in fleet:
        o = old.get(d.name) or {}
        if o.get('bytes_in') is not None:
            d.bytes_in, d.bytes_out = int(o['bytes_in']), int(o['bytes_out'])
        if o.get('uptime') is not None and o.get('metrics_ts'):
            d.boot_epoch = old_fx._epoch(_parse(o['metrics_ts'])) - float(o['uptime'])
        d.rebooted = True                      # the OLD fixture's reboot is history
        d.cur_outage = None
        d.cur_timeouts = set()
    vault, labnas, atlantis = by_name['Vault-2'], by_name['Lab NAS'], by_name['Atlantis']
    vault.cur_outage = (begin + timedelta(hours=10), begin + timedelta(hours=11))
    vault.cur_timeouts = {(begin + timedelta(hours=4, seconds=vault.offset_s)).replace(microsecond=0)}
    old_collector_until = begin + timedelta(days=1)

    configs = {}
    for d in fleet:
        configs[d.name] = {
            'name': d.name, 'id': d.system_id, 'device_type': d.device_type,
            'platform': 'windows' if d.device_type == 'Server' else 'synology',
            'manager_url': 'http://current-manager.invalid:5000', 'manager_token': collector_token(d.name),
            'data_dir': str(state / d.system_id), 'volumes': list(d.volumes), 'scan_paths': d.scan_paths,
            'timeout_seconds': 3600, 'request_timeout_seconds': 30,
        }
    clog = logging.getLogger('current-collector')
    clog.setLevel(logging.CRITICAL)
    clog.propagate = False

    def post_raw(body, token):
        r = client.post('/api/data/queue', json=body, headers={'Authorization': 'Bearer ' + token})
        if r.status_code != 200:
            raise SystemExit(f'fixture: post failed: {r.status_code} {r.get_data(as_text=True)}')
        return r.get_json()

    def old_collector_run(d, ts):
        """The OLD collector's message shapes (no msg_id), sent with its legacy token."""
        stamp = ts.isoformat() + 'Z'
        header = {'device_name': d.name, 'device_id': d.system_id, 'device_type': d.device_type,
                  'ip_address': '10.20.0.77', 'timestamp': stamp}
        up = int(old_fx._epoch(ts) - d.boot_epoch)
        msgs = [{'header': dict(header), 'data': {
            'data_type': 'system_metrics', 'cpu_percent': d.cpu(), 'cpu_percent_unit': '%',
            'ram_percent': d.ram(), 'ram_percent_unit': '%', 'total_ram_bytes': d.total_ram,
            'total_ram_bytes_unit': 'bytes', 'uptime_seconds': up, 'uptime_seconds_unit': 's',
            'uptime_formatted': '%dd %dh' % (up // 86400, up % 86400 // 3600),
            'network_bytes_in': d.bytes_in, 'network_bytes_in_unit': 'bytes',
            'network_bytes_out': d.bytes_out, 'network_bytes_out_unit': 'bytes',
            'network_bandwidth_in_mbps': round(rnd.uniform(1, 90), 2), 'network_bandwidth_in_mbps_unit': 'Mbps',
            'network_bandwidth_out_mbps': round(rnd.uniform(1, 40), 2), 'network_bandwidth_out_mbps_unit': 'Mbps',
            'gpus': [], 'gpu_count': 0}}]
        for u in d.user_stats(collector.format_bytes):
            msgs.append({'header': dict(header), 'data': dict(u, data_type='user_metrics')})
        res = post_raw({'queue_id': '%s-%s' % (d.name, ts.strftime('%Y-%m-%d-%H-%M-%S')), 'name': d.name,
                        'id': d.system_id, 'messages': msgs}, OLD_TOKEN)
        stats['old_collector_posts'] += 1
        if res.get('stored') != len(msgs):
            raise SystemExit('fixture: OLD-collector batch not stored: %s' % res)

    tick = begin
    counts = {d.name: {'metrics_runs': 0, 'disk_runs': 0} for d in fleet}
    while tick < end:
        day = (tick - old_fx.START).days
        for d in fleet:
            us = 0 if rnd.random() < 0.15 else rnd.randint(1, 999999)
            clock.now = tick + timedelta(seconds=d.offset_s, microseconds=us)
            Sim.current = d
            d.day = day
            d.advance_network()
            if d is labnas and tick < old_collector_until:
                old_collector_run(d, clock.now)
            else:
                collector.collect_metrics(configs[d.name], clog)
            counts[d.name]['metrics_runs'] += 1
            if tick.hour == 2 and tick.minute == 0 and not (d is labnas and tick < old_collector_until):
                clock.now = tick + timedelta(seconds=d.offset_s + 30, microseconds=us)
                d.timed_out_today = ['/volume1/Data/project_07'] if d is atlantis and tick > begin else []
                collector.collect_disk_usage(configs[d.name], clog)
                counts[d.name]['disk_runs'] += 1
        if tick == begin + timedelta(hours=6):
            # Lab NAS sensor script (no msg_id, the label the new rules keep only for its table)
            clock.now = tick + timedelta(seconds=5)
            sensor = [{'header': {'device_name': 'Lab NAS', 'device_id': labnas.system_id, 'device_type': 'NAS',
                                  'timestamp': (tick + timedelta(minutes=m, seconds=5)).isoformat() + 'Z'},
                       'data': {'data_type': '1wire_sensors', 'temp_c': 22.0 + m / 10, 'temp_c_unit': 'C',
                                'sensor': '28-0000072a1b3c'}} for m in (0, 5)]
            post_raw({'queue_id': 'Lab NAS-sensors-%d' % day, 'name': 'Lab NAS', 'id': labnas.system_id,
                      'messages': sensor}, OLD_TOKEN)
        if tick == begin + timedelta(hours=30):
            # A broken home-made script: rejected, kept in dead_letter
            res = post_raw({'queue_id': 'Lab NAS-script-1', 'name': 'Lab NAS', 'id': labnas.system_id,
                            'messages': [
                                {'header': {'msg_id': '%032x' % ids.getrandbits(128), 'device_name': 'Lab NAS',
                                            'device_id': labnas.system_id, 'device_type': 'NAS',
                                            'timestamp': (tick + timedelta(seconds=9)).isoformat() + 'Z'},
                                 'data': {'data_type': 'system_metrics', 'cpu_percent': 'high'}},
                                {'header': {'msg_id': '%032x' % ids.getrandbits(128), 'device_name': 'Lab NAS',
                                            'device_id': labnas.system_id, 'device_type': 'NAS'},
                                 'data': {'data_type': 'system_metrics', 'cpu_percent': 3.0}}]}, ADMIN_TOKEN)
            if res['counts']['rejected'] != 2:
                raise SystemExit('fixture: the broken messages were not rejected: %s' % res)
        tick += timedelta(minutes=5)

    # Flush whatever is still queued (e.g. after the simulated timeout)
    for d in fleet:
        Sim.current = d
        qp = collector.get_queue_path(configs[d.name], d.name)
        collector.sync_queue_to_manager(configs[d.name], d.name, d.system_id, qp, clog)
        if collector.local_queue.snapshot(qp, clog):
            raise SystemExit('fixture: %s still has queued messages' % d.name)

    extra = 0
    if args.extra_messages:
        for body in json.loads(Path(args.extra_messages).read_text(encoding='utf-8')):
            res = post_raw(body, ADMIN_TOKEN)
            if res['counts']['stored'] != len(body['messages']):
                raise SystemExit('fixture: extra messages not all stored: %s' % res['counts'])
            extra += len(body['messages'])

    cur_manager._close_stores()
    summary = {
        'data_dir': str(out), 'collector_state': str(state), 'tokens': {
            'admin': ADMIN_TOKEN, 'read': READ_TOKEN, 'legacy': OLD_TOKEN},
        'devices': [d.name for d in fleet], 'runs': counts, 'stats': stats, 'backups': backups,
        'start': begin.isoformat() + 'Z', 'end': end.isoformat() + 'Z', 'extra_messages': extra,
    }
    if args.real_collector:
        real_collector_phase(cur, out, state, Path(args.real_collector).resolve(), summary)
    snapshot_phase(cur, out, summary)
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
