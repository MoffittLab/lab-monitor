# Implementation Plan: Message Format + Typed Data Storage

## Overview

Five files change. One new file is created.

```
shared/models.py          — new message model (header + data)
collector/collector.py    — restructure entry building + queue payload
manager/data_store.py     — NEW: TypedDataStore with schema evolution
manager/manager.py        — update ingest_queue() to new message format
manager/metrics.py        — simplify to central summary DB (dashboard only)
```

---

## 1. `shared/models.py` — New Message Model

Replace the old `UsageReport`/`FolderUsage`/`NASInfo` dataclasses with the canonical
message format agreed in the design.

```python
@dataclass
class MessageHeader:
    device_name: str
    device_id: str
    device_type: str
    timestamp: str  # ISO 8601

    def to_dict(self):
        return asdict(self)


@dataclass
class Message:
    """Single message: header + data payload."""
    header: MessageHeader
    data: Dict[str, Any]   # data_type key is optional inside data

    def to_dict(self):
        return {
            'header': self.header.to_dict(),
            'data': self.data
        }

    @staticmethod
    def from_dict(d: dict) -> 'Message':
        header = MessageHeader(**d['header'])
        return Message(header=header, data=d.get('data', {}))


@dataclass
class QueuePayload:
    """Batch of messages from one collector run."""
    queue_id: str
    messages: List[Message]

    def to_dict(self):
        return {
            'queue_id': self.queue_id,
            'messages': [m.to_dict() for m in self.messages]
        }
```

Keep `NASInfo` if dashboard code still uses it; everything else can go.

---

## 2. `collector/collector.py` — Restructure Entries

### 2a. `collect_disk_usage()` — entry shape

**Before:**
```python
entry = {
    'type': 'disk',
    'timestamp': datetime.utcnow().isoformat() + 'Z',
    'folders': folders,
    'device_type': device_type
}
```

**After:**
```python
entry = {
    'header': {
        'device_name': name,
        'device_id': system_id,
        'device_type': device_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    },
    'data': {
        'data_type': 'folder_usage',
        'folders': folders
    }
}
```

### 2b. `collect_metrics()` — entry shape

**Before:**
```python
entry = {
    'type': 'metrics',
    'timestamp': datetime.utcnow().isoformat() + 'Z',
    'cpu_percent': cpu_percent,
    'ram_percent': ram_percent,
    'uptime_seconds': uptime_seconds,
    'uptime_formatted': uptime_formatted,
    'network_bytes_in': ...,
    'network_bytes_out': ...,
    'network_bandwidth_in_mbps': ...,
    'network_bandwidth_out_mbps': ...,
    'device_type': device_type
}
```

**After:**
```python
entry = {
    'header': {
        'device_name': name,
        'device_id': system_id,
        'device_type': device_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    },
    'data': {
        'data_type': 'system_metrics',
        'cpu_percent': cpu_percent,
        'ram_percent': ram_percent,
        'uptime_seconds': uptime_seconds,
        'uptime_formatted': uptime_formatted,
        'network_bytes_in': ...,
        'network_bytes_out': ...,
        'network_bandwidth_in_mbps': ...,
        'network_bandwidth_out_mbps': ...
    }
}
```

### 2c. `sync_queue_to_manager()` — queue payload

**Before:**
```python
payload = {
    'queue_id': queue_id,
    'name': name,
    'id': system_id,
    'entries': entries
}
# POSTs to /api/data/queue
```

**After:**
```python
payload = {
    'queue_id': queue_id,
    'messages': entries   # header info now lives inside each message
}
# POSTs to /api/data/queue  (endpoint unchanged)
```

No other changes to the queue/retry/delete logic — the handshake model stays the same.

---

## 3. `manager/data_store.py` — NEW FILE

This is the core new module. It replaces the per-system storage logic that was embedded
in `metrics.py`.

```python
"""
TypedDataStore — per-system SQLite with one table per data_type.
Handles automatic schema evolution: new keys → new columns + NaN backfill.
"""
import os
import re
import math
import sqlite3
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

RESERVED_COLS = {'id', 'timestamp', 'data_type'}
DEFAULT_TABLE  = 'not_specified'


def _sanitize_name(name: str) -> str:
    """Convert arbitrary string to valid SQLite identifier (lowercase, a-z0-9_)."""
    return re.sub(r'[^a-z0-9_]', '_', name.lower())[:64]


class TypedDataStore:
    """
    Manages per-system SQLite databases.
    Each database lives at:  data_dir/<system_name>/data.db
    Each data_type gets its own table inside that database.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._dbs: Dict[str, sqlite3.Connection] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _db_path(self, system_name: str) -> str:
        system_dir = os.path.join(self.data_dir, _sanitize_name(system_name))
        os.makedirs(system_dir, exist_ok=True)
        return os.path.join(system_dir, 'data.db')

    def _get_db(self, system_name: str) -> sqlite3.Connection:
        if system_name not in self._dbs:
            db = sqlite3.connect(self._db_path(system_name), check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._dbs[system_name] = db
        return self._dbs[system_name]

    def _get_columns(self, db: sqlite3.Connection, table: str) -> set:
        cursor = db.execute(f'PRAGMA table_info("{table}")')
        return {row[1] for row in cursor.fetchall()}

    def _ensure_table(self, db: sqlite3.Connection, table: str):
        """Create table with minimal base schema if it doesn't exist."""
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS "{table}" (
                id        INTEGER  PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                device_type TEXT   DEFAULT 'unknown'
            )
        ''')
        db.execute(f'''
            CREATE INDEX IF NOT EXISTS "idx_{table}_ts"
            ON "{table}" (timestamp DESC)
        ''')
        db.commit()

    def _evolve_schema(self, db: sqlite3.Connection, table: str, payload: dict):
        """
        Add any columns present in payload that don't yet exist in the table.
        Backfill all prior rows with NaN so callers can distinguish
        'not measured yet' from 'measured as zero'.
        """
        existing = self._get_columns(db, table)
        incoming = {k for k in payload if k not in RESERVED_COLS}
        new_cols  = incoming - existing

        for col in sorted(new_cols):  # sorted for deterministic migration order
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" REAL')
            db.execute(
                f'UPDATE "{table}" SET "{col}" = ? WHERE "{col}" IS NULL',
                (float('nan'),)
            )
            logger.info(f"[{table}] schema evolved: added column '{col}', backfilled NaN")

        if new_cols:
            db.commit()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def store(self,
              system_name: str,
              timestamp: str,
              device_type: str,
              data_type: Optional[str],
              data: dict) -> bool:
        """
        Store one data payload in the appropriate typed table.

        Args:
            system_name: Human-readable system name (used as folder name)
            timestamp:   ISO 8601 string
            device_type: e.g. 'synology', 'windows'
            data_type:   e.g. 'folder_usage', 'system_metrics', or None
            data:        The full data dict from the message (may include 'data_type' key)
        """
        try:
            db    = self._get_db(system_name)
            table = _sanitize_name(data_type or DEFAULT_TABLE)

            # Strip internal metadata key from payload before storing
            payload = {k: v for k, v in data.items() if k != 'data_type'}

            self._ensure_table(db, table)
            self._evolve_schema(db, table, payload)

            # Build parameterised INSERT
            cols  = ['timestamp', 'device_type'] + list(payload.keys())
            vals  = [timestamp,   device_type]   + list(payload.values())
            ph    = ', '.join(['?'] * len(cols))
            cnames = ', '.join(f'"{c}"' for c in cols)

            db.execute(f'INSERT INTO "{table}" ({cnames}) VALUES ({ph})', vals)
            db.commit()
            return True

        except Exception as e:
            logger.error(f"TypedDataStore.store failed for {system_name}: {e}", exc_info=True)
            return False

    def get_recent(self,
                   system_name: str,
                   data_type: str,
                   limit: int = 100) -> list:
        """Fetch recent rows from a typed table as plain dicts."""
        try:
            db    = self._get_db(system_name)
            table = _sanitize_name(data_type or DEFAULT_TABLE)
            if table not in self._list_tables(db):
                return []
            cursor = db.execute(
                f'SELECT * FROM "{table}" ORDER BY timestamp DESC LIMIT ?', (limit,)
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"get_recent failed for {system_name}/{data_type}: {e}")
            return []

    def _list_tables(self, db: sqlite3.Connection) -> set:
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in cursor.fetchall()}

    def list_data_types(self, system_name: str) -> list:
        """Return list of data_type table names present for a system."""
        try:
            db = self._get_db(system_name)
            return sorted(self._list_tables(db))
        except Exception:
            return []

    def close(self):
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()
```

---

## 4. `manager/manager.py` — Update `ingest_queue()`

### 4a. Add import + init

```python
from data_store import TypedDataStore

_data_store = None   # add alongside _metrics_db

def init_app(config, logger):
    global _config, _logger, _metrics_db, _data_store
    ...
    _data_store = TypedDataStore(data_dir=data_dir)
```

### 4b. Replace `ingest_queue()` body

**Before** (routes on `entry.get("type")`, hardcoded disk vs metrics):
```python
for entry in entries:
    entry_type = entry.get("type")
    if entry_type == "disk":
        _metrics_db.insert_disk_usage(name, ...)
    elif entry_type == "metrics":
        _metrics_db.insert_metric(name, ...)
```

**After** (generic routing through TypedDataStore):
```python
messages = data.get("messages", [])

for msg in messages:
    header  = msg.get("header", {})
    payload = msg.get("data",   {})

    dev_name  = header.get("device_name")
    dev_id    = header.get("device_id")
    dev_type  = header.get("device_type", "unknown")
    timestamp = header.get("timestamp")
    data_type = payload.get("data_type")   # None → stored in not_specified table

    if not dev_name or not timestamp:
        logger.warning("Skipping message missing device_name or timestamp")
        continue

    # Store in per-system typed table (full history)
    if _data_store.store(dev_name, timestamp, dev_type, data_type, payload):
        stored_count += 1

    # Update central summary DB (latest state per system, for dashboard)
    _metrics_db.update_summary(dev_name, dev_id, dev_type, timestamp, data_type, payload)
```

### 4c. New API route (optional, for dashboard queries)

```python
@app.route('/api/data/<system_name>/<data_type>', methods=['GET'])
@require_auth
def get_typed_data(system_name, data_type):
    """Get recent typed data for a system."""
    limit = request.args.get('limit', 100, type=int)
    rows  = _data_store.get_recent(system_name, data_type, limit=limit)
    return jsonify({"system": system_name, "data_type": data_type, "rows": rows}), 200
```

---

## 5. `manager/metrics.py` — Simplify to Central Summary DB

The per-system historical storage now lives in `TypedDataStore`. `MetricsDB` keeps only the
**latest-state-per-system** table used by the dashboard.

### Keep as-is
- `get_latest_for_all()` — dashboard still uses this
- `get_latest()` — still useful per-system
- `get_latest_disk_usage()` / `get_latest_disk_usage_for_all()` — keep for now

### Add: `update_summary()`
Replaces the separate `insert_metric()` and `insert_disk_usage()` calls with a single
generic method:

```python
def update_summary(self, name: str, system_id: str, device_type: str,
                   timestamp: str, data_type: str, data: dict):
    """
    Upsert the central summary record for a system.
    Stores the latest snapshot of each (name, data_type) pair.
    Dashboard reads from here; full history lives in TypedDataStore.
    """
    data_json = json.dumps({k: v for k, v in data.items() if k != 'data_type'})
    self.db.execute('''
        INSERT INTO system_latest (name, system_id, device_type, data_type, timestamp, data_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, data_type) DO UPDATE SET
            system_id   = excluded.system_id,
            device_type = excluded.device_type,
            timestamp   = excluded.timestamp,
            data_json   = excluded.data_json
    ''', (name, system_id, device_type, data_type or 'not_specified', timestamp, data_json))
    self.db.commit()
```

### New central table schema
```sql
CREATE TABLE IF NOT EXISTS system_latest (
    name        TEXT NOT NULL,
    system_id   TEXT,
    device_type TEXT DEFAULT 'unknown',
    data_type   TEXT NOT NULL DEFAULT 'not_specified',
    timestamp   DATETIME NOT NULL,
    data_json   TEXT NOT NULL,
    PRIMARY KEY (name, data_type)
)
```

This replaces the current two-table (`metrics`, `disk_usage`) approach with a single
flexible table. One row per (system, data_type) pair, always the latest.

### Remove (or deprecate)
- `insert_metric()` — replaced by `update_summary()`
- `insert_disk_usage()` — replaced by `update_summary()`
- `_init_system_db_schema()` / `get_system_db()` — per-system logic moves to `TypedDataStore`

---

## Migration Path for Existing Data

Existing collectors send the old format (`entries` with flat fields). Until they're updated:
- Manager can detect old format: `"entries" in data` → run old ingestion path as fallback
- New format: `"messages" in data` → run new path
- Both paths can coexist temporarily behind a single endpoint

```python
# Transition shim in ingest_queue():
if "messages" in data:
    messages = data["messages"]
    _process_new_format(messages, ...)
elif "entries" in data:
    # Legacy fallback
    _process_legacy_format(data.get("name"), data.get("id"), data.get("entries"), ...)
```

---

## Summary of File Changes

| File | Change type | What changes |
|------|-------------|--------------|
| `shared/models.py` | Rewrite | Add `MessageHeader`, `Message`, `QueuePayload` |
| `collector/collector.py` | Refactor | Entry shape → header + data; queue payload → `messages` |
| `manager/data_store.py` | **New file** | `TypedDataStore` with schema evolution |
| `manager/manager.py` | Refactor | `ingest_queue()` uses new format; init `TypedDataStore` |
| `manager/metrics.py` | Simplify | Central summary DB only; add `update_summary()`; remove per-system DB code |
