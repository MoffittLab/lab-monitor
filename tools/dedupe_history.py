#!/usr/bin/env python3
r"""
Lab Monitor - Remove duplicate history rows that an older Manager stored

Before the review-fix upgrade a collector re-sent its whole queue whenever a
save failed or a response timed out, and the old Manager stored every copy:
the same sample (same timestamp, same values) can be in the history several
times.  The new Manager stores each message once, but the copies stored
before the upgrade stay.  This tool removes them.

What counts as a duplicate: a row stored before the upgrade (msg_id IS NULL)
whose every column except its row id is identical to an earlier row of the
same table.  The earliest copy (lowest id) is kept.  A row that differs in
any value is never touched, and neither is anything the new Manager stored
(those rows have a msg_id and can't be duplicates).

Tables:
  <system>\data.db   every history table (system_metrics, user_metrics, ...);
                     folder scans in folder_usage_msgs with their
                     folder_usage_items (a scan is a duplicate when its time,
                     device type, metadata and every folder value are equal)
  metrics.db         user_metrics_history
Never touched: folder_usage_legacy (the pre-upgrade folder table, kept as it
was), the device registry, snapshots and lifetime totals (duplicates never
changed those), dead_letter, and every backup file.

Report (default, read-only, the Manager may keep running):
    python tools\dedupe_history.py --manager-config manager\config.json
    python tools\dedupe_history.py --data-dir E:\Users\lab-monitor\manager-data

Apply (STOP the Manager first: Stop-ScheduledTask "Lab Monitor Manager"):
    python tools\dedupe_history.py --data-dir <data_dir> --apply [--vacuum] [--only triton5]

With --apply, for each database that has duplicates:
  1. it is copied with SQLite's backup API to
     "<db>.pre-dedupe-<YYYYmmddTHHMMSSZ>.bak" (temporary name, flushed to
     disk, then renamed: a .bak file is always complete)
  2. in ONE transaction per database, the duplicates are deleted, then
     checked: every distinct row is still there exactly once and no row with
     a msg_id was touched.  If a check fails, the transaction is rolled back
     and the database is left exactly as it was
  3. with --vacuum the file is then compacted (VACUUM) to return the space
The tool holds the Manager's own lock (<data_dir>\manager.lock) the whole
time, so it refuses to run while a Manager is running, and a Manager can't
start while it runs.  Databases without duplicates are not opened for
writing.  To undo: stop the Manager and copy a .bak back over its database
(after removing that database's -wal / -shm files).

Exit code: 0 done (or nothing to do), 1 bad arguments / refused (Manager
running, not enough space), 2 one or more databases failed (each of those
was rolled back and is unchanged; the others were done).
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_MANAGER_DIR = Path(__file__).resolve().parents[1] / 'manager'
if str(_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(_MANAGER_DIR))
import storage_util  # noqa: E402  (standard library only)

BACKUP_INFIX = '.pre-dedupe-'
FU_MSGS = 'folder_usage_msgs'
FU_ITEMS = 'folder_usage_items'
CENTRAL_HISTORY = 'user_metrics_history'
_NOT_HISTORY = {'sqlite_sequence', FU_MSGS, FU_ITEMS, 'dead_letter'}


class CheckFailed(Exception):
    """A post-delete check failed; the transaction is rolled back."""


def qi(name: str) -> str:
    """SQL-quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


def _columns(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info({qi(table)})')]


def _tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def history_tables(conn, central: bool) -> list:
    """The row-per-sample tables that can hold old duplicates."""
    out = []
    for t in _tables(conn):
        low = t.lower()
        if central and low != CENTRAL_HISTORY:
            continue
        if low in _NOT_HISTORY or low.startswith('folder_usage_legacy') or low.startswith('sqlite_'):
            continue
        cols = {c.lower() for c in _columns(conn, t)}
        if {'id', 'timestamp', 'msg_id'} <= cols:
            out.append(t)
    return out


def content_columns(conn, table: str) -> list:
    """Every column except the row id and msg_id: what makes two rows the same sample."""
    return [c for c in _columns(conn, table) if c.lower() not in ('id', 'msg_id')]


def table_stats(conn, table: str) -> dict:
    cols = ', '.join(qi(c) for c in content_columns(conn, table))
    t = qi(table)
    total, old, new = conn.execute(
        f'SELECT COUNT(*), SUM(msg_id IS NULL), SUM(msg_id IS NOT NULL) FROM {t}').fetchone()
    distinct_old = conn.execute(
        f'SELECT COUNT(*) FROM (SELECT 1 FROM {t} WHERE msg_id IS NULL GROUP BY {cols})').fetchone()[0]
    return {'rows': total or 0, 'old': old or 0, 'new': new or 0,
            'distinct_old': distinct_old, 'duplicates': (old or 0) - distinct_old}


def folder_duplicates(conn) -> tuple:
    """
    (ids of duplicate scans in folder_usage_msgs, number of distinct old scans).
    Only scans stored before the upgrade (msg_id IS NULL); the earliest of
    identical scans is kept.
    """
    tables = {t.lower() for t in _tables(conn)}
    if FU_MSGS not in tables or FU_ITEMS not in tables:
        return [], 0
    items = {}
    for row_id, key, value in conn.execute(
            f'SELECT i.msg_row_id, i.key, i.value FROM {FU_ITEMS} i JOIN {FU_MSGS} m '
            f'ON m.id = i.msg_row_id WHERE m.msg_id IS NULL'):
        items.setdefault(row_id, []).append((key, type(value).__name__, value))
    seen, dups = set(), []
    for row_id, ts, dtype, meta in conn.execute(
            f'SELECT id, timestamp, device_type, meta_json FROM {FU_MSGS} WHERE msg_id IS NULL ORDER BY id'):
        signature = (ts, dtype, meta, tuple(sorted(items.get(row_id, []))))
        if signature in seen:
            dups.append(row_id)
        else:
            seen.add(signature)
    return dups, len(seen)


# --------------------------------------------------------------------------- #
# Finding the databases                                                       #
# --------------------------------------------------------------------------- #

def find_databases(data_dir: Path, only=None) -> list:
    """[(label, path, central)]: every <system>/data.db, then metrics.db."""
    wanted = {o.lower() for o in (only or [])}
    out = []
    for d in sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith('.')):
        db = d / 'data.db'
        if db.is_file() and (not wanted or d.name.lower() in wanted):
            out.append((d.name, db, False))
    central = data_dir / 'metrics.db'
    if central.is_file() and (not wanted or 'metrics' in wanted or 'metrics.db' in wanted):
        out.append(('metrics.db', central, True))
    return out


def db_bytes(path: Path) -> int:
    return sum(os.path.getsize(str(path) + s) for s in ('', '-wal') if os.path.exists(str(path) + s))


# --------------------------------------------------------------------------- #
# Survey (read-only)                                                          #
# --------------------------------------------------------------------------- #

def survey_database(path: Path, central: bool) -> dict:
    conn = sqlite3.connect(storage_util.sqlite_uri(str(path), 'mode=ro'), uri=True, timeout=30)
    try:
        tables = {t: table_stats(conn, t) for t in history_tables(conn, central)}
        if not central:
            dups, distinct = folder_duplicates(conn)
            if dups or distinct:
                tables[FU_MSGS] = {'duplicates': len(dups), 'distinct_old': distinct,
                                   'rows': conn.execute(f'SELECT COUNT(*) FROM {FU_MSGS}').fetchone()[0]}
        return tables
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Apply                                                                       #
# --------------------------------------------------------------------------- #

def _fsync(path: str):
    fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_to(conn, db_path: Path, stamp: str) -> str:
    dest = f'{db_path}{BACKUP_INFIX}{stamp}.bak'
    n = 1
    while os.path.exists(dest):
        dest = f'{db_path}{BACKUP_INFIX}{stamp}-{n}.bak'
        n += 1
    tmp = dest + '.tmp'
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
    finally:
        target.close()
    try:
        _fsync(tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dest


def _dedupe_table(conn, table: str) -> int:
    before = table_stats(conn, table)
    if not before['duplicates']:
        return 0
    cols = ', '.join(qi(c) for c in content_columns(conn, table))
    t = qi(table)
    cur = conn.execute(
        f'DELETE FROM {t} WHERE msg_id IS NULL AND id NOT IN '
        f'(SELECT MIN(id) FROM {t} WHERE msg_id IS NULL GROUP BY {cols})')
    deleted = cur.rowcount
    after = table_stats(conn, table)
    problems = []
    if deleted != before['duplicates']:
        problems.append(f"deleted {deleted}, expected {before['duplicates']}")
    if after['new'] != before['new']:
        problems.append(f"rows with a msg_id changed: {before['new']} -> {after['new']}")
    if after['distinct_old'] != before['distinct_old']:
        problems.append(f"distinct rows changed: {before['distinct_old']} -> {after['distinct_old']}")
    if after['old'] != before['distinct_old'] or after['duplicates'] != 0:
        problems.append(f"{after['duplicates']} duplicates left")
    if problems:
        raise CheckFailed(f'{table}: ' + '; '.join(problems))
    return deleted


def _dedupe_folder_scans(conn) -> int:
    dups, distinct_before = folder_duplicates(conn)
    if not dups:
        return 0
    items_before = conn.execute(f'SELECT COUNT(*) FROM {FU_ITEMS}').fetchone()[0]
    dup_items = 0
    for i in range(0, len(dups), 500):
        chunk = dups[i:i + 500]
        marks = ','.join('?' * len(chunk))
        dup_items += conn.execute(f'DELETE FROM {FU_ITEMS} WHERE msg_row_id IN ({marks})', chunk).rowcount
        conn.execute(f'DELETE FROM {FU_MSGS} WHERE id IN ({marks})', chunk)
    left, distinct_after = folder_duplicates(conn)
    items_after = conn.execute(f'SELECT COUNT(*) FROM {FU_ITEMS}').fetchone()[0]
    orphans = conn.execute(
        f'SELECT COUNT(*) FROM {FU_ITEMS} WHERE msg_row_id NOT IN (SELECT id FROM {FU_MSGS})').fetchone()[0]
    if left or distinct_after != distinct_before or items_after != items_before - dup_items or orphans:
        raise CheckFailed(f'{FU_MSGS}: {len(left)} duplicates left, distinct scans '
                          f'{distinct_before} -> {distinct_after}, {orphans} orphaned items')
    return len(dups)


def apply_database(path: Path, central: bool, stamp: str, vacuum: bool) -> dict:
    """Back up, then delete the duplicates of one database in one transaction."""
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout = 60000')
        started = time.monotonic()
        backup = backup_to(conn, path, stamp)
        deleted = {}
        conn.execute('BEGIN IMMEDIATE')
        try:
            for table in history_tables(conn, central):
                n = _dedupe_table(conn, table)
                if n:
                    deleted[table] = n
            if not central:
                n = _dedupe_folder_scans(conn)
                if n:
                    deleted[FU_MSGS] = n
            check = conn.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise CheckFailed(f'quick_check: {check}')
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        size_before = db_bytes(path)
        if vacuum and deleted:
            conn.execute('VACUUM')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return {'deleted': deleted, 'backup': backup, 'seconds': time.monotonic() - started,
                'size_before': size_before, 'size_after': db_bytes(path)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def _gb(n: int) -> str:
    return f'{n / 1e9:.2f} GB'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Remove duplicate history rows stored by the old Manager (report by default).')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--data-dir', help="the Manager's data_dir")
    src.add_argument('--manager-config', help="the Manager's config.json (data_dir is read from it)")
    ap.add_argument('--apply', action='store_true',
                    help='delete the duplicates (the Manager must be stopped); default: report only')
    ap.add_argument('--vacuum', action='store_true',
                    help='with --apply: compact each changed database afterwards (returns the space; '
                         'needs free space about the size of the largest database)')
    ap.add_argument('--only', action='append', default=[], metavar='SYSTEM',
                    help="only this system's data.db (directory name; 'metrics' for metrics.db). Repeatable")
    ap.add_argument('--ignore-space', action='store_true', help='skip the free-space check')
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)     # progress shows up in a Tee'd log too
    except (AttributeError, ValueError):
        pass

    if args.vacuum and not args.apply:
        ap.error('--vacuum needs --apply')
    if args.manager_config:
        with open(args.manager_config, encoding='utf-8-sig') as f:
            data_dir = json.load(f).get('data_dir')
        if not data_dir:
            print(f'ERROR: no data_dir in {args.manager_config}')
            return 1
    else:
        data_dir = args.data_dir
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        print(f'ERROR: data_dir not found: {data_dir}')
        return 1
    dbs = find_databases(data_dir, args.only)
    if args.only:
        found = {label.lower() for label, _, central in dbs if not central}
        if any(central for _, _, central in dbs):
            found |= {'metrics', 'metrics.db'}
        missing = [o for o in args.only if o.lower() not in found]
        if missing:
            print(f"ERROR: --only matched no database in {data_dir}: {', '.join(missing)}")
            return 1
    if not dbs:
        print(f'Nothing to do: no data.db or metrics.db in {data_dir}')
        return 0

    lock = None
    if args.apply:
        try:
            lock = storage_util.InstanceLock(str(data_dir)).acquire()
        except storage_util.InstanceLockError as e:
            print(f'ERROR: a Manager is running on this data_dir ({e.holder or "holder unknown"}).')
            print('       Stop it first (Stop-ScheduledTask "Lab Monitor Manager"), then run again.')
            return 1
    try:
        return _run(args, data_dir, dbs)
    finally:
        if lock is not None:
            lock.release()


def _run(args, data_dir: Path, dbs: list) -> int:
    mode = 'APPLY' + (' + VACUUM' if args.vacuum else '') if args.apply else 'REPORT (read-only)'
    print(f'data_dir : {data_dir}')
    print(f'mode     : {mode}')
    print()
    started = time.monotonic()
    plan, totals = [], {}
    for label, path, central in dbs:
        stats = survey_database(path, central)
        dups = {t: s['duplicates'] for t, s in stats.items() if s['duplicates']}
        for t, s in stats.items():
            agg = totals.setdefault('folder scans' if t == FU_MSGS else t, [0, 0])
            agg[0] += s['rows']
            agg[1] += s['duplicates']
        print(f'{label:16} ' + (', '.join(f'{t} {n}' for t, n in dups.items()) if dups else 'no duplicates'))
        if dups:
            plan.append((label, path, central))
    print()
    print(f'{"table":24} {"rows":>12} {"duplicates":>12} {"share":>7}')
    for t, (rows, d) in totals.items():
        print(f'{t:24} {rows:>12} {d:>12} {100.0 * d / max(rows, 1):>6.1f}%')
    print(f'(survey: {time.monotonic() - started:.0f} s)')
    print()
    if not plan:
        print('Nothing to remove.')
        return 0
    if not args.apply:
        print(f'{len(plan)} database(s) have duplicates. Nothing was changed (report only).')
        print('To remove them: stop the Manager, then run again with --apply.')
        return 0

    need = sum(db_bytes(p) for _, p, _ in plan)          # the .pre-dedupe backups
    if args.vacuum:
        need += max(db_bytes(p) for _, p, _ in plan)     # VACUUM's temporary copy
    free = shutil.disk_usage(str(data_dir)).free
    print(f'space    : {_gb(need)} needed (backups{" + VACUUM" if args.vacuum else ""}), {_gb(free)} free')
    if need * 1.1 > free and not args.ignore_space:
        print('ERROR: not enough free space (or use --ignore-space).')
        return 1

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    failed = 0
    for label, path, central in plan:
        print(f'...      {label} ({_gb(db_bytes(path))})')
        try:
            r = apply_database(path, central, stamp, args.vacuum)
        except (CheckFailed, sqlite3.Error, OSError) as e:
            failed += 1
            print(f'[FAILED] {label}: {e} (rolled back, unchanged)')
            continue
        removed = ', '.join(f'{t} {n}' for t, n in r['deleted'].items())
        size = (f", {_gb(r['size_before'])} -> {_gb(r['size_after'])}" if args.vacuum else '')
        print(f"[OK]     {label}: removed {removed} ({r['seconds']:.0f} s{size})")
        print(f"         backup: {r['backup']}")
    print()
    print(f'Done in {time.monotonic() - started:.0f} s: {len(plan) - failed} database(s) cleaned, {failed} failed.')
    print('Keep the .pre-dedupe-*.bak files until you have checked the Dashboard; delete them afterwards.')
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
