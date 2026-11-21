"""Per-process and per-user CPU-seconds: /proc parsing, matching, children recovery, coverage, guards."""
import logging
import os

import pytest

LOG = logging.getLogger('test')
HZ = 100.0


@pytest.fixture
def pa(collector):
    from lib import proc_accounting
    return proc_accounting


def _table(pa, procs, owners, source='procfs', hz=HZ):
    """procs {pid: (start, cpu, child, ppid)} (rss 1000, not zombie); owners {pid: name}."""
    rows = {pid: (start, cpu, child, ppid, None if source == 'psutil' else 1000 + pid, 1000, False)
            for pid, (start, cpu, child, ppid) in procs.items()}
    return pa.ProcessTable(source, hz, rows, dict(owners))


def _prev(procs, mono=1000.0, wall=1_795_000_000.0, hz=HZ):
    return {'hz': hz, 'mono': mono, 'wall': wall, 'procs': dict(procs)}


BIG = 10 ** 6    # attributable CPU-seconds large enough that the recovery cap never bites


def _cpu(users):
    return {name: u['cpu_seconds'] for name, u in users.items()}


# ---------------------------------------------------------------------------
# Reading /proc
# ---------------------------------------------------------------------------

def test_comm_with_spaces_and_parentheses(pa):
    raw = (b"4321 (my (odd) prog) R 77 4321 4321 0 -1 4194304 0 0 0 0 "
           b"150 50 7 3 20 0 1 0 12345 1000000 256 18446744073709551615 1 1 0\n")
    assert pa.parse_stat(raw) == ('R', 77, 200, 10, 12345, 256)
    assert pa.parse_stat(b"garbage") is None
    assert pa.parse_stat(b"1 (x) S 0 1") is None


def test_read_processes_from_procfs(pa, procfs, fakes):
    procfs.passwd = {0: 'root', 1000: 'alice'}
    procfs.proc_entry(1, 0, cpu=10, ppid=0, rss=100)
    procfs.proc_entry(2001, 1000, cpu=500, child=40, start=900, rss=2000, comm='train (gpu) job')
    procfs.proc_entry(2002, 1000, state='Z')
    procfs.proc_entry(2003, 4242, rss=10)                     # a container's user: no account here
    procfs.write()
    table = pa.read_processes({})
    assert table.source == 'procfs' and table.hz == fakes.CLK_TCK and table.per_user
    assert table.procs[2001] == (900, 500, 40, 1, 1000, 2000 * fakes.PAGE_SIZE, False)
    assert table.procs[2002][pa.ZOMBIE] is True
    assert table.owner_of == {1: 'root', 2001: 'alice', 2002: 'alice', 2003: 'uid:4242'}
    users, coverage = pa.user_cpu(None, table, None, None, 8)
    assert users['alice'] == {'cpu_seconds': None, 'cpu_cores': None, 'cpu_percent': None,
                              'ram_bytes': 2000 * fakes.PAGE_SIZE, 'process_count': 1}   # zombie not counted
    assert coverage is None


def test_uid_names_are_cached_per_run(pa, monkeypatch):
    calls = []
    monkeypatch.setattr(pa, '_passwd_name', lambda uid: calls.append(uid) or {1000: 'alice'}.get(uid))
    cache = {}
    assert [pa.user_name(u, cache) for u in (1000, 1000, 77, 77)] == ['alice', 'alice', 'uid:77', 'uid:77']
    assert calls == [1000, 77]


def test_unknown_uid_is_uid_n_with_the_real_pwd(pa):
    assert pa.user_name(2 ** 31 - 7, {}) == f'uid:{2 ** 31 - 7}'     # no such account here
    assert pa.user_name(os.getuid(), {}) not in ('', None)


def test_exited_process_is_skipped(pa, procfs, monkeypatch):
    procfs.passwd = {1000: 'alice'}
    procfs.proc_entry(10, 1000, cpu=5)
    procfs.proc_entry(11, 1000, cpu=5)
    procfs.write()
    real_read = pa._read_file

    def vanishing(path):
        if '/11/' in path:
            raise FileNotFoundError(path)
        return real_read(path)
    monkeypatch.setattr(pa, '_read_file', vanishing)
    table = pa.read_processes({})
    assert set(table.procs) == {10} and table.denied == 0 and table.per_user


def test_hidepid_skips_per_user_data(pa, procfs, caplog):
    procfs.passwd = {1000: 'alice'}
    procfs.proc_entry(10, 1000, cpu=5)
    procfs.mounts = 'sysfs /sys sysfs rw 0 0\nproc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=invisible 0 0\n'
    procfs.write()
    with caplog.at_level(logging.WARNING):
        table = pa.read_processes({})
    assert table.per_user is False and 'hidepid' in caplog.text
    assert pa.user_cpu(None, table, None, None, 8) == (None, None)


@pytest.mark.parametrize('options,hides', [
    ('rw,hidepid=0', False), ('rw,hidepid=off', False), ('rw,relatime', False),
    ('rw,hidepid=2', True), ('rw,hidepid=noaccess', True),
    ('rw,hidepid=2,gid={gid}', False),                    # this account is in the mount's gid= group
])
def test_hidepid_options(pa, options, hides):
    text = f"proc /proc proc {options.format(gid=os.getegid())} 0 0\n"
    assert pa.hidepid_hides(text) is (hides and os.geteuid() != 0)


def test_too_many_unreadable_pids_skip_per_user_data(pa, procfs, caplog):
    procfs.passwd = {1000: 'alice'}
    for pid in range(10, 15):
        procfs.proc_entry(pid, 1000, cpu=5)
    procfs.unreadable = {10, 11}                          # 2 of 5 = 40 % EACCES
    procfs.write()
    if os.geteuid() == 0:
        pytest.skip('root reads mode-0 files')
    with caplog.at_level(logging.WARNING):
        table = pa.read_processes({})
    assert table.per_user is False and table.denied == 2
    assert 'could not be read' in caplog.text
    procfs.unreadable = {10}
    for pid in range(15, 20):
        procfs.proc_entry(pid, 1000)
    procfs.write()
    assert pa.read_processes({}).per_user is True         # 1 of 10: still fine


# ---------------------------------------------------------------------------
# Matching processes to the previous run
# ---------------------------------------------------------------------------

def test_matched_born_and_missed_processes(pa):
    prev = _prev({100: (500, 1000, 0, 1)})
    cur = _table(pa, {100: (500, 1300, 0, 1),               # seen last time: +300
                      200: (100050, 250, 0, 1),              # started at 1000.5 s, after the previous run
                      300: (90000, 999, 0, 1)},              # started at 900 s but not seen: missed
                 {100: 'alice', 200: 'bob', 300: 'carol'})
    users, coverage = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'alice': 3.0, 'bob': 2.5, 'carol': 0.0}
    assert users['alice']['cpu_cores'] == 0.01 and users['alice']['cpu_percent'] == 0.12


def test_pid_reuse_is_a_new_process(pa):
    prev = _prev({500: (100, 10_000, 0, 1)})                 # alice's old process 500
    cur = _table(pa, {500: (100_010, 30, 0, 1)}, {500: 'bob'})   # pid 500 again, started since
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'bob': 0.3}


def test_dead_process_lowers_coverage(pa):
    prev = _prev({100: (500, 0, 0, 1), 400: (600, 0, 0, 999)})
    cur = _table(pa, {100: (500, 600, 0, 1)}, {100: 'alice'})   # 400 exited, nobody shows its CPU
    users, coverage = pa.user_cpu(prev, cur, 10.0, 10.0, 8)       # 10 CPU-seconds were used
    assert _cpu(users) == {'alice': 6.0} and coverage == 60.0


# ---------------------------------------------------------------------------
# Children recovery (Linux)
# ---------------------------------------------------------------------------

def test_one_child(pa):
    prev = _prev({100: (10, 1000, 0, 1), 101: (20, 500, 0, 100)})
    # the child used 300 more ticks, exited and was reaped: its 800 lifetime ticks land in the parent
    cur = _table(pa, {100: (10, 1100, 800, 1)}, {100: 'alice'})
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'alice': 4.0}                     # 100 own + 300 of the child


def test_grandchild_chain(pa):
    prev = _prev({100: (10, 1000, 0, 1), 101: (20, 500, 0, 100), 102: (30, 200, 0, 101)})
    # G (102) +100 and reaped by C (child 300); C (101) +50 and reaped by P: P.child = 550 + 300
    cur = _table(pa, {100: (10, 1000, 850, 1)}, {100: 'alice'})
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'alice': 1.5}


def test_orphan_is_attributed_to_pid_1(pa):
    prev = _prev({1: (1, 50, 0, 0), 300: (40, 100, 0, 299)})     # 300's parent was already gone
    cur = _table(pa, {1: (1, 50, 160, 0)}, {1: 'root'})           # init reaped it: lifetime 160
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'root': 0.6}


def test_child_share_is_clamped_at_zero(pa):
    # Reaped by a subreaper outside its old ppid chain: its parent's child time didn't grow
    prev = _prev({100: (10, 1000, 0, 1), 101: (20, 500, 0, 100)})
    cur = _table(pa, {100: (10, 1100, 0, 1)}, {100: 'alice'})
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8)
    assert _cpu(users) == {'alice': 1.0}


def test_recovered_time_is_scaled_to_what_the_cpus_spent(pa):
    prev = _prev({100: (10, 0, 0, 1), 200: (10, 0, 0, 1)})
    cur = _table(pa, {100: (10, 300, 0, 1), 200: (10, 0, 400, 1)}, {100: 'alice', 200: 'bob'})
    # 5 CPU-seconds (500 ticks) attributable, 300 own: at most 200 of bob's 400 child ticks fit
    users, coverage = pa.user_cpu(prev, cur, 300.0, 5.0, 8)
    assert _cpu(users) == {'alice': 3.0, 'bob': 2.0} and coverage == 100.0


def test_recovery_can_be_switched_off(pa):
    prev = _prev({100: (10, 1000, 0, 1), 101: (20, 500, 0, 100)})
    cur = _table(pa, {100: (10, 1100, 800, 1)}, {100: 'alice'})
    users, _ = pa.user_cpu(prev, cur, 300.0, BIG, 8, count_children=False)
    assert _cpu(users) == {'alice': 1.0}


# ---------------------------------------------------------------------------
# Exclusions, Windows
# ---------------------------------------------------------------------------

def test_excluded_users_still_count_toward_coverage(pa):
    prev = _prev({100: (10, 0, 0, 1), 200: (10, 0, 0, 1)})
    cur = _table(pa, {100: (10, 400, 0, 1), 200: (10, 600, 0, 1)}, {100: 'alice', 200: 'bob'})
    keep = lambda pid, name, uid: name != 'bob'
    users, coverage = pa.user_cpu(prev, cur, 10.0, 10.0, 8, keep=keep)
    assert _cpu(users) == {'alice': 4.0} and coverage == 100.0


def test_collector_filter_applies_to_the_procfs_rows(collector, config, procfs, monkeypatch):
    # The same rules as the psutil path: not the collector's own process, not
    # its system account, not daemon accounts with a name, exclude_users
    monkeypatch.setattr(os, 'getuid', lambda: 998)            # labmon
    monkeypatch.setattr(collector, '_uid_min', lambda: 1000)
    procfs.passwd = {0: 'root', 998: 'labmon', 101: 'systemd-resolve', 1000: 'alice', 1001: 'bob'}
    procfs.proc_entry(os.getpid(), 998, rss=1)
    procfs.proc_entry(5000, 998, rss=1)
    procfs.proc_entry(610, 101, rss=1)
    procfs.proc_entry(650, 999, rss=1)                         # no account: shown as uid:999
    procfs.proc_entry(1, 0, rss=1)
    procfs.proc_entry(2001, 1000, rss=1)
    procfs.proc_entry(2002, 1001, rss=1)
    procfs.write()
    config.update(platform='linux', exclude_users=['b*'])
    from lib import proc_accounting
    table = proc_accounting.read_processes(config)
    users, _ = proc_accounting.user_cpu(None, table, None, None, 8,
                                        keep=collector._user_filter(config, table.uid_of()))
    assert set(users) == {'root', 'alice', 'uid:999'}


def test_windows_create_time_jitter_and_access_denied(pa, windows_counters, monkeypatch):
    w = windows_counters.install(monkeypatch)
    t0 = 1_795_000_000.0
    w.procs = {0: {'username': None, 'create_time': 0.0, 'cpu': 10 ** 6},        # System Idle Process
               4: {'username': 'NT AUTHORITY\\SYSTEM', 'create_time': t0 - 86400, 'cpu': 50.0},
               2001: {'username': 'LAB\\alice', 'create_time': t0 - 3600.4, 'cpu': 130.0, 'rss': 10},
               2002: {'username': 'LAB\\bob', 'create_time': t0 - 3600, 'cpu': None}}   # AccessDenied
    table = pa.read_processes({})
    assert table.source == 'psutil' and table.hz == 1.0
    assert set(table.procs) == {4, 2001} and table.denied == 1
    prev = _prev({4: (t0 - 86400, 40.0, 0, 0), 2001: (t0 - 3600.9, 100.0, 0, 4)}, wall=t0 - 300, hz=1.0)
    users, coverage = pa.user_cpu(prev, table, 300.0, 50.0, 8)
    assert _cpu(users) == {'NT AUTHORITY\\SYSTEM': 10.0, 'LAB\\alice': 30.0}   # 0.5 s jitter: same process
    assert coverage == 80.0
    # More than 1 s apart: another process that started before the previous run (missed): 0
    prev['procs'][2001] = (t0 - 3605.0, 100.0, 0, 4)
    users, _ = pa.user_cpu(prev, table, 300.0, 50.0, 8)
    assert _cpu(users)['LAB\\alice'] == 0.0


def test_windows_without_psutil_has_no_process_table(pa, monkeypatch, no_psutil):
    monkeypatch.setattr('sys.platform', 'win32')
    assert pa.read_processes({}) is None
    assert pa.user_cpu(None, None, None, None, 8) == (None, None)
