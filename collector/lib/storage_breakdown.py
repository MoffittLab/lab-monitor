"""
Storage attribution: the 'storage_breakdown' message of the daily disk run
(contract item 15), sent next to folder_usage as its own message.

For each measured folder: bytes and file count, how much of it is cold
(content not modified within cold_days, by mtime; atime is never used), and
on Linux and DSM the same per file owner (st_uid, resolved to the account
name, 'uid:<n>' without one): the top storage_breakdown.top_owners owners
plus an 'other' row for the rest. The device entry rolls the top-level
folders up (a folder inside another measured folder is not counted twice).
Windows reports bytes, files and cold data with the owner fields null
(owner_source 'none'). The numbers come from the walk folder_usage already
makes (disk_usage.scan_folders with BreakdownOptions); the time guard there
decides which folders get them.

folder_usage itself is unchanged. Sizes are apparent sizes (st_size), hard
links counted once, as in folder_usage.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from . import disk_usage, settings

logger = logging.getLogger(__name__)

STATE_FILE = 'storage_breakdown_state.json'
SCHEMA_VERSION = 1
SECONDS_PER_DAY = 86400
OWNER_MAX_CHARS = 256          # contract R10 usernames
PATH_MAX_CHARS = 4096          # contract R10 paths
NUMBERS = ('bytes', 'files', 'cold_1y_bytes', 'cold_3y_bytes')


def _wall_time() -> float:
    return time.time()


def _passwd_name(uid: int) -> Optional[str]:
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return None


def resolve_owner_names(uids: Iterable[int], budget_s: float = 30) -> Dict[int, str]:
    """
    {uid: account name} for the uids, looked up in a daemon thread for at
    most budget_s seconds in all (NSS, winbind or LDAP can block). A uid
    without an account, or not resolved within the budget, is 'uid:<n>'.
    """
    uids = sorted(set(uids))
    found = {}

    def lookup():
        for uid in uids:
            try:
                found[uid] = _passwd_name(uid)
            except Exception:
                found[uid] = None
    worker = threading.Thread(target=lookup, daemon=True, name='lab-monitor-owner-names')
    worker.start()
    worker.join(max(0.0, budget_s))
    if worker.is_alive():
        logger.warning(f"Storage breakdown: owner name lookups took longer than {budget_s:g}s; "
                       f"{len(uids) - len(found)} owner(s) are reported as uid:<n>")
    return {uid: (found.get(uid) or f'uid:{uid}')[:OWNER_MAX_CHARS] for uid in uids}


class Options:
    """The storage_breakdown.* settings and the cold cut-offs of one disk run."""

    def __init__(self, config: dict, windows: bool, log: Optional[logging.Logger] = None,
                 now: Optional[float] = None):
        def value(key):
            return settings.value(config, f'storage_breakdown.{key}', log)
        self.enabled = value('enabled')
        self.windows = windows
        self.owners = value('owners') == 'file' and not windows
        self.top_owners = value('top_owners')
        self.device_top_owners = value('device_top_owners')
        self.max_rows = value('max_rows')
        self.cold_days = value('cold_days')
        self.name_budget = value('name_lookup_budget_seconds')
        self.max_fraction = value('max_timeout_fraction')
        self.reference_time = _wall_time() if now is None else now
        self.cut1 = self.reference_time - self.cold_days[0] * SECONDS_PER_DAY
        self.cut3 = self.reference_time - self.cold_days[1] * SECONDS_PER_DAY
        self.previous = {}           # the time guard's {path: seconds} (load_state)
        self.state_path = None       # where the collector keeps it

    def scan_options(self, previous: Optional[dict]) -> disk_usage.BreakdownOptions:
        return disk_usage.BreakdownOptions(self.cut1, self.cut3, self.owners, previous, self.max_fraction)


def load_state(path: str) -> Dict[str, float]:
    """{path: seconds} from storage_breakdown_state.json ({} when missing or corrupt: the first run)."""
    import json
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {p: s for p, s in state.items() if isinstance(p, str) and isinstance(s, (int, float))
            and not isinstance(s, bool)}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _record(rec) -> dict:
    return {'bytes': rec[0], 'files': rec[1], 'cold_1y_bytes': rec[2], 'cold_3y_bytes': rec[3]}


def _by_owner(acc: dict, names: Dict[int, str]) -> Dict[str, list]:
    """{owner name: [bytes, files, cold1, cold3]} (two uids with one name are merged)."""
    owners = {}
    for uid, rec in acc.items():
        name = names.get(uid) or f'uid:{uid}'
        into = owners.setdefault(name, [0, 0, 0, 0])
        for i in range(4):
            into[i] += rec[i]
    return owners


def _owner_rows(owners: Dict[str, list], top: int):
    """(top owner rows, 'other' row) sorted by bytes, largest first."""
    ranked = sorted(owners.items(), key=lambda item: (-item[1][0], item[0]))
    rows = [dict(owner=name, **_record(rec)) for name, rec in ranked[:top]]
    rest = [rec for _, rec in ranked[top:]]
    other = {'owner_count': len(rest), **_record([sum(r[i] for r in rest) for i in range(4)])}
    return rows, other


def _totals(acc: Optional[dict]):
    """[bytes, files, cold1, cold3] over every owner's record, or None."""
    if acc is None:
        return None
    return [sum(rec[i] for rec in acc.values()) for i in range(4)]


def _has_ancestor(key: str, keys: set) -> bool:
    """Whether keys holds an ancestor of key: a prefix followed by a separator, or one ending in one ('/', 'C:\\')."""
    for i, ch in enumerate(key):
        if ch == os.sep and key[:i] in keys:
            return True
        if ch in '/\\' and i + 1 < len(key) and key[:i + 1] in keys:
            return True
    return False


def _top_level(paths: List[str]) -> List[str]:
    """
    The paths no other path in the list is an ancestor of (case-insensitive
    on Windows). Each path's own prefixes are looked up in a set, so this
    stays linear in the number of folders (thousands under /volume1/homes).
    """
    keys = {p: os.path.normcase(os.path.normpath(p)) for p in paths}
    present = set(keys.values())
    return [p for p in paths if not _has_ancestor(keys[p], present)]


def build_payload(scan: dict, folder_order: List[str], opts: Options,
                  names: Optional[Dict[int, str]] = None) -> Optional[dict]:
    """
    The 'storage_breakdown' message data from a scan_folders() result with
    breakdown (folders in folder_order), or None when no folder was
    measured. names: {uid: name} (resolved here when not given).

    Row budget: each folder costs 1 + min(N, owners) + 1 rows. Over
    max_rows, N drops to max(1, max_rows // folders - 2); if that is still
    over, per-folder owners are left out (folder totals and the device
    entry remain; top_owners_effective 0). Either way owners_truncated is
    true. The device entry (at most device_top_owners + 2 rows) is outside
    the budget.
    """
    sizes, files = scan.get('sizes') or {}, scan.get('files') or {}
    details = scan.get('breakdown') or {}
    folders = [p for p in folder_order if p in sizes and len(p) <= PATH_MAX_CHARS]
    if not folders:
        return None
    if names is None and opts.owners:
        uids = {uid for p in folders for uid in ((details.get(p) or {}).get('acc') or {}) if uid is not None}
        uids |= {(details.get(p) or {}).get('folder_owner') for p in folders} - {None}
        names = resolve_owner_names(uids, opts.name_budget)
    names = names or {}

    entries, owner_maps = [], {}
    for path in folders:
        detail = details.get(path) or {}
        acc = detail.get('acc')
        totals = _totals(acc)
        count = files.get(path)
        entry = {'path': path, 'folder_owner': None, 'bytes': sizes[path],
                 'files': count if isinstance(count, int) and count >= 0 else None,
                 'cold_1y_bytes': totals[2] if totals else None, 'cold_3y_bytes': totals[3] if totals else None,
                 'owners': None, 'other': None}
        if opts.owners:
            uid = detail.get('folder_owner')
            entry['folder_owner'] = None if uid is None else names.get(uid, f'uid:{uid}')
            if acc is not None:
                owner_maps[path] = _by_owner(acc, names)
        entries.append(entry)

    def rows(top):
        return sum(1 + (min(top, len(owner_maps[e['path']])) + 1 if e['path'] in owner_maps else 0)
                   for e in entries)
    # The folders' rows; the device entry adds at most device_top_owners + 2
    top, truncated, keep_owners = opts.top_owners, False, True
    if rows(top) > opts.max_rows:
        truncated = True
        top = max(1, opts.max_rows // len(entries) - 2)
        if rows(top) > opts.max_rows:
            keep_owners, top = False, 0
    if keep_owners:
        for entry in entries:
            if entry['path'] in owner_maps:
                entry['owners'], entry['other'] = _owner_rows(owner_maps[entry['path']], top)

    device = _device(entries, owner_maps, opts)
    return {
        '_schema': SCHEMA_VERSION,
        '_unit': 'bytes',
        'reference_time': _iso(opts.reference_time),
        'cold_thresholds_days': list(opts.cold_days),
        'owner_source': 'posix_uid' if opts.owners else 'none',
        'age_source': 'mtime',
        'top_owners': opts.top_owners,
        'top_owners_effective': top,
        'owners_truncated': truncated,
        'folders': entries,
        'device': device,
        'volume_owners': None,
        '_timed_out': list(scan.get('timed_out') or []),
        '_errors': list(scan.get('errors') or []),
        '_breakdown_unavailable': list(scan.get('breakdown_unavailable') or []),
    }


def _device(entries: List[dict], owner_maps: Dict[str, dict], opts: Options) -> Optional[dict]:
    """
    The rollup of the top-level folders, from the full per-folder figures
    (before the row budget). A total that any of those folders lacks (no
    file count, no cold data, no owners) is null rather than partial.
    """
    by_path = {e['path']: e for e in entries}
    roots = [by_path[p] for p in _top_level(list(by_path))]
    if not roots:
        return None
    device = {}
    for key in NUMBERS:
        values = [e[key] for e in roots]
        device[key] = None if any(v is None for v in values) else sum(values)
    device['owners'] = device['other'] = None
    if opts.owners and all(e['path'] in owner_maps for e in roots):
        merged = {}
        for e in roots:
            for name, rec in owner_maps[e['path']].items():
                into = merged.setdefault(name, [0, 0, 0, 0])
                for i in range(4):
                    into[i] += rec[i]
        device['owners'], device['other'] = _owner_rows(merged, opts.device_top_owners)
    return device
