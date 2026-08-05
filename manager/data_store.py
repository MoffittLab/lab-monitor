"""
TypedDataStore — per-system SQLite with one table per data_type.

Storage layout:
    data_dir/
    └── <system_name>/
        └── data.db
            ├── folder_usage     (one row per disk collection run)
            ├── system_metrics   (one row per metrics collection run)
            ├── not_specified    (fallback for messages with no data_type)
            └── <any_future_type>

Schema evolution:
    When a message arrives with a key the table has never seen before, the
    column is added via ALTER TABLE and all prior rows are backfilled with
    NaN.  This cleanly distinguishes "we didn't measure this yet" from
    "we measured zero".
"""

import json
import os
import re
import math
import sqlite3
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Columns managed by the store itself — never added dynamically
_INTERNAL_COLS = {'id', 'timestamp', 'device_type', 'data_type'}

# Table used when a message carries no data_type label
DEFAULT_TABLE = 'not_specified'


def _col_type(value: Any) -> str:
    """
    Determine the SQLite column type for a value.
    Scalars (int, float, bool, None) → REAL
    Complex types (list, dict, str)  → TEXT
    """
    if isinstance(value, (list, dict)):
        return 'TEXT'
    if isinstance(value, str):
        return 'TEXT'
    return 'REAL'


def _serialize(value: Any) -> Any:
    """
    Convert a value to a SQLite-bindable type.
    Lists and dicts are JSON-serialized to TEXT.
    Everything else is passed through.
    """
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _sanitize_table(name: str) -> str:
    """
    Convert a data_type string into a valid unquoted SQLite table name.
    Lowercases, replaces anything that isn't a-z0-9_ with underscore,
    and truncates to 64 characters.
    """
    return re.sub(r'[^a-z0-9_]', '_', name.lower())[:64]


def _quote_col(name: str) -> str:
    """
    Escape a column name for use inside a double-quoted SQLite identifier.
    SQLite only needs double-quotes within the identifier to be doubled.
    e.g.  /volume1/JeffMoffitt  →  /volume1/JeffMoffitt   (no change needed)
          col"name              →  col""name
    """
    return name.replace('"', '""')


class TypedDataStore:
    """
    Manages one SQLite database per system.
    Each data_type gets its own table; unknown types go to 'not_specified'.
    Tables are created on first write; columns are added as new keys arrive.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._dbs: Dict[str, sqlite3.Connection] = {}

    # ------------------------------------------------------------------ #
    # Connection management                                                #
    # ------------------------------------------------------------------ #

    def _db_path(self, system_name: str) -> str:
        system_dir = os.path.join(self.data_dir, _sanitize_table(system_name))
        os.makedirs(system_dir, exist_ok=True)
        return os.path.join(system_dir, 'data.db')

    def _get_db(self, system_name: str) -> sqlite3.Connection:
        if system_name not in self._dbs:
            db = sqlite3.connect(self._db_path(system_name), check_same_thread=False)
            db.row_factory = sqlite3.Row
            # Enable WAL for better concurrent read performance
            db.execute('PRAGMA journal_mode=WAL')
            self._dbs[system_name] = db
        return self._dbs[system_name]

    # ------------------------------------------------------------------ #
    # Schema helpers                                                       #
    # ------------------------------------------------------------------ #

    def _get_columns(self, db: sqlite3.Connection, table: str) -> set:
        """Return the set of column names currently in a table."""
        cursor = db.execute(f'PRAGMA table_info("{table}")')
        return {row[1] for row in cursor.fetchall()}

    def _list_tables(self, db: sqlite3.Connection) -> set:
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in cursor.fetchall()}

    def _ensure_table(self, db: sqlite3.Connection, table: str):
        """Create the table with the base schema if it doesn't exist yet."""
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS "{table}" (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME NOT NULL,
                device_type TEXT     DEFAULT 'unknown'
            )
        ''')
        db.execute(f'''
            CREATE INDEX IF NOT EXISTS "idx_{table}_ts"
            ON "{table}" (timestamp DESC)
        ''')
        db.commit()

    def _evolve_schema(self, db: sqlite3.Connection, table: str, payload: dict):
        """
        Inspect payload for keys the table hasn't seen before.
        For each new key:
          1. ALTER TABLE to add the column (REAL type — covers int, float, and NaN).
          2. UPDATE all prior rows to NaN so they're clearly 'not measured'.
        Logs every migration for auditability.
        """
        existing = self._get_columns(db, table)
        incoming = {k for k in payload if k not in _INTERNAL_COLS}
        new_cols  = incoming - existing

        for col in sorted(new_cols):
            col_type = _col_type(payload[col])
            qcol = _quote_col(col)
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{qcol}" {col_type}')
            backfill = float('nan') if col_type == 'REAL' else ''
            db.execute(
                f'UPDATE "{table}" SET "{qcol}" = ? WHERE "{qcol}" IS NULL',
                (backfill,)
            )
            logger.info(
                f"[data_store] schema evolved: '{table}'.\"{col}\" ({col_type}) added, "
                f"prior rows backfilled {'NaN' if col_type == 'REAL' else 'empty string'}"
            )

        if new_cols:
            db.commit()

    # ------------------------------------------------------------------ #
    # Public write API                                                     #
    # ------------------------------------------------------------------ #

    def store(self,
              system_name: str,
              timestamp:   str,
              device_type: str,
              data_type:   Optional[str],
              data:        Dict[str, Any]) -> bool:
        """
        Store one message's data payload in the appropriate typed table.

        Args:
            system_name:  e.g. "Triton"
            timestamp:    ISO 8601 string from the message header
            device_type:  e.g. "synology", "windows"
            data_type:    e.g. "folder_usage", "system_metrics", or None
            data:         The full data dict from the message (may include
                          the 'data_type' key itself; it is stripped before
                          inserting so it doesn't become a column)
        """
        try:
            db    = self._get_db(system_name)
            table = _sanitize_table(data_type or DEFAULT_TABLE)

            # Strip the data_type label — it's the table name, not a column
            payload = {k: v for k, v in data.items() if k != 'data_type'}

            self._ensure_table(db, table)
            self._evolve_schema(db, table, payload)

            # Column names stored verbatim (quoted); values serialized for SQLite
            cols   = ['timestamp', 'device_type'] + list(payload.keys())
            vals   = [timestamp,   device_type]   + [_serialize(v) for v in payload.values()]
            ph     = ', '.join(['?'] * len(cols))
            cnames = ', '.join(f'"{_quote_col(c)}"' for c in cols)

            db.execute(f'INSERT INTO "{table}" ({cnames}) VALUES ({ph})', vals)
            db.commit()
            return True

        except Exception as e:
            logger.error(
                f"[data_store] store failed for {system_name}/{data_type}: {e}",
                exc_info=True
            )
            return False

    # ------------------------------------------------------------------ #
    # Public read API                                                      #
    # ------------------------------------------------------------------ #

    def get_recent(self,
                   system_name: str,
                   data_type:   str,
                   limit:       int = 100) -> List[dict]:
        """
        Return the most recent `limit` rows from a typed table as plain dicts.
        Returns [] if the system or table doesn't exist yet.
        """
        try:
            db    = self._get_db(system_name)
            table = _sanitize_table(data_type or DEFAULT_TABLE)
            if table not in self._list_tables(db):
                return []
            cursor = db.execute(
                f'SELECT * FROM "{table}" ORDER BY timestamp DESC LIMIT ?',
                (limit,)
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[data_store] get_recent failed for {system_name}/{data_type}: {e}")
            return []

    def get_between(self,
                    system_name: str,
                    data_type:   str,
                    start:       str,
                    end:         str) -> List[dict]:
        """
        Return rows whose timestamp falls in [start, end) (ISO 8601 strings).
        """
        try:
            db    = self._get_db(system_name)
            table = _sanitize_table(data_type or DEFAULT_TABLE)
            if table not in self._list_tables(db):
                return []
            cursor = db.execute(
                f'SELECT * FROM "{table}" WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
                (start, end)
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[data_store] get_between failed for {system_name}/{data_type}: {e}")
            return []

    def list_data_types(self, system_name: str) -> List[str]:
        """Return the list of data_type table names present for a system."""
        try:
            db = self._get_db(system_name)
            return sorted(self._list_tables(db) - {'sqlite_sequence'})
        except Exception:
            return []

    def list_systems(self) -> List[str]:
        """Return system names that have a data.db in the data_dir."""
        try:
            return sorted(
                d for d in os.listdir(self.data_dir)
                if os.path.isfile(os.path.join(self.data_dir, d, 'data.db'))
            )
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def delete_system(self, name: str) -> bool:
        """
        Delete the per-system history database file entirely.
        Closes any open connection first, then removes the .db file.
        Returns True if the file was deleted, False if it didn't exist.
        """
        system_key = name.lower()
        # Close open connection if any
        if system_key in self._dbs:
            try:
                self._dbs[system_key].close()
            except Exception:
                pass
            del self._dbs[system_key]

        db_path = os.path.join(self.data_dir, name, 'data.db')
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
                logger.info(f"Deleted per-system DB: {db_path}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete {db_path}: {e}")
                return False
        else:
            logger.warning(f"No per-system DB found at {db_path}")
            return False

    def close(self):
        for db in self._dbs.values():
            try:
                db.close()
            except Exception:
                pass
        self._dbs.clear()
