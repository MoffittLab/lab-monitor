"""queue.json handling: locking, atomic writes, corrupt files, msg_id assignment, cap."""
import glob
import hashlib
import json
import logging
import multiprocessing
import os
import sys
import time

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32', reason='uses fork')


def _legacy_entry(i):
    """A message as written by a pre-msg_id collector."""
    return {
        'header': {'device_name': 'testbox', 'device_id': 'linux-testbox',
                   'device_type': 'Server', 'timestamp': f'2026-08-04T07:0{i}:00Z'},
        'data': {'data_type': 'system_metrics', 'cpu_percent': 1.0},
    }


def _manager_legacy_id(name, data_type, timestamp, username):
    """Independent copy of the Manager's derivation (contract section 1)."""
    key = '|'.join((name, data_type, timestamp, username))
    return 'legacy-' + hashlib.sha1(key.encode('utf-8')).hexdigest()


def test_legacy_id_falls_back_to_queue_directory_name(tmp_path, local_queue):
    path = str(tmp_path / 'Triton' / 'queue.json')
    os.makedirs(os.path.dirname(path))
    with open(path, 'w') as f:
        json.dump([{'header': {'timestamp': 'T1'},
                    'data': {'data_type': 'user_metrics', 'username': 'alice'}}], f)
    (entry,) = local_queue.snapshot(path)
    assert entry['header']['msg_id'] == _manager_legacy_id('Triton', 'user_metrics', 'T1', 'alice')


def test_missing_queue_is_empty(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    assert local_queue.snapshot(path) == []
    assert not os.path.exists(path)


def test_legacy_entries_get_stable_msg_ids(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    with open(path, 'w') as f:
        json.dump([_legacy_entry(1), _legacy_entry(2)], f, indent=2)   # old on-disk format

    first = local_queue.snapshot(path)
    ids = [e['header']['msg_id'] for e in first]
    # Same id the Manager derives for a message without msg_id, so a message an
    # older collector already delivered is deduplicated when re-sent
    assert ids == [_manager_legacy_id('testbox', 'system_metrics', f'2026-08-04T07:0{i}:00Z', '')
                   for i in (1, 2)]

    # The ids were written back, so the next read (and every retry) sees the same ones
    with open(path) as f:
        on_disk = json.load(f)
    assert [e['header']['msg_id'] for e in on_disk] == ids
    assert [e['header']['msg_id'] for e in local_queue.snapshot(path)] == ids


def test_existing_msg_ids_are_kept(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    entry = _legacy_entry(1)
    entry['header']['msg_id'] = 'a' * 32
    with open(path, 'w') as f:
        json.dump([entry], f)
    mtime = os.stat(path).st_mtime_ns
    assert local_queue.snapshot(path)[0]['header']['msg_id'] == 'a' * 32
    assert os.stat(path).st_mtime_ns == mtime   # nothing to fix → not rewritten


def test_corrupt_queue_is_moved_aside_not_discarded(tmp_path, local_queue, caplog):
    path = str(tmp_path / 'queue.json')
    garbage = '[{"header": {"device_name": "testbox"'      # truncated write
    with open(path, 'w') as f:
        f.write(garbage)

    with caplog.at_level(logging.ERROR):
        assert local_queue.snapshot(path) == []
    corrupt = glob.glob(str(tmp_path / 'queue.corrupt-*.json'))
    assert len(corrupt) == 1
    with open(corrupt[0]) as f:
        assert f.read() == garbage
    assert not os.path.exists(path)
    assert 'could not be parsed' in caplog.text

    # A new queue starts cleanly and the quarantined file is left alone
    local_queue.enqueue(path, [_legacy_entry(3)])
    assert len(local_queue.snapshot(path)) == 1
    assert glob.glob(str(tmp_path / 'queue.corrupt-*.json')) == corrupt


def test_non_list_queue_is_quarantined(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    with open(path, 'w') as f:
        json.dump({'not': 'a list'}, f)
    assert local_queue.snapshot(path) == []
    assert len(glob.glob(str(tmp_path / 'queue.corrupt-*.json'))) == 1


def test_two_corrupt_files_in_one_second_dont_collide(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    for _ in range(2):
        with open(path, 'w') as f:
            f.write('{{{')
        local_queue.snapshot(path)
    assert len(glob.glob(str(tmp_path / 'queue.corrupt-*.json'))) == 2


def test_malformed_entries_are_set_aside(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    with open(path, 'w') as f:
        json.dump([42, _legacy_entry(1), {'header': 'nope'}], f)
    entries = local_queue.snapshot(path)
    assert len(entries) == 1 and entries[0]['data']['cpu_percent'] == 1.0
    (sidecar,) = glob.glob(str(tmp_path / 'queue.corrupt-*.json'))
    with open(sidecar) as f:
        assert json.load(f) == [42, {'header': 'nope'}]


def test_empty_file_is_an_empty_queue(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    open(path, 'w').close()
    assert local_queue.snapshot(path) == []
    assert glob.glob(str(tmp_path / 'queue.corrupt-*.json')) == []


def test_queue_written_compact_and_atomically(tmp_path, local_queue, monkeypatch):
    path = str(tmp_path / 'queue.json')
    local_queue.enqueue(path, [_legacy_entry(1)])
    with open(path) as f:
        text = f.read()
    assert '\n' not in text and ': ' not in text          # no indent=2

    # A failure while replacing must leave the previous queue intact and no temp files
    def boom(src, dst, attempts=5):
        raise OSError('disk full')
    monkeypatch.setattr(local_queue, '_replace_with_retry', boom)
    with pytest.raises(OSError):
        local_queue.enqueue(path, [_legacy_entry(2)])
    with open(path) as f:
        assert f.read() == text
    assert [p for p in os.listdir(tmp_path) if p.endswith('.tmp')] == []


def test_queue_file_removed_when_drained(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    local_queue.enqueue(path, [_legacy_entry(1), _legacy_entry(2)])
    ids = [e['header']['msg_id'] for e in local_queue.snapshot(path)]
    assert local_queue.remove_acked(path, ids) == 2
    assert not os.path.exists(path)


def test_remove_rereads_so_new_entries_survive(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    local_queue.enqueue(path, [_legacy_entry(1), _legacy_entry(2)])
    sent = local_queue.snapshot(path)
    local_queue.enqueue(path, [_legacy_entry(3)])          # appended "during the POST"
    local_queue.remove_acked(path, [e['header']['msg_id'] for e in sent])
    remaining = local_queue.snapshot(path)
    assert [e['header']['timestamp'] for e in remaining] == ['2026-08-04T07:03:00Z']


def test_cap_drops_oldest_with_warning(tmp_path, local_queue, caplog):
    path = str(tmp_path / 'queue.json')
    local_queue.enqueue(path, [_legacy_entry(i) for i in range(6)], max_messages=0)  # 0 = no cap
    with caplog.at_level(logging.WARNING):
        n = local_queue.enqueue(path, [_legacy_entry(7), _legacy_entry(8)], max_messages=5)
    assert n == 5
    stamps = [e['header']['timestamp'][15:16] for e in local_queue.snapshot(path)]
    assert stamps == ['3', '4', '5', '7', '8']             # oldest three dropped
    assert 'OLDEST' in caplog.text and 'archive' in caplog.text


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------

def _hold_lock(queue_path, seconds, ready):
    from lib import local_queue
    with local_queue.QueueLock(queue_path):
        ready.set()
        time.sleep(seconds)


def _append_many(queue_path, worker, count):
    from lib import local_queue
    for i in range(count):
        local_queue.enqueue(queue_path, [{
            'header': {'device_name': 'testbox', 'timestamp': f'w{worker}-{i}'},
            'data': {'data_type': 'system_metrics'},
        }])


@posix_only
def test_lock_excludes_other_processes(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    ctx = multiprocessing.get_context('fork')
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(path, 1.0, ready))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(local_queue.QueueLockTimeout):
            with local_queue.QueueLock(path, timeout=0.2):
                pass
        start = time.monotonic()
        with local_queue.QueueLock(path, timeout=10):      # acquired once the holder exits
            assert time.monotonic() - start > 0.1
    finally:
        holder.join(10)


@posix_only
def test_concurrent_appends_lose_nothing(tmp_path, local_queue):
    path = str(tmp_path / 'queue.json')
    ctx = multiprocessing.get_context('fork')
    workers = [ctx.Process(target=_append_many, args=(path, w, 40)) for w in range(4)]
    for p in workers:
        p.start()
    for p in workers:
        p.join(60)
        assert p.exitcode == 0
    entries = local_queue.snapshot(path)
    assert len(entries) == 160
    assert len({e['header']['msg_id'] for e in entries}) == 160
    assert {e['header']['timestamp'] for e in entries} == {f'w{w}-{i}' for w in range(4) for i in range(40)}


# ---------------------------------------------------------------------------
# Lock errors other than contention; files usable by other accounts
# ---------------------------------------------------------------------------

def _failing_lock(local_queue, monkeypatch, err):
    calls = []

    def fail(fh):
        calls.append(1)
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(local_queue, '_lock_file', fail)
    monkeypatch.setattr(local_queue, '_unlocked_warned', set())
    return calls


@pytest.mark.skipif(not hasattr(__import__('errno'), 'ENOLCK'), reason='no ENOLCK')
def test_filesystem_without_locks_works_unlocked_with_loud_error(tmp_path, local_queue,
                                                                  monkeypatch, caplog):
    # e.g. data_dir on an NFS mount without lockd: flock fails with ENOLCK.
    # It used to be retried as contention: a 120 s stall per run, then a
    # misleading timeout, and nothing was ever queued.
    import errno
    _failing_lock(local_queue, monkeypatch, errno.ENOLCK)
    path = str(tmp_path / 'queue.json')
    start = time.monotonic()
    with caplog.at_level(logging.ERROR):
        local_queue.enqueue(path, [_legacy_entry(1)])
        local_queue.enqueue(path, [_legacy_entry(2)])
    assert time.monotonic() - start < 5
    assert len(local_queue.snapshot(path)) == 2
    assert caplog.text.count('WITHOUT a lock') == 1          # once per process, not per call


def test_other_lock_errors_are_raised_at_once(tmp_path, local_queue, monkeypatch):
    import errno
    calls = _failing_lock(local_queue, monkeypatch, errno.EIO)
    start = time.monotonic()
    with pytest.raises(OSError) as info:
        with local_queue.QueueLock(str(tmp_path / 'queue.json'), timeout=30):
            pass
    assert not isinstance(info.value, local_queue.QueueLockTimeout)
    assert info.value.errno == errno.EIO
    assert len(calls) == 1 and time.monotonic() - start < 5


def test_contention_errors_are_still_retried(tmp_path, local_queue, monkeypatch):
    import errno
    calls = _failing_lock(local_queue, monkeypatch, errno.EWOULDBLOCK)
    with pytest.raises(local_queue.QueueLockTimeout):
        with local_queue.QueueLock(str(tmp_path / 'queue.json'), timeout=0.3, poll_interval=0.01):
            pass
    assert len(calls) > 1


@posix_only
def test_queue_files_follow_umask_not_0600(tmp_path, local_queue):
    # mkstemp creates 0600. queue.json holds no secrets; a 0600 queue left by a
    # one-off run under another account (sudo) would stop the scheduled account
    # from reading, and so from sending, its queue.
    path = str(tmp_path / 'queue.json')
    old = os.umask(0o022)
    try:
        local_queue.enqueue(path, [_legacy_entry(1)])
    finally:
        os.umask(old)
    assert os.stat(path).st_mode & 0o777 == 0o644


@posix_only
def test_lock_file_the_user_cannot_write_is_opened_read_only(tmp_path, local_queue):
    if os.geteuid() == 0:
        pytest.skip('root can write anything')
    path = str(tmp_path / 'queue.json')
    open(path + '.lock', 'w').close()
    os.chmod(path + '.lock', 0o444)           # e.g. created by root, mode 0644
    local_queue.enqueue(path, [_legacy_entry(1)])
    assert len(local_queue.snapshot(path)) == 1
