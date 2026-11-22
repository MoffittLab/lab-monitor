"""
tools/verify_upgrade.py and what the review-fix Manager leaves in a data_dir:
its nightly snapshots' <data_dir>/snapshot-status.json and
<data_dir>/.snapshot-staging/ (manager/snapshot.py), and its lock file
<data_dir>/manager.lock (manager/storage_util.py InstanceLock, never deleted).

The old side of this release is always f086e7e, which takes no snapshots, so
these files appear in the live data_dir only after the review-fix Manager ran
on it with snapshots on: for example after a rollback to f086e7e (they stay,
the old Manager never touches them) before a second upgrade attempt.  A
review-fix Manager killed while it staged a database leaves a half-written
copy with a hot rollback journal, which a read-only SQLite connection can't
open ("attempt to write a readonly database"): copying it made the whole run
fail.  Neither file holds data: neither is copied, staging files are
fingerprinted by size and mtime only, and changes to them count like the live
Manager's database files (WARN while a live Manager answers /health, FAIL
otherwise).  The lock file holds no data either and the NEW Manager on copy B
writes its own: it is not copied (copying it made the 'files' check FAIL) and
is fingerprinted by size and mtime; the old Manager never writes it, so a
change to it during the run stays a FAIL.
"""
import json
import os
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from compat_helpers import (build_fixture, leave_killed_staging, leave_manager_lock, run_verify,  # noqa: E402
                            snapshot)

QUICK = ['--window-cells', '5000', '--max-folders', '30']
STAMP = '20261122T033000Z'


def _summary(text):
    i = text.find('=' * 78)
    return text[i:] if i >= 0 else text[-6000:]


def _snapshot_layout(root):
    """A data_dir with a snapshot status file and a killed snapshot's staging leftover."""
    src = root / 'data'
    (src / 'atlantis').mkdir(parents=True)
    for db in (src / 'metrics.db', src / 'atlantis' / 'data.db'):
        conn = sqlite3.connect(str(db))
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        conn.executemany('INSERT INTO t (v) VALUES (?)', [('x' * 300,) for _ in range(3000)])
        conn.commit()
        conn.close()
    (src / 'snapshot-status.json').write_text('{"last_success": "2026-11-22T03:30:41Z"}', encoding='utf-8')
    staged = src / '.snapshot-staging' / STAMP / 'atlantis' / 'data.db'
    leave_killed_staging(src / 'atlantis' / 'data.db', staged)
    return src, staged


# --------------------------------------------------------------------------- #
# Units                                                                       #
# --------------------------------------------------------------------------- #

def test_a_killed_snapshots_staging_files_are_never_copied(tmp_path):
    import verify_upgrade as vu
    src, staged = _snapshot_layout(tmp_path)
    (tmp_path / 'staging').mkdir()
    copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(tmp_path / 'staging'))
    assert {'file': '.snapshot-staging', 'kind': 'scratch', 'method': 'not copied'} in copied
    assert {'file': 'snapshot-status.json', 'kind': 'status', 'method': 'not copied'} in copied
    assert not [c for c in copied if c['file'].startswith('.snapshot-staging/')]
    assert sorted(os.listdir(str(tmp_path / 'copy'))) == ['atlantis', 'metrics.db']
    assert vu.quick_check(str(tmp_path / 'copy' / 'atlantis' / 'data.db')) == 'ok'
    # the leftover is untouched (its hot journal is still there), and only its size and mtime are recorded
    assert Path(str(staged) + '-journal').stat().st_size > 0
    fp = vu.fingerprint(str(src))
    rel = staged.relative_to(src).as_posix()
    assert fp['files'][rel]['sha256'] is None and fp['files'][rel + '-journal']['sha256'] is None
    assert fp['files'][rel]['size'] == staged.stat().st_size
    assert fp['files']['snapshot-status.json']['sha256']
    # ... and not counted in the space the copies need
    v = vu.Verifier(types.SimpleNamespace(ignore_space=True))
    v.work = tmp_path
    v.space_check(fp)
    scratch = {k: f['size'] for k, f in fp['files'].items() if k.startswith('.snapshot-staging/')}
    assert sum(scratch.values()) > 0
    assert v.report['space']['source_bytes'] == sum(f['size'] for f in fp['files'].values()) - sum(scratch.values())
    assert v.report['space']['database_bytes'] == sum(
        f['size'] for k, f in fp['files'].items() if k.endswith('.db') and k not in scratch)


def test_a_staging_file_that_vanishes_while_it_is_fingerprinted_is_left_out(tmp_path, monkeypatch):
    """A running snapshot removes its staged copies: not an unreadable file. Any other file still is."""
    import verify_upgrade as vu
    src, staged = _snapshot_layout(tmp_path)
    real = vu.os.walk

    def walk(top, *a, **kw):
        # each listing names one file more, removed before it is read
        for dirpath, dirnames, filenames in real(top, *a, **kw):
            yield dirpath, dirnames, filenames + ['gone.db']
    monkeypatch.setattr(vu.os, 'walk', walk)
    fp = vu.fingerprint(str(src))
    monkeypatch.undo()
    stage_dir = staged.parent.relative_to(src).as_posix()
    assert stage_dir + '/data.db' in fp['files'] and stage_dir + '/gone.db' not in fp['files']
    assert '.snapshot-staging/gone.db' not in fp['files']
    assert 'FileNotFoundError' in fp['files']['gone.db']['error']
    assert 'FileNotFoundError' in fp['files']['atlantis/gone.db']['error']


@pytest.mark.parametrize('live', [True, False])
def test_a_snapshot_the_live_manager_takes_during_the_run(tmp_path, live):
    """
    A review-fix Manager's snapshot rewrites snapshot-status.json and creates and removes
    staging files.  That is the Manager's own writing, like its databases: WARN while it
    answers /health, and still FAIL when nothing should be running.  Anything else
    changed is always a FAIL.
    """
    import verify_upgrade as vu
    src, staged = _snapshot_layout(tmp_path)
    (src / 'Vault-2').mkdir()
    (src / 'Vault-2' / 'usage.jsonl').write_text('{"legacy": 1}\n', encoding='utf-8')
    before = vu.fingerprint(str(src))
    shutil.rmtree(str(src / '.snapshot-staging' / STAMP))                       # a new run clears it ...
    (src / '.snapshot-staging' / '20261122T100000Z' / 'metrics.db').parent.mkdir(parents=True)
    (src / '.snapshot-staging' / '20261122T100000Z' / 'metrics.db').write_bytes(b'x' * 100)  # ... stages
    (src / 'snapshot-status.json').write_text('{"last_success": "2026-11-22T10:00:09Z"}', encoding='utf-8')
    (src / 'snapshot-status.json.tmp').write_text('{}', encoding='utf-8')
    v = vu.Verifier(types.SimpleNamespace())
    v.fp_before, v.fp_after, v.live_manager = before, vu.fingerprint(str(src)), live
    v.check_source_unchanged()
    assert v.checks['source']['status'] == ('WARN' if live else 'FAIL'), v.checks['source']
    if live:
        assert 'its snapshot ran' in v.checks['source']['summary']
    # a file the Manager does not write changed as well: FAIL either way
    (src / 'Vault-2' / 'usage.jsonl').write_text('{"legacy": 2}\n', encoding='utf-8')
    v = vu.Verifier(types.SimpleNamespace())
    v.fp_before, v.fp_after, v.live_manager = before, vu.fingerprint(str(src)), live
    v.check_source_unchanged()
    assert v.checks['source']['status'] == 'FAIL'
    # and a new folder that is not the staging folder: FAIL either way
    v = vu.Verifier(types.SimpleNamespace())
    after = vu.fingerprint(str(src))
    after['dirs'].append('new_system')
    v.fp_before, v.fp_after, v.live_manager = vu.fingerprint(str(src)), after, live
    v.check_source_unchanged()
    assert v.checks['source']['status'] == 'FAIL'


def _small_data_dir(root):
    src = root / 'data'
    for db in (src / 'metrics.db', src / 'atlantis' / 'data.db'):
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute('CREATE TABLE t (v TEXT)')
        conn.execute("INSERT INTO t VALUES ('x')")
        conn.commit()
        conn.close()
    (src / 'Vault-2').mkdir()
    (src / 'Vault-2' / 'usage.jsonl').write_text('{"legacy": 1}\n', encoding='utf-8')
    (root / 'staging').mkdir()
    return src


def test_a_database_removed_right_after_its_backup(tmp_path, monkeypatch):
    """
    The live Manager may remove a database (DELETE /api/system) right after it was
    copied, before the copy's modification time is set from it: the run goes on, and the
    source check reports the change.
    """
    import verify_upgrade as vu
    src = _small_data_dir(tmp_path)
    before = vu.fingerprint(str(src))
    real = vu.backup_sqlite

    def backup_then_removed(p, t, staging):
        method = real(p, t, staging)
        if p.endswith(os.path.join('atlantis', 'data.db')):
            os.remove(p)
        return method
    monkeypatch.setattr(vu, 'backup_sqlite', backup_then_removed)
    copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(tmp_path / 'staging'))
    assert {c['file']: c['kind'] for c in copied} == {
        'metrics.db': 'sqlite', 'atlantis/data.db': 'sqlite', 'Vault-2/usage.jsonl': 'file'}
    assert vu.quick_check(str(tmp_path / 'copy' / 'atlantis' / 'data.db')) == 'ok'
    v = vu.Verifier(types.SimpleNamespace())
    v.fp_before, v.fp_after, v.live_manager = before, vu.fingerprint(str(src)), False
    v.check_source_unchanged()
    assert v.checks['source']['status'] == 'FAIL'       # no live Manager: nothing may change


def test_a_file_removed_before_its_copy(tmp_path, monkeypatch):
    """A non-database file that vanishes between the listing and its copy is recorded, and FAILs source."""
    import verify_upgrade as vu
    src = _small_data_dir(tmp_path)
    before = vu.fingerprint(str(src))
    real = vu.shutil.copy2

    def removed_then_copy(p, t, *a, **kw):
        if str(p).endswith('usage.jsonl'):
            os.remove(p)
        return real(p, t, *a, **kw)
    monkeypatch.setattr(vu.shutil, 'copy2', removed_then_copy)
    copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(tmp_path / 'staging'))
    monkeypatch.undo()
    assert {c['file']: c['kind'] for c in copied} == {
        'metrics.db': 'sqlite', 'atlantis/data.db': 'sqlite', 'Vault-2/usage.jsonl': 'vanished'}
    v = vu.Verifier(types.SimpleNamespace())
    v.fp_before, v.fp_after, v.live_manager = before, vu.fingerprint(str(src)), True
    v.check_source_unchanged()
    assert v.checks['source']['status'] == 'FAIL'       # usage.jsonl is not the Manager's database


def test_a_review_fix_managers_lock_file_is_neither_copied_nor_compared(tmp_path):
    """
    The NEW Manager on copy B takes its own lock: a copied manager.lock was rewritten
    there, and the other-files check FAILed on it.
    """
    import verify_upgrade as vu
    src = _small_data_dir(tmp_path)
    lock = leave_manager_lock(src)
    assert lock.read_bytes().startswith(b'\npid=')
    fp = vu.fingerprint(str(src))
    assert fp['files']['manager.lock'] == {'size': lock.stat().st_size, 'mtime_ns': lock.stat().st_mtime_ns,
                                           'sha256': None}
    copied = vu.copy_data_dir(str(src), str(tmp_path / 'copy'), str(tmp_path / 'staging'))
    assert {'file': 'manager.lock', 'kind': 'lock', 'method': 'not copied'} in copied
    assert not (tmp_path / 'copy' / 'manager.lock').exists()
    leave_manager_lock(tmp_path / 'copy')                    # the NEW Manager on copy B
    v = vu.Verifier(types.SimpleNamespace())
    v.copied, v.fp_before, v.copy_b = copied, fp, tmp_path / 'copy'
    v.check_other_files()
    assert v.checks['files']['status'] == 'PASS', v.checks['files']
    assert v.checks['files']['summary'].startswith('1 other files')           # usage.jsonl


@pytest.mark.parametrize('live', [True, False])
def test_a_lock_file_that_changes_during_the_run_is_a_fail(tmp_path, live):
    """
    The live Manager of this release's Step 1 is the old one, which never writes
    manager.lock: a new or rewritten lock file means a review-fix Manager started on
    the live data_dir.  FAIL, with or without a live Manager.
    """
    import verify_upgrade as vu
    src = _small_data_dir(tmp_path)
    for step in ('new', 'rewritten'):
        before = vu.fingerprint(str(src))
        lock = leave_manager_lock(src)
        if step == 'rewritten':
            os.utime(str(lock), ns=(lock.stat().st_atime_ns, before['files']['manager.lock']['mtime_ns'] + 10 ** 9))
        v = vu.Verifier(types.SimpleNamespace())
        v.fp_before, v.fp_after, v.live_manager = before, vu.fingerprint(str(src)), live
        v.check_source_unchanged()
        assert v.checks['source']['status'] == 'FAIL', (step, v.checks['source'])
    # unchanged: PASS, and the summary says how it was compared
    v = vu.Verifier(types.SimpleNamespace())
    v.fp_before = v.fp_after = vu.fingerprint(str(src))
    v.live_manager = live
    v.check_source_unchanged()
    assert v.checks['source']['status'] == 'PASS'
    assert 'manager.lock: size, mtime' in v.checks['source']['summary']


# --------------------------------------------------------------------------- #
# End to end on an OLD data_dir with a killed snapshot's leftover             #
# --------------------------------------------------------------------------- #

def test_verify_passes_with_a_killed_snapshots_leftover_in_the_live_data_dir(old_code, compat_dir):
    """
    The OLD data_dir after a review-fix Manager ran on it with snapshots (and was killed
    mid-snapshot) and was rolled back: its manager.lock, its snapshot-status.json and a
    staged copy with a hot journal are still there.  The run must PASS, copy none of
    them, and leave the live data_dir byte-identical, the hot journal included.
    """
    data = compat_dir / 'fixture-snapshot-leftover' / 'data'
    build_fixture(old_code, data, '--days', '1')
    (data / 'snapshot-status.json').write_text(json.dumps(
        {'last_attempt': '2026-11-22T03:30:00Z', 'last_result': 'ok', 'last_success': '2026-11-21T03:30:41Z'}),
        encoding='utf-8')
    staged = data / '.snapshot-staging' / STAMP / 'atlantis' / 'data.db'
    leave_killed_staging(data / 'atlantis' / 'data.db', staged)
    shutil.copyfile(str(data / 'metrics.db'), str(data / '.snapshot-staging' / STAMP / 'metrics.db'))
    leave_manager_lock(data)
    work = compat_dir / 'work-snapshot-leftover'
    work.mkdir()
    report = compat_dir / 'verify-report-snapshot-leftover.json'
    before = snapshot(data)
    rc, rep, text = run_verify('--data-dir', data, '--old-code', old_code, '--work-dir', work,
                               '--report', report, *QUICK)
    assert snapshot(data) == before
    assert Path(str(staged) + '-journal').stat().st_size > 0
    assert rep is not None, text[-4000:]
    assert rc == 0 and rep['result'] == 'PASS', _summary(text)
    assert rep['checks']['source']['status'] == 'PASS'
    copies = {c['file']: c for c in rep['copies']}
    assert copies['.snapshot-staging'] == {'file': '.snapshot-staging', 'kind': 'scratch', 'method': 'not copied'}
    assert copies['snapshot-status.json']['kind'] == 'status'
    assert copies['manager.lock'] == {'file': 'manager.lock', 'kind': 'lock', 'method': 'not copied'}
    assert rep['checks']['files']['status'] == 'PASS'
    assert not [f for f in copies if f.startswith('.snapshot-staging/')]
    # the systems are the real ones only
    assert not [s for s in rep['inventory']['systems'] if 'snapshot' in str(s.get('dir'))]
    assert not [k for k in rep['inventory']['databases'] if k.startswith('.snapshot-staging')]
