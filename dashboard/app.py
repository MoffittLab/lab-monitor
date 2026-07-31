#!/usr/bin/env python3
"""
Lab Monitor Dashboard

Web interface for viewing NAS usage statistics.
Polls Manager API and displays real-time data.

Usage:
    python3 app.py --config config.json
"""
import os
import json
import logging
import argparse
import requests
from datetime import datetime

from flask import Flask, render_template, jsonify, request


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


# Initialize Flask
app = Flask(__name__)

# Global config
_config = {}
_logger = None


def init_app(config: dict, logger: logging.Logger):
    """Initialize app with config"""
    global _config, _logger
    _config = config
    _logger = logger


def format_bytes(bytes_value: int) -> str:
    """Convert bytes to human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.2f} PB"


def get_manager_data() -> dict:
    """Fetch latest data from Manager API"""
    try:
        manager_url = _config.get("manager_url", "http://localhost:5000")
        timeout = _config.get("manager_timeout_seconds", 5)
        
        response = requests.get(
            f"{manager_url}/api/usage/all",
            timeout=timeout
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            _logger.warning(f"Manager returned {response.status_code}")
            return {}
    
    except requests.exceptions.Timeout:
        _logger.warning("Timeout fetching from Manager")
        return {}
    except Exception as e:
        _logger.error(f"Error fetching from Manager: {e}")
        return {}


def process_nas_data(nas_data: dict) -> dict:
    """Process raw NAS data for display"""
    processed = {
        "nas_name": nas_data.get("nas_name", "Unknown"),
        "nas_id": nas_data.get("nas_id"),
        "timestamp": nas_data.get("timestamp"),
        "folders": [],
        "total_usage_bytes": 0,
        "total_usage_formatted": "0 B"
    }
    
    for folder in nas_data.get("folders", []):
        path = folder.get("path")
        usage = folder.get("usage_bytes", 0)
        
        processed["folders"].append({
            "path": path,
            "usage_bytes": usage,
            "usage_formatted": format_bytes(usage)
        })
        
        processed["total_usage_bytes"] += usage
    
    processed["total_usage_formatted"] = format_bytes(processed["total_usage_bytes"])
    return processed


# Routes

@app.route('/')
def index():
    """Main dashboard page"""
    manager_url = _config.get("manager_url", "http://localhost:5000")
    refresh_interval = _config.get("refresh_interval_seconds", 30)
    
    return render_template(
        'index.html',
        manager_url=manager_url,
        refresh_interval=refresh_interval
    )


@app.route('/api/data')
def get_data():
    """API endpoint for dashboard data (called by JavaScript)"""
    manager_data = get_manager_data()
    
    # Process each NAS
    processed = {}
    for nas_name, nas_data in manager_data.items():
        processed[nas_name] = process_nas_data(nas_data)
    
    return jsonify({
        "success": True,
        "timestamp": datetime.utcnow().isoformat(),
        "nas_systems": processed
    })


@app.route('/api/nas/<nas_name>/history')
def get_history(nas_name):
    """Get historical data for a NAS"""
    try:
        manager_url = _config.get("manager_url", "http://localhost:5000")
        days = request.args.get('days', 30)
        
        response = requests.get(
            f"{manager_url}/api/usage/history/{nas_name}?days={days}",
            timeout=5
        )
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Not found"}), 404
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="Lab Monitor Dashboard")
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
    logger.info("Lab Monitor Dashboard starting")
    manager_url = config.get("manager_url", "http://localhost:5000")
    logger.info(f"Manager URL: {manager_url}")
    logger.info("=" * 60)
    
    # Run Flask app
    host = config.get("host", "0.0.0.0")
    port = config.get("port", 5001)
    debug = config.get("debug", False)
    
    logger.info(f"Listening on {host}:{port}")
    logger.info(f"Access at http://localhost:{port}/")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
