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

from compat_helpers import old_manager_reads, run_verify, snapshot  # noqa: E402

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


def _verifier(copy_b, dbs):
    import verify_upgrade as vu
    v = vu.Verifier(types.SimpleNamespace())
    v.copy_b, v.b_files_initial = copy_b, set()
    v.inv = {'dbs': {rel: {'kind': 'system', 'tables': tables} for rel, tables in dbs.items()}}
    return v


# --------------------------------------------------------------------------- #
# Units: check_backups                                                        #
# --------------------------------------------------------------------------- #

def test_a_database_that_held_no_table_needs_no_backup(tmp_path):
    copy_b = tmp_path / 'copyB'
    # after the run: the NEW Manager created its tables in the empty data.db
    _db(copy_b / 'ghost_pc' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    v = _verifier(copy_b, {'ghost_pc/data.db': {}})
    v.check_backups()
    assert v.checks['backups']['status'] == 'PASS', v.checks['backups']
    assert v.report['backups']['ghost_pc/data.db']['empty_original'] is True
    assert 'ghost_pc/data.db' in v.checks['backups']['summary']


def test_a_database_that_held_a_table_still_needs_its_backup(tmp_path):
    """The strict rule is unchanged for anything that held data, even an empty table."""
    copy_b = tmp_path / 'copyB'
    _db(copy_b / 'ghost_pc' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    _db(copy_b / 'atlantis' / 'data.db', ['CREATE TABLE system_metrics (id INTEGER PRIMARY KEY, msg_id TEXT)'])
    v = _verifier(copy_b, {'ghost_pc/data.db': {},
                           'atlantis/data.db': {'system_metrics': {'columns': ['id'], 'rows': 0}}})
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
    assert rep['backups']['ghost_pc/data.db'] == {'schema_changed': True, 'backups': [], 'matching_original': [],
                                                  'empty_original': True}
    # ... every database that held data has its backup ...
    assert all(r['matching_original'] for rel, r in rep['backups'].items()
               if not r.get('empty_original') and rel.endswith(('data.db', 'metrics.db')))
    # ... and after the rollback only the answers about ghost-pc's own data differ
    nr = rep['rollback_not_restored']
    assert 'ghost_pc/data.db' in nr['empty_before_upgrade']
    assert nr['answers_showing_new_data'] and set(nr['answers_showing_new_data']) <= {
        '/api/data/ghost_pc', '/api/data/ghost-pc'}
    assert rep['checks']['rollback']['status'] == 'PASS'
