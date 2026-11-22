"""
Upgrading databases written by the ORIGINAL Manager (commit f086e7e):
legacy duplicates, grandfathered device names, colliding spellings in the
central DB, and the pre-migration backups.  The old databases are written by
the old code itself (old_manager.old_ingest).
"""
import glob
import logging
import os
import sqlite3
import uuid

import pytest

import data_store
import manager as mgr
import metrics
from manager_helpers import ADMIN, COLL_TRITON, READ, auth, make_msg, metrics_payload, post_queue
from old_manager import old_ingest, old_msg


def _old_batch(messages, name='Triton'):
    return {'name': name, 'id': f'id-{name.lower()}', 'messages': messages}


def _count(path, table):
    con = sqlite3.connect(path)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def _columns(path, table):
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    finally:
        con.close()


def _tables(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _backups(path):
    return sorted(glob.glob(glob.escape(path) + '.pre-upgrade-*.bak'))


def _statuses(resp):
    return [r['status'] for r in resp.get_json()['results']]


# Messages the old Manager stored.  Old collectors wrote utcnow().isoformat()+'Z':
# no fraction when the microsecond is 0, and a v1 collector's 'entries' used
# whatever timestamp it had.
OLD_METRICS = [
    old_msg('system_metrics', metrics_payload(1000, 500), '2026-08-04T07:00:00.123456Z'),
    old_msg('system_metrics', metrics_payload(1500, 700), '2026-08-04T07:05:00Z'),
]
OLD_USERS = [
    old_msg('user_metrics', {'username': 'alice', 'cpu_percent': 5.0, 'ram_bytes': 100,
                             'ram_formatted': '100 B'}, '2026-08-04T07:05:00Z'),
    old_msg('user_metrics', {'username': 'bob', 'cpu_percent': 1.0, 'ram_bytes': 50,
                             'ram_formatted': '50 B'}, '2026-08-04T07:05:00Z'),
]
OLD_FOLDERS = [
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 100, '/volume1': 300, 'total_usage': 300},
            '2026-08-03T02:00:00.5Z'),
]
OLD_OTHER = [old_msg(None, {'note': 'x'}, '2026-08-04T07:06:00.000001Z')]
for m in OLD_OTHER:
    del m['data']['data_type']
OLD_ENTRIES = {'name': 'Triton', 'id': 'id-triton',
               'entries': [{'type': 'metrics', 'timestamp': '2026-08-04T06:55:00Z',
                            'cpu_percent': 3.0, 'network_bytes_in': 900, 'network_bytes_out': 400}]}
ALL_OLD = OLD_METRICS + OLD_USERS + OLD_FOLDERS + OLD_OTHER


def _build_old(data_dir):
    old_ingest(data_dir, [_old_batch(ALL_OLD), OLD_ENTRIES])


def _with_legacy_ids(messages):
    """What an upgraded collector re-sends for entries an old collector queued."""
    out = []
    for m in messages:
        m = {'header': dict(m['header']), 'data': dict(m['data'])}
        m['header']['msg_id'] = mgr._legacy_id('Triton', m['header'], m['data'])
        out.append(m)
    return out


def _entries_as_messages():
    return mgr._legacy_entries_to_messages('Triton', 'id-triton', OLD_ENTRIES['entries'])


def _with_new_ids(messages):
    """The same messages as a NEW collector sent them: each with its own random msg_id."""
    out = []
    for m in messages:
        m = {'header': dict(m['header']), 'data': dict(m['data'])}
        m['header']['msg_id'] = uuid.uuid4().hex
        out.append(m)
    return out


# --------------------------------------------------------------------------- #
# 1. Legacy duplicates                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('how', ['old_collector', 'upgraded_collector'])
def test_resent_old_messages_are_duplicates(make_client, data_dir, how):
    _build_old(data_dir)
    db = os.path.join(data_dir, 'triton', 'data.db')
    central = os.path.join(data_dir, 'metrics.db')
    before = {t: _count(db, t) for t in ('system_metrics', 'user_metrics', 'not_specified')}
    assert before == {'system_metrics': 3, 'user_metrics': 2, 'not_specified': 1}
    client = make_client()
    totals = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']

    resend = ALL_OLD + _entries_as_messages()
    if how == 'upgraded_collector':
        resend = _with_legacy_ids(resend)
    r = post_queue(client, resend)
    assert _statuses(r) == ['duplicate'] * len(resend), r.get_json()

    assert {t: _count(db, t) for t in before} == before
    assert _count(db, 'folder_usage_msgs') == 1
    assert _count(central, 'user_metrics_history') == 2
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton'] == totals


def test_new_collector_messages_stored_by_old_manager_are_duplicates(make_client, data_dir):
    # A collector upgraded BEFORE the Manager: the old Manager stored its
    # messages but dropped header.msg_id, so the rows have none.  After the
    # Manager upgrade the same messages arrive again with their random ids
    # (the old Manager's ACK was lost, or the collector archive is replayed).
    sent = _with_new_ids(ALL_OLD)
    old_ingest(data_dir, [_old_batch(sent)])
    db = os.path.join(data_dir, 'triton', 'data.db')
    central = os.path.join(data_dir, 'metrics.db')
    tables = ('system_metrics', 'user_metrics', 'not_specified')
    before = {t: _count(db, t) for t in tables}
    assert before == {'system_metrics': 2, 'user_metrics': 2, 'not_specified': 1}
    assert 'msg_id' not in _columns(db, 'system_metrics')          # the old Manager dropped the ids
    client = make_client()
    totals = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']

    for _ in range(2):
        r = post_queue(client, sent)
        assert _statuses(r) == ['duplicate'] * len(sent), r.get_json()
        assert r.get_json()['stored'] == len(sent)                  # old collectors drain too
    assert {t: _count(db, t) for t in tables} == before
    assert _count(db, 'folder_usage_msgs') == 1
    assert _count(central, 'user_metrics_history') == 2
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton'] == totals
    h = client.get('/api/history/Triton/system_metrics/cpu_percent', headers=auth(READ)).get_json()
    assert len(h['data']) == 2
    assert len(client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()) == 1

    # new readings from that collector are stored as usual
    nxt = make_msg('system_metrics', metrics_payload(1600, 800), ts='2026-08-04T07:10:00.000001Z')
    assert _statuses(post_queue(client, [nxt])) == ['stored']
    assert _statuses(post_queue(client, [nxt])) == ['duplicate']
    assert _count(db, 'system_metrics') == 3


@pytest.mark.parametrize('ids', ['none', 'new_collector'])
def test_gap_fill_does_not_duplicate_central_user_history(make_client, data_dir, ids):
    # The old Manager applied central updates even when its per-system store
    # failed: the per-user history row exists centrally, the history row in
    # data.db doesn't.  A replay fills the data.db gap without a second
    # central row, whatever id the replayed message carries.
    users = OLD_USERS if ids == 'none' else _with_new_ids(OLD_USERS)
    old_ingest(data_dir, [_old_batch(OLD_METRICS + users)])
    db = os.path.join(data_dir, 'triton', 'data.db')
    central = os.path.join(data_dir, 'metrics.db')
    con = sqlite3.connect(db)
    con.execute("DELETE FROM user_metrics WHERE username = 'alice'")
    con.commit()
    con.close()
    client = make_client()
    assert _statuses(post_queue(client, users)) == ['stored', 'duplicate']
    assert _count(db, 'user_metrics') == 2
    assert _count(central, 'user_metrics_history') == 2
    assert len(client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()) == 1


def test_legacy_v1_entries_resent_are_duplicates(make_client, data_dir):
    _build_old(data_dir)
    client = make_client()
    body = dict(OLD_ENTRIES, queue_id='Triton-v1')
    r = client.post('/api/data/queue', json=body, headers=auth(COLL_TRITON))
    assert _statuses(r) == ['duplicate']


def test_natural_key_is_narrow(make_client, data_dir):
    _build_old(data_dir)
    db = os.path.join(data_dir, 'triton', 'data.db')
    client = make_client()
    carol = old_msg('user_metrics', {'username': 'carol', 'cpu_percent': 2.0, 'ram_bytes': 7},
                    '2026-08-04T07:05:00Z')
    later = old_msg('system_metrics', metrics_payload(1600, 800), '2026-08-04T07:10:00Z')
    # a new collector's reading one microsecond after an old row is a different message ...
    fresh = make_msg('system_metrics', metrics_payload(1700, 900), ts='2026-08-04T07:05:00.000001Z')
    # ... and so is a message of another data_type at an old row's timestamp
    scan = make_msg('folder_usage', {'/volume1': 400, 'total_usage': 400}, ts='2026-08-04T07:05:00Z')
    assert _statuses(post_queue(client, [carol, later, fresh, scan])) == ['stored'] * 4
    assert _count(db, 'user_metrics') == 3 and _count(db, 'system_metrics') == 5
    assert _count(db, 'folder_usage_msgs') == 2
    # ... and the newly stored legacy messages are now found by their msg_id
    assert _statuses(post_queue(client, [carol, later])) == ['duplicate', 'duplicate']


def test_natural_key_lookup_uses_indexes(make_client, data_dir, monkeypatch):
    _build_old(data_dir)
    client = make_client()
    plans = []
    real = data_store.TypedDataStore._has_pre_upgrade_row.__func__

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            if 'msg_id IS NULL' in sql or 'FROM "folder_usage" WHERE' in sql:
                plans.append(' '.join(r[-1] for r in self.conn.execute('EXPLAIN QUERY PLAN ' + sql, params)))
            return self.conn.execute(sql, params)

    monkeypatch.setattr(data_store.TypedDataStore, '_has_pre_upgrade_row',
                        classmethod(lambda cls, conn, *a: real(cls, Spy(conn), *a)))
    post_queue(client, [OLD_METRICS[0], OLD_USERS[0], OLD_FOLDERS[0], OLD_OTHER[0]])
    assert len(plans) == 4
    assert all('USING INDEX' in p or 'USING COVERING INDEX' in p for p in plans), plans
    assert not any(p.startswith('SCAN') for p in plans), plans


# --------------------------------------------------------------------------- #
# 2. Device names                                                             #
# --------------------------------------------------------------------------- #

def test_grandfathered_device_name(make_client, data_dir, caplog):
    old_ingest(data_dir, [_old_batch([old_msg('system_metrics', metrics_payload(10, 5),
                                              '2026-08-04T07:00:00.1Z', name='Lab PC')], name='Lab PC')])
    client = make_client()
    caplog.set_level(logging.WARNING)

    def send(name, ts):
        return post_queue(client, [make_msg('system_metrics', metrics_payload(20, 9), ts=ts, name=name)],
                          token=ADMIN, name=name)

    for ts in ('2026-08-04T07:05:00Z', '2026-08-04T07:10:00Z'):
        r = send('Lab PC', ts)
        assert r.status_code == 200 and _statuses(r) == ['stored']
    assert sum('grandfathered' in rec.getMessage() and 'Accepting' in rec.getMessage()
               for rec in caplog.records) == 1                      # logged once
    assert send('New PC', '2026-08-04T07:15:00Z').status_code == 400   # new invalid names refused
    assert send('lab pc', '2026-08-04T07:15:00Z').status_code == 400   # only the exact old spelling

    assert [d['name'] for d in client.get('/api/devices', headers=auth(READ)).get_json()['devices']] == ['Lab PC']
    h = client.get('/api/history/Lab%20PC/system_metrics/cpu_percent', headers=auth(READ))
    assert h.status_code == 200 and len(h.get_json()['data']) == 3
    assert client.get('/api/metrics/nas/Lab%20PC', headers=auth(READ)).status_code == 200
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Lab PC']['total_bytes_in'] == 20


def test_grandfathered_name_with_collector_token(make_client, data_dir):
    # collector_tokens keys must follow the pattern; the normalized-equal
    # spelling ('Lab_PC') authorizes the grandfathered device 'Lab PC'
    old_ingest(data_dir, [_old_batch([old_msg('system_metrics', metrics_payload(10, 5),
                                              '2026-08-04T07:00:00.1Z', name='Lab PC')], name='Lab PC')])
    token = 'lab-pc-token-' + '7' * 20
    client = make_client(collector_tokens={'Lab_PC': token})
    r = post_queue(client, [make_msg('system_metrics', metrics_payload(20, 9), ts='2026-08-04T07:05:00Z',
                                     name='Lab PC')], token=token, name='Lab PC')
    assert _statuses(r) == ['stored']
    with pytest.raises(mgr.ConfigError, match='Lab_PC'):
        make_client(collector_tokens={'Lab PC': token})


def test_colliding_old_spellings_keep_their_own_totals(make_client, data_dir, caplog):
    # An old Manager keyed central rows by the raw name: a collector renamed
    # from 'Triton' to 'triton' left two rows.  'triton' is the active one.
    old_ingest(data_dir, [
        _old_batch([old_msg('system_metrics', metrics_payload(1000, 100), '2026-07-01T00:00:00.1Z')]),
        _old_batch([old_msg('system_metrics', metrics_payload(5000, 500), '2026-08-01T00:00:00.1Z',
                            name='triton'),
                    old_msg('system_metrics', metrics_payload(6000, 600), '2026-08-01T00:05:00.1Z',
                            name='triton')], name='triton'),
    ])
    caplog.set_level(logging.WARNING)
    client = make_client()
    assert any('normalize to the same name' in rec.getMessage() for rec in caplog.records)

    t = client.get('/api/totals', headers=auth(READ)).get_json()
    assert t['devices']['Triton']['total_bytes_in'] == 1000
    assert t['devices']['triton']['total_bytes_in'] == 6000
    assert t['global']['total_bytes_in'] == 7000

    # new data from the renamed collector continues the active row from its current counters
    post_queue(client, [make_msg('system_metrics', metrics_payload(6500, 650), ts='2026-08-01T00:10:00Z',
                                 name='triton')], name='triton')
    t = client.get('/api/totals', headers=auth(READ)).get_json()
    assert t['devices']['triton']['total_bytes_in'] == 6500
    assert t['devices']['Triton']['total_bytes_in'] == 1000      # stale row untouched
    assert t['global']['total_bytes_in'] == 7500

    # each old row stays readable under its own spelling
    assert client.get('/api/metrics/nas/Triton', headers=auth(READ)).get_json()['network_bytes_in'] == 1000
    assert client.get('/api/metrics/nas/triton', headers=auth(READ)).get_json()['network_bytes_in'] == 6500
    names = [d['name'] for d in client.get('/api/devices', headers=auth(READ)).get_json()['devices']]
    assert names == ['Triton', 'triton']


def test_first_reading_after_upgrade_detects_a_reboot(make_client, data_dir):
    # The old Manager moved device_totals.last_updated with folder_usage messages
    # too; the reboot check must measure uptime against the last COUNTER reading
    up = dict(metrics_payload(10_000, 5_000), uptime_seconds=900_000)
    old_ingest(data_dir, [_old_batch([
        old_msg('system_metrics', up, '2026-08-04T07:00:00.1Z'),
        old_msg('folder_usage', {'/volume1': 5, 'total_usage': 5}, '2026-08-04T09:00:00.1Z'),
    ])])
    client = make_client()
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert t['network_last_updated'] == '2026-08-04T07:00:00.1Z'
    assert t['last_updated'] == '2026-08-04T09:00:00.1Z'
    rebooted = dict(metrics_payload(300, 200), uptime_seconds=3_600)     # booted at 08:05
    post_queue(client, [make_msg('system_metrics', rebooted, ts='2026-08-04T09:05:00Z')])
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']
    assert (t['total_bytes_in'], t['total_bytes_out']) == (10_300, 5_200)


# --------------------------------------------------------------------------- #
# 3. Pre-migration backups                                                    #
# --------------------------------------------------------------------------- #

def test_backups_taken_once_before_migration(make_client, data_dir):
    _build_old(data_dir)
    db = os.path.join(data_dir, 'triton', 'data.db')
    central = os.path.join(data_dir, 'metrics.db')
    make_client()

    (db_bak,), (central_bak,) = _backups(db), _backups(central)
    assert db_bak.endswith('.bak') and '.pre-upgrade-' in db_bak
    # the copies are the old databases, unmigrated and complete
    assert 'folder_usage' in _tables(db_bak) and 'folder_usage_msgs' not in _tables(db_bak)
    assert 'msg_id' not in _columns(db_bak, 'system_metrics')
    assert _count(db_bak, 'system_metrics') == 3 and _count(db_bak, 'user_metrics') == 2
    assert 'dead_letter' not in _tables(central_bak)
    assert 'msg_id' not in _columns(central_bak, 'user_metrics_history')
    assert _count(central_bak, 'devices') == 1
    # ... and the live ones were migrated
    assert 'msg_id' in _columns(db, 'system_metrics') and 'msg_id' in _columns(db, 'user_metrics')
    assert 'folder_usage_msgs' in _tables(db)
    assert not glob.glob(os.path.join(data_dir, '**', '*.tmp'), recursive=True)

    client = make_client()                         # already migrated: no second backup
    post_queue(client, [make_msg('system_metrics', metrics_payload(), ts='2026-08-05T00:00:00Z')])
    make_client()
    assert len(_backups(db)) == 1 and len(_backups(central)) == 1


def test_no_backup_for_new_databases(client, data_dir):
    post_queue(client, [make_msg('system_metrics', metrics_payload())])
    assert not glob.glob(os.path.join(data_dir, '**', '*.bak'), recursive=True)


def test_backup_can_be_turned_off(make_client, data_dir, caplog):
    _build_old(data_dir)
    caplog.set_level(logging.WARNING)
    make_client(backup_before_migration=False)
    assert not glob.glob(os.path.join(data_dir, '**', '*.bak'), recursive=True)
    assert 'msg_id' in _columns(os.path.join(data_dir, 'triton', 'data.db'), 'system_metrics')
    assert any('WITHOUT a backup' in rec.getMessage() for rec in caplog.records)


def test_backup_option_must_be_boolean(make_client):
    with pytest.raises(mgr.ConfigError):
        make_client(backup_before_migration='yes')


def _failing_backup(*a, **kw):
    raise OSError(28, 'No space left on device')


def test_failed_per_system_backup_blocks_migration(make_client, data_dir, monkeypatch):
    _build_old(data_dir)
    db = os.path.join(data_dir, 'triton', 'data.db')
    monkeypatch.setattr(data_store, 'backup_database', _failing_backup)
    client = make_client()

    # nothing was changed ...
    assert 'folder_usage' in _tables(db) and 'folder_usage_msgs' not in _tables(db)
    assert 'msg_id' not in _columns(db, 'system_metrics')
    # ... the old data is still readable, including the unmigrated folder history ...
    rows = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows']
    assert len(rows) == 1 and rows[0]['/volume1/A'] == 100 and rows[0]['_unit'] == 'bytes'
    h = client.get('/api/history/Triton/folder_usage/volume1', headers=auth(READ)).get_json()
    assert [p['value'] for p in h['data']] == [300.0]
    assert len(client.get('/api/data/Triton/system_metrics', headers=auth(READ)).get_json()['rows']) == 3
    # ... and new messages are kept by the collector ('error'), with no side effects
    new = make_msg('system_metrics', metrics_payload(5000, 2500), ts='2026-08-05T00:00:00Z')
    r = post_queue(client, [new])
    assert _statuses(r) == ['error']
    assert _count(db, 'system_metrics') == 3
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']['total_bytes_in'] == 1500

    # once the backup works, the next write (after the retry interval) migrates
    monkeypatch.undo()
    mgr._data_store._get_db('Triton').upgrade_attempted -= data_store.UPGRADE_RETRY_SECONDS
    assert _statuses(post_queue(client, [new])) == ['stored']
    assert len(_backups(db)) == 1 and 'msg_id' in _columns(db, 'system_metrics')
    assert 'folder_usage_msgs' in _tables(db)


def test_failed_central_backup_blocks_migration(make_client, data_dir, monkeypatch):
    _build_old(data_dir)
    central = os.path.join(data_dir, 'metrics.db')
    db = os.path.join(data_dir, 'triton', 'data.db')
    monkeypatch.setattr(metrics, 'backup_database', _failing_backup)
    client = make_client()

    assert 'dead_letter' not in _tables(central)
    assert 'network_last_updated' not in _columns(central, 'device_totals')
    # reads keep working on the old schema
    assert [d['name'] for d in client.get('/api/devices', headers=auth(READ)).get_json()['devices']] == ['Triton']
    assert client.get('/api/totals', headers=auth(READ)).get_json()['devices']['Triton']['total_bytes_in'] == 1500
    # (the old Manager let the last message stored, the v1 entry, set the snapshot)
    assert client.get('/api/metrics/all', headers=auth(READ)).get_json()['Triton']['cpu_percent'] == 3.0
    assert len(client.get('/api/user_metrics/history/Triton/alice', headers=auth(READ)).get_json()) == 1
    # writes: 'error' for stored and rejected messages alike; the per-system insert is rolled back
    rows_before = _count(db, 'system_metrics')
    good = make_msg('system_metrics', metrics_payload(5000, 2500), ts='2026-08-05T00:00:00Z')
    bad = make_msg('system_metrics', {'cpu_percent': 'lots'}, ts='2026-08-05T00:01:00Z')
    assert _statuses(post_queue(client, [good, bad])) == ['error', 'error']
    assert _count(db, 'system_metrics') == rows_before

    monkeypatch.undo()
    mgr._metrics_db._upgrade_attempted -= data_store.UPGRADE_RETRY_SECONDS
    assert _statuses(post_queue(client, [good])) == ['stored']
    assert len(_backups(central)) == 1 and 'dead_letter' in _tables(central)


def test_current_backup_is_reused_not_repeated(scratch):
    from storage_util import backup_database
    path = os.path.join(scratch, 'x.db')
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE t (a)')
    con.commit()
    first = backup_database(con, path)
    assert backup_database(con, path) == first            # nothing written since
    os.utime(path, (os.path.getmtime(first) + 10,) * 2)    # written after the backup
    second = backup_database(con, path)
    con.close()
    assert second != first and len(_backups(path)) == 2


def test_interrupted_backup_leftovers_are_removed(scratch):
    from storage_util import backup_database
    path = os.path.join(scratch, 'x.db')
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE t (a)')
    con.commit()
    stale = path + '.pre-upgrade-20200101T000000Z.bak.tmp'      # killed mid-copy earlier
    open(stale, 'wb').write(b'partial')
    backup_database(con, path)
    con.close()
    assert not os.path.exists(stale) and len(_backups(path)) == 1


# --------------------------------------------------------------------------- #
# 4. A migration step that fails                                              #
# --------------------------------------------------------------------------- #

OLD_SCANS = [
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 100, '/volume1': 300, 'total_usage': 300},
            '2026-05-01T02:00:00.5Z'),
    old_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 110, '/volume1': 310, 'total_usage': 310},
            '2026-05-02T02:00:00.5Z'),
]


def _locked_once(monkeypatch, attr):
    """Make TypedDataStore.<attr> fail with 'database is locked' until undone."""
    def fail(*a, **kw):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(data_store.TypedDataStore, attr, fail)


def _folder_history(client, field='volume1/A'):
    h = client.get(f'/api/history/Triton/folder_usage/{field}', headers=auth(READ)).get_json()
    return [(p['timestamp'][:10], p['value']) for p in h['data']]


def test_failed_folder_migration_keeps_old_history_visible(make_client, data_dir, monkeypatch):
    """The wide -> narrow copy fails (another program holds the database, disk full): the old
    folder history must stay visible, and the narrow tables must not be started next to the
    unmigrated wide table (they would hide it). New scans wait on the collector ('error')."""
    old_ingest(data_dir, [_old_batch(OLD_METRICS + OLD_SCANS)])
    db = os.path.join(data_dir, 'triton', 'data.db')
    _locked_once(monkeypatch, '_migrate_wide_folder_usage')
    client = make_client()
    assert 'folder_usage' in _tables(db) and 'folder_usage_msgs' not in _tables(db)

    scan = make_msg('folder_usage', {'_unit': 'bytes', '/volume1/A': 120, '/volume1': 320, 'total_usage': 320},
                    ts='2026-06-01T02:00:00Z')
    assert _statuses(post_queue(client, [scan])) == ['error']         # kept by the collector
    metrics_msg = make_msg('system_metrics', metrics_payload(2000, 900), ts='2026-06-01T02:05:00Z')
    assert _statuses(post_queue(client, [metrics_msg])) == ['stored']  # other data is not held back
    assert 'folder_usage_msgs' not in _tables(db)
    assert _folder_history(client) == [('2026-05-01', 100.0), ('2026-05-02', 110.0)]
    rows = client.get('/api/data/Triton/folder_usage', headers=auth(READ)).get_json()['rows']
    assert [r['/volume1/A'] for r in rows] == [110, 100]

    # a restart while it still fails takes no second pre-upgrade copy (the first one holds
    # this table unmigrated; msg_id columns were added since, so the database changed)
    assert len(_backups(db)) == 1
    client = make_client()
    assert len(_backups(db)) == 1
    assert _folder_history(client) == [('2026-05-01', 100.0), ('2026-05-02', 110.0)]

    # once the database is free again, the next scan (after the retry interval) migrates first
    monkeypatch.undo()
    mgr._data_store._get_db('Triton').folder_attempted -= data_store.UPGRADE_RETRY_SECONDS
    assert _statuses(post_queue(client, [scan])) == ['stored']
    assert 'folder_usage_legacy' in _tables(db) and 'folder_usage' not in _tables(db)
    assert _folder_history(client) == [('2026-05-01', 100.0), ('2026-05-02', 110.0), ('2026-06-01', 120.0)]
    assert len(_backups(db)) == 1


def test_partly_migrated_database_gets_no_second_pre_upgrade_copy(make_client, data_dir, monkeypatch):
    """The folder copy commits but adding the msg_id columns fails: the next start must not take
    a 'pre-upgrade' copy of the partly migrated database (rollback would restore it and the old
    Manager would show no folder history)."""
    old_ingest(data_dir, [_old_batch(OLD_METRICS + OLD_SCANS)])
    db = os.path.join(data_dir, 'triton', 'data.db')
    _locked_once(monkeypatch, '_ensure_table')
    make_client()
    assert 'folder_usage_legacy' in _tables(db) and 'msg_id' not in _columns(db, 'system_metrics')
    (first,) = _backups(db)
    monkeypatch.undo()

    os.utime(db, (os.path.getmtime(first) + 10,) * 2)     # changed since the copy
    client = make_client()
    assert _backups(db) == [first]
    assert 'msg_id' in _columns(db, 'system_metrics')
    assert 'folder_usage' in _tables(first) and 'folder_usage_msgs' not in _tables(first)
    assert _folder_history(client) == [('2026-05-01', 100.0), ('2026-05-02', 110.0)]


def test_unchanged_old_database_reuses_its_copy(make_client, data_dir, monkeypatch):
    """Every migration step fails (disk full) and nothing is written: a restart whose checkpoint
    touched the file must not take another full copy of the same data."""
    old_ingest(data_dir, [_old_batch(OLD_METRICS + OLD_SCANS)])
    db = os.path.join(data_dir, 'triton', 'data.db')
    _locked_once(monkeypatch, '_migrate_wide_folder_usage')
    _locked_once(monkeypatch, '_ensure_table')
    make_client()
    (first,) = _backups(db)
    os.utime(db, (os.path.getmtime(first) + 10,) * 2)
    make_client()
    assert _backups(db) == [first]
    # new data, though, gets a new copy
    monkeypatch.undo()
    mgr._close_stores()
    old_ingest(data_dir, [_old_batch([old_msg('system_metrics', metrics_payload(3000, 1000),
                                               '2026-08-04T07:10:00Z')])])
    os.utime(db, (os.path.getmtime(first) + 20,) * 2)
    make_client()
    assert len(_backups(db)) == 2


# --------------------------------------------------------------------------- #
# 5. Two machines whose names normalize alike                                 #
# --------------------------------------------------------------------------- #

def test_two_live_devices_with_alike_names_keep_their_own_rows(make_client, data_dir):
    """'lab-a' and 'lab_a' are different machines; the old Manager kept a central row for each
    (they shared data/lab_a/data.db). Their data must not be merged into one row."""
    tb = 1024 ** 4
    old_ingest(data_dir, [
        _old_batch([old_msg('system_metrics', metrics_payload(5 * tb, tb), '2026-08-01T00:00:00Z', name='lab-a')],
                   name='lab-a'),
        _old_batch([old_msg('system_metrics', metrics_payload(1 * tb, tb), '2026-08-01T00:01:00Z', name='lab_a')],
                   name='lab_a'),
    ])
    client = make_client(collector_tokens={'*': COLL_TRITON})
    for i in range(3):          # alternating 5-minute posts, each a little more traffic
        for name, base in (('lab-a', 5 * tb), ('lab_a', 1 * tb)):
            ts = f'2026-08-01T00:{5 * (i + 1):02d}:{0 if name == "lab-a" else 30:02d}Z'
            msg = make_msg('system_metrics', metrics_payload(base + (i + 1) * 1000, tb, cpu=float(i)),
                           ts=ts, name=name)
            assert _statuses(post_queue(client, [msg], name=name)) == ['stored']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert t['lab-a']['total_bytes_in'] == 5 * tb + 3000
    assert t['lab_a']['total_bytes_in'] == 1 * tb + 3000
    assert client.get('/api/metrics/nas/lab-a', headers=auth(READ)).get_json()['network_bytes_in'] == 5 * tb + 3000
    assert client.get('/api/metrics/nas/lab_a', headers=auth(READ)).get_json()['network_bytes_in'] == 1 * tb + 3000

    # deleting one of them leaves the other's rows, and the history database they share
    r = client.delete('/api/system/lab-a', headers=auth(ADMIN)).get_json()
    assert r['central_db_rows_cleared'] is True and r['per_system_db_deleted'] is False
    assert r['per_system_db_kept_for'] == ['lab_a']
    assert os.path.isfile(os.path.join(data_dir, 'lab_a', 'data.db'))
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert 'lab-a' not in t and t['lab_a']['total_bytes_in'] == 1 * tb + 3000
    # the deleted machine sends again: it is registered anew (its device_id is not lab_a's),
    # as the old Manager did, and lab_a's row is untouched
    msg = make_msg('system_metrics', metrics_payload(5 * tb + 4000, tb), ts='2026-08-01T00:30:00Z', name='lab-a')
    assert _statuses(post_queue(client, [msg], name='lab-a')) == ['stored']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert t['lab-a']['total_bytes_in'] == 5 * tb + 4000 and t['lab_a']['total_bytes_in'] == 1 * tb + 3000
    assert client.delete('/api/system/lab-a', headers=auth(ADMIN)).get_json()['per_system_db_kept_for'] == ['lab_a']
    # deleting the last spelling removes the history database too
    r = client.delete('/api/system/lab_a', headers=auth(ADMIN)).get_json()
    assert r['per_system_db_deleted'] is True
    assert not os.path.exists(os.path.join(data_dir, 'lab_a', 'data.db'))


def test_new_machine_with_an_alike_name_gets_its_own_row(make_client, data_dir):
    """A new collector 'lab_a' next to the registered 'lab-a': another machine (its device_id
    differs), so it gets its own central row; a renamed collector (same device_id) continues the
    registered row."""
    old_ingest(data_dir, [_old_batch([old_msg('system_metrics', metrics_payload(5000, 100),
                                              '2026-08-01T00:00:00Z', name='lab-a')], name='lab-a')])
    client = make_client(collector_tokens={'*': COLL_TRITON})
    other = make_msg('system_metrics', metrics_payload(700, 100), ts='2026-08-01T00:05:00Z', name='lab_a')
    assert _statuses(post_queue(client, [other], name='lab_a')) == ['stored']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert t['lab-a']['total_bytes_in'] == 5000 and t['lab_a']['total_bytes_in'] == 700

    renamed = make_msg('system_metrics', metrics_payload(5600, 100), ts='2026-08-01T00:10:00Z', name='LAB-A')
    renamed['header']['device_id'] = 'id-lab-a'                  # same machine, new spelling
    assert _statuses(post_queue(client, [renamed], name='LAB-A')) == ['stored']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert 'LAB-A' not in t
    assert t['lab-a']['total_bytes_in'] == 5600 and t['lab_a']['total_bytes_in'] == 700

    # the same after a restart, when 'lab_a' is the most recently seen spelling
    other = make_msg('system_metrics', metrics_payload(800, 100), ts='2026-08-01T00:12:00Z', name='lab_a')
    assert _statuses(post_queue(client, [other], name='lab_a')) == ['stored']
    client = make_client(collector_tokens={'*': COLL_TRITON})
    assert mgr._metrics_db._names['lab_a'] == 'lab_a'
    renamed = make_msg('system_metrics', metrics_payload(5900, 100), ts='2026-08-01T00:15:00Z', name='LAB-A')
    renamed['header']['device_id'] = 'id-lab-a'
    assert _statuses(post_queue(client, [renamed], name='LAB-A')) == ['stored']
    t = client.get('/api/totals', headers=auth(READ)).get_json()['devices']
    assert 'LAB-A' not in t
    assert t['lab-a']['total_bytes_in'] == 5900 and t['lab_a']['total_bytes_in'] == 800


def test_tables_of_a_later_manager_are_left_alone(make_client, data_dir):
    # A later Manager adds tables under the reserved "_" and "storage_breakdown_"
    # prefixes to data.db (this one's own are _rollup_* and storage_breakdown_msgs /
    # _rows / _legacy).  Running this one again on such a database must not list
    # them, treat them as old tables (backup on every start) or fail on them.
    client = make_client()
    post_queue(client, [make_msg('system_metrics', metrics_payload(), ts='2026-08-05T00:00:00Z')])
    db = os.path.join(data_dir, 'triton', 'data.db')
    con = sqlite3.connect(db)
    try:
        con.execute('CREATE TABLE _rollup_hourly (bucket TEXT, n INTEGER)')
        con.execute('CREATE TABLE storage_breakdown_quota (id INTEGER PRIMARY KEY, ts TEXT)')
        con.execute('CREATE TABLE storage_breakdown_owners_v2 (msg INTEGER, owner TEXT)')
        con.commit()
    finally:
        con.close()

    client = make_client()
    make_client()
    assert not _backups(db)
    assert 'msg_id' not in _columns(db, '_rollup_hourly')
    resp = client.get('/api/data/Triton', headers=auth(READ))
    assert resp.status_code == 200
    assert not [t for t in resp.get_json()['data_types'] if t.startswith(('_', 'storage_breakdown_'))]
    assert _statuses(post_queue(client, [
        make_msg('system_metrics', metrics_payload(), ts='2026-08-05T00:05:00Z'),
        make_msg('storage_breakdown', {'x': 1}, ts='2026-08-05T00:05:00Z'),
        make_msg('storage_breakdown_x', {'x': 1}, ts='2026-08-05T00:05:00Z'),
    ])) == ['stored', 'stored', 'rejected']
