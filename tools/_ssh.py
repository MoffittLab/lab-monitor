#!/usr/bin/env python3
"""
Lab Monitor - shared SSH helpers for the fleet tools

Used by run_collectors.py, update_collectors.py, toggle_collectors.py,
archive_all_collectors.py and restart_manager_dashboard.py so they all
connect, authenticate and run remote commands the same way.

Host keys (trust on first use):
    - Keys in ~/.ssh/known_hosts are honoured.
    - The first time a host is seen, its key is appended to tools/.known_hosts
      (gitignored) and the fingerprint is printed.
    - A host whose key has CHANGED is rejected (HostKeyChangedError). If the
      change is expected (host reinstalled, new SSH server), delete that host's
      line from tools/.known_hosts (or ~/.ssh/known_hosts) and reconnect.
    - Lines paramiko can't parse (OpenSSH @cert-authority / @revoked markers,
      a damaged line) are skipped with a warning instead of breaking every
      connection; a key listed as @revoked is never trusted on first use.

Authentication, per collectors.csv row:
    key_filename  optional column: private key file to offer first (skipped
                  with a warning if the file doesn't exist or can't be read)
    (always)      ssh-agent keys and ~/.ssh/id_* keys are offered next
    password      tried last, as a fallback, when the column is non-empty

Remote commands:
    exec_command() reads stdout and stderr while the command runs, so a command
    that prints a lot can't stall on a full SSH window (the classic paramiko
    recv_exit_status() hang), and enforces an overall timeout.

Fleet runs:
    run_hosts() works on one host at a time or, with --parallel N, on N at
    once. Ctrl-C stops it in both modes: hosts not yet started are skipped.

Requirements:
    pip install paramiko
"""

import argparse
import base64
import binascii
import csv
import hashlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


# Persisted trust-on-first-use host keys (gitignored; see .gitignore).
KNOWN_HOSTS_FILE = Path(__file__).resolve().parent / '.known_hosts'

# Serializes reads/writes of the known_hosts file between --parallel workers
# (re-entrant: the policy loads the file while holding it).
_known_hosts_lock = threading.RLock()

# known_hosts files already warned about (warn once per run, not per host).
_warned_known_hosts = set()

# Read size per recv() call, and how long to keep reading after the exit
# status arrives in case the server sends trailing output before its EOF.
_CHUNK = 32768
_EOF_GRACE_SECONDS = 2.0


def _noop(_msg: str):
    pass


def require_paramiko():
    """Exit with an install hint if paramiko is missing."""
    if paramiko is None:
        print("ERROR: paramiko is required. Install with: pip install paramiko")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Platform / quoting helpers
# ---------------------------------------------------------------------------

def is_windows_path(path: str) -> bool:
    """Heuristic: Windows paths contain a drive letter, backslash or .exe extension."""
    p = (path or '').strip()
    return (
        p.lower().endswith('.exe') or
        (len(p) >= 2 and p[1] == ':') or
        '\\' in p
    )


# PowerShell treats the curly single quotes as quote characters too
# (about_Quoting_Rules), so they must be doubled like the ASCII apostrophe.
_PS_SINGLE_QUOTES = re.compile("(['\u2018\u2019\u201a\u201b])")


def ps_quote(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal (no expansion)."""
    return "'" + _PS_SINGLE_QUOTES.sub(r'\1\1', str(value)) + "'"


def powershell_command(script: str) -> str:
    """
    Wrap a PowerShell script as a command line that survives any remote shell.

    -EncodedCommand takes base64 UTF-16LE, so no quoting/escaping is needed
    whether the Windows OpenSSH default shell is cmd.exe or PowerShell.
    Works with Windows PowerShell 5.1. Progress output is turned off so it
    doesn't end up (as CLIXML) on stderr.
    """
    script = "$ProgressPreference = 'SilentlyContinue'\n" + script
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return ('powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-EncodedCommand {encoded}')


# ---------------------------------------------------------------------------
# Inventory (collectors.csv)
# ---------------------------------------------------------------------------

def read_inventory(csv_path) -> tuple:
    """
    Read collectors.csv. Returns (rows, fieldnames).

    Header names are stripped of whitespace and a UTF-8 BOM (Excel) is
    tolerated. Columns: ip, username, password, git_path, python_path, plus
    the optional key_filename and port.
    """
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = [(n or '').strip() for n in (reader.fieldnames or [])]
        reader.fieldnames = fieldnames
        rows = []
        for row in reader:
            # Normalize missing cells (short rows) to '' so .strip() is safe.
            rows.append({k: (v if v is not None else '') for k, v in row.items() if k is not None})
    return rows, fieldnames


def _host_matches(host: str, wanted: str) -> bool:
    """
    True if the inventory host (the ip column) is `wanted`, ignoring case, or
    a host name whose first label is `wanted` (triton2 -> triton2.example.org).
    An IP address only matches exactly, so '192' never selects 192.168.1.42.
    """
    host, wanted = host.strip().lower(), wanted.strip().lower()
    if not host or not wanted:
        return False
    if host == wanted:
        return True
    looks_like_ip = all(c.isdigit() or c in '.:' for c in host)
    return not looks_like_ip and host.split('.', 1)[0] == wanted


def select_rows(rows: list, only: list) -> tuple:
    """
    The inventory rows chosen by --only values (all rows when `only` is empty).
    Returns (selected rows in inventory order, values that matched no row).
    """
    if not only:
        return list(rows), []
    unmatched = [w for w in only if not any(_host_matches(row_value(r, 'ip'), w) for r in rows)]
    selected = [r for r in rows if any(_host_matches(row_value(r, 'ip'), w) for w in only)]
    return selected, unmatched


def row_value(row: dict, key: str, default: str = '') -> str:
    """Stripped CSV cell value ('' when the column is absent or empty)."""
    value = row.get(key)
    if value is None:
        return default
    return str(value).strip() or default


# ---------------------------------------------------------------------------
# Host keys
# ---------------------------------------------------------------------------

def key_fingerprint(key) -> str:
    """OpenSSH-style SHA256 fingerprint of a paramiko key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


_BadHostKeyBase = paramiko.BadHostKeyException if paramiko else Exception
_PolicyBase = paramiko.MissingHostKeyPolicy if paramiko else object


class HostKeyChangedError(_BadHostKeyBase):
    """The host presented a different key than the one we trusted before."""

    def __init__(self, hostname, got_key, expected_key, known_hosts_file=None):
        super().__init__(hostname, got_key, expected_key)
        self.known_hosts_file = known_hosts_file

    def __str__(self):
        where = self.known_hosts_file or KNOWN_HOSTS_FILE
        return (
            f"HOST KEY CHANGED for {self.hostname}: got {self.key.get_name()} "
            f"{key_fingerprint(self.key)}, expected {self.expected_key.get_name()} "
            f"{key_fingerprint(self.expected_key)}. Refusing to connect (possible "
            f"man-in-the-middle). If the host was legitimately reinstalled, remove its "
            f"line from {where} (or ~/.ssh/known_hosts) and reconnect."
        )


class RevokedHostKeyError(_BadHostKeyBase):
    """The host presented a key that a known_hosts file lists as @revoked."""

    def __init__(self, hostname, key):
        super().__init__(hostname, key, key)

    def __str__(self):
        return (f"REVOKED HOST KEY for {self.hostname}: {self.key.get_name()} "
                f"{key_fingerprint(self.key)} is listed as @revoked in a known_hosts "
                f"file. Refusing to connect.")


def _scan_known_hosts(path) -> tuple:
    """
    Read a known_hosts file line by line. Returns (usable_lines, ignored, revoked):

        usable_lines  the lines paramiko can load
        ignored       line numbers paramiko can't use: OpenSSH '@cert-authority'
                      and '@revoked' markers, damaged lines. HostKeys.load()
                      raises InvalidHostKey on the first of these, which would
                      otherwise stop every connection.
        revoked       key blobs (bytes) of the '@revoked' lines
    """
    usable, ignored, revoked = [], [], set()
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('@'):
                fields = line.split()
                if fields[0] == '@revoked' and len(fields) >= 4:
                    try:
                        revoked.add(base64.b64decode(fields[3]))
                    except (binascii.Error, ValueError):
                        pass
                ignored.append(lineno)
                continue
            try:
                paramiko.hostkeys.HostKeyEntry.from_line(line, lineno)
            except paramiko.SSHException:
                pass                    # HostKeys.load() itself skips these
            except Exception:
                ignored.append(lineno)
                continue
            usable.append(line)
    return usable, ignored, revoked


def _load_known_hosts(load, path) -> set:
    """
    Load a known_hosts file with load(filename) (HostKeys.load or
    SSHClient.load_system_host_keys), skipping the lines paramiko can't parse
    with a one-time warning. Returns the '@revoked' key blobs.
    """
    usable, ignored, revoked = _scan_known_hosts(path)
    if not ignored:
        load(str(path))
        return revoked
    with _known_hosts_lock:
        first_warning = str(path) not in _warned_known_hosts
        _warned_known_hosts.add(str(path))
    if first_warning:
        shown = ', '.join(str(n) for n in ignored[:10]) + (', ...' if len(ignored) > 10 else '')
        print(f"  [ssh] WARNING: ignoring {len(ignored)} line(s) of {path} that paramiko can't "
              f"use (line {shown}: @cert-authority/@revoked markers or damaged entries)", flush=True)
    fd, tmp = tempfile.mkstemp(prefix='known_hosts.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(usable) + '\n')
        load(tmp)
    finally:
        os.unlink(tmp)
    return revoked


def _append_host_key(path: Path, hostname: str, key):
    """
    Append one host key line to a known_hosts file (created with mode 0600).
    Existing lines, including ones paramiko can't parse, are left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ''
    if path.exists() and path.stat().st_size:
        with open(path, 'rb') as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b'\n':
                prefix = '\n'
    line = paramiko.hostkeys.HostKeyEntry([hostname], key).to_line()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(prefix + line)


class TrustOnFirstUsePolicy(_PolicyBase):
    """
    Accept and persist the key of a host we have never seen; reject a host
    whose key differs from a saved one.

    Paramiko itself raises BadHostKeyException for a known host with a changed
    key (the keys are loaded into the client before connecting); this policy
    only runs for hosts with no saved key. It re-reads the file under a lock
    and appends to it, so parallel workers (and a key saved meanwhile) are
    merged rather than overwritten. Keys in `revoked` (blobs from '@revoked'
    known_hosts lines) are refused.
    """

    def __init__(self, known_hosts_file=None, log=None, revoked=None):
        self.known_hosts_file = Path(known_hosts_file or KNOWN_HOSTS_FILE)
        self.log = log or _noop
        self.revoked = set(revoked or ())

    def missing_host_key(self, client, hostname, key):
        keytype = key.get_name()
        if key.asbytes() in self.revoked:
            raise RevokedHostKeyError(hostname, key)
        with _known_hosts_lock:
            saved = paramiko.HostKeys()
            revoked = set()
            if self.known_hosts_file.exists():
                revoked = _load_known_hosts(saved.load, self.known_hosts_file)
            if key.asbytes() in revoked:
                raise RevokedHostKeyError(hostname, key)
            known = saved.lookup(hostname)
            if known is not None:
                # Saved by another worker/process since this client loaded the file.
                expected = known.get(keytype) or list(known.values())[0]
                if expected != key:
                    raise HostKeyChangedError(hostname, key, expected, self.known_hosts_file)
            else:
                _append_host_key(self.known_hosts_file, hostname, key)
                print(f"  [ssh] New host {hostname}: trusting {keytype} key "
                      f"{key_fingerprint(key)} (saved to {self.known_hosts_file})", flush=True)
        client.get_host_keys().add(hostname, keytype, key)


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------

def connect(host: str, username: str, password: str = None, key_filename: str = None,
            port: int = 22, timeout: float = 30, known_hosts_file=None, log=None):
    """
    Open an authenticated SSHClient with trust-on-first-use host key checking.

    Offers key_filename (if given and readable; a missing file is skipped
    with a warning), then ssh-agent and ~/.ssh/id_* keys, then the password
    (if given). Raises HostKeyChangedError on a changed host key,
    RevokedHostKeyError on a key listed as @revoked,
    paramiko.AuthenticationException / SSHException / socket errors otherwise.
    """
    require_paramiko()
    log = log or _noop
    kh = Path(known_hosts_file or KNOWN_HOSTS_FILE)

    key_path = os.path.expanduser(key_filename) if key_filename else None
    if key_path and not (os.path.isfile(key_path) and os.access(key_path, os.R_OK)):
        # paramiko would raise FileNotFoundError here instead of trying the
        # other methods, which is not the documented fallback.
        print(f"  [ssh] WARNING: key file {key_filename} not found or not readable; "
              f"trying agent/default keys{' and password' if password else ''} for {host}",
              flush=True)
        key_path = None

    methods = []
    if key_path:
        methods.append(f'key {key_filename}')
    methods.append('agent/default keys')
    if password:
        methods.append('password')
    log(f"Connecting to {host}:{port} as '{username}' "
        f"(auth: {', '.join(methods)}, timeout: {timeout}s)")

    client = paramiko.SSHClient()
    revoked = set()
    system_kh = Path(os.path.expanduser('~/.ssh/known_hosts'))   # read-only, optional
    if system_kh.is_file():
        try:
            revoked |= _load_known_hosts(client.load_system_host_keys, system_kh)
        except OSError:
            pass                        # unreadable: ignored, as paramiko does
    if kh.exists():
        with _known_hosts_lock:
            revoked |= _load_known_hosts(client.get_host_keys().load, kh)
    client.set_missing_host_key_policy(TrustOnFirstUsePolicy(kh, log, revoked))

    kwargs = dict(
        hostname=host,
        port=int(port or 22),
        username=username,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        allow_agent=True,
        look_for_keys=True,
    )
    if key_path:
        kwargs['key_filename'] = key_path
    if password:
        kwargs['password'] = password

    try:
        client.connect(**kwargs)
    except (HostKeyChangedError, RevokedHostKeyError):
        client.close()
        raise
    except paramiko.BadHostKeyException as e:
        client.close()
        raise HostKeyChangedError(e.hostname, e.key, e.expected_key, kh) from None
    except BaseException:
        client.close()
        raise
    log(f"Connected to {host}")
    return client


def connect_row(row: dict, timeout: float = 30, log=None, key_filename: str = None,
                known_hosts_file=None):
    """
    connect() using a collectors.csv row (ip, username, password and the
    optional key_filename / port columns). An explicit key_filename argument
    (e.g. a --key flag) takes precedence over the row's column.
    """
    port_text = row_value(row, 'port', '22')
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"invalid port {port_text!r} in CSV row for {row_value(row, 'ip')}")
    return connect(
        row_value(row, 'ip'),
        row_value(row, 'username'),
        password=row_value(row, 'password') or None,
        key_filename=key_filename or row_value(row, 'key_filename') or None,
        port=port,
        timeout=timeout,
        known_hosts_file=known_hosts_file,
        log=log,
    )


# ---------------------------------------------------------------------------
# Remote commands
# ---------------------------------------------------------------------------

class RemoteCommandTimeout(Exception):
    """The remote command did not finish within the timeout."""

    def __init__(self, timeout, stdout: bytes = b'', stderr: bytes = b''):
        super().__init__(f"remote command timed out after {timeout:g}s")
        self.timeout = timeout
        self.stdout = stdout.decode('utf-8', errors='replace')
        self.stderr = stderr.decode('utf-8', errors='replace')


def _drain(chan, out: bytearray, err: bytearray) -> bool:
    """Read whatever is buffered on both streams. Returns True if anything was read."""
    got = False
    while chan.recv_ready():
        data = chan.recv(_CHUNK)
        if not data:
            break
        out += data
        got = True
    while chan.recv_stderr_ready():
        data = chan.recv_stderr(_CHUNK)
        if not data:
            break
        err += data
        got = True
    return got


def collect_output(chan, timeout: float = None, poll_interval: float = 0.05) -> tuple:
    """
    Read stdout/stderr from a channel whose command is running until the
    command exits. Returns (stdout_bytes, stderr_bytes, exit_code).

    Output is consumed continuously, so the remote side never blocks on a full
    window waiting for us while we wait for its exit status. timeout (seconds,
    None/0 = no limit) bounds the whole command; RemoteCommandTimeout carries
    the partial output.
    """
    deadline = time.monotonic() + timeout if timeout else None
    out, err = bytearray(), bytearray()

    while True:
        got = _drain(chan, out, err)
        if not got and chan.exit_status_ready():
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise RemoteCommandTimeout(timeout, bytes(out), bytes(err))
        if not got:
            time.sleep(poll_interval)

    # Exit status is in. Normally all output precedes it, but keep reading
    # briefly until EOF/close in case the server sent the status first.
    grace = time.monotonic() + _EOF_GRACE_SECONDS
    while not (chan.eof_received or chan.closed) and time.monotonic() < grace:
        if not _drain(chan, out, err):
            time.sleep(poll_interval)
    _drain(chan, out, err)

    return bytes(out), bytes(err), chan.recv_exit_status()


def exec_command(client, command: str, timeout: float = None, log=None,
                 poll_interval: float = 0.05) -> tuple:
    """
    Run a command on the remote host. Returns (stdout, stderr, exit_code) with
    both streams decoded (UTF-8, undecodable bytes replaced) and stripped.

    exit_code is -1 if the server closed the channel without an exit status.
    Raises RemoteCommandTimeout if the command runs longer than timeout seconds
    (None/0 = wait indefinitely); the channel is closed in that case.
    """
    log = log or _noop
    log(f"→ {command}")
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise paramiko.SSHException('SSH session is not active')
    chan = transport.open_session(timeout=timeout or None)
    try:
        chan.exec_command(command)
        try:
            chan.shutdown_write()       # we never send stdin; let the command see EOF
        except Exception:
            pass
        out_b, err_b, code = collect_output(chan, timeout=timeout, poll_interval=poll_interval)
    finally:
        chan.close()

    out = out_b.decode('utf-8', errors='replace').strip()
    err = err_b.decode('utf-8', errors='replace').strip()
    log(f"← exit {code}")
    if out:
        log(f"  stdout: {out[:500]}{'...' if len(out) > 500 else ''}")
    if err:
        log(f"  stderr: {err[:500]}{'...' if len(err) > 500 else ''}")
    return out, err, code


# ---------------------------------------------------------------------------
# Fleet execution
# ---------------------------------------------------------------------------

def _host_label(item) -> str:
    """Name of a work item in messages: a CSV row's ip, else str(item)."""
    if isinstance(item, dict):
        return row_value(item, 'ip') or '?'
    return str(item)


def run_hosts(items: list, worker, parallel: int = 1, on_start=None, on_done=None,
              label=None) -> list:
    """
    Run worker(item) for every item and return the results in input order.

    parallel=1 (default) runs hosts one at a time, calling on_start(i, item)
    before and on_done(i, item, result) after each one (i is 1-based).
    parallel>1 runs up to that many hosts at once in threads; on_start/on_done
    are still called from the calling thread, in input order, as results
    become available, so printed output stays grouped per host.

    Ctrl-C stops a parallel run the way it stops a sequential one: hosts not
    yet started are never started, results that already finished are still
    printed, the hosts that were mid-command are listed (their state is
    unknown), and the KeyboardInterrupt is re-raised at once. The worker
    threads are daemon threads, so an abandoned remote command (e.g. a long
    disk scan) can't keep the process alive. label(item) names hosts in that
    report (default: the CSV row's ip).

    worker must catch its own exceptions and return a result object.
    """
    parallel = max(1, int(parallel or 1))
    label = label or _host_label
    results = []
    if parallel == 1 or len(items) <= 1:
        for i, item in enumerate(items, 1):
            if on_start:
                on_start(i, item)
            result = worker(item)
            results.append(result)
            if on_done:
                on_done(i, item, result)
        return results

    n = len(items)
    results = [None] * n
    errors = [None] * n
    started = [False] * n
    finished = [threading.Event() for _ in range(n)]
    lock = threading.Lock()
    stop = threading.Event()
    next_index = [0]

    def runner():
        while True:
            with lock:
                if stop.is_set() or next_index[0] >= n:
                    return
                i = next_index[0]
                next_index[0] += 1
                started[i] = True
            try:
                results[i] = worker(items[i])
            except BaseException as e:      # re-raised in the calling thread
                errors[i] = e
            finished[i].set()

    for _ in range(min(parallel, n)):
        threading.Thread(target=runner, name='run_hosts', daemon=True).start()

    shown = 0           # items whose result has been handed to on_start/on_done
    try:
        for i in range(n):
            # Short waits keep Ctrl-C responsive (a blocking wait isn't
            # interruptible on Windows).
            while not finished[i].wait(0.2):
                pass
            if errors[i] is not None:
                raise errors[i]
            shown = i + 1
            if on_start:
                on_start(i + 1, items[i])
            if on_done:
                on_done(i + 1, items[i], results[i])
    except BaseException as exc:
        with lock:
            stop.set()                      # no new hosts from here on
            was_started = list(started)
        reason = 'Interrupted' if isinstance(exc, KeyboardInterrupt) else f'Stopped ({type(exc).__name__})'
        _report_abort(items, shown, was_started, finished, errors, results,
                      on_start, on_done, label, reason)
        raise
    return results


def _report_abort(items, shown, started, finished, errors, results, on_start, on_done, label,
                  reason):
    """After Ctrl-C (or a worker crash) in a parallel run_hosts: say what happened to each host."""
    running, not_started = [], []
    print('', flush=True)
    for i in range(shown, len(items)):
        if not started[i]:
            not_started.append(label(items[i]))
        elif finished[i].is_set() and errors[i] is None:
            # Finished out of order while an earlier host was still running.
            if on_start:
                on_start(i + 1, items[i])
            if on_done:
                on_done(i + 1, items[i], results[i])
        elif not finished[i].is_set():
            running.append(label(items[i]))
    print(f"[!] {reason}.", flush=True)
    if running:
        print(f"    Stopped while running (state unknown, check these): {', '.join(running)}", flush=True)
    if not_started:
        print(f"    Not started: {', '.join(not_started)}", flush=True)


def positive_int(text: str) -> int:
    """argparse type for --parallel."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError('must be >= 1')
    return value
