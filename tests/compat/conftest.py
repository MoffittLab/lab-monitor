"""
Fixtures for the upgrade-compatibility tests.

  old_code      the OLD code (base commit f086e7e) extracted with git archive
  current_code  collector/, manager/ and dashboard/ of the CURRENT release
                (fb6498c), extracted the same way
  old_data_dir  an OLD-format Manager data_dir written by that code
                (make_old_fixture.py: the old collector feeding the old
                Manager through its HTTP routes); built once per session
  orphans_data_dir  the same with --variant orphans (a device directory
                without central rows, central rows without a directory,
                per-user rows of deleted devices, an empty data.db)

tools/verify_upgrade.py is run as a subprocess, the way the owner runs it.
"""
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parents[1] / 'tools'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from compat_helpers import CURRENT_REF, OLD_REF, REPO, build_fixture  # noqa: E402


@pytest.fixture(scope='session')
def compat_dir(tmp_path_factory):
    return tmp_path_factory.mktemp('compat')


@pytest.fixture(scope='session')
def old_code(compat_dir):
    dest = compat_dir / 'old-code'
    try:
        tar = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', OLD_REF],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f'cannot extract the old code ({OLD_REF}) with git archive: {e}')
    kw = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        for m in tf.getmembers():
            if m.isfile() or m.isdir():
                tf.extract(m, str(dest), **kw)
    return dest


@pytest.fixture(scope='session')
def current_code(compat_dir):
    """The CURRENT release (fb6498c: collector, manager, dashboard) extracted with git archive."""
    dest = compat_dir / 'current-code'
    try:
        tar = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', CURRENT_REF,
                              'collector', 'manager', 'dashboard'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f'cannot extract the CURRENT code ({CURRENT_REF}) with git archive: {e}')
    kw = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        for m in tf.getmembers():
            if m.isfile() or m.isdir():
                tf.extract(m, str(dest), **kw)
    return dest


@pytest.fixture(scope='session')
def old_data_dir(old_code, compat_dir):
    out = compat_dir / 'fixture' / 'data'
    build_fixture(old_code, out)
    return out


@pytest.fixture(scope='session')
def orphans_data_dir(old_code, compat_dir):
    """The --variant orphans data_dir: leftovers of the old DELETE and a lost history database."""
    out = compat_dir / 'fixture-orphans' / 'data'
    build_fixture(old_code, out, '--days', '2', '--variant', 'orphans')
    return out
