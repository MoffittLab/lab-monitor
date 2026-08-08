#!/usr/bin/env python3
r"""
Lab Monitor Collector

Runs on each NAS/Windows system with two modes:

DISK MODE (daily):
  1. Measures disk usage of configured folders (auto-discovers if not specified)
  2. Appends to local archive (monthly JSONL)
  3. Appends entry to queue.json
  4. POSTs queue to Manager
  5. On success, deletes queue.json

METRICS MODE (every 5 minutes):
  1. Measures CPU%, RAM%, network traffic
  2. Appends to local archive (monthly JSONL)
  3. Appends entry to queue.json
  4. POSTs queue to Manager
  5. On success, deletes queue.json

Usage:
    python collector.py --config config.json --mode disk     # Daily
    python collector.py --config config.json --mode metrics  # Every 5 min

Configuration:
    If 'volumes' is omitted, collector auto-discovers volume* directories
    (Synology: /volume1, /volume2, etc.)
    (Windows: E:, F:, etc.)
"""
import sys
import os
import json
import logging
import argparse
import subprocess
import socket
import time
from datetime import datetime
from pathlib import Path

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

from disk_usage import measure_all_folders, measure_leaf_folders, discover_volumes, measure_volume_capacity, discover_at_depth, discover_folders, measure_folder_recursive


def setup_logging(log_file: str = None, log_level: str = "INFO"):
    """Configure logging to console and file"""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level))
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    if log_file:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_local_ip() -> str:
    """Detect the primary outbound IP address (cross-platform).

    Opens a UDP socket toward an external address to identify which
    local interface would be used. No data is actually sent.
    Falls back to hostname resolution, then 'unknown' on failure.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return 'unknown'


def build_header(name: str, system_id: str, device_type: str) -> dict:
    """Build standard message header including timestamp and IP address."""
    return {
        'device_name': name,
        'device_id':   system_id,
        'device_type': device_type,
        'ip_address':  get_local_ip(),
        'timestamp':   datetime.utcnow().isoformat() + 'Z',
    }


def get_data_dir(config: dict) -> str:
    """Get or create data directory from config"""
    if sys.platform == 'win32':
        # Windows
        data_dir = config.get('data_dir', 'data')
    else:
        # Synology/Linux
        data_dir = config.get('data_dir', '/volume1/lab-monitor/data')
    
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_archive_path(config: dict, nas_name: str) -> str:
    """Get path to monthly archive JSONL for this NAS"""
    data_dir = get_data_dir(config)
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    
    year_month = datetime.utcnow().strftime("%Y-%m")
    return os.path.join(nas_dir, f"{year_month}.jsonl")


def get_queue_path(config: dict, nas_name: str) -> str:
    """Get path to queue.json for this NAS"""
    data_dir = get_data_dir(config)
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    return os.path.join(nas_dir, "queue.json")


def append_to_archive(archive_path: str, entry: dict, logger: logging.Logger):
    """Append entry to monthly JSONL archive"""
    try:
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with open(archive_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        logger.error(f"Failed to append to archive {archive_path}: {e}")


def read_queue(queue_path: str) -> list:
    """Read all entries from queue.json"""
    if not os.path.exists(queue_path):
        return []
    
    try:
        with open(queue_path, 'r') as f:
            return json.load(f)
    except Exception:
        return []


def write_queue(queue_path: str, entries: list, logger: logging.Logger):
    """Write entries to queue.json"""
    try:
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        with open(queue_path, 'w') as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write queue: {e}")


def delete_queue(queue_path: str, logger: logging.Logger):
    """Delete queue.json"""
    try:
        if os.path.exists(queue_path):
            os.remove(queue_path)
            logger.info("Queue deleted after successful sync")
    except Exception as e:
        logger.error(f"Failed to delete queue: {e}")


def collect_disk_usage(config: dict, logger: logging.Logger) -> bool:
    """Collect disk usage (daily mode)"""
    logger.info("=== DISK USAGE COLLECTION ===")

    if not config.get('active', True):
        logger.info("Collector is inactive (active=false in config) - skipping collection and sync.")
        return True

    name = config.get('name') or config.get('nas_name')
    system_id = config.get('id') or config.get('nas_id')
    device_type = config.get('device_type', 'unknown')

    if not name:
        logger.error("Config missing 'name' field. Update config.json.")
        return False

    volumes   = config.get('volumes', [])
    scan_paths = config.get('scan_paths', [])
    timeout   = config.get('timeout_seconds', 3600)
    nas_type  = 'synology' if sys.platform != 'win32' else 'windows'

    try:
        logger.info(f"Measuring disk usage for {name} (device_type={device_type})")

        # ------------------------------------------------------------------
        # 1. Volume capacity (used / free / total) — always via shutil.
        #    'volumes' exists purely for this purpose.
        #    Auto-discover if omitted.
        # ------------------------------------------------------------------
        if not volumes:
            logger.info("No volumes specified — auto-discovering...")
            volumes = discover_volumes(nas_type)
            if volumes:
                logger.info(f"Auto-discovered volumes: {volumes}")
            else:
                logger.warning("No volumes found. Add 'volumes' to config.json.")

        volume_capacity = {}   # keyed as "{vol}_total_bytes" / "{vol}_free_bytes"
        volume_used     = {}   # keyed as "{vol}" → used bytes
        for vol in volumes:
            cap = measure_volume_capacity(vol)
            if cap:
                volume_capacity[f"{vol}_total_bytes"] = cap['total_bytes']
                volume_capacity[f"{vol}_free_bytes"]  = cap['free_bytes']
                volume_used[vol] = cap['total_bytes'] - cap['free_bytes']
                logger.info(
                    f"Volume {vol}: used={volume_used[vol]:,}  "
                    f"total={cap['total_bytes']:,}  free={cap['free_bytes']:,}"
                )

        # ------------------------------------------------------------------
        # 2. Folder usage — driven by 'scan_paths'.
        #
        #    Each entry is either:
        #      "/volume1/Data1"    → measure that folder as a single total
        #      "/volume1/Data1/*"  → measure each immediate subfolder separately
        #
        #    Backward-compat: if scan_paths is absent, fall back to scanning
        #    one level deep in each volume (old behaviour).
        # ------------------------------------------------------------------
        folder_data = {}

        if not scan_paths:
            # Legacy fallback: one level deep in each volume
            logger.info("No scan_paths configured — falling back to one level deep in each volume.")
            for vol in volumes:
                subfolders = discover_folders(vol, nas_type)
                measured   = measure_leaf_folders(subfolders, timeout=timeout)
                for f in measured:
                    folder_data[f['path']] = f['usage_bytes']
        else:
            for spec in scan_paths:
                spec = spec.strip()
                if spec.endswith('/*'):
                    # Glob mode: measure each immediate subfolder
                    root = spec[:-2].rstrip('/').rstrip('\\')
                    logger.info(f"Scanning subfolders of: {root}")
                    subfolders = discover_folders(root, nas_type)
                    if not subfolders:
                        logger.warning(f"No subfolders found under {root}")
                    measured = measure_leaf_folders(subfolders, timeout=timeout)
                    for f in measured:
                        folder_data[f['path']] = f['usage_bytes']
                else:
                    # Single-folder mode: measure this path as one total
                    logger.info(f"Measuring folder: {spec}")
                    if not os.path.exists(spec):
                        logger.warning(f"scan_path does not exist: {spec}")
                        continue
                    bytes_used, files_counted = measure_folder_recursive(spec, timeout=timeout)
                    folder_data[spec] = bytes_used
                    logger.info(f"  {spec}: {bytes_used:,} bytes ({files_counted:,} files)")

        total_usage = sum(volume_used.values())

        entry = {
            'header': build_header(name, system_id, device_type),
            'data': {
                'data_type':   'folder_usage',
                '_unit':       'bytes',
                **folder_data,      # per-folder usage from scan_paths
                **volume_used,      # per-volume used bytes (shutil, authoritative)
                **volume_capacity,  # per-volume total/free bytes
                'total_usage': total_usage,
            }
        }
        # Append to local archive
        archive_path = get_archive_path(config, name)
        append_to_archive(archive_path, entry, logger)
        logger.info(f"Archived to {archive_path}")
        
        # Add to queue
        queue_path = get_queue_path(config, name)
        entries = read_queue(queue_path)
        entries.append(entry)
        write_queue(queue_path, entries, logger)
        logger.info(f"Added to queue ({len(entries)} entries)")
        
        # Try to sync queue to manager
        return sync_queue_to_manager(config, name, system_id, queue_path, logger)
        
    except Exception as e:
        logger.error(f"Disk collection failed: {e}", exc_info=True)
        return False


def collect_metrics(config: dict, logger: logging.Logger) -> bool:
    """Collect system metrics: CPU, RAM, network (5-minute mode)"""
    logger.info("=== METRICS COLLECTION ===")

    if not config.get('active', True):
        logger.info("Collector is inactive (active=false in config) — skipping collection and sync.")
        return True

    # Support both 'name' and old 'nas_name' for backward compatibility
    name = config.get('name') or config.get('nas_name')
    system_id = config.get('id') or config.get('nas_id')
    device_type = config.get('device_type', 'unknown')  # Default to 'unknown' if not specified
    
    if not name:
        logger.error("Config missing 'name' field (or old 'nas_name'). Update config.json.")
        return False
    
    try:
        cpu_percent = get_cpu_percent()
        ram_percent = get_ram_percent()
        network_stats = get_network_stats(config, name)
        uptime_seconds = get_uptime_seconds()
        uptime_formatted = format_uptime(uptime_seconds)
        
        gpu_stats = get_gpu_stats()
        if gpu_stats:
            gpu_list = [f"{g.get('name', 'Unknown')} {g.get('gpu_percent', 'N/A')}%" for g in gpu_stats]
            gpu_summary = f" | GPUs: {len(gpu_stats)} ({', '.join(gpu_list)})"
        else:
            gpu_summary = " | GPUs: none/unavailable"
        logger.info(f"CPU: {cpu_percent}% | RAM: {ram_percent}% | Uptime: {uptime_formatted} | Network: {network_stats}{gpu_summary}")

        # Create message (header + data).
        # Each numeric field is accompanied by a sibling *_unit field so any
        # consumer (dashboard, downstream scripts) can interpret values
        # correctly without hardcoding field-name→unit mappings.
        entry = {
            'header': build_header(name, system_id, device_type),
            'data': {
                'data_type':                        'system_metrics',
                'cpu_percent':                      cpu_percent,
                'cpu_percent_unit':                 '%',
                'ram_percent':                      ram_percent,
                'ram_percent_unit':                 '%',
                'uptime_seconds':                   uptime_seconds,
                'uptime_seconds_unit':              's',
                'uptime_formatted':                 uptime_formatted,
                'network_bytes_in':                 network_stats.get('bytes_in', 0),
                'network_bytes_in_unit':            'bytes',
                'network_bytes_out':                network_stats.get('bytes_out', 0),
                'network_bytes_out_unit':           'bytes',
                'network_bandwidth_in_mbps':        network_stats.get('bandwidth_in_mbps', 0.0),
                'network_bandwidth_in_mbps_unit':   'Mbps',
                'network_bandwidth_out_mbps':       network_stats.get('bandwidth_out_mbps', 0.0),
                'network_bandwidth_out_mbps_unit':  'Mbps',
                'gpus':                             gpu_stats,
                'gpu_count':                        len(gpu_stats),
            }
        }
        
        # Append to local archive
        archive_path = get_archive_path(config, name)
        append_to_archive(archive_path, entry, logger)
        logger.info(f"Archived to {archive_path}")
        
        # Add to queue
        queue_path = get_queue_path(config, name)
        entries = read_queue(queue_path)
        entries.append(entry)
        
        # Per-user metrics (one message per user for normalized history storage)
        user_stats = get_per_user_stats(config)
        for user in user_stats:
            user_entry = {
                'header': build_header(name, system_id, device_type),
                'data': {
                    'data_type':     'user_metrics',
                    'username':      user['username'],
                    'cpu_percent':   user['cpu_percent'],
                    'ram_bytes':     user['ram_bytes'],
                    'ram_formatted': user['ram_formatted'],
                },
            }
            entries.append(user_entry)
        
        write_queue(queue_path, entries, logger)
        logger.info(f"Added {len(entries)} entries to queue (including {len(user_stats)} user metrics)")
        
        # Try to sync queue to manager
        return sync_queue_to_manager(config, name, system_id, queue_path, logger)
        
    except Exception as e:
        logger.error(f"Metrics collection failed: {e}", exc_info=True)
        return False


def sync_queue_to_manager(config: dict, name: str, system_id: str, queue_path: str, logger: logging.Logger) -> bool:
    """Post queue to Manager and delete on success"""
    if not os.path.exists(queue_path):
        logger.info("Queue is empty, nothing to sync")
        return True
    
    try:
        entries = read_queue(queue_path)
        if not entries:
            logger.info("Queue is empty, nothing to sync")
            return True
        
        # Create queue payload
        queue_id = f"{name}-{datetime.utcnow().strftime('%Y-%m-%d-%H-%M-%S')}"

        payload = {
            'queue_id': queue_id,
            'name':     name,
            'id':       system_id,
            'messages': entries,
        }
        
        # Post to Manager
        manager_url = config.get('manager_url')
        manager_token = config.get('manager_token')
        timeout = config.get('request_timeout_seconds', 30)
        
        logger.info(f"Posting queue to Manager ({len(entries)} entries)")
        
        import requests
        headers = {
            'Authorization': f'Bearer {manager_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.post(
            f"{manager_url}/api/data/queue",
            json=payload,
            headers=headers,
            timeout=timeout
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get('status') == 'ok' and result.get('queue_id') == queue_id:
                logger.info(f"Queue synced successfully ({result.get('stored')} entries stored)")
                delete_queue(queue_path, logger)
                return True
            elif result.get('status') == 'ok' and result.get('queue_id') != queue_id:
                logger.warning(
                    f"Manager ACK queue_id mismatch: sent {queue_id!r}, "
                    f"got {result.get('queue_id')!r} — not deleting queue"
                )
        
        logger.warning(f"Manager returned {response.status_code}: {response.text}")
        logger.info("Queue persisted for retry on next run")
        return False
        
    except requests.exceptions.RequestException as e:
        logger.warning(f"Network error posting queue: {e}")
        logger.info("Queue persisted for retry on next run")
        return False
    except Exception as e:
        logger.error(f"Queue sync failed: {e}", exc_info=True)
        logger.info("Queue persisted for retry on next run")
        return False


def get_cpu_percent() -> float:
    """Get CPU usage percentage"""
    try:
        import psutil
        return psutil.cpu_percent(interval=1)
    except ImportError:
        return fallback_cpu_percent()


def fallback_cpu_percent() -> float:
    """Fallback CPU measurement without psutil"""
    try:
        if sys.platform == 'win32':
            return 0.0
        else:
            # Linux: estimate from loadavg
            import os
            loadavg = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            return min(100.0, (loadavg / cpu_count) * 100)
    except:
        return 0.0


def get_ram_percent() -> float:
    """Get RAM usage percentage"""
    try:
        import psutil
        return psutil.virtual_memory().percent
    except ImportError:
        return fallback_ram_percent()


def get_per_user_stats(config: dict) -> list:
    """
    Aggregate CPU% and RAM (bytes) by Windows user account across all processes.
    Returns a list sorted by cpu_percent descending.
    Filters out blank/None usernames and accounts matching exclude_users patterns.
    Each entry: {username, cpu_percent, ram_bytes, ram_formatted}
    """
    try:
        import psutil
        from collections import defaultdict
        import fnmatch

        user_cpu = defaultdict(float)
        user_ram = defaultdict(int)
        cpu_count = psutil.cpu_count(logical=True) or 1
        # Always exclude well-known Windows service accounts.
        # Additional patterns can be added via exclude_users in config.json.
        default_excludes = [
            'UMFD-*',
            'NT AUTHORITY\\*',
            'NT SERVICE\\*',
            'Window Manager\\*',
            'Font Driver Host\\*',
        ]
        exclude_patterns = default_excludes + config.get('exclude_users', [])

        for proc in psutil.process_iter(['username', 'cpu_percent', 'memory_info']):
            try:
                username = proc.info.get('username') or ''
                if not username:
                    continue
                # Skip if matches any exclude pattern (e.g., 'UMFD*', 'SYSTEM')
                if any(fnmatch.fnmatch(username, pattern) for pattern in exclude_patterns):
                    continue
                user_cpu[username] += proc.info.get('cpu_percent') or 0.0
                mem = proc.info.get('memory_info')
                user_ram[username] += mem.rss if mem else 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        result = []
        for username, cpu_total in user_cpu.items():
            ram_b = user_ram[username]
            result.append({
                'username':      username,
                'cpu_percent':   round(cpu_total / cpu_count, 2),
                'ram_bytes':     ram_b,
                'ram_formatted': _format_bytes(ram_b),
            })
        return sorted(result, key=lambda x: x['cpu_percent'], reverse=True)

    except Exception:
        return []


def _format_bytes(b: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def get_gpu_stats() -> list:
    """
    Collect per-GPU utilisation stats via nvidia-ml-py (NVIDIA only).
    Returns an empty list if nvidia-ml-py is not installed, no NVIDIA driver is
    present, or no GPUs are found — callers should treat an empty list as
    "GPU data unavailable" rather than an error.

    Each entry:
        index, name, gpu_percent, vram_used_bytes, vram_total_bytes,
        vram_used_formatted, vram_total_formatted,
        temperature_c (None if unavailable), power_watts (None if unavailable)
    """
    try:
        import pynvml
    except ImportError:
        return []   # pynvml not installed

    try:
        pynvml.nvmlInit()
    except Exception:
        return []   # no NVIDIA driver / GPU

    gpus = []
    try:
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)

                raw_name = pynvml.nvmlDeviceGetName(handle)
                name = raw_name.decode('utf-8') if isinstance(raw_name, bytes) else raw_name

                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem  = pynvml.nvmlDeviceGetMemoryInfo(handle)

                try:
                    temp = pynvml.nvmlDeviceGetTemperature(
                        handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = None

                try:
                    power = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
                except Exception:
                    power = None

                gpus.append({
                    'index':                i,
                    'name':                 name,
                    'gpu_percent':          util.gpu,
                    'vram_used_bytes':      mem.used,
                    'vram_total_bytes':     mem.total,
                    'vram_used_formatted':  _format_bytes(mem.used),
                    'vram_total_formatted': _format_bytes(mem.total),
                    'temperature_c':        temp,
                    'power_watts':          power,
                })
            except Exception:
                pass   # skip individual GPU on error
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    return gpus


def fallback_ram_percent() -> float:
    """Fallback RAM measurement without psutil"""
    try:
        if sys.platform == 'win32':
            # Windows: using wmic
            import subprocess
            output = subprocess.check_output('wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value').decode()
            total = 0
            free = 0
            for line in output.split('\n'):
                if 'TotalVisibleMemorySize' in line:
                    total = int(line.split('=')[1].strip())
                elif 'FreePhysicalMemory' in line:
                    free = int(line.split('=')[1].strip())
            if total > 0:
                return ((total - free) / total) * 100
            return 0.0
        else:
            # Linux: from /proc/meminfo
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            mem_total = int(lines[0].split()[1])
            mem_avail = int(lines[2].split()[1])
            return ((mem_total - mem_avail) / mem_total) * 100
    except:
        return 0.0


def get_uptime_seconds() -> int:
    """Get system uptime in seconds"""
    try:
        import psutil
        boot_time = psutil.boot_time()
        current_time = time.time()
        uptime_seconds = int(current_time - boot_time)
        return max(0, uptime_seconds)  # Ensure non-negative
    except Exception:
        return 0


def format_uptime(seconds: int) -> str:
    """Format uptime in seconds to human-readable format"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"


def get_network_snapshot_path(config: dict, nas_name: str) -> str:
    """Get path to network snapshot file"""
    data_dir = get_data_dir(config)
    nas_dir = os.path.join(data_dir, nas_name)
    os.makedirs(nas_dir, exist_ok=True)
    return os.path.join(nas_dir, "network_snapshot.json")


def read_network_snapshot(snapshot_path: str) -> dict:
    """Read previous network snapshot"""
    if not os.path.exists(snapshot_path):
        return None
    
    try:
        with open(snapshot_path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def write_network_snapshot(snapshot_path: str, data: dict) -> bool:
    """Write current network snapshot for next run"""
    try:
        with open(snapshot_path, 'w') as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


def get_network_stats(config: dict, nas_name: str) -> dict:
    """Get network traffic (cumulative + bandwidth)"""
    try:
        import psutil
        net = psutil.net_io_counters()
        
        current_bytes_in = net.bytes_recv
        current_bytes_out = net.bytes_sent
        current_timestamp = datetime.utcnow().isoformat() + 'Z'
        
        # Read previous snapshot
        snapshot_path = get_network_snapshot_path(config, nas_name)
        previous = read_network_snapshot(snapshot_path)
        
        # Calculate bandwidth if we have a previous snapshot
        bandwidth_in_mbps = 0.0
        bandwidth_out_mbps = 0.0
        
        if previous:
            prev_bytes_in = previous.get('bytes_in', 0)
            prev_bytes_out = previous.get('bytes_out', 0)
            prev_timestamp = previous.get('timestamp', '')
            
            # Calculate time delta in seconds
            try:
                prev_dt = datetime.fromisoformat(prev_timestamp.replace('Z', '+00:00'))
                curr_dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                time_delta = (curr_dt - prev_dt).total_seconds()
                
                if time_delta > 0:
                    # Calculate bytes/sec, then convert to Mbps
                    bytes_in_delta = max(0, current_bytes_in - prev_bytes_in)
                    bytes_out_delta = max(0, current_bytes_out - prev_bytes_out)
                    
                    bandwidth_in_mbps = (bytes_in_delta / time_delta) / (1024 * 1024)
                    bandwidth_out_mbps = (bytes_out_delta / time_delta) / (1024 * 1024)
            except Exception:
                pass  # Silently fail on timestamp parsing, just report 0 bandwidth
        
        # Save current snapshot for next run
        write_network_snapshot(snapshot_path, {
            'timestamp': current_timestamp,
            'bytes_in': current_bytes_in,
            'bytes_out': current_bytes_out
        })
        
        return {
            'bytes_in': current_bytes_in,
            'bytes_out': current_bytes_out,
            'bandwidth_in_mbps': bandwidth_in_mbps,
            'bandwidth_out_mbps': bandwidth_out_mbps
        }
    except ImportError:
        return {
            'bytes_in': 0,
            'bytes_out': 0,
            'bandwidth_in_mbps': 0.0,
            'bandwidth_out_mbps': 0.0
        }


def main():
    parser = argparse.ArgumentParser(description='Lab Monitor Collector')
    parser.add_argument('--config', required=True, help='Config file')
    parser.add_argument('--mode', choices=['disk', 'metrics'], default='disk', help='Collection mode')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.config):
        print(f"ERROR: Config not found: {args.config}")
        sys.exit(1)
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    logger = setup_logging(config.get('log_file'), config.get('log_level', 'INFO'))
    logger.info(f"Starting collector ({args.mode} mode)")
    
    if args.mode == 'disk':
        success = collect_disk_usage(config, logger)
    else:
        success = collect_metrics(config, logger)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
