# Data Type Label Enhancement

## Overview

Add an optional `data_type` field to the data portion of each message. This allows the manager to apply special handling, validation, or transformation logic based on the data's semantic type, while maintaining backward compatibility with untyped (flat key-value) data.

## Current Message Structure

**Collector → Queue Entry:**
```json
{
  "type": "disk|metrics",
  "timestamp": "2026-08-02T12:00:00Z",
  "device_type": "synology|windows|other",
  "data": {
    "folders": [...],  // for disk
    "cpu_percent": 45.2,  // for metrics
    "ram_percent": 62.1,
    // ... other keys
  }
}
```

**Queue POST to Manager:**
```json
{
  "queue_id": "...",
  "name": "Triton",
  "id": "synology-triton",
  "entries": [
    { "type": "disk|metrics", "timestamp": "...", ... },
    { "type": "disk|metrics", "timestamp": "...", ... }
  ]
}
```

## Proposed Changes

### 1. Message Format (Collector)

Add optional `data_type` label inside the data section:

```json
{
  "type": "disk|metrics",
  "timestamp": "2026-08-02T12:00:00Z",
  "device_type": "synology|windows|other",
  "data": {
    "data_type": "folder_usage|system_metrics|custom_metric",  // OPTIONAL
    "folders": [...],
    "cpu_percent": 45.2,
    // ... arbitrary key-value pairs
  }
}
```

### 2. Storage & Processing (Manager)

### Storage Architecture: Separate Tables Per Data Type

Manager maintains **one table per data_type**, plus one for untyped data:

```
per-system database: data_dir/[system-name]/metrics.db
├── folder_usage
│   └── columns: timestamp, device_type, path, usage_bytes, ...
├── system_metrics
│   └── columns: timestamp, device_type, cpu_percent, ram_percent, uptime_seconds, ...
├── not-specified
│   └── columns: timestamp, dynamic (added as new keys appear)
└── [custom_type_1]
    └── columns: timestamp, device_type, key1, key2, ...
```

**Key behaviors:**
1. If `data_type` is present: Data goes to that type's table
2. If `data_type` is absent: Data goes to `not-specified` table
3. **Schema evolution**: When a new key appears within a type:
   - Manager adds that column to the table
   - Backfills all existing rows with `NAN`
   - Future data populates the column normally
4. **Type isolation**: No cross-contamination between data types

### 3. Implementation Steps

#### Phase 1: Add Data Type Label to Collector (No-op)
1. Modify `collector.py`:
   - Add `data_type` field to the entry's data section:
     - `"data_type": "folder_usage"` for disk mode
     - `"data_type": "system_metrics"` for metrics mode
   - This is backward compatible—manager can ignore it for now

**Example:**
```python
# In collect_disk_usage() and collect_metrics()
entry = {
    'type': 'disk',
    'timestamp': ...,
    'data': {
        'data_type': 'folder_usage',
        'folders': [...],
        'device_type': ...
    }
}
```

#### Phase 2: Extend Manager to Recognize Data Types
1. Modify `manager.py`:
   - Update `/api/data/queue` endpoint to read `data_type`
   - Route to type-specific storage handlers

2. Modify `metrics.py`:
   - Add `DataTypeHandler` base class with registry
   - Implement handlers for known types:
     - `folder_usage` → disk usage logic
     - `system_metrics` → metrics logic
     - `unknown` or `null` → flat key-value fallback

3. Create `data_types.py`:
   ```python
   class DataTypeHandler:
       def validate(self, data: dict) -> Tuple[bool, str]
       def store(self, system_name: str, timestamp: str, data: dict) -> bool
       def transform(self, data: dict) -> dict  # Optional pre-storage transformation
   
   class FolderUsageHandler(DataTypeHandler):
       # folder_usage schema: requires 'folders' array
   
   class SystemMetricsHandler(DataTypeHandler):
       # system_metrics schema: requires cpu_percent, ram_percent, etc.
   
   class GenericHandler(DataTypeHandler):
       # Fallback: flatten as-is into columns (or JSON blob)
   ```

#### Phase 3: Dashboard-Aware Rendering
1. Dashboard queries can now request data filtered by `data_type`
2. Dashboard can apply specialized formatting/visualization per type

## Schema Evolution Example

**Initial state:** `system_metrics` table has [timestamp, cpu_percent, ram_percent]

```sql
CREATE TABLE system_metrics (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME NOT NULL,
    cpu_percent REAL,
    ram_percent REAL
);
```

**Collector upgrade adds network latency metric:**

Collector sends:
```json
{
  "data_type": "system_metrics",
  "timestamp": "2026-08-04T14:00:00Z",
  "cpu_percent": 45.2,
  "ram_percent": 62.1,
  "network_latency_ms": 2.5
}
```

**Manager detects new key and:**
1. Adds column: `ALTER TABLE system_metrics ADD COLUMN network_latency_ms REAL;`
2. Backfills: `UPDATE system_metrics SET network_latency_ms = NAN WHERE network_latency_ms IS NULL;`
3. Logs the migration
4. Inserts new row with `network_latency_ms = 2.5`

**Result:** Queries can see when each system started reporting latency

## Benefits

| Aspect | Benefit |
|--------|----------|
| **Type Separation** | No mixing of folder_usage and system_metrics in one table |
| **Safe Evolution** | NAN backfill shows "we didn't measure this then" vs. 0 = "we measured zero" |
| **Query Performance** | Narrower tables, fewer NAN columns, faster scans |
| **Extensibility** | Add new data types without changing core logic |
| **Validation** | Type-specific validation catches collector errors early |
| **Transformations** | Type handlers can normalize/enrich data before storage |
| **Dashboard Integration** | UI can adapt rendering based on data type |
| **Backward Compatibility** | Absent `data_type` = `not-specified` table (safe default) |

## Example Flow

### Collector (No Changes Yet)
```python
# collector.py
entry = {
    'type': 'disk',
    'timestamp': '2026-08-02T12:00:00Z',
    'device_type': 'synology',
    'data': {
        'data_type': 'folder_usage',  # NEW
        'folders': [
            {'path': '/volume1/project-a', 'usage_bytes': 1099511627776}
        ]
    }
}
```

### Manager Ingestion
```python
# manager.py: /api/data/queue
for entry in entries:
    data = entry.get('data', {})
    data_type = data.get('data_type')  # NEW
    
    handler = get_handler(data_type)  # Resolves to FolderUsageHandler
    if handler.validate(data):
        handler.store(system_name, timestamp, data)
```

### Storage (Flexible)
- **Structured**: Disk table with `path, usage_bytes` columns
- **Flexible**: JSON blob for future ad-hoc metrics
- **Backward-compatible**: Unknown types fall back to key-value flattening

## Implementation Details: Schema Evolution Handler

```python
class TypedDataHandler:
    def store(self, system_name: str, data_type: str, timestamp: str, data: dict):
        """Store data with automatic schema evolution."""
        db = get_system_db(system_name)
        table = data_type or 'not-specified'
        
        # Get current schema for this table
        existing_cols = get_columns(db, table)
        incoming_keys = set(data.keys()) - {'data_type', 'timestamp'}  # Skip metadata
        
        # Detect new columns
        new_cols = incoming_keys - existing_cols
        
        if new_cols:
            for col in new_cols:
                # Add column and backfill
                db.execute(f'ALTER TABLE {table} ADD COLUMN {col} REAL;')
                db.execute(f'UPDATE {table} SET {col} = NAN;')
                logger.info(f"Added column {col} to {table}")
        
        # Insert new row
        db.insert(table, {'timestamp': timestamp, **data})
        db.commit()
```

## Testing Strategy

1. **Backward compatibility**: Entries without `data_type` go to `not-specified`
2. **Type isolation**: folder_usage and system_metrics data don't mix
3. **Schema evolution**: New keys are detected and columns added with NAN backfill
4. **Query validation**: Queries return NAN where data wasn't available
5. **Round-trip**: Data survives collection → queue → storage → query

## Future Considerations

- **Schema versioning**: Support breaking changes gracefully
- **Type aliases**: Map collector versions to handler versions  
- **Audit trail**: Track when columns were added and backfilled
- **Type registration**: Central registry of approved types (optional validation)
- **Compression**: Archive old data with sparse NAN values
- **Dashboard hints**: Metadata about which columns are new/experimental
