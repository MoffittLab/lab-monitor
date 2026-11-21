"""
Spec section 18 with real processes: the collector, Manager and Dashboard of
different releases talking HTTP on 127.0.0.1, as during a staged rollout.

  NEW collector -> OLD (f086e7e) and CURRENT (fb6498c) Manager: every run's
      batch is acknowledged (the OLD Manager sends no per-message results,
      only stored == len(batch)), the queue drains and every archived message
      is stored exactly once.
  NEW Dashboard -> those Managers: not degraded, the new sections hidden
      (R7), per-user history refused instead of mixing every user.
  NEW Manager on a copy of what they stored: every row reads as before.
  CURRENT Dashboard -> NEW Manager: the same /api/data as on the CURRENT
      Manager over the same data.
  CURRENT collector -> NEW Manager: stored, queue drained, rolled up; the
      new endpoints answer 404 for that system and the NEW Dashboard hides
      their sections.

test_new_payloads_old_managers.py proves the storage rules with the
Appendix A payloads through each Manager's Flask routes in one process; here
the real collector runs on this host (its /proc, utmp, disks, and a scan tree
with old mtimes) against the real servers, so the payloads, the ACK handling
and the queue are the real ones.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from compat_helpers import REPO, free_port  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'),
                                reason='runs the collector as platform linux on this host')

NAME = 'mixbox'
NEW_TYPES = ('system_metrics', 'user_metrics', 'sessions', 'storage_health', 'storage_breakdown', 'folder_usage')
# what a NEW collector adds to user rows (spec section 12)
NEW_USER_KEYS = {'cpu_seconds', 'cpu_cores', 'interval_s', 'process_count', 'gpu_vram_bytes', 'gpu_util_percent',
                 'gpu_busy_seconds', 'gpus_held', 'gpu_held_seconds', 'gpu_processes'}
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')


def http(url, token=None, method='GET', body=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    data = None
    if body is not None:
        data, headers['Content-Type'] = json.dumps(body).encode(), 'application/json'
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw or b'null')
    except ValueError:
        return status, None


class Server:
    """manager.py or app.py of some release, on a free 127.0.0.1 port."""

    def __init__(self, script, cfg, workdir, label, up_path):
        self.port = cfg['port'] = free_port()
        self.url = 'http://127.0.0.1:%d' % self.port
        cfg.setdefault('host', '127.0.0.1')
        cfg.setdefault('log_file', str(workdir / (label + '.log')))
        path = workdir / (label + '.json')
        path.write_text(json.dumps(cfg), encoding='utf-8')
        self.out = workdir / (label + '.out')
        with open(self.out, 'wb') as out:
            self.proc = subprocess.Popen([sys.executable, str(script), '--config', str(path)], cwd=str(workdir),
                                         stdout=out, stderr=subprocess.STDOUT, env=ENV)
        deadline = time.time() + 60
        while True:
            if self.proc.poll() is not None:
                raise AssertionError('%s exited: %s' % (label, self.out.read_text(errors='replace')[-3000:]))
            try:
                urllib.request.urlopen(self.url + up_path, timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                if time.time() > deadline:
                    self.stop()
                    raise AssertionError('%s did not start' % label)
                time.sleep(0.1)

    def get(self, path, token=None):
        return http(self.url + path, token)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(15)


def start_manager(code, data_dir, workdir, label, legacy=False, **extra):
    """A Manager of the release in `code`: OLD gets the legacy auth_tokens, the others scoped tokens."""
    tok = {k: secrets.token_hex(20) for k in ('admin', 'read', 'collector')}
    cfg = {'data_dir': str(data_dir), 'log_level': 'INFO'}
    if legacy:
        tok['read'] = tok['collector'] = tok['admin']
        cfg['auth_tokens'] = [tok['admin']]
    else:
        cfg.update(admin_tokens=[tok['admin']], read_tokens=[tok['read']], collector_tokens={NAME: tok['collector']})
    cfg.update(extra)
    srv = Server(code / 'manager' / 'manager.py', cfg, workdir, label, '/api/systems')
    srv.tokens = tok
    return srv


def start_dashboard(code, manager, workdir, label):
    cfg = {'manager_url': manager.url, 'manager_token': manager.tokens['read'], 'log_level': 'INFO',
           'refresh_interval_seconds': 30, 'manager_timeout_seconds': 30}
    return Server(code / 'dashboard' / 'app.py', cfg, workdir, label, '/')


def make_tree(root):
    """Three folders whose files were last modified 0 days to 4 years ago."""
    now, day = time.time(), 86400
    for d, files in {'projA': [(4000, 10), (3000, 500), (2000, 1500)], 'projB': [(10000, 2), (5000, 800)]}.items():
        (root / d / 'sub').mkdir(parents=True)
        for i, (size, age) in enumerate(files):
            p = root / d / ('sub' if i % 2 else '') / ('f%d.bin' % i)
            p.write_bytes(b'x' * size)
            os.utime(p, (now - age * day, now - age * day))


class Collector:
    """A collector of the release in `code` with its own data_dir, sending to `manager`."""

    def __init__(self, code, manager, workdir, tree):
        self.code, self.dir = code, workdir
        workdir.mkdir(parents=True)
        self.cfg = {'name': NAME, 'id': 'linux-' + NAME, 'device_type': 'Server', 'platform': 'linux',
                    'manager_url': manager.url, 'manager_token': manager.tokens['collector'],
                    'scan_paths': [str(tree) + '/*'], 'volumes': [str(tree)], 'data_dir': str(workdir / 'state'),
                    'log_file': str(workdir / 'collector.log'), 'log_level': 'INFO', 'timeout_seconds': 300}
        self.path = workdir / 'collector.json'
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')
        self.runs = []

    def run(self, mode):
        r = subprocess.run([sys.executable, str(self.code / 'collector' / 'collector.py'), '--config', str(self.path),
                            '--mode', mode], capture_output=True, env=ENV, timeout=300)
        queue = self.dir / 'state' / NAME / 'queue.json'
        self.runs.append((mode, r.returncode, json.loads(queue.read_text()) if queue.exists() else []))
        assert r.returncode == 0, (mode, r.stderr.decode('utf-8', 'replace')[-3000:])

    def archived(self):
        counts = {}
        for f in (self.dir / 'state' / NAME).glob('*.jsonl'):
            for line in f.read_text(encoding='utf-8').splitlines():
                t = json.loads(line)['data']['data_type']
                counts[t] = counts.get(t, 0) + 1
        return counts

    def log(self):
        return (self.dir / 'collector.log').read_text(encoding='utf-8', errors='replace')


def stored_rows(manager, name=NAME):
    status, listing = manager.get('/api/data/' + name, manager.tokens['read'])
    assert status == 200, listing
    out = {}
    for t in listing['data_types']:
        status, body = manager.get('/api/data/%s/%s?limit=100000' % (name, t), manager.tokens['read'])
        assert status == 200, (t, body)
        out[t] = body['rows']
    return out


def runs_in_order(collectors, modes):
    """Every collector runs each mode in turn; 'wait' gives the second metrics run a real window (> 5 s)."""
    for mode in modes:
        if mode == 'wait':
            time.sleep(5.5)
            continue
        for c in collectors:
            c.run(mode)


def new_code_copy(dest):
    """manager/ and dashboard/ of this checkout, copied so their PID files stay out of the checkout."""
    code = dest / 'new-code'
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(code / part), ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return code


def without_payload_time(data):
    return {k: v for k, v in data.items() if k != 'timestamp'}


# --------------------------------------------------------------------------- #
# NEW collector -> OLD and CURRENT Manager                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='module')
def deployed(tmp_path_factory, old_code, current_code):
    """
    The OLD and the CURRENT Manager, each fed by its own NEW collector
    (metrics, metrics 5.5 s later, health, disk, disk), then read through
    their APIs, the NEW Dashboard and the CURRENT Dashboard.
    """
    work = tmp_path_factory.mktemp('mixed')
    tree = work / 'tree'
    make_tree(tree)
    new_code = new_code_copy(work)
    out = {}
    servers = []
    try:
        for ver, code in (('old', old_code), ('current', current_code)):
            d = work / ver
            d.mkdir()
            m = start_manager(code, d / 'data', d, 'manager-' + ver, legacy=(ver == 'old'))
            servers.append(m)
            out[ver] = {'manager': m, 'dir': d, 'collector': Collector(REPO, m, d / 'collector', tree)}
        runs_in_order([v['collector'] for v in out.values()], ['metrics', 'wait', 'metrics', 'health', 'disk', 'disk'])
        for ver, v in out.items():
            m = v['manager']
            v['rows'] = stored_rows(m)
            v['optional'] = {p: m.get(p, m.tokens['read'])[0] for p in
                             ('/api/sessions/all', '/api/storage/health/all', '/api/storage/breakdown/all',
                              '/api/rollups/status')}
            dash = start_dashboard(new_code, m, v['dir'], 'new-dashboard')
            servers.append(dash)
            v['new_dash'] = dash.get('/api/data')
            q = urllib.parse.quote(v['rows']['user_metrics'][0]['username'], safe='')
            v['new_dash_entity'] = dash.get('/api/history/%s/user_metrics/cpu_percent?entity=%s&from=2000-01-01T00:00:00Z'
                                            % (NAME, q))
            v['new_dash_chart'] = dash.get('/api/history/%s/system_metrics/cpu_percent?from=2000-01-01T00:00:00Z'
                                           '&max_points=2000' % NAME)
            dash.stop()
            if ver == 'current':
                dash = start_dashboard(current_code, m, v['dir'], 'current-dashboard')
                servers.append(dash)
                v['current_dash'] = dash.get('/api/data')
                dash.stop()
    finally:
        for s in servers:
            s.stop()
    return out


@pytest.mark.parametrize('ver', ['old', 'current'])
def test_new_collector_batches_are_acknowledged_and_the_queue_drains(deployed, ver):
    v = deployed[ver]
    c = v['collector']
    assert [(mode, rc, len(q)) for mode, rc, q in c.runs] == [
        ('metrics', 0, 0), ('metrics', 0, 0), ('health', 0, 0), ('disk', 0, 0), ('disk', 0, 0)]
    log = c.log()
    assert 'keeping batch queued' not in log and 'ACK stored count mismatch' not in log
    if ver == 'old':                              # no per-message results: the whole batch by its count
        assert log.count('Manager stored all') == 5
    else:
        assert log.count('duplicate 0, rejected 0, error 0') == 5
    # every archived message stored exactly once, in its own (wide) table
    archived = c.archived()
    assert set(archived) == set(NEW_TYPES)
    assert {t: len(rows) for t, rows in v['rows'].items()} == archived
    assert v['optional'] == {'/api/sessions/all': 404, '/api/storage/health/all': 404,
                             '/api/storage/breakdown/all': 404, '/api/rollups/status': 404}
    # the second metrics run carried the interval averages
    second = max(v['rows']['system_metrics'], key=lambda r: r['timestamp'])
    assert second['interval_s'] >= 5 and second['cpu_iowait_percent'] is not None
    assert any(r.get('cpu_seconds') is not None for r in v['rows']['user_metrics'])


@pytest.mark.parametrize('ver', ['old', 'current'])
def test_new_dashboard_on_a_deployed_manager(deployed, ver):
    v = deployed[ver]
    status, data = v['new_dash']
    assert status == 200 and data['degraded'] is False and data['errors'] == []
    assert data['features'] == {'sessions': False, 'storage_health': False, 'breakdown': False, 'usage': False}
    sysd = data['systems'][NAME]
    assert not {'sessions', 'storage_health', 'breakdown', 'usage'} & set(sysd)
    assert sysd['users'] and sysd['metrics']['cpu_percent'] is not None
    # per-user history is refused: the deployed Manager would ignore ?entity and mix every user
    status, body = v['new_dash_entity']
    assert status == 404 and 'newer Manager' in body['error']
    status, body = v['new_dash_chart']
    assert status == 200 and len(body['data']) == 2


@pytest.mark.parametrize('ver', ['old', 'current'])
def test_new_manager_reads_what_a_deployed_manager_stored(deployed, ver, tmp_path):
    v = deployed[ver]
    data = tmp_path / 'data'
    shutil.copytree(str(v['dir'] / 'data'), str(data), ignore=shutil.ignore_patterns('*.lock'))
    new_code = new_code_copy(tmp_path)
    m = start_manager(new_code, data, tmp_path, 'manager-new')
    try:
        after = stored_rows(m)
        deadline = time.time() + 60
        while True:
            status, rs = m.get('/api/rollups/status', m.tokens['read'])
            if all(t['complete'] for t in rs[NAME].values()) or time.time() > deadline:
                break
            time.sleep(0.2)
        status, usage = m.get('/api/user_metrics/usage?window=7d', m.tokens['read'])
        status_h, hourly = m.get('/api/history/%s/system_metrics/disk_read_bytes_per_s?from=2000-01-01T00:00:00Z'
                                 '&resolution=hour' % NAME, m.tokens['read'])
        status_b, breakdown = m.get('/api/storage/breakdown/' + NAME, m.tokens['read'])
    finally:
        m.stop()
    # one pre-upgrade copy of each database it altered
    assert sorted(p.name.split('.pre-upgrade-')[0] for p in data.rglob('*.pre-upgrade-*.bak')) == ['data.db', 'metrics.db']

    def norm(t, rows):
        out = []
        for r in rows:
            r = dict(r)
            if ver == 'old':
                r.pop('msg_id', None)                 # the OLD Manager kept none (N2)
            if t == 'storage_breakdown':              # migrated: JSON text now nested (section 18)
                r = {k: (json.loads(x) if isinstance(x, str) and x[:1] in '[{' else x) for k, x in r.items()}
            out.append(r)
        return sorted(out, key=lambda r: (r['timestamp'], r['id']))

    assert set(after) == set(v['rows'])
    for t in v['rows']:
        assert norm(t, after[t]) == norm(t, v['rows'][t]), t
    assert all(t['complete'] for t in rs[NAME].values()), rs
    assert {'system_metrics', 'user_metrics', 'sessions', 'storage_health'} <= set(rs[NAME])
    assert status == 200 and usage['devices'][NAME]['users']
    assert status_h == 200 and hourly['resolution'] == 'hour' and hourly['data'][0]['count'] >= 1
    # the newest breakdown run measured cold data (the first run could not)
    assert status_b == 200 and all(f['cold_1y_bytes'] is not None for f in breakdown['folders'])


def test_current_dashboard_on_the_new_manager(deployed, current_code, tmp_path):
    """Same data, CURRENT Dashboard: the NEW Manager's answers give the same /api/data as CURRENT's."""
    v = deployed['current']
    data = tmp_path / 'data'
    shutil.copytree(str(v['dir'] / 'data'), str(data), ignore=shutil.ignore_patterns('*.lock'))
    new_code = new_code_copy(tmp_path)
    m = start_manager(new_code, data, tmp_path, 'manager-new')
    try:
        dash = start_dashboard(current_code, m, tmp_path, 'current-dashboard')
        try:
            status, now = dash.get('/api/data')
        finally:
            dash.stop()
    finally:
        m.stop()
    status_before, before = v['current_dash']
    assert status_before == status == 200
    assert now['degraded'] is False and now['errors'] == []
    assert without_payload_time(now) == without_payload_time(before)


# --------------------------------------------------------------------------- #
# CURRENT collector -> NEW Manager                                            #
# --------------------------------------------------------------------------- #

def test_current_collector_into_the_new_manager(current_code, tmp_path):
    tree = tmp_path / 'tree'
    make_tree(tree)
    new_code = new_code_copy(tmp_path)
    m = start_manager(new_code, tmp_path / 'data', tmp_path, 'manager-new')
    try:
        c = Collector(current_code, m, tmp_path / 'collector', tree)
        runs_in_order([c], ['metrics', 'metrics', 'disk'])
        assert [(mode, rc, len(q)) for mode, rc, q in c.runs] == [('metrics', 0, 0), ('metrics', 0, 0), ('disk', 0, 0)]
        assert c.log().count('duplicate 0, rejected 0, error 0') == 3
        rows = stored_rows(m)
        assert {t: len(r) for t, r in rows.items()} == c.archived()
        assert set(rows) == {'system_metrics', 'user_metrics', 'folder_usage'} and len(rows['system_metrics']) == 2
        assert not NEW_USER_KEYS & {k for r in rows['user_metrics'] for k, x in r.items() if x is not None}
        deadline = time.time() + 60
        while True:
            status, rs = m.get('/api/rollups/status', m.tokens['read'])
            if all(t['complete'] for t in rs[NAME].values()) or time.time() > deadline:
                break
            time.sleep(0.2)
        assert set(rs[NAME]) == {'system_metrics', 'user_metrics'} and all(t['complete'] for t in rs[NAME].values())
        status, h = m.get('/api/history/%s/system_metrics/cpu_percent?from=2000-01-01T00:00:00Z&resolution=hour'
                          % NAME, m.tokens['read'])
        assert status == 200 and h['resolution'] == 'hour' and sum(p['count'] for p in h['data']) == 2
        for p in ('/api/sessions/', '/api/storage/health/', '/api/storage/breakdown/'):
            assert m.get(p + NAME, m.tokens['read'])[0] == 404, p
        dash = start_dashboard(new_code, m, tmp_path, 'new-dashboard')
        try:
            status, data = dash.get('/api/data')
        finally:
            dash.stop()
    finally:
        m.stop()
    assert status == 200 and data['degraded'] is False and data['errors'] == []
    sysd = data['systems'][NAME]
    # nothing reported for the new sections: hidden; usage has users but no measured totals
    assert not {'sessions', 'storage_health', 'breakdown'} & set(sysd)
    assert all(u['cpu_core_hours'] is None and u['gpu_busy_hours'] is None for u in sysd['usage']['users'])
    assert not NEW_USER_KEYS & {k for u in sysd['users'] for k in u}
