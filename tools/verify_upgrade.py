#!/usr/bin/env python3
r"""
Lab Monitor - Upgrade Verification (OLD Manager vs NEW Manager on copies of your data)

Run this ON THE MANAGER SERVER, with the same Python environment as the
Manager, before switching production to the new code.  It answers one
question: "after the upgrade, is every piece of data the old Manager stored
still readable, unchanged, through the new Manager and Dashboard?"

    python tools\verify_upgrade.py --manager-config manager\config.json --report verify.json
    python tools\verify_upgrade.py --data-dir E:\Users\lab-monitor\data --work-dir F:\verify ^
                                   --with-dashboard --keep-work --report verify.json

The live data_dir is NEVER written to:
  - it is only read: SQLite databases are opened read-only (URI mode=ro; with
    immutable=1 when no other process has them open) and copied with SQLite's
    online backup API, which gives a consistent snapshot even while the live
    Manager keeps writing; other files (usage.jsonl, ...) are copied with shutil;
  - the size, mtime and sha256 of every file are recorded before and after the
    run and compared;
  - the work directory must not be inside the data_dir.
Everything else happens on two copies under --work-dir:
  copy A  served by the OLD Manager (extracted with "git archive <--old-ref>",
          default f086e7e, or --old-code DIR)
  copy B  served by the NEW Manager (this checkout, or --new-code DIR), which
          migrates it on first start exactly as it will migrate production.
Both run as subprocesses on free 127.0.0.1 ports with generated configs and
tokens; the code is copied into the work directory first, so PID files and
logs never touch the real installation.

What is checked (each check is PASS / FAIL / WARN in the summary and report):
  source      data_dir unchanged (size + mtime + sha256 of every file)
  copies      every database copied and passes PRAGMA quick_check
  config      the NEW Manager's --check-config on your --manager-config
  storage     copy B after migration: every original table and row is still
              there (row counts and content hashes per table); the old wide
              folder_usage table is kept as folder_usage_legacy with identical
              content; folder_usage_msgs has one row per original folder row and
              folder_usage_items one row per measured (row, folder) cell
  endpoints   every read endpoint the Dashboard and tools use, OLD vs NEW:
              /health, /api/systems, /api/devices, /api/metrics/all,
              /api/usage/all, /api/totals, /api/user_metrics/all, and per
              system /api/metrics/nas, /api/usage/nas, /api/usage/history,
              /api/user_metrics/<s>, /api/user_metrics/history/<s>/<user>,
              /api/data/<s>, /api/data/<s>/<type> (recent rows and every row
              over the full time range), /api/history/<s>/<type>/<field> for
              every field of every history table and every folder_usage key
              (all volumes and totals + up to --max-folders folders per system,
              sampled deterministically and listed in the report), as a tail
              query and over the full time range, plus the Dashboard's own
              "All Available" query (max_points)
  totals      lifetime network totals identical per device (API and database)
  devices     every old device present in the new /api/devices, /api/metrics/all,
              /api/usage/all, /api/totals
  restart     the NEW Manager restarted on copy B: migration is idempotent and
              every endpoint returns the same as before the restart
  ingest      one new-format system_metrics + folder_usage message per device
              into copy B (and A): totals continue from the old values, the new
              points appear after the old history, a re-send is a duplicate
              (two old spellings of one device: under the current one, N16)
  legacy-resend  stored messages re-sent the way an OLD collector does (no
              msg_id), and again with a new collector's random msg_id, are
              duplicates; a scan the old Manager only kept in the central
              snapshot (its history insert failed) is stored now
  backups     every database the NEW Manager changed has a pre-upgrade backup
              (*.bak) with exactly the original content
  rollback    tools/rollback_upgrade.py --apply on the migrated copy B (after
              the new-message checks): every database holds its original
              content again, and the OLD Manager started on it gives the same
              answers it gave before the upgrade
  dashboard   (--with-dashboard) the NEW Dashboard on copy B vs the OLD
              Dashboard on copy A: /api/data lists every system with the same
              metrics, folders and totals; charts load
Large time ranges are compared in windows (--window-cells) and the results
are streamed, so a data_dir of a few GB is fine; expect minutes, not seconds.
For the full-range history of a field, the OLD answer is computed from the OLD
Manager's own /api/data rows of the same window (both run the same
TypedDataStore.get_between query; the old route's field and value rules are
applied), so the old Manager reads every row once instead of once per field;
--old-history-windows asks its /api/history endpoint per window as well.  The
restart check replays every NEW request of the comparison against the
restarted NEW Manager and requires identical answers.

Only the INTENDED differences listed in INTENDED below are normalized away
(each with its reason); anything else is reported as a difference, grouped by
endpoint with examples.

Exit status: 0 = PASS (no unexpected differences), 1 = differences found,
2 = the verification itself could not run (bad arguments, a Manager that would
not start, not enough disk space, ...).
"""
import argparse
import hashlib
import http.client
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DEFAULT_OLD_REF = 'f086e7e'
# manager/metrics.py FUTURE_SLACK_SECONDS: a message further ahead of the
# Manager's clock does not move the device state
FUTURE_SLACK_SECONDS = 3600

LOW_TS = '0001-01-01T00:00:00.000000Z'
HIGH_TS = '9999-12-31T23:59:59.999999Z'
NEW_MAX_LIMIT = 200000          # manager.MAX_QUERY_LIMIT
INTERNAL_COLS = ('id', 'timestamp', 'device_type', 'msg_id')
SQLITE_MAGIC = b'SQLite format 3\x00'
MAX_EXAMPLES = 12
BACKUP_NAME_RE = re.compile(r'(\.bak($|[.-])|pre-upgrade|backup)', re.IGNORECASE)
DASHBOARD_ALL_FROM = '2000-01-01T00:00:00Z'   # dashboard.js ALL_TIME_FROM
TS_KEYS = frozenset({
    'timestamp', 'first_seen', 'last_seen', 'last_updated', 'metrics_timestamp',
    'disk_timestamp', 'network_last_updated', 'received_at',
})

# --------------------------------------------------------------------------- #
# Intended differences (the ONLY things normalized away)                       #
# --------------------------------------------------------------------------- #

INTENDED = OrderedDict([
    ('N1', ('timestamps compared as instants (canonical UTC)',
            'The new Manager stores timestamps as fixed-width ISO-8601 UTC '
            '("2026-08-04T07:00:00.000000Z"): migrated folder_usage rows and all new data. '
            'Rows elsewhere keep their stored text. Both sides are parsed and compared in that form.')),
    ('N2', ('fields the new Manager adds are ignored',
            "'msg_id' on history rows (de-duplication; NULL for pre-upgrade rows), "
            "'truncated'/'downsampled' flags (checked separately: they must be false for the "
            "exact windows this tool requests), 'network_last_updated' on totals, "
            "'status' on error bodies.")),
    ('N3', ('NaN compared as null',
            'Unmeasured values are null (contract 4); the old Manager could emit NaN.')),
    ('N4', ('integral floats compared as integers',
            'Narrow folder storage uses NUMERIC affinity, so 1234.0 from the old REAL column '
            'reads back as 1234 (the same value).')),
    ('N5', ('folder_usage rows: keys with null / "" values dropped',
            'The old wide table had a column for every folder ever seen, so every row carried '
            'null (or "" for text columns) for folders that scan did not report; the narrow '
            'layout stores only what each scan reported.')),
    ('N6', ('error responses: status code only',
            'Error bodies are {"status":"error","error":<generic text>} now and never contain '
            'exception text.')),
    ('N7', ('old error, new success',
            'Endpoints the old Manager could not serve: /api/usage/history always failed (it '
            'called a method that did not exist); /api/data*, /api/metrics/nas and /api/usage/nas '
            'rejected names containing ".". The new answer is checked against the data instead '
            '(the old /api/*/all endpoints, the rows compared under the directory name, or the '
            'original database).')),
    ('N8', ('history tail queries: newest N measured points',
            'Old: the measured points among the newest N rows (nulls dropped after the cut). '
            'New: nulls dropped before the cut. The old points must be exactly the newest '
            'points of the new answer.')),
    ('N9', ('history windows: "field"/"unit" not compared',
            'For a window the old Manager resolved the field name and unit on the OLDEST row of '
            'the window (nothing for an empty window); the new one uses the newest row of the '
            'table. Both are compared on the tail query.')),
    ('N10', ('equal timestamps compared as a set',
             'Neither Manager orders rows/points with the same timestamp; the order of ties '
             'may differ.')),
    ('N11', ('/api/metrics/all: current counters and total_ram_bytes',
             'New takes network_bytes_in/out from the latest metrics message (null when it '
             'measured none) instead of device_totals.last_bytes_*, and reports a missing '
             'total_ram_bytes as null instead of 0. Accepted only when the new value equals '
             'the stored latest message (device_snapshot.metrics_json).')),
    ('N12', ('internal folder tables hidden',
             'folder_usage_msgs/_items/_legacy are storage details; /api/data/<system> lists '
             '"folder_usage" once, as before.')),
    ('N13', ('Dashboard /api/data presentation',
             '*_formatted byte sizes are labelled with binary units now ("58.45 GiB" where the old '
             'Dashboard printed "58.45 GB" for the same 1024-based number) and large or negative '
             'values are scaled: sizes are compared as numbers (within the 1 % rounding of a '
             'two-decimal display), every other *_formatted string exactly; new keys timed_out / '
             'errors / scan_seconds / degraded '
             '(scan warnings); "_" metadata keys of a newer collector ("_scan_seconds") are no '
             'longer listed as a volume or folder (contract 5); unmeasured values are null '
             'instead of 0.')),
    ('N14', ('removed write endpoint POST /api/usage/report',
             'Legacy report ingest that nothing calls any more. The usage.jsonl files it wrote '
             'are checked to stay byte-identical; neither Manager reads them.')),
    ('N15', ('internal columns are not charted',
             '/api/history refuses id/timestamp/device_type/msg_id now; not compared.')),
    ('N16', ('two registered spellings of one name: each keeps its own row',
             'The old Manager keyed central rows by the raw name, so "Atlantis" and "ATLANTIS" '
             '(one data/atlantis/data.db) could each have their own device / snapshot / totals row. '
             'Both rows stay unchanged and readable under their own names; data sent under a '
             'registered spelling goes to that row (as before), data under a new spelling to the '
             'most recently seen one. The ingest check sends its probe under the most recent '
             'spelling and requires the other rows to stay exactly as they were.')),
    ('N17', ('"All Available" charts are bucket-averaged',
             'With more points than max_points the NEW Manager averages equal-width time buckets '
             '(the Dashboard says "downsampled"). Accepted only if the chart has at most max_points '
             'points, every average lies within the OLD values\' range, and the first and last '
             'points lie within one bucket of the OLD series\' first and last point.')),
])

HINTS = {
    'name-rejected': ('manager/storage_util.py DEVICE_NAME_RE / manager.py validate_nas_name: the '
                      'new name rule ^[A-Za-z0-9_.-]{1,64}$ rejects a name the old Manager stored; '
                      'registered names need to stay readable (and writable)'),
    'ingest-name': ('manager/manager.py ingest_queue: is_valid_device_name(name) refuses a device '
                    'the old Manager registered, so its collector can no longer deliver'),
    'dashboard-name': ('dashboard/app.py NAME_RE / _valid_name: the Dashboard refuses a registered '
                       'device name for chart history'),
    'backup': ('manager: no pre-upgrade copy of a database the new Manager altered '
               '(backup_before_migration)'),
    'not-found': ('manager/manager.py _system_known / _user_device_known: the NEW Manager answers 404 for '
                  'a system the OLD one served (history database, registry row or per-user rows exist)'),
    'totals': 'manager/metrics.py device_totals migration / _accumulate_network',
    'folder': 'manager/data_store.py _migrate_wide_folder_usage / _folder_rows',
    'history': 'manager/data_store.py get_field_series / _series',
    'rows': 'manager/data_store.py fetch_rows',
}


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #

class SetupError(Exception):
    """The verification cannot run (exit 2)."""


def _ascii(text) -> str:
    return str(text).encode('ascii', 'backslashreplace').decode('ascii')


def say(msg=''):
    sys.stdout.write(_ascii(msg) + '\n')
    sys.stdout.flush()


def short(value, limit=300) -> str:
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + '...(%d chars)' % len(text)


def qi(name: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def seg(*parts) -> str:
    """URL path from segments, every segment fully percent-encoded."""
    return '/' + '/'.join(quote(str(p), safe='') for p in parts)


def normalize_device_name(name) -> str:
    """Same transform as the old _sanitize_table / new storage_util (data/<key>/data.db)."""
    return re.sub(r'[^a-z0-9_]', '_', str(name).lower())[:64]


def old_name_ok(name) -> bool:
    """The old Manager's validate_nas_name (used by /api/data*, /api/metrics/nas, /api/usage/nas)."""
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', name)) and len(name) <= 100


def norm_path(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def is_within(child, parent) -> bool:
    c, p = norm_path(child), norm_path(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:      # different drives (Windows)
        return False


def is_sqlite(path) -> bool:
    try:
        with open(path, 'rb') as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def sha256_file(path, chunk=1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stat_key(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def is_unc(path) -> bool:
    p = str(path)
    return os.name == 'nt' and (p.startswith('\\\\') or p.startswith('//'))


def ro_uri(path, immutable=False) -> str:
    p = os.path.abspath(str(path))
    if is_unc(p):
        # \\server\share\x.db -> file:////server/share/x.db (empty URI authority)
        uri = 'file://' + quote(p.replace('\\', '/'), safe='/:')
    else:
        uri = Path(p).as_uri()
    return uri + '?mode=ro' + ('&immutable=1' if immutable else '')


def ro_connect(path, immutable=False) -> sqlite3.Connection:
    conn = sqlite3.connect(ro_uri(path, immutable), uri=True, timeout=60)
    return conn


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def digest(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=True, default=str)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


# ---- timestamps ---- #

_TS_RE = re.compile(
    r'^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?'
    r'\s*(Z|z|UTC|[+-]\d{2}(?::?\d{2})?)?\s*$')


def parse_ts(value):
    """Aware UTC datetime for an ISO-8601 string, or None if unparseable."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.match(value)
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        us = int((frac or '0')[:6].ljust(6, '0'))
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), us)
        if tz and tz not in ('Z', 'z', 'UTC'):
            sign = 1 if tz[0] == '+' else -1
            digits = tz[1:].replace(':', '')
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt - sign * offset
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def fmt_ts(dt) -> str:
    dt = dt.astimezone(timezone.utc)
    return '%04d-%02d-%02dT%02d:%02d:%02d.%06dZ' % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)


_TS_CACHE = {}


def canon_ts(value):
    """Canonical form of a timestamp string (unparseable values are returned unchanged)."""
    if not isinstance(value, str):
        return value
    if len(value) == 27 and value[26] == 'Z' and value[19] == '.' and value[10] == 'T':
        return value                     # already canonical (the common case)
    c = _TS_CACHE.get(value)
    if c is None:
        dt = parse_ts(value)
        c = fmt_ts(dt) if dt is not None else value
        if len(_TS_CACHE) > 500000:
            _TS_CACHE.clear()
        _TS_CACHE[value] = c
    return c


def utc_now_ts() -> str:
    return fmt_ts(datetime.now(timezone.utc))


# ---- normalization ---- #

class Applied:
    """Counts how often each intended difference was applied."""

    def __init__(self):
        self.counts = Counter()
        self.enabled = True

    def add(self, nid, n=1):
        if self.enabled:
            self.counts[nid] += n


def canon_num(v, applied=None):
    if isinstance(v, float):
        if math.isnan(v):
            if applied:
                applied.add('N3')
            return None
        if math.isfinite(v) and v.is_integer() and abs(v) < 2 ** 63:
            return int(v)
    return v


def canon(obj, applied=None, key=None):
    """Timestamps -> canonical UTC (N1); NaN -> None (N3); integral float -> int (N4)."""
    if isinstance(obj, dict):
        return {k: canon(v, applied, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [canon(v, applied) for v in obj]
    if isinstance(obj, str) and key in TS_KEYS:
        c = canon_ts(obj)
        if applied and c != obj:
            applied.add('N1')
        return c
    if isinstance(obj, float):
        return canon_num(obj, applied)
    return obj


_SIZE_TEXT_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*(B|[KMGTPE]i?B)\s*$')
_SIZE_POWER = {'B': 0, 'K': 1, 'M': 2, 'G': 3, 'T': 4, 'P': 5, 'E': 6}


class SizeText(float):
    """
    A *_formatted byte size as a number of bytes (N13).  Equal to another size
    within the rounding of a two-decimal display ("18.78 TiB" vs the old
    "-20650519212851.00 B"): 1 %.
    """

    def __eq__(self, other):
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            a, b = float(self), float(other)
            return abs(a - b) <= 0.01 * max(abs(a), abs(b), 1.0)
        return NotImplemented

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    __hash__ = float.__hash__


def size_text_value(text):
    """'58.45 GB' / '58.45 GiB' (both 1024-based) -> SizeText(bytes); None if not a byte size."""
    if not isinstance(text, str):
        return None
    m = _SIZE_TEXT_RE.match(text)
    if not m:
        return None
    return SizeText(float(m.group(1)) * 1024 ** _SIZE_POWER[m.group(2)[0]])


def series_stats_of(pts):
    vals = [(t, v) for t, v in pts if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return {'n': 0, 'min': None, 'max': None, 'tmin': None, 'tmax': None}
    return {'n': len(vals), 'min': min(v for _, v in vals), 'max': max(v for _, v in vals),
            'tmin': min(t for t, _ in vals), 'tmax': max(t for t, _ in vals)}


def downsampled_problem(pts, max_points, stats):
    """
    Why a bucket-averaged chart (N17) does not faithfully cover the OLD series,
    or None: at most max_points points, every average within the OLD values'
    range, and the first / last point within one bucket of the OLD series'
    first / last point (the whole time range is shown, nothing cut off).
    """
    if not stats['n']:
        return 'points where the OLD series has none' if pts else None
    if not pts:
        return 'no points'
    if len(pts) > max_points:
        return 'more than max_points (%d)' % max_points
    vals = [v for _, v in pts if isinstance(v, (int, float))]
    lo, hi = stats['min'], stats['max']
    if vals and (min(vals) < lo - 1e-6 * abs(lo) - 1e-9 or max(vals) > hi + 1e-6 * abs(hi) + 1e-9):
        return 'values outside the OLD value range'
    t0, t1 = parse_ts(stats['tmin']), parse_ts(stats['tmax'])
    p0, p1 = parse_ts(pts[0][0]), parse_ts(pts[-1][0])
    if None in (t0, t1, p0, p1):
        return None
    width = (t1 - t0) / max(1, max_points) + timedelta(seconds=1)
    if not (t0 - timedelta(seconds=1) <= p0 <= t0 + width):
        return 'starts at %s, the OLD series at %s (part of the range missing)' % (pts[0][0], stats['tmin'])
    if not (t1 - width <= p1 <= t1 + timedelta(seconds=1)):
        return 'ends at %s, the OLD series at %s (part of the range missing)' % (pts[-1][0], stats['tmax'])
    return None


def same_scalar(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b


def diff_obj(a, b, path='$', out=None, limit=8):
    """List of (path, old, new) differences between two JSON values."""
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if len(out) >= limit:
                break
            p = '%s.%s' % (path, k)
            if k not in a:
                out.append((p, '<absent>', b[k]))
            elif k not in b:
                out.append((p, a[k], '<absent>'))
            else:
                diff_obj(a[k], b[k], p, out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path + '.length', len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            if len(out) >= limit:
                break
            diff_obj(x, y, '%s[%d]' % (path, i), out, limit)
    elif not same_scalar(a, b):
        out.append((path, a, b))
    return out


def cut_equal(a, b, limit, ts_of):
    """
    a, b: sorted results of the same LIMIT query.  When the cut fell inside a
    group of rows with the same timestamp, each Manager may keep a different
    subset of that group (N10); everything newer must be identical.
    """
    if len(a) != len(b) or len(a) < limit or not a:
        return False
    cut_a, cut_b = min(ts_of(x) for x in a), min(ts_of(x) for x in b)
    if cut_a != cut_b:
        return False
    return [x for x in a if ts_of(x) != cut_a] == [x for x in b if ts_of(x) != cut_b]


def describe_diffs(d):
    paths = ', '.join(p for p, _, _ in d[:4])
    return '%d difference(s): %s%s' % (len(d), paths, ' ...' if len(d) > 4 else '')


def norm_row(row, table, applied=None):
    """One /api/data row: N1/N3/N4, drop msg_id (N2), folder null/'' keys (N5)."""
    if not isinstance(row, dict):
        return canon(row, applied)
    folder = table == 'folder_usage'
    r = {}
    dropped = 0
    for k, v in row.items():
        if k == 'msg_id':
            if applied:
                applied.add('N2')
            continue
        if isinstance(v, float):
            v = canon_num(v, applied)
        elif isinstance(v, str):
            if k in TS_KEYS:
                c = canon_ts(v)
                if applied and c != v:
                    applied.add('N1')
                v = c
        elif isinstance(v, (dict, list)):
            v = canon(v, applied)
        if folder and (v is None or v == ''):
            dropped += 1
            continue
        r[k] = v
    if dropped and applied:
        applied.add('N5')
    return r


def norm_points(data, applied=None):
    pts = []
    for p in data or []:
        if not isinstance(p, dict):
            continue
        ts = p.get('timestamp')
        c = canon_ts(ts)
        if applied and c != ts:
            applied.add('N1')
        pts.append((c if isinstance(c, str) else str(c), canon_num(p.get('value'), applied)))
    return pts


def sort_points(pts):
    return sorted(pts, key=lambda p: (p[0], repr(p[1])))


def old_series_from_rows(rows, url_field, applied=None):
    """
    The points the OLD /api/history returns for a window, computed from the OLD
    Manager's /api/data rows of the same window: both run
    TypedDataStore.get_between(system, type, from, to); this applies the old
    route's field resolution (unquote, then a leading '/') and value rules
    (non-null, float()-convertible, not NaN).  Non-finite values are skipped
    (N3: the new Manager never plots them).
    """
    if not rows:
        return []
    field = unquote(url_field)
    if field not in rows[0] and ('/' + field) in rows[0]:
        field = '/' + field
    out = []
    for rec in rows:
        ts, val = rec.get('timestamp'), rec.get(field)
        if ts is None or val is None:
            continue
        if isinstance(val, str):
            try:
                val = float(val)
            except ValueError:
                continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        if math.isinf(f):
            if applied:
                applied.add('N3')
            continue
        c = canon_ts(ts)
        if applied and c != ts:
            applied.add('N1')
        out.append((c if isinstance(c, str) else str(c), canon_num(f)))
    return out


def points_of(data):
    return sort_points(norm_points((data or {}).get('data')))


# --------------------------------------------------------------------------- #
# Source data_dir: fingerprint and copy                                       #
# --------------------------------------------------------------------------- #

def fingerprint(root):
    """{'files': {rel: {size, mtime_ns, sha256}}, 'dirs': [rel]} for every file under root."""
    files, dirs = {}, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != '.':
            dirs.append(rel_dir.replace(os.sep, '/'))
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace(os.sep, '/')
            try:
                st = os.stat(p)
                files[rel] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'sha256': sha256_file(p)}
            except OSError as e:
                files[rel] = {'error': '%s: %s' % (type(e).__name__, e)}
    return {'files': files, 'dirs': dirs}


def list_files(root):
    out = set()
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for fn in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, fn), str(root)).replace(os.sep, '/'))
    return out


def compare_fingerprints(before, after):
    b, a = before['files'], after['files']
    changed = []
    for rel in sorted(set(b) & set(a)):
        fields = [k for k in ('size', 'mtime_ns', 'sha256', 'error') if b[rel].get(k) != a[rel].get(k)]
        if fields:
            changed.append({'file': rel, 'fields': fields})
    return {
        'added': sorted(set(a) - set(b)),
        'removed': sorted(set(b) - set(a)),
        'changed': changed,
        'dirs_added': sorted(set(after['dirs']) - set(before['dirs'])),
        'dirs_removed': sorted(set(before['dirs']) - set(after['dirs'])),
    }


def _remove_db_files(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.remove(str(path) + suffix)
        except FileNotFoundError:
            pass


def _sqlite_backup(src_conn_factory, dst):
    _remove_db_files(dst)
    src = src_conn_factory()
    try:
        dconn = sqlite3.connect(str(dst))
        try:
            src.backup(dconn)        # one step: a consistent snapshot of the source
        finally:
            dconn.close()
    finally:
        src.close()


def backup_sqlite(src, dst, staging):
    """
    Copy one SQLite database without writing to its directory.
      - no -wal / -journal next to it (no process has it open): read with
        mode=ro&immutable=1, which creates no side files; verified afterwards
        that nothing touched the source meanwhile;
      - a -wal exists (live Manager, or one that was killed): copy db + wal to a
        private staging dir while their size/mtime stay stable and back up from
        that copy (SQLite replays the WAL there);
      - otherwise (busy live database): SQLite read-only connection on the live
        file (SQLite's locking keeps the snapshot consistent; SQLite may update
        the volatile -shm index, as every reader does).
    Returns the method used.
    """
    src, dst = str(src), str(dst)
    wal, journal = src + '-wal', src + '-journal'
    if not os.path.exists(wal) and not os.path.exists(journal) and not is_unc(src):
        before = stat_key(src)
        _sqlite_backup(lambda: ro_connect(src, immutable=True), dst)
        if stat_key(src) == before and not os.path.exists(wal) and not os.path.exists(journal):
            return 'immutable'
    if not os.path.exists(journal):
        for _attempt in range(5):
            st = (stat_key(src), stat_key(wal))
            stage = os.path.join(staging, 'stage.db')
            _remove_db_files(stage)
            shutil.copyfile(src, stage)
            if os.path.exists(wal):
                shutil.copyfile(wal, stage + '-wal')
            if (stat_key(src), stat_key(wal)) == st:
                _sqlite_backup(lambda: sqlite3.connect(stage, timeout=60), dst)
                _remove_db_files(stage)
                return 'staged-wal'
            time.sleep(1.0)
        _remove_db_files(os.path.join(staging, 'stage.db'))
    _sqlite_backup(lambda: ro_connect(src), dst)
    return 'read-only-live'


def copy_data_dir(src, dst, staging):
    """Copy src -> dst: databases via the backup API, other files with shutil.copy2."""
    copied = []
    os.makedirs(dst, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, src)
        target_dir = dst if rel_dir == '.' else os.path.join(dst, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        names = set(filenames)
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, src).replace(os.sep, '/')
            side = next((fn[:-len(s)] for s in ('-wal', '-shm', '-journal')
                         if fn.endswith(s) and fn[:-len(s)] in names), None)
            if side and is_sqlite(os.path.join(dirpath, side)):
                continue                      # copied together with its database
            t = os.path.join(target_dir, fn)
            if is_sqlite(p):
                method = backup_sqlite(p, t, staging)
                copied.append({'file': rel, 'kind': 'sqlite', 'method': method})
                # Keep the modification time (of the database or its -wal, whichever is newer):
                # the NEW Manager compares it with an existing pre-upgrade backup's to decide
                # whether that backup is still current, so the copy must look like the original.
                try:
                    times = [os.stat(x).st_mtime_ns for x in (p, p + '-wal') if os.path.exists(x)]
                    os.utime(t, ns=(max(times), max(times)))
                except OSError:
                    pass
            else:
                shutil.copy2(p, t)
                copied.append({'file': rel, 'kind': 'file'})
    return copied


def quick_check(path) -> str:
    conn = ro_connect(path, immutable=True)       # our own copy, nobody writes it yet
    try:
        rows = conn.execute('PRAGMA quick_check').fetchall()
        return '; '.join(str(r[0]) for r in rows[:5])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Inventory of the original databases (direct SQL on pristine copy A)         #
# --------------------------------------------------------------------------- #

def table_hash(conn, table, cols):
    h = hashlib.sha256()          # content only (a renamed table hashes the same)
    h.update(('%s\0' % '\0'.join(cols)).encode('utf-8', 'backslashreplace'))
    sel = ', '.join(qi(c) for c in cols)
    try:
        cur = conn.execute('SELECT %s FROM %s ORDER BY rowid' % (sel, qi(table)))
    except sqlite3.OperationalError:          # WITHOUT ROWID
        cur = conn.execute('SELECT %s FROM %s ORDER BY %s' % (sel, qi(table), sel))
    n = 0
    for row in cur:              # repr() of floats round-trips exactly; bytes repr is deterministic
        h.update(repr(row).encode('utf-8', 'backslashreplace'))
        n += 1
    return h.hexdigest(), n


def db_tables(conn):
    out = OrderedDict()
    for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        info = conn.execute('PRAGMA table_info(%s)' % qi(name)).fetchall()
        out[name] = {'columns': [r[1] for r in info],
                     'types': {r[1]: (r[2] or '').upper() for r in info}, 'sql': sql}
    return out


def logical_hash(path, tables=None):
    """Per-table content hashes of a database (tables: restrict to these, with these columns)."""
    conn = ro_connect(path, immutable=True)
    try:
        present = db_tables(conn)
        result = {}
        for t in (tables or present):
            if t not in present:
                result[t] = None
                continue
            cols = (tables[t] if tables else present[t]['columns'])
            if any(c not in present[t]['columns'] for c in cols):
                result[t] = 'missing-columns'
                continue
            result[t] = table_hash(conn, t, cols)[0]
        return result
    finally:
        conn.close()


def combine_hash(per_table):
    return digest(sorted(per_table.items()))


def inventory_db(path, kind, window_cells, want_hash):
    conn = ro_connect(path, immutable=True)       # pristine copy, nobody writes it yet
    try:
        tables = db_tables(conn)
        for t, info in tables.items():
            cols = info['columns']
            info['rows'] = conn.execute('SELECT COUNT(*) FROM %s' % qi(t)).fetchone()[0]
            if want_hash:
                info['hash'] = table_hash(conn, t, cols)[0]
            if kind == 'system' and 'timestamp' in cols and t != 'sqlite_sequence':
                per = max(100, min(50000, window_cells // max(1, len(cols))))
                bounds, i, tmin, tmax = [], 0, None, None
                for (ts,) in conn.execute('SELECT timestamp FROM %s ORDER BY timestamp' % qi(t)):
                    c = canon_ts(ts)
                    if isinstance(c, str) and parse_ts(c) is not None:
                        tmin = c if tmin is None or c < tmin else tmin
                        tmax = c if tmax is None or c > tmax else tmax
                        if i and i % per == 0:
                            bounds.append(c)
                    i += 1
                if t != 'folder_usage' and info['rows']:
                    cur = conn.execute('SELECT * FROM %s ORDER BY timestamp DESC, rowid DESC LIMIT 1' % qi(t))
                    info['newest_row'] = dict(zip([c[0] for c in cur.description], cur.fetchone()))
                edges = [LOW_TS] + sorted(set(bounds)) + [HIGH_TS]
                info['windows'] = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
                info['min_ts'], info['max_ts'] = tmin, tmax
            if kind == 'system' and t == 'folder_usage':
                data_cols = [c for c in cols if c.lower() not in INTERNAL_COLS]
                keys, items, meta = Counter(), 0, set()
                if data_cols:
                    sel = ', '.join(qi(c) for c in data_cols)
                    for row in conn.execute('SELECT %s FROM folder_usage' % sel):
                        for c, v in zip(data_cols, row):
                            if v is None or v == '' or isinstance(v, bytes):
                                continue
                            if c.startswith('_') or isinstance(v, str):
                                meta.add(c)
                                continue
                            items += 1
                            keys[c] += 1
                info['folder'] = {'expected_items': items, 'keys': dict(keys),
                                  'meta_columns': sorted(meta), 'data_columns': data_cols,
                                  'timestamps': sorted({canon_ts(r[0]) for r in conn.execute(
                                      'SELECT timestamp FROM folder_usage') if isinstance(r[0], str)})}
        result = {'kind': kind, 'tables': tables}
        if kind == 'central':
            result['devices'] = []
            if 'devices' in tables:
                cur = conn.execute('SELECT * FROM devices ORDER BY name')
                names = [c[0] for c in cur.description]
                result['devices'] = [dict(zip(names, r)) for r in cur.fetchall()]
            mj = {}
            if 'device_snapshot' in tables and 'metrics_json' in tables['device_snapshot']['columns']:
                for name, raw in conn.execute('SELECT name, metrics_json FROM device_snapshot'):
                    try:
                        mj[name] = json.loads(raw or '{}')
                    except ValueError:
                        mj[name] = {}
            result['metrics_json'] = mj
            users = []
            if 'user_metrics_history' in tables:
                for dev, user, n in conn.execute(
                        'SELECT device_name, username, COUNT(*) FROM user_metrics_history '
                        'GROUP BY device_name, username ORDER BY device_name, username'):
                    users.append((dev, user, n))
            if 'device_user_snapshot' in tables:
                seen = {(d, u) for d, u, _ in users}
                for dev, user in conn.execute('SELECT name, username FROM device_user_snapshot'):
                    if (dev, user) not in seen:
                        users.append((dev, user, 0))
            result['users'] = users
            result['totals'] = {}
            if 'device_totals' in tables:
                cur = conn.execute('SELECT * FROM device_totals')
                names = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    d = dict(zip(names, r))
                    result['totals'][d['name']] = d
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Code + processes + HTTP                                                     #
# --------------------------------------------------------------------------- #

def git(repo, *args, **kw):
    return subprocess.run(['git', '-C', str(repo)] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, **kw)


def safe_extract(tar_path, dest):
    dest = os.path.abspath(dest)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            name = m.name.replace('\\', '/')
            if name.startswith('/') or '..' in name.split('/') or re.match(r'^[A-Za-z]:', name):
                raise SetupError('unsafe path in git archive: %r' % m.name)
            if not (m.isfile() or m.isdir()):
                continue                     # no symlinks/devices
            target = os.path.join(dest, *name.split('/'))
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            with open(target, 'wb') as f:
                shutil.copyfileobj(src, f)


def _backup_belongs(f, rel):
    """Is file f (relative path) a backup of database rel? (<db>.pre-upgrade-*.bak, <db>.bak, backup/<db> ...)"""
    if f.startswith(rel):
        return True
    fdir, fbase = f.rsplit('/', 1) if '/' in f else ('', f)
    rdir, rbase = rel.rsplit('/', 1) if '/' in rel else ('', rel)
    stem = rbase.rsplit('.', 1)[0]
    same_tree = fdir == rdir or (fdir.startswith(rdir + '/') if rdir else '/' not in fdir)
    return same_tree and stem in fbase


class Resp:
    def __init__(self, status, data, text, elapsed):
        self.status, self.data, self.text, self.elapsed = status, data, text, elapsed


class Http:
    """Minimal keep-alive JSON client (stdlib only)."""

    def __init__(self, host, port, token=None, timeout=1800.0):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self._c = None
        self.requests = 0

    def _close(self):
        if self._c is not None:
            try:
                self._c.close()
            except Exception:
                pass
        self._c = None

    def close(self):
        self._close()

    def request(self, method, path, params=None, body=None, token='default', redirects=0):
        url = path + ('?' + urlencode(params) if params else '')
        headers = {'Accept': 'application/json'}
        tok = self.token if token == 'default' else token
        if tok:
            headers['Authorization'] = 'Bearer ' + tok
        payload = None
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        t0 = time.monotonic()
        for attempt in (0, 1):
            try:
                if self._c is None:
                    self._c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self._c.request(method, url, body=payload, headers=headers)
                r = self._c.getresponse()
                raw = r.read()
                status, location = r.status, r.getheader('Location')
                if (r.getheader('Connection') or '').lower() == 'close' or r.version == 10:
                    self._close()
                break
            except (http.client.HTTPException, OSError):
                self._close()
                if attempt or method != 'GET':
                    raise
        self.requests += 1
        if status in (301, 302, 303, 307, 308) and location and redirects < 5 and method == 'GET':
            parts = urlsplit(location)
            return self.request(method, parts.path + ('?' + parts.query if parts.query else ''),
                                None, None, token, redirects + 1)
        text = raw.decode('utf-8', 'replace')
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return Resp(status, data, text, time.monotonic() - t0)

    def get(self, path, params=None, token='default'):
        return self.request('GET', path, params, token=token)

    def post(self, path, body, token='default'):
        return self.request('POST', path, body=body, token=token)


class Proc:
    """A Manager or Dashboard subprocess."""

    def __init__(self, label, script, config, work, env, health='/health'):
        self.label, self.script, self.config, self.work, self.env = label, Path(script), config, work, env
        self.health = health
        self.proc = None
        self.port = config['port']
        self.log_path = work / 'logs' / ('%s.console.log' % label)
        self.cfg_path = work / 'configs' / ('%s.json' % label)

    def start(self, timeout):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(self.config, indent=2), encoding='utf-8')
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        log = open(str(self.log_path), 'ab')
        try:
            self.proc = subprocess.Popen(
                [sys.executable, '-u', str(self.script), '--config', str(self.cfg_path)],
                cwd=str(self.script.parent), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=self.env, **kw)
        finally:
            log.close()
        probe = Http('127.0.0.1', self.port, timeout=5)
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SetupError('%s exited with code %s during startup:\n%s'
                                 % (self.label, self.proc.returncode, self.log_tail()))
            try:
                if probe.get(self.health, token=None).status == 200:
                    probe.close()
                    return time.monotonic() - t0
            except OSError:
                pass
            time.sleep(0.25)
        self.stop()
        raise SetupError('%s did not answer /health within %ss:\n%s' % (self.label, timeout, self.log_tail()))

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(15)
        self.proc = None

    def log_tail(self, n=40):
        try:
            lines = self.log_path.read_text(encoding='utf-8', errors='replace').splitlines()
            return '\n'.join(lines[-n:])
        except OSError:
            return '(no log)'


# --------------------------------------------------------------------------- #
# The verifier                                                                #
# --------------------------------------------------------------------------- #

class Verifier:

    def __init__(self, args):
        self.args = args
        self.applied = Applied()
        self.checks = OrderedDict()
        self.endpoints = defaultdict(lambda: {'requests': 0, 'equal': 0, 'intended': Counter(),
                                              'different': 0, 'examples': []})
        self.differences = []
        self.warnings = []
        self.timings = OrderedDict()
        self.sampling = {}
        self.procs = []
        self.digests = {}
        self.digest_changes = []
        self.pass_no = 1
        self.folder_row_digests = defaultdict(dict)
        self.series_stats = {}
        self.name_rejected = set()      # (endpoint pattern, registered name) the NEW Manager refused
        self.old_answers = OrderedDict()    # OLD answers before any write: replayed after the rollback
        self.report = OrderedDict()

    # ---------------- recording ---------------- #

    def check(self, cid, status, summary, **details):
        """Record a check result; a check run twice (e.g. before and after the restart) keeps the worst."""
        say('[%s] %s: %s' % (status, cid, summary))
        prev = self.checks.get(cid)
        if prev is None:
            self.checks[cid] = {'status': status, 'summary': summary, 'details': details}
            return
        rank = {'PASS': 0, 'WARN': 1, 'FAIL': 2}
        prev['summary'] += '; ' + summary
        prev['details'].setdefault('more', []).append(dict(details, status=status))
        if rank.get(status, 2) > rank.get(prev['status'], 2):
            prev['status'] = status

    def ep(self, pattern):
        return self.endpoints[pattern]

    def ok(self, pattern):
        self.ep(pattern)['equal'] += 1

    def intended(self, pattern, nid):
        self.ep(pattern)['intended'][nid] += 1
        self.applied.add(nid)

    def diff(self, pattern, request, what, old=None, new=None, hint=None, system=None, count=1):
        e = self.ep(pattern)
        e['different'] += 1
        item = OrderedDict([('endpoint', pattern), ('request', request), ('system', system),
                            ('what', what), ('old', short(old)), ('new', short(new)),
                            ('count', count), ('pass', self.pass_no)])
        if hint:
            item['suspected_cause'] = HINTS.get(hint, hint)
        if len(e['examples']) < MAX_EXAMPLES:
            e['examples'].append(item)
        self.differences.append(item)

    def warn(self, text):
        self.warnings.append(text)
        say('[WARN] ' + text)

    def remember(self, key, pattern, path, params, normalize, status, obj):
        """Record a NEW response (normalized) so it can be replayed after the restart."""
        self.digests[key] = {'digest': digest(obj), 'pattern': pattern, 'path': path, 'params': params,
                             'normalize': normalize, 'status': status}

    def replay_after_restart(self):
        """Ask the restarted NEW Manager every NEW request of the comparison again: same answers?"""
        say('Replaying %d NEW-Manager requests after the restart ...' % len(self.digests))
        self.applied.enabled = False
        try:
            for key, rec in self.digests.items():
                r = self.new.get(rec['path'], rec['params'])
                if r.status != rec['status']:
                    self.digest_changes.append((key, 'HTTP %s -> %s' % (rec['status'], r.status)))
                elif r.status == 200 and digest(rec['normalize'](r.data)) != rec['digest']:
                    self.digest_changes.append((key, 'response changed'))
        finally:
            self.applied.enabled = True
        for key, why in self.digest_changes[:MAX_EXAMPLES]:
            rec = self.digests[key]
            self.diff('restart: ' + rec['pattern'], rec['path'] + ('?' + urlencode(rec['params']) if rec['params'] else ''),
                      'answer changed after restarting the NEW Manager (%s)' % why)
        if len(self.digest_changes) > MAX_EXAMPLES:
            self.diff('restart', '-', '%d more answers changed after the restart'
                      % (len(self.digest_changes) - MAX_EXAMPLES), count=len(self.digest_changes) - MAX_EXAMPLES)

    # ---------------- setup ---------------- #

    def resolve_inputs(self):
        a = self.args
        self.manager_config = None
        if a.manager_config:
            p = Path(a.manager_config)
            try:
                raw = p.read_bytes()
                enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
                self.manager_config = json.loads(raw.decode(enc))
            except (OSError, ValueError) as e:
                raise SetupError('cannot read --manager-config %s: %s' % (p, e))
        data_dir = a.data_dir
        if not data_dir and self.manager_config is not None:
            dd = self.manager_config.get('data_dir', 'data')
            if os.path.isabs(dd):
                data_dir = dd
            else:
                cands = [Path(a.manager_config).resolve().parent / dd, REPO_ROOT / 'manager' / dd, Path.cwd() / dd]
                data_dir = next((str(c) for c in cands if c.is_dir()), None)
                if data_dir is None:
                    raise SetupError('data_dir %r in the config is relative and was not found next to the '
                                     'config, in manager/ or in the current directory; pass --data-dir' % dd)
                self.warn('data_dir %r is relative; using %s' % (dd, data_dir))
        if not data_dir:
            raise SetupError('pass --data-dir or --manager-config')
        self.data_dir = Path(data_dir).resolve()
        if not self.data_dir.is_dir():
            raise SetupError('data_dir %s does not exist' % self.data_dir)
        if not (self.data_dir / 'metrics.db').is_file():
            raise SetupError('%s has no metrics.db - is this a Manager data_dir?' % self.data_dir)

        base = Path(a.work_dir).resolve() if a.work_dir else Path(tempfile.gettempdir()).resolve()
        if is_within(base, self.data_dir):
            raise SetupError('--work-dir %s must not be inside the data_dir %s' % (base, self.data_dir))
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='lab-monitor-verify-', dir=str(base)))
        self.copy_a = self.work / 'copyA-old'
        self.copy_b = self.work / 'copyB-new'
        self.staging = self.work / 'staging'
        self.staging.mkdir()
        say('Work directory: %s' % self.work)

        self.old_token = 'verify-old-' + uuid.uuid4().hex
        self.admin_token = 'verify-admin-' + uuid.uuid4().hex
        self.read_token = 'verify-read-' + uuid.uuid4().hex

    def space_check(self, fp):
        total = sum(f.get('size', 0) for f in fp['files'].values())
        dbs = sum(f.get('size', 0) for rel, f in fp['files'].items() if rel.endswith('.db'))
        # copy A + copy B + B's pre-upgrade backups + narrow folder tables + the rollback's restored copies
        need = int(total * 2 + dbs * 2.5) + 64 * 1024 * 1024
        free = shutil.disk_usage(str(self.work)).free
        self.report['space'] = {'source_bytes': total, 'database_bytes': dbs, 'estimated_need': need, 'free': free}
        if free < need and not self.args.ignore_space:
            raise SetupError('not enough free space in %s: need about %.1f GB, %.1f GB free '
                             '(use --work-dir on a bigger disk, or --ignore-space)'
                             % (self.work, need / 1e9, free / 1e9))

    def env(self, old=False):
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        if old and not self._has_module('flask_cors'):
            shim = self.work / 'old-shims'
            shim.mkdir(exist_ok=True)
            (shim / 'flask_cors.py').write_text(
                '# stub: CORS does not matter for server-side requests on 127.0.0.1\n'
                'def CORS(app=None, *a, **kw):\n    return app\n', encoding='utf-8')
            env['PYTHONPATH'] = str(shim) + os.pathsep + env.get('PYTHONPATH', '')
        return env

    @staticmethod
    def _has_module(name):
        import importlib.util
        return importlib.util.find_spec(name) is not None

    def prepare_code(self):
        a = self.args
        repo = Path(a.repo).resolve() if a.repo else REPO_ROOT
        info = OrderedDict()
        if a.old_code:
            old = Path(a.old_code).resolve()
            info['old'] = {'source': 'directory', 'path': str(old)}
        else:
            old = self.work / 'old-code'
            tar_path = self.work / 'old-code.tar'
            try:
                rev = git(repo, 'rev-parse', '--verify', a.old_ref + '^{commit}').stdout.decode().strip()
                with open(tar_path, 'wb') as f:
                    subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', rev],
                                   stdout=f, stderr=subprocess.PIPE, check=True)
            except FileNotFoundError:
                raise SetupError('git is not installed or not on PATH; extract the old code yourself and '
                                 'pass --old-code DIR')
            except subprocess.CalledProcessError as e:
                raise SetupError('git archive %s failed in %s: %s' % (a.old_ref, repo, e.stderr.decode(errors='replace')))
            safe_extract(str(tar_path), str(old))
            tar_path.unlink()
            info['old'] = {'source': 'git archive', 'ref': a.old_ref, 'commit': rev, 'repo': str(repo)}
        if not (old / 'manager' / 'manager.py').is_file():
            raise SetupError('%s has no manager/manager.py' % old)
        self.old_code = old

        src_new = Path(a.new_code).resolve() if a.new_code else REPO_ROOT
        new = self.work / 'new-code'
        ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'config.json', '*.log', 'logs', 'data', '.pids')
        for part in ('manager', 'dashboard'):
            if (src_new / part).is_dir():
                shutil.copytree(str(src_new / part), str(new / part), ignore=ignore)
        if not (new / 'manager' / 'manager.py').is_file():
            raise SetupError('%s has no manager/manager.py' % src_new)
        if (src_new / 'tools' / 'rollback_upgrade.py').is_file():
            (new / 'tools').mkdir(exist_ok=True)
            shutil.copy2(str(src_new / 'tools' / 'rollback_upgrade.py'), str(new / 'tools' / 'rollback_upgrade.py'))
        self.new_code = new
        ninfo = {'source': str(src_new)}
        try:
            ninfo['commit'] = git(src_new, 'rev-parse', 'HEAD').stdout.decode().strip()
            dirty = git(src_new, 'status', '--porcelain', '--', 'manager', 'dashboard').stdout.decode().strip()
            ninfo['uncommitted_changes'] = bool(dirty)
        except (OSError, subprocess.CalledProcessError):
            pass
        info['new'] = ninfo
        self.report['code'] = info

    def preflight_config(self):
        if self.manager_config is None:
            return
        cfg = dict(self.manager_config)
        cfg.pop('log_file', None)
        empty = self.work / 'preflight-empty'
        empty.mkdir(exist_ok=True)
        cfg['data_dir'] = str(empty)
        path = self.work / 'configs' / 'preflight-your-config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding='utf-8')
        r = subprocess.run([sys.executable, str(self.new_code / 'manager' / 'manager.py'),
                            '--config', str(path), '--check-config'],
                           cwd=str(self.new_code / 'manager'), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=self.env(), timeout=120)
        out = r.stdout.decode('utf-8', 'replace').strip().splitlines()
        if r.returncode == 0:
            self.check('config', 'PASS', 'the NEW Manager accepts your config (--check-config)')
        else:
            # A refused config means the upgraded Manager would not start at
            # all (exit 2): that must never come out as PASS.
            # The Manager's message names the setting (e.g. "auth_tokens[1]"), never a token value
            reason = next((ln for ln in reversed(out) if 'Refusing' in ln or 'must be' in ln),
                          out[-1] if out else '')
            self.check('config', 'FAIL', 'the NEW Manager would REFUSE to start with your config '
                       '(exit %s), so the upgrade would take the Manager down: fix the config first '
                       '(docs/UPGRADE.md, "Fix a refused token without downtime"), then run this '
                       'again. %s' % (r.returncode, reason.strip()[-300:]), output=out[-10:])

    def start_manager(self, which, data_dir, port=None):
        port = port or free_port()
        log_dir = self.work / 'logs'
        if which == 'old':
            cfg = {'data_dir': str(data_dir), 'host': '127.0.0.1', 'port': port,
                   'auth_tokens': [self.old_token], 'log_file': str(log_dir / 'old-manager.log'),
                   'log_level': 'WARNING', 'cors_origins': ['http://127.0.0.1']}
            p = Proc('old-manager', self.old_code / 'manager' / 'manager.py', cfg, self.work, self.env(old=True))
        else:
            cfg = {'data_dir': str(data_dir), 'host': '127.0.0.1', 'port': port,
                   'admin_tokens': [self.admin_token], 'read_tokens': [self.read_token],
                   'log_file': str(log_dir / 'new-manager.log'), 'log_level': 'INFO',
                   'backup_before_migration': True, 'history_retention_days': None}
            p = Proc('new-manager', self.new_code / 'manager' / 'manager.py', cfg, self.work, self.env())
        secs = p.start(self.args.startup_timeout)
        self.procs.append(p)
        return p, secs

    def stop(self, proc):
        proc.stop()
        if proc in self.procs:
            self.procs.remove(proc)

    def stop_all(self):
        for p in list(self.procs):
            try:
                p.stop()
            except Exception:
                pass
        self.procs = []

    # ---------------- phases ---------------- #

    def run(self):
        a = self.args
        t_start = time.monotonic()
        self.report['started'] = utc_now_ts()
        self.resolve_inputs()
        self.report['inputs'] = OrderedDict([
            ('data_dir', str(self.data_dir)), ('manager_config', a.manager_config),
            ('work_dir', str(self.work)), ('old_ref', None if a.old_code else a.old_ref),
            ('old_code', a.old_code), ('new_code', a.new_code or str(REPO_ROOT)),
            ('max_folders', a.max_folders), ('window_cells', a.window_cells),
            ('tail_limit', a.tail_limit), ('folder_tail_limit', a.folder_tail_limit),
            ('user_history_limit', a.user_history_limit), ('with_dashboard', a.with_dashboard),
            ('old_history_windows', a.old_history_windows), ('python', sys.version.split()[0]),
            ('platform', sys.platform),
        ])
        try:
            self._run_phases()
        finally:
            self.stop_all()
            self.timings['total'] = round(time.monotonic() - t_start, 1)

    def _timed(self, name, fn, *args):
        t0 = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.timings[name] = round(time.monotonic() - t0, 1)

    def _run_phases(self):
        say('Fingerprinting the source data_dir (read only): %s' % self.data_dir)
        self.fp_before = self._timed('fingerprint_before', fingerprint, str(self.data_dir))
        if any('error' in f for f in self.fp_before['files'].values()):
            self.warn('some source files could not be read: %s' % [
                k for k, f in self.fp_before['files'].items() if 'error' in f][:5])
        self.space_check(self.fp_before)
        self.live_manager = self._live_manager_running()

        self._timed('code', self.prepare_code)
        self._timed('preflight_config', self.preflight_config)

        say('Copying the data_dir (SQLite backup API) to copy A ...')
        self.copied = self._timed('copy', copy_data_dir, str(self.data_dir), str(self.copy_a), str(self.staging))
        bad = []
        if not self.args.no_quick_check:
            for c in self.copied:
                if c['kind'] == 'sqlite':
                    res = quick_check(self.copy_a / c['file'])
                    c['quick_check'] = res
                    if res != 'ok':
                        bad.append((c['file'], res))
        self.report['copies'] = self.copied
        methods = Counter(c.get('method', 'file') for c in self.copied)
        if bad:
            self.check('copies', 'FAIL', 'database integrity problems in the SOURCE data: %s' % bad, copied=self.copied)
        else:
            self.check('copies', 'PASS', '%d files copied (%s)' % (
                len(self.copied), ', '.join('%s: %d' % kv for kv in sorted(methods.items()))))
        shutil.copytree(str(self.copy_a), str(self.copy_b))
        self.b_files_initial = list_files(self.copy_b)

        say('Inventory of the original databases ...')
        self.inv = self._timed('inventory', self.inventory)

        say('Starting the OLD Manager on copy A and the NEW Manager on copy B ...')
        self.old_proc, t_old = self.start_manager('old', self.copy_a)
        self.new_proc, t_new = self.start_manager('new', self.copy_b)
        self.timings['old_manager_start'] = round(t_old, 1)
        self.timings['new_manager_first_start'] = round(t_new, 1)
        self.old = Http('127.0.0.1', self.old_proc.port, self.old_token, self.args.request_timeout)
        self.new = Http('127.0.0.1', self.new_proc.port, self.read_token, self.args.request_timeout)
        self.new_admin = Http('127.0.0.1', self.new_proc.port, self.admin_token, self.args.request_timeout)

        self._timed('warm_up', self.warm_up)
        self._timed('storage_after_migration', self.check_storage, 'after first start')
        self._timed('compare', self.compare_all)
        if self.args.with_dashboard:
            self._timed('dashboard', self.check_dashboard)

        say('Restarting the NEW Manager on copy B (migration must be idempotent) ...')
        self.stop(self.new_proc)
        self.scan_log('new-manager.log', 'first start')
        self.new_proc, t_new2 = self.start_manager('new', self.copy_b)
        self.timings['new_manager_restart'] = round(t_new2, 1)
        for h in (self.new, self.new_admin):
            h.close()
            h.port = self.new_proc.port
        self.pass_no = 2
        self._timed('storage_after_restart', self.check_storage, 'after restart')
        self._timed('replay_after_restart', self.replay_after_restart)
        if self.digest_changes:
            self.check('restart', 'FAIL', '%d answers changed after restarting the NEW Manager'
                       % len(self.digest_changes), examples=[short(k) for k in self.digest_changes[:10]])
        else:
            self.check('restart', 'PASS', 'NEW Manager restarted on the migrated copy: storage unchanged, '
                       'all %d NEW answers identical' % len(self.digests))

        self._timed('ingest', self.check_ingest)
        self._timed('legacy_resend', self.check_legacy_resend)
        self.stop_all()
        self.scan_log('new-manager.log', 'whole run')
        self._timed('backups', self.check_backups)
        self.check_other_files()
        self._timed('rollback', self.check_rollback)

        say('Fingerprinting the source data_dir again ...')
        self.fp_after = self._timed('fingerprint_after', fingerprint, str(self.data_dir))
        self.check_source_unchanged()

    def _live_manager_running(self):
        port = (self.manager_config or {}).get('port', 5000)
        try:
            r = Http('127.0.0.1', int(port), timeout=3).get('/health', token=None)
            return r.status == 200
        except (OSError, ValueError):
            return False

    def inventory(self):
        inv = {'dbs': OrderedDict(), 'systems': []}
        want_hash = not self.args.no_hash
        for c in self.copied:
            if c['kind'] != 'sqlite':
                continue
            rel = c['file']
            parts = rel.split('/')
            if rel == 'metrics.db':
                kind = 'central'
            elif len(parts) == 2 and parts[1] == 'data.db':
                kind = 'system'
            else:
                kind = 'other'
            inv['dbs'][rel] = inventory_db(self.copy_a / rel, kind, self.args.window_cells, want_hash)
        central = inv['dbs'].get('metrics.db') or {'devices': [], 'metrics_json': {}, 'users': [], 'totals': {}}
        inv['central'] = central
        dirs = {rel.split('/')[0] for rel, d in inv['dbs'].items() if d['kind'] == 'system'}
        # Windows: the directory may have been created in another case first (the old
        # /api/usage/report wrote data/<raw name>/), the same directory for the OS
        by_lower = {}
        for x in sorted(dirs):
            by_lower.setdefault(x.lower(), x)

        def dir_of(name):
            d = normalize_device_name(name)
            if d in dirs:
                return d
            return by_lower.get(d) if os.name == 'nt' else d

        covered = set()
        registered = {dev['name'] for dev in central['devices']}
        for dev in central['devices']:
            d = dir_of(dev['name'])
            inv['systems'].append({'name': dev['name'], 'dir': d if d in dirs else None, 'registered': True,
                                   'device_type': dev.get('device_type'), 'system_id': dev.get('system_id')})
            covered.add(d)
            if d not in dirs:
                self.warn('device %r has no data/%s/data.db (central rows only)'
                          % (dev['name'], normalize_device_name(dev['name'])))
        # Per-user rows outlive their device: the old DELETE /api/system/<name> never removed them
        user_only = sorted({dev for dev, _u, _n in central['users'] if dev not in registered}, key=str)
        for d in sorted(dirs - covered):
            names = [u for u in user_only if dir_of(u) == d]
            inv['systems'].append({'name': names[0] if names else None, 'dir': d, 'registered': False,
                                   'device_type': None, 'system_id': None})
            covered.add(d)
            user_only = [u for u in user_only if u not in names[:1]]
            self.warn('data/%s/data.db has no device in metrics.db; compared under its directory name%s'
                      % (d, ' and the per-user rows\' name %r' % names[0] if names else ''))
        for u in user_only:
            d = dir_of(u)
            inv['systems'].append({'name': u, 'dir': d if d in dirs else None, 'registered': False,
                                   'device_type': None, 'system_id': None})
            self.warn('per-user rows of %r, which has no device in metrics.db (left by an old DELETE); '
                      'compared under that name' % u)
        total_rows = sum(t['rows'] for db in inv['dbs'].values() for t in db['tables'].values())
        self.report['inventory'] = {
            'databases': {rel: {t: info['rows'] for t, info in db['tables'].items()} for rel, db in inv['dbs'].items()},
            'systems': inv['systems'], 'total_rows': total_rows,
        }
        say('  %d databases, %d systems, %d rows' % (len(inv['dbs']), len(inv['systems']), total_rows))
        return inv

    def warm_up(self):
        """First read of every system on the NEW Manager (runs the folder_usage migration)."""
        times = {}
        for s in self.inv['systems']:
            if not s['dir']:
                continue
            t0 = time.monotonic()
            r = self.new.get(seg('api', 'data', s['dir']))
            times[s['dir']] = round(time.monotonic() - t0, 2)
            if r.status != 200:
                self.warn('NEW /api/data/%s answered HTTP %s on first open' % (s['dir'], r.status))
        self.timings['first_open_per_system'] = times

    # ---------------- storage checks (copy B) ---------------- #

    def check_storage(self, phase):
        problems, notes = [], []
        for rel, dbinv in self.inv['dbs'].items():
            path = self.copy_b / rel
            if not path.is_file():
                problems.append('%s: database missing in copy B' % rel)
                continue
            conn = ro_connect(path)
            try:
                btables = db_tables(conn)
                for t, info in dbinv['tables'].items():
                    if t == 'sqlite_sequence':
                        continue
                    orig_cols = info['columns']
                    if dbinv['kind'] == 'system' and t == 'folder_usage':
                        self._check_folder_storage(rel, conn, btables, info, problems, notes)
                        continue
                    if t not in btables:
                        problems.append('%s: table %s is gone' % (rel, t))
                        continue
                    missing = [c for c in orig_cols if c not in btables[t]['columns']]
                    if missing:
                        problems.append('%s: %s lost columns %s' % (rel, t, missing[:5]))
                        continue
                    n = conn.execute('SELECT COUNT(*) FROM %s' % qi(t)).fetchone()[0]
                    if n != info['rows']:
                        problems.append('%s: %s has %d rows, the original %d' % (rel, t, n, info['rows']))
                        continue
                    if 'hash' in info:
                        h, _ = table_hash(conn, t, orig_cols)
                        if h != info['hash']:
                            problems.append('%s: %s rows changed (content hash over the original columns differs)'
                                            % (rel, t))
            finally:
                conn.close()
        if problems:
            self.check('storage', 'FAIL', '%s: %s' % (phase, '; '.join(problems[:6])), problems=problems)
        else:
            self.check('storage', 'PASS', '%s: every original table and row is intact in copy B%s'
                       % (phase, ' (%s)' % '; '.join(notes[:3]) if notes else ''), notes=notes)

    def _check_folder_storage(self, rel, conn, btables, info, problems, notes):
        legacy = sorted(t for t in btables if t == 'folder_usage_legacy' or t.startswith('folder_usage_legacy_'))
        if 'folder_usage' in btables and not legacy:
            problems.append('%s: wide folder_usage table was not migrated' % rel)
            return
        if len(legacy) != 1:
            problems.append('%s: expected one folder_usage_legacy table, found %s' % (rel, legacy or 'none'))
            if not legacy:
                return
        lt = legacy[0]
        missing = [c for c in info['columns'] if c not in btables[lt]['columns']]
        if missing:
            problems.append('%s: %s lost columns %s' % (rel, lt, missing[:5]))
        else:
            n = conn.execute('SELECT COUNT(*) FROM %s' % qi(lt)).fetchone()[0]
            if n != info['rows']:
                problems.append('%s: %s has %d rows, the original folder_usage %d' % (rel, lt, n, info['rows']))
            elif 'hash' in info and table_hash(conn, lt, info['columns'])[0] != info['hash']:
                problems.append('%s: %s content differs from the original folder_usage' % (rel, lt))
        if 'folder_usage' in btables:
            problems.append('%s: a wide folder_usage table exists next to %s' % (rel, lt))
        for t in ('folder_usage_msgs', 'folder_usage_items'):
            if t not in btables:
                problems.append('%s: %s missing' % (rel, t))
                return
        msgs = conn.execute('SELECT COUNT(*), MIN(id), MAX(id) FROM folder_usage_msgs').fetchone()
        items = conn.execute('SELECT COUNT(*) FROM folder_usage_items').fetchone()[0]
        expected = info['folder']['expected_items']
        if msgs[0] != info['rows']:
            problems.append('%s: folder_usage_msgs has %d rows, the original folder_usage %d'
                            % (rel, msgs[0], info['rows']))
        if items != expected:
            problems.append('%s: folder_usage_items has %d rows, expected %d measured cells'
                            % (rel, items, expected))
        notes.append('%s: %d folder rows x %d columns -> %d narrow items' % (
            rel.split('/')[0], info['rows'], len(info['folder']['data_columns']), items))

    # ---------------- endpoint comparison ---------------- #

    def compare_all(self):
        say('Comparing endpoints (OLD vs NEW) ...')
        self.compare_global()
        systems = self.inv['systems']
        dirs_done = set()
        for i, s in enumerate(systems, 1):
            label = s['name'] if s['name'] is not None else s['dir']
            t0 = time.monotonic()
            if s['dir'] and s['dir'] not in dirs_done:       # two spellings may share one data.db
                dirs_done.add(s['dir'])
                self.compare_system_storage(s)
            if s['name'] is not None:
                self.compare_device(s)
            say('  [%d/%d] %s (%.1fs, %d requests so far)' % (
                i, len(systems), label, time.monotonic() - t0, self.old.requests + self.new.requests))
        self.summarize_endpoints()

    def get_pair(self, path, params=None, new_path=None, new_params=None):
        o = self.old.get(path, params)
        n = self.new.get(new_path or path, new_params if new_params is not None else params)
        return o, n

    def status_outcome(self, pattern, request, o, n, system=None, allow_old_error=False, hint=None):
        """
        Returns 'both-ok', 'same-error', 'old-error-ok' (old failed where the new may
        succeed: N7) or None after recording a difference.
        """
        if o.status == 200 and n.status == 200:
            return 'both-ok'
        if o.status == n.status:
            if o.text != n.text:
                self.intended(pattern, 'N6')
            else:
                self.ok(pattern)
            return 'same-error'
        if n.status == 200 and allow_old_error:
            self.intended(pattern, 'N7')
            return 'old-error-ok'
        if o.status == 200 and n.status == 404:
            hint = 'not-found'
        self.diff(pattern, request, 'HTTP %s (old) -> %s (new)' % (o.status, n.status),
                  short(o.data if o.data is not None else o.text, 160),
                  short(n.data if n.data is not None else n.text, 160), hint=hint, system=system)
        return None

    def rejected(self, pattern, name, d, o, n, request):
        """
        The NEW Manager answered 400 for a registered system name that the OLD one
        served: record it (once per endpoint and system) and return True, so the
        caller re-asks the NEW Manager under the directory name (data/<dir>/),
        which both accept, to still compare the data itself.
        """
        if n.status != 400 or not d or name == d or o.status not in (200, 500):
            return False
        if 'name' not in str((n.data or {}).get('error') if isinstance(n.data, dict) else n.text).lower():
            return False            # refused for another reason (e.g. the data type): a plain difference
        if (pattern, name) not in self.name_rejected:
            self.name_rejected.add((pattern, name))
            self.diff(pattern, request, 'registered system name %r rejected by the NEW Manager (HTTP 400); '
                      'its data is compared under the directory name %r' % (name, d),
                      'HTTP %s' % o.status, short(n.data, 120), hint='name-rejected', system=name)
        return True

    def new_sys(self, pattern, name, d):
        """System name to send to the NEW Manager for this endpoint."""
        return d if (pattern, name) in self.name_rejected else name

    def compare_simple(self, pattern, path, normalize, params=None, system=None, fix=None,
                       allow_old_error=False, hint=None, key=None, new_path=None, oracle=None,
                       alt_new_path=None, accept=None):
        o, n = self.get_pair(path, params, new_path=new_path)
        st = self.ep(pattern)
        st['requests'] += 1
        request = (new_path or path) + ('?' + urlencode(params) if params else '')
        if alt_new_path and self.rejected(pattern, system, alt_new_path[0], o, n, request):
            n = self.new.get(alt_new_path[1], params)
            request = alt_new_path[1] + ('?' + urlencode(params) if params else '')
        outcome = self.status_outcome(pattern, request, o, n, system, allow_old_error, hint)
        if self.pass_no == 1:
            was, self.applied.enabled = self.applied.enabled, False     # not counted twice
            try:
                self.old_answers[('old',) + tuple(key or ('simple', request))] = {
                    'pattern': pattern, 'path': path, 'params': params, 'normalize': normalize,
                    'status': o.status, 'digest': digest(normalize(o.data)) if o.status == 200 else None}
            finally:
                self.applied.enabled = was
        if outcome is None:
            return o, n
        if outcome == 'same-error':
            return o, n
        b = normalize(n.data)
        self.remember(key or ('simple', request), pattern, request.split('?')[0], params, normalize, n.status, b)
        if outcome == 'old-error-ok':
            if oracle is not None:
                a = oracle()
                if a is not None:
                    if fix:
                        a, b = fix(a, b)
                    d = diff_obj(a, b)
                    if d:
                        self.diff(pattern, request, 'new answer differs from the data (the old endpoint '
                                  'failed, HTTP %s): %s' % (o.status, describe_diffs(d)), d[0][1], d[0][2],
                                  hint=hint, system=system, count=len(d))
            return o, n
        a = normalize(o.data)
        if fix:
            a, b = fix(a, b)
        d = diff_obj(a, b)
        nid = accept(a, b) if (d and accept) else None
        if nid:
            self.intended(pattern, nid)
        elif d:
            self.diff(pattern, request, describe_diffs(d), d[0][1], d[0][2], hint=hint, system=system,
                      count=len(d))
        else:
            self.ok(pattern)
        return o, n

    # ---- global endpoints ---- #

    def compare_global(self):
        ap = self.applied

        def n_health(d):
            return {'status': (d or {}).get('status')}

        o = self.old.get('/health', token=None)
        n = self.new.get('/health', token=None)
        self.ep('/health')['requests'] += 1
        if o.status == n.status == 200 and n_health(o.data) == n_health(n.data):
            self.ok('/health')
        else:
            self.diff('/health', '/health', 'health', o.status, n.status)

        def n_devices(d):
            rows = canon((d or {}).get('devices') or [], ap)
            return sorted(rows, key=lambda r: str(r.get('name')))

        self.compare_simple('/api/devices', '/api/devices', n_devices, key=('global', 'devices'))
        self.compare_simple('/api/systems', '/api/systems',
                            lambda d: sorted((d or {}).get('systems') or [], key=str))

        o, n = self.compare_simple('/api/metrics/all', '/api/metrics/all', lambda d: canon(d or {}, ap),
                                   fix=self.fix_metrics, key=('global', 'metrics_all'))
        self.old_metrics_all = canon(o.data or {}, None) if o.status == 200 else {}
        self.new_metrics_all = canon(n.data or {}, None) if n.status == 200 else {}
        o, n = self.compare_simple('/api/usage/all', '/api/usage/all', lambda d: canon(d or {}, ap),
                                   key=('global', 'usage_all'))
        self.old_usage_all = canon(o.data or {}, None) if o.status == 200 else {}
        if self.pass_no == 1:
            self.old_usage_all_raw = o.data if o.status == 200 and isinstance(o.data, dict) else {}
        self.new_usage_all = canon(n.data or {}, None) if n.status == 200 else {}

        def n_totals(d):
            d = canon(d or {}, ap)
            for row in (d.get('devices') or {}).values():
                if isinstance(row, dict) and 'network_last_updated' in row:
                    row.pop('network_last_updated')
                    ap.add('N2')
            return d

        o, n = self.compare_simple('/api/totals', '/api/totals', n_totals, hint='totals', key=('global', 'totals'))
        self.old_totals = n_totals(o.data) if o.status == 200 else {}
        self.new_totals = n_totals(n.data) if n.status == 200 else {}

        def n_users(d):
            d = canon(d or {}, ap)
            return {k: sorted(v, key=lambda r: str(r.get('username'))) if isinstance(v, list) else v
                    for k, v in d.items()}

        self.compare_simple('/api/user_metrics/all', '/api/user_metrics/all', n_users, key=('global', 'users_all'))
        r = self.new_admin.get('/api/dead_letter')
        self.ep('/api/dead_letter (new only)')['requests'] += 1
        if r.status == 200 and isinstance((r.data or {}).get('dead_letters'), list):
            self.ok('/api/dead_letter (new only)')
        else:
            self.diff('/api/dead_letter (new only)', '/api/dead_letter', 'admin read failed', 200, r.status)
        if self.pass_no == 1:
            self.check_presence_and_totals()

    def fix_metrics(self, a, b):
        """N11: accept the new current counters / total_ram_bytes when they equal the stored message."""
        mj = self.inv['central']['metrics_json']
        single = 'name' in a and not isinstance(a.get('name'), dict)
        pairs = [(a.get('name'), a, b)] if single else [(k, a[k], b.get(k)) for k in a if isinstance(a.get(k), dict)]
        for name, ra, rb in pairs:
            if not isinstance(rb, dict):
                continue
            raw = mj.get(name, {})
            for k in ('network_bytes_in', 'network_bytes_out', 'total_ram_bytes'):
                if k in ra and k in rb and not same_scalar(ra[k], rb[k]):
                    v = raw.get(k)
                    expect = canon_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                    if same_scalar(rb[k], expect) or (rb[k] is None and expect is None):
                        ra[k] = rb[k]
                        self.applied.add('N11')
        return a, b

    def check_presence_and_totals(self):
        old_devs = [s['name'] for s in self.inv['systems'] if s['registered']]
        r = self.new.get('/api/devices')
        new_devs = {d.get('name') for d in (r.data or {}).get('devices', [])} if r.status == 200 else set()
        missing = {
            '/api/devices': [d for d in old_devs if d not in new_devs],
            '/api/metrics/all': [d for d in self.old_metrics_all if d not in self.new_metrics_all],
            '/api/usage/all': [d for d in self.old_usage_all if d not in self.new_usage_all],
            '/api/totals': [d for d in (self.old_totals.get('devices') or {})
                            if d not in (self.new_totals.get('devices') or {})],
        }
        lost = {k: v for k, v in missing.items() if v}
        if lost:
            self.check('devices', 'FAIL', 'old devices missing in the new API: %s' % lost, missing=lost)
        else:
            self.check('devices', 'PASS', 'all %d old devices present in /api/devices, /api/metrics/all (%d), '
                       '/api/usage/all (%d), /api/totals' % (len(old_devs), len(self.old_metrics_all),
                                                             len(self.old_usage_all)))
        # Lifetime totals: API (exact) and database
        bad = []
        od, nd = self.old_totals.get('devices') or {}, self.new_totals.get('devices') or {}
        for name, row in od.items():
            nrow = nd.get(name) or {}
            for k in ('total_bytes_in', 'total_bytes_out', 'last_bytes_in', 'last_bytes_out', 'total_disk_bytes'):
                if not same_scalar(row.get(k), nrow.get(k)):
                    bad.append('%s.%s old=%s new=%s' % (name, k, row.get(k), nrow.get(k)))
        if diff_obj(self.old_totals.get('global'), self.new_totals.get('global')):
            bad.append('global %s -> %s' % (short(self.old_totals.get('global')), short(self.new_totals.get('global'))))
        conn = ro_connect(self.copy_b / 'metrics.db')
        try:
            cur = conn.execute('SELECT * FROM device_totals')
            cols = [c[0] for c in cur.description]
            brows = {r[cols.index('name')]: dict(zip(cols, r)) for r in cur.fetchall()}
        finally:
            conn.close()
        for name, row in self.inv['central']['totals'].items():
            for k, v in row.items():
                if not same_scalar(v, (brows.get(name) or {}).get(k)):
                    bad.append('database device_totals[%s].%s: %s -> %s' % (name, k, v, (brows.get(name) or {}).get(k)))
        if bad:
            self.check('totals', 'FAIL', '%d lifetime-total differences: %s' % (len(bad), '; '.join(bad[:4])),
                       differences=bad, suspected_cause=HINTS['totals'])
        else:
            self.check('totals', 'PASS', 'lifetime network/disk totals identical for %d devices (API and database)'
                       % len(od))

    # ---- per device (registered name) ---- #

    def compare_device(self, s):
        name, d = s['name'], s['dir']
        ap = self.applied
        dot_name = not old_name_ok(name)

        # /api/metrics/nas/<n>: old rejected "." names (N7) -> check against /api/metrics/all
        self.compare_simple(
            '/api/metrics/nas/<system>', seg('api', 'metrics', 'nas', name), lambda x: canon(x or {}, ap),
            system=name, fix=self.fix_metrics, allow_old_error=dot_name, hint='name-rejected',
            oracle=lambda: self.old_metrics_all.get(name), key=('metrics_nas', name))
        self.compare_simple(
            '/api/usage/nas/<system>', seg('api', 'usage', 'nas', name), lambda x: canon(x or {}, ap),
            system=name, allow_old_error=dot_name, hint='name-rejected',
            oracle=lambda: self.old_usage_all.get(name), key=('usage_nas', name))

        def n_user_rows(x):
            rows = canon(x if isinstance(x, list) else [], ap)
            return sorted(rows, key=lambda r: (str(r.get('username')), str(r.get('timestamp'))))

        alt = (d, seg('api', 'user_metrics', d)) if d else None
        self.compare_simple('/api/user_metrics/<system>', seg('api', 'user_metrics', name), n_user_rows,
                            system=name, hint='name-rejected', key=('user_one', name), alt_new_path=alt)

        if d:
            self.compare_simple(
                '/api/data/<system>', seg('api', 'data', name),
                lambda x: {'data_types': sorted((x or {}).get('data_types') or [])},
                system=name, allow_old_error=dot_name, hint='name-rejected',
                oracle=lambda: {'data_types': self._old_data_types(d)}, key=('data_types', name))
            self.compare_usage_history(name, d)

        users = [(u, c) for dev, u, c in self.inv['central']['users'] if dev == name]
        pattern = '/api/user_metrics/history/<system>/<user>'
        for user, count in users:
            # newest rows per user (every row is also covered by the storage check's content hash)
            limit = max(1, min(count or 200, self.args.user_history_limit, NEW_MAX_LIMIT))

            def n_hist(x):
                rows = canon(x if isinstance(x, list) else [], ap)
                return sorted(rows, key=lambda r: (str(r.get('timestamp')), repr(r.get('cpu_percent')),
                                                   repr(r.get('ram_bytes'))))

            alt = (d, seg('api', 'user_metrics', 'history', d, user)) if d else None
            self.compare_simple(pattern, seg('api', 'user_metrics', 'history', name, user), n_hist,
                                params={'limit': limit}, system=name, hint='name-rejected',
                                new_path=seg('api', 'user_metrics', 'history', self.new_sys(pattern, name, d), user),
                                key=('user_hist', name, user), alt_new_path=alt)

    def _system_db(self, d):
        rel = '%s/data.db' % d
        db = self.inv['dbs'].get(rel)
        return [(rel, db)] if db else []

    def _old_data_types(self, d):
        r = self.old.get(seg('api', 'data', d))
        return sorted((r.data or {}).get('data_types') or []) if r.status == 200 else None

    def compare_usage_history(self, name, d):
        """/api/usage/history: old always failed (N7); new rows must equal the folder rows."""
        pattern = '/api/usage/history/<system>'
        path = seg('api', 'usage', 'history', name)
        params = {'days': 36500, 'limit': NEW_MAX_LIMIT}
        o, n = self.get_pair(path, params)
        self.ep(pattern)['requests'] += 1
        if self.rejected(pattern, name, d, o, n, path):
            n = self.new.get(seg('api', 'usage', 'history', d), params)
        outcome = self.status_outcome(pattern, path, o, n, name, allow_old_error=True, hint='name-rejected')
        if outcome not in ('old-error-ok', 'both-ok'):
            return
        info = (self.inv['dbs'].get('%s/data.db' % d) or {}).get('tables', {}).get('folder_usage')
        records = (n.data or {}).get('records') or []
        expect_rows = info['rows'] if info else 0
        # rows older than 100 years are outside days=36500; the fixture/real data never are
        self.remember(('usage_hist', name), pattern, seg('api', 'usage', 'history', self.new_sys(pattern, name, d)),
                      params, lambda x: [norm_row(r, 'folder_usage') for r in (x or {}).get('records') or []],
                      n.status, [norm_row(r, 'folder_usage') for r in records])
        if len(records) != expect_rows:
            self.diff(pattern, path, 'record count vs original folder rows', expect_rows, len(records),
                      hint='folder', system=name)
            return
        known = self.folder_row_digests.get(d, {})
        bad = 0
        for r in records:
            nr = norm_row(r, 'folder_usage')
            if known.get(nr.get('id')) != digest(nr):
                bad += 1
        if bad:
            self.diff(pattern, path, '%d records differ from the /api/data folder rows' % bad,
                      hint='folder', system=name, count=bad)

    # ---- history series ---- #

    def history_fields(self, d, table, info):
        cols = [c for c in info['columns'] if c.lower() not in INTERNAL_COLS]
        if table != 'folder_usage':
            numeric = [c for c in cols if info['types'].get(c) != 'TEXT']
            return numeric, [c for c in cols if c not in numeric], []
        f = info['folder']
        keys = sorted(f['keys'])
        always, folders = [], []
        for k in keys:
            parts = [p for p in k.replace('\\', '/').split('/') if p]
            if k == 'total_usage' or k.endswith('_total_bytes') or k.endswith('_free_bytes') or len(parts) <= 1:
                always.append(k)
            else:
                folders.append(k)
        # numeric metadata columns (e.g. "_scan_seconds" from a newer collector) were chartable too
        always += [c for c in f['meta_columns'] if info['types'].get(c) != 'TEXT' and c not in f['keys']]
        sampled = folders
        if self.args.max_folders >= 0 and len(folders) > self.args.max_folders:
            rnd = random.Random('verify-upgrade:' + d)
            sampled = sorted(rnd.sample(folders, self.args.max_folders))
        text_cols = [c for c in cols if c not in f['keys'] and c not in always]
        if self.pass_no == 1:
            self.sampling[d] = {'folder_keys_total': len(folders), 'folders_sampled': len(sampled),
                                'always_included': always, 'sampled': sampled,
                                'not_sampled': len(folders) - len(sampled)}
        return always + sampled, text_cols, []

    def _hist_path(self, sysname, table, field):
        url_field = field[1:] if field.startswith('/') else field
        return seg('api', 'history', sysname, table, url_field)

    @staticmethod
    def _url_field(field):
        return field[1:] if field.startswith('/') else field

    def compare_history_tail(self, name, d, table, field):
        """/api/history tail query (no from/to): OLD vs NEW, including field resolution and unit."""
        ap = self.applied
        pattern = '/api/history/<system>/%s/<field> (tail)' % table
        # the OLD Manager reads every column of the newest `limit` rows for each field:
        # keep that small for the (wide, daily) folder table
        limit = self.args.folder_tail_limit if table == 'folder_usage' else self.args.tail_limit
        params = {'limit': limit}
        opath = self._hist_path(name, table, field)
        npath = self._hist_path(self.new_sys(pattern, name, d), table, field)
        o, n = self.get_pair(opath, params, new_path=npath)
        self.ep(pattern)['requests'] += 1
        if self.rejected(pattern, name, d, o, n, npath):
            npath = self._hist_path(d, table, field)
            n = self.new.get(npath, params)
        if self.status_outcome(pattern, npath, o, n, name, hint='history') != 'both-ok':
            return
        od, nd = o.data or {}, n.data or {}
        a, b = sort_points(norm_points(od.get('data'), ap)), sort_points(norm_points(nd.get('data'), ap))

        def norm(x):
            x = x or {}
            return [x.get('field'), x.get('unit'), points_of(x)]

        self.remember(('tail', name, table, field), pattern, npath, params, norm, n.status, norm(nd))
        problems = []
        if od.get('field') != nd.get('field'):
            problems.append(('field', od.get('field'), nd.get('field')))
        if (od.get('unit') or None) != (nd.get('unit') or None) and a:
            problems.append(('unit', od.get('unit'), nd.get('unit')))
        if nd.get('downsampled'):
            problems.append(('downsampled', False, True))
        if a != b:
            if len(a) < len(b) and b[len(b) - len(a):] == a:
                self.intended(pattern, 'N8')
            elif cut_equal(a, b, limit, lambda p: p[0]):
                self.intended(pattern, 'N10')
            else:
                problems.append(('data', self._series_summary(a), self._series_summary(b)))
        if problems:
            self.diff(pattern, npath, '%s: %s differ' % (field, '/'.join(p[0] for p in problems)),
                      problems[0][1], problems[0][2], hint='history', system=name, count=len(problems))
        else:
            self.ok(pattern)

    @staticmethod
    def _series_summary(pts):
        if not pts:
            return {'points': 0}
        return {'points': len(pts), 'first': pts[0], 'last': pts[-1]}

    def compare_table(self, name, d, table, info):
        """
        One history table of one system, over its whole time range in windows:
          - every row: OLD vs NEW /api/data/<dir>/<table>?start&end
          - every charted field: NEW /api/history ...?from&to vs what the OLD
            /api/history gives for that window (old_series_from_rows on the OLD
            rows, or the OLD endpoint itself with --old-history-windows)
        """
        ap = self.applied
        rows_pat = '/api/data/<system>/<data_type> (every row, full range)'
        hist_pat = '/api/history/<system>/%s/<field> (full range)' % table
        hint_rows = 'folder' if table == 'folder_usage' else 'rows'
        rows_path = seg('api', 'data', d, table)
        numeric, text_cols, _ = self.history_fields(d, table, info)
        for field in numeric + text_cols:
            self.compare_history_tail(name, d, table, field)
        if not info['rows']:
            return

        series = {f: {'pend_old': Counter(), 'pend_new': Counter(), 'old_total': 0, 'new_total': 0,
                      'n': 0, 'min': None, 'max': None, 'keep': [], 'failed': False} for f in numeric}
        pend_old, pend_new = {}, {}
        n_old = 0
        rows_failed, field_diffs, examples = False, 0, []
        for (s, e) in info['windows']:
            o = self.old.get(rows_path, {'start': s, 'end': e})
            nparams = {'start': s, 'end': e, 'limit': NEW_MAX_LIMIT}
            n = self.new.get(rows_path, nparams)
            self.ep(rows_pat)['requests'] += 1
            if self.status_outcome(rows_pat, rows_path, o, n, name, hint=hint_rows) != 'both-ok':
                rows_failed = True
                continue
            if (n.data or {}).get('truncated'):
                self.diff(rows_pat, rows_path, 'window [%s, %s) truncated' % (s, e), False, True,
                          hint=hint_rows, system=name)
                rows_failed = True
            old_raw = (o.data or {}).get('rows') or []
            orows = {}
            for r in old_raw:
                orows[r.get('id')] = norm_row(r, table, ap)
            nrows = {}
            for r in (n.data or {}).get('rows') or []:
                nr = norm_row(r, table, ap)
                nrows[nr.get('id')] = nr
                if table == 'folder_usage':
                    self.folder_row_digests[d][nr.get('id')] = digest(nr)

            def norm_rows(x, table=table):
                out = [norm_row(r, table) for r in (x or {}).get('rows') or []]
                return sorted(((str(r.get('id')), r) for r in out), key=lambda kv: kv[0])

            self.remember(('rows', d, table, s), rows_pat, rows_path, nparams, norm_rows, n.status,
                          sorted(((str(k), v) for k, v in nrows.items()), key=lambda kv: kv[0]))
            n_old += len(orows)
            for rid in set(orows) & set(nrows):
                if orows[rid] == nrows[rid]:
                    continue
                dd = diff_obj(orows[rid], nrows[rid], '$[id=%s]' % rid)
                if dd:
                    field_diffs += 1
                    if len(examples) < 4:
                        examples.extend(dd[:2])
            for rid in set(orows) - set(nrows):
                pend_old[rid] = orows[rid]
            for rid in set(nrows) - set(orows):
                pend_new[rid] = nrows[rid]

            for field in numeric:
                self._history_window(name, d, table, field, s, e, old_raw, series[field], hist_pat)

        # rows: reconcile across windows, report
        for rid in set(pend_old) & set(pend_new):
            dd = diff_obj(pend_old.pop(rid), pend_new.pop(rid), '$[id=%s]' % rid)
            if dd:
                field_diffs += 1
                if len(examples) < 4:
                    examples.extend(dd[:2])
        problems = False
        for side, pend in (('OLD', pend_old), ('NEW', pend_new)):
            if pend:
                ex = [{'id': k, 'timestamp': v.get('timestamp')} for k, v in
                      sorted(pend.items(), key=lambda kv: str(kv[0]))[:3]]
                self.diff(rows_pat, rows_path, '%s: %d rows only in %s' % (table, len(pend), side),
                          ex if side == 'OLD' else None, ex if side == 'NEW' else None,
                          hint=hint_rows, system=name, count=len(pend))
                problems = True
        if field_diffs:
            self.diff(rows_pat, rows_path, '%s: %d rows with different values, e.g. %s' % (
                table, field_diffs, ', '.join(x[0] for x in examples[:4])), examples[0][1], examples[0][2],
                hint=hint_rows, system=name, count=field_diffs)
            problems = True
        if not rows_failed and n_old != info['rows']:
            self.warn('%s/%s: the OLD Manager returned %d rows over the full range, the database has %d '
                      '(rows with unparseable timestamps?)' % (d, table, n_old, info['rows']))
        if not rows_failed and not problems:
            self.ok(rows_pat)

        # charted fields: reconcile, report, then the Dashboard's own query
        for field in numeric:
            st = series[field]
            common = st['pend_old'] & st['pend_new']
            st['pend_old'] -= common
            st['pend_new'] -= common
            npath = self._hist_path(self.new_sys(hist_pat, name, d), table, field)
            if st['pend_old'] or st['pend_new']:
                self.diff(hist_pat, npath, '%s: %d points only in OLD, %d only in NEW (old total %d, new %d)'
                          % (field, sum(st['pend_old'].values()), sum(st['pend_new'].values()),
                             st['old_total'], st['new_total']),
                          sorted(st['pend_old'].elements())[:5], sorted(st['pend_new'].elements())[:5],
                          hint='history', system=name,
                          count=sum(st['pend_old'].values()) + sum(st['pend_new'].values()))
            elif not st['failed']:
                self.ok(hist_pat)
            self.series_stats[(name, table, field)] = {'total': st['new_total'], 'n': st['n']}
            if not st['failed'] and table in ('system_metrics', 'folder_usage'):
                full = st['keep'] if st['n'] <= self.args.dashboard_max_points else None
                self.check_dashboard_range(name, table, field, npath, st, full)

    def _history_window(self, name, d, table, field, s, e, old_rows, st, pattern):
        ap = self.applied
        params = {'from': s, 'to': e, 'limit': NEW_MAX_LIMIT}
        npath = self._hist_path(self.new_sys(pattern, name, d), table, field)
        n = self.new.get(npath, params)
        self.ep(pattern)['requests'] += 1
        if self.args.old_history_windows or (n.status == 400 and (pattern, name) not in self.name_rejected):
            o = self.old.get(self._hist_path(name, table, field), {'from': s, 'to': e})
        else:
            o = Resp(200, None, '', 0.0)
        if self.rejected(pattern, name, d, o, n, npath):
            npath = self._hist_path(d, table, field)
            n = self.new.get(npath, params)
        if n.status != 200 or o.status != 200:
            if self.status_outcome(pattern, npath, o, n, name, hint='history') != 'both-ok':
                st['failed'] = True
                return
        nd = n.data or {}
        if nd.get('downsampled') or nd.get('truncated'):
            self.diff(pattern, npath, '%s: window [%s, %s) came back downsampled/truncated' % (field, s, e),
                      False, True, hint='history', system=name)
            st['failed'] = True
            return
        if o.data is not None:
            a = sort_points(norm_points(o.data.get('data'), ap))
        else:
            a = sort_points(old_series_from_rows(old_rows, self._url_field(field), ap))
        b = sort_points(norm_points(nd.get('data'), ap))
        self.remember(('window', name, table, field, s), pattern, npath, params, points_of, n.status, b)
        st['old_total'] += len(a)
        st['new_total'] += len(b)
        for ts, v in a:              # the OLD data is what the Dashboard's chart must show
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                st['n'] += 1
                st['min'] = v if st['min'] is None else min(st['min'], v)
                st['max'] = v if st['max'] is None else max(st['max'], v)
                st['tmin'] = ts if st.get('tmin') is None else min(st['tmin'], ts)
                st['tmax'] = ts if st.get('tmax') is None else max(st['tmax'], ts)
        if len(st['keep']) <= self.args.dashboard_max_points:
            st['keep'].extend(a)
        if a != b:
            ca, cb = Counter(a), Counter(b)
            common = ca & cb
            st['pend_old'].update(ca - common)
            st['pend_new'].update(cb - common)
            if any(len(set(t for t, _ in x)) != len(x) for x in (a, b)):
                ap.add('N10')

    def check_dashboard_range(self, name, table, field, npath, stats, full):
        """The Dashboard's "All Available" query (from/to + max_points) on the NEW Manager."""
        pattern = '/api/history/<system>/%s/<field> (dashboard max_points)' % table
        mp = self.args.dashboard_max_points
        r = self.new.get(npath, {'from': DASHBOARD_ALL_FROM, 'to': HIGH_TS, 'max_points': mp})
        self.ep(pattern)['requests'] += 1
        if r.status != 200:
            self.diff(pattern, npath, '%s: HTTP %s' % (field, r.status), 200, r.status, hint='history', system=name)
            return
        pts = sort_points(norm_points((r.data or {}).get('data')))
        old_enough = [p for p in (full or []) if p[0] >= canon_ts(DASHBOARD_ALL_FROM)]
        if full is not None and stats['n'] <= mp:
            if pts != sort_points(old_enough):
                self.diff(pattern, npath, '%s: "All Available" chart differs from the OLD series' % field,
                          self._series_summary(sort_points(old_enough)), self._series_summary(pts),
                          hint='history', system=name)
                return
        else:
            why = downsampled_problem(pts, mp, stats)
            if why:
                vals = [v for _, v in pts if isinstance(v, (int, float))]
                self.diff(pattern, npath, '%s: downsampled "All Available" chart: %s' % (field, why),
                          {'raw_points': stats['n'], 'min': stats['min'], 'max': stats['max'],
                           'first': stats.get('tmin'), 'last': stats.get('tmax')},
                          {'points': len(pts), 'min': min(vals) if vals else None, 'max': max(vals) if vals else None,
                           'first': pts[0][0] if pts else None, 'last': pts[-1][0] if pts else None},
                          hint='history', system=name)
                return
            self.intended(pattern, 'N17')
            return
        self.ok(pattern)

    # ---- rows (per directory name, valid in both Managers) ---- #

    def compare_system_storage(self, s):
        d = s['dir']
        name = s['name'] if s['name'] is not None else d
        ap = self.applied
        self.compare_simple('/api/data/<system> (directory name)', seg('api', 'data', d),
                            lambda x: {'data_types': sorted((x or {}).get('data_types') or [])},
                            system=name, key=('data_types_dir', d))
        for rel, dbinv in self._system_db(d):
            for table, info in dbinv['tables'].items():
                if 'windows' not in info:
                    continue
                # recent rows (default limit 100), newest first
                pattern = '/api/data/<system>/<data_type> (recent)'

                def n_recent(x, table=table):
                    rows = [norm_row(r, table, ap) for r in ((x or {}).get('rows') or [])]
                    return sorted(rows, key=lambda r: (str(r.get('timestamp')), str(r.get('id'))))

                self.compare_simple(pattern, seg('api', 'data', d, table), n_recent, system=name,
                                    hint='rows' if table != 'folder_usage' else 'folder',
                                    key=('recent', d, table),
                                    accept=lambda a, b: 'N10' if cut_equal(
                                        a, b, 100, lambda r: str(r.get('timestamp'))) else None)
                self.compare_table(name, d, table, info)

    def summarize_endpoints(self):
        n_diff = sum(e['different'] for e in self.endpoints.values())
        n_req = sum(e['requests'] for e in self.endpoints.values())
        status = 'FAIL' if n_diff else 'PASS'
        self.check('endpoints', status, 'pass %d: %d comparisons, %d unexpected differences'
                   % (self.pass_no, n_req, n_diff))

    # ---------------- dashboard ---------------- #

    def start_dashboard(self, which, manager_port, token):
        port = free_port()
        code = self.old_code if which == 'old' else self.new_code
        cfg = {'manager_url': 'http://127.0.0.1:%d' % manager_port, 'manager_token': token,
               'host': '127.0.0.1', 'port': port, 'log_file': str(self.work / 'logs' / ('%s-dashboard.log' % which)),
               'log_level': 'WARNING', 'manager_timeout_seconds': self.args.request_timeout,
               'refresh_interval_seconds': 30, 'cache_ttl_seconds': 0}
        # the old Dashboard has no /health route
        p = Proc('%s-dashboard' % which, code / 'dashboard' / 'app.py', cfg, self.work, self.env(old=which == 'old'),
                 health='/' if which == 'old' else '/health')
        p.start(self.args.startup_timeout)
        self.procs.append(p)
        return p, Http('127.0.0.1', port, None, self.args.request_timeout)

    def check_dashboard(self):
        say('Comparing the OLD Dashboard (copy A) with the NEW Dashboard (copy B) ...')
        try:
            od, oh = self.start_dashboard('old', self.old_proc.port, self.old_token)
            nd, nh = self.start_dashboard('new', self.new_proc.port, self.read_token)
        except SetupError as e:
            self.check('dashboard', 'FAIL', 'a Dashboard did not start: %s' % str(e).splitlines()[0])
            return
        problems = []
        try:
            o, n = oh.get('/api/data'), nh.get('/api/data')
            if o.status != 200 or n.status != 200:
                problems.append('/api/data HTTP old %s new %s' % (o.status, n.status))
            else:
                nd_ = n.data or {}
                if not nd_.get('success') or nd_.get('degraded') or nd_.get('errors'):
                    problems.append('new /api/data success=%s degraded=%s errors=%s'
                                    % (nd_.get('success'), nd_.get('degraded'), short(nd_.get('errors'))))
                a, b = self.norm_dash(o.data), self.norm_dash(n.data)
                for p, x, y in diff_obj(a, b, '$', limit=20):
                    problems.append('%s: old %s new %s' % (p, short(x, 80), short(y, 80)))
                    self.diff('dashboard /api/data', '/api/data', p, x, y)
                if not diff_obj(a, b):
                    self.ok('dashboard /api/data')
                self.ep('dashboard /api/data')['requests'] += 1
            r = nh.get('/')
            if r.status != 200 or '<html' not in r.text.lower():
                problems.append('new Dashboard page / HTTP %s' % r.status)
            # one chart per system through the Dashboard proxy
            for s in self.inv['systems']:
                if not s['registered'] or not s['dir']:       # the Dashboards show registered devices
                    continue
                for table, field in self._chart_fields(s['dir']):
                    path = seg('api', 'history', s['name'], table, field[1:] if field.startswith('/') else field)
                    # what the Dashboard page asks for on "All Available" (chart_max_points)
                    mp = max(10, min(self.args.dashboard_max_points, 10000))
                    params = {'from': DASHBOARD_ALL_FROM, 'to': utc_now_ts(), 'max_points': mp}
                    o, n = oh.get(path, params), nh.get(path, params)
                    pattern = 'dashboard /api/history/<system>/%s/<field>' % table
                    self.ep(pattern)['requests'] += 1
                    if o.status != 200 or n.status != 200:
                        self.diff(pattern, path, 'HTTP old %s new %s' % (o.status, n.status),
                                  short(o.data, 100), short(n.data, 100), system=s['name'],
                                  hint='dashboard-name' if n.status == 400 else None)
                        problems.append('%s chart %s: HTTP old %s new %s' % (s['name'], field, o.status, n.status))
                        continue
                    a = sort_points(norm_points((o.data or {}).get('data')))
                    b = sort_points(norm_points((n.data or {}).get('data')))
                    if (n.data or {}).get('downsampled'):
                        ok = not downsampled_problem(b, mp, series_stats_of(a))
                        if ok:
                            self.intended(pattern, 'N17')
                            continue
                    else:
                        ok = a == b
                    if ok:
                        self.ok(pattern)
                    else:
                        self.diff(pattern, path, '%s: chart data differs' % field, self._series_summary(a),
                                  self._series_summary(b), system=s['name'])
                        problems.append('%s chart %s differs' % (s['name'], field))
        finally:
            for h in (oh, nh):
                h.close()
            self.stop(od)
            self.stop(nd)
        if problems:
            self.check('dashboard', 'FAIL', '%d problems, e.g. %s' % (len(problems), problems[0]), problems=problems[:30])
        else:
            self.check('dashboard', 'PASS', 'NEW Dashboard shows the same systems, metrics, folders and totals '
                       'as the OLD one; charts load')

    def _chart_fields(self, d):
        out = []
        db = self.inv['dbs'].get('%s/data.db' % d)
        if not db:
            return out
        sm = db['tables'].get('system_metrics')
        if sm and 'cpu_percent' in sm['columns']:
            out.append(('system_metrics', 'cpu_percent'))
        fu = db['tables'].get('folder_usage')
        if fu:
            vols = [k for k in sorted(fu['folder']['keys'])
                    if len([p for p in k.replace('\\', '/').split('/') if p]) <= 1
                    and not k.endswith(('_total_bytes', '_free_bytes')) and k != 'total_usage']
            if vols:
                out.append(('folder_usage', vols[0]))
        return out

    def norm_dash(self, payload):
        """N13: presentation-only differences of the Dashboard's /api/data."""
        ap = self.applied

        def strip(x):
            if isinstance(x, dict):
                out = {}
                for k, v in x.items():
                    if isinstance(k, str) and k.endswith('_formatted'):
                        c = size_text_value(v)
                        if c is not None:
                            if 'iB' in v or ' B' in v:
                                ap.add('N13')
                            out[k] = c
                            continue
                    out[k] = strip(v)
                return out
            if isinstance(x, list):
                return [strip(v) for v in x]
            return x

        systems = {}
        for name, s in ((payload or {}).get('systems') or {}).items():
            s = strip(canon(s, ap))
            disk = s.get('disk')
            if isinstance(disk, dict):
                # new keys: the scan warnings the NEW Dashboard shows (the old one had none)
                for k in ('timed_out', 'errors', 'scan_seconds'):
                    if k in disk:
                        if disk.pop(k) not in ([], None):
                            ap.add('N13')
                # the OLD Dashboard listed numeric metadata keys of a newer collector
                # ("_scan_seconds") as volumes/folders; the NEW one skips "_" keys
                for k in ('volumes', 'folders'):
                    if isinstance(disk.get(k), list):
                        kept = [e for e in disk[k] if not (isinstance(e, dict)
                                                           and str(e.get('path', '')).startswith('_'))]
                        if len(kept) != len(disk[k]):
                            ap.add('N13')
                            disk[k] = kept
                for k in ('total_usage_bytes',):
                    if disk.get(k) in (0, None):
                        disk[k] = None
            m = s.get('metrics')
            if isinstance(m, dict):
                for k in ('total_ram_bytes',):
                    if m.get(k) in (0, None):
                        m[k] = None
            if isinstance(s.get('users'), list):
                s['users'] = sorted(s['users'], key=lambda u: str(u.get('username')))
            systems[name] = s
        return {'systems': systems, 'global_totals': canon((payload or {}).get('global_totals') or {}, ap)}

    # ---------------- ingest ---------------- #

    def check_ingest(self):
        say('Ingesting one new-format message per device into copy B (and A) ...')
        problems, done = [], []
        # newer than anything stored (collector clocks may run ahead of this server),
        # but never so far ahead that the NEW Manager treats the probe as a message
        # from a wrong clock: a stored time beyond that slack (an old message from a
        # collector with a wrong clock) no longer holds back current messages.
        now = datetime.now(timezone.utc)
        limit = now + timedelta(seconds=FUTURE_SLACK_SECONDS - 120)
        seen = [parse_ts(v) for row in (self.old_totals.get('devices') or {}).values() if isinstance(row, dict)
                for v in (row.get('last_updated'),)] + \
               [parse_ts(r.get('timestamp')) for r in self.old_metrics_all.values() if isinstance(r, dict)] + \
               [parse_ts(dv.get('last_seen')) for dv in self.inv['central']['devices']]
        seen = [t for t in seen if t is not None and t <= now + timedelta(seconds=FUTURE_SLACK_SECONDS)]
        if seen and max(seen) + timedelta(minutes=1) > now:
            now = min(max(seen) + timedelta(minutes=1), limit)
        totals_before = dict(self.old_totals.get('devices') or {})
        devices, frozen = self._ingest_targets()
        for i, s in enumerate(devices):
            name = s['name']
            t1 = now + timedelta(seconds=2 * i)
            ts1, ts2 = fmt_ts(t1), fmt_ts(t1 + timedelta(seconds=1))
            tb = totals_before.get(name) or {}
            lin, lout = tb.get('last_bytes_in') or 0, tb.get('last_bytes_out') or 0
            d_in, d_out = 123456789, 98765432
            metrics_old = self.old_metrics_all.get(name) or {}
            up = metrics_old.get('uptime_seconds') or 0
            last_ts = parse_ts(metrics_old.get('timestamp') or '')
            if last_ts is not None:
                up = int(up + max(0, (t1 - last_ts).total_seconds()))
            cpu = 12.5 + i
            header = {'device_name': name, 'device_id': s.get('system_id') or name,
                      'device_type': s.get('device_type') or 'unknown'}
            m1 = {'header': dict(header, msg_id=uuid.uuid4().hex, timestamp=ts1), 'data': {
                'data_type': 'system_metrics', 'cpu_percent': cpu, 'cpu_percent_unit': '%',
                'ram_percent': 33.3, 'ram_percent_unit': '%', 'total_ram_bytes': None,
                'uptime_seconds': up, 'uptime_seconds_unit': 's', 'uptime_formatted': '%dd' % (up // 86400),
                'network_bytes_in': lin + d_in, 'network_bytes_in_unit': 'bytes',
                'network_bytes_out': lout + d_out, 'network_bytes_out_unit': 'bytes',
                'network_bandwidth_in_mbps': 1.0, 'network_bandwidth_in_mbps_unit': 'Mbps',
                'network_bandwidth_out_mbps': 0.5, 'network_bandwidth_out_mbps_unit': 'Mbps',
                'gpus': [], 'gpu_count': 0}}
            msgs = [m1]
            probe_key = None
            snap = self.old_usage_all.get(name)
            if isinstance(snap, dict):
                data = {k: v for k, v in snap.items()
                        if k not in ('name', 'system_id', 'device_type', 'timestamp', 'total_disk_bytes', 'data_type')}
                vols = [k for k in data if isinstance(k, str) and not k.startswith('_')
                        and len([p for p in k.replace('\\', '/').split('/') if p]) <= 1
                        and not k.endswith(('_total_bytes', '_free_bytes')) and k != 'total_usage']
                base = vols[0] if vols else '/volume1'
                sep = '\\' if (':' in base or '\\' in base) else '/'
                probe_key = base.rstrip('/\\') + sep + 'verify-upgrade-probe'
                data.update({'data_type': 'folder_usage', probe_key: 4096, '_scan_seconds': 1.5,
                             '_timed_out': [], '_errors': []})
                msgs.append({'header': dict(header, msg_id=uuid.uuid4().hex, timestamp=ts2), 'data': data})
            batch = {'queue_id': '%s-%s' % (normalize_device_name(name), uuid.uuid4().hex[:12]),
                     'name': name, 'id': header['device_id'], 'messages': msgs}
            rb = self.new_admin.post('/api/data/queue', batch)
            ra = self.old.post('/api/data/queue', batch)
            if ra.status != 200 or (ra.data or {}).get('stored') != len(msgs):
                self.warn('OLD Manager did not store the probe for %s: HTTP %s %s' % (name, ra.status, short(ra.data, 120)))
            if rb.status != 200:
                problems.append('%s: NEW Manager refused the new-format batch: HTTP %s %s'
                                % (name, rb.status, short(rb.data, 120)))
                self.diff('POST /api/data/queue', '/api/data/queue', 'existing device %r cannot send data '
                          '(HTTP %s)' % (name, rb.status), 200, short(rb.data, 120), hint='ingest-name', system=name)
                continue
            counts = (rb.data or {}).get('counts') or {}
            if counts.get('stored') != len(msgs):
                problems.append('%s: NEW stored %s of %d (%s)' % (name, counts.get('stored'), len(msgs),
                                                                    short((rb.data or {}).get('results'), 200)))
                continue
            done.append(dict(name=name, dir=s['dir'], ts1=ts1, ts2=ts2, cpu=cpu, d_in=d_in, d_out=d_out,
                             lin=lin, lout=lout, probe=probe_key, batch=batch, before=tb))
        # verification
        t_new = self.new.get('/api/totals')
        t_old = self.old.get('/api/totals')
        ntot = canon((t_new.data or {}).get('devices') or {}) if t_new.status == 200 else {}
        otot = canon((t_old.data or {}).get('devices') or {}) if t_old.status == 200 else {}
        m_new = canon(self.new.get('/api/metrics/all').data or {})
        u_new = canon(self.new.get('/api/usage/all').data or {})
        for x in done:
            name = x['name']
            nt = ntot.get(name) or {}
            b = x['before']
            exp_in = (b.get('total_bytes_in') or 0) + x['d_in'] if b else x['lin'] + x['d_in']
            exp_out = (b.get('total_bytes_out') or 0) + x['d_out'] if b else x['lout'] + x['d_out']
            if not same_scalar(nt.get('total_bytes_in'), exp_in) or not same_scalar(nt.get('total_bytes_out'), exp_out):
                problems.append('%s: totals did not continue from the old values: in %s -> %s (expected %s), '
                                'out %s -> %s (expected %s)' % (name, b.get('total_bytes_in'), nt.get('total_bytes_in'),
                                                                 exp_in, b.get('total_bytes_out'),
                                                                 nt.get('total_bytes_out'), exp_out))
            ot = otot.get(name) or {}
            for k in ('total_bytes_in', 'total_bytes_out', 'last_bytes_in', 'last_bytes_out'):
                if not same_scalar(ot.get(k), nt.get(k)):
                    problems.append('%s: after the same message OLD %s=%s, NEW %s' % (name, k, ot.get(k), nt.get(k)))
            mt = (m_new.get(name) or {}).get('timestamp')
            if mt != x['ts1']:
                problems.append('%s: /api/metrics/all timestamp %s, expected %s' % (name, mt, x['ts1']))
            hname = name
            r = self.new.get(self._hist_path(hname, 'system_metrics', 'cpu_percent'), {'limit': 3})
            pts = norm_points((r.data or {}).get('data')) if r.status == 200 else []
            if not pts or pts[-1] != (x['ts1'], canon_num(x['cpu'])):
                problems.append('%s: newest cpu_percent point %s, expected %s' % (name, pts[-1:] or r.status,
                                                                                   (x['ts1'], x['cpu'])))
            st = self.series_stats.get((name, 'system_metrics', 'cpu_percent'))
            if st and st['total'] + 1 <= NEW_MAX_LIMIT:
                r = self.new.get(self._hist_path(hname, 'system_metrics', 'cpu_percent'),
                                 {'from': LOW_TS, 'to': HIGH_TS, 'limit': NEW_MAX_LIMIT})
                n_pts = len((r.data or {}).get('data') or [])
                if n_pts != st['total'] + 1:
                    problems.append('%s: cpu_percent history has %d points, expected the old %d + 1'
                                    % (name, n_pts, st['total']))
            if x['probe'] and x['dir']:
                r = self.new.get(seg('api', 'data', x['dir'], 'folder_usage'), {'limit': 2})
                rows = [norm_row(row, 'folder_usage') for row in ((r.data or {}).get('rows') or [])]
                if not rows or rows[0].get('timestamp') != x['ts2'] or rows[0].get(x['probe']) != 4096:
                    problems.append('%s: newest folder row is not the probe (%s)' % (name, short(rows[:1], 160)))
                elif len(rows) > 1 and self.folder_row_digests.get(x['dir']):
                    if self.folder_row_digests[x['dir']].get(rows[1].get('id')) != digest(rows[1]):
                        problems.append('%s: the previous newest folder row changed after the new ingest' % name)
                if (u_new.get(name) or {}).get(x['probe']) != 4096:
                    problems.append('%s: /api/usage/all does not show the new folder snapshot' % name)
            # re-send: duplicates, no side effects
            rr = self.new_admin.post('/api/data/queue', x['batch'])
            c = (rr.data or {}).get('counts') or {}
            if rr.status != 200 or c.get('duplicate') != len(x['batch']['messages']):
                problems.append('%s: re-sent batch not reported as duplicate: %s' % (name, short(c)))
        t2 = canon((self.new.get('/api/totals').data or {}).get('devices') or {})
        for x in done:
            if not same_scalar((t2.get(x['name']) or {}).get('total_bytes_in'),
                               (ntot.get(x['name']) or {}).get('total_bytes_in')):
                problems.append('%s: totals changed on a duplicate re-send' % x['name'])
        # N16: the other spellings of a normalized name keep their rows unchanged
        for name, current in frozen:
            before = canon(totals_before.get(name) or {})
            after = t2.get(name) or {}
            for k in ('total_bytes_in', 'total_bytes_out', 'last_bytes_in', 'last_bytes_out', 'total_disk_bytes'):
                if k in before and not same_scalar(before.get(k), after.get(k)):
                    problems.append('%s: device_totals.%s changed (%s -> %s) although new data for %r '
                                    'is filed under %r' % (name, k, before.get(k), after.get(k),
                                                           normalize_device_name(name), current))
            self.intended('POST /api/data/queue', 'N16')
        if self.args.with_dashboard and done:
            try:
                nd, nh = self.start_dashboard('new', self.new_proc.port, self.read_token)
                try:
                    r = nh.get('/api/data')
                    systems = (r.data or {}).get('systems') or {}
                    for x in done:
                        ts = canon_ts(((systems.get(x['name']) or {}).get('metrics') or {}).get('timestamp'))
                        if ts != x['ts1']:
                            problems.append('%s: NEW Dashboard does not show the new metrics (%s)' % (x['name'], ts))
                finally:
                    nh.close()
                    self.stop(nd)
            except SetupError as e:
                problems.append('NEW Dashboard did not start for the ingest check: %s' % str(e).splitlines()[0])
        if problems:
            self.check('ingest', 'FAIL', '%d problems, e.g. %s' % (len(problems), problems[0]), problems=problems)
        else:
            self.check('ingest', 'PASS', '%d devices: new data stored next to the old history, totals continue '
                       'from the old values, re-send is a duplicate' % len(done))

    def _ingest_targets(self):
        """
        Registered devices to send a new message for, and [(spelling, current)] of
        the spellings that get none.  An older Manager keyed the central rows by
        the raw name, so two spellings may normalize alike (N16): the NEW Manager
        files data under exactly the registered spelling it was sent under (a new
        spelling under the most recently seen one, metrics.resolve_name); the
        probe goes to the most recent spelling and the other rows must not change.
        """
        groups = defaultdict(list)
        for s in self.inv['systems']:
            if s['registered']:
                groups[normalize_device_name(s['name'])].append(s)
        devs = {dv['name']: dv for dv in self.inv['central']['devices']}

        def key(s):
            dv = devs.get(s['name']) or {}
            return (parse_ts(dv.get('last_seen')) or datetime(1970, 1, 1, tzinfo=timezone.utc),
                    parse_ts(dv.get('first_seen')) or datetime(1970, 1, 1, tzinfo=timezone.utc), s['name'])

        targets, frozen = [], []
        for s in self.inv['systems']:
            if not s['registered']:
                continue
            group = groups[normalize_device_name(s['name'])]
            current = max(group, key=key)
            if current is s:
                targets.append(s)
            else:
                frozen.append((s['name'], current['name']))
        return targets, frozen

    def check_legacy_resend(self):
        """
        An OLD collector that re-sends what the old Manager already stored (its
        queue at upgrade time, a timed-out batch, an archive replay) sends no
        msg_id.  Re-send the newest stored row of each history table in the old
        format: the NEW Manager must answer "duplicate" and store nothing.
        Then re-send them once more as a collector upgraded BEFORE the Manager
        did (each with its own random msg_id, which the old Manager dropped):
        again all "duplicate".
        """
        say('Re-sending already-stored messages in the OLD format (no msg_id) to copy B ...')
        problems, sent, sent_ids, gap_filled = [], 0, 0, []
        for s in self.inv['systems']:
            name, d = s['name'], s['dir']
            if name is None or not d:
                continue
            msgs, expect = [], []
            tables = (self.inv['dbs'].get('%s/data.db' % d) or {}).get('tables', {})
            for table, info in (self.inv['dbs'].get('%s/data.db' % d) or {}).get('tables', {}).items():
                row = info.get('newest_row')
                if not row or table not in ('system_metrics', 'user_metrics'):
                    continue
                data = {k: v for k, v in row.items() if k.lower() not in INTERNAL_COLS and v is not None and v != ''}
                data['data_type'] = table
                msgs.append({'header': {'device_name': name, 'device_id': s.get('system_id') or name,
                                        'device_type': row.get('device_type') or 'unknown',
                                        'timestamp': row['timestamp']}, 'data': data})
                expect.append('duplicate')
            snap = (getattr(self, 'old_usage_all_raw', {}) or {}).get(name)
            if isinstance(snap, dict) and snap.get('timestamp'):
                data = {k: v for k, v in snap.items()
                        if k not in ('name', 'system_id', 'device_type', 'timestamp', 'total_disk_bytes')}
                data['data_type'] = 'folder_usage'
                msgs.append({'header': {'device_name': name, 'device_id': s.get('system_id') or name,
                                        'device_type': snap.get('device_type') or 'unknown',
                                        'timestamp': snap['timestamp']}, 'data': data})
                # The old Manager updated the snapshot even when its history insert
                # failed (e.g. a folder key colliding case-insensitively with a column):
                # such a scan is not in the history, so a re-send must be STORED now.
                fu = (tables.get('folder_usage') or {}).get('folder') or {}
                in_history = canon_ts(snap['timestamp']) in set(fu.get('timestamps') or [])
                expect.append('duplicate' if in_history else 'stored')
                if not in_history:
                    gap_filled.append('%s folder_usage %s' % (name, snap['timestamp']))
            if not msgs:
                continue
            before = self._b_counts(d)
            batch = {'queue_id': '%s-%s' % (name, datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')),
                     'name': name, 'id': s.get('system_id') or name, 'messages': msgs}
            r = self.new_admin.post('/api/data/queue', batch)
            sent += len(msgs)
            after = self._b_counts(d)
            if r.status != 200:
                problems.append('%s: HTTP %s %s' % (name, r.status, short(r.data, 120)))
                continue
            results = (r.data or {}).get('results') or []
            stored = [m['data']['data_type'] for m, res, want in zip(msgs, results, expect)
                      if res.get('status') == 'stored' and want == 'duplicate']
            missed = [m['data']['data_type'] for m, res, want in zip(msgs, results, expect)
                      if want == 'stored' and res.get('status') != 'stored']
            grown = {'folder_usage_msgs': sum(1 for m, want in zip(msgs, expect)
                                              if want == 'stored' and m['data']['data_type'] == 'folder_usage')}
            expected_after = {t: n + grown.get(t, 0) for t, n in before.items()}
            if stored or after != expected_after:
                problems.append('%s: re-sent old messages were stored AGAIN (%s); history rows %s -> %s'
                                % (name, ', '.join(stored) or '?', before, after))
            if missed:
                problems.append('%s: a re-sent message the old Manager never kept in its history was not '
                                'stored (%s): %s' % (name, ', '.join(missed), short(results, 200)))
            if (r.data or {}).get('stored') != len(msgs):
                problems.append('%s: an OLD collector would keep its queue: stored=%s of %d'
                                % (name, (r.data or {}).get('stored'), len(msgs)))
            # The rows the old Manager stored, re-sent with a NEW collector's random ids
            again = [dict(m, header=dict(m['header'], msg_id=uuid.uuid4().hex))
                     for m, want in zip(msgs, expect) if want == 'duplicate']
            if not again:
                continue
            batch = {'queue_id': '%s-%s' % (normalize_device_name(name), uuid.uuid4().hex[:12]),
                     'name': name, 'id': s.get('system_id') or name, 'messages': again}
            r = self.new_admin.post('/api/data/queue', batch)
            sent_ids += len(again)
            after_ids = self._b_counts(d)
            if r.status != 200:
                problems.append('%s: re-send with msg_ids: HTTP %s %s' % (name, r.status, short(r.data, 120)))
                continue
            results = (r.data or {}).get('results') or []
            if [res.get('status') for res in results] != ['duplicate'] * len(again) or after_ids != after:
                problems.append('%s: re-sent with a new collector\'s random msg_id, messages the old Manager '
                                'stored were stored AGAIN (%s); history rows %s -> %s'
                                % (name, short(results, 160), after, after_ids))
        if problems:
            self.check('legacy-resend', 'FAIL', '%d problems, e.g. %s' % (len(problems), problems[0]),
                       problems=problems, suspected_cause='manager/data_store.py store_message: duplicates '
                       'of pre-upgrade rows (msg_id NULL) are not detected')
        else:
            self.check('legacy-resend', 'PASS', '%d already-stored messages re-sent without msg_id and %d '
                       'with a new collector\'s random msg_id: all acknowledged as duplicates, nothing '
                       'stored twice%s' % (
                           sent, sent_ids, ('; %d the old Manager never kept in its history now stored (%s)'
                                            % (len(gap_filled), '; '.join(gap_filled[:3]))) if gap_filled else ''),
                       gap_filled=gap_filled, resent_with_ids=sent_ids)

    def _b_counts(self, d):
        path = self.copy_b / d / 'data.db'
        conn = ro_connect(path)
        try:
            out = {}
            for t in db_tables(conn):
                if t in ('system_metrics', 'user_metrics', 'folder_usage_msgs'):
                    out[t] = conn.execute('SELECT COUNT(*) FROM %s' % qi(t)).fetchone()[0]
            return out
        finally:
            conn.close()

    # ---------------- backups / files / source ---------------- #

    def check_backups(self):
        now_files = list_files(self.copy_b)
        new_files = set(now_files - self.b_files_initial)
        # a backup already in the data_dir (an earlier upgrade attempt) counts if the NEW
        # Manager reused it because nothing was written since: it must hold the original
        cands = [f for f in sorted(now_files) if BACKUP_NAME_RE.search(f.split('/')[-1])
                 and '.post-upgrade-' not in f and is_sqlite(self.copy_b / f)]
        cand_hash = {}
        for f in cands:
            try:
                cand_hash[f] = combine_hash(logical_hash(self.copy_b / f))
            except sqlite3.Error as e:
                cand_hash[f] = 'error: %s' % e
        results, problems = OrderedDict(), []
        for rel, dbinv in self.inv['dbs'].items():
            orig_tables = {t: info['columns'] for t, info in dbinv['tables'].items()}
            conn = ro_connect(self.copy_b / rel)
            try:
                now_tables = db_tables(conn)
            finally:
                conn.close()
            changed = set(now_tables) != set(orig_tables) or any(
                now_tables[t]['columns'] != cols for t, cols in orig_tables.items() if t in now_tables)
            orig_hash = combine_hash({t: info.get('hash') for t, info in dbinv['tables'].items()}) \
                if all('hash' in i for i in dbinv['tables'].values()) else None
            mine = [f for f in cands if _backup_belongs(f, rel)]
            matching = [f for f in mine if orig_hash and cand_hash.get(f) == orig_hash]
            results[rel] = {'schema_changed': changed, 'backups': mine, 'matching_original': matching}
            if changed and not mine:
                problems.append('%s was altered by the NEW Manager but has no pre-upgrade backup' % rel)
            elif changed and orig_hash and not matching:
                problems.append('%s: backup(s) %s do not contain the original content' % (rel, mine))
            taken = [f for f in mine if f in new_files]
            if len(taken) > 1:
                self.warn('%s has %d new backups after a restart (expected one)' % (rel, len(taken)))
        self.report['backups'] = results
        if problems:
            self.check('backups', 'FAIL', '%d databases: %s' % (len(problems), problems[0]), problems=problems,
                       suspected_cause=HINTS['backup'])
        else:
            n = sum(1 for rel, r in results.items()
                    if r['matching_original'] and self.inv['dbs'][rel]['kind'] in ('central', 'system'))
            self.check('backups', 'PASS', '%d databases have a pre-upgrade backup identical to the original' % n)

    def check_rollback(self):
        """
        The owner's way back: tools/rollback_upgrade.py on the migrated copy B
        (after everything else: the NEW Manager stopped, new messages ingested),
        then the OLD Manager on it.  Every database must hold exactly its
        original content again, and every OLD answer recorded before any write
        must come back identical.
        """
        tool = self.new_code / 'tools' / 'rollback_upgrade.py'
        if not tool.is_file():
            self.check('rollback', 'WARN', 'the NEW code has no tools/rollback_upgrade.py: rollback not checked')
            return
        say('Rolling copy B back to its pre-upgrade backups (tools/rollback_upgrade.py) ...')
        r = subprocess.run([sys.executable, str(tool), '--data-dir', str(self.copy_b), '--apply', '--force'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env(), timeout=3600)
        out = r.stdout.decode('utf-8', 'replace').strip().splitlines()
        self.report['rollback_tool_output'] = out[-60:]
        if r.returncode != 0:
            self.check('rollback', 'FAIL', 'tools/rollback_upgrade.py --apply failed (exit %s): %s'
                       % (r.returncode, ' | '.join(out[-3:])), output=out[-30:])
            return
        problems = []
        for rel, dbinv in self.inv['dbs'].items():
            want = {t: info.get('hash') for t, info in dbinv['tables'].items()}
            path = self.copy_b / rel
            if not path.is_file():
                problems.append('%s: missing after the rollback' % rel)
                continue
            if None in want.values():          # --no-hash: row counts only
                conn = ro_connect(path)
                try:
                    got = {t: conn.execute('SELECT COUNT(*) FROM %s' % qi(t)).fetchone()[0] for t in db_tables(conn)}
                finally:
                    conn.close()
                if got != {t: info['rows'] for t, info in dbinv['tables'].items()}:
                    problems.append('%s: row counts differ from the original after the rollback' % rel)
                continue
            got = logical_hash(path)
            if got != want:
                bad = sorted(t for t in set(got) | set(want) if got.get(t) != want.get(t))
                problems.append('%s: content differs from the original after the rollback (tables %s)'
                                % (rel, ', '.join(bad[:6])))
        moved = [f for f in list_files(self.copy_b) if '.post-upgrade-' in f]
        replayed, changed = 0, []
        if not problems:
            try:
                proc, _secs = self.start_manager('old', self.copy_b)
            except SetupError as e:
                self.check('rollback', 'FAIL', 'the OLD Manager did not start on the rolled-back copy: %s'
                           % str(e).splitlines()[0])
                return
            h = Http('127.0.0.1', proc.port, self.old_token, self.args.request_timeout)
            self.applied.enabled = False
            try:
                for key, rec in self.old_answers.items():
                    resp = h.get(rec['path'], rec['params'])
                    replayed += 1
                    if resp.status != rec['status']:
                        changed.append('%s: HTTP %s -> %s' % (rec['path'], rec['status'], resp.status))
                    elif resp.status == 200 and digest(rec['normalize'](resp.data)) != rec['digest']:
                        changed.append('%s: answer differs' % rec['path'])
            finally:
                self.applied.enabled = True
                h.close()
                self.stop(proc)
            problems.extend(changed)
        if problems:
            self.check('rollback', 'FAIL', '%d problems after the rollback, e.g. %s' % (len(problems), problems[0]),
                       problems=problems[:30], tool_output=out[-20:])
        else:
            self.check('rollback', 'PASS', 'tools/rollback_upgrade.py restored every database to its original '
                       'content (%d post-upgrade files kept aside); the OLD Manager on it gave the same %d '
                       'answers as before the upgrade' % (len(moved), replayed))

    def check_other_files(self):
        bad = []
        for c in self.copied:
            if c['kind'] != 'file':
                continue
            want = self.fp_before['files'].get(c['file'], {}).get('sha256')
            p = self.copy_b / c['file']
            got = sha256_file(p) if p.is_file() else None
            if want != got:
                bad.append(c['file'])
        if bad:
            self.check('files', 'FAIL', 'the NEW Manager changed or removed non-database files: %s' % bad[:5])
        else:
            n = sum(1 for c in self.copied if c['kind'] == 'file')
            self.check('files', 'PASS', '%d other files (usage.jsonl, ...) byte-identical in copy B after the run' % n)

    def check_source_unchanged(self):
        cmp_ = compare_fingerprints(self.fp_before, self.fp_after)
        self.report['source_changes'] = cmp_
        touched = cmp_['added'] or cmp_['removed'] or cmp_['changed'] or cmp_['dirs_added'] or cmp_['dirs_removed']
        n = len(self.fp_before['files'])
        size = sum(f.get('size', 0) for f in self.fp_before['files'].values())
        if not touched:
            self.check('source', 'PASS', 'data_dir unchanged: %d files, %.2f GB (size, mtime, sha256)' % (n, size / 1e9))
            return
        db_like = re.compile(r'\.db(-wal|-shm|-journal)?$')
        only_db = all(db_like.search(f) for f in cmp_['added'] + cmp_['removed'] +
                      [c['file'] for c in cmp_['changed']]) and not cmp_['dirs_added']
        if only_db and self.live_manager:
            self.check('source', 'WARN', 'database files in the data_dir changed during the run while the live '
                       'Manager is running (it keeps storing collector data); this tool only opens them '
                       'read-only. Stop the Manager task for a strict byte-identical check.', changes=cmp_)
        else:
            self.check('source', 'FAIL', 'the data_dir changed during the run: %s' % short(cmp_, 400), changes=cmp_)

    def scan_log(self, name, phase):
        path = self.work / 'logs' / name
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return
        bad = [ln for ln in lines if ' - ERROR - ' in ln or ' - CRITICAL - ' in ln or 'Traceback' in ln]
        migr = [ln for ln in lines if 'migrat' in ln.lower() or 'backup' in ln.lower()]
        self.report.setdefault('new_manager_log', {})[phase] = {'errors': bad[:30], 'migration': migr[:60]}
        if bad and phase == 'whole run':
            self.warn('NEW Manager logged %d errors (see report new_manager_log), e.g. %s' % (len(bad), bad[0][:200]))

    # ---------------- report ---------------- #

    def finish(self, setup_error=None):
        r = self.report
        r['finished'] = utc_now_ts()
        r['checks'] = self.checks
        eps = OrderedDict()
        for k in sorted(self.endpoints):
            e = self.endpoints[k]
            eps[k] = {'requests': e['requests'], 'equal': e['equal'], 'intended': dict(e['intended']),
                      'different': e['different'], 'examples': e['examples']}
        r['endpoints'] = eps
        r['differences'] = self.differences[:500]
        r['differences_total'] = len(self.differences)
        r['intended_differences'] = OrderedDict(
            (k, {'title': t, 'reason': why, 'applied': self.applied.counts.get(k, 0)}) for k, (t, why) in INTENDED.items())
        r['sampling'] = self.sampling
        r['warnings'] = self.warnings
        r['timings_seconds'] = self.timings
        if setup_error:
            r['result'] = 'ERROR'
            r['error'] = setup_error
        else:
            failed = [k for k, c in self.checks.items() if c['status'] == 'FAIL']
            r['result'] = 'FAIL' if failed or self.differences else 'PASS'
            r['failed_checks'] = failed
        return r

    def print_summary(self):
        r = self.report
        say('')
        say('=' * 78)
        say('Lab Monitor upgrade verification - %s' % r.get('result'))
        say('=' * 78)
        say('data_dir (read only): %s' % r.get('inputs', {}).get('data_dir'))
        code = r.get('code', {})
        if code:
            say('OLD code: %s   NEW code: %s%s' % (
                code.get('old', {}).get('commit') or code.get('old', {}).get('path'),
                (code.get('new', {}).get('commit') or '?')[:12],
                ' (+uncommitted changes)' if code.get('new', {}).get('uncommitted_changes') else ''))
        say('')
        for cid, c in self.checks.items():
            say('  %-5s %-10s %s' % (c['status'], cid, c['summary'][:300]))
        say('')
        say('Endpoints (OLD vs NEW):')
        for k, e in r.get('endpoints', {}).items():
            intended = ', '.join('%s x%d' % kv for kv in sorted(e['intended'].items()))
            say('  %-70s %5d req  %s%s' % (k[:70], e['requests'],
                                           'DIFFERENT x%d' % e['different'] if e['different'] else 'ok',
                                           ('  [intended: %s]' % intended) if intended else ''))
        if self.differences:
            say('')
            say('Unexpected differences (%d), by endpoint:' % len(self.differences))
            by_ep = defaultdict(list)
            for d in self.differences:
                by_ep[d['endpoint']].append(d)
            for ep, items in by_ep.items():
                say('  %s  (%d)' % (ep, len(items)))
                for d in items[:4]:
                    say('    - %s %s: %s' % (d.get('system') or '', d['request'][:100], d['what'][:160]))
                    say('        old: %s' % d['old'][:200])
                    say('        new: %s' % d['new'][:200])
                    if d.get('suspected_cause'):
                        say('        cause?: %s' % d['suspected_cause'][:200])
        if self.warnings:
            say('')
            say('Warnings:')
            for w in self.warnings[:20]:
                say('  - %s' % w[:300])
        applied = [(k, self.applied.counts[k]) for k in INTENDED if self.applied.counts.get(k)]
        if applied:
            say('')
            say('Intended differences normalized: %s' % ', '.join('%s %s (x%d)' % (k, INTENDED[k][0], n)
                                                                    for k, n in applied))
        say('')
        say('RESULT: %s' % r.get('result'))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description='Verify that existing Manager data stays readable after the upgrade '
                    '(OLD vs NEW Manager on copies of your data_dir; the data_dir is never written).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Exit status: 0 PASS, 1 differences found, 2 could not run.\n'
               'See tools/README.md "verify_upgrade.py" for what each check means.')
    ap.add_argument('--data-dir', help='the live Manager data_dir (only read)')
    ap.add_argument('--manager-config', help='the live Manager config.json (data_dir is taken from it, '
                                             'and the NEW Manager --check-config is run on a copy of it)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--old-ref', default=DEFAULT_OLD_REF, help='git ref of the OLD code (default %(default)s)')
    g.add_argument('--old-code', help='an extracted OLD checkout instead of git archive')
    ap.add_argument('--new-code', help='NEW checkout to test (default: the checkout this tool is in)')
    ap.add_argument('--repo', help='git repository for --old-ref (default: the checkout this tool is in)')
    ap.add_argument('--work-dir', help='where copies, code and logs go (a new subdirectory is created; '
                                       'default: the system temp dir). Needs about 4x the data_dir size.')
    ap.add_argument('--keep-work', action='store_true', help='keep the work subdirectory afterwards')
    ap.add_argument('--report', help='write the JSON report here')
    ap.add_argument('--with-dashboard', action='store_true', help='also compare the OLD and NEW Dashboards')
    ap.add_argument('--max-folders', type=int, default=200,
                    help='folder keys per system charted via /api/history (volumes and totals are always '
                         'included; every folder value is still compared via the row check). -1 = all. '
                         'Default %(default)s')
    ap.add_argument('--window-cells', type=int, default=500000,
                    help='rows x columns per compared time window (default %(default)s)')
    ap.add_argument('--tail-limit', type=int, default=500, help='points in tail history queries (default 500)')
    ap.add_argument('--folder-tail-limit', type=int, default=60,
                    help='points in folder_usage tail history queries (default %(default)s)')
    ap.add_argument('--user-history-limit', type=int, default=20000,
                    help='newest rows compared per user via /api/user_metrics/history (default %(default)s)')
    ap.add_argument('--dashboard-max-points', type=int, default=2000, help='max_points of the Dashboard query check')
    ap.add_argument('--old-history-windows', action='store_true',
                    help='ask the OLD /api/history endpoint for every field and window too (by default the OLD '
                         'full-range answer is computed from the OLD Manager\'s rows of the same window, which '
                         'is the same query; this option re-reads every row once per field: slow on big data)')
    ap.add_argument('--startup-timeout', type=float, default=300, help='seconds to wait for /health')
    ap.add_argument('--request-timeout', type=float, default=1800,
                    help='seconds per HTTP request (the first read of a big system runs its migration)')
    ap.add_argument('--no-hash', action='store_true', help='skip per-table content hashes (faster; '
                                                           'storage/backup checks then compare row counts only)')
    ap.add_argument('--no-quick-check', action='store_true', help='skip PRAGMA quick_check on the copies')
    ap.add_argument('--ignore-space', action='store_true', help='run even if the free-space estimate is too low')
    return ap.parse_args(argv)


def _on_signal(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors='backslashreplace')
    except (AttributeError, ValueError):
        pass
    import signal
    for sig in ('SIGTERM', 'SIGBREAK', 'SIGHUP'):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _on_signal)   # stop the child processes too
            except (ValueError, OSError):
                pass
    args = parse_args(argv)
    v = Verifier(args)
    error = None
    try:
        v.run()
    except SetupError as e:
        error = str(e)
        say('ERROR: %s' % error)
    except KeyboardInterrupt:
        error = 'interrupted'
        say('Interrupted.')
    except Exception as e:      # a bug in this tool: report it, never touch the data
        error = '%s: %s' % (type(e).__name__, e)
        say('ERROR: unexpected failure in verify_upgrade.py:')
        say(traceback.format_exc())
    finally:
        v.stop_all()
    report = v.finish(error)
    if args.report:
        try:
            with open(args.report, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=True, default=str)
        except OSError as e:
            say('ERROR: could not write the report: %s' % e)
    v.print_summary()
    if args.report:
        say('Report: %s' % os.path.abspath(args.report))
    work = getattr(v, 'work', None)
    if work is not None:
        if args.keep_work:
            say('Work directory kept: %s' % work)
        else:
            shutil.rmtree(str(work), ignore_errors=True)
            if report['result'] != 'PASS':
                say('(work directory removed; re-run with --keep-work to inspect the copies and logs)')
    if error:
        return 2
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
