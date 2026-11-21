"""
Metrics Storage Module — central summary database (dashboard layer).

Three tables, each with a clear purpose:

  devices          — device registry; one row per system, never deleted.
                     Records identity (name, id, device_type) and first/last seen.

  device_snapshot  — latest known state per device; one row per system.
                     Holds the most recent metrics (cpu, ram, uptime,
                     bandwidth) and disk state (total_disk_bytes, raw
                     folders_json).  Only a message at least as new as the
                     current snapshot overwrites it, so a gap-fill of older
                     messages (tools/replay_archives.py, retried 'error'
                     results) adds history without rolling the snapshot back.

  device_totals    — running cumulative counters per device.
                     Accumulates network bytes across reboots by tracking
                     the last-seen OS counter and adding deltas.
                     Also mirrors the current total_disk_bytes for
                     quick global-sum queries.

Plus dead_letter (messages the Manager rejected, with the raw message and
the reason) and the per-user tables managed by UserMetricsStore.

All writes for one ingested message happen in one transaction
(apply_side_effects).  Device names are matched through
storage_util.normalize_device_name, so "Lab-A" and "lab-a" resolve to the
same registered device.

Full historical data lives in TypedDataStore (data_store.py).
"""

import json
import logging
import math
from typing import Any, Dict, List, Optional

from storage_util import LockedConnection, normalize_device_name, timestamp_le, utc_now

logger = logging.getLogger(__name__)


_INT64_MAX = 2 ** 63 - 1


def _number_or_none(value):
    """Finite numbers pass through; anything else (None, str, bool, NaN, ±inf) → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _not_older(timestamp: str, current: Optional[str]) -> bool:
    """True if a message at `timestamp` may replace state recorded at `current`."""
    return not current or timestamp_le(current, timestamp)


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The later of two stored timestamps (either may be missing)."""
    if not a:
        return b
    if not b:
        return a
    return b if timestamp_le(a, b) else a


class MetricsDB:

    def __init__(self, db_path: str, data_dir: str = None):
        self.db_path  = db_path
        self.data_dir = data_dir   # kept for signature compat; unused here
        self._connect()
        self._init_schema()
        self._names: Dict[str, str] = {}
        self._load_name_index()
        self.users = UserMetricsStore(self)

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def _connect(self):
        self._conn = LockedConnection(self.db_path)
        self._conn.conn.execute('PRAGMA foreign_keys=ON')
        # Raw connection kept for compatibility (autocommit mode)
        self.db = self._conn.conn

    def transaction(self):
        """Context manager: one BEGIN IMMEDIATE ... COMMIT on the central DB."""
        return self._conn.transaction()

    # ------------------------------------------------------------------ #
    # Schema                                                               #
    # ------------------------------------------------------------------ #

    def _columns(self, table: str) -> set:
        return {row[1] for row in self._conn.query(f'PRAGMA table_info("{table}")')}

    def _init_schema(self):
        with self._conn.transaction() as db:
            # ---- device registry ----------------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS devices (
                    name        TEXT PRIMARY KEY,
                    system_id   TEXT,
                    device_type TEXT DEFAULT 'unknown',
                    first_seen  DATETIME NOT NULL,
                    last_seen   DATETIME NOT NULL
                )
            ''')

            # ---- latest snapshot per device ------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_snapshot (
                    name                        TEXT PRIMARY KEY,
                    metrics_timestamp           DATETIME,
                    cpu_percent                 REAL,
                    ram_percent                 REAL,
                    uptime_seconds              INTEGER DEFAULT 0,
                    uptime_formatted            TEXT    DEFAULT '0s',
                    network_bandwidth_in_mbps   REAL    DEFAULT 0,
                    network_bandwidth_out_mbps  REAL    DEFAULT 0,
                    disk_timestamp              DATETIME,
                    total_disk_bytes            INTEGER DEFAULT 0,
                    folders_json                TEXT    DEFAULT '{}',
                    metrics_json                TEXT    DEFAULT '{}'
                )
            ''')

            # ---- running cumulative counters ------------------------------ #
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_totals (
                    name                 TEXT PRIMARY KEY,
                    total_bytes_in       INTEGER DEFAULT 0,
                    total_bytes_out      INTEGER DEFAULT 0,
                    last_bytes_in        INTEGER DEFAULT 0,
                    last_bytes_out       INTEGER DEFAULT 0,
                    total_disk_bytes     INTEGER DEFAULT 0,
                    last_updated         DATETIME,
                    network_last_updated DATETIME
                )
            ''')

            # ---- rejected messages ---------------------------------------- #
            db.execute('''
                CREATE TABLE IF NOT EXISTS dead_letter (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    device_name TEXT,
                    msg_id      TEXT,
                    data_type   TEXT,
                    reason      TEXT NOT NULL,
                    raw_json    TEXT NOT NULL
                )
            ''')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_dead_letter_msg_id ON dead_letter (msg_id)')

        # Migration: add metrics_json to existing databases that pre-date it
        if 'metrics_json' not in self._columns('device_snapshot'):
            self._conn.execute("ALTER TABLE device_snapshot ADD COLUMN metrics_json TEXT DEFAULT '{}'")
            logger.info("Migrated device_snapshot: added metrics_json column")

        # Migration: network replay guard gets its own timestamp, so a
        # folder_usage update (which also sets last_updated) can't make a
        # later-arriving metrics message look like a replay.  Seeded from
        # last_updated for existing rows.
        if 'network_last_updated' not in self._columns('device_totals'):
            with self._conn.transaction() as db:
                db.execute('ALTER TABLE device_totals ADD COLUMN network_last_updated DATETIME')
                db.execute('UPDATE device_totals SET network_last_updated = last_updated')
            logger.info("Migrated device_totals: added network_last_updated column")

    # ------------------------------------------------------------------ #
    # Device names                                                         #
    # ------------------------------------------------------------------ #

    def _load_name_index(self):
        """Map normalized name → registered name (earliest registration wins)."""
        names: Dict[str, str] = {}
        rows = self._conn.query('SELECT name FROM devices ORDER BY first_seen, name')
        for row in rows:
            key = normalize_device_name(row['name'])
            if key in names:
                logger.warning(
                    f"Central DB has devices '{names[key]}' and '{row['name']}' that "
                    f"normalize to the same name; new data goes to '{names[key]}'"
                )
                continue
            names[key] = row['name']
        self._names = names

    def resolve_name(self, name: str) -> str:
        """The registered spelling of a device name (or the name itself if new)."""
        with self._conn.lock:
            return self._names.get(normalize_device_name(name), name)

    def is_known(self, name: str) -> bool:
        with self._conn.lock:
            return normalize_device_name(name) in self._names

    # ------------------------------------------------------------------ #
    # Write — one message                                                  #
    # ------------------------------------------------------------------ #

    def apply_side_effects(self, name: str, system_id: str, device_type: str,
                           timestamp: str, data_type: str, payload: Dict[str, Any],
                           msg_id: Optional[str] = None):
        """
        Apply every central-DB update for one newly stored message in a
        single transaction: device registry, then the data-type specific
        snapshot / running totals / per-user rows.  Raises on failure (the
        transaction is rolled back).
        """
        with self._conn.transaction() as db:
            self._upsert_device(db, name, system_id, device_type, timestamp)
            self._remember_name(name)
            if data_type == 'system_metrics':
                self._update_snapshot_metrics(db, name, timestamp, payload)
                self._accumulate_network(
                    db, name,
                    payload.get('network_bytes_in'),
                    payload.get('network_bytes_out'),
                    timestamp,
                )
            elif data_type == 'folder_usage':
                if self._update_snapshot_disk(db, name, timestamp, payload):
                    self._update_disk_total(db, name, payload.get('total_usage'), timestamp)
            elif data_type == 'user_metrics':
                self.users._ingest(
                    db, name, timestamp,
                    payload.get('username'),
                    _number_or_none(payload.get('cpu_percent')),
                    _number_or_none(payload.get('ram_bytes')),
                    payload.get('ram_formatted'),
                    msg_id,
                )
            # Unknown / not_specified: nothing else to do in central DB

    def add_dead_letter(self, device_name: Optional[str], msg_id: Optional[str],
                        data_type: Optional[str], reason: str, raw: Any):
        """Record a rejected message (first rejection of a msg_id wins)."""
        try:
            raw_json = json.dumps(raw, default=str)
        except (TypeError, ValueError):
            raw_json = json.dumps(str(raw))
        with self._conn.transaction() as db:
            db.execute('''
                INSERT OR IGNORE INTO dead_letter
                    (received_at, device_name, msg_id, data_type, reason, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (utc_now(), device_name, msg_id,
                  data_type if isinstance(data_type, str) else None, reason, raw_json))

    def get_dead_letters(self, limit: int = 100) -> List[Dict]:
        rows = self._conn.query(
            'SELECT * FROM dead_letter ORDER BY id DESC LIMIT ?', (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Write — device registry                                              #
    # ------------------------------------------------------------------ #

    def _upsert_device(self, db, name, system_id, device_type, timestamp):
        """
        Register on first contact.  Later messages move last_seen (and the
        identity fields) forward only when they are newer, and first_seen
        back when they are older, so stored order doesn't matter.
        """
        row = db.execute('SELECT first_seen, last_seen FROM devices WHERE name = ?', (name,)).fetchone()
        if row is None:
            db.execute('''
                INSERT INTO devices (name, system_id, device_type, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, system_id, device_type, timestamp, timestamp))
            return
        if _not_older(timestamp, row['last_seen']):
            db.execute('''
                UPDATE devices SET system_id = ?, device_type = ?, last_seen = ?
                WHERE name = ?
            ''', (system_id, device_type, timestamp, name))
        if row['first_seen'] and not timestamp_le(row['first_seen'], timestamp):
            db.execute('UPDATE devices SET first_seen = ? WHERE name = ?', (timestamp, name))

    def _remember_name(self, name: str):
        # Called inside the write transaction (lock held); a rolled-back
        # registration only leaves a harmless alias for the same name.
        self._names.setdefault(normalize_device_name(name), name)

    def upsert_device(self, name: str, system_id: str,
                      device_type: str, timestamp: str) -> bool:
        """Register a device on first contact; update last_seen on every ingest."""
        try:
            with self._conn.transaction() as db:
                self._upsert_device(db, name, system_id, device_type, timestamp)
                self._remember_name(name)
            return True
        except Exception as e:
            logger.error(f"upsert_device failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — snapshot                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_units(data: Dict[str, Any]) -> Dict[str, str]:
        """
        Pull all unit fields out of a raw payload dict.
        Captures both per-field siblings ({field}_unit) and the
        message-level default (_unit).
        """
        return {k: v for k, v in data.items()
                if k == '_unit' or k.endswith('_unit')}

    @staticmethod
    def _update_snapshot_metrics(db, name, timestamp, data) -> bool:
        """
        Overwrite the metrics columns unless the snapshot already holds a
        newer system_metrics message.  Returns True if it was updated.
        """
        # Full raw payload kept as metrics_json so unit fields and any future
        # collector additions are preserved alongside the fast-access columns.
        # Unmeasured values are stored as NULL, never 0.
        db.execute('INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,))
        current = db.execute(
            'SELECT metrics_timestamp FROM device_snapshot WHERE name = ?', (name,)).fetchone()[0]
        if not _not_older(timestamp, current):
            return False
        metrics_json = json.dumps(
            {k: v for k, v in data.items() if k != 'data_type'}, default=str
        )
        db.execute('''
            UPDATE device_snapshot SET
                metrics_timestamp          = ?,
                cpu_percent                = ?,
                ram_percent                = ?,
                uptime_seconds             = ?,
                uptime_formatted           = ?,
                network_bandwidth_in_mbps  = ?,
                network_bandwidth_out_mbps = ?,
                metrics_json               = ?
            WHERE name = ?
        ''', (
            timestamp,
            _number_or_none(data.get('cpu_percent')),
            _number_or_none(data.get('ram_percent')),
            _number_or_none(data.get('uptime_seconds')),
            data.get('uptime_formatted') if isinstance(data.get('uptime_formatted'), str) else None,
            _number_or_none(data.get('network_bandwidth_in_mbps')),
            _number_or_none(data.get('network_bandwidth_out_mbps')),
            metrics_json,
            name,
        ))
        return True

    def update_snapshot_metrics(self, name: str, timestamp: str,
                                 data: Dict[str, Any]) -> bool:
        """
        Update the metrics columns of device_snapshot from a system_metrics payload.
        Creates the row if it doesn't exist yet (disk columns default to empty).
        """
        try:
            with self._conn.transaction() as db:
                self._update_snapshot_metrics(db, name, timestamp, data)
            return True
        except Exception as e:
            logger.error(f"update_snapshot_metrics failed for {name}: {e}")
            return False

    @staticmethod
    def _update_snapshot_disk(db, name, timestamp, data) -> bool:
        """
        Overwrite the disk columns unless the snapshot already holds a newer
        folder_usage message.  Returns True if it was updated.
        """
        # Full payload (minus data_type) kept as folders_json so the dashboard
        # can reconstruct the folder breakdown without hitting the history DB.
        db.execute('INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,))
        current = db.execute(
            'SELECT disk_timestamp FROM device_snapshot WHERE name = ?', (name,)).fetchone()[0]
        if not _not_older(timestamp, current):
            return False
        folders_json = json.dumps(
            {k: v for k, v in data.items() if k != 'data_type'}, default=str
        )
        db.execute('''
            UPDATE device_snapshot SET
                disk_timestamp   = ?,
                total_disk_bytes = ?,
                folders_json     = ?
            WHERE name = ?
        ''', (timestamp, _number_or_none(data.get('total_usage')), folders_json, name))
        return True

    def update_snapshot_disk(self, name: str, timestamp: str,
                              data: Dict[str, Any]) -> bool:
        """Update the disk columns of device_snapshot from a folder_usage payload."""
        try:
            with self._conn.transaction() as db:
                self._update_snapshot_disk(db, name, timestamp, data)
            return True
        except Exception as e:
            logger.error(f"update_snapshot_disk failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — running totals                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _accumulate_network(db, name, current_bytes_in, current_bytes_out, timestamp) -> bool:
        """
        Add the traffic since the last reading to the lifetime totals.

          - either counter null/missing  → skip (unmeasured, never treated as 0)
          - timestamp <= last network update → skip (replay / out-of-order)
          - current < last (reboot / counter reset) → delta = current
            (traffic since boot)
          - otherwise delta = current - last

        On first contact the current counter value seeds the totals.
        Returns True if the totals were updated.
        """
        cur_in, cur_out = _number_or_none(current_bytes_in), _number_or_none(current_bytes_out)
        if cur_in is None or cur_out is None:
            return False
        cur_in, cur_out = int(cur_in), int(cur_out)
        if cur_in < 0 or cur_out < 0 or cur_in > _INT64_MAX or cur_out > _INT64_MAX:
            logger.warning(f"Ignoring out-of-range network counters from {name}")
            return False

        row = db.execute('SELECT * FROM device_totals WHERE name = ?', (name,)).fetchone()
        if row:
            last_ts = row['network_last_updated']
            if last_ts and timestamp_le(timestamp, last_ts):
                logger.info(
                    f"Skipping network accumulation for {name}: message {timestamp} "
                    f"is not newer than last update {last_ts}"
                )
                return False
            last_in, last_out = row['last_bytes_in'] or 0, row['last_bytes_out'] or 0
            delta_in  = cur_in  if cur_in  < last_in  else cur_in  - last_in
            delta_out = cur_out if cur_out < last_out else cur_out - last_out
            new_total_in  = (row['total_bytes_in']  or 0) + delta_in
            new_total_out = (row['total_bytes_out'] or 0) + delta_out
            last_updated  = _later(row['last_updated'], timestamp)
        else:
            # First time we see this device — seed with current OS counter
            new_total_in, new_total_out = cur_in, cur_out
            last_updated = timestamp

        db.execute('''
            INSERT INTO device_totals
                (name, total_bytes_in, total_bytes_out,
                 last_bytes_in, last_bytes_out, last_updated, network_last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                total_bytes_in       = excluded.total_bytes_in,
                total_bytes_out      = excluded.total_bytes_out,
                last_bytes_in        = excluded.last_bytes_in,
                last_bytes_out       = excluded.last_bytes_out,
                last_updated         = excluded.last_updated,
                network_last_updated = excluded.network_last_updated
        ''', (name, new_total_in, new_total_out,
              cur_in, cur_out, last_updated, timestamp))
        return True

    def accumulate_network(self, name: str, current_bytes_in: Optional[int],
                           current_bytes_out: Optional[int], timestamp: str) -> bool:
        """Atomic (read + write in one transaction) wrapper; see _accumulate_network."""
        try:
            with self._conn.transaction() as db:
                return self._accumulate_network(db, name, current_bytes_in, current_bytes_out, timestamp)
        except Exception as e:
            logger.error(f"accumulate_network failed for {name}: {e}")
            return False

    @staticmethod
    def _update_disk_total(db, name, total_disk_bytes, timestamp):
        row = db.execute('SELECT last_updated FROM device_totals WHERE name = ?', (name,)).fetchone()
        last_updated = _later(row['last_updated'], timestamp) if row else timestamp
        db.execute('''
            INSERT INTO device_totals (name, total_disk_bytes, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                total_disk_bytes = excluded.total_disk_bytes,
                last_updated     = excluded.last_updated
        ''', (name, _number_or_none(total_disk_bytes), last_updated))

    def update_disk_total(self, name: str, total_disk_bytes: Optional[int],
                          timestamp: str) -> bool:
        """Mirror the latest total_disk_bytes into device_totals for global sums."""
        try:
            with self._conn.transaction() as db:
                self._update_disk_total(db, name, total_disk_bytes, timestamp)
            return True
        except Exception as e:
            logger.error(f"update_disk_total failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Read — device registry                                               #
    # ------------------------------------------------------------------ #

    def get_all_devices(self) -> List[Dict]:
        return [dict(row) for row in self._conn.query('SELECT * FROM devices ORDER BY name')]

    def get_device(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one('SELECT * FROM devices WHERE name = ?', (self.resolve_name(name),))
        return dict(row) if row else None

    def get_all_systems(self) -> List[str]:
        return [row[0] for row in self._conn.query('SELECT name FROM devices ORDER BY name')]

    # ------------------------------------------------------------------ #
    # Read — snapshots                                                     #
    # ------------------------------------------------------------------ #

    def get_snapshot(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one(
            'SELECT * FROM device_snapshot WHERE name = ?', (self.resolve_name(name),)
        )
        return self._unpack_snapshot(row) if row else None

    def get_all_snapshots(self) -> Dict[str, Dict]:
        rows = self._conn.query('SELECT * FROM device_snapshot ORDER BY name')
        return {row['name']: self._unpack_snapshot(row) for row in rows}

    def _unpack_snapshot(self, row) -> Dict:
        d = dict(row)
        try:
            d['folders'] = json.loads(d.pop('folders_json') or '{}')
        except Exception:
            d['folders'] = {}
        return d

    # ------------------------------------------------------------------ #
    # Read — totals                                                        #
    # ------------------------------------------------------------------ #

    def get_totals(self, name: str) -> Optional[Dict]:
        row = self._conn.query_one(
            'SELECT * FROM device_totals WHERE name = ?', (self.resolve_name(name),)
        )
        return dict(row) if row else None

    def get_all_totals(self) -> Dict[str, Dict]:
        rows = self._conn.query('SELECT * FROM device_totals ORDER BY name')
        return {row['name']: dict(row) for row in rows}

    def get_global_totals(self) -> Dict:
        """Sum network and disk totals across all devices."""
        row = self._conn.query_one('''
            SELECT
                SUM(total_bytes_in)   AS total_bytes_in,
                SUM(total_bytes_out)  AS total_bytes_out,
                SUM(total_disk_bytes) AS total_disk_bytes
            FROM device_totals
        ''')
        if row:
            return {
                'total_bytes_in':   row['total_bytes_in']   or 0,
                'total_bytes_out':  row['total_bytes_out']  or 0,
                'total_disk_bytes': row['total_disk_bytes'] or 0,
            }
        return {'total_bytes_in': 0, 'total_bytes_out': 0, 'total_disk_bytes': 0}

    # ------------------------------------------------------------------ #
    # Read — dashboard compatibility shims                                 #
    # ------------------------------------------------------------------ #

    def get_latest_for_all(self) -> Dict[str, Dict]:
        """
        Latest system_metrics snapshot for every system.
        Shape compatible with /api/metrics/all dashboard calls.
        Includes a 'units' dict extracted from metrics_json.
        """
        result = {}
        rows = self._conn.query('''
            SELECT d.name, d.system_id, d.device_type,
                   s.metrics_timestamp AS timestamp,
                   s.cpu_percent, s.ram_percent,
                   s.uptime_seconds, s.uptime_formatted,
                   s.network_bandwidth_in_mbps,
                   s.network_bandwidth_out_mbps,
                   s.metrics_json,
                   t.last_bytes_in  AS network_bytes_in,
                   t.last_bytes_out AS network_bytes_out
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            LEFT JOIN device_totals   t ON t.name = d.name
            WHERE s.metrics_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in rows:
            d = dict(row)
            try:
                raw = json.loads(d.pop('metrics_json') or '{}')
            except Exception:
                raw = {}
            # Current counters come from the latest message itself, so a
            # message that couldn't measure them shows null rather than the
            # previous reading.
            for key in ('network_bytes_in', 'network_bytes_out'):
                if key in raw:
                    d[key] = _number_or_none(raw[key])
            d['units']           = self._extract_units(raw)
            d['gpus']            = raw.get('gpus', [])
            d['total_ram_bytes'] = _number_or_none(raw.get('total_ram_bytes'))
            result[d['name']] = d
        return result

    def get_latest(self, name: str) -> Optional[Dict]:
        """Latest system_metrics snapshot for one system."""
        return self.get_latest_for_all().get(self.resolve_name(name))

    def get_latest_disk_usage_for_all(self) -> Dict[str, Dict]:
        """
        Latest folder_usage snapshot for every system.
        Shape compatible with /api/usage/all dashboard calls.
        """
        result = {}
        rows = self._conn.query('''
            SELECT d.name, d.system_id, d.device_type,
                   s.disk_timestamp   AS timestamp,
                   s.total_disk_bytes,
                   s.folders_json
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            WHERE s.disk_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in rows:
            d = dict(row)
            try:
                folders_raw = json.loads(d.pop('folders_json') or '{}')
            except Exception:
                folders_raw = {}
            d.update(folders_raw)   # inline flat folder keys for parse_folder_usage()
            result[d['name']] = d
        return result

    def get_latest_disk_usage(self, name: str) -> Optional[Dict]:
        return self.get_latest_disk_usage_for_all().get(self.resolve_name(name))

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def delete_system(self, name: str) -> bool:
        """
        Remove all records for a system from the central summary DB.
        Clears devices, device_snapshot, device_totals and the per-user
        tables for every registered spelling of the name.
        Returns True if any rows were deleted.
        """
        key = normalize_device_name(name)
        try:
            with self._conn.transaction() as db:
                names = {r['name'] for r in db.execute('SELECT name FROM devices').fetchall()
                         if normalize_device_name(r['name']) == key}
                names.add(name)
                deleted = 0
                for n in names:
                    for table, col in (('devices', 'name'), ('device_snapshot', 'name'),
                                       ('device_totals', 'name'), ('device_user_snapshot', 'name'),
                                       ('user_metrics_history', 'device_name')):
                        deleted += db.execute(f'DELETE FROM {table} WHERE {col} = ?', (n,)).rowcount
                self._names.pop(key, None)
            logger.info(f"Deleted system '{name}' from central metrics DB ({deleted} rows)")
            return deleted > 0
        except Exception as e:
            logger.error(f"Failed to delete system '{name}' from metrics DB: {e}")
            return False

    def close(self):
        self._conn.close()


class UserMetricsStore:
    """
    Manages per-user CPU/RAM snapshot and normalized history.

    Snapshot table  (device_user_snapshot): latest row per (device, username).
    History table   (user_metrics_history):  one row per (device, timestamp, username),
                                             msg_id UNIQUE for dedup.
    """

    def __init__(self, metrics_db: MetricsDB):
        self._conn = metrics_db._conn
        self.db = metrics_db.db   # raw connection, kept for compatibility
        self._init_tables()

    def _init_tables(self):
        with self._conn.transaction() as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS device_user_snapshot (
                    name          TEXT NOT NULL,
                    username      TEXT NOT NULL,
                    cpu_percent   REAL,
                    ram_bytes     INTEGER,
                    ram_formatted TEXT,
                    timestamp     TEXT,
                    PRIMARY KEY (name, username)
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS user_metrics_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_name TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    username    TEXT NOT NULL,
                    cpu_percent REAL,
                    ram_bytes   INTEGER,
                    ram_formatted TEXT,
                    msg_id      TEXT
                )
            ''')
            cols = {row[1] for row in db.execute('PRAGMA table_info(user_metrics_history)').fetchall()}
            if 'msg_id' not in cols:
                # Migration: existing rows get NULL (UNIQUE allows many NULLs)
                db.execute('ALTER TABLE user_metrics_history ADD COLUMN msg_id TEXT')
                logger.info("Migrated user_metrics_history: added msg_id column")
            db.execute('''
                CREATE INDEX IF NOT EXISTS idx_umh_device_ts
                    ON user_metrics_history (device_name, timestamp)
            ''')
            db.execute('''
                CREATE INDEX IF NOT EXISTS idx_umh_device_user_ts
                    ON user_metrics_history (device_name, username, timestamp)
            ''')
            db.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_umh_msg_id
                    ON user_metrics_history (msg_id)
            ''')

    @staticmethod
    def _ingest(db, device_name, timestamp, username, cpu_percent, ram_bytes,
                ram_formatted, msg_id=None):
        """History row always; the snapshot row only if this message is not older."""
        row = db.execute(
            'SELECT timestamp FROM device_user_snapshot WHERE name = ? AND username = ?',
            (device_name, username)).fetchone()
        if row is None or _not_older(timestamp, row['timestamp']):
            UserMetricsStore._upsert_snapshot(
                db, device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted)
        db.execute('''
            INSERT OR IGNORE INTO user_metrics_history
                (device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted, msg_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted, msg_id))

    @staticmethod
    def _upsert_snapshot(db, device_name, timestamp, username, cpu_percent, ram_bytes, ram_formatted):
        db.execute('''
            INSERT INTO device_user_snapshot
                (name, username, cpu_percent, ram_bytes, ram_formatted, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, username) DO UPDATE SET
                cpu_percent   = excluded.cpu_percent,
                ram_bytes     = excluded.ram_bytes,
                ram_formatted = excluded.ram_formatted,
                timestamp     = excluded.timestamp
        ''', (device_name, username, cpu_percent, ram_bytes, ram_formatted, timestamp))

    def ingest(self, device_name: str, timestamp: str, username: str,
               cpu_percent: Optional[float], ram_bytes: Optional[int],
               ram_formatted: Optional[str], msg_id: Optional[str] = None):
        """Store one user-metric observation: updates snapshot + appends history."""
        with self._conn.transaction() as db:
            self._ingest(db, device_name, timestamp, username, cpu_percent,
                         ram_bytes, ram_formatted, msg_id)

    def get_snapshot(self, device_name: str) -> list:
        """Current users for one device, sorted by cpu_percent desc."""
        rows = self._conn.query('''
            SELECT username, cpu_percent, ram_bytes, ram_formatted, timestamp
            FROM device_user_snapshot
            WHERE name = ?
            ORDER BY cpu_percent DESC
        ''', (device_name,))
        return [dict(r) for r in rows]

    def get_all_snapshots(self) -> dict:
        """Current users for all devices. Returns {device_name: [user_rows]}."""
        rows = self._conn.query('''
            SELECT name, username, cpu_percent, ram_bytes, ram_formatted, timestamp
            FROM device_user_snapshot
            ORDER BY name, cpu_percent DESC
        ''')
        result = {}
        for row in rows:
            d = dict(row)
            result.setdefault(d.pop('name'), []).append(d)
        return result

    def get_history(self, device_name: str, username: str, limit: int = 200) -> list:
        """Time-series rows for one user on one device."""
        rows = self._conn.query('''
            SELECT timestamp, cpu_percent, ram_bytes, ram_formatted
            FROM user_metrics_history
            WHERE device_name = ? AND username = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (device_name, username, limit))
        return [dict(r) for r in rows]

    # ---- retention support (archive_metrics.py) ---- #

    def history_older_than(self, cutoff: str, limit: int = 5000) -> list:
        """Oldest-first user_metrics_history rows with timestamp < cutoff."""
        rows = self._conn.query('''
            SELECT * FROM user_metrics_history
            WHERE timestamp < ?
            ORDER BY timestamp, id
            LIMIT ?
        ''', (cutoff, limit))
        return [dict(r) for r in rows]

    def delete_history_ids(self, ids: list) -> int:
        deleted = 0
        with self._conn.transaction() as db:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ', '.join(['?'] * len(chunk))
                deleted += db.execute(
                    f'DELETE FROM user_metrics_history WHERE id IN ({ph})', chunk).rowcount
        return deleted
