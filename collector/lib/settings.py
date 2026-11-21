"""
Collector config keys added with the interval counters, per-user GPU, the
optional metrics-run collectors (sessions, storage health) and the storage
breakdown: their defaults and how a value is checked.

A missing key means its default. An invalid value is replaced by its
default with a warning (as collector._cfg_number does), so a typo in
config.json never stops a run. Keys of a nested block are written
"block.key" (e.g. "sessions.timeout_seconds" reads config["sessions"]
["timeout_seconds"]); a block that is not a JSON object counts as missing.

config_tool.py validates `set` and `check` against the same SCHEMA.

List-like values whose entries are checked one by one ('ports', 'cidrs')
keep their valid entries: an invalid entry is dropped (with the warning),
not the whole value.
"""
import copy
import ipaddress
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# key: (kind, default, options)
#   kind 'bool'; 'number' / 'int' with options {'minimum': ..., 'maximum': ...};
#   'choice' with options = the allowed strings; 'globs' / 'strings' = a list
#   of non-empty strings; 'ports' = {service name: port or [ports]};
#   'cidrs' = a list of IP addresses or networks; 'temperatures' = {'hdd':
#   [warn, crit], 'ssd': [warn, crit]} in degrees C; 'days' = [a, b] with
#   0 < a < b
SCHEMA = {
    # Interval counters (perf_counters.py, proc_accounting.py)
    'perf_counters':              ('bool', True, None),     # false: the old 1-s sampling and per-user seeding
    'counter_max_window_seconds': ('number', 86400, {'minimum': 60}),
    'user_metrics':               ('choice', 'auto', ('auto', 'all', 'active', 'off')),
    'user_metrics_min_cores':     ('number', 0.02, {'minimum': 0}),
    'user_metrics_min_ram_bytes': ('int', 1073741824, {'minimum': 0}),
    'user_cpu_count_children':    ('bool', True, None),     # Linux only
    'disk_io':                    ('bool', True, None),
    'disk_io_exclude':            ('globs', [], None),
    # Per-user GPU (gpu.py)
    'gpu_per_user':               ('choice', 'auto', ('auto', 'off')),
    'metrics_interval_seconds':   ('number', 300, {'minimum': 1}),
    # Optional collectors run after the metrics are recorded (collector.collect_metrics)
    # Who is connected (sessions.py)
    'sessions.enabled':           ('bool', True, None),
    'sessions.detail':            ('choice', 'full', ('full', 'users', 'counts')),
    'sessions.history':           ('choice', 'changes', ('changes', 'every_run')),
    'sessions.list_heartbeat_seconds': ('number', 3600, {'minimum': 60}),
    'sessions.max_entries':       ('int', 200, {'minimum': 1, 'maximum': 5000}),
    'sessions.ports':             ('ports', None, None),     # null: the platform's map (sessions.py)
    'sessions.extra_ports':       ('ports', {}, None),
    'sessions.exclude_clients':   ('cidrs', [], None),
    'sessions.timeout_seconds':   ('number', 20, {'minimum': 1}),
    # Storage health (storage_health.py)
    'storage_health.enabled':     ('bool', True, None),
    'storage_health.interval_seconds': ('number', 3600, {'minimum': 60}),
    'storage_health.timeout_seconds': ('number', 90, {'minimum': 1}),
    'storage_health.temperature_c': ('temperatures', {'hdd': [50, 60], 'ssd': [70, 80]}, None),
    'storage_health.ignore':      ('strings', [], None),     # component ids
    # Storage breakdown in the daily disk run (storage_breakdown.py)
    'storage_breakdown.enabled':  ('bool', True, None),
    'storage_breakdown.owners':   ('choice', 'file', ('file', 'none')),
    'storage_breakdown.top_owners': ('int', 10, {'minimum': 1, 'maximum': 1000}),
    'storage_breakdown.device_top_owners': ('int', 25, {'minimum': 1, 'maximum': 1000}),
    'storage_breakdown.max_rows': ('int', 5000, {'minimum': 100, 'maximum': 45000}),
    'storage_breakdown.cold_days': ('days', [365, 1095], None),
    'storage_breakdown.name_lookup_budget_seconds': ('number', 30, {'minimum': 0}),
    'storage_breakdown.max_timeout_fraction': ('number', 0.6, {'minimum': 0.05, 'maximum': 1}),
}

# The nested blocks; a block that is not a JSON object counts as missing
BLOCKS = tuple(sorted({key.split('.', 1)[0] for key in SCHEMA if '.' in key}))

SERVICE_RE = re.compile(r'^[a-z][a-z0-9]{0,15}$')     # contract R10 service names

# Accounts left out of per-user data on every platform, before "exclude_users"
DEFAULT_EXCLUDE_USERS = (
    'UMFD-*',              # User Mode Driver Framework (Windows service)
    'NT AUTHORITY\\*',     # Windows system accounts
    'NT SERVICE\\*',       # Windows service accounts
    'Window Manager\\*',   # Windows display driver
    'Font Driver Host\\*', # Windows font driver
    'DWM.EXE',             # Desktop Window Manager
    'System',              # Windows system process
    'Idle',                # Idle process
)


def exclude_user_patterns(config: dict) -> list:
    """The username glob patterns left out of per-user data: the defaults plus "exclude_users"."""
    extra = (config or {}).get('exclude_users') or []
    return list(DEFAULT_EXCLUDE_USERS) + [p for p in extra if isinstance(p, str)]


def raw_value(config: dict, key: str) -> Any:
    """The configured value of key ('block.key' for nested blocks), or None if it isn't set."""
    node = config
    for part in key.split('.'):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def check(key: str, raw: Any):
    """
    (ok, value) for a raw config value of a SCHEMA key: value is the checked
    value (an int-valued float stays a float for 'number'), or the default
    when ok is False ('ports' and 'cidrs': the valid entries). None (unset)
    is ok and gives the default. The value is never SCHEMA's own object.
    """
    kind, default, options = SCHEMA[key]
    default = copy.deepcopy(default)
    if raw is None:
        return True, default
    if kind == 'bool':
        return (True, raw) if isinstance(raw, bool) else (False, default)
    if kind in ('number', 'int'):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False, default
        if kind == 'int' and not float(raw).is_integer():
            return False, default
        value = int(raw) if kind == 'int' else raw
        if value != value:   # NaN
            return False, default
        limits = options or {}
        if 'minimum' in limits and value < limits['minimum']:
            return False, default
        if 'maximum' in limits and value > limits['maximum']:
            return False, default
        return True, value
    if kind == 'choice':
        if isinstance(raw, str) and raw.strip().lower() in options:
            return True, raw.strip().lower()
        return False, default
    if kind in ('globs', 'strings'):
        if isinstance(raw, list) and all(isinstance(p, str) and p for p in raw):
            return True, list(raw)
        return False, default
    if kind == 'ports':
        return _check_ports(raw, default)
    if kind == 'cidrs':
        if not isinstance(raw, list):
            return False, default
        kept = [c for c in raw if isinstance(c, str) and _network(c) is not None]
        return len(kept) == len(raw), kept
    if kind == 'temperatures':
        if not isinstance(raw, dict) or set(raw) - set(default):
            return False, default
        merged = dict(default)
        for media, pair in raw.items():
            if not _increasing_pair(pair, number=True):
                return False, default
            merged[media] = list(pair)
        return True, merged
    if kind == 'days':
        return (True, list(raw)) if _increasing_pair(raw) else (False, default)
    raise ValueError(f"unknown kind {kind!r} for {key}")   # pragma: no cover - a SCHEMA typo


def _network(text: str):
    """ipaddress network of an IP address or CIDR ('10.0.0.0/8', 'fe80::1'), or None."""
    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None


def _increasing_pair(pair, number: bool = False) -> bool:
    """[a, b] with 0 < a < b (ints, or any numbers when number=True)."""
    kinds = (int, float) if number else (int,)
    return (isinstance(pair, list) and len(pair) == 2
            and all(isinstance(v, kinds) and not isinstance(v, bool) and v == v for v in pair)
            and 0 < pair[0] < pair[1])


def _check_ports(raw, default):
    """
    {service: [port, ...]} from {service: port or [ports]}: services named
    ^[a-z][a-z0-9]{0,15}$ with ports 1-65535. Invalid services and ports are
    dropped; ok is False when anything was.
    """
    if not isinstance(raw, dict):
        return False, default
    ok, ports = True, {}
    for service, values in raw.items():
        values = values if isinstance(values, list) else [values]
        valid = [p for p in values if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535]
        if not (isinstance(service, str) and SERVICE_RE.match(service)) or not valid:
            ok = False
            continue
        ok = ok and len(valid) == len(values)
        ports[service] = sorted(set(valid))
    return ok, ports


def value(config: dict, key: str, log: Optional[logging.Logger] = None):
    """The checked value of a SCHEMA key; an invalid one is logged and replaced by the default."""
    raw = raw_value(config or {}, key)
    ok, result = check(key, raw)
    if not ok:
        (log or logger).warning(f"Invalid {key}={raw!r} in config; using {result!r}")
    return result
