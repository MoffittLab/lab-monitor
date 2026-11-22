"""
Shared fixtures for the collector tests.

collector/collector.py is loaded by file path (it is a script, not a
package). It imports its own lib/ as the 'lib' package, appending the
collector directory to sys.path itself — the tests deliberately don't put it
first, so they also exercise that import path.
"""
import importlib.util
import json
import os
import sys

import pytest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COLLECTOR_DIR = os.path.join(REPO_DIR, 'collector')


def _load_collector():
    name = 'labmon_collector_main'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(COLLECTOR_DIR, 'collector.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector_module = _load_collector()


@pytest.fixture
def collector(monkeypatch, tmp_path):
    # build_header() would otherwise probe the network for the local IP
    monkeypatch.setattr(collector_module, 'get_local_ip', lambda: '10.0.0.1')
    # Not this host's interfaces (fake NIC names could match a real virtual
    # one, e.g. a veth eth0 inside a container) or account layout
    monkeypatch.setattr(collector_module, 'SYS_CLASS_NET', str(tmp_path / 'no-sys-class-net'))
    monkeypatch.setattr(collector_module, 'LOGIN_DEFS', str(tmp_path / 'no-login.defs'))
    return collector_module


@pytest.fixture
def local_queue():
    return collector_module.local_queue


@pytest.fixture
def disk_usage():
    from lib import disk_usage as module
    return module


@pytest.fixture
def config(tmp_path):
    return {
        'name': 'testbox',
        'id': 'linux-testbox',
        'device_type': 'Server',
        'manager_url': 'http://manager.invalid:5000',
        'manager_token': 'collector-token-0123456789',
        'data_dir': str(tmp_path / 'data'),
        'request_timeout_seconds': 5,
    }


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else '')

    def json(self):
        if self._body is None:
            raise ValueError('no JSON body')
        return self._body


class FakeManager:
    """
    Stand-in for requests.post. `handler(payload) -> FakeResponse` decides the
    reply; every decoded payload is kept in .calls.
    """

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or self.ack_all

    def __call__(self, url, data=None, json=None, headers=None, timeout=None):
        import json as _json
        payload = _json.loads(data) if data is not None else json
        self.calls.append({'url': url, 'payload': payload, 'headers': headers, 'timeout': timeout})
        return self.handler(payload)

    @staticmethod
    def ack(payload, statuses=None):
        """New-style ACK. statuses: {msg_id: status} (default: all 'stored')."""
        statuses = statuses or {}
        results = []
        for m in payload['messages']:
            mid = m['header']['msg_id']
            results.append({'msg_id': mid, 'status': statuses.get(mid, 'stored')})
        counts = {s: sum(1 for r in results if r['status'] == s)
                  for s in ('stored', 'duplicate', 'rejected', 'error')}
        return FakeResponse(200, {
            'status': 'ok', 'queue_id': payload['queue_id'],
            'received': len(results),
            'stored': counts['stored'] + counts['duplicate'] + counts['rejected'],
            'counts': counts, 'results': results,
        })

    @classmethod
    def ack_all(cls, payload):
        return cls.ack(payload)


@pytest.fixture
def fake_manager(collector, monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(collector.requests, 'post', manager)
    return manager


def make_entries(collector, n, name='testbox'):
    return [
        {'header': collector.build_header(name, 'linux-testbox', 'Server'),
         'data': {'data_type': 'system_metrics', 'cpu_percent': float(i)}}
        for i in range(n)
    ]


def read_queue_file(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


@pytest.fixture
def helpers():
    """Helper callables/classes (conftest can't be imported directly under importlib mode)."""
    import types
    return types.SimpleNamespace(
        FakeResponse=FakeResponse,
        FakeManager=FakeManager,
        make_entries=lambda n, name='testbox': make_entries(collector_module, n, name),
        read_queue_file=read_queue_file,
    )
