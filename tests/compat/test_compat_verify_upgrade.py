"""
Upgrade compatibility: tools/verify_upgrade.py end to end.

The fixture is an OLD-format data_dir written by the OLD code itself
(make_old_fixture.py).  verify_upgrade.py then runs the OLD Manager (git
archive f086e7e) and the NEW Manager (this checkout) on copies of it and
compares every read endpoint, the storage, the totals, a restart, new ingest,
the pre-upgrade backups and the Dashboards.

Also proven here: the source data_dir is left byte-identical (including the
-wal/-shm files a killed Manager leaves behind), and a deliberately broken NEW
read path is reported.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from compat_helpers import OLD_TOKEN, REPO, build_fixture, free_port, run_verify, snapshot  # noqa: E402

# Small windows (several per table) and a folder sample, so the fixture
# exercises windowing and sampling while staying fast.
QUICK = ['--window-cells', '5000', '--max-folders', '30']

EXPECTED_CHECKS = ('copies', 'config', 'storage', 'devices', 'totals', 'endpoints', 'dashboard',
                   'restart', 'ingest', 'legacy-resend', 'backups', 'files', 'rollback', 'source')


def _summary(text):
    """The human-readable summary part of the tool output (for assertion messages)."""
    i = text.find('=' * 78)
    return text[i:] if i >= 0 else text[-6000:]


@pytest.fixture(scope='session')
def verified(old_data_dir, compat_dir):
    """
    One full run, the way the owner runs it: --manager-config of the "live"
    Manager (old-style auth_tokens config), OLD code via git archive, both
    Dashboards.  The data_dir is snapshotted (size, mtime, sha256) around it.
    """
    cfg = compat_dir / 'live-manager-config.json'
    cfg.write_text(json.dumps({'data_dir': str(old_data_dir), 'host': '127.0.0.1', 'port': free_port(),
                               'auth_tokens': [OLD_TOKEN], 'log_level': 'INFO'}), encoding='utf-8')
    work = compat_dir / 'work-main'
    work.mkdir()
    report = compat_dir / 'verify-report.json'
    before = snapshot(old_data_dir)
    rc, data, text = run_verify('--manager-config', cfg, '--work-dir', work, '--report', report,
                                '--with-dashboard', *QUICK)
    after = snapshot(old_data_dir)
    return {'rc': rc, 'report': data, 'text': text, 'before': before, 'after': after, 'work': work}


# --------------------------------------------------------------------------- #
# The fixture really is the old format                                        #
# --------------------------------------------------------------------------- #

def test_fixture_is_a_realistic_old_data_dir(old_data_dir):
    conn = sqlite3.connect(str(old_data_dir / 'metrics.db'))
    try:
        names = {r[0] for r in conn.execute('SELECT name FROM devices')}
        assert {'Atlantis', 'ATLANTIS', 'Vault-2', 'win_srv.lab', 'Lab NAS', 'ws-07'} <= names
        assert conn.execute('SELECT COUNT(DISTINCT username) FROM user_metrics_history').fetchone()[0] >= 6
        cols = {r[1] for r in conn.execute('PRAGMA table_info(device_totals)')}
        assert 'network_last_updated' not in cols          # old schema
        # the case-colliding scan reached only the central snapshot
        snap_ts, folders = conn.execute(
            "SELECT disk_timestamp, folders_json FROM device_snapshot WHERE name = 'Atlantis'").fetchone()
        assert '/volume1/data/project_00' in json.loads(folders) and snap_ts.endswith('T03:00:33Z')
    finally:
        conn.close()

    atl = sqlite3.connect(str(old_data_dir / 'atlantis' / 'data.db'))
    try:
        cols = [r[1] for r in atl.execute('PRAGMA table_info(folder_usage)')]
        assert len(cols) > 200                              # old WIDE layout
        assert '/volume1_total_bytes' in cols and '/volume2/Archive' in cols
        assert 'volume2/Archive' in cols                     # the same path without its leading slash
        assert '/volume1/data/project_00' not in cols        # the old ALTER TABLE failed
        assert not atl.execute("SELECT COUNT(*) FROM folder_usage WHERE timestamp LIKE '%T03:00:33Z'").fetchone()[0]
        first_ts = atl.execute('SELECT timestamp FROM folder_usage ORDER BY id LIMIT 1').fetchone()[0]
        assert len(first_ts) == 20 and first_ts.endswith(':33Z')   # no microseconds
        tables = {r[0] for r in atl.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'folder_usage_msgs' not in tables
        # a folder that appeared on day 1 is NULL in the day-0 row
        first = atl.execute('SELECT "/volume1/Data/new_project" FROM folder_usage ORDER BY id LIMIT 1').fetchone()[0]
        assert first is None
        n = atl.execute('SELECT COUNT(*) FROM system_metrics').fetchone()[0]
        assert n >= 3 * 288
    finally:
        atl.close()

    win = sqlite3.connect(str(old_data_dir / 'win_srv_lab' / 'data.db'))
    try:
        cols = [r[1] for r in win.execute('PRAGMA table_info(folder_usage)')]
        assert any(c.startswith('E:\\Users\\') for c in cols) and 'E:_total_bytes' in cols
        # GPU columns added later: earlier rows NULL / '' (old backfill)
        row = win.execute('SELECT gpu_0_percent, gpu_0_percent_unit FROM system_metrics ORDER BY timestamp LIMIT 1').fetchone()
        assert row == (None, '')
    finally:
        win.close()

    vault = sqlite3.connect(str(old_data_dir / 'vault_2' / 'data.db'))
    try:
        dupes = vault.execute('SELECT COUNT(*) FROM (SELECT timestamp FROM system_metrics '
                              'GROUP BY timestamp HAVING COUNT(*) > 1)').fetchone()[0]
        assert dupes == 2                                   # stored twice after a response timeout
        # a NEW collector's scan reached the OLD Manager: TEXT columns backfilled with '',
        # REAL with NaN (stored as NULL)
        types = {r[1]: r[2] for r in vault.execute('PRAGMA table_info(folder_usage)')}
        assert types['_timed_out'] == 'TEXT' and types['_scan_seconds'] == 'REAL'
        rows = vault.execute('SELECT "_timed_out", "_errors", "_scan_seconds" FROM folder_usage ORDER BY id').fetchall()
        assert rows[0] == ('', '', None)
        assert ('["/volume1/Shares/share-05"]', '[]', 812.4) in rows
        assert vault.execute('SELECT COUNT(*) FROM system_metrics WHERE cpu_percent IS NULL').fetchone()[0] == 1
    finally:
        vault.close()
    assert (old_data_dir / 'Vault-2' / 'usage.jsonl').is_file()          # legacy /api/usage/report
    assert (old_data_dir / 'lab_nas' / 'data.db').is_file()              # "Lab NAS"
    lab = sqlite3.connect(str(old_data_dir / 'lab_nas' / 'data.db'))
    try:
        assert lab.execute('SELECT COUNT(*) FROM "1wire_sensors"').fetchone()[0] == 3
    finally:
        lab.close()


# --------------------------------------------------------------------------- #
# End to end                                                                  #
# --------------------------------------------------------------------------- #

def test_verify_upgrade_passes_on_current_code(verified):
    report = verified['report']
    assert report is not None, verified['text'][-4000:]
    assert verified['rc'] == 0 and report['result'] == 'PASS', _summary(verified['text'])


def test_verify_upgrade_ran_every_check(verified):
    report = verified['report']
    assert report is not None, verified['text'][-4000:]
    checks = report['checks']
    for cid in EXPECTED_CHECKS:
        assert cid in checks, (cid, list(checks))
        assert checks[cid]['status'] in ('PASS', 'WARN'), (cid, checks[cid])
    eps = report['endpoints']
    for pattern in ('/api/devices', '/api/metrics/all', '/api/usage/all', '/api/totals',
                    '/api/user_metrics/all', '/api/user_metrics/<system>',
                    '/api/user_metrics/history/<system>/<user>', '/api/usage/history/<system>',
                    '/api/data/<system>/<data_type> (every row, full range)',
                    '/api/history/<system>/system_metrics/<field> (tail)',
                    '/api/history/<system>/system_metrics/<field> (full range)',
                    '/api/history/<system>/folder_usage/<field> (full range)',
                    '/api/history/<system>/folder_usage/<field> (dashboard max_points)',
                    '/api/history/<system>/user_metrics/<field> (full range)',
                    'dashboard /api/data'):
        assert eps.get(pattern, {}).get('requests', 0) > 0, pattern
    # every system, under its registered name; sampling is logged
    names = {s['name'] for s in report['inventory']['systems']}
    assert {'Atlantis', 'Vault-2', 'win_srv.lab', 'Lab NAS', 'ws-07'} <= names
    atl = report['sampling']['atlantis']
    assert atl['folders_sampled'] == 30 and atl['not_sampled'] > 0
    assert '/volume1' in atl['always_included'] and '/volume1_total_bytes' in atl['always_included']
    # several windows per table were compared
    assert eps['/api/data/<system>/<data_type> (every row, full range)']['requests'] > 15
    # a pre-upgrade backup with the original content for every altered database
    for rel, b in report['backups'].items():
        if b['schema_changed']:
            assert b['matching_original'], (rel, b)
    assert report['code']['old']['source'] == 'git archive'
    # the fixture's quirks were exercised
    intended = {k: v['applied'] for k, v in report['intended_differences'].items()}
    assert intended['N16'] >= 1                         # Atlantis / ATLANTIS: probe under the current spelling
    assert checks['legacy-resend']['details']['gap_filled'], checks['legacy-resend']   # case-colliding scan
    # ... and the old Manager's rows re-sent with a new collector's random msg_ids are duplicates too
    assert checks['legacy-resend']['details']['resent_with_ids'] > 0, checks['legacy-resend']
    assert checks['rollback']['status'] == 'PASS', checks['rollback']
    # numeric folder metadata ("_scan_seconds" from a newer collector) is charted like a volume
    assert '_scan_seconds' in report['sampling']['vault_2']['always_included'], report['sampling']['vault_2']
    # a table whose data_type the new ingest rules refuse is still compared (rows and charts)
    assert report['inventory']['databases']['lab_nas/data.db'].get('1wire_sensors') == 3
    assert eps['/api/history/<system>/1wire_sensors/<field> (full range)']['requests'] > 0


def test_source_data_dir_left_byte_identical(verified):
    assert verified['report'] is not None, verified['text'][-4000:]
    assert verified['before'] == verified['after']
    assert verified['report']['checks']['source']['status'] == 'PASS'
    # nothing of the run is left in the work dir (no --keep-work)
    assert list(verified['work'].iterdir()) == []


def test_source_with_wal_files_of_a_killed_manager_left_byte_identical(old_code, compat_dir):
    """A Manager killed by Task Scheduler leaves data in -wal files: copied, never touched."""
    data = compat_dir / 'fixture-wal' / 'data'
    build_fixture(old_code, data, '--days', '1', '--leave-wal')
    wal_files = sorted(p.relative_to(data).as_posix() for p in data.rglob('*-wal'))
    assert wal_files, 'fixture should leave -wal files'
    # what the databases hold, read from a private copy of db + wal
    expected = {}
    scratch = compat_dir / 'wal-scratch'
    for db in [data / 'metrics.db'] + sorted(data.glob('*/data.db')):
        rel = db.relative_to(data).as_posix()
        t = scratch / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(db, t)
        if (Path(str(db) + '-wal')).exists():
            shutil.copyfile(str(db) + '-wal', str(t) + '-wal')
        conn = sqlite3.connect(str(t))
        try:
            expected[rel] = {r[0]: conn.execute('SELECT COUNT(*) FROM "%s"' % r[0]).fetchone()[0]
                             for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    before = snapshot(data)
    work = compat_dir / 'work-wal'
    work.mkdir()
    report = compat_dir / 'verify-report-wal.json'
    rc, rep, text = run_verify('--data-dir', data, '--old-code', old_code, '--work-dir', work,
                               '--report', report, *QUICK)
    assert snapshot(data) == before
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    # databases with a -wal were copied from a private staged copy (no SQLite open of the source),
    # including what only the -wal holds
    methods = {c['file']: c.get('method') for c in rep['copies']}
    for rel in wal_files:
        assert methods[rel[:-len('-wal')]] == 'staged-wal', methods
    assert rep['inventory']['databases'] == expected


def _broken_new_code(dest):
    """A copy of this checkout's manager/ + dashboard/ with three deliberately broken read paths."""
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', 'logs', 'data')
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(dest / part), ignore=ignore)
    with open(dest / 'manager' / 'data_store.py', 'a', encoding='utf-8') as f:
        f.write('''

# --- deliberately broken read paths (tests/compat) ---
_vu_fetch_rows = TypedDataStore.fetch_rows


def _vu_broken_fetch_rows(self, system_name, data_type, *args, **kwargs):
    rows, truncated = _vu_fetch_rows(self, system_name, data_type, *args, **kwargs)
    if data_type == 'folder_usage':
        for row in rows:
            row.pop('/volume1/homes/user010', None)       # one folder silently lost
    return rows, truncated


TypedDataStore.fetch_rows = _vu_broken_fetch_rows
_vu_series = TypedDataStore.get_field_series


def _vu_broken_series(self, system_name, data_type, field, *args, **kwargs):
    res = _vu_series(self, system_name, data_type, field, *args, **kwargs)
    if res and field == 'ram_percent' and res['points']:
        res['points'] = res['points'][:-1]                 # newest point of a chart missing
    return res


TypedDataStore.get_field_series = _vu_broken_series
''')
    with open(dest / 'manager' / 'metrics.py', 'a', encoding='utf-8') as f:
        f.write('''

# --- deliberately broken read path (tests/compat) ---
_vu_totals = MetricsDB.get_all_totals


def _vu_broken_totals(self):
    out = _vu_totals(self)
    for row in out.values():
        row['total_bytes_in'] = (row.get('total_bytes_in') or 0) + 1    # off by one byte
    return out


MetricsDB.get_all_totals = _vu_broken_totals
''')


def test_detects_a_broken_new_read_path(old_data_dir, old_code, compat_dir):
    new_code = compat_dir / 'broken-new-code'
    _broken_new_code(new_code)
    work = compat_dir / 'work-broken'
    work.mkdir()
    report = compat_dir / 'verify-report-broken.json'
    before = snapshot(old_data_dir)
    rc, rep, text = run_verify('--data-dir', old_data_dir, '--old-code', old_code, '--new-code', new_code,
                               '--work-dir', work, '--report', report, *QUICK)
    assert snapshot(old_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rc == 1 and rep['result'] == 'FAIL', _summary(text)
    diffs = rep['differences']

    def found(endpoint_part, what_part):
        return any(endpoint_part in d['endpoint'] and what_part in d['what'] for d in diffs)

    assert found('/api/totals', 'total_bytes_in'), _summary(text)
    assert rep['checks']['totals']['status'] == 'FAIL'
    assert found('(every row, full range)', 'folder_usage'), _summary(text)
    assert any('/volume1/homes/user010' in d['what'] for d in diffs), _summary(text)
    assert found('system_metrics/<field>', 'ram_percent'), _summary(text)
    # nothing else was flagged as broken
    assert rep['checks']['storage']['status'] == 'PASS'
    assert rep['checks']['backups']['status'] == 'PASS'


def test_detects_a_new_manager_that_rewrites_an_old_row_while_storing_new_data(old_data_dir, old_code, compat_dir):
    """
    Found by the round-3 review: the storage check ran only before the ingest
    and re-send checks.  Here storing a new message rewrites the newest row
    the OLD Manager stored; the comparison (before), the backups and the
    rollback (which restores them) can't see it.
    """
    new_code = compat_dir / 'broken-ingest-rewrite-code'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', 'logs', 'data')
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(new_code / part), ignore=ignore)
    (new_code / 'tools').mkdir()
    shutil.copy2(str(REPO / 'tools' / 'rollback_upgrade.py'), str(new_code / 'tools' / 'rollback_upgrade.py'))
    with open(new_code / 'manager' / 'data_store.py', 'a', encoding='utf-8') as f:
        f.write('''

# --- deliberately broken (tests/compat): storing a new message rewrites the newest older row ---
_vu_store = TypedDataStore.store_message


def _vu_broken_store(self, system_name, msg_id, timestamp, device_type, data_type, data, *a, **kw):
    status = _vu_store(self, system_name, msg_id, timestamp, device_type, data_type, data, *a, **kw)
    if status == 'stored' and data_type == 'system_metrics':
        with self._get_db(system_name).transaction() as conn:
            conn.execute('UPDATE system_metrics SET ram_percent = 1.5 WHERE id = (SELECT id FROM system_metrics '
                         'WHERE msg_id IS NOT ? ORDER BY id DESC LIMIT 1)', (msg_id,))
    return status


TypedDataStore.store_message = _vu_broken_store
''')
    work = compat_dir / 'work-broken-ingest-rewrite'
    work.mkdir()
    report = compat_dir / 'verify-report-broken-ingest-rewrite.json'
    before = snapshot(old_data_dir)
    rc, rep, text = run_verify('--data-dir', old_data_dir, '--old-code', old_code, '--new-code', new_code,
                               '--work-dir', work, '--report', report, *QUICK)
    assert snapshot(old_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rc == 1 and rep['result'] == 'FAIL', _summary(text)
    checks = rep['checks']
    assert checks['storage']['status'] == 'FAIL', checks['storage']
    late = [p for m in checks['storage']['details'].get('more', []) for p in m.get('problems') or []]
    assert any(p.startswith('atlantis/data.db: system_metrics: rows stored before were changed') for p in late), late
    for cid in ('endpoints', 'ingest', 'legacy-resend', 'backups', 'rollback'):
        assert checks[cid]['status'] == 'PASS', (cid, checks[cid])


# --------------------------------------------------------------------------- #
# Orphans: leftovers an old Manager could produce                             #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='session')
def verified_orphans(orphans_data_dir, old_code, compat_dir):
    work = compat_dir / 'work-orphans'
    work.mkdir()
    report = compat_dir / 'verify-report-orphans.json'
    before = snapshot(orphans_data_dir)
    # a small max_points so the Dashboard's "All Available" charts come back bucket-averaged (N17)
    rc, data, text = run_verify('--data-dir', orphans_data_dir, '--old-code', old_code, '--work-dir', work,
                                '--report', report, '--with-dashboard', '--dashboard-max-points', '100', *QUICK)
    return {'rc': rc, 'report': data, 'text': text, 'before': before, 'after': snapshot(orphans_data_dir)}


def test_orphans_variant_is_what_an_old_manager_left(orphans_data_dir):
    d = orphans_data_dir
    assert (d / 'retired_nas' / 'data.db').is_file() and not (d / 'ghost_pc').exists()
    assert (d / 'gone_ws').is_dir() and not (d / 'gone_ws' / 'data.db').exists()
    assert (d / 'typo_host' / 'data.db').is_file()
    conn = sqlite3.connect(str(d / 'metrics.db'))
    try:
        devices = {r[0] for r in conn.execute('SELECT name FROM devices')}
        user_devices = {r[0] for r in conn.execute('SELECT DISTINCT device_name FROM user_metrics_history')}
    finally:
        conn.close()
    assert 'ghost-pc' in devices and not {'retired-nas', 'gone-ws', 'typo-host'} & devices
    assert {'retired-nas', 'gone-ws', 'ghost-pc'} <= user_devices
    tables = sqlite3.connect(str(d / 'typo_host' / 'data.db')).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert tables == 0


def test_orphans_variant_passes(verified_orphans):
    report, text = verified_orphans['report'], verified_orphans['text']
    assert report is not None, text[-4000:]
    assert verified_orphans['rc'] == 0 and report['result'] == 'PASS', _summary(text)
    assert verified_orphans['before'] == verified_orphans['after']
    systems = {(s['name'], s['dir'], s['registered']) for s in report['inventory']['systems']}
    assert ('ghost-pc', None, True) in systems
    assert ('retired-nas', 'retired_nas', False) in systems
    assert ('gone-ws', None, False) in systems
    assert (None, 'typo_host', False) in systems
    eps = report['endpoints']
    assert eps['/api/user_metrics/history/<system>/<user>']['requests'] >= 5
    assert report['intended_differences']['N17']['applied'] > 0          # downsampled charts were checked
    for cid in ('rollback', 'legacy-resend', 'ingest', 'backups'):
        assert report['checks'][cid]['status'] == 'PASS', (cid, report['checks'][cid])


def test_detects_unreadable_orphans_and_metadata(orphans_data_dir, old_code, compat_dir):
    """The two regressions this stage fixed are caught when put back."""
    new_code = compat_dir / 'broken-orphans-code'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', 'logs', 'data')
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(new_code / part), ignore=ignore)
    mgr_py = new_code / 'manager' / 'manager.py'
    src = mgr_py.read_text(encoding='utf-8')
    main_guard = '\nif __name__ == "__main__":'
    assert src.count(main_guard) == 1
    mgr_py.write_text(src.replace(main_guard, '\n\ndef _user_device_known(name):    # per-user rows of deleted devices: 404\n'
                                              '    return _system_known(name)\n\n' + main_guard), encoding='utf-8')
    with open(new_code / 'manager' / 'data_store.py', 'a', encoding='utf-8') as f:
        f.write('\n\nTypedDataStore._folder_meta_key = staticmethod(lambda conn, field: None)\n')
    work = compat_dir / 'work-broken-orphans'
    work.mkdir()
    report = compat_dir / 'verify-report-broken-orphans.json'
    rc, rep, text = run_verify('--data-dir', orphans_data_dir, '--old-code', old_code, '--new-code', new_code,
                               '--work-dir', work, '--report', report, *QUICK)
    assert rep is not None, text[-4000:]
    assert rc == 1 and rep['result'] == 'FAIL', _summary(text)
    diffs = rep['differences']
    assert any(d['endpoint'] == '/api/user_metrics/<system>' and d['system'] == 'gone-ws'
               and '404' in d['what'] for d in diffs), _summary(text)
    assert any('/api/user_metrics/history/' in d['endpoint'] and d['system'] == 'gone-ws' for d in diffs)
    assert any('folder_usage' in d['endpoint'] and '_scan_seconds' in d['what'] for d in diffs), _summary(text)


# --------------------------------------------------------------------------- #
# More than 2000 hours of OLD data: "All Available" answered from rollups     #
# --------------------------------------------------------------------------- #

LONG_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LONG_HOURS = 2403          # 2403 hour buckets: merged two at a time, the last one alone

# stdin: queue bodies; the OLD Manager stores them through its own route
_OLD_INGEST = r'''
import json, logging, sys
sys.path.insert(0, sys.argv[1])
import manager as m
job = json.load(sys.stdin)
m.init_app({"data_dir": job["data_dir"], "auth_tokens": [job["token"]]}, logging.getLogger("old"))
client = m.app.test_client()
for body in job["bodies"]:
    r = client.post("/api/data/queue", json=body, headers={"Authorization": "Bearer " + job["token"]})
    assert r.status_code == 200 and r.get_json()["stored"] == len(body["messages"]), r.get_data()
'''


def _long_history_messages():
    """
    LongNas: a first test run at 00:01, the 5-minute collector from 01:00 to
    01:55, then one run an hour at hh:10 (the newest at 02:10 on day 100):
    the first and the last rollup bucket are partly filled.
    """
    stamps = [LONG_START + timedelta(minutes=1)] + [LONG_START + timedelta(hours=1, minutes=5 * i) for i in range(12)]
    stamps += [LONG_START + timedelta(hours=h, minutes=10) for h in range(2, LONG_HOURS)]
    msgs = []
    for i, t in enumerate(stamps):
        h = {'device_name': 'LongNas', 'device_id': 'id-long', 'device_type': 'synology',
             'timestamp': t.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}
        msgs.append({'header': h, 'data': {'data_type': 'system_metrics', 'cpu_percent': float(i % 97),
                                           'cpu_percent_unit': '%', 'ram_percent': 20.0 + i % 50,
                                           'uptime_seconds': 1000 + i * 3600, 'network_bytes_in': 10 ** 6 * i,
                                           'network_bytes_out': 10 ** 5 * i}})
        for u in ('alice', 'bob'):
            msgs.append({'header': h, 'data': {'data_type': 'user_metrics', 'username': u,
                                               'cpu_percent': float(i % 41), 'ram_bytes': 10 ** 8 + i,
                                               'ram_formatted': 'x'}})
    return [{'queue_id': 'LongNas-%05d' % n, 'name': 'LongNas', 'id': 'id-long', 'messages': msgs[i:i + 600]}
            for n, i in enumerate(range(0, len(msgs), 600))]


def test_verify_passes_on_more_than_2000_hours_of_old_data(old_code, compat_dir):
    """
    About 100 days: the Dashboard's "All Available" query (2000 points) is
    answered from hourly rollups merged two hours a point.  Its first and
    last points stay within the OLD series' first and last value (N17).
    """
    data = compat_dir / 'fixture-long' / 'data'
    data.mkdir(parents=True)
    proc = subprocess.run([sys.executable, '-c', _OLD_INGEST, str(old_code / 'manager')],
                          input=json.dumps({'data_dir': str(data), 'token': OLD_TOKEN,
                                            'bodies': _long_history_messages()}),
                          capture_output=True, text=True, timeout=600,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert proc.returncode == 0, proc.stderr[-4000:]
    before = snapshot(data)
    work = compat_dir / 'work-long'
    work.mkdir()
    report = compat_dir / 'verify-report-long.json'
    rc, rep, text = run_verify('--data-dir', data, '--old-code', old_code, '--work-dir', work, '--report', report)
    assert snapshot(data) == before
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    assert rep['intended_differences']['N17']['applied'] >= 4

    # what the NEW Manager answered: hourly rollups, inside the values' range
    copy = compat_dir / 'fixture-long-new' / 'data'
    shutil.copytree(str(data), str(copy))
    sys.path.insert(0, str(REPO / 'manager'))
    try:
        import manager as mgr
        mgr.init_app({'data_dir': str(copy), 'read_tokens': ['rd-' + 'b2' * 16]}, logging.getLogger('test-long'))
        try:
            assert mgr._data_store.run_rollup_backfill('longnas')
            r = mgr.app.test_client().get(
                '/api/history/LongNas/system_metrics/cpu_percent',
                query_string={'from': '2000-01-01T00:00:00Z', 'to': '9999-12-31T23:59:59Z', 'max_points': 2000},
                headers={'Authorization': 'Bearer ' + 'rd-' + 'b2' * 16})
            h = r.get_json()
        finally:
            mgr._close_stores()
    finally:
        sys.path.remove(str(REPO / 'manager'))
    assert (h['resolution'], h['bucket_seconds']) == ('hour', 7200) and len(h['data']) <= 2000
    first = (LONG_START + timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    last = (LONG_START + timedelta(hours=LONG_HOURS - 1, minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    assert first <= h['data'][0]['timestamp'] and h['data'][-1]['timestamp'] == last
    assert sum(p['count'] for p in h['data']) == LONG_HOURS + 11


# --------------------------------------------------------------------------- #
# Users who logged off (spec D17): hidden by the NEW Dashboard (N20)          #
# --------------------------------------------------------------------------- #

LOGOFF_START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _logoff_messages():
    """
    LogoffNas: 6 h of 5-minute runs.  alice and carol report every run; bob
    logs off after 2 h, so his snapshot row (the old Manager keeps it for
    ever) is 4 h older than the system's latest system_metrics.  User rows
    carry their own timestamps, a moment after the run's system_metrics.
    """
    msgs = []
    for i in range(72):
        t = LOGOFF_START + timedelta(minutes=5 * i, seconds=7)
        h = {'device_name': 'LogoffNas', 'device_id': 'id-logoff', 'device_type': 'NAS',
             'timestamp': t.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}
        msgs.append({'header': h, 'data': {
            'data_type': 'system_metrics', 'cpu_percent': 10.0 + i % 7, 'cpu_percent_unit': '%',
            'ram_percent': 40.0, 'ram_percent_unit': '%', 'total_ram_bytes': 8 * 2 ** 30,
            'uptime_seconds': 1000 + 300 * i, 'network_bytes_in': 10 ** 6 * i, 'network_bytes_out': 10 ** 5 * i,
            'gpus': [], 'gpu_count': 0}})
        uh = dict(h, timestamp=(t + timedelta(microseconds=50)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
        for u in ['alice', 'carol'] + (['bob'] if i < 24 else []):
            msgs.append({'header': uh, 'data': {'data_type': 'user_metrics', 'username': u, 'cpu_percent': 1.5,
                                                'ram_bytes': 2 ** 28, 'ram_formatted': '256.0 MB'}})
    return [{'queue_id': 'LogoffNas-%05d' % n, 'name': 'LogoffNas', 'id': 'id-logoff', 'messages': msgs[i:i + 100]}
            for n, i in enumerate(range(0, len(msgs), 100))]


@pytest.fixture(scope='session')
def logoff_data_dir(old_code, compat_dir):
    """An OLD data_dir, written by the OLD Manager's own route, with a user who logged off 4 h earlier."""
    data = compat_dir / 'fixture-logoff' / 'data'
    data.mkdir(parents=True)
    proc = subprocess.run([sys.executable, '-c', _OLD_INGEST, str(old_code / 'manager')],
                          input=json.dumps({'data_dir': str(data), 'token': OLD_TOKEN,
                                            'bodies': _logoff_messages()}),
                          capture_output=True, text=True, timeout=600,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert proc.returncode == 0, proc.stderr[-4000:]
    return data


def test_verify_passes_when_the_new_dashboard_hides_a_logged_off_user(logoff_data_dir, old_code, compat_dir):
    """Without N20 this run FAILs: $.systems.LogoffNas.users.length old 3 new 2."""
    conn = sqlite3.connect(str(logoff_data_dir / 'metrics.db'))
    try:
        snap = dict(conn.execute("SELECT username, timestamp FROM device_user_snapshot WHERE name = 'LogoffNas'"))
        metrics_ts = conn.execute("SELECT metrics_timestamp FROM device_snapshot WHERE name = 'LogoffNas'").fetchone()[0]
    finally:
        conn.close()
    import verify_upgrade as vu
    assert (vu.parse_ts(metrics_ts) - vu.parse_ts(snap['bob'])).total_seconds() > 3 * 3600
    before = snapshot(logoff_data_dir)
    work = compat_dir / 'work-logoff'
    work.mkdir()
    report = compat_dir / 'verify-report-logoff.json'
    rc, rep, text = run_verify('--data-dir', logoff_data_dir, '--old-code', old_code, '--work-dir', work,
                               '--report', report, '--with-dashboard')
    assert snapshot(logoff_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    assert rep['checks']['dashboard']['status'] == 'PASS', rep['checks']['dashboard']
    assert rep['intended_differences']['N20']['applied'] == 1          # bob, and only bob


def test_a_hidden_user_who_is_still_logged_in_is_reported(logoff_data_dir, old_code, compat_dir):
    """N20 accepts only stale rows: a NEW Dashboard that also drops a current user FAILs."""
    new_code = compat_dir / 'broken-logoff-code'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', 'logs', 'data')
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(new_code / part), ignore=ignore)
    app_py = new_code / 'dashboard' / 'app.py'
    src = app_py.read_text(encoding='utf-8')
    main_guard = '\nif __name__ == "__main__":'
    assert src.count(main_guard) == 1
    app_py.write_text(src.replace(main_guard, '\n_vu_drop = _drop_stale_users\n\n\n'
                                              'def _drop_stale_users(users, metrics_ts):    # alice is current\n'
                                              "    return [u for u in _vu_drop(users, metrics_ts) if u.get('username') != 'alice']\n\n"
                                  + main_guard), encoding='utf-8')
    work = compat_dir / 'work-broken-logoff'
    work.mkdir()
    report = compat_dir / 'verify-report-broken-logoff.json'
    rc, rep, text = run_verify('--data-dir', logoff_data_dir, '--old-code', old_code, '--new-code', new_code,
                               '--work-dir', work, '--report', report, '--with-dashboard')
    assert rep is not None, text[-4000:]
    assert rc == 1 and rep['result'] == 'FAIL', _summary(text)
    assert rep['checks']['dashboard']['status'] == 'FAIL'
    diffs = [d for d in rep['differences'] if d['endpoint'] == 'dashboard /api/data']
    assert any('LogoffNas.users' in d['what'] for d in diffs), _summary(text)
    assert rep['intended_differences']['N20']['applied'] == 1          # bob is still accepted
    for cid in ('storage', 'endpoints', 'backups'):
        assert rep['checks'][cid]['status'] == 'PASS', (cid, rep['checks'][cid])


# --------------------------------------------------------------------------- #
# A newer collector that reported to the OLD Manager (N21)                    #
# --------------------------------------------------------------------------- #

def _new_collector_messages():
    """NewNas: three runs of a usage-metrics collector (spec Appendix A payloads), the last 2 h ago."""
    sys.path.insert(0, str(REPO / 'tests' / 'manager'))
    sys.path.insert(0, str(REPO / 'manager'))
    try:
        from test_manager_new_types import APPENDIX_A
    finally:
        sys.path.remove(str(REPO / 'tests' / 'manager'))
        sys.path.remove(str(REPO / 'manager'))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    msgs = []
    for i in range(3):
        t = now - timedelta(hours=4 - i)
        h = {'device_name': 'NewNas', 'device_id': 'id-newnas', 'device_type': 'synology',
             'timestamp': t.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}
        for payload in APPENDIX_A:
            data = json.loads(json.dumps(payload))
            if data['data_type'] == 'system_metrics':
                data.update(cpu_percent=20.0 + i, uptime_seconds=10 ** 6 + 3600 * i,
                            network_bytes_in=10 ** 9 + i, network_bytes_out=10 ** 8 + i)
            msgs.append({'header': dict(h, msg_id='%032x' % len(msgs)), 'data': data})
    return [{'queue_id': 'NewNas-00001', 'name': 'NewNas', 'id': 'id-newnas', 'messages': msgs}]


def test_verify_passes_on_data_a_newer_collector_sent_to_the_old_manager(old_code, compat_dir):
    """
    Without N21 this run FAILs: storage "table storage_breakdown is gone", the
    item-11 keys in /api/metrics/* and the Dashboard, and the decoded
    storage_breakdown rows in /api/data.
    """
    data = compat_dir / 'fixture-newcollector' / 'data'
    data.mkdir(parents=True)
    proc = subprocess.run([sys.executable, '-c', _OLD_INGEST, str(old_code / 'manager')],
                          input=json.dumps({'data_dir': str(data), 'token': OLD_TOKEN,
                                            'bodies': _new_collector_messages()}),
                          capture_output=True, text=True, timeout=600,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert proc.returncode == 0, proc.stderr[-4000:]
    conn = sqlite3.connect(str(data / 'newnas' / 'data.db'))
    try:
        assert conn.execute('SELECT COUNT(*) FROM storage_breakdown').fetchone()[0] == 3
    finally:
        conn.close()
    before = snapshot(data)
    work = compat_dir / 'work-newcollector'
    work.mkdir()
    report = compat_dir / 'verify-report-newcollector.json'
    rc, rep, text = run_verify('--data-dir', data, '--old-code', old_code, '--work-dir', work,
                               '--report', report, '--with-dashboard')
    assert snapshot(data) == before
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    assert rep['intended_differences']['N21']['applied'] > 10
    assert 'storage_breakdown_legacy' in rep['checks']['storage']['summary']
    assert 'rollup_backfill' in rep['timings_seconds']


def test_refuses_a_work_dir_inside_the_data_dir(old_data_dir, compat_dir):
    data = compat_dir / 'fixture-copy' / 'data'
    shutil.copytree(str(old_data_dir), str(data))
    before = snapshot(data)
    rc, _rep, text = run_verify('--data-dir', data, '--work-dir', data / 'verify-work')
    assert rc == 2, text[-2000:]
    assert 'must not be inside the data_dir' in text
    assert snapshot(data) == before


# --------------------------------------------------------------------------- #
# Normalizer units                                                            #
# --------------------------------------------------------------------------- #

def test_timestamps_compare_as_instants():
    import verify_upgrade as vu
    assert vu.canon_ts('2026-08-01T00:00:00Z') == '2026-08-01T00:00:00.000000Z'
    assert vu.canon_ts('2026-08-01T02:00:00+02:00') == '2026-08-01T00:00:00.000000Z'
    assert vu.canon_ts('2026-08-01 00:00:00.5') == '2026-08-01T00:00:00.500000Z'
    assert vu.canon_ts('not a time') == 'not a time'


def test_folder_rows_drop_unmeasured_keys_only():
    import verify_upgrade as vu
    old = {'id': 3, 'timestamp': '2026-08-01T02:00:41Z', '/volume1/a': 1234.0, '/volume1/gone': None,
           '_unit': 'bytes', 'folders': ''}
    new = {'id': 3, 'msg_id': None, 'timestamp': '2026-08-01T02:00:41.000000Z', '/volume1/a': 1234,
           '_unit': 'bytes'}
    assert vu.norm_row(old, 'folder_usage') == vu.norm_row(new, 'folder_usage')
    # a measured zero is NOT dropped
    assert vu.norm_row(dict(old, **{'/volume1/a': 0.0}), 'folder_usage') != vu.norm_row(new, 'folder_usage')
    # outside folder_usage nulls are compared
    assert vu.norm_row({'a': None}, 'system_metrics') != vu.norm_row({}, 'system_metrics')


def test_old_history_rules_from_rows():
    import verify_upgrade as vu
    rows = [{'timestamp': '2026-08-01T00:00:00Z', '/volume1': 5.0},
            {'timestamp': '2026-08-02T00:00:00Z', '/volume1': None},
            {'timestamp': '2026-08-03T00:00:00Z', '/volume1': '7'},
            {'timestamp': '2026-08-04T00:00:00Z', '/volume1': 'bytes'}]
    assert vu.old_series_from_rows(rows, 'volume1') == [
        ('2026-08-01T00:00:00.000000Z', 5), ('2026-08-03T00:00:00.000000Z', 7)]


def test_limit_cut_inside_equal_timestamps_is_accepted():
    import verify_upgrade as vu
    a = [('t1', 1), ('t2', 2), ('t3', 3)]
    b = [('t1', 9), ('t2', 2), ('t3', 3)]
    assert vu.cut_equal(a, b, 3, lambda p: p[0])
    assert not vu.cut_equal(a, [('t1', 1), ('t2', 5), ('t3', 3)], 3, lambda p: p[0])
    assert not vu.cut_equal(a, b, 4, lambda p: p[0])      # not cut: must be identical


def test_backup_file_matching():
    import verify_upgrade as vu
    assert vu._backup_belongs('atlantis/data.db.pre-upgrade-20261121T151159Z.bak', 'atlantis/data.db')
    assert vu._backup_belongs('metrics.db.pre-upgrade-20261121T151159Z.bak', 'metrics.db')
    assert not vu._backup_belongs('vault_2/data.db.pre-upgrade-1.bak', 'atlantis/data.db')
    assert not vu._backup_belongs('atlantis/data.db.pre-upgrade-1.bak', 'metrics.db')


def test_formatted_sizes_compare_as_numbers():
    import verify_upgrade as vu
    assert vu.size_text_value('58.45 GB') == vu.size_text_value('58.45 GiB')          # same 1024-based number
    assert vu.size_text_value('-20650519212851.00 B') == vu.size_text_value('-18.78 TiB')
    assert vu.size_text_value('58.45 GiB') != vu.size_text_value('59.45 GiB')
    assert vu.size_text_value('14d 23h') is None                                       # compared as text


def test_downsampled_chart_must_cover_the_whole_range():
    import verify_upgrade as vu
    old = [('2026-08-%02dT00:00:00.000000Z' % d, float(d)) for d in range(1, 31)]
    st = vu.series_stats_of(old)
    good = [('2026-08-02T12:00:00.000000Z', 2.5), ('2026-08-29T12:00:00.000000Z', 29.5)]
    assert vu.downsampled_problem(good, 10, st) is None
    assert 'starts at' in vu.downsampled_problem([('2026-08-20T00:00:00.000000Z', 20.0)] + good[1:], 10, st)
    assert 'ends at' in vu.downsampled_problem(good[:1] + [('2026-08-10T00:00:00.000000Z', 10.0)], 10, st)
    assert 'range' in vu.downsampled_problem([(good[0][0], 0.5), good[1]], 10, st)
    assert vu.downsampled_problem([], 10, st) == 'no points'
    # a rollup answer's point covers bucket_seconds (whole hours / days merged): within one point
    late = [('2026-08-05T00:00:00.000000Z', 4.5)] + good[1:]
    assert 'starts at' in vu.downsampled_problem(late, 10, st)
    assert vu.downsampled_problem(late, 10, st, 5 * 86400) is None
    assert 'starts at' in vu.downsampled_problem(late, 10, st, 3 * 86400)
    assert vu.rollup_point_seconds({'resolution': 'hour', 'bucket_seconds': 7200}) == 7200
    assert vu.rollup_point_seconds({'resolution': 'raw', 'bucket_seconds': 7200}) is None
    assert vu.rollup_point_seconds(None) is None


def test_a_rollup_answer_cannot_widen_its_own_tolerance():
    """N17: bucket_seconds is capped at what the planner can produce, so a chart cut short at the end is reported."""
    import verify_upgrade as vu
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = [(vu.canon_ts((t0 + timedelta(minutes=5 * i)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')), 10.0 + i % 11)
           for i in range(120 * 288)]                                   # 120 days of 5-minute points
    st, mp = vu.series_stats_of(old), 2000

    def answer(lost_days, hours=2):
        end, t, pts = t0 + timedelta(days=120 - lost_days), t0, []
        while t < end:
            pts.append(((t + timedelta(hours=hours / 2)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'), 15.0))
            t += timedelta(hours=hours)
        return vu.sort_points(vu.norm_points([{'timestamp': a, 'value': b} for a, b in pts]))

    def problem(pts, claimed):
        return vu.downsampled_problem(pts, mp, st, vu.rollup_point_seconds(
            {'resolution': 'hour', 'bucket_seconds': claimed}, st, mp))
    # 120 days of hourly buckets at 2000 points: two hours a point
    assert vu.rollup_point_seconds({'resolution': 'hour', 'bucket_seconds': 30 * 86400}, st, mp) == 7200
    assert vu.rollup_point_seconds({'resolution': 'day', 'bucket_seconds': 86400}, st, mp) == 86400
    assert problem(answer(0), 7200) is None
    for lost, claimed in ((3, 7200), (3, 4 * 86400), (20, 30 * 86400)):
        assert 'ends at' in problem(answer(lost), claimed), (lost, claimed)
    # without the OLD series the answer's own width is all there is (as before)
    assert vu.rollup_point_seconds({'resolution': 'hour', 'bucket_seconds': 30 * 86400}) == 30 * 86400


class _FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def get(self, path, params=None, token='default'):
        assert path == '/api/rollups/status'
        self.calls += 1
        status, data = self.answers[min(self.calls, len(self.answers)) - 1]
        return type('R', (), {'status': status, 'data': data})()


def test_compare_waits_for_the_rollup_backfill(monkeypatch):
    """The restart check must not see long windows switch from raw rows to rollups (backfill done in between)."""
    import types
    import verify_upgrade as vu
    monkeypatch.setattr(vu.time, 'sleep', lambda s: None)
    pending = {'atlantis': {'system_metrics': {'complete': True}, 'user_metrics': {'complete': False}}}
    done = {'atlantis': {'system_metrics': {'complete': True}, 'user_metrics': {'complete': True}}}

    def run(answers, timeout=60):
        warnings = []
        me = types.SimpleNamespace(new=_FakeHttp(answers), args=types.SimpleNamespace(rollup_timeout=timeout),
                                   warn=warnings.append)
        vu.Verifier.wait_for_rollups(me)
        return me.new.calls, warnings
    assert run([(200, pending), (200, pending), (200, done)]) == (3, [])
    assert run([(200, {})]) == (1, [])                                  # rollups off, or no rolled-up table
    assert run([(404, None)]) == (1, [])                                # a NEW Manager without rollups
    calls, warnings = run([(200, pending)], timeout=0)
    assert calls == 1 and 'not complete' in warnings[0] and '1 tables pending' in warnings[0]


def test_n21_accepts_only_what_the_stored_message_holds():
    import verify_upgrade as vu
    raw = {'cpu_percent': 5.0, 'interval_s': 300.2, 'cpu_count': 16, 'disk_busy_max_name': 'sda',
           'disk_await_ms': 1.5}
    old = {'cpu_percent': 5.0}
    new = {'cpu_percent': 5.0, 'interval_s': 300.2, 'cpu_count': 16.0, 'disk_busy_max_name': 'sda',
           'disk_await_ms': 9.9, 'gpu_sample_seconds': 0.2, 'surprise': 1}
    applied = vu.Applied()
    vu.passthrough_added(old, new, raw, applied)
    # equal to the message: accepted; a different value, a key the message lacks, an unknown key: not
    assert old == {'cpu_percent': 5.0, 'interval_s': 300.2, 'cpu_count': 16.0, 'disk_busy_max_name': 'sda'}
    assert applied.counts['N21'] == 3
    # JSON text of the old wide storage_breakdown table, decoded only there and only to a list / object
    assert vu.decode_json_text('[1, {"a": 2}]') == [1, {'a': 2}] and vu.decode_json_text('{}') == {}
    for text in ('"x"', '12', '[broken', 'plain', None, 5):
        assert vu.decode_json_text(text) is None
    row = {'id': 1, 'timestamp': '2026-08-01T00:00:00Z', 'folders': '[{"path": "/v"}]', 'note': '[x'}
    assert vu.norm_row(row, 'storage_breakdown')['folders'] == [{'path': '/v'}]
    assert vu.norm_row(row, 'sessions')['folders'] == '[{"path": "/v"}]'
    assert vu.norm_row(row, 'storage_breakdown')['note'] == '[x'


def test_n21_passthrough_keys_are_the_managers():
    import verify_upgrade as vu
    sys.path.insert(0, str(REPO / 'manager'))
    try:
        import metrics
    finally:
        sys.path.remove(str(REPO / 'manager'))
    assert vu.NEW_METRICS_PASSTHROUGH == tuple(metrics.SYSTEM_METRICS_EXTRA) + ('disk_busy_max_name',)


def test_logged_off_users_are_only_the_stale_rows_missing_from_the_new_dashboard():
    import verify_upgrade as vu
    metrics = {'timestamp': '2026-08-01T06:00:07.000000Z'}

    def payload(*users):
        return {'systems': {'Nas': {'metrics': dict(metrics), 'users': [dict(u) for u in users]}}}

    stale = {'username': 'bob', 'timestamp': '2026-08-01T02:00:07Z'}                  # 4 h older
    edge = {'username': 'dan', 'timestamp': '2026-08-01T05:58:07.000000Z'}             # exactly 120 s: shown
    recent = {'username': 'erin', 'timestamp': '2026-08-01T05:59:00Z'}                 # 67 s: shown
    no_ts = {'username': 'fay'}
    bad_ts = {'username': 'gus', 'timestamp': 'yesterday'}
    old = payload(stale, edge, recent, no_ts, bad_ts, {'username': 'alice', 'timestamp': metrics['timestamp']})
    assert vu.logged_off_users(old, payload()) == {'Nas': {('bob', stale['timestamp'])}}
    # still listed by the NEW Dashboard: compared as usual, not accepted
    assert vu.logged_off_users(old, payload(stale)) == {}
    # no readable metrics timestamp: nothing is accepted
    no_metrics = payload(stale)
    no_metrics['systems']['Nas']['metrics'] = None
    assert vu.logged_off_users(no_metrics, payload()) == {}
    # a system the NEW Dashboard does not list at all is reported elsewhere, not here
    assert vu.logged_off_users(old, {'systems': {}}) == {}
    assert vu.logged_off_users(None, None) == {}


def test_n20_uses_the_dashboard_stale_user_threshold():
    import verify_upgrade as vu
    src = (REPO / 'dashboard' / 'app.py').read_text(encoding='utf-8')
    m = re.search(r'^STALE_USER_SECONDS\s*=\s*(\d+)\s*$', src, re.M)
    assert m and int(m.group(1)) == vu.NEW_STALE_USER_SECONDS
