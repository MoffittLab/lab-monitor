"""Tests for tools/replay_archives.py: reading archives, batching, ACK handling (mock requests)."""
import json
import re

import pytest

import replay_archives as replay


def _msg(i, name='Triton', msg_id=True):
    header = {'device_name': name, 'device_id': f'synology-{name.lower()}',
              'device_type': 'NAS', 'timestamp': f'2026-08-04T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z'}
    if msg_id:
        header['msg_id'] = f'{i:032x}'
    return {'header': header, 'data': {'data_type': 'system_metrics', 'cpu_percent': i % 100}}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeManager:
    """Fake requests.Session: records POSTs, answers like the new Manager."""

    def __init__(self, statuses=None, legacy=False, max_messages=None):
        self.posts = []
        self.statuses = statuses or (lambda msg, attempt: 'stored')
        self.legacy = legacy
        self.max_messages = max_messages
        self.attempts = {}

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append({'url': url, 'payload': json, 'timeout': timeout, 'headers': headers})
        msgs = json['messages']
        if self.max_messages and len(msgs) > self.max_messages:
            return FakeResponse(413, {'status': 'error', 'error': 'too large'})
        if self.legacy:
            return FakeResponse(200, {'status': 'ok', 'queue_id': json['queue_id'], 'stored': len(msgs)})
        results = []
        for m in msgs:
            key = m['header'].get('msg_id') or m['header']['timestamp']
            self.attempts[key] = self.attempts.get(key, 0) + 1
            results.append({'msg_id': key, 'status': self.statuses(m, self.attempts[key])})
        counts = {s: sum(1 for r in results if r['status'] == s)
                  for s in ('stored', 'duplicate', 'rejected', 'error')}
        return FakeResponse(200, {'status': 'ok', 'queue_id': json['queue_id'], 'received': len(msgs),
                                  'stored': counts['stored'] + counts['duplicate'] + counts['rejected'],
                                  'counts': counts, 'results': results})


def _msgs(n, **kw):
    return [_msg(i, **kw) for i in range(n)]


def test_iter_messages_skips_bad_lines(tmp_path):
    p = tmp_path / '2026-08.jsonl'
    lines = [json.dumps(_msg(1)), '', '{not json', json.dumps({'type': 'disk', 'timestamp': 'x'}),
             json.dumps(_msg(2, msg_id=False))]
    p.write_text('\n'.join(lines) + '\n')
    stats = replay.ReadStats()
    messages = list(replay.iter_messages(p, stats))
    assert len(messages) == 2 and stats.read == 2
    assert stats.bad_lines == 2 and len(stats.bad_examples) == 2
    assert 'msg_id' not in messages[1]['header']      # read unchanged, no id invented


def test_directory_source_reads_archives_not_queue(tmp_path):
    for name in ('2026-07.jsonl', '2026-08.jsonl', 'queue.json', 'notes.txt'):
        (tmp_path / name).write_text('')
    files = replay.source_files([str(tmp_path)])
    assert [f.name for f in files] == ['2026-07.jsonl', '2026-08.jsonl']
    assert [f.name for f in replay.source_files([str(tmp_path / 'queue.json')])] == ['queue.json']


def test_queue_json_source(tmp_path):
    p = tmp_path / 'queue.json'
    p.write_text(json.dumps([_msg(1), _msg(2)]))
    stats = replay.ReadStats()
    assert len(list(replay.iter_messages(p, stats))) == 2 and stats.bad_lines == 0


def _write_archive(path, msgs):
    path.write_text(''.join(json.dumps(m) + '\n' for m in msgs))
    return path


def test_select_skips_legacy_by_default_and_repeated_ids(tmp_path):
    a = _write_archive(tmp_path / '2026-07.jsonl', [_msg(1), _msg(2, msg_id=False), _msg(3)])
    b = _write_archive(tmp_path / '2026-08.jsonl', [_msg(3), _msg(4, msg_id=False), _msg(5)])
    stats = replay.ReadStats()
    picked = list(replay.select_messages([a, b], stats))
    # Pre-upgrade messages (no msg_id) would duplicate pre-dedup rows the Manager holds.
    assert [m['header']['msg_id'] for m in picked] == [f'{i:032x}' for i in (1, 3, 5)]
    assert (stats.read, stats.legacy, stats.repeated, stats.selected) == (6, 2, 1, 3)

    stats = replay.ReadStats()
    picked = list(replay.select_messages([a, b], stats, include_legacy=True))
    assert len(picked) == 5 and stats.legacy == 0     # legacy kept (Manager derives their id)


def test_select_window(tmp_path):
    a = _write_archive(tmp_path / '2026-08.jsonl', [_msg(3600 * h) for h in (6, 7, 8)])
    stats = replay.ReadStats()
    picked = list(replay.select_messages([a], stats, since='2026-08-04T07', until='2026-08-04T08'))
    assert len(picked) == 1 and stats.outside_window == 2


def test_replay_streams_without_reading_everything_first():
    """The first batch goes out after batch_size messages were read, not after the whole archive."""
    produced = []

    def source():
        for i in range(250):
            produced.append(i)
            yield _msg(i)

    class Recording(FakeManager):
        def post(self, url, json=None, timeout=None, headers=None):
            self.produced_at_post = getattr(self, 'produced_at_post', []) + [len(produced)]
            return super().post(url, json=json, timeout=timeout, headers=headers)

    fake = Recording()
    totals = replay.replay(source(), 'http://m', 't', 100, 5, retry_delay=0, session=fake)
    assert totals['stored'] == 250
    assert fake.produced_at_post == [100, 100, 200, 250]       # probe + 3 batches


def test_replay_splits_interleaved_devices_into_own_batches():
    msgs = [_msg(i, name='Triton' if i % 2 else 'atlantis') for i in range(10)]
    fake = FakeManager()
    totals = replay.replay(msgs, 'http://m', 't', 3, 5, retry_delay=0, session=fake)
    assert totals['stored'] == 10
    for post in fake.posts:
        payload = post['payload']
        assert all(m['header']['device_name'] == payload['name'] for m in payload['messages'])
    probes = [p['payload']['name'] for p in fake.posts if not p['payload']['messages']]
    assert sorted(probes) == ['Triton', 'atlantis']                 # one probe per device


def test_replay_batches_of_100(monkeypatch):
    fake = FakeManager()
    totals = replay.replay(_msgs(250), 'http://mgr:5000/', 'tok', batch_size=100, timeout=12,
                           retry_delay=0, session=fake)
    probe, *batches = fake.posts
    assert probe['payload']['messages'] == []
    assert [len(p['payload']['messages']) for p in batches] == [100, 100, 50]
    for p in fake.posts:
        assert p['url'] == 'http://mgr:5000/api/data/queue'
        assert set(p['payload']) == {'queue_id', 'name', 'id', 'messages'}
        assert re.fullmatch(r'Triton-[0-9a-f]{12}', p['payload']['queue_id'])
        assert p['payload']['name'] == 'Triton' and p['payload']['id'] == 'synology-triton'
        assert p['headers']['Authorization'] == 'Bearer tok' and p['timeout'] == 12
    assert len({p['payload']['queue_id'] for p in fake.posts}) == len(fake.posts)
    assert totals['stored'] == 250 and totals['error'] == totals['undelivered'] == 0


def test_replay_counts_duplicates_and_rejections():
    def statuses(msg, attempt):
        i = int(msg['header']['msg_id'], 16)
        return 'duplicate' if i < 10 else 'rejected' if i == 10 else 'stored'
    totals = replay.replay(_msgs(30), 'http://m', 't', 100, 5, retry_delay=0,
                           session=FakeManager(statuses))
    assert (totals['duplicate'], totals['rejected'], totals['stored']) == (10, 1, 19)


def test_replay_retries_only_transient_errors():
    def statuses(msg, attempt):
        return 'error' if attempt == 1 and int(msg['header']['msg_id'], 16) % 2 else 'stored'
    fake = FakeManager(statuses)
    totals = replay.replay(_msgs(10), 'http://m', 't', 100, 5, retry_delay=0, session=fake)
    assert totals['stored'] == 10 and totals['error'] == 0
    assert [len(p['payload']['messages']) for p in fake.posts] == [0, 10, 5]


def test_replay_gives_up_after_retries():
    fake = FakeManager(lambda msg, attempt: 'error')
    totals = replay.replay(_msgs(4), 'http://m', 't', 100, 5, retries=2, retry_delay=0, session=fake)
    assert totals['error'] == 4 and totals['stored'] == 0
    assert len(fake.posts) == 1 + 3                    # probe + first try + 2 retries


def test_replay_splits_batch_on_413():
    fake = FakeManager(max_messages=30)
    totals = replay.replay(_msgs(100), 'http://m', 't', 100, 5, retry_delay=0, session=fake)
    assert totals['stored'] == 100 and totals['undelivered'] == 0
    sizes = [len(p['payload']['messages']) for p in fake.posts[1:]]
    assert sizes[:3] == [100, 50, 25]                  # 413 → halve until it fits
    assert sum(s for s in sizes if s <= 30) == 100     # every message accepted exactly once


def test_replay_refuses_old_manager_without_dedup():
    fake = FakeManager(legacy=True)
    totals = replay.replay(_msgs(5), 'http://m', 't', 100, 5, retry_delay=0, session=fake)
    assert totals['undelivered'] == 5 and totals['stored'] == 0
    assert len(fake.posts) == 1                        # only the empty probe was sent


def test_replay_old_manager_allowed_explicitly():
    fake = FakeManager(legacy=True)
    totals = replay.replay(_msgs(5), 'http://m', 't', 100, 5, retry_delay=0, session=fake,
                           allow_duplicates=True)
    assert totals['stored'] == 5 and totals['legacy_ack'] == 5


def test_replay_auth_refused_stops():
    class Forbidden(FakeManager):
        def post(self, url, json=None, timeout=None, headers=None):
            self.posts.append(json)
            return FakeResponse(403, {'status': 'error', 'error': 'forbidden'})
    with pytest.raises(replay.AuthRefused):
        replay.replay(_msgs(5), 'http://m', 't', 100, 5, retry_delay=0, session=Forbidden())


def test_replay_queue_id_mismatch_is_retried():
    class Mismatch(FakeManager):
        def post(self, url, json=None, timeout=None, headers=None):
            resp = super().post(url, json=json, timeout=timeout, headers=headers)
            if json['messages'] and len(self.posts) == 2:
                resp._body = dict(resp._body, queue_id='someone-else')
            return resp
    fake = Mismatch()
    totals = replay.replay(_msgs(3), 'http://m', 't', 100, 5, retry_delay=0, session=fake)
    assert totals['stored'] == 3
    assert len(fake.posts) == 3                        # probe, mismatched ACK, retry


def test_in_window_compares_iso_prefixes():
    m = _msg(3600 * 7)                                  # 2026-08-04T07:00:00Z
    assert replay.in_window(m, since='2026-08-04', until='2026-08-05')
    assert replay.in_window(m, since='2026-08-04T07:00')
    assert not replay.in_window(m, since='2026-08-04T07:01')
    assert not replay.in_window(m, until='2026-08-04T07:00')
    assert replay.in_window(m)


def test_main_dry_run_sends_nothing(monkeypatch, tmp_path, capsys):
    import sys
    src = tmp_path / '2026-08.jsonl'
    src.write_text('\n'.join(json.dumps(_msg(i)) for i in range(5)) + '\n')

    class NoSession:
        def __init__(self, *a, **kw):
            raise AssertionError('network used in dry-run')
    monkeypatch.setattr(replay.requests, 'Session', NoSession)
    monkeypatch.setattr(sys, 'argv', ['replay_archives.py', '--source', str(tmp_path), '--dry-run'])
    with pytest.raises(SystemExit) as info:
        replay.main()
    assert info.value.code == 0
    assert 'Triton: 5 message(s), 1 batch(es)' in capsys.readouterr().out


def test_main_all_legacy_explains_include_legacy(monkeypatch, tmp_path, capsys):
    import sys
    _write_archive(tmp_path / '2026-08.jsonl', [_msg(i, msg_id=False) for i in range(4)])
    monkeypatch.setattr(sys, 'argv', ['replay_archives.py', '--source', str(tmp_path), '--dry-run'])
    with pytest.raises(SystemExit) as info:
        replay.main()
    out = capsys.readouterr().out
    assert info.value.code == 1 and 'Nothing to replay' in out
    assert '4 message(s) without a msg_id' in out and '--include-legacy' in out

    monkeypatch.setattr(sys, 'argv', ['replay_archives.py', '--source', str(tmp_path), '--dry-run',
                                      '--include-legacy'])
    with pytest.raises(SystemExit) as info:
        replay.main()
    assert info.value.code == 0 and 'Triton: 4 message(s), 1 batch(es)' in capsys.readouterr().out
