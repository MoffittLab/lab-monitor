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
  current_data_dir  a CURRENT-format data_dir (make_current_fixture.py): a
                2-day OLD fixture upgraded by the CURRENT Manager and fed two
                more days by the CURRENT collector (on Linux also its real
                manager.py and collector.py); the data_dir the next upgrade
                starts from

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

from compat_helpers import CURRENT_REF, OLD_REF, REPO, build_current_fixture, build_fixture  # noqa: E402


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


@pytest.fixture(scope='session')
def old_data_for_current(old_code, compat_dir):
    """The 2-day OLD data_dir the CURRENT fixtures start from (never changed: they copy it)."""
    out = compat_dir / 'fixture-before-current' / 'data'
    build_fixture(old_code, out, '--days', '2')
    return out


@pytest.fixture(scope='session')
def current_data_dir(old_data_for_current, current_code, compat_dir):
    out = compat_dir / 'fixture-current' / 'data'
    extra = ['--old-collector-state', str(old_data_for_current) + '-collectors']
    if sys.platform.startswith('linux'):
        tree = compat_dir / 'fixture-current-tree'
        for d, size in (('projA', 40000), ('projB', 12000)):
            (tree / d).mkdir(parents=True)
            (tree / d / 'f.bin').write_bytes(b'x' * size)
        extra += ['--real-collector', str(tree)]
    build_current_fixture(current_code, old_data_for_current, out, *extra)
    return out
