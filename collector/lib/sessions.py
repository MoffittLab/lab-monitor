"""
Who is connected: the 'sessions' message of each metrics run (contract
item 13).

Client counts per service come from the TCP table, the same way on every
platform. A service is counted only while this host has a LISTEN socket on
one of its ports (DEFAULT_SERVICE_PORTS, or config sessions.ports /
sessions.extra_ports), so a missing <svc>_clients key means the service is
not running and 0 means it runs with no clients. Only ESTABLISHED
connections to such a port count; outbound connections never match (their
local port is ephemeral).

  Linux/DSM  /proc/net/tcp and /proc/net/tcp6 (mode 0444, not hidden by
             hidepid). psutil.net_connections is not used there: it walks
             every /proc/*/fd.
  Windows    psutil.net_connections('inet'); without psutil, no TCP data.

Named users come from the user-level sources:
  utmp        Linux/DSM ssh and console logins (/run/utmp)
  logind      Linux without a utmp file (Ubuntu 25.10+): /run/systemd/sessions
  smb         smbstatus -b, only when the collector runs as root (smbstatus
              refuses other users since Samba 4.1.14): 'not_permitted'
              otherwise. sudo is never used, and plain smbstatus (which
              lists open file names) never runs.
  wts, netsession  Windows (sessions_win.py): console and RDP sessions, and
              SMB sessions (NetSessionEnum level 10)

Not in this release (extension points, see _user_sources): the Synology
Services MIB over SNMP (per-service user counts without names, contract
source 'snmp') and a privileged helper's smb.json (SMB usernames on DSM
without running the collector as root).

The list is sent in full only when it changed, on the first run, every
sessions.list_heartbeat_seconds, or every run with sessions.history
"every_run"; otherwise '_list_state' is 'unchanged' and the counts alone
are sent. Privacy (contract R9): usernames, client IPs and the host names
the protocols themselves report; never process names, PIDs, share or file
names, and nothing from DNS.
"""
import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import platform_info, settings

logger = logging.getLogger(__name__)

STATE_FILE = 'sessions_state.json'
SCHEMA_VERSION = 1

# Services and their ports, by platform. DSM's 5000/5001 are left out on
# Linux and Windows, where the Manager or Dashboard may listen on 5000.
DEFAULT_SERVICE_PORTS = {
    'synology': {'smb': [445, 139], 'nfs': [2049], 'afp': [548], 'ssh': [22], 'dsm': [5000, 5001],
                 'drive': [6690], 'rsync': [873], 'iscsi': [3260], 'ftp': [21], 'webdav': [5005, 5006]},
    'windows':  {'smb': [445], 'rdp': [3389], 'ssh': [22]},
    'linux':    {'ssh': [22], 'smb': [445, 139], 'nfs': [2049], 'rdp': [3389]},
}

PROC_NET = '/proc/net'
UTMP_PATHS = ('/var/run/utmp', '/run/utmp')
LOGIND_SESSIONS = '/run/systemd/sessions'
SMBSTATUS_TIMEOUT = 15

TCP_ESTABLISHED, TCP_LISTEN = '01', '0A'

# struct utmp (glibc, 384 bytes): ut_type, pid, line, id, user, host,
# exit status, session, tv_sec, tv_usec, ut_addr_v6 (raw bytes, network order)
UTMP_FORMAT = '=h2xi32s4s32s256shhiii16s20x'
UTMP_RECORD = struct.calcsize(UTMP_FORMAT)
USER_PROCESS = 7

# The services each user-level source can count users of (<svc>_users)
SOURCE_SERVICES = {
    'utmp':       ('ssh', 'console'),
    'logind':     ('ssh', 'console'),
    'smb':        ('smb',),
    'wts':        ('console', 'rdp'),
    'netsession': ('smb',),
}
_SUCCEEDED = ('ok', 'partial')

USERNAME_MAX_CHARS = 256
HOST_MAX_CHARS = 256

# smbstatus -b rows: PID, Username, Group (may contain spaces), Machine, then
# "(ipv4:10.0.0.5:51234)"; the machine name is the last word before "("
_SMBSTATUS_ROW = re.compile(r'^\s*(\d+)\s+(\S+)\s+(.*?)\s*\((ipv[46]):(.+):(\d+)\)')


def _wall_time() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    """ISO-8601 UTC ('2026-11-21T12:35:00Z') of a Unix time, or None for an unset (<= 0) one."""
    if ts is None or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(text) -> Optional[float]:
    try:
        return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def normalize_ip(text) -> Optional[str]:
    """
    An IP address as text, IPv4-mapped IPv6 as IPv4 and without a zone id
    ('::ffff:10.0.0.5' -> '10.0.0.5', 'fe80::1%eth0' -> 'fe80::1'), or None
    when it isn't one.
    """
    if text is None:
        return None
    text = str(text).strip().strip('[]').split('%', 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Options:
    """The sessions.* settings for one run (each read once, so a bad value warns once)."""

    def __init__(self, config: dict, platform: str, log: logging.Logger):
        def value(key):
            return settings.value(config, f'sessions.{key}', log)
        self.platform = platform
        self.detail = value('detail')
        self.history = value('history')
        self.heartbeat = value('list_heartbeat_seconds')
        self.max_entries = value('max_entries')
        ports = value('ports')
        self.services = dict(DEFAULT_SERVICE_PORTS.get(platform, DEFAULT_SERVICE_PORTS['linux'])
                             if ports is None else ports)
        self.services.update(value('extra_ports'))
        self.exclude_networks = [ipaddress.ip_network(c.strip(), strict=False)
                                 for c in value('exclude_clients')]
        # Windows machine accounts (HOST$) open SMB sessions too
        self.user_patterns = [p.lower() for p in settings.exclude_user_patterns(config) + ['*$']]
        self._excluded = {}

    def client_excluded(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr.version == net.version and addr in net for net in self.exclude_networks)

    def user_excluded(self, username: str) -> bool:
        excluded = self._excluded.get(username)
        if excluded is None:
            lower = username.lower()
            excluded = self._excluded[username] = any(fnmatch.fnmatch(lower, p) for p in self.user_patterns)
        return excluded


# ---------------------------------------------------------------------------
# TCP
# ---------------------------------------------------------------------------

def _proc_net_addr(text: str, byteorder: str) -> Tuple[Optional[str], int]:
    """
    ('10.0.0.5', 445) from /proc/net/tcp's 'ADDR:PORT'. ADDR is 8 hex digits
    per 32-bit word, each word the in-memory (native-endian) value; the port
    is plain hex.
    """
    addr, _, port = text.partition(':')
    if len(addr) not in (8, 32) or not port:
        raise ValueError(text)
    raw = b''.join(int(addr[i:i + 8], 16).to_bytes(4, byteorder) for i in range(0, len(addr), 8))
    return normalize_ip(str(ipaddress.ip_address(raw))), int(port, 16)


def parse_proc_net_tcp(text: str, byteorder: str = sys.byteorder):
    """
    (listen_ports, connections) from the text of /proc/net/tcp or tcp6:
    the local ports in LISTEN state, and (local_port, remote_ip, local_ip)
    of every ESTABLISHED connection. Malformed lines are skipped.
    """
    listen, conns = set(), []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] not in (TCP_ESTABLISHED, TCP_LISTEN):
            continue
        try:
            local_ip, local_port = _proc_net_addr(fields[1], byteorder)
            remote_ip, _ = _proc_net_addr(fields[2], byteorder)
        except ValueError:
            continue
        if fields[3] == TCP_LISTEN:
            listen.add(local_port)
        elif remote_ip is not None:
            conns.append((local_port, remote_ip, local_ip))
    return listen, conns


def read_proc_net_tcp(proc_net: Optional[str] = None, log: Optional[logging.Logger] = None):
    """('ok' | 'unavailable', listen_ports, connections) from /proc/net/tcp and tcp6 (a missing tcp6 is fine)."""
    log = log or logger
    proc_net = proc_net or PROC_NET
    listen, conns, read_any = set(), [], False
    for name in ('tcp', 'tcp6'):
        try:
            with open(os.path.join(proc_net, name), 'r', encoding='ascii', errors='replace') as f:
                text = f.read()
        except OSError as e:
            log.debug(f"Cannot read {os.path.join(proc_net, name)}: {e}")
            continue
        read_any = True
        more_listen, more_conns = parse_proc_net_tcp(text)
        listen |= more_listen
        conns += more_conns
    return ('ok' if read_any else 'unavailable'), listen, conns


def read_psutil_tcp(log: Optional[logging.Logger] = None):
    """('ok' | 'not_permitted' | 'unavailable' | 'error', listen_ports, connections) via psutil (Windows)."""
    log = log or logger
    try:
        import psutil
    except ImportError:
        log.debug("psutil is not installed: no TCP table for sessions")
        return 'unavailable', set(), []
    try:
        table = psutil.net_connections('inet')
    except getattr(psutil, 'AccessDenied', PermissionError) as e:
        log.debug(f"psutil.net_connections: access denied ({e})")
        return 'not_permitted', set(), []
    except Exception as e:
        log.warning(f"Sessions: cannot read the TCP table ({e})")
        return 'error', set(), []
    listen, conns = set(), []
    for c in table:
        if getattr(c, 'type', socket.SOCK_STREAM) != socket.SOCK_STREAM or not c.laddr:
            continue
        status = str(getattr(c, 'status', '')).upper()
        if status == 'LISTEN':
            listen.add(c.laddr[1])
        elif status == 'ESTABLISHED' and c.raddr:
            remote = normalize_ip(c.raddr[0])
            if remote is not None:
                conns.append((c.laddr[1], remote, normalize_ip(c.laddr[0])))
    return 'ok', listen, conns


def count_clients(opts: Options, listen: set, conns: list):
    """
    ({service: number of distinct client IPs} for every listening monitored
    service, the distinct client IPs of all of them, Counter{(service, ip):
    connections}). Remote loopback, self-connections and exclude_clients
    are left out.
    """
    port_service = {}
    for service in sorted(opts.services):
        for port in opts.services[service]:
            if port in listen:
                port_service.setdefault(port, service)
    per_service = {service: set() for service in sorted(set(port_service.values()))}
    per_client = Counter()
    for local_port, remote_ip, local_ip in conns:
        service = port_service.get(local_port)
        if service is None or remote_ip == local_ip or _is_loopback(remote_ip) or opts.client_excluded(remote_ip):
            continue
        per_service[service].add(remote_ip)
        per_client[(service, remote_ip)] += 1
    every = set().union(*per_service.values()) if per_service else set()
    return {s: len(ips) for s, ips in per_service.items()}, every, per_client


# ---------------------------------------------------------------------------
# utmp and logind (Linux, DSM)
# ---------------------------------------------------------------------------

def _cstr(raw: bytes) -> str:
    return raw.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()


def _pid_alive(pid: int) -> bool:
    """Whether a utmp record's process still exists (stale records outlive a crashed login)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _utmp_addr(raw: bytes) -> Optional[str]:
    """ut_addr_v6: IPv4 in the first 4 bytes (the rest zero), else IPv6; None when all zero."""
    if not any(raw):
        return None
    if not any(raw[4:]):
        return normalize_ip(str(ipaddress.IPv4Address(raw[:4])))
    return normalize_ip(str(ipaddress.IPv6Address(raw)))


def _host_fields(host: str, client: Optional[str]) -> dict:
    """client / client_host from a source's host text: an IP is a client, anything else a host name."""
    fields = {}
    ip = normalize_ip(host) if host else None
    if client is None and ip is not None:
        client = ip
    if client is not None:
        fields['client'] = client
    if host and ip is None:
        fields['client_host'] = host[:HOST_MAX_CHARS]
    return fields


def parse_utmp(data: bytes, alive=_pid_alive) -> Optional[List[dict]]:
    """
    Session rows from utmp records: USER_PROCESS entries whose process is
    alive. A pts/* line with a remote host (not empty, not ':0') is ssh,
    with the client from ut_addr_v6 (or the host when it is an IP) and a
    host name in client_host; anything else is console. None when data
    isn't whole records (another layout, or a truncated file).
    """
    if len(data) % UTMP_RECORD:
        return None
    rows = []
    for offset in range(0, len(data), UTMP_RECORD):
        (ut_type, pid, line, _id, user, host, _term, _exit, _session,
         tv_sec, _tv_usec, addr) = struct.unpack_from(UTMP_FORMAT, data, offset)
        if ut_type != USER_PROCESS:
            continue
        user, line, host = _cstr(user), _cstr(line), _cstr(host)
        if not user or not alive(pid):
            continue
        ssh = line.startswith('pts/') and bool(host) and not host.startswith(':')
        row = {'service': 'ssh' if ssh else 'console', 'username': user, 'source': 'utmp'}
        if ssh:     # a console login has no client (gdm writes "login screen" as its host)
            row.update(_host_fields(host, _utmp_addr(addr)))
        since = _iso(tv_sec)
        if since:
            row['since'] = since
        rows.append(row)
    return rows


def read_utmp(paths=None, log: Optional[logging.Logger] = None):
    """('ok', rows) from the first utmp file that exists, ('unavailable', None) with none, or 'error'."""
    log = log or logger
    for path in paths or UTMP_PATHS:
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            continue
        except OSError as e:
            log.debug(f"Cannot read {path}: {e}")
            return ('not_permitted' if isinstance(e, PermissionError) else 'unavailable'), None
        rows = parse_utmp(data)
        if rows is None:
            log.warning(f"Sessions: {path} is not a whole number of {UTMP_RECORD}-byte utmp records; "
                        f"ssh and console users are not reported")
            return 'unavailable', None
        return 'ok', rows
    return 'unavailable', None


def utmp_exists(paths=None) -> bool:
    return any(os.path.lexists(p) for p in paths or UTMP_PATHS)


def parse_logind_session(text: str) -> Optional[dict]:
    """
    A row from one /run/systemd/sessions/<id> file (KEY=value lines), or
    None. systemd calls the format private, so every key is optional: no
    USER, or a session that isn't a user's (CLASS manager, greeter, ...) or
    is closing, gives None.
    """
    keys = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep and key and not key.startswith('#'):
            keys[key.strip()] = value.strip()
    user = keys.get('USER')
    if not user or keys.get('STATE') == 'closing':
        return None
    if keys.get('CLASS') and not keys['CLASS'].startswith('user'):
        return None
    row = {'service': 'ssh' if keys.get('SERVICE') == 'sshd' else 'console', 'username': user,
           'source': 'logind'}
    row.update(_host_fields(keys.get('REMOTE_HOST', ''), None))
    try:
        since = _iso(int(keys['REALTIME']) / 1e6)       # microseconds since the epoch
    except (KeyError, ValueError):
        since = None
    if since:
        row['since'] = since
    return row


def read_logind(directory: Optional[str] = None, log: Optional[logging.Logger] = None):
    """('ok', rows) from logind's session files; ('unavailable', None) when the directory is missing."""
    log = log or logger
    directory = directory or LOGIND_SESSIONS
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        log.debug(f"Cannot list {directory}: {e}")
        return 'unavailable', None
    rows = []
    for name in names:
        path = os.path.join(directory, name)
        if '.' in name or not os.path.isfile(path):       # <id>.ref is a FIFO
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                row = parse_logind_session(f.read())
        except (OSError, ValueError) as e:
            log.debug(f"Skipping {path}: {e}")
            continue
        if row is not None:
            rows.append(row)
    return 'ok', rows


# ---------------------------------------------------------------------------
# SMB usernames (Linux, DSM): smbstatus -b, as root only
# ---------------------------------------------------------------------------

def parse_smbstatus_brief(text: str) -> List[dict]:
    """Rows from `smbstatus -b` text (Samba 4.x; --json only exists from 4.17)."""
    rows = []
    for line in text.splitlines():
        m = _SMBSTATUS_ROW.match(line)
        if not m:
            continue
        machine = m.group(3).split()[-1] if m.group(3).split() else ''
        client = normalize_ip(m.group(5))
        row = {'service': 'smb', 'username': m.group(2), 'source': 'smb'}
        if client is not None:
            row['client'] = client
        if machine and normalize_ip(machine) is None:
            row['client_host'] = machine[:HOST_MAX_CHARS]
        rows.append(row)
    return rows


def read_smbstatus(run=None, which=None, log: Optional[logging.Logger] = None):
    """
    ('ok', rows), or (status, None): 'not_permitted' unless running as root
    (smbstatus refuses other users; sudo is never tried), 'unavailable'
    when smbstatus isn't installed, 'timeout' or 'error'.
    """
    log = log or logger
    if not platform_info.is_root():
        return 'not_permitted', None
    exe = (which or shutil.which)('smbstatus')
    if not exe:
        return 'unavailable', None
    try:
        proc = (run or subprocess.run)([exe, '-b'], capture_output=True, text=True,
                                       timeout=SMBSTATUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning(f"Sessions: smbstatus -b took longer than {SMBSTATUS_TIMEOUT}s")
        return 'timeout', None
    except OSError as e:
        log.warning(f"Sessions: cannot run smbstatus ({e})")
        return 'error', None
    output = (proc.stdout or '') + (proc.stderr or '')
    if 'only works as root' in output.lower():
        return 'not_permitted', None
    if proc.returncode != 0:
        log.warning(f"Sessions: smbstatus -b exited with {proc.returncode}: {output.strip()[:200]}")
        return 'error', None
    return 'ok', parse_smbstatus_brief(proc.stdout or '')


# ---------------------------------------------------------------------------
# Merge, counts and the list
# ---------------------------------------------------------------------------

def _sort_key(row: dict):
    return ('username' not in row, row.get('since') is None, row.get('since') or '',
            row['service'], row.get('client') or '', row.get('username') or '')


def _fold(rows: List[dict], key_fields: Tuple[str, ...]) -> List[dict]:
    """Rows aggregated on key_fields: connections summed, earliest since, 'active' over 'disconnected'."""
    folded = {}
    for row in rows:
        key = tuple(row.get(k) for k in key_fields)
        into = folded.get(key)
        if into is None:
            folded[key] = dict(row, connections=row.get('connections', 1))
            continue
        into['connections'] += row.get('connections', 1)
        if row.get('since') and (not into.get('since') or row['since'] < into['since']):
            into['since'] = row['since']
        if row.get('client_host') and not into.get('client_host'):
            into['client_host'] = row['client_host']
        if row.get('state') == 'active':
            into['state'] = 'active'
    return list(folded.values())


def merge_and_aggregate(user_rows: List[dict], tcp_connections: Counter, detail: str,
                        max_entries: int, user_excluded=lambda name: False):
    """
    (list, truncated) for _detail full or users:
      1. user-level rows aggregated on (service, username, client);
      2. one 'tcp' row per (service, client) no user-level row covers (an
         excluded user's row still covers its client, so it doesn't come
         back as an anonymous TCP row);
      3. users first, then oldest since, then service, then client;
      4. 'users': client and client_host dropped, aggregated on (service,
         username), TCP-only rows dropped;
      5. cut to max_entries.
    """
    rows = _fold(user_rows, ('service', 'username', 'client'))
    covered = {(r['service'], r.get('client')) for r in rows if r.get('client')}
    rows = [r for r in rows if not user_excluded(r['username'])]
    if detail == 'users':
        rows = _fold([{k: v for k, v in r.items() if k not in ('client', 'client_host')} for r in rows],
                     ('service', 'username'))
    else:
        rows += [{'service': service, 'client': ip, 'connections': n, 'source': 'tcp'}
                 for (service, ip), n in sorted(tcp_connections.items()) if (service, ip) not in covered]
    rows.sort(key=_sort_key)
    return rows[:max_entries], len(rows) > max_entries


def list_hash(rows: List[dict]) -> str:
    """16 hex digits over the sorted (service, username, client, client_host, state); since and connections don't count."""
    items = sorted((r.get('service') or '', r.get('username') or '', r.get('client') or '',
                    r.get('client_host') or '', r.get('state') or '') for r in rows)
    return hashlib.sha1(json.dumps(items, separators=(',', ':')).encode('utf-8')).hexdigest()[:16]


def _plain_name(username: str, windows: bool) -> str:
    """The name users_total counts: without a DOMAIN\\ prefix, case-folded on Windows."""
    name = username.rsplit('\\', 1)[-1]
    return name.lower() if windows else name


def user_counts(rows: List[dict], sources: Dict[str, str], windows: bool) -> dict:
    """
    users_total and <svc>_users (plus rdp_disconnected on Windows) from the
    user-level rows, each present only when a source for it succeeded;
    users_total is None when none did. <svc>_users counts the users with a
    session that isn't disconnected; rdp_disconnected the disconnected RDP
    sessions.
    """
    succeeded = [s for s in SOURCE_SERVICES if sources.get(s) in _SUCCEEDED]
    counts = {'users_total': len({_plain_name(r['username'], windows) for r in rows})
              if succeeded else None}
    services = sorted({svc for s in succeeded for svc in SOURCE_SERVICES[s]})
    for service in services:
        names = {r['username'] for r in rows if r['service'] == service and r.get('state') != 'disconnected'}
        counts[f'{service}_users'] = len(names)
    if sources.get('wts') in _SUCCEEDED:
        counts['rdp_disconnected'] = sum(1 for r in rows if r['service'] == 'rdp'
                                         and r.get('state') == 'disconnected')
    return counts


def _clean_user_rows(rows: List[dict], opts: Options) -> List[dict]:
    """Excluded clients dropped, a loopback client cleared (the login itself is kept), R10 lengths."""
    kept = []
    for row in rows:
        client = row.get('client')
        if client is not None:
            if opts.client_excluded(client):
                continue
            if _is_loopback(client):
                row = {k: v for k, v in row.items() if k != 'client'}
        row = dict(row, username=row['username'][:USERNAME_MAX_CHARS])
        kept.append(row)
    return kept


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

def _user_sources(opts: Options, smb_listening: bool, log: logging.Logger):
    """({source: status}, rows) from the platform's user-level sources."""
    sources, rows = {}, []
    if opts.platform == 'windows':
        from . import sessions_win     # imported only on Windows
        for source, (status, found) in sessions_win.read_all(_wall_time(), log).items():
            sources[source] = status
            rows += found or []
        return sources, rows
    if utmp_exists():
        sources['utmp'], found = read_utmp(log=log)
    else:
        sources['logind'], found = read_logind(log=log)
    rows += found or []
    # Future option: SMB usernames from a privileged helper's smb.json, and
    # DSM's per-service user counts (Synology Services MIB) over SNMP
    if 'smb' in opts.services and smb_listening:
        sources['smb'], found = read_smbstatus(log=log)
        rows += found or []
    return sources, rows


def _base(opts: Options) -> dict:
    return {'_schema': SCHEMA_VERSION, '_unit': 'count', '_detail': opts.detail}


def _platform_sources(platform: str) -> Tuple[str, ...]:
    return ('tcp', 'wts', 'netsession') if platform == 'windows' else ('tcp', 'utmp', 'smb')


def failure_payload(config: dict, status: str) -> dict:
    """The message when collect() raised ('error') or overran its budget ('timeout'): null counts."""
    platform = platform_info.platform_kind(config)
    detail = settings.value(config, 'sessions.detail')
    return {'_schema': SCHEMA_VERSION, '_unit': 'count', '_detail': detail,
            '_sources': {s: status for s in _platform_sources(platform)},
            '_list_state': 'none', '_list_hash': None, 'clients_total': None, 'users_total': None}


def _load_state(path: str) -> Optional[dict]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _list_state(opts: Options, rows: List[dict], previous: Optional[dict], now: float):
    """('full' | 'unchanged' | 'none', hash or None, the new state or None to keep the file)."""
    if opts.detail == 'counts':
        # No list now: the next list that is sent must be a full one
        if previous is not None and previous.get('hash') is None:
            return 'none', None, None
        return 'none', None, {'last_full_utc': None, 'hash': None}
    digest = list_hash(rows)
    last_full = _parse_iso((previous or {}).get('last_full_utc'))
    age = None if last_full is None else now - last_full
    if (opts.history == 'every_run' or previous is None or previous.get('hash') != digest
            or age is None or age < 0 or age >= opts.heartbeat):
        return 'full', digest, {'last_full_utc': _iso(now), 'hash': digest}
    return 'unchanged', digest, None


def collect(config: dict, data_dir: str, name: str, logger: Optional[logging.Logger] = None):
    """
    (payload, state) for this run: the 'sessions' message data and the new
    sessions_state.json ({last_full_utc, hash}), or None to leave it. Reads
    data/<name>/sessions_state.json but never writes a file (the collector
    writes the state once the message is recorded).
    """
    log = logger or logging.getLogger(__name__)
    started = time.monotonic()
    now = _wall_time()
    opts = Options(config, platform_info.platform_kind(config), log)

    if opts.platform == 'windows':
        tcp_status, listen, conns = read_psutil_tcp(log)
    else:
        tcp_status, listen, conns = read_proc_net_tcp(log=log)
    tcp_ok = tcp_status == 'ok'
    per_service, clients, tcp_rows = count_clients(opts, listen, conns) if tcp_ok else ({}, set(), Counter())
    smb_listening = not tcp_ok or 'smb' in per_service

    sources, user_rows = _user_sources(opts, smb_listening, log)
    sources = {'tcp': tcp_status, **sources}
    user_rows = _clean_user_rows(user_rows, opts)
    counted = [r for r in user_rows if not opts.user_excluded(r['username'])]

    payload = _base(opts)
    payload['_sources'] = sources
    payload['clients_total'] = len(clients) if tcp_ok else None
    payload.update(user_counts(counted, sources, opts.platform == 'windows'))
    for service, n in per_service.items():
        payload[f'{service}_clients'] = n

    rows = []
    if opts.detail != 'counts':
        rows, truncated = merge_and_aggregate(user_rows, tcp_rows, opts.detail, opts.max_entries,
                                              opts.user_excluded)
        payload['sessions_truncated'] = truncated
    previous = _load_state(os.path.join(data_dir, name, STATE_FILE))
    list_state, digest, state = _list_state(opts, rows, previous, now)
    payload['_list_state'] = list_state
    payload['_list_hash'] = digest
    if list_state == 'full':
        payload['sessions'] = rows
    payload['_collect_seconds'] = round(time.monotonic() - started, 3)

    users = payload['users_total']
    log.info(f"Sessions: {_count_text(payload['clients_total'], 'client')}, {_count_text(users, 'user')} "
             f"({', '.join(f'{s} {v}' for s, v in sources.items())}) {payload['_collect_seconds']:.2f}s")
    return payload, state


def _count_text(n: Optional[int], noun: str) -> str:
    if n is None:
        return f"{noun}s n/a"
    return f"{n} {noun}{'' if n == 1 else 's'}"
