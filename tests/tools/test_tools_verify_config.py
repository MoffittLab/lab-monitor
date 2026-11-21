"""
tools/verify_upgrade.py, "config" check: a config the NEW Manager refuses (the
upgrade would take the Manager down, exit code 2) is a FAIL, so the run can
never end with RESULT: PASS.  (The full run: tests/compat.)
"""
import os
import types
from pathlib import Path

import pytest

import verify_upgrade as vu

REPO = Path(__file__).resolve().parents[2]


def _fake_verifier(tmp_path, manager_config):
    fake = types.SimpleNamespace(manager_config=manager_config, work=tmp_path, new_code=REPO, checks={})
    fake.env = lambda old=False: dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    fake.check = types.MethodType(vu.Verifier.check, fake)
    return fake


@pytest.mark.parametrize('tokens', [
    ['CHANGE-ME-TO-SECURE-TOKEN'],                          # the old installer's default
    ['a' * 40, 'GENERATE-RANDOM-TOKEN-2'],                  # a leftover from the old example config
    ['short-token'],
])
def test_refused_config_fails_the_run(tmp_path, tokens):
    fake = _fake_verifier(tmp_path, {'data_dir': 'unused', 'auth_tokens': tokens})
    vu.Verifier.preflight_config(fake)
    check = fake.checks['config']
    assert check['status'] == 'FAIL'
    assert 'REFUSE' in check['summary'] and 'auth_tokens[' in check['summary']
    assert not any(t in check['summary'] for t in tokens if len(t) > 12)   # never prints a token


def test_accepted_config_passes(tmp_path):
    fake = _fake_verifier(tmp_path, {'data_dir': 'unused', 'auth_tokens': ['b7' * 20]})
    vu.Verifier.preflight_config(fake)
    assert fake.checks['config']['status'] == 'PASS'
