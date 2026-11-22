"""Thread safety under concurrent posts, waitress startup, root logging."""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import manager as mgr
from manager_helpers import (
    COLL_ATLAS, COLL_TRITON, READ, auth, base_config, make_msg, metrics_payload, post_queue,
)


def test_concurrent_posts_are_all_stored_once(client):
    errors = []
    per_thread = 25

    def worker(idx, name, token):
        c = mgr.app.test_client()
        msgs = []
        for i in range(per_thread):
            minute = idx * per_thread + i
            ts = f'2026-08-04T{minute // 60:02d}:{minute % 60:02d}:00Z'
            msgs.append(make_msg('system_metrics', metrics_payload(1000 + minute, 0), ts=ts, name=name))
            msgs.append(make_msg('user_metrics', {'username': f'u{idx}', 'cpu_percent': 1.0, 'ram_bytes': i},
                                 ts=ts, name=name))
        # send in small batches, each twice (retries) to exercise dedup under contention
        for j in range(0, len(msgs), 10):
            for _ in range(2):
                ack = post_queue(c, msgs[j:j + 10], token=token, name=name).get_json()
                if ack['counts']['error'] or ack['counts']['rejected']:
                    errors.append(ack)

    threads = [threading.Thread(target=worker, args=(i, name, tok))
               for i in range(4) for name, tok in (('Triton', COLL_TRITON), ('Atlas', COLL_ATLAS))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    for name in ('Triton', 'Atlas'):
        rows = client.get(f'/api/data/{name}/system_metrics?limit=1000', headers=auth(READ)).get_json()['rows']
        assert len(rows) == 4 * per_thread
        rows = client.get(f'/api/data/{name}/user_metrics?limit=1000', headers=auth(READ)).get_json()['rows']
        assert len(rows) == 4 * per_thread


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_manager(scratch, data_dir, **overrides):
    """Run manager.py in a subprocess (waitress); returns (proc, port)."""
    port = _free_port()
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(data_dir, host='127.0.0.1', port=port, **overrides), f)
    script = os.path.join(os.path.dirname(mgr.__file__), 'manager.py')
    proc = subprocess.Popen([sys.executable, script, '--config', cfg_path],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while True:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as r:
                assert json.load(r) == {'status': 'ok'}
                return proc, port
        except OSError:
            if time.time() > deadline or proc.poll() is not None:
                proc.kill()
                raise AssertionError('manager did not start: ' + proc.communicate()[0])
            time.sleep(0.2)


def test_manager_serves_with_waitress_and_logs_to_file(scratch, data_dir):
    log_file = os.path.join(scratch, 'logs', 'manager.log')
    proc, port = _start_manager(scratch, data_dir, log_file=log_file)
    try:
        body = json.dumps({'queue_id': 'Triton-x', 'name': 'Triton', 'id': 'i',
                           'messages': [make_msg('system_metrics', metrics_payload())]}).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/data/queue', data=body, method='POST',
                                     headers={**auth(COLL_TRITON), 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)['counts']['stored'] == 1
    finally:
        proc.terminate()
        out = proc.communicate(timeout=30)[0]

    assert 'DEVELOPMENT server' not in out           # waitress was used
    with open(log_file) as f:
        text = f.read()
    # module loggers (data_store, metrics) reach the file via the root logger
    assert 'data_store - INFO - [data_store] schema evolved' in text
    assert 'Processed queue Triton-x' in text


def test_waitress_oversize_body_gets_json_413(scratch, data_dir):
    # Flask, not waitress, must answer an oversized body so it gets the
    # contract's JSON error body (waitress's own 413 is text/plain)
    proc, port = _start_manager(scratch, data_dir, max_request_bytes=2048)
    try:
        body = json.dumps({'queue_id': 'Triton-x', 'name': 'Triton', 'id': 'i',
                           'messages': [make_msg('custom', {'blob': 'x' * 5000})]}).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/data/queue', data=body, method='POST',
                                     headers={**auth(COLL_TRITON), 'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError('expected 413')
        except urllib.error.HTTPError as e:
            assert e.code == 413
            assert e.headers.get_content_type() == 'application/json'
            assert json.load(e) == {'status': 'error', 'error': 'Request body too large'}
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


def test_setup_logging_is_idempotent(scratch):
    log_file = os.path.join(scratch, 'm.log')
    mgr.setup_logging(log_file, 'INFO')
    mgr.setup_logging(log_file, 'INFO')
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, '_lab_monitor', False)]
    try:
        assert len(ours) == 2
        logging.getLogger('data_store').info('hello-from-data-store')
        for h in ours:
            h.flush()
        with open(log_file) as f:
            assert 'hello-from-data-store' in f.read()
    finally:
        for h in ours:
            root.removeHandler(h)
            h.close()


def test_unreachable_data_dir_exits_1_with_hint(scratch):
    # e.g. a UNC data_dir the task account can't reach: exit 1 (the launcher
    # retries) with a message that points at the account, not a bare traceback
    blocker = os.path.join(scratch, 'not-a-dir')
    with open(blocker, 'w') as f:
        f.write('x')
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(os.path.join(blocker, 'data'), port=_free_port()), f)
    script = os.path.join(os.path.dirname(mgr.__file__), 'manager.py')
    r = subprocess.run([sys.executable, script, '--config', cfg_path],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert 'Cannot open data_dir' in r.stderr and 'network share' in r.stderr
    assert 'Traceback' not in r.stderr


def _drop_our_handlers():
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, '_lab_monitor', False)]:
        root.removeHandler(h)
        h.close()


def test_second_manager_on_the_same_data_dir_refuses_to_start(scratch, data_dir):
    """Two Managers must never share a data_dir: the second exits 3 (the launcher retries
    later) before it opens, backs up or migrates any database."""
    from storage_util import InstanceLock
    holder = InstanceLock(data_dir).acquire()
    try:
        cfg_path = os.path.join(scratch, 'config.json')
        with open(cfg_path, 'w') as f:
            json.dump(base_config(data_dir, port=_free_port()), f)
        script = os.path.join(os.path.dirname(mgr.__file__), 'manager.py')
        r = subprocess.run([sys.executable, script, '--config', cfg_path],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 3, r.stderr
        assert 'Another Manager is already running' in r.stderr
        assert f'pid={os.getpid()}' in r.stderr              # names the holder
        assert not os.path.exists(os.path.join(data_dir, 'metrics.db'))
    finally:
        holder.release()
    # released (as when the holder exits): the next one gets it
    InstanceLock(data_dir).acquire().release()


def test_pid_file_is_written_before_the_databases_are_opened(scratch, data_dir, monkeypatch):
    """The first start after an upgrade migrates before it listens: the PID file must already
    name this process then (the installer and the restart tool find the Manager through it)."""
    order = []
    monkeypatch.setattr(mgr, 'write_pid_file', lambda: order.append('pid') or None)

    def fake_init(config, logger=None):
        order.append('init')
        raise mgr.ConfigError('stop here')
    monkeypatch.setattr(mgr, 'init_app', fake_init)
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(data_dir), f)
    monkeypatch.setattr(sys, 'argv', ['manager.py', '--config', cfg_path])
    try:
        with pytest.raises(SystemExit) as e:
            mgr.main()
    finally:
        _drop_our_handlers()
    assert e.value.code == 2 and order == ['pid', 'init']


def test_unrotated_old_log_is_moved_out_of_the_rotation(scratch):
    """The old Manager logged to one ever-growing manager.log. Rotating it would delete it after
    log_backup_count more rotations, so it is set aside under a name rotation never touches."""
    log_file = os.path.join(scratch, 'logs', 'manager.log')
    os.makedirs(os.path.dirname(log_file))
    old_text = 'old manager line\n' * 200                  # 3400 bytes > max_bytes below
    with open(log_file, 'w') as f:
        f.write(old_text)

    # --check-config never touches the log file
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump(base_config(os.path.join(scratch, 'data'), log_file=log_file, log_max_bytes=1024), f)
    script = os.path.join(os.path.dirname(mgr.__file__), 'manager.py')
    r = subprocess.run([sys.executable, script, '--config', cfg_path, '--check-config'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert os.listdir(os.path.dirname(log_file)) == ['manager.log']
    with open(log_file) as f:
        assert f.read() == old_text

    # the first real start moves it aside, outside manager.log.1 .. .N
    try:
        mgr.setup_logging(log_file, 'INFO', max_bytes=1024, backup_count=2)
        for i in range(100):
            logging.getLogger('manager').info('new line %d %s', i, 'x' * 50)
    finally:
        _drop_our_handlers()
    kept = [n for n in os.listdir(os.path.dirname(log_file)) if '.pre-rotation-' in n]
    assert len(kept) == 1
    with open(os.path.join(os.path.dirname(log_file), kept[0])) as f:
        assert f.read() == old_text
    with open(log_file + '.2') as f:                        # rotation ran, and left it alone
        assert 'new line' in f.read()
