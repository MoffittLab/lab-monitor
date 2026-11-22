"""Tests for tools/update_collectors.py, run_collectors.py and reset_system.py command building."""
import sys

import pytest

import reset_system
import run_collectors
import update_collectors


NAS = {'ip': '192.168.1.42', 'username': 'jeff', 'password': 'pw',
       'git_path': '/volume1/lab-monitor/scripts/lab-monitor',
       'python_path': '/volume1/miniconda/envs/lab-monitor/bin/python'}
WIN = {'ip': 'atlantis', 'username': 'admin', 'password': '',
       'git_path': 'E:\\Users\\lab-monitor\\scripts\\lab-monitor',
       'python_path': 'C:\\ProgramData\\Miniconda3\\envs\\lab-monitor\\python.exe'}


def test_update_git_pull_only_by_default():
    assert update_collectors.build_commands(NAS, skip_pip=True) == [
        ('git pull', 'git -C "/volume1/lab-monitor/scripts/lab-monitor" pull origin main')]


def test_update_pip_uses_collector_requirements():
    # POSIX collectors: the pins come from the remote collector/requirements.txt,
    # psutil best-effort (see test_tools_update_pip.py)
    cmds = dict(update_collectors.build_commands(NAS, skip_pip=False))
    assert cmds['pip install'].startswith('[ -r /volume1/lab-monitor/scripts/lab-monitor/collector/requirements.txt ]')
    assert "/volume1/miniconda/envs/lab-monitor/bin/python -m pip install -q $strict" in cmds['pip install']
    cmds = dict(update_collectors.build_commands(WIN, skip_pip=False))
    assert cmds['pip install'] == ('"C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe" -m pip install '
                                   '-r "E:/Users/lab-monitor/scripts/lab-monitor/collector/requirements.txt" -q')


def test_update_requirements_override_and_windows_paths():
    cmds = dict(update_collectors.build_commands(WIN, skip_pip=False, requirements='requirements.txt'))
    assert cmds['pip install'] == ('"C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe" -m pip install '
                                   '-r "E:/Users/lab-monitor/scripts/lab-monitor/requirements.txt" -q')


def test_update_filters_sshd_preamble_only():
    err = "Could not chdir to home directory /var/services/homes/jeff: No such file or directory\nreal error"
    assert update_collectors._filter_sshd_preamble(err) == 'real error'


def test_update_dry_run_does_not_connect(monkeypatch):
    monkeypatch.setattr(update_collectors._ssh, 'connect_row',
                        lambda *a, **kw: pytest.fail('connected in dry-run'))
    result = update_collectors.update_system(NAS, dry_run=True, skip_pip=False, timeout=5)
    assert result['status'] == 'dry-run' and len(result['steps']) == 3


@pytest.mark.parametrize('host, wanted, expected', [
    ('triton2.hms.example.org', 'triton2', True),     # first label of a host name
    ('triton2.hms.example.org', 'TRITON2', True),     # case ignored
    ('triton2.hms.example.org', 'triton2.hms.example.org', True),
    ('triton2.hms.example.org', 'triton', False),     # no prefix matches
    ('triton20.hms.example.org', 'triton2', False),
    ('192.168.1.42', '192.168.1.42', True),
    ('192.168.1.42', '192', False),                   # an IP address only matches exactly
    ('atlantis', 'atlantis', True),
    ('', 'triton2', False),
])
def test_only_host_matching(host, wanted, expected):
    assert update_collectors._ssh._host_matches(host, wanted) is expected


INVENTORY = ('ip,username,password,git_path,python_path\n'
             'triton2.hms.example.org,u,,/volume1/lab-monitor/scripts/lab-monitor,/py\n'
             'triton20.hms.example.org,u,,/volume1/lab-monitor/scripts/lab-monitor,/py\n'
             '192.168.1.42,u,,/volume1/lab-monitor/scripts/lab-monitor,/py\n')


def _run_update(monkeypatch, tmp_path, *args):
    csv = tmp_path / 'collectors.csv'
    csv.write_text(INVENTORY)
    seen = []
    monkeypatch.setattr(update_collectors, 'update_system', lambda row, **kw: seen.append(row['ip']) or {
        'ip': row['ip'], 'status': 'dry-run', 'steps': [], 'elapsed': 0.0})
    monkeypatch.setattr(sys, 'argv', ['update_collectors.py', '--csv', str(csv), '--dry-run', *args])
    with pytest.raises(SystemExit) as info:
        update_collectors.main()
    return info.value.code, seen


def test_only_updates_just_the_selected_systems(monkeypatch, tmp_path, capsys):
    code, seen = _run_update(monkeypatch, tmp_path, '--only', 'triton2')
    assert code == 0 and seen == ['triton2.hms.example.org']
    assert 'Systems : 1 (--only triton2)' in capsys.readouterr().out
    code, seen = _run_update(monkeypatch, tmp_path, '--only', '192.168.1.42', '--only', 'triton2')
    assert code == 0 and seen == ['triton2.hms.example.org', '192.168.1.42']   # inventory order


def test_only_with_an_unknown_system_contacts_nobody(monkeypatch, tmp_path, capsys):
    code, seen = _run_update(monkeypatch, tmp_path, '--only', 'triton2', '--only', 'trtion3')
    assert code == 1 and seen == []
    out = capsys.readouterr().out
    assert '--only matched no system' in out and 'trtion3' in out and 'triton2,' not in out


def test_without_only_every_system_is_updated(monkeypatch, tmp_path):
    code, seen = _run_update(monkeypatch, tmp_path)
    assert code == 0 and len(seen) == 3


def test_run_collectors_dry_run_command():
    r = run_collectors.run_collection(WIN, mode='disk', dry_run=True, timeout=5)
    assert r['output'] == ('Would run: "C:/ProgramData/Miniconda3/envs/lab-monitor/python.exe" '
                           '"E:/Users/lab-monitor/scripts/lab-monitor/collector/collector.py" '
                           '--config "E:/Users/lab-monitor/scripts/lab-monitor/collector/local/config.json" '
                           '--mode disk')


def test_run_collectors_exit_code_2_on_failure(monkeypatch, tmp_path):
    csv = tmp_path / 'c.csv'
    csv.write_text('ip,username,password,git_path,python_path\n1.2.3.4,u,p,/g,/py\n')
    monkeypatch.setattr(run_collectors, 'run_collection', lambda row, **kw: {
        'ip': row['ip'], 'status': 'failed', 'output': '', 'error': 'unreachable', 'elapsed': 0.1})
    monkeypatch.setattr(sys, 'argv', ['run_collectors.py', '--csv', str(csv), '--mode', 'metrics'])
    with pytest.raises(SystemExit) as info:
        run_collectors.main()
    assert info.value.code == 2


class _Resp:
    def __init__(self, code, body=None, text=''):
        self.status_code, self._body, self.text = code, body or {}, text

    def json(self):
        return self._body


def test_reset_wipe_uses_given_token_and_quotes_name(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(reset_system.requests, 'delete',
                        lambda url, headers, timeout: calls.append((url, headers)) or _Resp(200))
    assert reset_system.wipe_manager('http://mgr:5000/', 'admintok', 'Lab-A') is True
    assert calls == [('http://mgr:5000/api/system/Lab-A', {'Authorization': 'Bearer admintok'})]


def test_reset_wipe_403_explains_admin_token(monkeypatch, capsys):
    monkeypatch.setattr(reset_system.requests, 'delete',
                        lambda url, headers, timeout: _Resp(403, text='{"status":"error"}'))
    assert reset_system.wipe_manager('http://mgr:5000', 'collectortok', 'Triton') is False
    assert 'ADMIN token' in capsys.readouterr().out


def test_reset_help_mentions_admin_token_and_replay():
    assert 'replay_archives.py' in reset_system.__doc__
    assert 'admin' in reset_system.__doc__.lower()
    assert not hasattr(reset_system, 're')


def _reset_config(tmp_path):
    data = tmp_path / 'data'
    (data / 'Triton').mkdir(parents=True)
    (data / 'Triton' / '2026-08.jsonl').write_text('{}\n')
    (data / 'Triton' / 'queue.json').write_text('[]')
    cfg = tmp_path / 'config.json'
    cfg.write_text('{"name": "Triton", "data_dir": "%s", "manager_url": "http://mgr:5000", '
                   '"manager_token": "collector-token-0123456789"}' % data.as_posix())
    return cfg, data / 'Triton'


@pytest.mark.parametrize('dead_letter, systems, expected', [
    (200, None, True),          # new Manager, admin token
    (403, None, False),         # new Manager, collector/read token
    (401, None, False),         # unknown token
    (404, 200, True),           # old Manager (no dead_letter route): valid token = full rights
    (404, 401, False),
    (500, None, False),
])
def test_reset_check_admin_token(monkeypatch, dead_letter, systems, expected):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return _Resp(dead_letter if url.endswith('/api/dead_letter') else systems)
    monkeypatch.setattr(reset_system.requests, 'get', fake_get)
    ok, message = reset_system.check_admin_token('http://mgr:5000/', 'tok')
    assert ok is expected
    assert calls[0] == 'http://mgr:5000/api/dead_letter'
    if dead_letter in (401, 403):
        assert 'ADMIN token' in message


def test_reset_refused_token_moves_nothing(monkeypatch, tmp_path, capsys):
    """A collector token must not leave the system half reset (archives moved, Manager intact)."""
    cfg, system_dir = _reset_config(tmp_path)
    monkeypatch.delenv('LAB_MONITOR_ADMIN_TOKEN', raising=False)
    monkeypatch.setattr(reset_system.requests, 'get', lambda *a, **kw: _Resp(403))
    monkeypatch.setattr(reset_system.requests, 'delete',
                        lambda *a, **kw: pytest.fail('DELETE sent with a refused token'))
    monkeypatch.setattr(sys, 'argv', ['reset_system.py', '--config', str(cfg), '--force'])
    with pytest.raises(SystemExit) as info:
        reset_system.main()
    assert info.value.code == 1
    assert sorted(p.name for p in system_dir.iterdir()) == ['2026-08.jsonl', 'queue.json']
    assert 'Nothing was changed' in capsys.readouterr().out


def test_reset_unreachable_manager_moves_nothing(monkeypatch, tmp_path):
    cfg, system_dir = _reset_config(tmp_path)

    def down(*a, **kw):
        raise reset_system.requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(reset_system.requests, 'get', down)
    monkeypatch.setattr(sys, 'argv', ['reset_system.py', '--config', str(cfg), '--force',
                                      '--admin-token', 'admin-token-0123456789'])
    with pytest.raises(SystemExit) as info:
        reset_system.main()
    assert info.value.code == 1
    assert sorted(p.name for p in system_dir.iterdir()) == ['2026-08.jsonl', 'queue.json']


def test_reset_admin_token_resets_and_prints_replay_with_legacy(monkeypatch, tmp_path, capsys):
    cfg, system_dir = _reset_config(tmp_path)
    monkeypatch.setattr(reset_system.requests, 'get', lambda *a, **kw: _Resp(200))
    monkeypatch.setattr(reset_system.requests, 'delete', lambda *a, **kw: _Resp(200))
    monkeypatch.setattr(sys, 'argv', ['reset_system.py', '--config', str(cfg), '--force',
                                      '--admin-token', 'admin-token-0123456789'])
    with pytest.raises(SystemExit) as info:
        reset_system.main()
    assert info.value.code == 0
    backups = [p for p in system_dir.iterdir() if p.name.startswith('backup_')]
    assert len(backups) == 1
    assert sorted(p.name for p in backups[0].iterdir()) == ['2026-08.jsonl', 'queue.json']
    out = capsys.readouterr().out
    assert 'replay_archives.py' in out and '--include-legacy' in out


# --only in run_collectors.py and toggle_collectors.py (same selection as update_collectors.py)

FLEET_CSV = ('ip,username,password,git_path,python_path,platform\n'
             'triton2.example.org,u,,/g,/py,\n192.168.1.42,u,,/g,/py,\nkraken.example.org,u,,/g,,linux\n')


def _run_tool(monkeypatch, tmp_path, module, worker, argv):
    """Run module.main() with the per-system worker replaced. Returns (exit code, hosts the worker saw)."""
    csv = tmp_path / 'collectors.csv'
    csv.write_text(FLEET_CSV)
    seen = []

    def fake(row, **kw):
        seen.append(row['ip'])
        return {'ip': row['ip'], 'status': 'ok', 'output': '', 'error': '', 'elapsed': 0.0,
                'config_before': {}, 'config_after': {}}
    monkeypatch.setattr(module, worker, fake)
    monkeypatch.setattr(module._ssh, 'require_paramiko', lambda: None)
    monkeypatch.setattr(sys, 'argv', [module.__name__ + '.py', '--csv', str(csv), *argv])
    with pytest.raises(SystemExit) as info:
        module.main()
    return info.value.code, seen


@pytest.mark.parametrize('tool, worker, mode', [
    ('run_collectors', 'run_collection', 'metrics'),
    ('toggle_collectors', 'toggle_system', 'on'),
])
def test_only_in_run_and_toggle(monkeypatch, tmp_path, capsys, tool, worker, mode):
    import importlib
    module = importlib.import_module(tool)

    code, seen = _run_tool(monkeypatch, tmp_path, module, worker, ['--mode', mode, '--only', 'kraken'])
    assert code == 0 and seen == ['kraken.example.org']
    assert 'Systems: 1 (--only kraken)' in capsys.readouterr().out

    code, seen = _run_tool(monkeypatch, tmp_path, module, worker,
                           ['--mode', mode, '--only', 'triton2', '--only', '192.168.1.42'])
    assert code == 0 and seen == ['triton2.example.org', '192.168.1.42']

    code, seen = _run_tool(monkeypatch, tmp_path, module, worker, ['--mode', mode])
    assert code == 0 and len(seen) == 3                                  # no --only: every system

    code, seen = _run_tool(monkeypatch, tmp_path, module, worker, ['--mode', mode, '--only', 'krakn'])
    assert code == 1 and seen == []                                      # a typo contacts nobody
    assert '--only matched no system' in capsys.readouterr().out
