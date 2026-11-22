"""
Upgrade compatibility: tools/verify_upgrade.py for the next real upgrade,
CURRENT (the review-fix release, fb6498c) -> NEW (this checkout).

The fixture is the data_dir a server has after the CURRENT release upgraded
its OLD data and ran for a while (make_current_fixture.py: the CURRENT
Manager and collector code write it).  verify_upgrade.py then runs the
CURRENT Manager (the old side, detected from its code) and the NEW Manager
on copies of it, with the expectations for a CURRENT old side: every CURRENT
table keeps every row, every CURRENT endpoint answers the same, re-sends of
stored messages are duplicates, one pre-upgrade copy per changed database,
and the CURRENT Manager started on the migrated data (the documented way
back) changes nothing and answers as before.

Also proven here: deliberately broken NEW Managers (one CURRENT row
rewritten and no backup, CURRENT rows / msg_ids / a dead letter hidden, a
table CURRENT would migrate on the way back, stored rows rewritten when a
message is re-sent, a trigger CURRENT can't run so it can no longer store
new data on the way back) are reported, a trigger or index added without a
backup counts as a change, and an old side that does not match the
data_dir's layout is refused.
OLD -> NEW stays covered by test_compat_verify_upgrade.py.
"""
import json
import os
import shutil
import sqlite3
import stat
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from compat_helpers import REPO, build_current_fixture, run_verify, snapshot  # noqa: E402

QUICK = ['--window-cells', '5000', '--max-folders', '30']
EXPECTED_CHECKS = ('copies', 'storage', 'devices', 'totals', 'endpoints', 'dashboard', 'restart', 'ingest',
                   'legacy-resend', 'backups', 'files', 'rollback', 'source')
# what the CURRENT release already does itself: never normalized with it as the old side
OLD_ONLY = ('N2', 'N5', 'N7', 'N8', 'N11', 'N13')


def _summary(text):
    i = text.find('=' * 78)
    return text[i:] if i >= 0 else text[-6000:]


def _tables(path):
    conn = sqlite3.connect('file:%s?mode=ro&immutable=1' % path, uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.fixture(scope='session')
def verified_current(current_data_dir, current_code, compat_dir):
    work = compat_dir / 'work-current'
    work.mkdir()
    report = compat_dir / 'verify-report-current.json'
    before = snapshot(current_data_dir)
    rc, data, text = run_verify('--data-dir', current_data_dir, '--old-code', current_code, '--work-dir', work,
                                '--report', report, '--with-dashboard', *QUICK)
    return {'rc': rc, 'report': data, 'text': text, 'before': before, 'after': snapshot(current_data_dir)}


# --------------------------------------------------------------------------- #
# The fixture is what the CURRENT release leaves                              #
# --------------------------------------------------------------------------- #

def test_current_fixture_is_what_the_review_fix_release_leaves(current_data_dir):
    d = current_data_dir
    central = sqlite3.connect('file:%s?mode=ro&immutable=1' % (d / 'metrics.db'), uri=True)
    try:
        assert central.execute('SELECT COUNT(*) FROM dead_letter').fetchone()[0] == 2
        cols = {r[1] for r in central.execute('PRAGMA table_info(device_totals)')}
        assert 'network_last_updated' in cols
    finally:
        central.close()
    # the CURRENT upgrade's own pre-upgrade copies, kept next to every database it migrated
    assert list((d / 'atlantis').glob('data.db.pre-upgrade-*.bak'))
    assert list(d.glob('metrics.db.pre-upgrade-*.bak')) and list(d.glob('ws_07/data.db.pre-upgrade-*.bak'))
    assert not list(d.glob('nas_3/*.bak'))                     # first seen by CURRENT: never backed up
    atl = d / 'atlantis' / 'data.db'
    assert {'folder_usage_msgs', 'folder_usage_items', 'folder_usage_legacy'} <= _tables(atl)
    assert 'folder_usage' not in _tables(atl)
    conn = sqlite3.connect('file:%s?mode=ro&immutable=1' % atl, uri=True)
    try:
        # rows from before the CURRENT upgrade (msg_id NULL) and its own (collector msg_ids)
        nulls, ids = conn.execute('SELECT SUM(msg_id IS NULL), SUM(msg_id IS NOT NULL) FROM system_metrics').fetchone()
        assert nulls > 500 and ids > 500
        assert conn.execute("SELECT COUNT(*) FROM folder_usage_msgs WHERE meta_json LIKE '%_volumes%'").fetchone()[0]
    finally:
        conn.close()
    lab = sqlite3.connect('file:%s?mode=ro&immutable=1' % (d / 'lab_nas' / 'data.db'), uri=True)
    try:
        # an OLD collector kept sending to CURRENT: ids the Manager derived ("legacy-<sha1>")
        assert lab.execute("SELECT COUNT(*) FROM system_metrics WHERE msg_id LIKE 'legacy-%'").fetchone()[0] > 100
        assert lab.execute("SELECT COUNT(*) FROM \"1wire_sensors\" WHERE msg_id LIKE 'legacy-%'").fetchone()[0] == 2
    finally:
        lab.close()
    vault = sqlite3.connect('file:%s?mode=ro&immutable=1' % (d / 'vault_2' / 'data.db'), uri=True)
    try:
        # a batch re-sent after its answer timed out was a duplicate: stored once
        assert vault.execute('SELECT COUNT(*) FROM (SELECT msg_id FROM system_metrics WHERE msg_id IS NOT NULL '
                             'GROUP BY timestamp HAVING COUNT(*) > 1)').fetchone()[0] == 0
    finally:
        vault.close()
    if sys.platform.startswith('linux'):
        assert (d / 'manager.lock').is_file() and (d / 'cur_linux' / 'data.db').is_file()


# --------------------------------------------------------------------------- #
# CURRENT -> NEW end to end                                                   #
# --------------------------------------------------------------------------- #

def test_verify_current_to_new_passes(verified_current):
    report, text = verified_current['report'], verified_current['text']
    assert report is not None, text[-4000:]
    assert verified_current['rc'] == 0 and report['result'] == 'PASS', _summary(text)
    assert report['code']['old']['version']['name'] == 'current'
    assert report['data_layout']['layout'] == 'current'
    for cid in EXPECTED_CHECKS:
        assert report['checks'][cid]['status'] in ('PASS', 'WARN'), (cid, report['checks'][cid])
    assert verified_current['before'] == verified_current['after']


def test_verify_current_compares_every_current_endpoint(verified_current):
    report = verified_current['report']
    assert report is not None, verified_current['text'][-4000:]
    eps = report['endpoints']
    for pattern in ('/api/dead_letter', '/api/usage/history/<system>', '/api/data/<system>',
                    '/api/data/<system>/<data_type> (every row, full range)',
                    '/api/history/<system>/folder_usage/<field> (full range)',
                    '/api/history/<system>/folder_usage/<field> (tail)',
                    '/api/history/<system>/system_metrics/<field> (dashboard max_points)',
                    '/api/history/<system>/1wire_sensors/<field> (full range)',
                    '/api/user_metrics/history/<system>/<user>', 'dashboard /api/data',
                    'dashboard /api/history/<system>/folder_usage/<field>'):
        assert eps.get(pattern, {}).get('requests', 0) > 0, pattern
    # the storage internals are not data types
    assert not [p for p in eps if 'folder_usage_msgs' in p or 'folder_usage_legacy' in p], sorted(eps)
    # nothing CURRENT already does itself was normalized away
    applied = {k: v['applied'] for k, v in report['intended_differences'].items()}
    assert all(applied[k] == 0 for k in OLD_ONLY), applied
    assert 'normalizations' not in report['checks']
    assert report['intended_differences']['N22']['old_sides'] == ['current']


def test_verify_current_resends_stored_messages_with_their_own_ids(verified_current):
    report = verified_current['report']
    assert report is not None, verified_current['text'][-4000:]
    details = report['checks']['legacy-resend']['details']
    resent = details['resent']
    for kind in ('own msg_id', 'OLD-style, the id derived from it', 'OLD-style, no msg_id', 'random msg_id'):
        assert resent.get(kind, 0) > 0, resent


def test_verify_current_backups_and_the_way_back(verified_current):
    report = verified_current['report']
    assert report is not None, verified_current['text'][-4000:]
    backups = report['backups']
    changed = [rel for rel, b in backups.items() if b['schema_changed']]
    assert 'metrics.db' in changed and 'nas_3/data.db' in changed
    for rel in changed:
        # one pre-upgrade copy per changed database, identical to the original
        assert len(backups[rel]['new_backups']) == 1, (rel, backups[rel])
        assert backups[rel]['new_backups'] == backups[rel]['matching_original'], (rel, backups[rel])
    back = report['back_to_current']
    assert back['replayed'] > 500 and back['new_backups'] == [] and back['changed_tables'] == []
    assert back['not_shown_N22'] == 0
    # ... and stores and serves new data there, of every data type and from a usage-metrics collector too
    assert back['new_messages_stored'] == back['new_messages_sent'] > 20
    # the original rows were checked again after the ingest and re-send checks
    assert 'after ingest and re-sends: the rows stored before are unchanged' in report['checks']['storage']['summary']


# --------------------------------------------------------------------------- #
# A NEW collector already reported to CURRENT (the rollout order forbids it)  #
# --------------------------------------------------------------------------- #

def test_verify_current_to_new_passes_after_a_new_collector_reported(old_data_for_current, current_code,
                                                                     compat_dir):
    from test_compat_verify_upgrade import _new_collector_messages
    extra = compat_dir / 'current-newcollector-messages.json'
    extra.write_text(json.dumps(_new_collector_messages()), encoding='utf-8')
    data = compat_dir / 'fixture-current-newcollector' / 'data'
    build_current_fixture(current_code, old_data_for_current, data, '--extra-messages', extra)
    assert 'storage_breakdown' in _tables(data / 'newnas' / 'data.db')
    work = compat_dir / 'work-current-newcollector'
    work.mkdir()
    report = compat_dir / 'verify-report-current-newcollector.json'
    rc, rep, text = run_verify('--data-dir', data, '--work-dir', work, '--old-code', current_code,
                               '--report', report, '--with-dashboard', *QUICK)
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    assert 'storage_breakdown_legacy' in rep['checks']['storage']['summary']
    assert rep['intended_differences']['N21']['applied'] > 0
    # back on CURRENT the moved storage_breakdown runs are not shown, and only those (N22)
    assert rep['back_to_current']['not_shown_N22'] > 0
    assert rep['intended_differences']['N22']['applied'] == rep['back_to_current']['not_shown_N22']


# --------------------------------------------------------------------------- #
# Deliberately broken NEW Managers                                            #
# --------------------------------------------------------------------------- #

def _new_code_copy(dest):
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', 'logs', 'data')
    for part in ('manager', 'dashboard'):
        shutil.copytree(str(REPO / part), str(dest / part), ignore=ignore)


def _run_broken(current_data_dir, current_code, compat_dir, label, patch):
    new_code = compat_dir / ('broken-current-' + label)
    _new_code_copy(new_code)
    with open(new_code / 'manager' / 'data_store.py', 'a', encoding='utf-8') as f:
        f.write(patch)
    work = compat_dir / ('work-broken-current-' + label)
    work.mkdir()
    report = compat_dir / ('verify-report-broken-current-%s.json' % label)
    before = snapshot(current_data_dir)
    rc, rep, text = run_verify('--data-dir', current_data_dir, '--old-code', current_code, '--new-code', new_code,
                               '--work-dir', work, '--report', report, *QUICK)
    assert snapshot(current_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rc == 1 and rep['result'] == 'FAIL', _summary(text)
    return rep, text


def test_detects_a_new_manager_that_rewrites_a_current_row_without_a_backup(current_data_dir, current_code,
                                                                             compat_dir):
    rep, text = _run_broken(current_data_dir, current_code, compat_dir, 'rewrite', '''

# --- deliberately broken (tests/compat): no backup of a data.db, one CURRENT row rewritten ---
_vu_backup = backup_database


def backup_database(conn, db_path, *args, **kwargs):
    if str(db_path).endswith('data.db'):
        return None
    return _vu_backup(conn, db_path, *args, **kwargs)


_vu_prepare = TypedDataStore.prepare_existing


def _vu_broken_prepare(self, *args, **kwargs):
    out = _vu_prepare(self, *args, **kwargs)
    conn = sqlite3.connect(os.path.join(self.data_dir, 'atlantis', 'data.db'), timeout=60)
    try:
        conn.execute("UPDATE system_metrics SET ram_percent = ram_percent + 1 WHERE id = "
                     "(SELECT MAX(id) FROM system_metrics WHERE msg_id IS NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    return out


TypedDataStore.prepare_existing = _vu_broken_prepare
''')
    checks = rep['checks']
    assert checks['storage']['status'] == 'FAIL', checks['storage']
    assert any('atlantis/data.db: system_metrics rows changed' in p for p in checks['storage']['details']['problems'])
    assert checks['backups']['status'] == 'FAIL' and 'no pre-upgrade backup' in checks['backups']['summary']
    assert checks['endpoints']['status'] == 'FAIL'
    assert any(d['system'] in ('Atlantis', 'ATLANTIS') and 'system_metrics' in d['what'] and 'ram_percent' in d['what']
               for d in rep['differences']), _summary(text)
    assert checks['rollback']['status'] == 'FAIL'           # CURRENT on the migrated data: that row differs


def test_detects_a_new_manager_that_hides_current_rows(current_data_dir, current_code, compat_dir):
    """Rows an OLD collector sent to CURRENT, the msg_id of user rows (N2 does not apply), a dead letter."""
    rep, text = _run_broken(current_data_dir, current_code, compat_dir, 'hide', '''

# --- deliberately broken read paths (tests/compat) ---
_vu_fetch_rows = TypedDataStore.fetch_rows


def _vu_broken_fetch_rows(self, system_name, data_type, *args, **kwargs):
    rows, truncated = _vu_fetch_rows(self, system_name, data_type, *args, **kwargs)
    if data_type == 'system_metrics':
        rows = [r for r in rows if not str(r.get('msg_id') or '').startswith('legacy-')]
    if data_type == 'user_metrics':
        for r in rows:
            r.pop('msg_id', None)
    return rows, truncated


TypedDataStore.fetch_rows = _vu_broken_fetch_rows

import metrics as _vu_metrics                           # noqa: E402
_vu_dead = _vu_metrics.MetricsDB.get_dead_letters


def _vu_broken_dead(self, *args, **kwargs):
    return _vu_dead(self, *args, **kwargs)[1:]


_vu_metrics.MetricsDB.get_dead_letters = _vu_broken_dead
''')
    diffs = rep['differences']
    assert rep['checks']['endpoints']['status'] == 'FAIL'
    assert any(d['system'] == 'Lab NAS' and 'system_metrics' in d['what'] and 'only in OLD' in d['what']
               for d in diffs), _summary(text)
    assert any('user_metrics' in d['what'] and 'msg_id' in d['what'] for d in diffs), _summary(text)
    assert any(d['endpoint'] == '/api/dead_letter' for d in diffs), _summary(text)
    assert rep['intended_differences']['N2']['applied'] == 0
    for cid in ('storage', 'backups', 'legacy-resend', 'rollback'):
        assert rep['checks'][cid]['status'] == 'PASS', (cid, rep['checks'][cid])


def test_detects_a_new_manager_the_review_fix_release_cannot_go_back_to(current_data_dir, current_code,
                                                                       compat_dir):
    """A table CURRENT does not reserve, so it migrates it (ALTER TABLE ... msg_id) on the way back."""
    rep, text = _run_broken(current_data_dir, current_code, compat_dir, 'noway', '''

# --- deliberately broken (tests/compat): a new table outside the reserved prefixes ---
_vu_prepare = TypedDataStore.prepare_existing


def _vu_broken_prepare(self, *args, **kwargs):
    out = _vu_prepare(self, *args, **kwargs)
    for key in self.list_systems():
        conn = sqlite3.connect(self._db_path(key), timeout=60)
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS usage_cache (id INTEGER PRIMARY KEY, timestamp TEXT)')
            conn.commit()
        finally:
            conn.close()
    return out


TypedDataStore.prepare_existing = _vu_broken_prepare
''')
    back = rep['checks']['rollback']
    assert back['status'] == 'FAIL', back
    assert any('changed the migrated data' in p and 'usage_cache' in p for p in back['details']['problems']), back
    assert 'atlantis/data.db usage_cache' in rep['back_to_current']['changed_tables']
    # the original tables themselves are intact
    assert rep['checks']['storage']['status'] == 'PASS'


def test_detects_a_new_manager_that_rewrites_stored_rows_when_a_message_is_resent(current_data_dir, current_code,
                                                                                  compat_dir):
    """
    Found by the round-3 review: the storage check ran only before the ingest
    and re-send checks, and those compared statuses and row counts, so a
    duplicate that rewrites the row it duplicates gave PASS.
    """
    rep, text = _run_broken(current_data_dir, current_code, compat_dir, 'resend-rewrite', '''

# --- deliberately broken (tests/compat): a re-sent duplicate rewrites the stored row ---
_vu_store = TypedDataStore.store_message


def _vu_broken_store(self, system_name, msg_id, timestamp, device_type, data_type, data, *a, **kw):
    status = _vu_store(self, system_name, msg_id, timestamp, device_type, data_type, data, *a, **kw)
    if status == 'duplicate' and data_type == 'system_metrics':
        with self._get_db(system_name).transaction() as conn:
            conn.execute('UPDATE system_metrics SET cpu_percent = cpu_percent + 1 WHERE msg_id = ?', (msg_id,))
    return status


TypedDataStore.store_message = _vu_broken_store
''')
    checks = rep['checks']
    assert checks['storage']['status'] == 'FAIL', checks['storage']
    late = [p for m in checks['storage']['details'].get('more', []) for p in m.get('problems') or []]
    assert any(p.startswith('atlantis/data.db: system_metrics: rows stored before were changed') for p in late), late
    assert any('the duplicate re-send changed the stored message' in p
               for p in checks['ingest']['details']['problems']), checks['ingest']
    # the comparison before the writes, the backups and the way back (copy C) are fine
    for cid in ('endpoints', 'backups', 'rollback'):
        assert checks[cid]['status'] == 'PASS', (cid, checks[cid])


def test_detects_a_new_manager_the_review_fix_release_cannot_store_new_data_on(current_data_dir, current_code,
                                                                                compat_dir):
    """
    Found by the round-3 review: a trigger calling a function only the NEW
    Manager's connections have.  Every read on the way back still answers as
    before, but CURRENT rejects every new system_metrics message (and the
    collector then drops it).
    """
    rep, text = _run_broken(current_data_dir, current_code, compat_dir, 'trigger', '''

# --- deliberately broken (tests/compat): a trigger only the NEW Manager's connections can run ---
_vu_register = _register_functions


def _register_functions(conn):
    _vu_register(conn)
    conn.create_function('vu_rollup_hook', 0, lambda: 1)


_vu_prepare = TypedDataStore.prepare_existing


def _vu_broken_prepare(self, *args, **kwargs):
    out = _vu_prepare(self, *args, **kwargs)
    for key in self.list_systems():
        conn = sqlite3.connect(self._db_path(key), timeout=60)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'system_metrics'").fetchone():
                conn.execute('CREATE TRIGGER IF NOT EXISTS vu_hook AFTER INSERT ON system_metrics '
                             'BEGIN SELECT vu_rollup_hook(); END')
                conn.commit()
        finally:
            conn.close()
    return out


TypedDataStore.prepare_existing = _vu_broken_prepare
''')
    back = rep['checks']['rollback']
    assert back['status'] == 'FAIL', back
    assert any('a new system_metrics message was answered rejected' in p for p in back['details']['problems']), back
    assert rep['back_to_current']['changed_tables'] == []         # the replayed reads alone saw nothing
    assert 0 < rep['back_to_current']['new_messages_stored'] < rep['back_to_current']['new_messages_sent']
    for cid in ('storage', 'endpoints', 'ingest', 'legacy-resend'):
        assert rep['checks'][cid]['status'] == 'PASS', (cid, rep['checks'][cid])


def _verifier_for_backups(tmp_path, change):
    """A Verifier whose copy B holds one metrics.db that `change` altered after the inventory."""
    import verify_upgrade as vu
    a, b = tmp_path / 'A', tmp_path / 'B'
    for d in (a, b):
        d.mkdir()
        conn = sqlite3.connect(str(d / 'metrics.db'))
        conn.execute('CREATE TABLE devices (name TEXT PRIMARY KEY, last_seen TEXT)')
        conn.execute("INSERT INTO devices VALUES ('Atlantis', '2026-11-01T00:00:00.000000Z')")
        conn.commit()
        conn.close()
    v = vu.Verifier(types.SimpleNamespace(no_hash=False))
    v.exact = True
    v.copy_b = b
    v.inv = {'dbs': {'metrics.db': vu.inventory_db(a / 'metrics.db', 'central', 5000, True, layout='current')}}
    v.b_files_initial = vu.list_files(b)
    if change:
        conn = sqlite3.connect(str(b / 'metrics.db'))
        conn.execute(change)
        conn.commit()
        conn.close()
    v.check_backups()
    return v


@pytest.mark.parametrize('change', [
    None,
    'CREATE TRIGGER vu_hook AFTER INSERT ON devices BEGIN SELECT 1; END',
    'CREATE INDEX idx_devices_seen ON devices (last_seen)',
    'CREATE VIEW vu_devices AS SELECT name FROM devices',
])
def test_an_index_trigger_or_view_without_a_backup_is_a_change(tmp_path, change):
    """Found by the round-3 review: only table names and columns decided whether a backup was needed."""
    v = _verifier_for_backups(tmp_path, change)
    if change is None:
        assert v.checks['backups']['status'] == 'PASS', v.checks['backups']
        assert v.report['backups']['metrics.db']['schema_changed'] is False
    else:
        assert v.checks['backups']['status'] == 'FAIL', v.checks['backups']
        assert 'altered by the NEW Manager but has no pre-upgrade backup' in v.checks['backups']['summary']
        assert v.report['backups']['metrics.db']['indexes_triggers_views_changed']


def test_central_rows_may_change_only_for_the_devices_sent_new_data():
    import verify_upgrade as vu
    from collections import Counter
    before = {'device_totals': Counter({('Atlantis', "('Atlantis', 1)"): 1, ('ATLANTIS', "('ATLANTIS', 7)"): 1}),
              'notes': Counter({(None, "('x',)"): 1})}
    moved = dict(before, device_totals=Counter({('Atlantis', "('Atlantis', 2)"): 1, ('ATLANTIS', "('ATLANTIS', 7)"): 1}))
    assert vu.Verifier._central_changes(before, moved, {'Atlantis'}) == []
    # another spelling of the same device (N16) is another device here
    assert vu.Verifier._central_changes(before, moved, {'ATLANTIS'}) == ["device_totals: the row of 'Atlantis' changed"]
    assert vu.Verifier._central_changes(before, moved, set()) == ["device_totals: the row of 'Atlantis' changed"]
    # a row without a device name, added or removed rows
    gone = dict(before, notes=Counter())
    assert vu.Verifier._central_changes(before, gone, {'Atlantis'}) == ["notes: the row of None changed"]
    added = dict(before, devices=Counter({('ws-07', "('ws-07',)"): 1}))
    assert vu.Verifier._central_changes(before, added, {'Atlantis'}) == ["devices: the row of 'ws-07' changed"]


def test_only_the_first_rows_are_the_rows_stored_before(tmp_path):
    """table_hash(limit=n): rows appended later don't change it, a changed earlier row does (also WITHOUT ROWID)."""
    import verify_upgrade as vu
    conn = sqlite3.connect(str(tmp_path / 'd.db'))
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v REAL)')
    conn.execute('CREATE TABLE items (msg_row_id INTEGER NOT NULL, key TEXT NOT NULL, value NUMERIC, '
                 'PRIMARY KEY (msg_row_id, key)) WITHOUT ROWID')
    conn.executemany('INSERT INTO t (v) VALUES (?)', [(1.5,), (2.5,)])
    conn.executemany('INSERT INTO items VALUES (?, ?, ?)', [(1, '/b', 1), (1, '/a', 2), (2, '/a', 3)])
    want = {t: vu.table_hash(conn, t, cols) for t, cols in (('t', ['id', 'v']), ('items', ['msg_row_id', 'key', 'value']))}
    conn.execute('INSERT INTO t (v) VALUES (3.5)')
    conn.executemany('INSERT INTO items VALUES (?, ?, ?)', [(3, '/0', 9), (3, '/z', 9)])
    for t, cols in (('t', ['id', 'v']), ('items', ['msg_row_id', 'key', 'value'])):
        assert vu.table_hash(conn, t, cols, limit=want[t][1]) == want[t]
        assert vu.table_hash(conn, t, cols)[1] == want[t][1] + (1 if t == 't' else 2)
    conn.execute('UPDATE t SET v = 2.75 WHERE id = 2')
    conn.execute("UPDATE items SET value = 5 WHERE msg_row_id = 1 AND key = '/b'")
    for t, cols in (('t', ['id', 'v']), ('items', ['msg_row_id', 'key', 'value'])):
        assert vu.table_hash(conn, t, cols, limit=want[t][1]) != want[t]
    conn.close()


# --------------------------------------------------------------------------- #
# The old side must match the data_dir                                        #
# --------------------------------------------------------------------------- #

def test_an_old_side_that_does_not_match_the_data_dir_is_refused(current_data_dir, old_data_dir, old_code,
                                                                 current_code, compat_dir):
    for label, data, code, expect in (('old-on-current', current_data_dir, old_code, 'written by the review-fix'),
                                      ('current-on-old', old_data_dir, current_code, 'still in the layout')):
        work = compat_dir / ('work-mismatch-' + label)
        work.mkdir()
        before = snapshot(data)
        rc, rep, text = run_verify('--data-dir', data, '--old-code', code, '--work-dir', work,
                                   '--report', compat_dir / ('verify-report-mismatch-%s.json' % label))
        assert rc == 2 and rep['result'] == 'ERROR', (label, _summary(text))
        assert expect in rep['error'], (label, rep['error'])
        assert snapshot(data) == before


# --------------------------------------------------------------------------- #
# Units                                                                       #
# --------------------------------------------------------------------------- #

def test_old_version_is_detected_from_the_code(old_code, current_code):
    import verify_upgrade as vu
    assert vu.detect_old_version(old_code) == 'old'
    assert vu.detect_old_version(current_code) == 'current'
    assert vu.detect_old_version(REPO) == 'usage-metrics'


def test_every_intended_difference_names_its_old_sides():
    import verify_upgrade as vu
    assert set(vu.INTENDED_SIDES) == set(vu.INTENDED)
    assert all(set(sides) <= set(vu.OLD_VERSIONS) and sides for sides in vu.INTENDED_SIDES.values())
    assert all('old' in vu.INTENDED_SIDES[k] for k in vu.INTENDED if k != 'N22')     # the OLD side is unchanged
    assert vu.INTENDED_SIDES['N22'] == ('current',)
    applied = vu.Applied()
    applied.side = 'current'
    applied.add('N2')
    applied.add('N21')
    assert applied.counts == {'N21': 1} and applied.disallowed == {'N2': 1}


def test_a_disallowed_intended_difference_is_a_difference():
    import verify_upgrade as vu
    v = vu.Verifier(types.SimpleNamespace())
    v.applied.side = v.old_version = 'current'
    v.intended('/api/history/<system>/system_metrics/<field> (tail)', 'N8')
    v.intended('/api/history/<system>/user_metrics/<field> (tail)', 'N18')
    assert [d['what'] for d in v.differences] == [
        'intended difference N8 (history tail queries: newest N measured points) does not apply with current '
        'as the old side']
    assert v.endpoints['/api/history/<system>/user_metrics/<field> (tail)']['intended'] == {'N18': 1}


def test_the_running_managers_lock_file_is_never_read(tmp_path):
    """On Windows the live Manager's locked byte can't be read: an unreadable manager.lock stands in for it."""
    import verify_upgrade as vu
    src = tmp_path / 'data'
    src.mkdir()
    sqlite3.connect(str(src / 'metrics.db')).close()
    lock = src / 'manager.lock'
    lock.write_bytes(b'\npid=4242 started=2026-11-21T10:00:00Z\n')
    lock.chmod(0)
    try:
        if os.access(str(lock), os.R_OK):
            pytest.skip('running as a user who can read any file')
        fp = vu.fingerprint(str(src))
        assert fp['files']['manager.lock']['sha256'] is None and 'error' not in fp['files']['manager.lock']
        staging = tmp_path / 'staging'
        staging.mkdir()
        copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(staging))
        assert {'file': 'manager.lock', 'kind': 'lock', 'method': 'not copied'} in copied
        assert not (tmp_path / 'copy' / 'manager.lock').exists() and (tmp_path / 'copy' / 'metrics.db').exists()
    finally:
        lock.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_collectors_lock_files_are_never_read(tmp_path):
    """A collector sharing the data_dir holds queue.json.lock / perf_snapshot.json.lock while it runs."""
    import verify_upgrade as vu
    src = tmp_path / 'data'
    (src / 'atlantis').mkdir(parents=True)
    sqlite3.connect(str(src / 'metrics.db')).close()
    (src / 'atlantis' / '2026-11.jsonl').write_text('{}\n')
    locks = [src / 'atlantis' / 'queue.json.lock', src / 'atlantis' / 'perf_snapshot.json.lock']
    for lock in locks:
        lock.write_bytes(b'')
        lock.chmod(0)
    try:
        if os.access(str(locks[0]), os.R_OK):
            pytest.skip('running as a user who can read any file')
        fp = vu.fingerprint(str(src))
        staging = tmp_path / 'staging'
        staging.mkdir()
        copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(staging))
        for rel in ('atlantis/queue.json.lock', 'atlantis/perf_snapshot.json.lock'):
            assert fp['files'][rel]['sha256'] is None and 'error' not in fp['files'][rel]
            assert {'file': rel, 'kind': 'lock', 'method': 'not copied'} in copied
            assert not (tmp_path / 'copy' / rel).exists()
        # data files next to them are still hashed and copied
        assert fp['files']['atlantis/2026-11.jsonl']['sha256']
        assert (tmp_path / 'copy' / 'atlantis' / '2026-11.jsonl').read_text() == '{}\n'
    finally:
        for lock in locks:
            lock.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_n22_accepts_only_storage_breakdown_runs_that_are_no_longer_shown():
    import verify_upgrade as vu
    v = vu.Verifier(types.SimpleNamespace())
    v.breakdown_migrated = {'newnas'}
    listing = lambda x: {'data_types': sorted((x or {}).get('data_types') or [])}    # noqa: E731
    rows = lambda x: (x or {}).get('rows') or []                                      # noqa: E731
    tail = lambda x: [None, None, vu.points_of(x)]                                     # noqa: E731
    old_types = ['storage_breakdown', 'system_metrics']

    def rec(normalize, kind, data_type=None, d='newnas', answer=None):
        meta = {'dir': d, 'kind': kind, 'data_type': data_type}
        if answer is not None:
            meta['answer'] = answer
        return {'status': 200, 'normalize': normalize, 'meta': meta}

    def resp(data, status=200):
        return vu.Resp(status, data, '', 0.0)

    gone = resp({'data_types': ['system_metrics']})
    assert v._not_shown_breakdown(rec(listing, 'listing', answer={'data_types': old_types}), gone)
    # another type missing too, a system whose table was not moved, an error: not accepted
    assert not v._not_shown_breakdown(rec(listing, 'listing', answer={'data_types': old_types}),
                                      resp({'data_types': []}))
    assert not v._not_shown_breakdown(rec(listing, 'listing', d='atlantis', answer={'data_types': old_types}), gone)
    assert not v._not_shown_breakdown(rec(rows, 'rows', 'storage_breakdown'), resp(None, 404))
    assert v._not_shown_breakdown(rec(rows, 'rows', 'storage_breakdown'), resp({'rows': []}))
    assert not v._not_shown_breakdown(rec(rows, 'rows', 'storage_breakdown'), resp({'rows': [{'id': 1}]}))
    assert not v._not_shown_breakdown(rec(rows, 'rows', 'system_metrics'), resp({'rows': []}))
    assert v._not_shown_breakdown(rec(tail, 'tail', 'storage_breakdown'), resp({'data': []}))
    assert not v._not_shown_breakdown(rec(tail, 'tail', 'storage_breakdown'),
                                      resp({'data': [{'timestamp': '2026-08-01T00:00:00Z', 'value': 1}]}))
