"""
Shared fixtures for the Manager tests.

manager/*.py import each other as top-level modules (metrics, data_store,
storage_util), so the manager directory goes on sys.path; so does this
directory, for manager_helpers.  Each test gets a fresh data_dir and a freshly
initialised app (init_app closes the previous databases).
"""
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MANAGER_DIR = HERE.parents[1] / 'manager'
for _p in (MANAGER_DIR, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import manager as mgr  # noqa: E402
from manager_helpers import base_config  # noqa: E402

# Scratch space: under /tmp/claude-1000 when it exists (sandboxed runs)
_SCRATCH_BASE = '/tmp/claude-1000' if os.path.isdir('/tmp/claude-1000') else None


@pytest.fixture
def scratch():
    path = tempfile.mkdtemp(prefix='lm-manager-test-', dir=_SCRATCH_BASE)
    yield path
    mgr._close_stores()
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def data_dir(scratch):
    return os.path.join(scratch, 'data')


@pytest.fixture
def make_client(data_dir):
    def _make(**overrides):
        mgr.init_app(base_config(data_dir, **overrides), logging.getLogger('test-manager'))
        return mgr.app.test_client()
    return _make


@pytest.fixture
def client(make_client):
    return make_client()
