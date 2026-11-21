#!/usr/bin/env python3
"""
Lab Monitor - Replay Collector Archives into the Manager

Reads collector archive files (YYYY-MM.jsonl: one {"header": ..., "data": ...}
message per line, exactly as collector.py writes them) and POSTs the messages
back to the Manager's POST /api/data/queue in batches, the same way a
collector sends its queue: {queue_id, name, id, messages}.

Replaying is safe to repeat: the Manager de-duplicates on header.msg_id, so
messages it already holds come back as "duplicate" and are not stored again.
Messages are sent unchanged — this tool never adds a msg_id (a new random id
would defeat that de-duplication).

Messages WITHOUT a msg_id (written before the collector's de-duplication
upgrade) are skipped unless --include-legacy is given. The Manager derives an
id for them, but the history rows it stored before it gained de-duplication
carry no id, so replaying them into a Manager that still holds those rows
stores them a second time. Use --include-legacy only for a system wiped by
reset_system.py, or a Manager that never received that period.
--since/--until limit the replay to a time window (header timestamps,
ISO 8601, compared as text).

Dashboard card: the Manager shows the last message it stored on the device
card (metrics, disk, "last seen") whatever its timestamp, and its network
totals skip messages older than the newest it has. Replaying older messages
into a live system therefore rolls the card back. Pause the collector first
(toggle_collectors.py --mode off, or "active": false in its config), replay,
then resume it and run it once in metrics and disk mode
(run_collectors.py --mode metrics / --mode disk) to show current values.

Typical uses:
    - After reset_system.py: rebuild the system from its backup folder
      (with --include-legacy; reset_system.py prints the command)
    - Re-send a backup folder (archive_all_collectors.py, reset_system.py)
      to a Manager that lost that data
    - Fill a gap after a Manager outage or a restore from an old backup

Usage (run where the archive files are, e.g. on the collector):
    python3 replay_archives.py --config /path/to/collector/config.json --source /volume1/lab-monitor/data/Triton/backup_20260806_084530
    python3 replay_archives.py --config config.json --source data/Triton/2026-08.jsonl --dry-run
    python3 replay_archives.py --manager-url http://atlantis.med.harvard.edu:5000 --token <token> --source backup_dir/
    python3 replay_archives.py --config config.json --source data/Triton --since 2026-08-04T00:00 --until 2026-08-05

Sources:
    --source takes files or directories (repeatable). A directory contributes
    its YYYY-MM.jsonl files, oldest month first. queue.json is only read when
    named explicitly — every queued message is also in the archives. Files
    are read in the order given and streamed line by line, so memory use does
    not grow with the archive size; messages are sent in file order
    (collectors append them chronologically), so give older sources first.

Authentication:
    A collector token (manager config "collector_tokens") is accepted only for
    that collector's own device name; to replay several systems at once, or
    another system's archives, use an admin token.

Exit code: 0 every message stored or already present, 1 bad arguments / no
messages / authentication refused, 2 some messages could not be delivered
(transient errors — re-run to retry). Messages the Manager rejects as
permanently invalid are reported (the Manager keeps them in its dead_letter
table) but do not change the exit code.

Requirements:
    pip install requests
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


DEFAULT_BATCH = 100        # contract: collector "max_batch_messages" default
DEFAULT_TIMEOUT = 30       # contract: collector "request_timeout_seconds" default
_ARCHIVE_GLOB = '[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl'


class AuthRefused(Exception):
    """The Manager refused the token (401/403); retrying will not help."""


# ---------------------------------------------------------------------------
# Reading sources
# ---------------------------------------------------------------------------

def source_files(sources: list) -> list:
    """Expand --source arguments into a list of files (directories → their YYYY-MM.jsonl)."""
    files = []
    for src in sources:
        path = Path(src)
        if path.is_dir():
            files.extend(sorted(path.glob(_ARCHIVE_GLOB)))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"source not found: {src}")
    return files


class ReadStats:
    """What select_messages() read and skipped; filled in while streaming."""

    def __init__(self, keep_examples: int = 20):
        self.read = 0               # {header, data} messages read
        self.bad_lines = 0          # lines/entries that are not such messages
        self.bad_examples = []      # the first few "file:line: reason"
        self.outside_window = 0     # outside --since/--until
        self.legacy = 0             # no msg_id, skipped (no --include-legacy)
        self.repeated = 0           # msg_id already selected earlier in this run
        self.selected = 0
        self._keep = keep_examples

    def bad(self, reason: str):
        self.bad_lines += 1
        if len(self.bad_examples) < self._keep:
            self.bad_examples.append(reason)


def _jsonl_items(path: Path, stats: ReadStats):
    """Yield (line number, parsed JSON) for each non-empty line, reading line by line."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError as e:
                stats.bad(f"{path}:{i}: not valid JSON ({e.msg})")


def iter_messages(path: Path, stats: ReadStats):
    """
    Yield the {header, data} messages of one archive (.jsonl, streamed) or
    queue (.json list) file. Anything else is counted in stats.
    """
    if path.suffix.lower() == '.json':
        # A queue file is a single JSON list, bounded by the collector's queue.
        text = path.read_text(encoding='utf-8', errors='replace')
        try:
            items = json.loads(text) if text.strip() else []
        except json.JSONDecodeError as e:
            stats.bad(f"{path}: not valid JSON ({e})")
            return
        if not isinstance(items, list):
            stats.bad(f"{path}: expected a JSON list of messages")
            return
        numbered = enumerate(items, 1)
    else:
        numbered = _jsonl_items(path, stats)

    for i, item in numbered:
        if (isinstance(item, dict) and isinstance(item.get('header'), dict)
                and isinstance(item.get('data'), dict)):
            stats.read += 1
            yield item
        else:
            stats.bad(f"{path}:{i}: not a {{header, data}} message (old/legacy entry?)")


def in_window(msg: dict, since: str = None, until: str = None) -> bool:
    """
    True if the message's header timestamp is within [since, until). Bounds
    are ISO 8601 prefixes compared as text ('2026-08-04' or
    '2026-08-04T07:00'), which orders correctly for the collector's
    'YYYY-MM-DDTHH:MM:SS...Z' timestamps.
    """
    ts = str(msg['header'].get('timestamp') or '')
    if since and ts < since:
        return False
    if until and ts >= until:
        return False
    return True


def select_messages(files: list, stats: ReadStats, since: str = None, until: str = None,
                    include_legacy: bool = False):
    """
    Stream the messages to replay from files, in order. Drops messages outside
    [since, until), messages without a msg_id (unless include_legacy) and
    repeats of a msg_id already selected. Only the msg_id set stays in memory.
    Prints a line per file once it has been read.
    """
    seen_ids = set()
    for path in files:
        read_before, bad_before = stats.read, stats.bad_lines
        for msg in iter_messages(path, stats):
            if not in_window(msg, since, until):
                stats.outside_window += 1
                continue
            msg_id = msg['header'].get('msg_id')
            if not msg_id:
                if not include_legacy:
                    stats.legacy += 1
                    continue
            else:
                msg_id = str(msg_id)
                if msg_id in seen_ids:
                    stats.repeated += 1
                    continue
                seen_ids.add(msg_id)
            stats.selected += 1
            yield msg
        n_bad = stats.bad_lines - bad_before
        print(f"  Read {stats.read - read_before:6d} message(s) from {path}"
              + (f" ({n_bad} unreadable line(s) skipped)" if n_bad else ''), flush=True)


def device_key(msg: dict, default_name: str = None, default_id: str = None) -> tuple:
    """
    (name, id) a message is sent under. The Manager requires each message's
    device name to match the payload "name", so each device gets its own batches.
    """
    header = msg['header']
    name = header.get('device_name') or default_name
    return name, header.get('device_id') or default_id or name


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _new_counts() -> dict:
    return dict(stored=0, duplicate=0, rejected=0, error=0, undelivered=0, legacy_ack=0)


def new_queue_id(name: str) -> str:
    """Contract: queue_id = f"{name}-{uuid4().hex[:12]}"."""
    return f"{name}-{uuid.uuid4().hex[:12]}"


def _post(session, url: str, token: str, payload: dict, timeout: float):
    return session.post(url, json=payload, timeout=timeout,
                        headers={'Authorization': f'Bearer {token}',
                                 'Content-Type': 'application/json'})


def probe_manager(session, url: str, token: str, name: str, dev_id: str, timeout: float) -> str:
    """
    POST an empty batch to learn whether the Manager de-duplicates and accepts
    the token for this device. Returns 'dedup', 'legacy' (old Manager: no
    per-message results, so replays would store duplicates) or 'unknown'.
    Raises AuthRefused on 401/403.
    """
    payload = {'queue_id': new_queue_id(name), 'name': name, 'id': dev_id, 'messages': []}
    try:
        resp = _post(session, url, token, payload, timeout)
    except requests.exceptions.RequestException:
        return 'unknown'
    if resp.status_code in (401, 403):
        raise AuthRefused(f"Manager refused the token for '{name}' (HTTP {resp.status_code})")
    if resp.status_code != 200:
        return 'unknown'
    try:
        body = resp.json()
    except ValueError:
        return 'unknown'
    return 'dedup' if isinstance(body, dict) and 'results' in body else 'legacy'


def send_batch(session, url: str, token: str, name: str, dev_id: str, batch: list,
               timeout: float, retries: int = 3, retry_delay: float = 5.0) -> dict:
    """
    Send one batch, retrying transient failures. Returns counts
    {'stored', 'duplicate', 'rejected', 'error', 'undelivered', 'legacy_ack'}:
    'error' are messages the Manager reported as transient errors on the last
    attempt; 'undelivered' could not be sent at all. A 413 (body too large)
    splits the batch in half.
    """
    counts = _new_counts()
    pending = list(batch)
    attempt = 0
    while pending:
        attempt += 1
        queue_id = new_queue_id(name)
        payload = {'queue_id': queue_id, 'name': name, 'id': dev_id, 'messages': pending}
        reported_errors = False     # True when the Manager answered but flagged transient errors
        try:
            resp = _post(session, url, token, payload, timeout)
        except requests.exceptions.RequestException as e:
            resp, problem = None, f"{type(e).__name__}: {e}"
        else:
            problem = None

        if resp is not None:
            if resp.status_code in (401, 403):
                raise AuthRefused(f"Manager refused the token for '{name}' (HTTP {resp.status_code})")
            if resp.status_code == 413:
                if len(pending) == 1:
                    print(f"    [!] message {pending[0]['header'].get('msg_id') or '?'} is larger "
                          f"than the Manager's max_request_bytes; not sent")
                    counts['undelivered'] += 1
                    return counts
                half = len(pending) // 2
                for part in (pending[:half], pending[half:]):
                    sub = send_batch(session, url, token, name, dev_id, part,
                                     timeout, retries, retry_delay)
                    for k in counts:
                        counts[k] += sub[k]
                return counts
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if (isinstance(body, dict) and body.get('status') == 'ok'
                        and body.get('queue_id') == queue_id):
                    results = body.get('results')
                    if isinstance(results, list) and len(results) == len(pending):
                        retry = []
                        for msg, res in zip(pending, results):
                            status = (res or {}).get('status')
                            if status in ('stored', 'duplicate', 'rejected'):
                                counts[status] += 1
                                if status == 'rejected':
                                    print(f"    [!] rejected {msg['header'].get('timestamp')}: "
                                          f"{(res or {}).get('error', 'no reason given')}")
                            else:
                                retry.append(msg)
                        pending = retry
                        if not pending:
                            return counts
                        reported_errors = True
                        problem = f"{len(pending)} message(s) reported as transient errors"
                    elif 'results' not in body and body.get('stored') == len(pending):
                        # Old Manager: no per-message results, all stored.
                        counts['stored'] += len(pending)
                        counts['legacy_ack'] += len(pending)
                        return counts
                    else:
                        problem = (f"incomplete acknowledgment (stored={body.get('stored')!r} "
                                   f"of {len(pending)})")
                else:
                    problem = "acknowledgment missing or queue_id mismatch"
            else:
                problem = f"HTTP {resp.status_code}"
                if resp.status_code == 400:
                    # Malformed batch: retrying the same body won't help.
                    print(f"    [!] Manager rejected the batch as malformed (HTTP 400)")
                    counts['undelivered'] += len(pending)
                    return counts

        if attempt > retries:
            print(f"    [!] giving up on {len(pending)} message(s) after {attempt} attempts: {problem}")
            counts['error' if reported_errors else 'undelivered'] += len(pending)
            return counts
        print(f"    retrying {len(pending)} message(s) in {retry_delay:g}s ({problem})")
        time.sleep(retry_delay)
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def replay(messages, manager_url: str, token: str, batch_size: int, timeout: float,
           retries: int = 3, retry_delay: float = 5.0, session=None,
           allow_duplicates: bool = False, default_name: str = None,
           default_id: str = None) -> dict:
    """
    Send messages (any iterable; consumed lazily, so at most batch_size
    messages per device are held in memory) in batches per device. The first
    batch of each device is preceded by an empty probe batch. Returns total counts.
    """
    session = session or requests.Session()
    url = f"{manager_url.rstrip('/')}/api/data/queue"
    totals = _new_counts()
    devices = {}        # (name, id) → {'pending': [...], 'state': 'new'|'ok'|'blocked', 'batches': n}

    def flush(key, dev):
        batch, dev['pending'] = dev['pending'], []
        if not batch:
            return
        name, dev_id = key
        if dev['state'] == 'new':
            print(f"  {name}: sending in batches of {batch_size}")
            kind = probe_manager(session, url, token, name, dev_id, timeout)
            if kind == 'legacy' and not allow_duplicates:
                print("  ERROR: this Manager does not report per-message results (an older version")
                print("         without de-duplication), so a replay would store duplicates.")
                print("         Upgrade the Manager first, or pass --allow-duplicates (e.g. right")
                print("         after reset_system.py wiped this system).")
                dev['state'] = 'blocked'
            else:
                dev['state'] = 'ok'
        if dev['state'] == 'blocked':
            totals['undelivered'] += len(batch)
            return
        dev['batches'] += 1
        counts = send_batch(session, url, token, name, dev_id, batch, timeout,
                            retries=retries, retry_delay=retry_delay)
        for k in totals:
            totals[k] += counts[k]
        print(f"    {name} batch {dev['batches']}: stored {counts['stored']}, "
              f"duplicate {counts['duplicate']}, rejected {counts['rejected']}, "
              f"not delivered {counts['error'] + counts['undelivered']}", flush=True)
        if counts['legacy_ack'] and not allow_duplicates:
            print("  ERROR: the Manager acknowledged without per-message results (an older")
            print(f"         version without de-duplication); sending nothing more for {name}.")
            print("         Pass --allow-duplicates to send the rest anyway.")
            dev['state'] = 'blocked'

    for msg in messages:
        key = device_key(msg, default_name, default_id)
        dev = devices.get(key)
        if dev is None:
            dev = devices[key] = {'pending': [], 'state': 'new', 'batches': 0}
        if dev['state'] == 'blocked':
            totals['undelivered'] += 1
            continue
        dev['pending'].append(msg)
        if len(dev['pending']) >= batch_size:
            flush(key, dev)
    for key, dev in devices.items():
        flush(key, dev)
    return totals


def print_read_summary(stats: ReadStats):
    """What was read and why messages were skipped."""
    for line in stats.bad_examples:
        print(f"    skipped {line}")
    if stats.bad_lines > len(stats.bad_examples):
        print(f"    ... and {stats.bad_lines - len(stats.bad_examples)} more skipped")
    print()
    print(f"Messages read: {stats.read}   selected for replay: {stats.selected}")
    if stats.outside_window:
        print(f"  {stats.outside_window} message(s) outside --since/--until skipped")
    if stats.repeated:
        print(f"  {stats.repeated} repeated msg_id(s) skipped")
    if stats.legacy:
        print(f"  {stats.legacy} message(s) without a msg_id (written before the collector's")
        print("  de-duplication upgrade) skipped. Pass --include-legacy to send them, but only")
        print("  to a system wiped by reset_system.py (or a Manager that never received them):")
        print("  a Manager that still holds them would store them twice.")


def main():
    parser = argparse.ArgumentParser(
        description='Replay collector archive JSONL files into the Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--source', action='append', required=True, metavar='PATH',
                        help='Archive file, queue.json, or directory of YYYY-MM.jsonl files (repeatable)')
    parser.add_argument('--config', metavar='PATH',
                        help='Collector config.json (manager_url, manager_token, name, id, batch/timeout settings)')
    parser.add_argument('--manager-url', metavar='URL', help='Manager base URL (overrides the config)')
    parser.add_argument('--token', metavar='TOKEN',
                        help='Bearer token (default: $LAB_MONITOR_TOKEN, else the config manager_token)')
    parser.add_argument('--batch-size', type=int, metavar='N',
                        help=f'Messages per request (default: config max_batch_messages or {DEFAULT_BATCH})')
    parser.add_argument('--timeout', type=float, metavar='SECONDS',
                        help=f'HTTP timeout (default: config request_timeout_seconds or {DEFAULT_TIMEOUT})')
    parser.add_argument('--since', metavar='TIMESTAMP',
                        help='Only messages with header timestamp >= this (ISO 8601 prefix, e.g. 2026-08-04T07:00)')
    parser.add_argument('--until', metavar='TIMESTAMP',
                        help='Only messages with header timestamp < this (ISO 8601 prefix)')
    parser.add_argument('--retries', type=int, default=3, help='Retries per batch on transient failures (default 3)')
    parser.add_argument('--include-legacy', action='store_true',
                        help='Also send messages without a msg_id (pre-upgrade). Only for a system '
                             'wiped by reset_system.py: a Manager that holds them stores them twice')
    parser.add_argument('--allow-duplicates', action='store_true',
                        help='Replay even to an old Manager without de-duplication')
    parser.add_argument('--dry-run', action='store_true', help='Read and count messages, send nothing')
    args = parser.parse_args()

    if requests is None:
        print("ERROR: requests is required. Install with: pip install requests")
        sys.exit(1)

    config = {}
    if args.config:
        try:
            config = load_config(args.config)
        except Exception as e:
            print(f"ERROR: could not read config {args.config}: {e}")
            sys.exit(1)

    manager_url = args.manager_url or config.get('manager_url')
    token = args.token or os.environ.get('LAB_MONITOR_TOKEN') or config.get('manager_token')
    batch_size = args.batch_size or int(config.get('max_batch_messages') or DEFAULT_BATCH)
    timeout = args.timeout or float(config.get('request_timeout_seconds') or DEFAULT_TIMEOUT)
    if batch_size < 1:
        print("ERROR: --batch-size must be >= 1")
        sys.exit(1)
    if not args.dry_run and not (manager_url and token):
        print("ERROR: need a Manager URL and token (--config, or --manager-url and --token)")
        sys.exit(1)

    try:
        files = source_files(args.source)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    stats = ReadStats()
    default_name, default_id = config.get('name'), config.get('id')
    messages = select_messages(files, stats, args.since, args.until, args.include_legacy)

    if args.dry_run:
        per_device = {}
        for msg in messages:
            key = device_key(msg, default_name, default_id)
            per_device[key] = per_device.get(key, 0) + 1
        print_read_summary(stats)
        if not per_device:
            print("Nothing to replay.")
            sys.exit(1)
        for (name, _dev_id), count in per_device.items():
            print(f"  {name}: {count} message(s), {-(-count // batch_size)} batch(es)")
        print("[DRY-RUN] nothing sent")
        sys.exit(0)

    print(f"Manager: {manager_url}")
    try:
        totals = replay(messages, manager_url, token, batch_size, timeout,
                        retries=args.retries, allow_duplicates=args.allow_duplicates,
                        default_name=default_name, default_id=default_id)
    except AuthRefused as e:
        print(f"ERROR: {e}. Use a token allowed for this device, or an admin token.")
        sys.exit(1)

    print_read_summary(stats)
    if not stats.selected:
        print("Nothing to replay.")
        sys.exit(1)

    not_delivered = totals['error'] + totals['undelivered']
    print()
    print("=" * 60)
    print(f"Stored: {totals['stored']}   Already present: {totals['duplicate']}   "
          f"Rejected: {totals['rejected']}   Not delivered: {not_delivered}")
    if totals['legacy_ack']:
        print(f"  ({totals['legacy_ack']} acknowledged by an old Manager without de-duplication)")
    if totals['rejected']:
        print("  Rejected messages are permanently invalid; the Manager keeps them in dead_letter.")
    if not_delivered:
        print("  Some messages were not delivered — re-run to retry (already-stored ones are skipped).")
    if totals['stored']:
        print("  If newer data already reached the Manager, its device card now shows the last")
        print("  replayed message: resume the collector and run it once in metrics and disk mode")
        print("  (run_collectors.py --mode metrics / --mode disk) to show current values.")
    print("=" * 60)
    sys.exit(2 if not_delivered else 0)


if __name__ == '__main__':
    main()
