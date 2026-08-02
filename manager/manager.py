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
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
import re


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


def init_app(config: dict, logger: logging.Logger):
    """Initialize app with config"""
    global _config, _logger
    _config = config
    _logger = logger
    
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


@app.route('/api/usage/all', methods=['GET'])
@require_auth
def get_all_usage():
    """
    Get latest usage for all NAS systems (requires Bearer token).
    
    Returns: {nas_name: latest_snapshot}
    """
    try:
        data_dir = _config.get("data_dir", "/var/lib/lab-monitor/data")
        result = {}
        
        # Iterate over NAS directories
        if os.path.exists(data_dir):
            for nas_name in os.listdir(data_dir):
                nas_path = os.path.join(data_dir, nas_name)
                if os.path.isdir(nas_path):
                    log_path = os.path.join(nas_path, "usage.jsonl")
                    
                    if os.path.exists(log_path):
                        # Get last line (most recent report)
                        try:
                            with open(log_path, 'r') as f:
                                lines = f.readlines()
                                if lines:
                                    latest = json.loads(lines[-1])
                                    result[nas_name] = latest
                        except Exception as e:
                            _logger.error(f"Error reading {nas_name} log: {e}")
        
        return jsonify(result), 200
    
    except Exception as e:
        _logger.error(f"Error in get_all_usage: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/usage/nas/<nas_name>', methods=['GET'])
@require_auth
def get_nas_latest(nas_name):
    """
    Get latest usage for a specific NAS (requires Bearer token).
    
    Returns: Latest report or 404 if not found
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return jsonify({"error": "Invalid NAS name"}), 400
    try:
        log_path = get_usage_log_path(nas_name)
        
        if not os.path.exists(log_path):
            return jsonify({"error": "NAS not found"}), 404
        
        # Get last line
        with open(log_path, 'r') as f:
            lines = f.readlines()
            if lines:
                latest = json.loads(lines[-1])
                return jsonify(latest), 200
        
        return jsonify({"error": "No data for NAS"}), 404
    
    except Exception as e:
        _logger.error(f"Error fetching {nas_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/usage/history/<nas_name>', methods=['GET'])
@require_auth
def get_nas_history(nas_name):
    """
    Get usage history for a NAS within a date range (requires Bearer token).
    
    Query parameters:
        - days: Number of days to return (default: 30)
        - limit: Max number of records (default: no limit)
    
    Returns: List of historical reports
    """
    # Validate NAS name to prevent path traversal
    if not validate_nas_name(nas_name):
        return jsonify({"error": "Invalid NAS name"}), 400
    try:
        days = request.args.get('days', 30, type=int)
        limit = request.args.get('limit', None, type=int)
        
        log_path = get_usage_log_path(nas_name)
        
        if not os.path.exists(log_path):
            return jsonify({"error": "NAS not found"}), 404
        
        cutoff = datetime.utcnow() - timedelta(days=days)
        records = []
        
        with open(log_path, 'r') as f:
            for line in f:
                try:
                    record = json.loads(line)
                    ts = parse_iso_timestamp(record.get("timestamp", ""))
                    
                    if ts >= cutoff:
                        records.append(record)
                        if limit and len(records) >= limit:
                            break
                
                except (json.JSONDecodeError, ValueError):
                    continue
        
        return jsonify({
            "nas_name": nas_name,
            "days": days,
            "records": records
        }), 200
    
    except Exception as e:
        _logger.error(f"Error fetching history for {nas_name}: {e}")
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
