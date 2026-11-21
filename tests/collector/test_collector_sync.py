"""Batched POST + per-message ACK handling (requests.post is mocked)."""
import json
import logging
import os
import re

import pytest


@pytest.fixture
def queue_path(collector, config):
    return collector.get_queue_path(config, config['name'])


def _seed(local_queue, queue_path, entries):
    local_queue.enqueue(queue_path, entries)
    return [e['header']['msg_id'] for e in entries]


def _sync(collector, config, queue_path):
    return collector.sync_queue_to_manager(config, config['name'], config['id'],
                                           queue_path, logging.getLogger('test'))


def _queued_ids(helpers, queue_path):
    return [e['header']['msg_id'] for e in helpers.read_queue_file(queue_path)]


def test_new_ack_removes_handled_and_keeps_errors(collector, local_queue, config, queue_path,
                                                  fake_manager, helpers):
    ids = _seed(local_queue, queue_path, helpers.make_entries(4))
    statuses = {ids[0]: 'stored', ids[1]: 'duplicate', ids[2]: 'rejected', ids[3]: 'error'}
    fake_manager.handler = lambda p: helpers.FakeManager.ack(p, statuses)

    assert _sync(collector, config, queue_path) is False     # one message still pending
    assert _queued_ids(helpers, queue_path) == [ids[3]]

    # Next run re-sends exactly that message, with the same msg_id
    fake_manager.handler = helpers.FakeManager.ack_all
    assert _sync(collector, config, queue_path) is True
    assert [m['header']['msg_id'] for m in fake_manager.calls[-1]['payload']['messages']] == [ids[3]]
    assert not os.path.exists(queue_path)


def test_payload_shape_and_auth_header(collector, local_queue, config, queue_path,
                                       fake_manager, helpers):
    config['manager_url'] = 'http://manager.invalid:5000/'   # trailing slash tolerated
    _seed(local_queue, queue_path, helpers.make_entries(2))
    assert _sync(collector, config, queue_path) is True
    call = fake_manager.calls[0]
    assert call['url'] == 'http://manager.invalid:5000/api/data/queue'
    assert call['headers']['Authorization'] == 'Bearer collector-token-0123456789'
    assert call['timeout'] == 5
    payload = call['payload']
    assert set(payload) == {'queue_id', 'name', 'id', 'messages'}
    assert re.fullmatch(r'testbox-[0-9a-f]{12}', payload['queue_id'])
    assert payload['name'] == 'testbox' and payload['id'] == 'linux-testbox'


def test_old_manager_full_store_drains_batch(collector, local_queue, config, queue_path,
                                             fake_manager, helpers):
    _seed(local_queue, queue_path, helpers.make_entries(3))
    fake_manager.handler = lambda p: helpers.FakeResponse(
        200, {'status': 'ok', 'queue_id': p['queue_id'], 'stored': len(p['messages'])})
    assert _sync(collector, config, queue_path) is True
    assert not os.path.exists(queue_path)


def test_old_manager_short_store_keeps_batch_and_stops(collector, local_queue, config, queue_path,
                                                       fake_manager, helpers):
    config['max_batch_messages'] = 2
    ids = _seed(local_queue, queue_path, helpers.make_entries(4))
    fake_manager.handler = lambda p: helpers.FakeResponse(
        200, {'status': 'ok', 'queue_id': p['queue_id'], 'stored': len(p['messages']) - 1})
    assert _sync(collector, config, queue_path) is False
    assert _queued_ids(helpers, queue_path) == ids
    assert len(fake_manager.calls) == 1      # an old Manager would duplicate on resend; don't push more


def test_queue_id_mismatch_keeps_queue(collector, local_queue, config, queue_path,
                                       fake_manager, helpers):
    ids = _seed(local_queue, queue_path, helpers.make_entries(2))
    fake_manager.handler = lambda p: helpers.FakeManager.ack(dict(p, queue_id='someone-else'))
    assert _sync(collector, config, queue_path) is False
    assert _queued_ids(helpers, queue_path) == ids


def test_batches_of_max_batch_messages_oldest_first(collector, local_queue, config, queue_path,
                                                    fake_manager, helpers):
    ids = _seed(local_queue, queue_path, helpers.make_entries(250))
    assert _sync(collector, config, queue_path) is True       # default max_batch_messages=100
    batches = [[m['header']['msg_id'] for m in c['payload']['messages']] for c in fake_manager.calls]
    assert [len(b) for b in batches] == [100, 100, 50]
    assert sum(batches, []) == ids
    queue_ids = [c['payload']['queue_id'] for c in fake_manager.calls]
    assert len(set(queue_ids)) == 3
    assert not os.path.exists(queue_path)


def test_removals_are_flushed_periodically(collector, local_queue, config, queue_path,
                                           fake_manager, helpers, monkeypatch):
    config['max_batch_messages'] = 1
    _seed(local_queue, queue_path, helpers.make_entries(25))
    calls = []
    real_remove = local_queue.remove_acked
    monkeypatch.setattr(local_queue, 'remove_acked',
                        lambda path, ids, log=None: calls.append(len(ids)) or real_remove(path, ids, log))
    assert _sync(collector, config, queue_path) is True
    assert calls == [10, 10, 5]


def test_messages_appended_during_post_are_kept(collector, local_queue, config, queue_path,
                                                fake_manager, helpers):
    _seed(local_queue, queue_path, helpers.make_entries(3))
    late = helpers.make_entries(1)[0]

    def handler(payload):
        # Another collector run appends while the HTTP request is in flight.
        # This would time out if the sync held the queue lock during the POST.
        with local_queue.QueueLock(queue_path, timeout=2):
            local_queue.enqueue_locked(queue_path, [late])
        return helpers.FakeManager.ack(payload)

    fake_manager.handler = handler
    assert _sync(collector, config, queue_path) is True
    assert _queued_ids(helpers, queue_path) == [late['header']['msg_id']]


def test_network_error_keeps_queue(collector, local_queue, config, queue_path,
                                   fake_manager, helpers):
    ids = _seed(local_queue, queue_path, helpers.make_entries(2))

    def handler(payload):
        raise collector.requests.exceptions.ConnectionError('manager down')
    fake_manager.handler = handler
    assert _sync(collector, config, queue_path) is False
    assert _queued_ids(helpers, queue_path) == ids


@pytest.mark.parametrize('status', [401, 403, 400, 500])
def test_http_errors_stop_and_keep_queue(collector, local_queue, config, queue_path,
                                         fake_manager, helpers, status):
    config['max_batch_messages'] = 1
    ids = _seed(local_queue, queue_path, helpers.make_entries(3))
    fake_manager.handler = lambda p: helpers.FakeResponse(status, {'status': 'error', 'error': 'x'})
    assert _sync(collector, config, queue_path) is False
    assert len(fake_manager.calls) == 1
    assert _queued_ids(helpers, queue_path) == ids


def test_413_splits_batch(collector, local_queue, config, queue_path, fake_manager, helpers):
    _seed(local_queue, queue_path, helpers.make_entries(4))

    def handler(payload):
        if len(payload['messages']) > 2:
            return helpers.FakeResponse(413, {'status': 'error', 'error': 'too large'})
        return helpers.FakeManager.ack(payload)

    fake_manager.handler = handler
    assert _sync(collector, config, queue_path) is True
    assert [len(c['payload']['messages']) for c in fake_manager.calls] == [4, 2, 2]
    assert not os.path.exists(queue_path)


def test_batch_with_no_progress_stops_the_run(collector, local_queue, config, queue_path,
                                              fake_manager, helpers):
    config['max_batch_messages'] = 2
    ids = _seed(local_queue, queue_path, helpers.make_entries(6))
    fake_manager.handler = lambda p: helpers.FakeManager.ack(
        p, {m['header']['msg_id']: 'error' for m in p['messages']})
    assert _sync(collector, config, queue_path) is False
    assert len(fake_manager.calls) == 1
    assert _queued_ids(helpers, queue_path) == ids


def test_partial_errors_continue_with_next_batch(collector, local_queue, config, queue_path,
                                                 fake_manager, helpers):
    config['max_batch_messages'] = 2
    ids = _seed(local_queue, queue_path, helpers.make_entries(4))
    fake_manager.handler = lambda p: helpers.FakeManager.ack(p, {ids[0]: 'error'})
    assert _sync(collector, config, queue_path) is False
    assert len(fake_manager.calls) == 2
    assert _queued_ids(helpers, queue_path) == [ids[0]]


def test_legacy_queue_entries_keep_their_assigned_id_across_retries(
        collector, local_queue, config, queue_path, fake_manager, helpers):
    legacy = [{'header': {'device_name': 'testbox', 'timestamp': '2026-08-04T07:00:00Z'},
               'data': {'data_type': 'system_metrics'}}]
    with open(queue_path, 'w') as f:
        json.dump(legacy, f, indent=2)

    fake_manager.handler = lambda p: helpers.FakeResponse(503, text='busy')
    assert _sync(collector, config, queue_path) is False
    fake_manager.handler = helpers.FakeManager.ack_all
    assert _sync(collector, config, queue_path) is True
    first, second = (c['payload']['messages'][0]['header']['msg_id'] for c in fake_manager.calls)
    assert first == second and first.startswith('legacy-')


def test_missing_requests_package_is_reported(collector, local_queue, config, queue_path,
                                              helpers, monkeypatch, caplog):
    _seed(local_queue, queue_path, helpers.make_entries(1))
    monkeypatch.setattr(collector, 'requests', None)
    with caplog.at_level(logging.ERROR):
        assert _sync(collector, config, queue_path) is False
    assert "'requests' is not installed" in caplog.text


def test_empty_queue_is_success(collector, config, queue_path, fake_manager):
    assert _sync(collector, config, queue_path) is True
    assert fake_manager.calls == []


def test_archive_and_queue_carry_the_same_msg_id(collector, config, fake_manager, helpers):
    entries = helpers.make_entries(2)
    fake_manager.handler = lambda p: helpers.FakeResponse(503, text='busy')
    assert collector._record_and_sync(config, 'testbox', 'linux-testbox', entries,
                                      logging.getLogger('test')) is False
    archive = collector.get_archive_path(config, 'testbox')
    with open(archive) as f:
        archived = [json.loads(line)['header']['msg_id'] for line in f]
    queued = _queued_ids(helpers, collector.get_queue_path(config, 'testbox'))
    assert archived == queued == [e['header']['msg_id'] for e in entries]
    assert len(fake_manager.calls[0]['payload']['messages']) == 2


def test_data_dir_failure_is_logged_not_raised(collector, config, tmp_path, fake_manager,
                                               helpers, caplog):
    # Under Task Scheduler stderr is discarded: a traceback escaping main()
    # would leave a failing task with nothing in collector.log.
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('')
    config['data_dir'] = str(blocker / 'data')
    with caplog.at_level(logging.ERROR):
        assert collector._record_and_sync(config, 'testbox', 'linux-testbox',
                                          helpers.make_entries(1), logging.getLogger('test')) is False
    assert 'Cannot create the data directory' in caplog.text
    assert fake_manager.calls == []
