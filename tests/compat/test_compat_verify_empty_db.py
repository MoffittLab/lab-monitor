"""
tools/verify_upgrade.py and an empty <name>/data.db (no table) in the live
data_dir.  The old (f086e7e) Manager creates one whenever something reads a
system that has no history folder: its Dashboard asks
GET /api/data/<name>/system_metrics for every card, so a registered device whose
history database was lost (orphans fixture: ghost-pc) gets one at the first page
load.  The NEW Manager has nothing to migrate or back up in it and creates its
tables there when that system's data arrives; tools/rollback_upgrade.py has no
backup to restore and leaves it as it is.

That used to fail Step 1 twice: 'backups' ("altered by the NEW Manager but has
no pre-upgrade backup") and 'rollback' (the old answers about that system now
show the data stored after the upgrade).  A database that held no table needs
no backup; after the rollback it must be exactly as the NEW Manager left it, and
only the old answers about that system's own data may differ (same HTTP status).
Every other database still needs a backup identical to its original.

The same holds with the review-fix release (CURRENT) as the old side: CURRENT never
creates a data.db on a read, but it keeps an empty one from the f086e7e days as it is
(no table, no backup) until data for that system arrives, so the usage-metrics
upgrade can meet it too.

Only a database with no table at all is exempt.  One whose tables hold no row
(what CURRENT's history retention leaves of a silent system) still changes
schema when the NEW Manager adds the rollup tables, so the NEW Manager backs it up
first and `backups` requires that copy, as for every other database.
"""
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from compat_helpers import build_current_fixture, old_manager_reads, run_verify, snapshot  # noqa: E402

QUICK = ['--window-cells', '5000', '--max-folders', '30']


def _summary(text):
    i = text.find('=' * 78)
    return text[i:] if i >= 0 else text[-6000:]


def _db(path, sql=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    for s in sql:
        conn.execute(s)
    conn.commit()
    conn.close()


def _verifier(copy_b, dbs, old_version='old'):
    import verify_upgrade as vu
    v = vu.Verifier(types.SimpleNamespace())
    v.old_version, v.exact = old_version, old_version == 'current'
    v.copy_b, v.b_files_initial = copy_b, set()
    v.inv = {'dbs': {rel: {'kind': 'system', 'tables': tables} for rel, tables in dbs.items()}}
    return v


# --------------------------------------------------------------------------- #
# Units: check_backups                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('old_version', ['old', 'current'])
def test_a_database_that_held_no_table_needs_no_backup(tmp_path, old_version):
    copy_b = tmp_path / 'copyB'
    # after the run: the NEW Manager created its tables in the empty data.db
    _db(copy_b / 'ghost_pc' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    v = _verifier(copy_b, {'ghost_pc/data.db': {}}, old_version)
    v.check_backups()
    assert v.checks['backups']['status'] == 'PASS', v.checks['backups']
    assert v.report['backups']['ghost_pc/data.db']['empty_original'] is True
    assert 'ghost_pc/data.db' in v.checks['backups']['summary']


@pytest.mark.parametrize('old_version', ['old', 'current'])
def test_a_database_that_held_a_table_still_needs_its_backup(tmp_path, old_version):
    """The strict rule is unchanged for anything that held data, even an empty table."""
    copy_b = tmp_path / 'copyB'
    _db(copy_b / 'ghost_pc' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    _db(copy_b / 'atlantis' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    v = _verifier(copy_b, {'ghost_pc/data.db': {},
                           'atlantis/data.db': {'system_metrics': {'columns': ['id'], 'rows': 0}}}, old_version)
    v.check_backups()
    assert v.checks['backups']['status'] == 'FAIL'
    assert v.checks['backups']['details']['problems'] == [
        'atlantis/data.db was altered by the NEW Manager but has no pre-upgrade backup']


# --------------------------------------------------------------------------- #
# End to end: the orphans data_dir after the old Dashboard showed ghost-pc    #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='session')
def ghost_data_dir(orphans_data_dir, old_code, compat_dir):
    data = compat_dir / 'fixture-empty-db' / 'data'
    shutil.copytree(str(orphans_data_dir), str(data))
    assert not (data / 'ghost_pc').exists()
    old_manager_reads(old_code, data, 'ghost-pc')
    return data


def test_the_old_managers_read_left_an_empty_database(ghost_data_dir):
    db = ghost_data_dir / 'ghost_pc' / 'data.db'
    assert db.is_file() and (ghost_data_dir / 'ghost_pc' / 'data.db-wal').is_file()
    conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def test_verify_passes_with_an_empty_database_of_a_registered_system(ghost_data_dir, old_code, compat_dir):
    work = compat_dir / 'work-empty-db'
    work.mkdir()
    report = compat_dir / 'verify-report-empty-db.json'
    before = snapshot(ghost_data_dir)
    rc, rep, text = run_verify('--data-dir', ghost_data_dir, '--old-code', old_code, '--work-dir', work,
                               '--report', report, *QUICK)
    assert snapshot(ghost_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    # the NEW Manager stored the ingest check's messages for ghost-pc in that database ...
    ghost = rep['backups']['ghost_pc/data.db']
    assert {k: ghost.get(k) for k in ('schema_changed', 'backups', 'matching_original', 'empty_original')} == {
        'schema_changed': True, 'backups': [], 'matching_original': [], 'empty_original': True}
    assert ghost.get('new_backups', []) == []
    # ... every database that held data has its backup ...
    assert all(r['matching_original'] for rel, r in rep['backups'].items()
               if not r.get('empty_original') and rel.endswith(('data.db', 'metrics.db')))
    # ... and after the rollback only the answers about ghost-pc's own data differ
    nr = rep['rollback_not_restored']
    assert 'ghost_pc/data.db' in nr['empty_before_upgrade']
    assert nr['answers_showing_new_data'] and set(nr['answers_showing_new_data']) <= {
        '/api/data/ghost_pc', '/api/data/ghost-pc'}
    assert rep['checks']['rollback']['status'] == 'PASS'


# --------------------------------------------------------------------------- #
# End to end: the same data_dir after the CURRENT release ran on it           #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='session')
def current_ghost_data_dir(ghost_data_dir, current_code, compat_dir):
    """ghost_data_dir upgraded by the CURRENT release and fed by its collectors for a day."""
    out = compat_dir / 'fixture-current-empty-db' / 'data'
    build_current_fixture(current_code, ghost_data_dir, out, '--days', '1')
    return out


def test_the_current_release_keeps_the_empty_database_as_it_is(current_ghost_data_dir):
    d = current_ghost_data_dir / 'ghost_pc'
    conn = sqlite3.connect((d / 'data.db').as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()
    assert not list(d.glob('*.bak'))                  # nothing to back up: CURRENT took no copy either


def test_verify_passes_with_an_empty_database_with_the_current_release_as_the_old_side(
        current_ghost_data_dir, current_code, compat_dir):
    work = compat_dir / 'work-current-empty-db'
    work.mkdir()
    report = compat_dir / 'verify-report-current-empty-db.json'
    before = snapshot(current_ghost_data_dir)
    rc, rep, text = run_verify('--data-dir', current_ghost_data_dir, '--old-code', current_code, '--work-dir', work,
                               '--report', report, *QUICK)
    assert snapshot(current_ghost_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rep['code']['old']['version']['name'] == 'current'
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    ghost = rep['backups']['ghost_pc/data.db']
    assert ghost['schema_changed'] and ghost['empty_original'] and ghost['backups'] == []
    # every database that held data has exactly its one new backup, identical to the original
    assert all(r['matching_original'] and len(r['new_backups']) == 1 for rel, r in rep['backups'].items()
               if r['schema_changed'] and not r.get('empty_original'))
    assert rep['checks']['rollback']['status'] == 'PASS'


# --------------------------------------------------------------------------- #
# End to end: CURRENT data whose tables hold no row                           #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='session')
def current_emptied_data_dir(current_ghost_data_dir, compat_dir):
    """
    current_ghost_data_dir with every row of ws_07/data.db deleted: what CURRENT's
    history retention (history_retention_days) leaves of a system that went silent.
    Its tables are still there.
    """
    data = compat_dir / 'fixture-current-emptied' / 'data'
    shutil.copytree(str(current_ghost_data_dir), str(data), symlinks=True)
    db = data / 'ws_07' / 'data.db'
    conn = sqlite3.connect(str(db))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not r[0].startswith('sqlite_')]
        assert 'system_metrics' in tables, tables
        for t in tables:
            conn.execute('DELETE FROM "%s"' % t)
        conn.commit()
    finally:
        conn.close()
    assert not (data / 'ws_07' / 'data.db-wal').exists()
    return data


def test_verify_passes_when_the_new_manager_backs_up_tables_without_rows(
        current_emptied_data_dir, current_code, compat_dir):
    """
    Adding the rollup tables to ws_07's database changes its schema although no table
    holds a row, so the NEW Manager takes its one backup there too (data_store
    _rollup_backup) and `backups` stays as strict as for every other database.
    """
    work = compat_dir / 'work-current-emptied'
    work.mkdir()
    report = compat_dir / 'verify-report-current-emptied.json'
    before = snapshot(current_emptied_data_dir)
    rc, rep, text = run_verify('--data-dir', current_emptied_data_dir, '--old-code', current_code,
                               '--work-dir', work, '--report', report, *QUICK)
    assert snapshot(current_emptied_data_dir) == before
    assert rep is not None, text[-4000:]
    assert rep['code']['old']['version']['name'] == 'current'
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    ws = rep['backups']['ws_07/data.db']
    assert ws['schema_changed'] and not ws.get('empty_original'), ws
    assert len(ws['new_backups']) == 1 and ws['new_backups'] == ws['matching_original'], ws
    assert rep['checks']['rollback']['status'] == 'PASS'
