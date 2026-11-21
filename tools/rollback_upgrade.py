#!/usr/bin/env python3
r"""
Lab Monitor - Roll a Manager data_dir back to its pre-upgrade databases

The first time the new Manager opens a database written by an older Manager
it copies it to "<db>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak" (manager config
"backup_before_migration", on by default) and then migrates it.  This tool
puts those copies back, so the OLD Manager code can serve the data exactly as
it did before the upgrade.

Run it ON THE MANAGER SERVER with the Manager STOPPED (Task Scheduler: end
"Lab Monitor Manager"), using any Python 3.8+ (standard library only):

    python tools\rollback_upgrade.py --manager-config manager\config.json             (dry run: the plan)
    python tools\rollback_upgrade.py --manager-config manager\config.json --apply
    python tools\rollback_upgrade.py --data-dir E:\Users\lab-monitor\data --apply

For metrics.db and every <system>\data.db with a pre-upgrade backup:
  1. the backup is checked (PRAGMA quick_check); if any backup fails the
     check, nothing at all is changed
  2. the backup is copied next to the database (temporary file, flushed to
     disk)
  3. the current database and its -wal / -shm / -journal files are RENAMED to
     "<db>.post-upgrade-<stamp>" (+ "-wal" / "-shm" / "-journal")
  4. the copy is renamed to <db>
Nothing is deleted: the .bak stays where it is, and the moved-aside files
keep everything the new Manager stored after the upgrade (the collectors'
archives hold those messages too), so a rollback can itself be undone by
renaming the files back.

Which backup: the NEWEST pre-upgrade backup of each database that still has
the layout the old Manager wrote (each one is taken just before the new
Manager first alters a database it still had to migrate; a later one only
exists after an earlier rollback and a second upgrade, and it then holds more
data than the older one).  A backup that already has the new layout
(folder_usage_msgs / folder_usage_items / folder_usage_legacy, msg_id
columns, dead_letter: an earlier version of the new Manager could copy a
partly migrated database) is skipped with a warning, because the old Manager
would not show the folder history in it.  If no backup has the old layout the
newest is used, with a warning.  --use-oldest picks the oldest instead.

Databases WITHOUT a backup are left as they are and listed: databases the
new Manager created after the upgrade (systems first seen since), or ones it
never had to change.  The old Manager can open them; history the new Manager
stored in their folder_usage_msgs / folder_usage_items tables is not shown by
the old code.

After the rollback (see also docs/UPGRADE.md):
  - check out the old code in the installation (git checkout <old commit>)
    and start it the way it was started before (old installer / task);
  - the old Manager only accepts tokens listed in "auth_tokens": restore the
    config backup the installer made (config.json.bak-<date>) or make sure
    the Dashboard's manager_token and every collector's manager_token are in
    auth_tokens;
  - collectors already upgraded keep working against the old Manager, except
    that the old Manager reads their null network counters as 0 (lifetime
    totals then jump once): roll them back too, or keep the rollback short.

Exit status: 0 rolled back (or, without --apply, the plan is ready), 1 a
problem (bad backup, a file that could not be renamed: see the message),
2 refused (Manager running, bad arguments, nothing to roll back).
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BACKUP_INFIX = '.pre-upgrade-'           # manager/storage_util.py BACKUP_INFIX
MOVED_INFIX = '.post-upgrade-'
SIDE_SUFFIXES = ('-wal', '-shm', '-journal')
_STAMP_RE = re.compile(r'\.pre-upgrade-(\d{8}T\d{6}Z)(?:-(\d+))?\.bak$')


def say(msg=''):
    sys.stdout.write(str(msg).encode('ascii', 'backslashreplace').decode('ascii') + '\n')
    sys.stdout.flush()


def backups_of(db_path):
    """Pre-upgrade backups of one database, oldest first (by the stamp in the name)."""
    folder, base = os.path.split(db_path)
    out = []
    try:
        names = os.listdir(folder or '.')
    except OSError:
        return out
    for fn in names:
        if not fn.startswith(base + BACKUP_INFIX) or not fn.endswith('.bak'):
            continue
        m = _STAMP_RE.search(fn)
        if not m or fn[:m.start()] != base:
            continue
        out.append(((m.group(1), int(m.group(2) or 0)), os.path.join(folder, fn)))
    return [p for _k, p in sorted(out)]


def find_databases(data_dir):
    """metrics.db and every <system>/data.db that exists or has a pre-upgrade backup."""
    dbs = []
    central = os.path.join(data_dir, 'metrics.db')
    if os.path.isfile(central) or backups_of(central):
        dbs.append(central)
    for entry in sorted(os.listdir(data_dir)):
        sub = os.path.join(data_dir, entry)
        if not os.path.isdir(sub):
            continue
        db = os.path.join(sub, 'data.db')
        if os.path.isfile(db) or backups_of(db):
            dbs.append(db)
    return dbs


def _ro_uri(path):
    return Path(os.path.abspath(path)).as_uri() + '?mode=ro&immutable=1'


def has_old_layout(path, central):
    """
    True if a database file still has the layout the OLD Manager wrote, None
    if it can't be read.  metrics.db (central): no dead_letter table, no
    device_totals.network_last_updated, no user_metrics_history.msg_id.
    <system>/data.db: no folder_usage_msgs / _items / _legacy* table and no
    msg_id column in any table.
    """
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]

            def columns(table):
                return {r[1].lower() for r in conn.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))}
            if central:
                if 'dead_letter' in tables:
                    return False
                for table, column in (('device_totals', 'network_last_updated'),
                                      ('user_metrics_history', 'msg_id')):
                    if table in tables and column in columns(table):
                        return False
                return True
            for table in tables:
                if table in ('folder_usage_msgs', 'folder_usage_items') or table.startswith('folder_usage_legacy'):
                    return False
                if not table.startswith('sqlite_') and 'msg_id' in columns(table):
                    return False
            return True
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def choose_backup(db_path, backups, use_oldest=False):
    """(chosen backup, [newer backups skipped because they have the new layout], old_layout_found)."""
    if not backups:
        return None, [], False
    if use_oldest:
        return backups[0], [], bool(has_old_layout(backups[0], os.path.basename(db_path) == 'metrics.db'))
    central = os.path.basename(db_path) == 'metrics.db'
    skipped = []
    for b in reversed(backups):
        layout = has_old_layout(b, central)
        if layout is None:
            return b, skipped, False        # unreadable: check_backup reports it, nothing is changed
        if layout:
            return b, skipped, True
        skipped.append(b)
    return backups[-1], [], False


def check_backup(path):
    """'ok', or why this file can't be restored."""
    try:
        with open(path, 'rb') as f:
            if f.read(16) != b'SQLite format 3\x00':
                return 'not an SQLite database'
        conn = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            rows = conn.execute('PRAGMA quick_check').fetchall()
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as e:
        return '%s: %s' % (type(e).__name__, e)
    result = '; '.join(str(r[0]) for r in rows[:3])
    if result != 'ok':
        return 'quick_check: ' + result
    if not tables:
        return 'no tables'
    return 'ok'


def _fsync(path):
    fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename(src, dst):
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # Windows: still open (the Manager, an antivirus, a backup agent)
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def plan(data_dir, use_oldest=False):
    """[{db, backup, backups, check, exists, side_files}] for every database found."""
    items = []
    for db in find_databases(data_dir):
        bks = backups_of(db)
        chosen, skipped, old_layout = choose_backup(db, bks, use_oldest)
        items.append({
            'db': db,
            'rel': os.path.relpath(db, data_dir),
            'backups': bks,
            'backup': chosen,
            'skipped_new_layout': skipped,
            'old_layout': old_layout,
            'check': check_backup(chosen) if chosen else None,
            'exists': os.path.isfile(db),
            'side_files': [db + s for s in SIDE_SUFFIXES if os.path.exists(db + s)],
        })
    return items


def restore_one(item, stamp):
    """Restore one database from item['backup']; returns the files moved aside."""
    db, bak = item['db'], item['backup']
    tmp = db + '.rollback.tmp'
    shutil.copyfile(bak, tmp)
    _fsync(tmp)
    moved = []
    try:
        for p in ([db] if item['exists'] else []) + [db + s for s in SIDE_SUFFIXES]:
            if not os.path.exists(p):
                continue
            target = db + MOVED_INFIX + stamp + p[len(db):]
            _rename(p, target)
            moved.append((p, target))
        _rename(tmp, db)
    except BaseException:
        # put back whatever was already moved, leave the database as it was
        for src, dst in reversed(moved):
            try:
                os.replace(dst, src)
            except OSError:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return moved


def rollback(data_dir, apply=False, use_oldest=False, out=say):
    """Plan (and with apply=True perform) the rollback. Returns (exit code, plan)."""
    items = plan(data_dir, use_oldest)
    todo = [i for i in items if i['backup']]
    keep = [i for i in items if not i['backup']]
    if not todo:
        out('No pre-upgrade backups (*%s*.bak) found under %s: nothing to roll back.' % (BACKUP_INFIX, data_dir))
        return 2, items
    out('Databases to restore (%d):' % len(todo))
    for i in todo:
        others = len(i['backups']) - 1
        out('  %-40s <- %s%s%s' % (i['rel'], os.path.basename(i['backup']),
                                   ' (%d other backup%s ignored)' % (others, 's' if others > 1 else '') if others else '',
                                   '' if i['exists'] else '  [database missing now: recreated]'))
        for b in i['skipped_new_layout']:
            out('      WARNING: skipped the newer %s: it already has the new layout (a copy of a partly '
                'migrated database), so the old Manager would not show its folder history'
                % os.path.basename(b))
        if i['check'] == 'ok' and not i['old_layout']:
            out('      WARNING: %s already has the new layout (no backup of this database has the old '
                'one): the old Manager will not show the folder history stored in it' % os.path.basename(i['backup']))
        if i['check'] != 'ok':
            out('      BACKUP NOT USABLE: %s' % i['check'])
    if keep:
        out('Databases without a pre-upgrade backup, left as they are (%d):' % len(keep))
        for i in keep:
            out('  %s' % i['rel'])
    bad = [i for i in todo if i['check'] != 'ok']
    if bad:
        out('')
        out('ERROR: %d backup(s) failed the check; nothing was changed.' % len(bad))
        return 1, items
    if not apply:
        out('')
        out('Dry run: nothing changed. Stop the Manager, then re-run with --apply.')
        return 0, items
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    done = []
    for i in todo:
        try:
            i['moved'] = restore_one(i, stamp)
        except OSError as e:
            out('')
            out('ERROR restoring %s: %s: %s' % (i['rel'], type(e).__name__, e))
            out('That database is unchanged. Is the Manager (or another program) still using it?')
            if done:
                out('Already restored (current files moved to *%s%s*): %s'
                    % (MOVED_INFIX, stamp, ', '.join(d['rel'] for d in done)))
                out('Fix the cause and run again: the restored ones are simply restored once more.')
            return 1, items
        done.append(i)
        out('  restored %s' % i['rel'])
    out('')
    out('Rolled back %d database(s). The post-upgrade files are kept as *%s%s*; the .bak files are '
        'unchanged.' % (len(done), MOVED_INFIX, stamp))
    return 0, items


def manager_running(config):
    port = (config or {}).get('port', 5000)
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % int(port), timeout=3) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Restore the pre-upgrade backups of a Manager data_dir (dry run unless --apply).',
        epilog='Exit status: 0 done / plan ready, 1 problem, 2 refused. See the module docstring.')
    ap.add_argument('--data-dir', help='the Manager data_dir')
    ap.add_argument('--manager-config', help='the Manager config.json (data_dir and port are read from it)')
    ap.add_argument('--apply', action='store_true', help='perform the rollback (default: show the plan only)')
    ap.add_argument('--use-oldest', action='store_true', help='restore the oldest backup of each database')
    ap.add_argument('--force', action='store_true',
                    help='skip the check that no Manager answers on the configured port')
    args = ap.parse_args(argv)

    config = {}
    if args.manager_config:
        try:
            with open(args.manager_config, encoding='utf-8-sig') as f:
                config = json.load(f)
        except (OSError, ValueError) as e:
            say('ERROR: cannot read %s: %s' % (args.manager_config, e))
            return 2
    data_dir = args.data_dir or config.get('data_dir')
    if not data_dir:
        ap.error('give --data-dir or --manager-config')
    if args.manager_config and not os.path.isabs(data_dir):
        data_dir = os.path.join(os.path.dirname(os.path.abspath(args.manager_config)), data_dir)
    if not os.path.isdir(data_dir):
        say('ERROR: data_dir %s does not exist' % data_dir)
        return 2
    if args.apply and not args.force and manager_running(config):
        say('ERROR: a Manager answers on port %s. Stop it first (Task Scheduler: End "Lab Monitor '
            'Manager"), then run this again.' % config.get('port', 5000))
        return 2
    say('data_dir: %s' % os.path.abspath(data_dir))
    code, _items = rollback(data_dir, apply=args.apply, use_oldest=args.use_oldest)
    return code


if __name__ == '__main__':
    sys.exit(main())
