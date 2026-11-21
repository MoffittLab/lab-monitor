"""Tests for tools/restart_manager_dashboard.py: PID paths, process checks, kill/verify logic."""
import base64

import pytest

import restart_manager_dashboard as restart


MANAGER = restart.SERVICES[0]
DASHBOARD = restart.SERVICES[1]
PY = 'C:\\ProgramData\\Miniconda3\\envs\\lab-monitor\\python.exe'


@pytest.mark.parametrize('git_path', [
    'E:/Users/lab-monitor/scripts/lab-monitor',
    'E:\\Users\\lab-monitor\\scripts\\lab-monitor',
    'E:/Users/lab-monitor/scripts/lab-monitor/',
    ' E:\\Users\\lab-monitor\\scripts\\lab-monitor\\ ',
])
def test_pid_file_path_uses_git_path(git_path):
    assert restart.pid_file_path(git_path, 'manager.pid') == \
        'E:\\Users\\lab-monitor\\scripts\\lab-monitor\\.pids\\manager.pid'
    assert restart.config_file_path(git_path, 'dashboard') == \
        'E:\\Users\\lab-monitor\\scripts\\lab-monitor\\dashboard\\config.json'


def test_status_script_reads_pid_file_under_git_path():
    script = restart.build_status_script('D:/lab/lab-monitor', DASHBOARD)
    assert "$pidFile = 'D:\\lab\\lab-monitor\\.pids\\dashboard.pid'" in script
    assert 'Get-CimInstance -ClassName Win32_Process' in script
    assert "@('/health', '/')" in script
    assert 'ConvertTo-Json' in script


def test_kill_script_reverifies_command_line():
    script = restart.build_kill_script(4242, f'"{PY}" manager.py --config config.json')
    assert '$target = 4242' in script
    assert f"$expected = '\"{PY}\" manager.py --config config.json'" in script
    assert script.index('-cne $expected') < script.index('taskkill.exe')
    # The command for SSH is pure base64, so cmd.exe quoting can't break it.
    cmd = restart._ssh.powershell_command(script)
    assert base64.b64decode(cmd.split()[-1]).decode('utf-16-le').endswith('\n' + script)


@pytest.mark.parametrize('name,cmdline,script,expected', [
    ('python.exe', f'"{PY}" manager.py --config config.json', 'manager.py', True),
    ('python.exe', f'{PY} E:\\Users\\lab-monitor\\scripts\\lab-monitor\\manager\\manager.py', 'manager.py', True),
    ('pythonw.exe', f'{PY} app.py --config config.json', 'app.py', True),
    ('python.exe', f'{PY} app.py --config config.json', 'manager.py', False),
    ('python.exe', f'{PY} webapp.py', 'app.py', False),
    ('chrome.exe', 'chrome.exe --flag manager.py', 'manager.py', False),
    ('python.exe', '', 'manager.py', False),
    (None, None, 'manager.py', False),
])
def test_is_service_process(name, cmdline, script, expected):
    assert restart.is_service_process(name, cmdline, script) is expected


def _status(pid=100, alive=True, name='python.exe', cmd=f'"{PY}" manager.py', http=200, script='manager.py'):
    st = {'pid_file_exists': pid is not None, 'pid': pid, 'alive': alive, 'name': name,
          'command_line': cmd, 'port': 5000, 'http_status': http,
          'http_url': 'http://127.0.0.1:5000/health', 'http_error': None}
    st['matches'] = alive and restart.is_service_process(name, cmd, script)
    st['healthy'] = st['matches'] and http == 200
    return st


def test_kill_skips_stale_pid_and_never_counts_not_found(monkeypatch):
    statuses = {
        'Manager': _status(pid=100, alive=False),                        # stale PID file
        'Dashboard': _status(pid=200, name='notepad.exe', cmd='notepad.exe', script='app.py'),  # PID reused
    }
    monkeypatch.setattr(restart, 'get_status', lambda client, gp, svc: statuses[svc['label']])
    monkeypatch.setattr(restart, 'python_processes', lambda client: [])
    ran = []
    monkeypatch.setattr(restart, 'run_powershell', lambda client, script, desc: ran.append(desc) or ('', '', 0))

    ok, stopped = restart.kill_processes(None, 'E:/repo')
    assert ok is True and stopped == {}
    assert ran == []                                   # nothing was killed


def test_kill_verified_process(monkeypatch):
    statuses = {
        'Manager': _status(pid=100),
        'Dashboard': _status(pid=200, cmd=f'"{PY}" app.py', script='app.py'),
    }
    monkeypatch.setattr(restart, 'get_status', lambda client, gp, svc: statuses[svc['label']])
    results = {'100': 'SUCCESS: killed\nRESULT: KILLED', '200': 'RESULT: NOT_RUNNING'}
    scripts = []

    def fake_ps(client, script, desc):
        scripts.append(script)
        pid = script.split('$target = ')[1].split()[0]
        return results[pid], '', 0
    monkeypatch.setattr(restart, 'run_powershell', fake_ps)

    ok, stopped = restart.kill_processes(None, 'E:/repo')
    assert ok is True
    # Dashboard was verified running, then exited on its own before taskkill: it is gone,
    # so it counts as stopped (verification then demands a new PID).
    assert stopped == {'Manager': 100, 'Dashboard': 200}
    assert len(scripts) == 2


def test_kill_ends_tasks_first(monkeypatch):
    """With the launcher restart loop the task is ended first; survivors are killed after checks."""
    calls = []
    state = {'ended': False}

    def fake_status(client, gp, svc):
        if svc['label'] == 'Manager':
            # Ending the task took the Manager down with it
            return _status(pid=100, alive=not state['ended'])
        # The Dashboard survived the task end and must be killed explicitly
        return _status(pid=200, cmd=f'"{PY}" app.py', script='app.py')
    monkeypatch.setattr(restart, 'get_status', fake_status)

    def fake_ps(client, script, desc):
        calls.append(desc)
        if desc.startswith('End task'):
            state['ended'] = True
            return 'SUCCESS: terminated', '', 0
        return 'RESULT: KILLED', '', 0
    monkeypatch.setattr(restart, 'run_powershell', fake_ps)

    ok, stopped = restart.kill_processes(None, 'E:/repo', ['Lab Monitor Manager', 'Lab Monitor Dashboard'])
    assert ok is True and stopped == {'Manager': 100, 'Dashboard': 200}
    assert calls == ['End task Lab Monitor Manager', 'End task Lab Monitor Dashboard',
                     'Kill Dashboard PID 200']


def test_kill_failure_reported(monkeypatch):
    monkeypatch.setattr(restart, 'get_status', lambda client, gp, svc: _status(
        pid=100, cmd=f'"{PY}" {svc["script"]}', script=svc['script']))
    monkeypatch.setattr(restart, 'run_powershell', lambda client, script, desc: ('RESULT: STILL_RUNNING', '', 1))
    ok, stopped = restart.kill_processes(None, 'E:/repo')
    assert ok is False and stopped == {}


def test_verify_requires_live_process_and_http(monkeypatch):
    def fake_status(client, gp, svc):
        if svc['label'] == 'Manager':
            return _status(pid=101)                                   # new PID, healthy
        return _status(pid=200, alive=False, http=None, script='app.py')  # PID file exists, process dead
    monkeypatch.setattr(restart, 'get_status', fake_status)
    assert restart.verify_services(None, 'E:/repo', {'Manager': 100}, timeout=0) is False


def test_verify_rejects_unchanged_pid(monkeypatch):
    monkeypatch.setattr(restart, 'get_status', lambda client, gp, svc: _status(
        pid=100, cmd=f'"{PY}" {svc["script"]}', script=svc['script']))
    assert restart.verify_services(None, 'E:/repo', {'Manager': 100}, timeout=0) is False
    assert restart.verify_services(None, 'E:/repo', {'Manager': 99}, timeout=0) is True


def test_verify_rejects_unhealthy_http(monkeypatch):
    monkeypatch.setattr(restart, 'get_status', lambda client, gp, svc: _status(
        pid=100, cmd=f'"{PY}" {svc["script"]}', script=svc['script'], http=500))
    assert restart.verify_services(None, 'E:/repo', timeout=0) is False


@pytest.mark.parametrize('found,expected', [
    (['Lab Monitor Manager', 'Lab Monitor Dashboard', 'Lab Monitor Startup'],
     ['Lab Monitor Manager', 'Lab Monitor Dashboard']),
    (['Lab Monitor Startup'], ['Lab Monitor Startup']),
    (['Lab Monitor Manager', 'Lab Monitor Startup'], ['Lab Monitor Startup']),
    ([], []),
])
def test_choose_tasks(found, expected):
    assert restart.choose_tasks(found) == expected


def test_parse_json_output_takes_last_json_line():
    assert restart.parse_json_output('noise\n{"a": 1}\n') == {'a': 1}
    with pytest.raises(ValueError):
        restart.parse_json_output('no json here')


def test_http_answering_rules():
    assert restart.http_answering(MANAGER, 200)
    assert not restart.http_answering(MANAGER, 401)
    assert not restart.http_answering(MANAGER, None)
    assert restart.http_answering(DASHBOARD, 401)       # Basic auth enabled: still up
    assert not restart.http_answering(DASHBOARD, 502)
    assert not restart.http_answering(DASHBOARD, None)
