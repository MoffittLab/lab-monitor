"""
update_collectors.py --upgrade-pip on Synology/Linux: requests (everything
but psutil) strictly, psutil best-effort, as install-collector-synology.sh
does.  The generated command runs here against a fake python that records
its pip calls.
"""
import os
import shutil
import subprocess

import pytest

import update_collectors

pytestmark = pytest.mark.skipif(shutil.which('sh') is None or os.name != 'posix',
                                reason='needs a POSIX shell')

REQ = os.path.join(os.path.dirname(__file__), '..', '..', 'collector', 'requirements.txt')

FAKE_PYTHON = """#!/bin/sh
# fake "python -m pip install -q SPEC..." : log the specs, fail on request
shift 4
echo "$*" >> "$PIP_LOG"
for spec in "$@"; do
  case "$spec" in
    psutil*) [ -n "$FAIL_PSUTIL" ] && { echo "error: no wheel for armv7l" >&2; exit 1; } ;;
    requests*) [ -n "$FAIL_REQUESTS" ] && { echo "error: network" >&2; exit 1; } ;;
  esac
done
exit 0
"""


@pytest.fixture
def host(tmp_path):
    git_path = tmp_path / 'lab monitor'            # a space, to check the quoting
    (git_path / 'collector').mkdir(parents=True)
    shutil.copy(REQ, git_path / 'collector' / 'requirements.txt')
    python = tmp_path / 'py' / 'python'
    python.parent.mkdir()
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    row = {'ip': 'nas', 'username': 'u', 'git_path': str(git_path), 'python_path': str(python)}
    return row, tmp_path / 'pip.log'


def _run(row, log, shell=('sh', '-c'), **env):
    cmd = dict(update_collectors.build_commands(row, skip_pip=False))['pip install']
    proc = subprocess.run([shutil.which(shell[0])] + list(shell[1:]) + [cmd], capture_output=True, text=True,
                          env={'PATH': os.environ['PATH'], 'PIP_LOG': str(log), **env}, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _pins():
    pins = {}
    for line in open(REQ):
        line = line.split('#')[0].strip()
        if line:
            pins[line.split('==')[0].lower()] = line
    return pins


@pytest.mark.parametrize('shell', [
    ('sh', '-c'),
    pytest.param(('busybox', 'sh', '-c'),
                 marks=pytest.mark.skipif(shutil.which('busybox') is None, reason='no busybox')),
])
def test_psutil_failure_is_only_a_warning(host, shell):
    row, log = host
    proc, calls = _run(row, log, shell, FAIL_PSUTIL='1')
    assert proc.returncode == 0, proc.stderr
    pins = _pins()
    assert calls == [pins['requests'], pins['psutil']]        # strict first, psutil on its own
    assert f"WARNING: {pins['psutil']} could not be installed" in proc.stdout


def test_psutil_installed_when_possible(host):
    row, log = host
    proc, calls = _run(row, log)
    assert proc.returncode == 0, proc.stderr
    assert f"{_pins()['psutil']} installed" in proc.stdout and 'WARNING' not in proc.stdout


def test_requests_failure_fails_the_step(host):
    row, log = host
    proc, calls = _run(row, log, FAIL_REQUESTS='1')
    assert proc.returncode == 1
    assert len(calls) == 1                                   # psutil not attempted


def test_missing_requirements_file_fails(host):
    row, log = host
    os.remove(os.path.join(row['git_path'], 'collector', 'requirements.txt'))
    proc, calls = _run(row, log)
    assert proc.returncode == 1 and 'not found' in proc.stderr and calls == []


def test_other_requirements_files_stay_strict(host):
    row, _ = host
    cmd = dict(update_collectors.build_commands(row, skip_pip=False, requirements='requirements.txt'))
    assert cmd['pip install'] == f'"{row["python_path"]}" -m pip install -r "{row["git_path"]}/requirements.txt" -q'
