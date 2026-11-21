"""
Windows sources of the 'sessions' message, through ctypes only (no
pywin32). sessions.py imports this module only on Windows.

  wts         WTSEnumerateSessionsW, then WTSQuerySessionInformationW per
              session: user, domain, client name and address, protocol and
              logon time. Session 0 (services), sessions without a user and
              the RDP listener are skipped. The client address is what the
              client reports; behind an RD Gateway there is none.
  netsession  NetSessionEnum level 10: SMB sessions (client, user, seconds
              connected). Levels 1, 2 and 502 need more rights and give
              nothing v1 uses. Running as SYSTEM (BUILTIN\\Administrators in
              its token) is enough; a user-logon install without elevation
              gets ERROR_ACCESS_DENIED -> 'not_permitted'.

quser / qwinsta are never parsed: their output is localized.

Every buffer the APIs return is freed (WTSFreeMemory, NetApiBufferFree),
also on ERROR_MORE_DATA. Strings are read as UTF-16 from raw memory, so the
parsing helpers behave the same on any OS (the unit tests run them on Linux
against fake DLLs).
"""
import ctypes
import logging
import struct
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DWORD = ctypes.c_uint32
WCHAR = ctypes.c_uint16          # UTF-16 code unit (ctypes.c_wchar is 4 bytes off Windows)

ERROR_ACCESS_DENIED = 5
ERROR_MORE_DATA = 234
NERR_SUCCESS = 0
MAX_PREFERRED_LENGTH = 0xFFFFFFFF

# WTS_INFO_CLASS values used
WTS_USER_NAME, WTS_DOMAIN_NAME, WTS_CLIENT_NAME = 5, 7, 10
WTS_CLIENT_ADDRESS_CLASS, WTS_CLIENT_PROTOCOL_TYPE, WTS_SESSION_INFO = 14, 16, 24

# WTS_CONNECTSTATE_CLASS
WTS_ACTIVE, WTS_CONNECTED, WTS_CONNECT_QUERY, WTS_SHADOW, WTS_DISCONNECTED = 0, 1, 2, 3, 4
WTS_IDLE, WTS_LISTEN, WTS_RESET, WTS_DOWN, WTS_INIT = 5, 6, 7, 8, 9
STATES = {WTS_DISCONNECTED: 'disconnected'}          # every other user session is 'active'

# WTSClientProtocolType: 0 console, 1 ICA (Citrix), 2 RDP
PROTOCOLS = {0: 'console', 1: 'ica', 2: 'rdp'}

AF_INET, AF_INET6 = 2, 23
FILETIME_EPOCH_OFFSET = 116444736000000000           # 1601-01-01 -> 1970-01-01 in 100 ns
WSTR_MAX_CHARS = 1024


class WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [('SessionId', DWORD), ('pWinStationName', ctypes.c_void_p), ('State', ctypes.c_int)]


class WTS_CLIENT_ADDRESS(ctypes.Structure):
    _fields_ = [('AddressFamily', DWORD), ('Address', ctypes.c_ubyte * 20)]


class WTSINFOW(ctypes.Structure):
    _fields_ = [('State', ctypes.c_int), ('SessionId', DWORD),
                ('IncomingBytes', DWORD), ('OutgoingBytes', DWORD),
                ('IncomingFrames', DWORD), ('OutgoingFrames', DWORD),
                ('IncomingCompressedBytes', DWORD), ('OutgoingCompressedBytes', DWORD),
                ('WinStationName', WCHAR * 32), ('Domain', WCHAR * 17), ('UserName', WCHAR * 21),
                ('ConnectTime', ctypes.c_longlong), ('DisconnectTime', ctypes.c_longlong),
                ('LastInputTime', ctypes.c_longlong), ('LogonTime', ctypes.c_longlong),
                ('CurrentTime', ctypes.c_longlong)]


class SESSION_INFO_10(ctypes.Structure):
    """lmshare.h order: sesi10_cname, sesi10_username, sesi10_time, sesi10_idle_time."""
    _fields_ = [('sesi10_cname', ctypes.c_void_p), ('sesi10_username', ctypes.c_void_p),
                ('sesi10_time', DWORD), ('sesi10_idle_time', DWORD)]


class WinApiError(OSError):
    """A failed Win32 call; code is its GetLastError() value (OSError.winerror exists only on Windows)."""

    def __init__(self, code: int, what: str):
        super().__init__(f"{what} failed (error {code})")
        self.code = code


def _last_error() -> int:
    get = getattr(ctypes, 'get_last_error', None)
    return get() if get is not None else 0


def _load(name: str):
    """ctypes.WinDLL(name), or AttributeError/OSError off Windows."""
    return ctypes.WinDLL(name, use_last_error=True)


def filetime_to_unix(ticks: int) -> Optional[float]:
    """Unix time of a FILETIME (100 ns since 1601-01-01 UTC), or None for 0 / out of range."""
    if not ticks or ticks <= FILETIME_EPOCH_OFFSET:
        return None
    return (ticks - FILETIME_EPOCH_OFFSET) / 1e7


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (OverflowError, OSError, ValueError):
        return None


def utf16(raw: bytes) -> str:
    """A NUL-terminated UTF-16LE string from raw bytes."""
    text = raw.decode('utf-16-le', 'replace')
    return text.split('\0', 1)[0].strip()


def _wstr_at(address: Optional[int]) -> str:
    """The NUL-terminated UTF-16 string at a memory address ('' for NULL)."""
    if not address:
        return ''
    raw = bytearray()
    for i in range(WSTR_MAX_CHARS):
        unit = ctypes.string_at(address + 2 * i, 2)
        if unit == b'\0\0':
            break
        raw += unit
    return utf16(bytes(raw))


def client_address(raw: bytes) -> Optional[str]:
    """
    The IP from a WTS_CLIENT_ADDRESS (DWORD family, BYTE Address[20]). For
    AF_INET the four address bytes start at Address[2]; AF_INET6 is taken
    as 16 raw bytes from Address[0]. None for anything else or all zeros.
    """
    import ipaddress
    if len(raw) < 24:
        return None
    family = struct.unpack_from('<I', raw, 0)[0]
    address = raw[4:24]
    try:
        if family == AF_INET and any(address[2:6]):
            return str(ipaddress.IPv4Address(bytes(address[2:6])))
        if family == AF_INET6 and any(address[:16]):
            ip = ipaddress.IPv6Address(bytes(address[:16]))
            return str(ip.ipv4_mapped or ip)
    except ValueError:
        return None
    return None


def strip_unc(name: str) -> str:
    """'\\\\10.0.0.5' -> '10.0.0.5' (NetSessionEnum client names), '[fe80::1]' -> 'fe80::1'."""
    return name.lstrip('\\').strip('[]').strip()


# ---------------------------------------------------------------------------
# WTS
# ---------------------------------------------------------------------------

def _wts_query(dll, session_id: int, info_class: int) -> bytes:
    """Raw bytes of one WTSQuerySessionInformationW answer; WinApiError on failure."""
    buf, size = ctypes.c_void_p(), DWORD()
    if not dll.WTSQuerySessionInformationW(ctypes.c_void_p(None), DWORD(session_id), info_class,
                                           ctypes.byref(buf), ctypes.byref(size)):
        raise WinApiError(_last_error(), f"WTSQuerySessionInformationW({info_class})")
    try:
        return ctypes.string_at(buf.value, size.value) if buf.value and size.value else b''
    finally:
        if buf.value:
            dll.WTSFreeMemory(buf)


def _wts_sessions(dll) -> List[Tuple[int, int]]:
    """[(session id, state)]; WinApiError when the sessions can't be listed."""
    info, count = ctypes.c_void_p(), DWORD()
    if not dll.WTSEnumerateSessionsW(ctypes.c_void_p(None), DWORD(0), DWORD(1),
                                     ctypes.byref(info), ctypes.byref(count)):
        raise WinApiError(_last_error(), "WTSEnumerateSessionsW")
    try:
        if not info.value or not count.value:
            return []
        array = ctypes.cast(info, ctypes.POINTER(WTS_SESSION_INFOW * count.value)).contents
        return [(int(s.SessionId), int(s.State)) for s in array]
    finally:
        if info.value:
            dll.WTSFreeMemory(info)


def _wts_row(dll, session_id: int, state: int) -> Optional[dict]:
    user = utf16(_wts_query(dll, session_id, WTS_USER_NAME))
    if not user:
        return None
    domain = utf16(_wts_query(dll, session_id, WTS_DOMAIN_NAME))
    raw_protocol = _wts_query(dll, session_id, WTS_CLIENT_PROTOCOL_TYPE)
    protocol = struct.unpack_from('<H', raw_protocol)[0] if len(raw_protocol) >= 2 else 0
    row = {'service': PROTOCOLS.get(protocol, 'rdp'),
           'username': f'{domain}\\{user}' if domain else user,
           'state': STATES.get(state, 'active'), 'source': 'wts'}
    if protocol != 0:
        client = client_address(_wts_query(dll, session_id, WTS_CLIENT_ADDRESS_CLASS))
        host = utf16(_wts_query(dll, session_id, WTS_CLIENT_NAME))
        if client:
            row['client'] = client
        if host and host != client:
            row['client_host'] = host
    raw_info = _wts_query(dll, session_id, WTS_SESSION_INFO)
    if len(raw_info) >= ctypes.sizeof(WTSINFOW):
        since = _iso(filetime_to_unix(WTSINFOW.from_buffer_copy(raw_info).LogonTime))
        if since:
            row['since'] = since
    return row


def read_wts(dll=None, log: Optional[logging.Logger] = None):
    """
    (status, rows): 'ok'; 'partial' when some sessions couldn't be read
    (access denied: a user-logon install); 'not_permitted' or 'error' when
    the sessions can't be listed; 'unavailable' off Windows.
    """
    log = log or logger
    try:
        dll = dll or _load('wtsapi32')
    except (AttributeError, OSError) as e:
        log.debug(f"WTS API unavailable: {e}")
        return 'unavailable', None
    try:
        sessions = _wts_sessions(dll)
    except WinApiError as e:
        if e.code == ERROR_ACCESS_DENIED:
            return 'not_permitted', None
        log.warning(f"Sessions: cannot list Windows sessions ({e})")
        return 'error', None
    rows, skipped = [], 0
    for session_id, state in sessions:
        if session_id == 0 or state == WTS_LISTEN:
            continue
        try:
            row = _wts_row(dll, session_id, state)
        except OSError as e:
            skipped += 1
            log.debug(f"Session {session_id}: {e}")
            continue
        if row is not None:
            rows.append(row)
    return ('partial' if skipped else 'ok'), rows


# ---------------------------------------------------------------------------
# NetSessionEnum (SMB sessions)
# ---------------------------------------------------------------------------

def read_netsessions(dll=None, now: Optional[float] = None, log: Optional[logging.Logger] = None):
    """(status, rows) from NetSessionEnum level 10: 'ok', 'not_permitted', 'error' or 'unavailable'."""
    import time
    log = log or logger
    now = time.time() if now is None else now
    try:
        dll = dll or _load('netapi32')
    except (AttributeError, OSError) as e:
        log.debug(f"NetSessionEnum unavailable: {e}")
        return 'unavailable', None
    rows = []
    resume = DWORD(0)
    for _ in range(1000):                    # each call returns a batch; ERROR_MORE_DATA = more to come
        buf, read, total = ctypes.c_void_p(), DWORD(), DWORD()
        status = dll.NetSessionEnum(None, None, None, DWORD(10), ctypes.byref(buf), DWORD(MAX_PREFERRED_LENGTH),
                                    ctypes.byref(read), ctypes.byref(total), ctypes.byref(resume))
        try:
            if status in (NERR_SUCCESS, ERROR_MORE_DATA) and buf.value and read.value:
                array = ctypes.cast(buf, ctypes.POINTER(SESSION_INFO_10 * read.value)).contents
                for s in array:
                    row = _netsession_row(s, now)
                    if row is not None:
                        rows.append(row)
        finally:
            if buf.value:
                dll.NetApiBufferFree(buf)
        if status == ERROR_MORE_DATA:
            continue
        if status == NERR_SUCCESS:
            return 'ok', rows
        if status == ERROR_ACCESS_DENIED:
            return 'not_permitted', None
        log.warning(f"Sessions: NetSessionEnum failed with {status}")
        return 'error', None
    return 'partial', rows


def _netsession_row(info: SESSION_INFO_10, now: float) -> Optional[dict]:
    import ipaddress
    user = _wstr_at(info.sesi10_username)
    if not user:
        return None
    cname = strip_unc(_wstr_at(info.sesi10_cname))
    row = {'service': 'smb', 'username': user, 'source': 'netsession'}
    try:
        ip = ipaddress.ip_address(cname.split('%', 1)[0])
        row['client'] = str(getattr(ip, 'ipv4_mapped', None) or ip)
    except ValueError:
        if cname:
            row['client_host'] = cname
    since = _iso(now - int(info.sesi10_time))
    if since:
        row['since'] = since
    return row


def read_all(now: float, log: Optional[logging.Logger] = None) -> dict:
    """{'wts': (status, rows), 'netsession': (status, rows)}."""
    return {'wts': read_wts(log=log), 'netsession': read_netsessions(now=now, log=log)}
