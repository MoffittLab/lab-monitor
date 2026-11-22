"""
Build Manager databases with the ORIGINAL Manager code (commit f086e7e, the
version deployed before this upgrade), so upgrade tests start from files the
old code really wrote rather than from a hand-made imitation.

The old manager/ directory is extracted once per test session with
`git archive` and run in a SUBPROCESS: its modules (manager, metrics,
data_store) have the same names as the new ones, so they can't share an
interpreter.  Tests are skipped when git or the commit is unavailable.
"""
import atexit
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

OLD_COMMIT = 'f086e7e'
REPO = Path(__file__).resolve().parents[2]
_SCRATCH_BASE = '/tmp/claude-1000' if os.path.isdir('/tmp/claude-1000') else None
_cache = {}

_RUNNER = r'''
import json, logging, sys
old_dir, data_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, old_dir)
import manager as m
m.init_app({"data_dir": data_dir}, logging.getLogger("old-manager"))
for batch in json.load(sys.stdin):
    if "entries" in batch:
        m._ingest_legacy_entries(batch["name"], batch["id"], batch["entries"])
    else:
        m._ingest_messages(batch["name"], batch["id"], batch["messages"])
m._data_store.close()
m._metrics_db.close()
'''


def old_manager_dir() -> Path:
    """manager/ as of OLD_COMMIT, extracted once per session (skips if unavailable)."""
    if 'dir' in _cache:
        return _cache['dir']
    try:
        tar = subprocess.run(['git', '-C', str(REPO), 'archive', OLD_COMMIT, 'manager'],
                             capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f'old Manager code ({OLD_COMMIT}) not available: {e}')
    root = Path(tempfile.mkdtemp(prefix='lm-old-manager-', dir=_SCRATCH_BASE))
    atexit.register(shutil.rmtree, root, True)
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        if hasattr(tarfile, 'data_filter'):
            tf.extractall(root, filter='data')
        else:
            tf.extractall(root)
    _cache['dir'] = root / 'manager'
    return _cache['dir']


def old_ingest(data_dir, batches):
    """
    Feed batches through the old Manager's ingest functions, writing
    data_dir/metrics.db and data_dir/<system>/data.db exactly as it did.
    Each batch is {"name", "id", "messages": [...]} (or "entries" for v1).
    """
    os.makedirs(data_dir, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, '-c', _RUNNER, str(old_manager_dir()), str(data_dir)],
        input=json.dumps(batches), capture_output=True, text=True, timeout=120,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    assert proc.returncode == 0, proc.stderr


def old_msg(data_type, data, ts, name='Triton', device_type='synology'):
    """A message as an old collector built it: no header.msg_id."""
    body = dict(data)
    body['data_type'] = data_type
    return {'header': {'device_name': name, 'device_id': f'id-{name.lower()}',
                       'device_type': device_type, 'timestamp': ts},
            'data': body}
