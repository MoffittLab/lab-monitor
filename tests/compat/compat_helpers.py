"""Helpers for the upgrade-compatibility tests (see conftest.py)."""
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TOOLS = REPO / 'tools'
OLD_REF = 'f086e7e'
# The release before this one: the review-fix release as it will be deployed
# (fix/review-findings: fb6498c plus nightly snapshots and the manager-data
# layout).  Its database code is fb6498c's, so what it writes is fb6498c-format
# data (test_compat_verify_current.py checks both: SAME_FORMAT_REFS).
CURRENT_REF = '160da09'
SAME_FORMAT_REFS = ('fb6498c',)
OLD_TOKEN = 'fixture-old-auth-token-0123456789abcdef'   # make_old_fixture.TOKEN


def free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def build_fixture(old_code, out, *extra):
    """Run make_old_fixture.py; returns its JSON summary."""
    r = subprocess.run(
        [sys.executable, str(HERE / 'make_old_fixture.py'), '--old-code', str(old_code),
         '--out', str(out), '--collector-state', str(out) + '-collectors', *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[-4000:]
    return json.loads(r.stdout.decode('utf-8').strip().splitlines()[-1])


def build_current_fixture(current_code, old_data, out, *extra):
    """Run make_current_fixture.py on a copy of old_data (a make_old_fixture.py data_dir); returns its summary."""
    r = subprocess.run(
        [sys.executable, str(HERE / 'make_current_fixture.py'), '--current-code', str(current_code),
         '--old-data', str(old_data), '--out', str(out), *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[-4000:]
    return json.loads(r.stdout.decode('utf-8').strip().splitlines()[-1])


# A process that dies (os._exit, like a kill) inside a write transaction of
# `staged`, a complete copy of `src`: what a Manager killed while it staged a
# database for its nightly snapshot leaves in <data_dir>/.snapshot-staging/
# (a copy whose changed pages are in the file, with a hot rollback journal
# that a read-only connection can't roll back).
_KILLED_STAGING = r'''
import os, sqlite3, sys
from pathlib import Path
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(Path(os.path.abspath(src)).as_uri() + '?mode=ro&immutable=1', uri=True)
d = sqlite3.connect(dst, isolation_level=None)
s.backup(d)
d.execute('PRAGMA journal_mode=DELETE')
d.execute('PRAGMA cache_size=5')
d.execute('BEGIN')
for (t,) in d.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    d.execute('DELETE FROM "%s"' % t.replace('"', '""'))
os._exit(0)
'''


def leave_killed_staging(src_db, staged):
    """Leave a killed snapshot's staged copy of src_db (with its hot -journal) at `staged`."""
    os.makedirs(os.path.dirname(str(staged)), exist_ok=True)
    subprocess.run([sys.executable, '-c', _KILLED_STAGING, str(src_db), str(staged)], check=True, timeout=300)
    if not os.path.getsize(str(staged) + '-journal'):
        raise RuntimeError('no hot journal next to %s' % staged)


def snapshot(root):
    """Every file and directory under root: relative path -> (size, mtime_ns, sha256)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root) + '/'] = 'dir'
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            with open(p, 'rb') as fh:
                h = hashlib.sha256(fh.read()).hexdigest()
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, h)
    return out


def run_verify(*args, timeout=1200):
    """Run tools/verify_upgrade.py; returns (exit code, report dict or None, output text)."""
    report = None
    args = [str(a) for a in args]
    if '--report' in args:
        report = args[args.index('--report') + 1]
    r = subprocess.run([sys.executable, str(TOOLS / 'verify_upgrade.py')] + args,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    text = r.stdout.decode('utf-8', 'replace')
    data = None
    if report and os.path.exists(report):
        with open(report, encoding='utf-8') as f:
            data = json.load(f)
    return r.returncode, data, text
