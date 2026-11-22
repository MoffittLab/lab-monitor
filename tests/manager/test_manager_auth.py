"""Auth scopes (contract 7) and startup refusal of unsafe tokens."""
import logging
import os
import subprocess
import sys
import json

import pytest

import manager as mgr
from manager_helpers import (
    ADMIN, COLL_ATLAS, COLL_TRITON, LEGACY, READ, auth, base_config, make_msg,
    metrics_payload, post_queue,
)


def test_missing_and_invalid_tokens_401(client):
    assert client.get('/api/devices').status_code == 401
    r = client.get('/api/devices', headers=auth('x' * 40))
    assert r.status_code == 401
    assert r.get_json() == {'status': 'error', 'error': 'Invalid token'}
    assert client.get('/health').status_code == 200


def test_collector_token_scoped_to_its_device(client):
    m = make_msg('system_metrics', metrics_payload(), name='Atlas')
    r = post_queue(client, [m], token=COLL_TRITON, name='Atlas')
    assert r.status_code == 403
    assert post_queue(client, [m], token=COLL_ATLAS, name='Atlas').status_code == 200
    # name match is case-insensitive after normalization
    m2 = make_msg('system_metrics', metrics_payload(), name='triton')
    assert post_queue(client, [m2], token=COLL_TRITON, name='triton').status_code == 200
    # collector tokens can't read or delete
    assert client.get('/api/devices', headers=auth(COLL_TRITON)).status_code == 403
    assert client.delete('/api/system/Triton', headers=auth(COLL_TRITON)).status_code == 403


def test_header_name_must_match_payload_name(client):
    m = make_msg('system_metrics', metrics_payload(), name='Atlas')
    ack = post_queue(client, [m], token=COLL_TRITON, name='Triton').get_json()
    assert ack['results'][0]['status'] == 'rejected'
    assert client.get('/api/devices', headers=auth(READ)).get_json()['devices'] == []


def test_read_token_cannot_write_or_delete(client):
    assert post_queue(client, [make_msg('system_metrics', metrics_payload())], token=READ).status_code == 403
    assert client.delete('/api/system/Triton', headers=auth(READ)).status_code == 403
    assert client.get('/api/dead_letter', headers=auth(READ)).status_code == 403
    assert client.get('/api/devices', headers=auth(READ)).status_code == 200


def test_admin_can_delete(client, data_dir):
    post_queue(client, [make_msg('system_metrics', metrics_payload())])
    post_queue(client, [make_msg('user_metrics', {'username': 'a', 'cpu_percent': 1.0, 'ram_bytes': 1})])
    assert os.path.isfile(os.path.join(data_dir, 'triton', 'data.db'))
    r = client.delete('/api/system/triton', headers=auth(ADMIN))   # any spelling
    assert r.status_code == 200
    body = r.get_json()
    assert body['per_system_db_deleted'] is True and body['central_db_rows_cleared'] is True
    assert not os.path.exists(os.path.join(data_dir, 'triton', 'data.db'))
    assert client.get('/api/devices', headers=auth(ADMIN)).get_json()['devices'] == []
    assert client.get('/api/user_metrics/all', headers=auth(ADMIN)).get_json() == {}
    assert client.get('/api/data/Triton', headers=auth(ADMIN)).status_code == 404


def test_legacy_auth_tokens_keep_full_rights(make_client):
    client = make_client(auth_tokens=[LEGACY], collector_tokens={})
    assert post_queue(client, [make_msg('system_metrics', metrics_payload(), name='Anything')],
                      token=LEGACY, name='Anything').status_code == 200
    assert client.get('/api/devices', headers=auth(LEGACY)).status_code == 200
    assert client.delete('/api/system/Anything', headers=auth(LEGACY)).status_code == 200


def test_wildcard_collector_token(make_client, caplog):
    wild = 'wild-' + 'f6' * 16
    with caplog.at_level(logging.WARNING):
        client = make_client(collector_tokens={'*': wild})
    assert any("'*'" in r.message for r in caplog.records)
    assert post_queue(client, [make_msg('system_metrics', metrics_payload(), name='Zeta')],
                      token=wild, name='Zeta').status_code == 200


@pytest.mark.parametrize('bad', [
    {'auth_tokens': ['GENERATE-RANDOM-TOKEN-1']},
    {'admin_tokens': ['CHANGE-ME-TO-SECURE-TOKEN']},
    {'read_tokens': ['YOUR-SECURE-TOKEN-1234567']},
    {'collector_tokens': {'Triton': 'short'}},
    {'collector_tokens': {'../x': 'z' * 32}},
    {'admin_tokens': 'not-a-list-' + 'x' * 20},
    {'history_retention_days': 0},
])
def test_unsafe_config_refuses_startup(data_dir, bad):
    with pytest.raises(mgr.ConfigError):
        mgr.init_app(base_config(data_dir, **bad), logging.getLogger('t'))


def test_no_tokens_refuses_startup(data_dir):
    with pytest.raises(mgr.ConfigError):
        mgr.init_app({'data_dir': data_dir}, logging.getLogger('t'))


def test_main_exits_nonzero_on_placeholder(data_dir, scratch):
    cfg_path = os.path.join(scratch, 'config.json')
    with open(cfg_path, 'w') as f:
        json.dump({'data_dir': data_dir, 'auth_tokens': ['GENERATE-RANDOM-TOKEN-1']}, f)
    manager_py = os.path.join(os.path.dirname(mgr.__file__), 'manager.py')
    r = subprocess.run([sys.executable, manager_py, '--config', cfg_path, '--check-config'],
                       capture_output=True, text=True, timeout=60, env={**os.environ})
    assert r.returncode == 2
    assert 'placeholder' in r.stderr

    with open(cfg_path, 'w') as f:
        json.dump(base_config(data_dir), f)
    r = subprocess.run([sys.executable, manager_py, '--config', cfg_path, '--check-config'],
                       capture_output=True, text=True, timeout=60, env={**os.environ})
    assert r.returncode == 0, r.stderr
    assert 'Config OK' in r.stdout
