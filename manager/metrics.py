"""
Metrics Storage Module — central summary database (dashboard layer).

Three tables, each with a clear purpose:

  devices          — device registry; one row per system, never deleted.
                     Records identity (name, id, device_type) and first/last seen.

  device_snapshot  — latest known state per device; one row per system,
                     overwritten on every ingest.  Holds the most recent
                     metrics (cpu, ram, uptime, bandwidth) and disk state
                     (total_disk_bytes, raw folders_json).

  device_totals    — running cumulative counters per device.
                     Accumulates network bytes across reboots by tracking
                     the last-seen OS counter and adding deltas.
                     Also mirrors the current total_disk_bytes for
                     quick global-sum queries.

Full historical data lives in TypedDataStore (data_store.py).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MetricsDB:

    def __init__(self, db_path: str, data_dir: str = None):
        self.db_path  = db_path
        self.data_dir = data_dir   # kept for signature compat; unused here
        self._connect()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def _connect(self):
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')

    # ------------------------------------------------------------------ #
    # Schema                                                               #
    # ------------------------------------------------------------------ #

    def _init_schema(self):
        # ---- device registry ----------------------------------------- #
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                name        TEXT PRIMARY KEY,
                system_id   TEXT,
                device_type TEXT DEFAULT 'unknown',
                first_seen  DATETIME NOT NULL,
                last_seen   DATETIME NOT NULL
            )
        ''')

        # ---- latest snapshot per device ------------------------------- #
        self.db.execute('''
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
                folders_json                TEXT    DEFAULT '{}'
            )
        ''')

        # ---- running cumulative counters ------------------------------ #
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS device_totals (
                name             TEXT PRIMARY KEY,
                total_bytes_in   INTEGER DEFAULT 0,
                total_bytes_out  INTEGER DEFAULT 0,
                last_bytes_in    INTEGER DEFAULT 0,
                last_bytes_out   INTEGER DEFAULT 0,
                total_disk_bytes INTEGER DEFAULT 0,
                last_updated     DATETIME
            )
        ''')

        self.db.commit()

    # ------------------------------------------------------------------ #
    # Write — device registry                                              #
    # ------------------------------------------------------------------ #

    def upsert_device(self, name: str, system_id: str,
                      device_type: str, timestamp: str) -> bool:
        """Register a device on first contact; update last_seen on every ingest."""
        try:
            self.db.execute('''
                INSERT INTO devices (name, system_id, device_type, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    system_id   = excluded.system_id,
                    device_type = excluded.device_type,
                    last_seen   = excluded.last_seen
            ''', (name, system_id, device_type, timestamp, timestamp))
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"upsert_device failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — snapshot                                                     #
    # ------------------------------------------------------------------ #

    def update_snapshot_metrics(self, name: str, timestamp: str,
                                 data: Dict[str, Any]) -> bool:
        """
        Update the metrics columns of device_snapshot from a system_metrics payload.
        Creates the row if it doesn't exist yet (disk columns default to empty).
        """
        try:
            self.db.execute(
                'INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,)
            )
            self.db.execute('''
                UPDATE device_snapshot SET
                    metrics_timestamp          = ?,
                    cpu_percent                = ?,
                    ram_percent                = ?,
                    uptime_seconds             = ?,
                    uptime_formatted           = ?,
                    network_bandwidth_in_mbps  = ?,
                    network_bandwidth_out_mbps = ?
                WHERE name = ?
            ''', (
                timestamp,
                data.get('cpu_percent'),
                data.get('ram_percent'),
                data.get('uptime_seconds', 0),
                data.get('uptime_formatted', '0s'),
                data.get('network_bandwidth_in_mbps', 0.0),
                data.get('network_bandwidth_out_mbps', 0.0),
                name,
            ))
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"update_snapshot_metrics failed for {name}: {e}")
            return False

    def update_snapshot_disk(self, name: str, timestamp: str,
                              data: Dict[str, Any]) -> bool:
        """
        Update the disk columns of device_snapshot from a folder_usage payload.
        Stores total_disk_bytes and the full raw payload as folders_json so the
        dashboard can reconstruct folder breakdown without hitting the history DB.
        """
        try:
            total_bytes = data.get('total_usage', 0) or 0
            # Store full payload (minus data_type) as JSON for dashboard parsing
            folders_json = json.dumps(
                {k: v for k, v in data.items() if k != 'data_type'}
            )
            self.db.execute(
                'INSERT OR IGNORE INTO device_snapshot (name) VALUES (?)', (name,)
            )
            self.db.execute('''
                UPDATE device_snapshot SET
                    disk_timestamp   = ?,
                    total_disk_bytes = ?,
                    folders_json     = ?
                WHERE name = ?
            ''', (timestamp, total_bytes, folders_json, name))
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"update_snapshot_disk failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Write — running totals                                               #
    # ------------------------------------------------------------------ #

    def accumulate_network(self, name: str, current_bytes_in: int,
                           current_bytes_out: int, timestamp: str) -> bool:
        """
        Add the delta since the last reading to the lifetime network totals.

        Delta is computed as  max(0, current - last_seen).
        Using max(0, …) means a counter reset (reboot) contributes 0 to the
        lifetime total for that interval — conservative but accurate.

        On first contact the current counter value seeds the totals.
        """
        try:
            cursor = self.db.execute(
                'SELECT * FROM device_totals WHERE name = ?', (name,)
            )
            row = cursor.fetchone()

            if row:
                delta_in  = max(0, current_bytes_in  - row['last_bytes_in'])
                delta_out = max(0, current_bytes_out - row['last_bytes_out'])
                new_total_in  = row['total_bytes_in']  + delta_in
                new_total_out = row['total_bytes_out'] + delta_out
            else:
                # First time we see this device — seed with current OS counter
                new_total_in  = current_bytes_in
                new_total_out = current_bytes_out

            self.db.execute('''
                INSERT INTO device_totals
                    (name, total_bytes_in, total_bytes_out,
                     last_bytes_in, last_bytes_out, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    total_bytes_in  = excluded.total_bytes_in,
                    total_bytes_out = excluded.total_bytes_out,
                    last_bytes_in   = excluded.last_bytes_in,
                    last_bytes_out  = excluded.last_bytes_out,
                    last_updated    = excluded.last_updated
            ''', (name, new_total_in, new_total_out,
                  current_bytes_in, current_bytes_out, timestamp))
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"accumulate_network failed for {name}: {e}")
            return False

    def update_disk_total(self, name: str, total_disk_bytes: int,
                          timestamp: str) -> bool:
        """Mirror the latest total_disk_bytes into device_totals for global sums."""
        try:
            self.db.execute('''
                INSERT INTO device_totals (name, total_disk_bytes, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    total_disk_bytes = excluded.total_disk_bytes,
                    last_updated     = excluded.last_updated
            ''', (name, total_disk_bytes, timestamp))
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"update_disk_total failed for {name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Read — device registry                                               #
    # ------------------------------------------------------------------ #

    def get_all_devices(self) -> List[Dict]:
        cursor = self.db.execute('SELECT * FROM devices ORDER BY name')
        return [dict(row) for row in cursor.fetchall()]

    def get_device(self, name: str) -> Optional[Dict]:
        cursor = self.db.execute('SELECT * FROM devices WHERE name = ?', (name,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_systems(self) -> List[str]:
        cursor = self.db.execute('SELECT name FROM devices ORDER BY name')
        return [row[0] for row in cursor.fetchall()]

    # ------------------------------------------------------------------ #
    # Read — snapshots                                                     #
    # ------------------------------------------------------------------ #

    def get_snapshot(self, name: str) -> Optional[Dict]:
        cursor = self.db.execute(
            'SELECT * FROM device_snapshot WHERE name = ?', (name,)
        )
        row = cursor.fetchone()
        return self._unpack_snapshot(row) if row else None

    def get_all_snapshots(self) -> Dict[str, Dict]:
        cursor = self.db.execute('SELECT * FROM device_snapshot ORDER BY name')
        return {row['name']: self._unpack_snapshot(row) for row in cursor.fetchall()}

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
        cursor = self.db.execute(
            'SELECT * FROM device_totals WHERE name = ?', (name,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_totals(self) -> Dict[str, Dict]:
        cursor = self.db.execute('SELECT * FROM device_totals ORDER BY name')
        return {row['name']: dict(row) for row in cursor.fetchall()}

    def get_global_totals(self) -> Dict:
        """Sum network and disk totals across all devices."""
        cursor = self.db.execute('''
            SELECT
                SUM(total_bytes_in)   AS total_bytes_in,
                SUM(total_bytes_out)  AS total_bytes_out,
                SUM(total_disk_bytes) AS total_disk_bytes
            FROM device_totals
        ''')
        row = cursor.fetchone()
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
        """
        result = {}
        cursor = self.db.execute('''
            SELECT d.name, d.system_id, d.device_type,
                   s.metrics_timestamp AS timestamp,
                   s.cpu_percent, s.ram_percent,
                   s.uptime_seconds, s.uptime_formatted,
                   s.network_bandwidth_in_mbps,
                   s.network_bandwidth_out_mbps,
                   t.last_bytes_in  AS network_bytes_in,
                   t.last_bytes_out AS network_bytes_out
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            LEFT JOIN device_totals   t ON t.name = d.name
            WHERE s.metrics_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in cursor.fetchall():
            d = dict(row)
            result[d['name']] = d
        return result

    def get_latest(self, name: str) -> Optional[Dict]:
        """Latest system_metrics snapshot for one system."""
        all_latest = self.get_latest_for_all()
        return all_latest.get(name)

    def get_latest_disk_usage_for_all(self) -> Dict[str, Dict]:
        """
        Latest folder_usage snapshot for every system.
        Shape compatible with /api/usage/all dashboard calls.
        """
        result = {}
        cursor = self.db.execute('''
            SELECT d.name, d.system_id, d.device_type,
                   s.disk_timestamp   AS timestamp,
                   s.total_disk_bytes,
                   s.folders_json
            FROM devices d
            LEFT JOIN device_snapshot s ON s.name = d.name
            WHERE s.disk_timestamp IS NOT NULL
            ORDER BY d.name
        ''')
        for row in cursor.fetchall():
            d = dict(row)
            try:
                folders_raw = json.loads(d.pop('folders_json') or '{}')
            except Exception:
                folders_raw = {}
            d.update(folders_raw)   # inline flat folder keys for parse_folder_usage()
            result[d['name']] = d
        return result

    def get_latest_disk_usage(self, name: str) -> Optional[Dict]:
        all_disk = self.get_latest_disk_usage_for_all()
        return all_disk.get(name)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def delete_system(self, name: str) -> bool:
        """
        Remove all records for a system from the central summary DB.
        Clears rows from devices, device_snapshot, and device_totals.
        Returns True if any rows were deleted.
        """
        try:
            cur = self.db.cursor()
            cur.execute('DELETE FROM devices WHERE name = ?', (name,))
            cur.execute('DELETE FROM device_snapshot WHERE name = ?', (name,))
            cur.execute('DELETE FROM device_totals WHERE name = ?', (name,))
            self.db.commit()
            deleted = cur.rowcount > 0
            logger.info(f"Deleted system '{name}' from central metrics DB")
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete system '{name}' from metrics DB: {e}")
            return False

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass
