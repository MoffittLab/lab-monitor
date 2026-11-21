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
CURRENT_REF = 'fb6498c'                                  # the release before this one
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
