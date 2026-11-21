"""Tests for tools/_ssh.py: exec helper (no window deadlock), host-key TOFU, helpers."""
import _thread
import base64
import socket
import threading
import time

import paramiko
import pytest

import _ssh


# ---------------------------------------------------------------------------
# Fake paramiko channel with a flow-control window
# ---------------------------------------------------------------------------

class FakeChannel:
    """
    Models an SSH channel whose remote command writes stdout/stderr but can
    only have `window` unread bytes in flight. The exit status becomes ready
    only after all output has been sent, i.e. only if the reader keeps
    draining — exactly the situation in which calling recv_exit_status()
    before reading hangs forever with real paramiko.
    """

    def __init__(self, stdout=b'', stderr=b'', exit_code=0, window=64 * 1024, finishes=True):
        self._remote_out = bytearray(stdout)
        self._remote_err = bytearray(stderr)
        self._buf_out = bytearray()
        self._buf_err = bytearray()
        self.window = window
        self.exit_code = exit_code
        self.finishes = finishes
        self.eof_received = False
        self.closed = False
        self.command = None
        self.write_shut = False

    # remote side -----------------------------------------------------------
    def _pump(self):
        room = self.window - len(self._buf_out) - len(self._buf_err)
        while room > 0 and (self._remote_out or self._remote_err):
            for remote, buf in ((self._remote_out, self._buf_out), (self._remote_err, self._buf_err)):
                if remote and room > 0:
                    n = min(room, 4096, len(remote))
                    buf += remote[:n]
                    del remote[:n]
                    room -= n
        if self.finishes and not self._remote_out and not self._remote_err:
            self.eof_received = True

    def _done(self):
        return self.finishes and not self._remote_out and not self._remote_err

    # channel API -----------------------------------------------------------
    def exec_command(self, command):
        self.command = command

    def shutdown_write(self):
        self.write_shut = True

    def recv_ready(self):
        self._pump()
        return bool(self._buf_out)

    def recv_stderr_ready(self):
        self._pump()
        return bool(self._buf_err)

    def recv(self, n):
        self._pump()
        data = bytes(self._buf_out[:n])
        del self._buf_out[:n]
        return data

    def recv_stderr(self, n):
        self._pump()
        data = bytes(self._buf_err[:n])
        del self._buf_err[:n]
        return data

    def exit_status_ready(self):
        self._pump()
        return self._done()

    def recv_exit_status(self):
        if not self.exit_status_ready():
            raise AssertionError("recv_exit_status() would block forever: output not drained")
        return self.exit_code

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, chan):
        self.chan = chan

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        return self.chan


class FakeClient:
    def __init__(self, chan):
        self.transport = FakeTransport(chan)

    def get_transport(self):
        return self.transport


def test_fake_channel_reproduces_old_deadlock():
    """Sanity check of the fake: the old 'recv_exit_status() then read()' pattern hangs."""
    chan = FakeChannel(stdout=b'x' * (1024 * 1024))
    with pytest.raises(AssertionError, match='block forever'):
        chan.recv_exit_status()


def test_collect_output_large_output_does_not_deadlock():
    out = bytes(range(256)) * 4096          # 1 MiB
    err = b'warning line\n' * 25000         # ~325 KB
    chan = FakeChannel(stdout=out, stderr=err, exit_code=0, window=64 * 1024)
    got_out, got_err, code = _ssh.collect_output(chan, timeout=10, poll_interval=0.001)
    assert got_out == out
    assert got_err == err
    assert code == 0


def test_exec_command_captures_exit_code_and_decodes():
    chan = FakeChannel(stdout=b'  hello\n', stderr=b'oops \xff\n', exit_code=3)
    out, err, code = _ssh.exec_command(FakeClient(chan), 'do-thing', timeout=5, poll_interval=0.001)
    assert (out, code) == ('hello', 3)
    assert err.startswith('oops')           # undecodable byte replaced, not an exception
    assert chan.command == 'do-thing'
    assert chan.write_shut and chan.closed


def test_exec_command_timeout_raises_with_partial_output():
    chan = FakeChannel(stdout=b'partial', finishes=False)
    start = time.monotonic()
    with pytest.raises(_ssh.RemoteCommandTimeout) as info:
        _ssh.exec_command(FakeClient(chan), 'sleep forever', timeout=0.2, poll_interval=0.01)
    assert time.monotonic() - start < 5
    assert info.value.stdout == 'partial'
    assert chan.closed


def test_exec_command_no_timeout_waits_for_exit():
    chan = FakeChannel(stdout=b'ok', exit_code=0)
    assert _ssh.exec_command(FakeClient(chan), 'x', timeout=None, poll_interval=0.001) == ('ok', '', 0)


# ---------------------------------------------------------------------------
# In-process SSH server (real paramiko) for host-key and end-to-end tests
# ---------------------------------------------------------------------------

class _ServerIface(paramiko.ServerInterface):
    def __init__(self, server):
        self.server = server

    def get_allowed_auths(self, username):
        return 'password'

    def check_auth_password(self, username, password):
        if username == 'tester' and password == 'pw':
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED_OR_UNKNOWN_CHANNEL_TYPE

    def check_channel_exec_request(self, channel, command):
        def run():
            # Let the transport send the exec-request reply first (like sshd does);
            # closing the channel before it would make the client's request fail.
            time.sleep(0.1)
            out, err, code = self.server.responses.get(command.decode(), (b'', b'unknown', 127))
            channel.sendall(out)
            channel.sendall_stderr(err)
            channel.send_exit_status(code)
            channel.shutdown_write()
            channel.close()
        threading.Thread(target=run, daemon=True).start()
        return True


class SSHTestServer:
    """Accepts connections on 127.0.0.1:<port>, presenting `self.host_key`."""

    def __init__(self, host_key):
        self.host_key = host_key
        self.responses = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            self.transports.append(t)
            try:
                t.start_server(server=_ServerIface(self))
            except Exception:
                pass

    def close(self):
        self._stop = True
        self.sock.close()
        for t in self.transports:
            t.close()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Keep ~/.ssh (known_hosts, keys) and the ssh-agent out of the tests."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    return home


@pytest.fixture
def ssh_server():
    server = SSHTestServer(paramiko.ECDSAKey.generate())
    yield server
    server.close()


def _connect(server, known_hosts):
    return _ssh.connect('127.0.0.1', 'tester', password='pw', port=server.port,
                        timeout=10, known_hosts_file=known_hosts)


def test_unknown_host_key_is_saved_then_trusted(ssh_server, isolated_home, tmp_path):
    kh = tmp_path / 'known_hosts'
    client = _connect(ssh_server, kh)
    client.close()

    assert kh.exists()
    saved = paramiko.HostKeys(str(kh))
    entry = saved.lookup(f'[127.0.0.1]:{ssh_server.port}')
    assert entry is not None
    assert entry[ssh_server.host_key.get_name()] == ssh_server.host_key

    # Second connection with the same key succeeds and doesn't rewrite the entry.
    client = _connect(ssh_server, kh)
    client.close()
    assert len(list(paramiko.HostKeys(str(kh)).keys())) == 1


def test_changed_host_key_is_rejected(ssh_server, isolated_home, tmp_path):
    kh = tmp_path / 'known_hosts'
    _connect(ssh_server, kh).close()
    original = kh.read_text()

    ssh_server.host_key = paramiko.ECDSAKey.generate()     # host "reinstalled" / MITM
    with pytest.raises(_ssh.HostKeyChangedError) as info:
        _connect(ssh_server, kh)
    assert isinstance(info.value, paramiko.BadHostKeyException)
    assert 'HOST KEY CHANGED' in str(info.value)
    assert kh.read_text() == original                      # trusted key not replaced


def test_end_to_end_large_output_and_exit_code(ssh_server, isolated_home, tmp_path):
    # Larger than paramiko's default 2 MiB window: the old code would hang here.
    big = b'0123456789abcdef' * (5 * 1024 * 1024 // 16)
    ssh_server.responses['spew'] = (big, b'some stderr\n' * 1000, 7)
    client = _connect(ssh_server, tmp_path / 'known_hosts')
    try:
        out, err, code = _ssh.exec_command(client, 'spew', timeout=60)
    finally:
        client.close()
    assert code == 7
    assert out == big.decode()
    assert err.count('some stderr') == 1000


def test_policy_rejects_key_saved_meanwhile(tmp_path):
    """A key saved by another worker after this client loaded the file still pins the host."""
    kh = tmp_path / 'known_hosts'
    first, second = paramiko.ECDSAKey.generate(), paramiko.ECDSAKey.generate()
    hk = paramiko.HostKeys()
    hk.add('nas1', first.get_name(), first)
    hk.save(str(kh))

    client = paramiko.SSHClient()
    policy = _ssh.TrustOnFirstUsePolicy(kh)
    with pytest.raises(_ssh.HostKeyChangedError):
        policy.missing_host_key(client, 'nas1', second)
    policy.missing_host_key(client, 'nas1', first)          # same key: accepted
    policy.missing_host_key(client, 'nas2', second)         # new host: saved, merged
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup('nas1')[first.get_name()] == first
    assert saved.lookup('nas2')[second.get_name()] == second


def _write_system_known_hosts(home, text):
    ssh_dir = home / '.ssh'
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / 'known_hosts').write_text(text)


def test_cert_authority_and_damaged_lines_do_not_block_connect(ssh_server, isolated_home, tmp_path, capsys):
    """paramiko's HostKeys.load raises on these lines; they must be skipped, not fatal."""
    other = paramiko.ECDSAKey.generate()
    _write_system_known_hosts(isolated_home, (
        f"@cert-authority *.med.harvard.edu {other.get_name()} {other.get_base64()}\n"
        f"@revoked * {other.get_name()} {other.get_base64()}\n"
        f"nas9 {other.get_name()} {other.get_base64()}\n"
        "brokenhost ssh-ed25519 AAAA-not-base64\n"
    ))
    kh = tmp_path / 'known_hosts'
    _connect(ssh_server, kh).close()
    _connect(ssh_server, kh).close()
    out = capsys.readouterr().out
    assert out.count("WARNING: ignoring 3 line(s)") == 1           # warned once per file
    assert paramiko.HostKeys(str(kh)).lookup(f'[127.0.0.1]:{ssh_server.port}') is not None


def test_damaged_line_in_tools_known_hosts_is_kept(ssh_server, isolated_home, tmp_path):
    other = paramiko.ECDSAKey.generate()
    kh = tmp_path / 'known_hosts'
    kh.write_text(f"nas9 {other.get_name()} {other.get_base64()}\nhalf-edited-line ssh-rsa AAA")   # no final newline
    _connect(ssh_server, kh).close()
    lines = kh.read_text().splitlines()
    assert lines[:2] == [f"nas9 {other.get_name()} {other.get_base64()}", "half-edited-line ssh-rsa AAA"]
    assert lines[2].startswith(f'[127.0.0.1]:{ssh_server.port} ')       # appended on its own line
    _connect(ssh_server, kh).close()                                        # and trusted afterwards


def test_revoked_key_is_not_trusted_on_first_use(ssh_server, isolated_home, tmp_path):
    key = ssh_server.host_key
    _write_system_known_hosts(isolated_home, f"@revoked * {key.get_name()} {key.get_base64()}\n")
    kh = tmp_path / 'known_hosts'
    with pytest.raises(_ssh.RevokedHostKeyError) as info:
        _connect(ssh_server, kh)
    assert 'REVOKED' in str(info.value)
    assert not kh.exists()


def test_missing_key_file_falls_back_to_password(ssh_server, isolated_home, tmp_path, capsys):
    client = _ssh.connect('127.0.0.1', 'tester', password='pw', port=ssh_server.port, timeout=10,
                          key_filename=str(tmp_path / 'no-such-key'),
                          known_hosts_file=tmp_path / 'known_hosts')
    client.close()
    assert 'key file' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def test_connect_row_reads_optional_columns(monkeypatch):
    calls = []
    monkeypatch.setattr(_ssh, 'connect', lambda *a, **kw: calls.append((a, kw)) or 'client')
    row = {'ip': ' nas1 ', 'username': 'jeff', 'password': '', 'key_filename': '~/.ssh/k', 'port': '2222'}
    assert _ssh.connect_row(row, timeout=5) == 'client'
    args, kwargs = calls[-1]
    assert args == ('nas1', 'jeff')
    assert kwargs['password'] is None and kwargs['key_filename'] == '~/.ssh/k' and kwargs['port'] == 2222

    # Old CSV without the new columns; explicit key overrides; password kept as fallback.
    _ssh.connect_row({'ip': 'nas2', 'username': 'u', 'password': 'pw'}, key_filename='/k')
    _args, kwargs = calls[-1]
    assert kwargs['password'] == 'pw' and kwargs['key_filename'] == '/k' and kwargs['port'] == 22


def test_read_inventory_handles_bom_and_short_rows(tmp_path):
    p = tmp_path / 'collectors.csv'
    p.write_bytes('\ufeffip, username ,password,git_path,python_path\n1.2.3.4,jeff\n'.encode('utf-8'))
    rows, fields = _ssh.read_inventory(p)
    assert fields == ['ip', 'username', 'password', 'git_path', 'python_path']
    assert rows[0]['ip'] == '1.2.3.4' and rows[0]['git_path'] == ''


def test_run_hosts_parallel_keeps_input_order():
    events = []

    def worker(n):
        time.sleep(0.05 * (5 - n))           # later items finish first
        return n * 10

    results = _ssh.run_hosts([1, 2, 3, 4], worker, parallel=4,
                             on_start=lambda i, item: events.append(('start', i)),
                             on_done=lambda i, item, r: events.append(('done', i, r)))
    assert results == [10, 20, 30, 40]
    assert [e for e in events if e[0] == 'done'] == [('done', i, i * 10) for i in (1, 2, 3, 4)]


def test_run_hosts_parallel_ctrl_c_stops_pending_hosts(capsys):
    """Ctrl-C must not let queued hosts run (archive/toggle would keep changing systems)."""
    ran, shown = [], []

    def worker(n):
        ran.append(n)
        time.sleep(0.05 if n == 1 else 1.0)
        return n

    threading.Timer(0.3, _thread.interrupt_main).start()
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _ssh.run_hosts(list(range(8)), worker, parallel=2, label=lambda n: f'host{n}',
                       on_done=lambda i, n, r: shown.append(n))
    assert time.monotonic() - t0 < 0.9                 # not after the running hosts finish
    time.sleep(1.2)
    assert sorted(ran) == [0, 1, 2]                    # nothing new started after Ctrl-C
    assert shown == [1]                                # finished out of order: still reported
    out = capsys.readouterr().out
    assert 'state unknown, check these): host0, host2' in out
    assert 'Not started: host3, host4, host5, host6, host7' in out


def test_run_hosts_sequential_calls_on_start_before_worker():
    events = []
    _ssh.run_hosts(['a', 'b'], lambda x: events.append(('work', x)),
                   on_start=lambda i, x: events.append(('start', x)))
    assert events == [('start', 'a'), ('work', 'a'), ('start', 'b'), ('work', 'b')]


def test_powershell_command_round_trips():
    script = "Write-Output 'it''s \"quoted\" | piped & $x'"
    cmd = _ssh.powershell_command(script)
    assert cmd.startswith('powershell -NoProfile -NonInteractive')
    encoded = cmd.split()[-1]
    assert base64.b64decode(encoded).decode('utf-16-le').endswith('\n' + script)
    assert set(encoded) <= set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')


def test_ps_quote_escapes_single_quotes():
    assert _ssh.ps_quote("E:\\it's") == "'E:\\it''s'"


def test_ps_quote_escapes_curly_single_quotes():
    # PowerShell also ends a '...' literal at U+2018..U+201B; doubling escapes them.
    assert _ssh.ps_quote('E:\\Jeff\u2019s \u2018lab\u201a\u201b') == \
        "'E:\\Jeff\u2019\u2019s \u2018\u2018lab\u201a\u201a\u201b\u201b'"


def test_run_collectors_over_real_ssh(ssh_server, isolated_home, tmp_path, monkeypatch):
    """run_collectors.run_collection end to end: CSV row (with port column) → SSH → exit code."""
    import run_collectors
    monkeypatch.setattr(_ssh, 'KNOWN_HOSTS_FILE', tmp_path / 'known_hosts')
    row = {'ip': '127.0.0.1', 'username': 'tester', 'password': 'pw', 'port': str(ssh_server.port),
           'git_path': '/g', 'python_path': '/py'}
    cmd = '"/py" "/g/collector/collector.py" --config "/g/collector/local/config.json" --mode metrics'
    ssh_server.responses[cmd] = (b'=== METRICS ===\nqueued 1\n', b'', 0)
    ok = run_collectors.run_collection(row, mode='metrics', dry_run=False, timeout=10, command_timeout=30)
    assert ok['status'] == 'ok' and 'queued 1' in ok['output'], ok

    ssh_server.responses[cmd] = (b'', b'Traceback: boom\n', 1)
    bad = run_collectors.run_collection(row, mode='metrics', dry_run=False, timeout=10, command_timeout=30)
    assert bad['status'] == 'error' and 'boom' in bad['error'], bad
    assert (tmp_path / 'known_hosts').exists()
