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
