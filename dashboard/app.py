#!/usr/bin/env python3
"""
Lab Monitor Dashboard

Web interface for viewing system usage statistics.
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


# ---------------------------------------------------------------------------
# Logging / config
# ---------------------------------------------------------------------------

def setup_logging(log_file: str = None, log_level: str = "INFO"):
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, log_level))
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def load_config(config_path: str) -> dict:
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        raise


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

_config = {}
_logger = None


def init_app(config: dict, logger: logging.Logger):
    global _config, _logger
    _config = config
    _logger = logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_bytes(bytes_value) -> str:
    """Convert bytes to human-readable string."""
    try:
        v = float(bytes_value)
        if v != v:  # NaN check
            return 'N/A'
    except (TypeError, ValueError):
        return 'N/A'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if v < 1024:
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} PB"


def _manager_headers() -> dict:
    token = _config.get("manager_token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _manager_get(path: str, params: dict = None) -> dict | list | None:
    """GET from manager; returns parsed JSON or None on error."""
    try:
        url     = _config.get("manager_url", "http://localhost:5000").rstrip('/') + path
        timeout = _config.get("manager_timeout_seconds", 5)
        r = requests.get(url, headers=_manager_headers(), params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        _logger.warning(f"Manager {path} returned {r.status_code}")
    except requests.exceptions.Timeout:
        _logger.warning(f"Timeout fetching {path}")
    except Exception as e:
        _logger.error(f"Error fetching {path}: {e}")
    return None


def parse_folder_usage(raw: dict) -> dict:
    """
    Convert the flat folder_usage summary dict (from system_latest) into a
    structured form the frontend can consume.

    Input keys fall into four categories:
      - Metadata: name, system_id, device_type, data_type, timestamp
      - Leaf folders:  /volume1/JeffMoffitt  (depth > 1 after splitting on /)
      - Volume roots:  /volume1              (depth == 1)
      - Grand total:   total_usage

    Returns:
        {
            folders:              [{path, usage_bytes, usage_formatted}, ...]
            volumes:              [{path, usage_bytes, usage_formatted}, ...]
            total_usage_bytes:    int
            total_usage_formatted: str
        }
    """
    METADATA = {'name', 'system_id', 'device_type', 'data_type', 'timestamp', 'total_disk_bytes', '_unit'}
    unit_default = raw.get('_unit')  # message-level unit default for all numeric fields

    folders  = {}
    volumes  = {}
    capacity = {}   # vol_path -> {'total_bytes': int, 'free_bytes': int}
    total    = 0

    for key, val in raw.items():
        if key in METADATA:
            continue
        if key == 'total_usage':
            try:
                total = int(val) if val == val else 0   # NaN guard
            except (TypeError, ValueError):
                pass
            continue

        # Capacity keys: /volume1_total_bytes, /volume1_free_bytes
        # Must be checked before generic depth classification
        for suffix, cap_key in (('_total_bytes', 'total_bytes'), ('_free_bytes', 'free_bytes')):
            if key.endswith(suffix):
                vol_path = key[:-len(suffix)]
                try:
                    v = float(val)
                    if v == v:   # NaN guard
                        capacity.setdefault(vol_path, {})[cap_key] = int(v)
                except (TypeError, ValueError):
                    pass
                break
        else:
            # Not a capacity key — must be numeric and not NaN
            try:
                v = float(val)
                if v != v:
                    continue
                v = int(v)
            except (TypeError, ValueError):
                continue

            # Classify: split on forward slash (normalise backslash too)
            parts = [p for p in key.replace('\\', '/').split('/') if p]
            if len(parts) <= 1:
                volumes[key] = v   # e.g. /volume1  or  E:
            else:
                folders[key] = v   # e.g. /volume1/JeffMoffitt

    # Build volume list, attaching capacity data where available
    vol_list = []
    for k, v in sorted(volumes.items()):
        cap   = capacity.get(k, {})
        entry = {
            'path':            k,
            'usage_bytes':     v,
            'usage_formatted': format_bytes(v),
        }
        if 'total_bytes' in cap:
            entry['total_bytes']     = cap['total_bytes']
            entry['total_formatted'] = format_bytes(cap['total_bytes'])
        if 'free_bytes' in cap:
            entry['free_bytes']     = cap['free_bytes']
            entry['free_formatted'] = format_bytes(cap['free_bytes'])
        vol_list.append(entry)

    return {
        'folders': [
            {'path': k, 'usage_bytes': v, 'usage_formatted': format_bytes(v)}
            for k, v in sorted(folders.items())
        ],
        'volumes':               vol_list,
        'total_usage_bytes':     total,
        'total_usage_formatted': format_bytes(total),
        'units':                 {'_unit': unit_default} if unit_default else {},
    }


def get_all_systems_data() -> tuple:
    """
    Fetch both system_metrics and folder_usage for every system and merge
    into a single dict keyed by system name.

    Returns:
        {
            "Triton": {
                "name":        "Triton",
                "device_type": "synology",
                "metrics":     { cpu_percent, ram_percent, ... } or None,
                "disk":        { folders, volumes, total_usage_bytes, ... } or None,
            },
            ...
        }
    """
    systems: dict = {}

    # --- device registry (authoritative system list) ---
    devices_resp = _manager_get('/api/devices') or {}
    for dev in devices_resp.get('devices', []):
        name = dev['name']
        systems[name] = {
            'name':        name,
            'system_id':   dev.get('system_id'),
            'device_type': dev.get('device_type', 'unknown'),
            'first_seen':  dev.get('first_seen'),
            'last_seen':   dev.get('last_seen'),
            'metrics':     None,
            'disk':        None,
            'totals':      None,
        }

    # --- metrics snapshots ---
    metrics_all = _manager_get('/api/metrics/all') or {}
    for name, data in metrics_all.items():
        systems.setdefault(name, {
            'name': name, 'system_id': None,
            'device_type': data.get('device_type', 'unknown'),
            'first_seen': None, 'last_seen': None,
            'metrics': None, 'disk': None, 'totals': None,
        })
        systems[name]['metrics'] = {
            'timestamp':                    data.get('timestamp'),
            'cpu_percent':                  data.get('cpu_percent', 0),
            'ram_percent':                  data.get('ram_percent', 0),
            'uptime_seconds':               data.get('uptime_seconds', 0),
            'uptime_formatted':             data.get('uptime_formatted', '0s'),
            'network_bytes_in':             data.get('network_bytes_in', 0),
            'network_bytes_out':            data.get('network_bytes_out', 0),
            'network_bandwidth_in_mbps':    data.get('network_bandwidth_in_mbps', 0.0),
            'network_bandwidth_out_mbps':   data.get('network_bandwidth_out_mbps', 0.0),
            'units':                        data.get('units', {}),
        }

    # --- disk snapshots ---
    disk_all = _manager_get('/api/usage/all') or {}
    for name, data in disk_all.items():
        systems.setdefault(name, {
            'name': name, 'system_id': None,
            'device_type': data.get('device_type', 'unknown'),
            'first_seen': None, 'last_seen': None,
            'metrics': None, 'disk': None, 'totals': None,
        })
        parsed = parse_folder_usage(data)
        parsed['timestamp'] = data.get('timestamp')
        systems[name]['disk'] = parsed

    # --- per-device running totals ---
    totals_resp = _manager_get('/api/totals') or {}
    for name, t in totals_resp.get('devices', {}).items():
        if name in systems:
            systems[name]['totals'] = {
                'total_bytes_in':   t.get('total_bytes_in',   0),
                'total_bytes_out':  t.get('total_bytes_out',  0),
                'total_disk_bytes': t.get('total_disk_bytes', 0),
            }

    # --- per-user metrics snapshot ---
    user_all = _manager_get('/api/user_metrics/all') or {}
    for name, users in user_all.items():
        if name in systems:
            systems[name]['users'] = users

    # Surface most-recent timestamp per system
    for sys in systems.values():
        ts = (sys.get('metrics') or {}).get('timestamp') \
          or (sys.get('disk')    or {}).get('timestamp')
        sys['timestamp'] = ts

    return systems, totals_resp.get('global', {})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template(
        'index.html',
        manager_url=_config.get("manager_url", "http://localhost:5000"),
        refresh_interval=_config.get("refresh_interval_seconds", 30)
    )


@app.route('/api/data')
def get_data():
    """Main data endpoint polled by the frontend JavaScript."""
    systems, global_totals = get_all_systems_data()
    return jsonify({
        "success":       True,
        "timestamp":     datetime.utcnow().isoformat() + 'Z',
        "systems":       systems,
        "global_totals": global_totals,
    })


@app.route('/api/history/<system_name>/<data_type>/<field>')
def get_metric_history(system_name, data_type, field):
    """Proxy metric time-series data from Manager for frontend charting."""
    try:
        limit = request.args.get('limit', 100, type=int)
        if limit < 1 or limit > 500:
            limit = min(limit, 500)
        
        # Forward to Manager's history endpoint
        data = _manager_get(
            f'/api/history/{system_name}/{data_type}/{field}',
            params={'limit': limit}
        )
        
        if data is None:
            return jsonify({'error': 'Manager unreachable or not found'}), 503
        
        return jsonify(data), 200
    
    except Exception as e:
        _logger.error(f"Error fetching metric history: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/system/<system_name>/disk_history')
def get_disk_history(system_name):
    """Historical folder_usage rows for a system."""
    try:
        limit = request.args.get('limit', 30, type=int)
        if limit < 1 or limit > 500:
            return jsonify({"error": "limit must be 1–500"}), 400

        data = _manager_get(f'/api/data/{system_name}/folder_usage',
                            params={'limit': limit})
        if data is None:
            return jsonify({"error": "Not found or manager unreachable"}), 404

        # Parse each historical row so the frontend gets structured data
        rows = []
        for row in data.get('rows', []):
            parsed = parse_folder_usage(row)
            parsed['timestamp'] = row.get('timestamp')
            rows.append(parsed)

        return jsonify({"system": system_name, "rows": rows})

    except Exception as e:
        _logger.error(f"Error fetching disk history for {system_name}: {e}")
        return jsonify({"error": "Internal error"}), 500


@app.route('/api/system/<system_name>/metrics_history')
def get_metrics_history(system_name):
    """Historical system_metrics rows for a system."""
    try:
        limit = request.args.get('limit', 100, type=int)
        if limit < 1 or limit > 1000:
            return jsonify({"error": "limit must be 1–1000"}), 400

        data = _manager_get(f'/api/data/{system_name}/system_metrics',
                            params={'limit': limit})
        if data is None:
            return jsonify({"error": "Not found or manager unreachable"}), 404

        return jsonify(data)

    except Exception as e:
        _logger.error(f"Error fetching metrics history for {system_name}: {e}")
        return jsonify({"error": "Internal error"}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Lab Monitor Dashboard")
    parser.add_argument("--config", default="config.json", help="Config file")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("log_file"), config.get("log_level", "INFO"))
    init_app(config, logger)

    logger.info("=" * 60)
    logger.info("Lab Monitor Dashboard starting")
    logger.info(f"Manager URL: {config.get('manager_url', 'http://localhost:5000')}")
    logger.info("=" * 60)

    host  = config.get("host", "0.0.0.0")
    port  = config.get("port", 5001)
    debug = config.get("debug", False)
    logger.info(f"Listening on {host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
