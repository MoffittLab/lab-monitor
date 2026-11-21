"""
reset_system.py and archive_all_collectors.py move a collector's queue while
holding its queue lock (queue.json.lock), also move queue.corrupt-*.json, and
never treat queue.json.lock as data.
"""
import importlib.util
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import archive_all_collectors as archive
import reset_system

REPO = Path(__file__).resolve().parents[2]
posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX flock')


def _local_queue():
    """The collector's own queue module (collector/lib/local_queue.py)."""
    spec = importlib.util.spec_from_file_location('lm_collector_local_queue',
                                                  REPO / 'collector' / 'lib' / 'local_queue.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _system(tmp_path, lock_file=True):
    system_dir = tmp_path / 'data' / 'Triton'
    system_dir.mkdir(parents=True)
    for name in ('2026-07.jsonl', '2026-08.jsonl', 'queue.json', 'queue.corrupt-20260801T020000Z.json',
                 'queue.corrupt-20260802T020000Z-1.json', 'network_snapshot.json'):
        (system_dir / name).write_text('[]\n')
    if lock_file:
        (system_dir / 'queue.json.lock').write_bytes(b'')
    return system_dir


def _hold_collector_lock(system_dir, seconds):
    """Hold the queue lock exactly as a collector run does, in a thread, for `seconds`."""
    lq = _local_queue()
    held = threading.Event()

    def run():
        with lq.QueueLock(str(system_dir / 'queue.json')):
            held.set()
            time.sleep(seconds)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert held.wait(5)
    return t


# --------------------------------------------------------------------------- #
# reset_system.py (runs on the collector machine)                             #
# --------------------------------------------------------------------------- #

def test_reset_moves_queue_and_corrupt_files_but_not_the_lock(tmp_path, capsys):
    system_dir = _system(tmp_path)
    backup = Path(reset_system.backup_and_clear(str(tmp_path / 'data'), 'Triton'))
    assert sorted(p.name for p in backup.iterdir()) == [
        '2026-07.jsonl', '2026-08.jsonl', 'queue.corrupt-20260801T020000Z.json',
        'queue.corrupt-20260802T020000Z-1.json', 'queue.json']
    left = sorted(p.name for p in system_dir.iterdir() if p.is_file())
    assert left == ['network_snapshot.json', 'queue.json.lock']
    assert 'WARNING' not in capsys.readouterr().out


def test_reset_moves_the_manager_state_files_but_not_the_host_state(tmp_path, capsys):
    """
    sessions_state.json / storage_health_state.json record what the Manager was last sent: after
    the wipe they would make the collector send "unchanged" session lists and skip health
    messages for up to an hour.  perf_snapshot.json and storage_breakdown_state.json describe
    the host and stay.
    """
    system_dir = _system(tmp_path)
    for name in ('sessions_state.json', 'storage_health_state.json', 'perf_snapshot.json',
                 'storage_breakdown_state.json'):
        (system_dir / name).write_text('{}\n')
    backup = Path(reset_system.backup_and_clear(str(tmp_path / 'data'), 'Triton'))
    moved = sorted(p.name for p in backup.iterdir())
    assert 'sessions_state.json' in moved and 'storage_health_state.json' in moved
    assert 'perf_snapshot.json' not in moved and 'storage_breakdown_state.json' not in moved
    left = sorted(p.name for p in system_dir.iterdir() if p.is_file())
    assert left == ['network_snapshot.json', 'perf_snapshot.json', 'queue.json.lock', 'storage_breakdown_state.json']
    # absent state files (an older collector) are simply not there to move
    other = tmp_path / 'other'
    (other / 'data' / 'Atlas').mkdir(parents=True)
    (other / 'data' / 'Atlas' / 'queue.json').write_text('[]\n')
    assert reset_system.find_manager_state_files(str(other / 'data'), 'Atlas') == []
    backup = Path(reset_system.backup_and_clear(str(other / 'data'), 'Atlas'))
    assert [p.name for p in backup.iterdir()] == ['queue.json']


@posix_only
def test_reset_waits_for_a_running_collector(tmp_path):
    system_dir = _system(tmp_path)
    t = _hold_collector_lock(system_dir, 1.0)
    start = time.monotonic()
    reset_system.backup_and_clear(str(tmp_path / 'data'), 'Triton')
    assert time.monotonic() - start >= 0.7
    assert not (system_dir / 'queue.json').exists()
    t.join()


@posix_only
def test_reset_lock_timeout_moves_nothing(tmp_path, monkeypatch):
    system_dir = _system(tmp_path)
    monkeypatch.setattr(reset_system, 'QUEUE_LOCK_TIMEOUT_SECONDS', 0.3)
    t = _hold_collector_lock(system_dir, 1.5)
    with pytest.raises(reset_system.QueueLockTimeout):
        reset_system.backup_and_clear(str(tmp_path / 'data'), 'Triton')
    assert (system_dir / 'queue.json').exists() and (system_dir / '2026-07.jsonl').exists()
    assert not list(system_dir.glob('backup_*'))
    t.join()


def test_reset_without_lock_file_warns_and_proceeds(tmp_path, capsys):
    system_dir = _system(tmp_path, lock_file=False)
    reset_system.backup_and_clear(str(tmp_path / 'data'), 'Triton')
    assert not (system_dir / 'queue.json').exists()
    assert 'predates queue locking' in capsys.readouterr().out
    assert not (system_dir / 'queue.json.lock').exists()   # not created either


@posix_only
def test_collector_waits_while_reset_holds_the_lock(tmp_path):
    # the other direction: a collector run blocks while reset holds the lock
    system_dir = _system(tmp_path)
    lq = _local_queue()
    with reset_system.queue_lock(str(system_dir)) as status:
        assert status == 'held'
        with pytest.raises(lq.QueueLockTimeout):
            with lq.QueueLock(str(system_dir / 'queue.json'), timeout=0.3):
                pass
    with lq.QueueLock(str(system_dir / 'queue.json'), timeout=1):
        pass


# --------------------------------------------------------------------------- #
# archive_all_collectors.py (remote shell command, run locally here)          #
# --------------------------------------------------------------------------- #

def _shell_env(tmp_path, flock):
    """PATH for the remote-like shell: real coreutils; flock real, BusyBox, or absent."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for tool in ('mv', 'mkdir', 'sleep'):
        os.symlink(shutil.which(tool), bindir / tool)
    if flock == 'util-linux':
        os.symlink(shutil.which('flock'), bindir / 'flock')
    elif flock == 'broken':      # e.g. a BusyBox built without the flock applet
        (bindir / 'flock').write_text('#!/bin/sh\necho "flock: applet not found" >&2\nexit 127\n')
        (bindir / 'flock').chmod(0o755)
    return {'PATH': str(bindir), 'LC_ALL': 'C'}


def _run(cmd, env, shell=('sh', '-c')):
    return subprocess.run([shutil.which(shell[0])] + list(shell[1:]) + [cmd],
                          capture_output=True, text=True, env=env, timeout=60)


needs_sh = pytest.mark.skipif(shutil.which('sh') is None or os.name != 'posix', reason='needs a POSIX shell')


@needs_sh
@pytest.mark.skipif(shutil.which('flock') is None, reason='no flock')
@pytest.mark.parametrize('shell', [
    ('sh', '-c'),
    # Synology DSM / minimal systems: BusyBox ash
    pytest.param(('busybox', 'sh', '-c'),
                 marks=pytest.mark.skipif(shutil.which('busybox') is None, reason='no busybox')),
])
def test_archive_queue_move_waits_for_the_collector_lock(tmp_path, shell):
    flock = 'util-linux'
    system_dir = _system(tmp_path)
    t = _hold_collector_lock(system_dir, 1.5)
    start = time.monotonic()
    proc = _run(archive.build_unix_command(str(system_dir), 'backup_1', True), _shell_env(tmp_path, flock), shell)
    assert proc.returncode == 0, proc.stderr
    assert time.monotonic() - start >= 1.0          # waited for the collector
    t.join()
    assert archive.parse_archive_output(proc.stdout, True) == (2, 'moved', None)
    assert archive.parse_queue_details(proc.stdout) == (2, 'held')
    assert sorted(p.name for p in (system_dir / 'backup_1').iterdir()) == [
        '2026-07.jsonl', '2026-08.jsonl', 'queue.corrupt-20260801T020000Z.json',
        'queue.corrupt-20260802T020000Z-1.json', 'queue.json']
    assert (system_dir / 'queue.json.lock').exists()


@needs_sh
@pytest.mark.skipif(shutil.which('flock') is None, reason='no flock')
def test_archive_queue_lock_timeout_leaves_the_queue(tmp_path, monkeypatch):
    system_dir = _system(tmp_path)
    monkeypatch.setattr(archive, 'QUEUE_LOCK_WAIT_SECONDS', 1)
    t = _hold_collector_lock(system_dir, 3)
    proc = _run(archive.build_unix_command(str(system_dir), 'backup_1', True), _shell_env(tmp_path, 'util-linux'))
    t.join()
    assert proc.returncode == 75
    assert 'held by a running collector' in proc.stderr
    assert (system_dir / 'queue.json').exists()
    assert 'QUEUE:' not in proc.stdout


@needs_sh
@pytest.mark.parametrize('lock_file,flock,expected', [
    (True, None, 'unavailable (no working flock command)'),
    (True, 'broken', 'unavailable (no working flock command)'),
    (False, None, 'none (collector predates queue locking)'),
])
def test_archive_queue_move_without_lock_warns(tmp_path, monkeypatch, lock_file, flock, expected):
    system_dir = _system(tmp_path, lock_file=lock_file)
    start = time.monotonic()
    proc = _run(archive.build_unix_command(str(system_dir), 'backup_1', True), _shell_env(tmp_path, flock))
    assert time.monotonic() - start < 10                 # no retry loop
    assert proc.returncode == 0, proc.stderr
    assert archive.parse_queue_details(proc.stdout) == (2, expected)
    assert not (system_dir / 'queue.json').exists()

    # archive_collector reports it
    monkeypatch.setattr(archive, 'run_remote', lambda client, command, timeout: (proc.stdout, '', 0))
    r = archive.archive_collector(None, '/volume1/x', {'name': 'Triton', 'data_dir': '/volume1/d'}, 30,
                                  include_queue=True)
    assert r['ok'] and '2 queue.corrupt-*.json' in r['message'] and 'without the queue lock' in r['message']


def test_archive_without_queue_flag_leaves_queue_files(tmp_path):
    if shutil.which('sh') is None:
        pytest.skip('needs a POSIX shell')
    system_dir = _system(tmp_path)
    proc = subprocess.run(['sh', '-c', archive.build_unix_command(str(system_dir), 'backup_1', False)],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert sorted(p.name for p in (system_dir / 'backup_1').iterdir()) == ['2026-07.jsonl', '2026-08.jsonl']


def test_windows_script_locks_and_moves_corrupt_queues():
    script = archive.build_windows_script('E:\\data\\atlantis', 'backup_1', True)
    assert "[System.IO.File]::Open($lockPath" in script and '$lock.Lock(0, 1)' in script
    assert '$lock.Unlock(0, 1)' in script                       # released in finally
    assert "'^queue\\.corrupt-.*\\.json$'" in script
    assert "Move-Item -LiteralPath 'queue.json.lock'" not in script
    assert 'Remove-Item' not in script
    script.encode('ascii')                                       # PowerShell 5.1 reads BOM-less files as ANSI
