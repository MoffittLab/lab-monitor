"""Tests for tools/archive_all_collectors.py: confirmation, && chaining, queue move, failures."""
import base64
import shutil
import subprocess
import sys

import pytest

import _ssh
import archive_all_collectors as archive


CSV = ("ip,username,password,git_path,python_path\n"
       "192.168.1.42,jeff,pw,/volume1/lab-monitor/scripts/lab-monitor,/usr/bin/python3\n")


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / 'collectors.csv'
    p.write_text(CSV)
    return p


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['archive_all_collectors.py'] + argv)
    with pytest.raises(SystemExit) as info:
        archive.main()
    return info.value.code


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('answer,expected', [
    ('ARCHIVE', True), ('  ARCHIVE  ', True), ('archive', False), ('y', False), ('', False),
])
def test_confirm_requires_exact_word(answer, expected):
    assert archive.confirm(3, False, input_fn=lambda prompt: answer) is expected


def test_confirm_eof_is_no():
    def eof(prompt):
        raise EOFError
    assert archive.confirm(3, True, input_fn=eof) is False


def test_main_declined_confirmation_touches_nothing(monkeypatch, csv_file):
    monkeypatch.setattr('builtins.input', lambda prompt: 'no')

    def must_not_run(*a, **kw):
        raise AssertionError('archive_host called without confirmation')
    monkeypatch.setattr(archive, 'archive_host', must_not_run)
    assert _run_main(monkeypatch, ['--csv', str(csv_file)]) == 1


def test_main_yes_skips_prompt_and_alias_moves_queue(monkeypatch, csv_file, capsys):
    def no_prompt(prompt):
        raise AssertionError('prompted despite --yes')
    monkeypatch.setattr('builtins.input', no_prompt)
    seen = {}

    def fake_host(row, timeout, command_timeout, include_queue, dry_run):
        seen.update(include_queue=include_queue, dry_run=dry_run)
        return {'ip': row['ip'], 'ok': True, 'name': 'Triton', 'message': 'Moved 2', 'backup_dir': '/b'}
    monkeypatch.setattr(archive, 'archive_host', fake_host)

    assert _run_main(monkeypatch, ['--csv', str(csv_file), '--yes', '--also-delete-queues']) == 0
    assert seen == {'include_queue': True, 'dry_run': False}
    assert 'deprecated' in capsys.readouterr().out


def test_main_dry_run_does_not_prompt(monkeypatch, csv_file):
    def no_prompt(prompt):
        raise AssertionError('prompted in dry-run')
    monkeypatch.setattr('builtins.input', no_prompt)
    monkeypatch.setattr(archive, 'archive_host', lambda row, *a: {
        'ip': row['ip'], 'ok': True, 'name': 'T', 'message': 'dry', 'backup_dir': '/b'})
    assert _run_main(monkeypatch, ['--csv', str(csv_file), '--dry-run']) == 0


def test_main_reports_failures_with_exit_2(monkeypatch, csv_file):
    monkeypatch.setattr(archive, 'archive_host', lambda row, *a: {
        'ip': row['ip'], 'ok': False, 'name': '?', 'message': 'boom', 'backup_dir': ''})
    assert _run_main(monkeypatch, ['--csv', str(csv_file), '--yes']) == 2


# ---------------------------------------------------------------------------
# Unix command
# ---------------------------------------------------------------------------

def test_unix_command_chains_with_and():
    cmd = archive.build_unix_command('/volume1/lab-monitor/data/Triton', 'backup_1', True)
    steps = cmd.split(' && ')
    assert steps[0] == 'cd /volume1/lab-monitor/data/Triton'
    assert steps[1] == 'mkdir -p backup_1'
    assert any('mv queue.json backup_1/' in s for s in steps)
    assert '|| exit 1' in cmd                  # a failed mv inside the loop aborts too
    assert 'rm ' not in cmd                    # nothing is deleted


def test_unix_command_quotes_paths():
    cmd = archive.build_unix_command("/data/it's here/Triton", 'backup_1', False)
    assert cmd.startswith("cd '/data/it'\"'\"'s here/Triton' && ")


@pytest.mark.skipif(shutil.which('sh') is None, reason='needs a POSIX shell')
def test_unix_command_moves_archives_and_queue(tmp_path):
    system_dir = tmp_path / 'data' / 'Triton'
    system_dir.mkdir(parents=True)
    for name in ('2026-07.jsonl', '2026-08.jsonl', 'queue.json', 'notes.jsonl'):
        (system_dir / name).write_text('x\n')

    cmd = archive.build_unix_command(str(system_dir), 'backup_1', True)
    proc = subprocess.run(['sh', '-c', cmd], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    moved, queue_state, error = archive.parse_archive_output(proc.stdout, True)
    assert (moved, queue_state, error) == (2, 'moved', None)
    assert sorted(p.name for p in (system_dir / 'backup_1').iterdir()) == \
        ['2026-07.jsonl', '2026-08.jsonl', 'queue.json']
    assert sorted(p.name for p in system_dir.iterdir()) == ['backup_1', 'notes.jsonl']


@pytest.mark.skipif(shutil.which('sh') is None, reason='needs a POSIX shell')
def test_unix_command_failed_cd_aborts(tmp_path):
    cmd = archive.build_unix_command(str(tmp_path / 'missing'), 'backup_1', True)
    proc = subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode != 0
    assert 'MOVED' not in proc.stdout
    assert not (tmp_path / 'backup_1').exists()     # no mkdir in the wrong directory


# ---------------------------------------------------------------------------
# Windows command
# ---------------------------------------------------------------------------

def _decode(cmd):
    return base64.b64decode(cmd.split()[-1]).decode('utf-16-le')


def test_windows_command_is_encoded_powershell():
    cmd, backup_dir = archive.build_archive_command(
        'atlantis', 'E:\\Users\\lab-monitor\\data\\', 'backup_1', True, windows=True)
    assert cmd.startswith('powershell ')
    assert backup_dir == 'E:\\Users\\lab-monitor\\data\\atlantis\\backup_1'
    script = _decode(cmd)
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "Set-Location -LiteralPath 'E:\\Users\\lab-monitor\\data\\atlantis'" in script
    assert "Move-Item -LiteralPath 'queue.json'" in script
    assert 'Remove-Item' not in script
    assert 'exit 1' in script


def test_windows_command_without_queue():
    cmd, _ = archive.build_archive_command('atlantis', 'E:/data', 'backup_1', False, windows=True)
    assert 'queue.json' not in _decode(cmd)


# ---------------------------------------------------------------------------
# archive_collector outcomes
# ---------------------------------------------------------------------------

CONFIG = {'name': 'Triton', 'data_dir': '/volume1/lab-monitor/data'}


def _fake_remote(monkeypatch, out='', err='', code=0):
    calls = []

    def run_remote(client, command, timeout):
        calls.append(command)
        return out, err, code
    monkeypatch.setattr(archive, 'run_remote', run_remote)
    return calls


def test_archive_collector_success(monkeypatch):
    calls = _fake_remote(monkeypatch, out='MOVED: 3\nQUEUE: moved')
    r = archive.archive_collector(None, '/volume1/x', CONFIG, 30, include_queue=True)
    assert r['ok'] and r['moved'] == 3 and 'queue.json moved' in r['message']
    assert r['backup_dir'].startswith('/volume1/lab-monitor/data/Triton/backup_')
    assert calls[0].startswith('cd /volume1/lab-monitor/data/Triton && ')


def test_archive_collector_command_failure_is_failure(monkeypatch):
    _fake_remote(monkeypatch, out='MOVED: 3', err="mv: cannot move 'queue.json'", code=1)
    r = archive.archive_collector(None, '/volume1/x', CONFIG, 30, include_queue=True)
    assert not r['ok'] and 'cannot move' in r['message']


def test_archive_collector_unexpected_output_is_failure(monkeypatch):
    _fake_remote(monkeypatch, out='something else')
    r = archive.archive_collector(None, '/volume1/x', CONFIG, 30)
    assert not r['ok'] and 'could not confirm' in r['message']


def test_archive_collector_dry_run_runs_nothing(monkeypatch):
    calls = _fake_remote(monkeypatch)
    r = archive.archive_collector(None, '/volume1/x', CONFIG, 30, include_queue=True, dry_run=True)
    assert r['ok'] and calls == []


@pytest.mark.parametrize('config', [
    {'name': 'bad;name', 'data_dir': '/volume1/data'},
    {'name': 'Triton', 'data_dir': 'data'},          # relative: location unknowable
    {'name': 'Triton'},
])
def test_archive_collector_refuses_unsafe_config(monkeypatch, config):
    calls = _fake_remote(monkeypatch)
    r = archive.archive_collector(None, '/volume1/x', config, 30)
    assert not r['ok'] and calls == []


def test_get_config_requires_ok_status(monkeypatch):
    _fake_remote(monkeypatch, out='{"status": "error", "error": "Config not found"}')
    config, err = archive.get_config(None, '/g', '/p', 30)
    assert config is None and 'Config not found' in err


def test_is_windows_path_shared():
    assert archive.is_windows_path is _ssh.is_windows_path
