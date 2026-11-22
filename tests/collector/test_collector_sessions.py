"""lib/sessions.py and lib/sessions_win.py: who is connected (the 'sessions' message)."""
import ctypes
import ipaddress
import json
import logging
import os
import struct
import subprocess
import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == 'win32',
                                reason='needs a POSIX host (os.getuid / os.sysconf / /proc / POSIX paths)')


LOG = logging.getLogger('test')


@pytest.fixture
def sessions(collector):
    from lib import sessions as module
    return module


@pytest.fixture
def win(collector):
    from lib import sessions_win as module
    return module


def _hex(ip, port, byteorder=sys.byteorder):
    packed = ipaddress.ip_address(ip).packed
    words = ''.join(f"{int.from_bytes(packed[i:i + 4], byteorder):08X}" for i in range(0, len(packed), 4))
    return f"{words}:{port:04X}"


def tcp_text(rows, byteorder=sys.byteorder):
    """/proc/net/tcp(6) text: rows of (local_ip, local_port, remote_ip, remote_port, state)."""
    lines = ['  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode']
    for i, (lip, lport, rip, rport, st) in enumerate(rows):
        lines.append(f"   {i}: {_hex(lip, lport, byteorder)} {_hex(rip, rport, byteorder)} {st} "
                     f"00000000:00000000 00:00000000 00000000  1000        0 1 1 0 100 0 0 10 0")
    return '\n'.join(lines) + '\n'


LISTEN, ESTAB = '0A', '01'


# ---------------------------------------------------------------------------
# /proc/net/tcp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('byteorder', ['little', 'big'])
def test_proc_net_tcp_ipv4_ipv6_and_mapped_in_both_byte_orders(sessions, byteorder):
    text = tcp_text([('0.0.0.0', 445, '0.0.0.0', 0, LISTEN),
                     ('10.0.0.5', 445, '10.0.0.17', 50001, ESTAB)], byteorder)
    listen, conns = sessions.parse_proc_net_tcp(text, byteorder)
    assert listen == {445} and conns == [(445, '10.0.0.17', '10.0.0.5')]
    text6 = tcp_text([('::', 22, '::', 0, LISTEN),
                      ('fd00::5', 22, 'fd00::40', 40000, ESTAB),
                      ('::ffff:10.0.0.5', 22, '::ffff:10.0.0.41', 40001, ESTAB)], byteorder)
    listen, conns = sessions.parse_proc_net_tcp(text6, byteorder)
    assert listen == {22}
    assert conns == [(22, 'fd00::40', 'fd00::5'), (22, '10.0.0.41', '10.0.0.5')]     # mapped -> IPv4


def test_this_hosts_layout(sessions):
    # As the kernel prints it on this little-endian box ([V-local]): 127.0.0.1:631 LISTEN
    listen, _ = sessions.parse_proc_net_tcp(
        'header\n   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1 1\n', 'little')
    assert listen == {631}


def test_malformed_lines_are_skipped(sessions):
    text = tcp_text([('10.0.0.5', 22, '10.0.0.9', 5000, ESTAB)]) + 'garbage line\n   9: ZZZZ:0016 00:00 01\n'
    assert sessions.parse_proc_net_tcp(text)[1] == [(22, '10.0.0.9', '10.0.0.5')]


def _opts(sessions, platform='linux', **block):
    return sessions.Options({'sessions': block}, platform, LOG)


def test_listen_gating_loopback_self_and_excluded_clients(sessions):
    opts = _opts(sessions, exclude_clients=['10.9.0.0/16', 'fd00::bad'])
    listen = {22, 445}
    conns = [(445, '10.0.0.17', '10.0.0.5'), (445, '10.0.0.17', '10.0.0.5'), (22, '10.0.0.40', '10.0.0.5'),
             (22, '127.0.0.1', '127.0.0.1'), (22, '10.0.0.5', '10.0.0.5'), (22, '10.9.3.3', '10.0.0.5'),
             (22, 'fd00::bad', 'fd00::5'), (51000, '10.0.0.9', '10.0.0.5'),        # outbound: ephemeral port
             (2049, '10.0.0.77', '10.0.0.5')]                                       # nfs: not listening
    per_service, clients, rows = sessions.count_clients(opts, listen, conns)
    assert per_service == {'smb': 1, 'ssh': 1}            # no nfs_clients / rdp_clients: not running
    assert clients == {'10.0.0.17', '10.0.0.40'}
    assert rows == {('smb', '10.0.0.17'): 2, ('ssh', '10.0.0.40'): 1}
    assert sessions.count_clients(opts, {445}, [])[0] == {'smb': 0}     # running, no clients: 0


def test_ports_config_replaces_or_extends_the_map(sessions):
    assert set(_opts(sessions).services) == {'ssh', 'smb', 'nfs', 'rdp'}
    assert set(_opts(sessions, 'synology').services) >= {'afp', 'dsm', 'drive', 'rsync', 'iscsi', 'webdav'}
    assert 'dsm' not in _opts(sessions, 'windows').services
    assert _opts(sessions, ports={'ssh': 2222}).services == {'ssh': [2222]}
    opts = _opts(sessions, extra_ports={'jupyter': [8888], 'Bad Name': [1], 'big': [70000]})
    assert opts.services['jupyter'] == [8888] and 'Bad Name' not in opts.services and 'big' not in opts.services


def test_unreadable_tcp_files(sessions, tmp_path):
    assert sessions.read_proc_net_tcp(str(tmp_path / 'none'), LOG) == ('unavailable', set(), [])
    (tmp_path / 'tcp').write_text(tcp_text([('0.0.0.0', 22, '0.0.0.0', 0, LISTEN)]))
    assert sessions.read_proc_net_tcp(str(tmp_path), LOG)[:2] == ('ok', {22})     # tcp6 missing is fine


# ---------------------------------------------------------------------------
# utmp
# ---------------------------------------------------------------------------

def utmp(user, line, host='', addr=None, pid=None, ut_type=7, tv=1_790_000_000):
    raw = b'\0' * 16
    if addr:
        ip = ipaddress.ip_address(addr)
        raw = ip.packed + b'\0' * (16 - len(ip.packed))
    return struct.pack('=h2xi32s4s32s256shhiii16s20x', ut_type, os.getpid() if pid is None else pid,
                       line.encode(), b'x', user.encode(), host.encode(), 0, 0, 0, tv, 0, raw)


def test_utmp_record_layout(sessions):
    assert sessions.UTMP_RECORD == 384 == len(utmp('a', 'pts/0'))


def test_utmp_user_processes_only_and_dead_pids_dropped(sessions):
    data = (utmp('alice', 'pts/0', '10.0.0.40', '10.0.0.40') + utmp('LOGIN', 'tty1', ut_type=6)
            + utmp('bob', 'pts/1', 'laptop.lab', pid=999999) + utmp('carol', 'tty2')
            + utmp('dave', 'pts/2', 'fe80::1%eth0') + utmp('erin', 'pts/3', ':0'))
    rows = sessions.parse_utmp(data, alive=lambda pid: pid != 999999)
    assert rows == [
        {'service': 'ssh', 'username': 'alice', 'source': 'utmp', 'client': '10.0.0.40', 'since': '2026-09-21T14:13:20Z'},
        {'service': 'console', 'username': 'carol', 'source': 'utmp', 'since': '2026-09-21T14:13:20Z'},
        {'service': 'ssh', 'username': 'dave', 'source': 'utmp', 'client': 'fe80::1', 'since': '2026-09-21T14:13:20Z'},
        {'service': 'console', 'username': 'erin', 'source': 'utmp', 'since': '2026-09-21T14:13:20Z'},
    ]


def test_utmp_host_name_vs_address(sessions):
    rows = sessions.parse_utmp(utmp('alice', 'pts/0', 'laptop.lab', '10.0.0.40'), alive=lambda pid: True)
    assert rows[0]['client'] == '10.0.0.40' and rows[0]['client_host'] == 'laptop.lab'


def test_utmp_multiplexer_and_mosh_hosts_are_not_logins(sessions):
    """libutempter rows (tmux, GNU screen, mosh) never send a process name or PID as client_host."""
    data = (utmp('alice', 'pts/0', '10.0.0.40', '10.0.0.40')              # the real ssh login
            + utmp('alice', 'pts/3', 'tmux(1357012).%0')                   # tmux panes
            + utmp('alice', 'pts/4', 'tmux(1357012).%12')
            + utmp('bob', 'pts/5', 'mosh [1357013]')                       # mosh-server, no client attached
            + utmp('bob', 'pts/6', '10.0.0.41 via mosh [1357014]')         # mosh client attached
            + utmp('bob', 'pts/7', 'fe80::2%eth0 via mosh [1357015]')
            + utmp('carol', 'pts/8', ':pts/1:S.0')                         # screen started on a console
            + utmp('carol', 'pts/9', 'laptop:S.2')                         # screen started over ssh
            + utmp('dave', 'pts/10', 'not a host [42]')                    # any other free text
            + utmp('erin', 'pts/11', 'laptop.lab'))
    rows = sessions.parse_utmp(data, alive=lambda pid: True)
    since = '2026-09-21T14:13:20Z'
    assert rows == [
        {'service': 'ssh', 'username': 'alice', 'source': 'utmp', 'client': '10.0.0.40', 'since': since},
        {'service': 'ssh', 'username': 'bob', 'source': 'utmp', 'client': '10.0.0.41', 'since': since},
        {'service': 'ssh', 'username': 'bob', 'source': 'utmp', 'client': 'fe80::2', 'since': since},
        {'service': 'ssh', 'username': 'dave', 'source': 'utmp', 'since': since},
        {'service': 'ssh', 'username': 'erin', 'source': 'utmp', 'client_host': 'laptop.lab', 'since': since},
    ]
    assert all('[' not in json.dumps(r) and 'tmux' not in json.dumps(r) for r in rows)


def test_host_fields_keep_only_addresses_and_host_names(sessions):
    assert sessions._host_fields('LAB-PC_2.lab.example', None) == {'client_host': 'LAB-PC_2.lab.example'}
    assert sessions._host_fields('::ffff:10.0.0.5', None) == {'client': '10.0.0.5'}
    for text in ('tmux(1).%0', 'mosh [7]', 'a b', '-dash', 'login screen', 'x' * 300 + ' y'):
        assert sessions._host_fields(text, None) == {}, text
    assert sessions._host_fields('x' * 300, '10.0.0.6') == {'client': '10.0.0.6', 'client_host': 'x' * 256}
    assert sessions.parse_logind_session('USER=amy\nSERVICE=sshd\nREMOTE_HOST=tmux(5).%1\n') == {
        'service': 'ssh', 'username': 'amy', 'source': 'logind'}


def test_pid_alive(sessions):
    assert sessions._pid_alive(os.getpid()) and not sessions._pid_alive(0) and not sessions._pid_alive(-5)
    assert sessions._pid_alive(1)              # PermissionError (not ours) still means alive


def test_truncated_or_missing_utmp_is_unavailable(sessions, tmp_path):
    path = tmp_path / 'utmp'
    path.write_bytes(utmp('alice', 'pts/0', '10.0.0.4', '10.0.0.4')[:300])
    assert sessions.read_utmp([str(path)], LOG) == ('unavailable', None)
    assert sessions.read_utmp([str(tmp_path / 'missing')], LOG) == ('unavailable', None)
    path.write_bytes(utmp('alice', 'pts/0', '10.0.0.4', '10.0.0.4'))
    status, rows = sessions.read_utmp([str(tmp_path / 'missing'), str(path)], LOG)
    assert status == 'ok' and rows[0]['username'] == 'alice'


# ---------------------------------------------------------------------------
# logind
# ---------------------------------------------------------------------------

@posix_only
def test_logind_session_files(sessions, tmp_path):
    d = tmp_path / 'sessions'
    d.mkdir()
    (d / '3').write_text('# private\nUSER=alice\nSERVICE=sshd\nREMOTE_HOST=10.0.0.40\nSTATE=active\n'
                         'CLASS=user\nREALTIME=1790000000000000\n')
    (d / '4').write_text('USER=bob\nSERVICE=gdm-password\nSTATE=online\n')                # console
    (d / '5').write_text('USER=carol\nSERVICE=sshd\nREMOTE_HOST=laptop.lab\n')             # host name, no time
    (d / '6').write_text('USER=dave\nCLASS=manager\n')                                     # systemd --user
    (d / '7').write_text('USER=erin\nSTATE=closing\n')
    (d / '8').write_text('SERVICE=sshd\n')                                                 # no user
    (d / '9').write_bytes(b'\xff\xfe\x00garbage')                                          # unparseable
    os.mkfifo(str(d / '3.ref'))
    status, rows = sessions.read_logind(str(d), LOG)
    assert status == 'ok'
    assert rows == [
        {'service': 'ssh', 'username': 'alice', 'source': 'logind', 'client': '10.0.0.40', 'since': '2026-09-21T14:13:20Z'},
        {'service': 'console', 'username': 'bob', 'source': 'logind'},
        {'service': 'ssh', 'username': 'carol', 'source': 'logind', 'client_host': 'laptop.lab'},
    ]
    assert sessions.read_logind(str(tmp_path / 'missing'), LOG) == ('unavailable', None)


def test_logind_is_read_only_without_a_utmp_file(sessions, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, 'read_logind', lambda log=None: calls.append('logind') or ('ok', []))
    monkeypatch.setattr(sessions, 'read_utmp', lambda log=None: calls.append('utmp') or ('ok', []))
    utmp_file = tmp_path / 'utmp'
    monkeypatch.setattr(sessions, 'UTMP_PATHS', (str(utmp_file),))
    opts = _opts(sessions)
    sources, _ = sessions._user_sources(opts, smb_listening=False, log=LOG)
    assert calls == ['logind'] and sources == {'logind': 'ok'}
    utmp_file.write_bytes(b'')
    calls.clear()
    sources, _ = sessions._user_sources(opts, smb_listening=False, log=LOG)
    assert calls == ['utmp'] and sources == {'utmp': 'ok'}


# ---------------------------------------------------------------------------
# smbstatus
# ---------------------------------------------------------------------------

# Samba 4.x `smbstatus -b` (stock layout)
SMBSTATUS_STOCK = """
Samba version 4.19.5-Ubuntu
PID     Username     Group        Machine                                   Protocol Version  Encryption           Signing
----------------------------------------------------------------------------------------------------------------------------------------
12345   alice        alice        10.0.0.17 (ipv4:10.0.0.17:51234)          SMB3_11           -                    partial(AES-128-CMAC)
12346   LAB\\bob      domain users laptop (ipv4:10.0.0.18:51235)             SMB3_11           -                    partial(AES-128-CMAC)
12347   carol        carol        fd00::18 (ipv6:fd00::18:51236)            SMB3_11           -                    -
"""
# Illustrative DSM 7 (Samba 4.15) layout, not captured from a unit: replace with a real capture (spec 5.4)
SMBSTATUS_DSM = """
Samba version 4.15.13
PID     Username     Group        Machine                                   Protocol Version  Encryption           Signing
-------------------------------------------------------------------------------------------------------------------------
21000   jeff         users        10.11.2.40 (ipv4:10.11.2.40:60123)        SMB3_11           -                    partial(AES-128-GMAC)
"""


def test_parse_smbstatus_brief(sessions):
    rows = sessions.parse_smbstatus_brief(SMBSTATUS_STOCK)
    assert rows == [
        {'service': 'smb', 'username': 'alice', 'source': 'smb', 'client': '10.0.0.17'},
        {'service': 'smb', 'username': 'LAB\\bob', 'source': 'smb', 'client': '10.0.0.18', 'client_host': 'laptop'},
        {'service': 'smb', 'username': 'carol', 'source': 'smb', 'client': 'fd00::18'},
    ]
    assert sessions.parse_smbstatus_brief(SMBSTATUS_DSM) == [
        {'service': 'smb', 'username': 'jeff', 'source': 'smb', 'client': '10.11.2.40'}]


def test_smbstatus_runs_only_as_root_and_never_through_sudo(sessions, monkeypatch):
    runs = []

    def run(args, **kwargs):
        runs.append(args)
        assert kwargs['timeout'] == sessions.SMBSTATUS_TIMEOUT
        return subprocess.CompletedProcess(args, 0, stdout=SMBSTATUS_STOCK, stderr='')
    which = {'smbstatus': '/usr/bin/smbstatus'}.get
    monkeypatch.setattr(sessions.platform_info, 'is_root', lambda: False)
    assert sessions.read_smbstatus(run, which, LOG) == ('not_permitted', None) and runs == []
    monkeypatch.setattr(sessions.platform_info, 'is_root', lambda: True)
    status, rows = sessions.read_smbstatus(run, which, LOG)
    assert status == 'ok' and len(rows) == 3
    assert runs == [['/usr/bin/smbstatus', '-b']]          # never sudo, never plain smbstatus (file names)

    def refused(args, **kwargs):
        return subprocess.CompletedProcess(args, 255, stdout='', stderr='smbstatus only works as root!\n')

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    assert sessions.read_smbstatus(refused, which, LOG) == ('not_permitted', None)
    assert sessions.read_smbstatus(slow, which, LOG) == ('timeout', None)
    assert sessions.read_smbstatus(run, lambda name: None, LOG) == ('unavailable', None)


def test_no_sudo_anywhere_in_the_sessions_code(collector):
    for name in ('sessions.py', 'sessions_win.py'):
        with open(os.path.join(os.path.dirname(collector.__file__), 'lib', name)) as f:
            assert 'sudo' not in f.read().replace('sudo is never', '').replace('sudo is never used', ''), name


# ---------------------------------------------------------------------------
# Merge, counts, list state
# ---------------------------------------------------------------------------

USER_ROWS = [
    {'service': 'ssh', 'username': 'alice', 'client': '10.0.0.40', 'since': '2026-11-21T10:00:00Z', 'source': 'utmp'},
    {'service': 'ssh', 'username': 'alice', 'client': '10.0.0.40', 'since': '2026-11-21T09:00:00Z', 'source': 'utmp'},
    {'service': 'smb', 'username': 'HOST01$', 'client': '10.0.0.50', 'source': 'smb'},
    {'service': 'smb', 'username': 'svc_backup', 'client': '10.0.0.51', 'source': 'smb'},
    {'service': 'console', 'username': 'bob', 'since': '2026-11-20T08:00:00Z', 'source': 'utmp'},
]


def test_merge_user_rows_then_tcp_leftovers(sessions):
    from collections import Counter
    opts = _opts(sessions)
    opts.user_patterns.append('svc_*')
    tcp = Counter({('ssh', '10.0.0.40'): 1, ('smb', '10.0.0.50'): 1, ('smb', '10.0.0.51'): 3, ('smb', '10.0.0.17'): 2})
    rows, truncated = sessions.merge_and_aggregate(USER_ROWS, tcp, 'full', 200, opts.user_excluded)
    assert not truncated
    assert rows == [
        {'service': 'console', 'username': 'bob', 'since': '2026-11-20T08:00:00Z', 'source': 'utmp', 'connections': 1},
        {'service': 'ssh', 'username': 'alice', 'client': '10.0.0.40', 'since': '2026-11-21T09:00:00Z',
         'source': 'utmp', 'connections': 2},
        # HOST01$ (machine account) and svc_backup (exclude_users) are dropped, and their
        # clients don't come back as anonymous TCP rows
        {'service': 'smb', 'client': '10.0.0.17', 'connections': 2, 'source': 'tcp'},
    ]
    rows, truncated = sessions.merge_and_aggregate(USER_ROWS, tcp, 'full', 2, opts.user_excluded)
    assert truncated and len(rows) == 2


def test_detail_users_drops_clients_and_tcp_rows(sessions):
    from collections import Counter
    rows, _ = sessions.merge_and_aggregate(
        USER_ROWS + [{'service': 'ssh', 'username': 'alice', 'client': '10.0.0.41', 'source': 'utmp'}],
        Counter({('smb', '10.0.0.17'): 2}), 'users', 200)
    assert all('client' not in r and 'client_host' not in r and r['source'] != 'tcp' for r in rows)
    alice = next(r for r in rows if r['username'] == 'alice')
    assert alice['connections'] == 3 and alice['since'] == '2026-11-21T09:00:00Z'


def test_user_counts(sessions):
    rows = [{'service': 'ssh', 'username': 'alice'}, {'service': 'ssh', 'username': 'alice'},
            {'service': 'console', 'username': 'bob'}]
    assert sessions.user_counts(rows, {'tcp': 'ok', 'utmp': 'ok'}, windows=False) == {
        'users_total': 2, 'console_users': 1, 'ssh_users': 1}
    # A source that failed gives no count; none at all: users_total null
    assert sessions.user_counts([], {'tcp': 'ok', 'utmp': 'unavailable', 'smb': 'not_permitted'}, False) == {
        'users_total': None}
    win_rows = [{'service': 'rdp', 'username': 'LAB\\alice', 'state': 'active'},
                {'service': 'rdp', 'username': 'LAB\\carol', 'state': 'disconnected'},
                {'service': 'smb', 'username': 'alice'}]
    assert sessions.user_counts(win_rows, {'wts': 'partial', 'netsession': 'ok'}, windows=True) == {
        'users_total': 2, 'console_users': 0, 'rdp_users': 1, 'smb_users': 1, 'rdp_disconnected': 1}


def test_list_hash_ignores_since_and_connections(sessions):
    a = [{'service': 'ssh', 'username': 'alice', 'client': '10.0.0.4', 'since': 'x', 'connections': 1}]
    b = [{'service': 'ssh', 'username': 'alice', 'client': '10.0.0.4', 'since': 'y', 'connections': 5}]
    assert sessions.list_hash(a) == sessions.list_hash(b) != sessions.list_hash([dict(a[0], client='10.0.0.5')])
    assert len(sessions.list_hash(a)) == 16


@pytest.fixture
def host(sessions, tmp_path, monkeypatch):
    """A Linux host for collect(): /proc/net/tcp, utmp, not root, a fixed clock."""
    net = tmp_path / 'net'
    net.mkdir()
    (net / 'tcp').write_text(tcp_text([('0.0.0.0', 22, '0.0.0.0', 0, LISTEN), ('0.0.0.0', 445, '0.0.0.0', 0, LISTEN),
                                       ('10.0.0.5', 22, '10.0.0.40', 50000, ESTAB),
                                       ('10.0.0.5', 445, '10.0.0.17', 50001, ESTAB)]))
    utmp_file = tmp_path / 'utmp'
    utmp_file.write_bytes(utmp('alice', 'pts/0', '10.0.0.40', '10.0.0.40'))
    monkeypatch.setattr(sessions, 'PROC_NET', str(net))
    monkeypatch.setattr(sessions, 'UTMP_PATHS', (str(utmp_file),))
    monkeypatch.setattr(sessions.platform_info, 'is_root', lambda: False)
    clock = {'now': 1_795_000_000.0}
    monkeypatch.setattr(sessions, '_wall_time', lambda: clock['now'])
    data_dir = tmp_path / 'data'
    (data_dir / 'box').mkdir(parents=True)

    def run(**block):
        payload, state = sessions.collect({'platform': 'linux', 'sessions': block}, str(data_dir), 'box', LOG)
        if state is not None:
            (data_dir / 'box' / sessions.STATE_FILE).write_text(json.dumps(state))
        return payload
    run.clock = clock
    run.state_file = data_dir / 'box' / sessions.STATE_FILE
    return run


def test_collect_full_then_unchanged_then_heartbeat(host):
    first = host()
    assert first['_list_state'] == 'full' and first['_sources'] == {'tcp': 'ok', 'utmp': 'ok', 'smb': 'not_permitted'}
    assert first['clients_total'] == 2 and first['ssh_clients'] == 1 and first['smb_clients'] == 1
    assert 'nfs_clients' not in first and first['ssh_users'] == 1 and first['users_total'] == 1
    assert [r['source'] for r in first['sessions']] == ['utmp', 'tcp'] and first['sessions_truncated'] is False
    assert first['_schema'] == 1 and first['_unit'] == 'count' and first['_detail'] == 'full'
    host.clock['now'] += 300
    second = host()
    assert second['_list_state'] == 'unchanged' and 'sessions' not in second
    assert second['_list_hash'] == first['_list_hash'] and second['clients_total'] == 2
    host.clock['now'] += 3600
    assert host()['_list_state'] == 'full'                 # hourly heartbeat
    host.clock['now'] += 60
    assert host(history='every_run')['_list_state'] == 'full'


def test_collect_does_not_count_tmux_panes_as_ssh_users(host, sessions, tmp_path):
    """A detached tmux server outlives the login: its panes are not a user, and adding one doesn't change the list."""
    first = host()
    (tmp_path / 'utmp').write_bytes(utmp('alice', 'pts/0', '10.0.0.40', '10.0.0.40')
                                    + utmp('bob', 'pts/3', 'tmux(1357012).%0')
                                    + utmp('bob', 'pts/4', 'tmux(1357012).%1'))
    host.clock['now'] += 300
    second = host()
    assert second['_list_state'] == 'unchanged' and second['_list_hash'] == first['_list_hash']
    assert second['ssh_users'] == 1 and second['users_total'] == 1


def test_collect_counts_only_and_missing_state(host):
    counts = host(detail='counts')
    assert counts['_list_state'] == 'none' and counts['_list_hash'] is None and 'sessions' not in counts
    assert counts['clients_total'] == 2
    # After counts mode the next list is sent in full even if it is the same as before
    assert host()['_list_state'] == 'full'
    host.state_file.unlink()
    assert host()['_list_state'] == 'full'
    host.state_file.write_text('{corrupt')
    assert host()['_list_state'] == 'full'


def test_collect_writes_no_files(sessions, host, tmp_path):
    before = sorted(p for p in tmp_path.rglob('*'))
    payload, state = sessions.collect({'platform': 'linux'}, str(tmp_path / 'data'), 'box', LOG)
    assert state is not None and sorted(p for p in tmp_path.rglob('*')) == before


def test_failure_payload(sessions):
    p = sessions.failure_payload({'platform': 'linux'}, 'timeout')
    assert p['_sources'] == {'tcp': 'timeout', 'utmp': 'timeout', 'smb': 'timeout'}
    assert p['clients_total'] is None and p['users_total'] is None and p['_list_state'] == 'none'
    assert set(sessions.failure_payload({'platform': 'windows'}, 'error')['_sources']) == {'tcp', 'wts', 'netsession'}


def test_no_snmp_source_in_this_release(host):
    assert 'snmp' not in host()['_sources']


# ---------------------------------------------------------------------------
# Windows (pure helpers and a fake ctypes layer, on any OS)
# ---------------------------------------------------------------------------

def test_filetime_and_unc_helpers(win):
    assert win.filetime_to_unix(116444736000000000 + 10 ** 7) == 1.0
    assert win.filetime_to_unix(0) is None
    assert win.strip_unc('\\\\10.0.0.5') == '10.0.0.5' and win.strip_unc('\\\\[fe80::1]') == 'fe80::1'


def test_client_address_ipv4_bytes_start_at_offset_2(win):
    raw = struct.pack('<I', win.AF_INET) + bytes([0, 0, 10, 0, 0, 5]) + b'\0' * 14
    assert win.client_address(raw) == '10.0.0.5'
    raw6 = struct.pack('<I', win.AF_INET6) + ipaddress.ip_address('fd00::7').packed + b'\0' * 4
    assert win.client_address(raw6) == 'fd00::7'
    assert win.client_address(struct.pack('<I', win.AF_INET) + b'\0' * 20) is None
    assert win.client_address(b'') is None


def test_state_and_protocol_maps(win):
    assert win.STATES.get(win.WTS_DISCONNECTED) == 'disconnected' and win.STATES.get(win.WTS_ACTIVE, 'active') == 'active'
    assert win.PROTOCOLS == {0: 'console', 1: 'ica', 2: 'rdp'}


def test_struct_layouts_follow_the_headers(win):
    p = ctypes.sizeof(ctypes.c_void_p)
    # lmshare.h: sesi10_cname, sesi10_username, sesi10_time, sesi10_idle_time
    assert [f[0] for f in win.SESSION_INFO_10._fields_] == ['sesi10_cname', 'sesi10_username', 'sesi10_time',
                                                             'sesi10_idle_time']
    assert (win.SESSION_INFO_10.sesi10_username.offset, win.SESSION_INFO_10.sesi10_time.offset,
            win.SESSION_INFO_10.sesi10_idle_time.offset, ctypes.sizeof(win.SESSION_INFO_10)) == (p, 2 * p, 2 * p + 4, 2 * p + 8)
    assert ctypes.sizeof(win.WTS_CLIENT_ADDRESS) == 24
    if p == 8:
        assert win.WTSINFOW.LogonTime.offset == 200 and ctypes.sizeof(win.WTSINFOW) == 216


class FakeWtsApi:
    """wtsapi32 as ctypes sees it: out-parameters filled through byref(), buffers freed by WTSFreeMemory."""

    def __init__(self, win, sessions, denied=()):
        self.win, self.sessions, self.denied = win, sessions, set(denied)
        self.buffers, self.freed = {}, []

    def _give(self, ref, raw):
        buf = ctypes.create_string_buffer(raw, len(raw))
        self.buffers[ctypes.addressof(buf)] = buf
        ref._obj.value = ctypes.addressof(buf)
        return len(raw)

    def WTSEnumerateSessionsW(self, server, reserved, version, info, count):
        array = (self.win.WTS_SESSION_INFOW * len(self.sessions))()
        for i, (sid, state, _) in enumerate(self.sessions):
            array[i].SessionId, array[i].State = sid, state
        self._give(info, bytes(array))
        count._obj.value = len(self.sessions)
        return 1

    def WTSQuerySessionInformationW(self, server, sid, info_class, buf, size):
        sid = sid.value
        if sid in self.denied:
            return 0
        info = next(i for s, _, i in self.sessions if s == sid)
        w = self.win
        if info_class in (w.WTS_USER_NAME, w.WTS_DOMAIN_NAME, w.WTS_CLIENT_NAME):
            key = {w.WTS_USER_NAME: 'user', w.WTS_DOMAIN_NAME: 'domain', w.WTS_CLIENT_NAME: 'host'}[info_class]
            raw = info.get(key, '').encode('utf-16-le') + b'\0\0'
        elif info_class == w.WTS_CLIENT_PROTOCOL_TYPE:
            raw = struct.pack('<H', info.get('protocol', 0))
        elif info_class == w.WTS_CLIENT_ADDRESS_CLASS:
            raw = struct.pack('<I', w.AF_INET) + bytes([0, 0]) + ipaddress.IPv4Address(info['ip']).packed + b'\0' * 14
        else:
            raw = bytes(w.WTSINFOW(LogonTime=info.get('logon', 0)))
        size._obj.value = self._give(buf, raw)
        return 1

    def WTSFreeMemory(self, ptr):
        self.freed.append(ptr.value)


def test_wts_sessions(win, monkeypatch):
    logon = 116444736000000000 + 1_795_000_000 * 10 ** 7
    api = FakeWtsApi(win, [
        (0, win.WTS_DISCONNECTED, {}),                                       # services: skipped
        (65536, win.WTS_LISTEN, {}),                                         # the RDP listener
        (1, win.WTS_ACTIVE, {'user': 'alice', 'domain': 'LAB', 'logon': logon}),
        (2, win.WTS_ACTIVE, {'user': 'bob', 'domain': 'LAB', 'protocol': 2, 'ip': '10.0.0.9', 'host': 'BOB-PC',
                             'logon': logon}),
        (3, win.WTS_DISCONNECTED, {'user': 'carol', 'protocol': 2, 'ip': '10.0.0.8'}),
        (4, win.WTS_CONNECTED, {}),                                          # nobody logged on yet
    ])
    status, rows = win.read_wts(api, LOG)
    assert status == 'ok'
    assert rows == [
        {'service': 'console', 'username': 'LAB\\alice', 'state': 'active', 'source': 'wts', 'since': '2026-11-18T11:06:40Z'},
        {'service': 'rdp', 'username': 'LAB\\bob', 'state': 'active', 'source': 'wts', 'client': '10.0.0.9',
         'client_host': 'BOB-PC', 'since': '2026-11-18T11:06:40Z'},
        {'service': 'rdp', 'username': 'carol', 'state': 'disconnected', 'source': 'wts', 'client': '10.0.0.8'},
    ]
    assert sorted(api.freed) == sorted(api.buffers)                          # every buffer freed
    # A session the account may not read (user-logon install): partial
    monkeypatch.setattr(win, '_last_error', lambda: win.ERROR_ACCESS_DENIED)
    api = FakeWtsApi(win, [(1, win.WTS_ACTIVE, {'user': 'alice'}), (2, win.WTS_ACTIVE, {'user': 'bob'})], denied={2})
    status, rows = win.read_wts(api, LOG)
    assert status == 'partial' and [r['username'] for r in rows] == ['alice']


def test_wts_enumeration_denied_is_not_permitted(win, monkeypatch):
    class Denied:
        def WTSEnumerateSessionsW(self, *args):
            return 0
    monkeypatch.setattr(win, '_last_error', lambda: win.ERROR_ACCESS_DENIED)
    assert win.read_wts(Denied(), LOG) == ('not_permitted', None)


class FakeNetApi:
    """netapi32.NetSessionEnum at level 10, in batches (ERROR_MORE_DATA), freeing every buffer."""

    def __init__(self, win, batches, final=0):
        self.win, self.batches, self.final = win, list(batches), final
        self.keep, self.freed, self.levels = [], [], []

    def _wstr(self, text):
        buf = ctypes.create_string_buffer(text.encode('utf-16-le') + b'\0\0')
        self.keep.append(buf)
        return ctypes.addressof(buf)

    def NetSessionEnum(self, server, client, user, level, buf, prefmax, read, total, resume):
        self.levels.append(level.value)
        if not self.batches:
            return self.final
        batch = self.batches.pop(0)
        array = (self.win.SESSION_INFO_10 * len(batch))()
        for i, (cname, username, seconds) in enumerate(batch):
            array[i].sesi10_cname, array[i].sesi10_username = self._wstr(cname), self._wstr(username)
            array[i].sesi10_time = seconds
        self.keep.append(array)
        buf._obj.value = ctypes.addressof(array)
        read._obj.value = len(batch)
        return self.win.ERROR_MORE_DATA if self.batches else 0

    def NetApiBufferFree(self, ptr):
        self.freed.append(ptr.value)


def test_netsessions_level_10_in_batches(win):
    api = FakeNetApi(win, [[('\\\\10.0.0.17', 'alice', 120)], [('\\\\LAPTOP', 'bob', 60), ('\\\\10.0.0.9', '', 5)]])
    status, rows = win.read_netsessions(api, now=1_795_000_000, log=LOG)
    assert status == 'ok' and api.levels == [10, 10]
    assert rows == [
        {'service': 'smb', 'username': 'alice', 'source': 'netsession', 'client': '10.0.0.17', 'since': '2026-11-18T11:04:40Z'},
        {'service': 'smb', 'username': 'bob', 'source': 'netsession', 'client_host': 'LAPTOP', 'since': '2026-11-18T11:05:40Z'},
    ]
    assert len(api.freed) == 2                     # freed after ERROR_MORE_DATA too
    assert win.read_netsessions(FakeNetApi(win, [], final=win.ERROR_ACCESS_DENIED), log=LOG) == ('not_permitted', None)
    assert win.read_netsessions(FakeNetApi(win, [], final=53), log=LOG) == ('error', None)


def test_off_windows_the_apis_are_unavailable(win):
    if sys.platform == 'win32':
        pytest.skip('real Windows')
    assert win.read_wts(log=LOG) == ('unavailable', None)
    assert win.read_netsessions(log=LOG) == ('unavailable', None)


def test_windows_collect_uses_psutil_tcp_and_the_win_sources(sessions, win, monkeypatch, tmp_path):
    import types
    ns = types.SimpleNamespace
    ps = types.ModuleType('psutil')
    ps.AccessDenied = type('AccessDenied', (Exception,), {})
    ps.net_connections = lambda kind='inet': [
        ns(type=1, laddr=('0.0.0.0', 3389), raddr=(), status='LISTEN'),
        ns(type=1, laddr=('10.0.0.5', 3389), raddr=('10.0.0.9', 50000), status='ESTABLISHED'),
        ns(type=2, laddr=('0.0.0.0', 445), raddr=(), status='NONE')]                # UDP: ignored
    monkeypatch.setitem(sys.modules, 'psutil', ps)
    monkeypatch.setattr(win, 'read_all', lambda now, log=None: {
        'wts': ('ok', [{'service': 'rdp', 'username': 'LAB\\bob', 'client': '10.0.0.9', 'state': 'active',
                        'source': 'wts'}]),
        'netsession': ('not_permitted', None)})
    payload, _ = sessions.collect({'platform': 'windows'}, str(tmp_path), 'box', LOG)
    assert payload['_sources'] == {'tcp': 'ok', 'wts': 'ok', 'netsession': 'not_permitted'}
    assert payload['rdp_clients'] == 1 and 'smb_clients' not in payload and payload['rdp_users'] == 1
    assert 'smb_users' not in payload and payload['rdp_disconnected'] == 0
    assert payload['sessions'] == [{'service': 'rdp', 'username': 'LAB\\bob', 'client': '10.0.0.9', 'state': 'active',
                                    'source': 'wts', 'connections': 1}]

    def denied(kind='inet'):
        raise ps.AccessDenied()
    ps.net_connections = denied
    payload, _ = sessions.collect({'platform': 'windows'}, str(tmp_path), 'box', LOG)
    assert payload['_sources']['tcp'] == 'not_permitted' and payload['clients_total'] is None
