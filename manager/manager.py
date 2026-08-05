#!/usr/bin/env python3
"""
Lab Monitor Manager

Central service that receives usage reports from collectors and stores them.
Provides REST API for Dashboard queries.

Usage:
    python3 manager.py --config config.json
"""
import os
import json
import logging
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
import re

from metrics import MetricsDB
from data_store import TypedDataStore


# Setup logging
def setup_logging(log_file: str = None, log_level: str = "INFO"):
    """Configure logging"""
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, log_level))
    
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# Load config
def load_config(config_path: str) -> dict:
    """Load configuration from JSON"""
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        raise


# Initialize Flask app
app = Flask(__name__)

# Global config (set during startup)
_config = {}
_logger = None
_metrics_db = None
_data_store = None


def init_app(config: dict, logger: logging.Logger):
    """Initialize app with config"""
    global _config, _logger, _metrics_db, _data_store
    _config = config
    _logger = logger

    data_dir = config.get("data_dir", "data")
    os.makedirs(data_dir, exist_ok=True)

    # Central summary DB (latest state per system — dashboard layer)
    db_path = os.path.join(data_dir, "metrics.db")
    _metrics_db = MetricsDB(db_path, data_dir=data_dir)
    logger.info(f"Central metrics DB: {db_path}")

    # Per-system typed data store (full history)
    _data_store = TypedDataStore(data_dir=data_dir)
    logger.info(f"TypedDataStore root: {data_dir}")

    # Configure CORS with specific origins
    cors_origins = config.get("cors_origins", ["http://localhost:5001"])
    CORS(app, origins=cors_origins)


def get_nas_data_dir(nas_name: str) -> str:
    """Get data directory for a NAS"""
    data_dir = _config.get("data_dir", "/var/lib/lab-monitor/data")
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    return nas_dir


def get_usage_log_path(nas_name: str) -> str:
    """Get path to JSONL log for a NAS"""
    return os.path.join(get_nas_data_dir(nas_name), "usage.jsonl")


def append_report_to_log(report: dict, nas_name: str) -> bool:
    """Append a report to the NAS's JSONL log"""
    try:
        log_path = get_usage_log_path(nas_name)
        with open(log_path, 'a') as f:
            f.write(json.dumps(report) + '\n')
        return True
    except Exception as e:
        _logger.error(f"Failed to write report for {nas_name}: {e}")
        return False


def validate_token(token: str) -> bool:
    """Validate bearer token"""
    valid_tokens = _config.get("auth_tokens", [])
    return token in valid_tokens


def require_auth(f):
    """Decorator to require Bearer token authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            _logger.warning("Request missing Bearer token")
            return jsonify({"error": "Missing or invalid authorization"}), 401
        
        token = auth_header[7:]  # Remove "Bearer "
        if not validate_token(token):
            _logger.warning(f"Invalid token in GET request")
            return jsonify({"error": "Invalid token"}), 401
        
        return f(*args, **kwargs)
    return decorated_function


def validate_nas_name(nas_name: str) -> bool:
    """Validate NAS name (alphanumeric + hyphen/underscore only)"""
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', nas_name)) and len(nas_name) <= 100


def validate_path(path: str) -> bool:
    """Validate folder path (no traversal attacks)"""
    if '..' in path or path.startswith('/'):
        return False
    return len(path) <= 500


def validate_report(report: dict) -> tuple[bool, str]:
    """Validate report structure and data"""
    # Check required fields
    nas_name = report.get("nas_name")
    if not nas_name:
        return False, "Missing nas_name"
    
    if not validate_nas_name(nas_name):
        return False, f"Invalid nas_name format: {nas_name}"
    
    timestamp = report.get("timestamp")
    if not timestamp:
        return False, "Missing timestamp"
    
    # Validate timestamp format
    try:
        parse_iso_timestamp(timestamp)
    except ValueError:
        return False, f"Invalid timestamp format: {timestamp}"
    
    # Validate folders array
    folders = report.get("folders", [])
    if not isinstance(folders, list):
        return False, "Folders must be an array"
    
    for folder in folders:
        path = folder.get("path")
        if not path:
            return False, "Folder missing path"
        
        if not validate_path(path):
            return False, f"Invalid folder path: {path}"
        
        usage = folder.get("usage_bytes")
        if not isinstance(usage, int) or usage < 0:
            return False, f"Invalid usage_bytes for {path}"
    
    return True, "Valid"


def cleanup_old_reports(days: int = None):
    """Delete reports older than retention_days"""
    if days is None:
        days = _config.get("retention_days")
    
    if not days:
        return  # Retention disabled
    
    try:
        data_dir = _config.get("data_dir", "/var/lib/lab-monitor/data")
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        for nas_name in os.listdir(data_dir):
            nas_path = os.path.join(data_dir, nas_name)
            if not os.path.isdir(nas_path):
                continue
            
            log_path = os.path.join(nas_path, "usage.jsonl")
            if not os.path.exists(log_path):
                continue
            
            # Read all reports and filter
            kept_reports = []
            deleted_count = 0
            
            with open(log_path, 'r') as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        ts = parse_iso_timestamp(record.get("timestamp", ""))
                        if ts >= cutoff:
                            kept_reports.append(line)
                        else:
                            deleted_count += 1
                    except (json.JSONDecodeError, ValueError):
                        # Keep unparseable lines
                        kept_reports.append(line)
            
            # Write back
            with open(log_path, 'w') as f:
                f.writelines(kept_reports)
            
            if deleted_count > 0:
                _logger.info(f"Cleaned {deleted_count} old reports from {nas_name}")
    
    except Exception as e:
        _logger.error(f"Error during cleanup: {e}")


def parse_iso_timestamp(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp with error handling"""
    try:
        # Handle "2026-07-29T02:00:00Z" format
        if ts_str.endswith('Z'):
            ts_str = ts_str[:-1]
        return datetime.fromisoformat(ts_str)
    except (ValueError, AttributeError) as e:
        _logger.warning(f"Invalid timestamp '{ts_str}': {e}")
        raise ValueError(f"Invalid ISO 8601 timestamp: {ts_str}")


# Routes

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"}), 200


@app.route('/api/usage/report', methods=['POST'])
def ingest_reports():
    """
    Receive and store usage reports from collectors.
    
    Accepts BOTH formats:
    1. Single report (no array):
       {
           "nas_name": "nas-01",
           "nas_id": "synology_01",
           "timestamp": "2026-07-29T02:00:00Z",
           "folders": [...]
       }
    
    2. Array of reports:
       {
           "reports": [{ ... }, { ... }]
       }
    
    Returns: 200 OK on success, 400/401 on error
    """
    # Check authentication
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        _logger.warning("Request missing Bearer token")
        return jsonify({"error": "Missing or invalid authorization"}), 401
    
    token = auth_header[7:]  # Remove "Bearer "
    if not validate_token(token):
        _logger.warning("Invalid token received")
        return jsonify({"error": "Invalid token"}), 401
    
    # Parse request
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Empty request body"}), 400
        
        # Support both single report and array of reports
        if "reports" in data:
            reports = data["reports"]
            if not isinstance(reports, list):
                return jsonify({"error": "Reports must be a list"}), 400
        elif "nas_name" in data:
            # Single report format
            reports = [data]
        else:
            return jsonify({"error": "Invalid request format: must have 'reports' array or 'nas_name' field"}), 400
    
    except Exception as e:
        _logger.error(f"Failed to parse request: {e}")
        return jsonify({"error": "Invalid JSON"}), 400
    
    # Process each report
    success_count = 0
    failed_reports = []
    
    for report in reports:
        # Validate report structure
        is_valid, error_msg = validate_report(report)
        if not is_valid:
            _logger.warning(f"Invalid report: {error_msg}")
            failed_reports.append(error_msg)
            continue
        
        nas_name = report.get("nas_name")
        if append_report_to_log(report, nas_name):
            success_count += 1
            _logger.debug(f"Stored report from {nas_name}")  # DEBUG level
        else:
            _logger.error(f"Failed to store report from {nas_name}")
            failed_reports.append(f"Storage failed for {nas_name}")
    
    # Run cleanup if retention_days is configured
    if _config.get("retention_days"):
        cleanup_old_reports()
    
    if success_count == len(reports):
        _logger.info(f"Successfully stored {success_count} report(s)")
        return jsonify({
            "status": "ok",
            "stored": success_count
        }), 200
    elif success_count > 0:
        _logger.warning(f"Partial success: {success_count}/{len(reports)} stored")
        return jsonify({
            "status": "partial",
            "stored": success_count,
            "total": len(reports),
            "errors": failed_reports
        }), 200  # Still 200 to acknowledge receipt
    else:
        _logger.error(f"All {len(reports)} reports failed validation")
        return jsonify({
            "status": "error",
            "stored": 0,
            "total": len(reports),
            "errors": failed_reports
        }), 400


@app.route('/api/data/queue', methods=['POST'])
@require_auth
def ingest_queue():
    """
    Receive and store a queue batch from a collector.

    New format (collectors v2+):
    {
        "queue_id": "Triton-2026-08-04-07-00-00",
        "name":     "Triton",
        "id":       "synology-triton",
        "messages": [
            {
                "header": {
                    "device_name": "Triton",
                    "device_id":   "synology-triton",
                    "device_type": "synology",
                    "timestamp":   "2026-08-04T07:00:00Z"
                },
                "data": {
                    "data_type": "folder_usage",
                    "folders": [...]
                }
            },
            ...
        ]
    }

    Legacy format (collectors v1 — backward compat):
    {
        "queue_id": "...",
        "name":     "Triton",
        "id":       "synology-triton",
        "entries":  [ { "type": "disk|metrics", "timestamp": "...", ... } ]
    }

    Returns: {"status": "ok", "queue_id": "...", "stored": N}
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Empty request"}), 400

        queue_id  = data.get("queue_id")
        name      = data.get("name")
        system_id = data.get("id")

        if not all([queue_id, name, system_id]):
            return jsonify({"error": "Missing required fields: queue_id, name, id"}), 400

        # ---------------------------------------------------------------- #
        # Route to new or legacy handler based on payload shape             #
        # ---------------------------------------------------------------- #
        if "messages" in data:
            stored_count, total = _ingest_messages(name, system_id, data["messages"])
        elif "entries" in data:
            _logger.info(
                f"Legacy queue format from {name} — consider upgrading collector"
            )
            stored_count, total = _ingest_legacy_entries(name, system_id, data["entries"])
        else:
            return jsonify({"error": "Payload must contain 'messages' or 'entries'"}), 400

        _logger.info(
            f"Processed queue {queue_id} from {name}: "
            f"stored {stored_count}/{total} messages"
        )

        return jsonify({
            "status":    "ok",
            "queue_id":  queue_id,
            "stored":    stored_count,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }), 200

    except Exception as e:
        _logger.error(f"Queue ingestion failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def _ingest_messages(name: str, system_id: str, messages: list) -> tuple:
    """
    Process the new message format: list of {header, data} dicts.
    Returns (stored_count, total).
    """
    stored = 0
    for msg in messages:
        header  = msg.get("header", {})
        payload = msg.get("data",   {})

        dev_name  = header.get("device_name", name)
        dev_id    = header.get("device_id",   system_id)
        dev_type  = header.get("device_type", "unknown")
        timestamp = header.get("timestamp")
        data_type = payload.get("data_type")  # may be None → not_specified table

        if not timestamp:
            _logger.warning(f"Skipping message from {dev_name}: missing timestamp")
            continue

        # Store full history in per-system typed table
        ok = _data_store.store(dev_name, timestamp, dev_type, data_type, payload)

        # ---- Central DB updates ----
        # 1. Always: register/refresh device
        _metrics_db.upsert_device(dev_name, dev_id, dev_type, timestamp)

        # 2. Data-type specific snapshot + totals
        if data_type == 'system_metrics':
            _metrics_db.update_snapshot_metrics(dev_name, timestamp, payload)
            _metrics_db.accumulate_network(
                dev_name,
                int(payload.get('network_bytes_in',  0) or 0),
                int(payload.get('network_bytes_out', 0) or 0),
                timestamp,
            )
        elif data_type == 'folder_usage':
            _metrics_db.update_snapshot_disk(dev_name, timestamp, payload)
            _metrics_db.update_disk_total(
                dev_name,
                int(payload.get('total_usage', 0) or 0),
                timestamp,
            )
        else:
            # Unknown / not_specified: nothing to do in central DB
            pass

        if ok:
            stored += 1
            _logger.debug(f"Stored {data_type or 'not_specified'} message from {dev_name}")
        else:
            _logger.warning(f"Failed to store message from {dev_name}")

    return stored, len(messages)


def _ingest_legacy_entries(name: str, system_id: str, entries: list) -> tuple:
    """
    Backward-compat handler for v1 collector entries (flat dicts with 'type' field).
    Translates old format into new message shape and routes through _ingest_messages.
    """
    messages = []
    for entry in entries:
        entry_type = entry.get("type", "unknown")
        timestamp  = entry.get("timestamp")
        dev_type   = entry.get("device_type", "unknown")

        if not timestamp:
            continue

        # Map legacy type names to new data_type labels
        data_type_map = {
            "disk":    "folder_usage",
            "metrics": "system_metrics",
        }
        data_type = data_type_map.get(entry_type, entry_type)

        # Build payload: everything except the legacy envelope fields
        envelope_keys = {"type", "timestamp", "device_type"}
        payload = {k: v for k, v in entry.items() if k not in envelope_keys}
        payload["data_type"] = data_type

        messages.append({
            "header": {
                "device_name": name,
                "device_id":   system_id,
                "device_type": dev_type,
                "timestamp":   timestamp,
            },
            "data": payload,
        })

    return _ingest_messages(name, system_id, messages)


@app.route('/api/usage/all', methods=['GET'])
@require_auth
def get_all_usage():
    """
    Get latest disk usage for all systems (requires Bearer token).
    
    Returns: {system_name: latest_disk_usage}
    """
    try:
        result = _metrics_db.get_latest_disk_usage_for_all()
        return jsonify(result), 200
    
    except Exception as e:
        _logger.error(f"Error in get_all_usage: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/usage/nas/<nas_name>', methods=['GET'])
@require_auth
def get_nas_latest(nas_name):
    """
    Get latest disk usage for a specific system (requires Bearer token).
    
    Returns: Latest report or 404 if not found
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return jsonify({"error": "Invalid NAS name"}), 400
    try:
        latest = _metrics_db.get_latest_disk_usage(nas_name)
        
        if not latest:
            return jsonify({"error": "System not found"}), 404
        
        return jsonify(latest), 200
    
    except Exception as e:
        _logger.error(f"Error fetching {nas_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/usage/history/<nas_name>', methods=['GET'])
@require_auth
def get_nas_history(nas_name):
    """
    Get disk usage history for a system within a date range (requires Bearer token).
    
    Query parameters:
        - days: Number of days to return (default: 30)
        - limit: Max number of records (default: no limit)
    
    Returns: List of historical disk usage records
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return jsonify({"error": "Invalid NAS name"}), 400
    try:
        days = request.args.get('days', 30, type=int)
        limit = request.args.get('limit', None, type=int)
        
        records = _metrics_db.get_disk_usage_history(nas_name, days=days, limit=limit)
        
        return jsonify({
            "system_name": nas_name,
            "days": days,
            "records": records
        }), 200
    
    except Exception as e:
        _logger.error(f"Error fetching history for {nas_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/metrics/all', methods=['GET'])
@require_auth
def get_all_metrics():
    """
    Get latest metrics for all NAS systems from SQLite (requires Bearer token).
    Returns the most recent metric entry for each NAS.
    
    Returns: {nas_name: {cpu_percent, ram_percent, network_bytes_in, network_bytes_out, timestamp}}
    """
    try:
        if not _metrics_db:
            return jsonify({"error": "Metrics database not initialized"}), 500
        
        result = _metrics_db.get_latest_for_all()
        return jsonify(result), 200
    
    except Exception as e:
        _logger.error(f"Error fetching all metrics: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/metrics/nas/<nas_name>', methods=['GET'])
@require_auth
def get_nas_metrics(nas_name):
    """
    Get latest metrics for a specific NAS (requires Bearer token).
    
    Returns: {cpu_percent, ram_percent, network_bytes_in, network_bytes_out, timestamp}
    """
    if not validate_nas_name(nas_name):
        return jsonify({"error": "Invalid NAS name"}), 400
    
    try:
        if not _metrics_db:
            return jsonify({"error": "Metrics database not initialized"}), 500
        
        metric = _metrics_db.get_latest(nas_name)
        if metric:
            return jsonify(metric), 200
        else:
            return jsonify({"error": "No metrics found for NAS"}), 404
    
    except Exception as e:
        _logger.error(f"Error fetching metrics for {nas_name}: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Typed data query endpoints (new — full history via TypedDataStore)
# ---------------------------------------------------------------------------

@app.route('/api/data/<system_name>', methods=['GET'])
@require_auth
def get_system_data_types(system_name):
    """
    List the data_types that have been recorded for a system.
    Returns: {"system": "Triton", "data_types": ["folder_usage", "system_metrics"]}
    """
    if not validate_nas_name(system_name):
        return jsonify({"error": "Invalid system name"}), 400
    try:
        types = _data_store.list_data_types(system_name)
        return jsonify({"system": system_name, "data_types": types}), 200
    except Exception as e:
        _logger.error(f"Error listing data types for {system_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/data/<system_name>/<data_type>', methods=['GET'])
@require_auth
def get_typed_data(system_name, data_type):
    """
    Get recent historical rows for a (system, data_type) pair.

    Query params:
        limit  int  Max rows to return (default 100)
        start  str  ISO 8601 start timestamp (inclusive)
        end    str  ISO 8601 end timestamp (exclusive)

    Returns: {"system": ..., "data_type": ..., "rows": [...]}
    """
    if not validate_nas_name(system_name):
        return jsonify({"error": "Invalid system name"}), 400
    try:
        limit = request.args.get('limit', 100, type=int)
        start = request.args.get('start')
        end   = request.args.get('end')

        if start and end:
            rows = _data_store.get_between(system_name, data_type, start, end)
        else:
            rows = _data_store.get_recent(system_name, data_type, limit=limit)

        return jsonify({
            "system":    system_name,
            "data_type": data_type,
            "rows":      rows,
        }), 200
    except Exception as e:
        _logger.error(f"Error fetching {data_type} data for {system_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/systems', methods=['GET'])
@require_auth
def list_systems():
    """
    List all registered systems.
    Returns: {"systems": ["Triton", "Atlas", ...]}
    """
    try:
        systems = _metrics_db.get_all_systems()
        return jsonify({"systems": systems}), 200
    except Exception as e:
        _logger.error(f"Error listing systems: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/devices', methods=['GET'])
@require_auth
def get_devices():
    """
    Full device registry — name, system_id, device_type, first_seen, last_seen.
    Returns: {"devices": [{...}, ...]}
    """
    try:
        return jsonify({"devices": _metrics_db.get_all_devices()}), 200
    except Exception as e:
        _logger.error(f"Error fetching devices: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/totals', methods=['GET'])
@require_auth
def get_totals():
    """
    Running cumulative totals — per-device and global.
    Returns:
        {
            "global": {total_bytes_in, total_bytes_out, total_disk_bytes},
            "devices": {"Triton": {...}, ...}
        }
    """
    try:
        return jsonify({
            "global":  _metrics_db.get_global_totals(),
            "devices": _metrics_db.get_all_totals(),
        }), 200
    except Exception as e:
        _logger.error(f"Error fetching totals: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/history/<system_name>/<data_type>/<field>', methods=['GET'])
def get_metric_history(system_name: str, data_type: str, field: str):
    """
    Fetch time-series data for a specific field within a system's data_type table.
    
    Args:
        system_name: System name (e.g., "triton5")
        data_type:   Table name (e.g., "system_metrics")
        field:       Column name (e.g., "cpu_percent", "ram_percent")
    
    Optional query params:
        ?limit=100   Return last N records (default 100)
    
    Returns:
        {
            "system_name": "triton5",
            "data_type": "system_metrics",
            "field": "cpu_percent",
            "data": [
                {"timestamp": "2026-08-04T18:30:00", "value": 45.2},
                {"timestamp": "2026-08-04T18:35:00", "value": 48.1},
                ...
            ]
        }
    """
    limit = request.args.get('limit', default=100, type=int)
    limit = min(limit, 500)  # cap at 500 to prevent abuse
    
    try:
        records = _data_store.get_recent(system_name, data_type, limit=limit)
        
        # Extract timestamps and the requested field
        data = []
        for record in reversed(records):  # oldest first
            ts = record.get('timestamp')
            val = record.get(field)
            if ts is not None and val is not None:
                # Handle stored values (might be JSON strings for complex types)
                if isinstance(val, str):
                    try:
                        val = float(val)
                    except (ValueError, TypeError):
                        continue
                data.append({
                    'timestamp': ts,
                    'value': float(val) if val is not None else None
                })
        
        return jsonify({
            'system_name': system_name,
            'data_type': data_type,
            'field': field,
            'data': data
        }), 200
    
    except Exception as e:
        _logger.error(f"Error fetching history for {system_name}/{data_type}/{field}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/system/<name>', methods=['DELETE'])
@require_auth
def delete_system(name: str):
    """
    Permanently delete all data for a system.

    Removes:
      - Per-system history DB:  data/<name>/data.db
      - Central summary rows:   devices, device_snapshot, device_totals

    This is irreversible. The collector's local JSONL archives are the
    source of truth and can be used to rebuild the manager if needed.

    Returns:
        {"status": "ok", "deleted": "<name>"}
    """
    if not name or not re.match(r'^[\w\-\.]+$', name):
        return jsonify({"error": "Invalid system name"}), 400

    _logger.warning(f"DELETE /api/system/{name} — permanently deleting all data for '{name}'")

    try:
        db_deleted  = _data_store.delete_system(name)
        rows_deleted = _metrics_db.delete_system(name)

        _logger.info(f"System '{name}' wiped: db_deleted={db_deleted}, central_rows_cleared={rows_deleted}")

        return jsonify({
            "status":  "ok",
            "deleted": name,
            "per_system_db_deleted": db_deleted,
            "central_db_rows_cleared": rows_deleted,
        }), 200

    except Exception as e:
        _logger.error(f"Error deleting system '{name}': {e}")
        return jsonify({"error": str(e)}), 500


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="Lab Monitor Manager")
    parser.add_argument("--config", default="config.json", help="Config file")
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Setup logging
    logger = setup_logging(
        config.get("log_file"),
        config.get("log_level", "INFO")
    )
    
    # Initialize app
    init_app(config, logger)
    
    logger.info("=" * 60)
    logger.info("Lab Monitor Manager starting")
    logger.info(f"Data directory: {config.get('data_dir')}")
    logger.info("=" * 60)
    
    # Run Flask app
    host = config.get("host", "0.0.0.0")
    port = config.get("port", 5000)
    debug = config.get("debug", False)
    
    logger.info(f"Listening on {host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
