"""
Run the CURRENT Manager (commit fb6498c: the release before rollups, with
contract items 1-9 and the reserved "_" / "storage_breakdown_" table
prefixes) on a data_dir, to build CURRENT-format databases and to check a
NEW -> CURRENT downgrade.

Like old_manager.py: manager/ of that commit is extracted once per session
with `git archive` and run in a SUBPROCESS (its modules have the same names
as the new ones).  Tests are skipped when git or the commit is unavailable.
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

CURRENT_COMMIT = 'fb6498c'
REPO = Path(__file__).resolve().parents[2]
_SCRATCH_BASE = '/tmp/claude-1000' if os.path.isdir('/tmp/claude-1000') else None
_cache = {}

# stdin: {"config": {...}, "posts": [queue bodies], "deletes": [paths], "gets": [paths], "token": admin token}
# stdout: {"posts": [{status, json}], "deletes": {path: {status, json}}, "gets": {path: {status, json}}}
_RUNNER = r'''
import json, logging, sys
code_dir = sys.argv[1]
sys.path.insert(0, code_dir)
import manager as m
job = json.load(sys.stdin)
logging.basicConfig(level=logging.WARNING)
m.init_app(job["config"], logging.getLogger("current-manager"))
client = m.app.test_client()
headers = {"Authorization": "Bearer " + job["token"]}
out = {"posts": [], "deletes": {}, "gets": {}}
for body in job.get("posts", []):
    r = client.post("/api/data/queue", json=body, headers=headers)
    out["posts"].append({"status": r.status_code, "json": r.get_json()})
for path in job.get("deletes", []):
    r = client.delete(path, headers=headers)
    out["deletes"][path] = {"status": r.status_code, "json": r.get_json()}
for path in job.get("gets", []):
    r = client.get(path, headers=headers)
    out["gets"][path] = {"status": r.status_code, "json": r.get_json()}
m._close_stores()
sys.stdout.write(json.dumps(out))
'''


def current_manager_dir() -> Path:
    """manager/ as of CURRENT_COMMIT, extracted once per session (skips if unavailable)."""
    if 'dir' in _cache:
        return _cache['dir']
    try:
        tar = subprocess.run(['git', '-C', str(REPO), 'archive', CURRENT_COMMIT, 'manager'],
                             capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f'CURRENT Manager code ({CURRENT_COMMIT}) not available: {e}')
    root = Path(tempfile.mkdtemp(prefix='lm-current-manager-', dir=_SCRATCH_BASE))
    atexit.register(shutil.rmtree, root, True)
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        if hasattr(tarfile, 'data_filter'):
            tf.extractall(root, filter='data')
        else:
            tf.extractall(root)
    _cache['dir'] = root / 'manager'
    return _cache['dir']


def run_current(config: dict, token: str, posts=(), gets=(), deletes=()) -> dict:
    """
    init_app() of the CURRENT Manager on config["data_dir"] (the start-up
    migration / backup decisions included), then the given POST
    /api/data/queue bodies, DELETE paths and GET paths through its Flask
    routes.
    """
    proc = subprocess.run(
        [sys.executable, '-c', _RUNNER, str(current_manager_dir())],
        input=json.dumps({'config': config, 'token': token, 'posts': list(posts), 'gets': list(gets),
                          'deletes': list(deletes)}),
        capture_output=True, text=True, timeout=300,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout)
