"""Startup warnings for the recommended data layout (local data_dir of its own + snapshots)."""
import logging
import os
from pathlib import Path

import pytest

import manager as mgr
from manager_helpers import base_config


def _cfg(data_dir, scratch, snap=True, dest=None):
    s = {'enabled': True, 'dest_dir': dest or r'\\nas.example.org\share\snaps'} if snap else None
    return base_config(data_dir, **({'snapshot': s} if s else {}))


@pytest.mark.parametrize('path, expected', [
    (r'\\server\share\data', True),
    ('//server/share/data', True),
    (r'\\?\UNC\server\share\data', True),
    (r'\\?\C:\local\data', False),
    ('relative/data', False),
])
def test_is_network_path_unc(path, expected):
    assert mgr.is_network_path(path) is expected


def test_recommended_layout_has_no_warnings(scratch):
    data = os.path.join(scratch, 'manager-data')
    os.makedirs(os.path.join(data, 'triton'))
    Path(data, 'triton', 'data.db').write_bytes(b'')
    Path(data, 'metrics.db').write_bytes(b'')
    assert mgr.data_layout_warnings(_cfg(data, scratch)) == []


def test_shared_with_collector_warns(scratch):
    data = os.path.join(scratch, 'data')
    os.makedirs(os.path.join(data, 'atlantis'))
    Path(data, 'atlantis', '2026-11.jsonl').write_text('{}\n')
    os.makedirs(os.path.join(data, 'triton'))
    Path(data, 'triton', 'data.db').write_bytes(b'')
    # retention archives are the Manager's own: not a collector sign
    os.makedirs(os.path.join(data, 'triton', 'archive'))
    Path(data, 'triton', 'archive', '2020-01-system_metrics.jsonl').write_text('{}\n')
    w = mgr.data_layout_warnings(_cfg(data, scratch))
    assert len(w) == 1 and "collector's data_dir" in w[0] and 'atlantis' in w[0]
    assert 'triton' not in w[0]


def test_network_data_dir_warns():
    w = mgr.data_layout_warnings(base_config(r'\\server\share\lab-monitor-data',
                                             snapshot={'enabled': True,
                                                       'dest_dir': r'\\other\share\snaps'}))
    assert len(w) == 1 and 'network share' in w[0]


def test_snapshots_off_or_local_warn(scratch):
    data = os.path.join(scratch, 'manager-data')
    os.makedirs(data)
    w = mgr.data_layout_warnings(_cfg(data, scratch, snap=False))
    assert len(w) == 1 and 'Snapshots are disabled' in w[0]
    w = mgr.data_layout_warnings(_cfg(data, scratch, dest=os.path.join(scratch, 'local-snaps')))
    assert len(w) == 1 and 'not on a network share' in w[0]


def test_warnings_are_logged_at_startup(make_client, caplog):
    with caplog.at_level(logging.WARNING, logger='test-manager'):
        make_client()
    assert any('Snapshots are disabled' in r.getMessage() for r in caplog.records)
