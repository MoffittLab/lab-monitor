"""The fleet tools on an install-collector-linux.sh install (platform column = linux)."""
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import _ssh
import archive_all_collectors as archive
import inventory_crypt
import run_collectors
import toggle_collectors
import update_collectors

REPO = Path(__file__).resolve().parents[2]
CHECKOUT = '/home/admin/code/lab-monitor'
LINUX = {'ip': 'server1.example.org', 'username': 'admin', 'password': '', 'git_path': CHECKOUT + '/',
         'python_path': '', 'platform': 'Linux'}
NAS = {'ip': '192.0.2.42', 'username': 'jeff', 'password': '',
       'git_path': '/volume1/lab-monitor/scripts/lab-monitor', 'python_path': '/volume1/env/bin/python3',
       'platform': ''}
LINUX_CONFIG = {'name': 'server1', 'data_dir': '/var/lib/lab-monitor', 'manager_token': '***'}
SUDO_REFUSED = 'sudo: a password is required\n'


def test_platform_values():
    assert _ssh.is_linux_install(LINUX) and not _ssh.is_linux_install(NAS)
    assert not _ssh.is_linux_install({'ip': 'x'})            # no platform column at all
    assert _ssh.platform_problems([LINUX, NAS, dict(NAS, platform='Windows')]) == []
    assert 'linx' in _ssh.platform_problems([dict(LINUX, platform='linx')])[0]


def test_read_inventory_refuses_unknown_platform(tmp_path, capsys):
    csv = tmp_path / 'collectors.csv'
    csv.write_text('ip,username,git_path,python_path,platform\nh1,u,/g,/p,linux\nh2,u,/g,/p,ubuntu\n')
    with pytest.raises(SystemExit) as e:
        _ssh.read_inventory(csv)
    assert e.value.code == 1 and "h2: unknown platform 'ubuntu'" in capsys.readouterr().out
    csv.write_text('ip,username,git_path,python_path,platform\nh1,u,/g,,linux\nh2,u,/g,/p,\n')
    rows, fields = _ssh.read_inventory(csv)
    assert [_ssh.is_linux_install(r) for r in rows] == [True, False]


def test_inventory_crypt_validate_checks_platform():
    rows = [dict(LINUX), dict(NAS, platform='synolgy')]
    problems = inventory_crypt.validate(rows, list(LINUX))
    assert len(problems) == 1 and 'synolgy' in problems[0]


def test_sudo_hint():
    assert 'sudoers' in _ssh.sudo_hint(SUDO_REFUSED)
    assert 'sudoers' in _ssh.sudo_hint('Sorry, user admin is not allowed to execute ...')
    assert _ssh.sudo_hint('fatal: not a git repository') == ''


# --- update ---------------------------------------------------------------

def test_update_pulls_the_checkout_then_runs_its_installer():
    expected = [('git pull', f'git -C {CHECKOUT} pull origin main'),
                ('install', f'sudo -n {CHECKOUT}/install-collector-linux.sh --non-interactive')]
    assert update_collectors.build_commands(LINUX, skip_pip=True) == expected
    # the installer refreshes the venv itself: --upgrade-pip adds no step
    assert update_collectors.build_commands(LINUX, skip_pip=False) == expected


def test_update_quotes_an_unusual_checkout_path():
    cmds = dict(update_collectors.build_commands(dict(LINUX, git_path='/srv/lab monitor'), skip_pip=True))
    assert cmds['git pull'] == "git -C '/srv/lab monitor' pull origin main"
    assert cmds['install'] == "sudo -n '/srv/lab monitor/install-collector-linux.sh' --non-interactive"


def test_update_other_platforms_unchanged():
    assert update_collectors.build_commands(NAS, skip_pip=True) == [
        ('git pull', 'git -C "/volume1/lab-monitor/scripts/lab-monitor" pull origin main')]


def test_update_failure_from_sudo_gets_the_hint(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    answers = iter([('Already up to date.', '', 0), ('', SUDO_REFUSED, 1)])
    monkeypatch.setattr(update_collectors, 'run_remote', lambda client, cmd, timeout: next(answers))
    r = update_collectors.update_system(LINUX, dry_run=False, skip_pip=True, timeout=5)
    assert r['status'] == 'error'
    assert [s['status'] for s in r['steps']] == ['ok', 'error']
    assert 'sudoers' in r['steps'][1]['error']


# --- run ------------------------------------------------------------------

@pytest.mark.parametrize('mode', ['metrics', 'disk'])
def test_run_starts_the_systemd_service(mode):
    assert run_collectors.build_run_command(LINUX, mode) == f'sudo -n systemctl start lab-monitor-{mode}.service'


def test_run_other_platforms_unchanged():
    assert run_collectors.build_run_command(NAS, 'metrics') == (
        '"/volume1/env/bin/python3" "/volume1/lab-monitor/scripts/lab-monitor/collector/collector.py" '
        '--config "/volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json" --mode metrics')


def test_run_reports_ok_and_failure(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    monkeypatch.setattr(_ssh, 'exec_command', lambda client, cmd, **kw: ('', '', 0))
    ok = run_collectors.run_collection(LINUX, mode='metrics', dry_run=False, timeout=5)
    assert ok['status'] == 'ok' and 'journalctl -u lab-monitor-metrics.service' in ok['output']
    monkeypatch.setattr(_ssh, 'exec_command', lambda client, cmd, **kw: (
        '', 'Job for lab-monitor-metrics.service failed because the control process exited', 1))
    bad = run_collectors.run_collection(LINUX, mode='metrics', dry_run=False, timeout=5)
    assert bad['status'] == 'error' and 'journalctl' in bad['error']
    monkeypatch.setattr(_ssh, 'exec_command', lambda client, cmd, **kw: ('', SUDO_REFUSED, 1))
    refused = run_collectors.run_collection(LINUX, mode='metrics', dry_run=False, timeout=5)
    assert 'sudoers' in refused['error']


# --- toggle ---------------------------------------------------------------

def test_toggle_uses_config_tool_on_the_etc_config_as_root():
    prefix = ('sudo -n /opt/lab-monitor/venv/bin/python /opt/lab-monitor/collector/config_tool.py '
              '--config /etc/lab-monitor/collector.json')
    assert toggle_collectors.config_tool_command(LINUX, 'list --json') == prefix + ' list --json'
    assert toggle_collectors.config_tool_command(LINUX, 'set active false --json') == \
        prefix + ' set active false --json'
    assert toggle_collectors.config_tool_command(NAS, 'list --json') == (
        '"/volume1/env/bin/python3" "/volume1/lab-monitor/scripts/lab-monitor/collector/config_tool.py" '
        '--config "/volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json" list --json')


def test_toggle_system_linux(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    state = {'active': True}
    seen = []

    def run_remote(client, command, timeout):
        seen.append(command)
        if ' set active ' in command:
            state['active'] = command.split(' set active ')[1].startswith('true')
            return json.dumps({'status': 'ok'}), '', 0
        return json.dumps({'status': 'ok', 'config': dict(LINUX_CONFIG, **state)}), '', 0
    monkeypatch.setattr(toggle_collectors, 'run_remote', run_remote)
    r = toggle_collectors.toggle_system(LINUX, mode='off', dry_run=False, timeout=5)
    assert r['status'] == 'ok'
    assert r['config_before']['active'] is True and r['config_after']['active'] is False
    assert all(c.startswith('sudo -n /opt/lab-monitor/venv/bin/python ') for c in seen)

    monkeypatch.setattr(toggle_collectors, 'run_remote', lambda client, command, timeout: ('', SUDO_REFUSED, 1))
    r = toggle_collectors.toggle_system(LINUX, mode='off', dry_run=False, timeout=5)
    assert r['status'] == 'failed' and 'sudoers' in r['error']


# --- archive --------------------------------------------------------------

def test_archive_linux_reads_config_as_root_and_moves_as_labmon(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    seen = []

    def run_remote(client, command, timeout):
        seen.append(command)
        if 'config_tool.py' in command:
            return json.dumps({'status': 'ok', 'config': LINUX_CONFIG}), '', 0
        return 'MOVED: 4\nQUEUE: moved\nQUEUE_LOCK: held', '', 0
    monkeypatch.setattr(archive, 'run_remote', run_remote)
    r = archive.archive_host(LINUX, timeout=5, command_timeout=30, include_queue=True, dry_run=False)
    assert r['ok'], r['message']
    assert seen[0] == _ssh.linux_config_tool_command('list --json')
    assert seen[1].startswith('sudo -n -u labmon /bin/sh -c ')
    inner = shlex.split(seen[1])[-1]
    assert '/var/lib/lab-monitor/server1' in inner and r['backup_dir'].startswith('/var/lib/lab-monitor/server1/backup_')


def test_archive_linux_sudo_refused(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    monkeypatch.setattr(archive, 'run_remote', lambda client, command, timeout: ('', SUDO_REFUSED, 1))
    r = archive.archive_host(LINUX, timeout=5, command_timeout=30, include_queue=False, dry_run=False)
    assert not r['ok'] and 'sudoers' in r['message']


def test_archive_nas_unchanged(monkeypatch):
    class Client:
        def close(self):
            pass
    monkeypatch.setattr(_ssh, 'connect_row', lambda row, **kw: Client())
    seen = []

    def run_remote(client, command, timeout):
        seen.append(command)
        if 'config_tool.py' in command:
            return json.dumps({'status': 'ok', 'config': {'name': 'nas1', 'data_dir': '/volume1/lab-monitor/data'}}), '', 0
        return 'MOVED: 1', '', 0
    monkeypatch.setattr(archive, 'run_remote', run_remote)
    assert archive.archive_host(NAS, timeout=5, command_timeout=30, include_queue=False, dry_run=False)['ok']
    assert '/volume1/lab-monitor/scripts/lab-monitor/collector/local/config.json' in seen[0]
    assert not seen[1].startswith('sudo')


# --- config_tool keeps the config's permissions ------------------------------

CONFIG_TOOL = REPO / 'collector' / 'config_tool.py'


def _config_tool(*args):
    return subprocess.run([sys.executable, str(CONFIG_TOOL), *args], capture_output=True, text=True)


def test_config_tool_set_keeps_content_and_mode(tmp_path):
    cfg = tmp_path / 'collector.json'
    cfg.write_text(json.dumps({'name': 'server1', 'manager_token': 'abcdefghijklmnopqrstuvwxyz', 'active': True}))
    if os.name != 'nt':
        os.chmod(cfg, 0o640)
    r = _config_tool('--config', str(cfg), 'set', 'active', 'false', '--json')
    assert r.returncode == 0, r.stderr
    data = json.loads(cfg.read_text())
    assert data == {'name': 'server1', 'manager_token': 'abcdefghijklmnopqrstuvwxyz', 'active': False}
    if os.name != 'nt':
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o640       # mkstemp alone would leave 0600
    assert list(tmp_path.iterdir()) == [cfg]                     # no temporary file left
