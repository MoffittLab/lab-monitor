#!/usr/bin/env python3
"""
Lab Monitor Manager/Dashboard Restart Tool

Reads collectors.csv to find the atlantis manager server, connects via SSH
(using paramiko), kills existing manager/dashboard processes, and restarts them.

Requirements:
    pip install paramiko

Usage:
    python restart_manager_dashboard.py --csv collectors.csv [--host atlantis.med.harvard.edu]
    python restart_manager_dashboard.py --csv collectors.csv --list-processes
    python restart_manager_dashboard.py --csv collectors.csv --kill-only
    python restart_manager_dashboard.py --csv collectors.csv --start-only
"""
import csv
import argparse
import sys
import logging
import time
from pathlib import Path
from typing import Optional, Dict

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def load_collectors_csv(csv_path: str) -> Dict[str, Dict]:
    """Load collectors.csv and index by hostname."""
    collectors = {}
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                host = row.get('ip', '').strip()
                if host:
                    collectors[host] = row
        logger.info(f"Loaded {len(collectors)} collectors from {csv_path}")
        return collectors
    except Exception as e:
        logger.error(f"Failed to load {csv_path}: {e}")
        sys.exit(1)


def find_manager_host(collectors: Dict[str, Dict], target_host: str) -> Optional[Dict]:
    """Find manager/dashboard host in collectors."""
    if target_host in collectors:
        return collectors[target_host]
    
    # Try to find atlantis in the list
    for host, config in collectors.items():
        if 'atlantis' in host.lower():
            logger.info(f"Found manager host: {host}")
            return config
    
    logger.error(f"Manager host {target_host} not found in collectors.csv")
    return None


def ssh_connect(hostname: str, username: str, password: Optional[str] = None, key_filename: Optional[str] = None) -> paramiko.SSHClient:
    """Establish SSH connection."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        if key_filename:
            logger.info(f"Connecting to {hostname} with SSH key...")
            client.connect(hostname, username=username, key_filename=key_filename, timeout=10)
        elif password:
            logger.info(f"Connecting to {hostname} with password...")
            client.connect(hostname, username=username, password=password, timeout=10)
        else:
            logger.info(f"Connecting to {hostname} with SSH key (default)...")
            client.connect(hostname, username=username, timeout=10)
        logger.info(f"✓ Connected to {hostname}")
        return client
    except Exception as e:
        logger.error(f"SSH connection failed: {e}")
        sys.exit(1)


def run_command(client: paramiko.SSHClient, command: str, description: str = None) -> tuple:
    """Run a command via SSH and return (stdout, stderr)."""
    if description:
        logger.info(f"Running: {description}")
    else:
        logger.info(f"Running: {command}")
    
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        exit_code = stdout.channel.recv_exit_status()
        
        if exit_code != 0 and err:
            logger.warning(f"Command returned {exit_code}: {err.strip()}")
        return out, err
    except Exception as e:
        logger.error(f"Command execution failed: {e}")
        raise


def list_processes(client: paramiko.SSHClient) -> None:
    """List manager and dashboard processes."""
    logger.info("=== Current Processes ===")
    
    # Windows
    out, _ = run_command(client, 'tasklist | findstr python', "List Python processes")
    if out:
        logger.info("Python processes:\n" + out)
    
    # Check for manager/dashboard specifically
    for proc_name in ['manager', 'dashboard', 'app.py']:
        out, _ = run_command(client, f'tasklist | findstr /i {proc_name}', f"Search for {proc_name}")
        if out.strip():
            logger.info(f"{proc_name} found: {out.strip()}")


def kill_processes(client: paramiko.SSHClient) -> None:
    """Kill manager and dashboard processes on Windows."""
    logger.info("=== Killing Existing Processes ===")
    
    # Kill by process name (manager.py, app.py)
    for proc_pattern in ['manager.py', 'app.py']:
        logger.info(f"Killing {proc_pattern}...")
        run_command(
            client, 
            f'taskkill /F /IM {proc_pattern} 2>nul || echo "Process not running"',
            f"Kill {proc_pattern}"
        )
    
    logger.info("✓ Processes killed (or were not running)")


def start_processes(client: paramiko.SSHClient, work_dir: str) -> None:
    """Start manager and dashboard via Task Scheduler on Windows."""
    logger.info("=== Restarting Services ===")
    
    # Restart via Task Scheduler
    tasks = [
        'Lab Monitor Manager',
        'Lab Monitor Dashboard'
    ]
    
    for task_name in tasks:
        logger.info(f"Starting task: {task_name}...")
        out, err = run_command(
            client,
            f'schtasks /run /tn "{task_name}" 2>nul || echo "Task not found"',
            f"Start {task_name}"
        )
        logger.info(out.strip() if out.strip() else f"Task scheduled: {task_name}")
    
    logger.info("✓ Services restarted")


def verify_services(client: paramiko.SSHClient) -> None:
    """Verify services are running."""
    logger.info("=== Verifying Services ===")
    
    out, _ = run_command(client, 'tasklist | findstr python', "Check for running Python processes")
    if 'python' in out.lower():
        logger.info("✓ Python processes are running")
    else:
        logger.warning("⚠ No Python processes found (may take a moment to start)")


def main():
    parser = argparse.ArgumentParser(
        description='Restart Lab Monitor Manager/Dashboard on atlantis server'
    )
    parser.add_argument('--csv', required=True, help='Path to collectors.csv')
    parser.add_argument('--host', default='atlantis.med.harvard.edu', help='Manager hostname')
    parser.add_argument('--key', help='SSH private key file (optional)')
    parser.add_argument('--list-processes', action='store_true', help='List processes and exit')
    parser.add_argument('--kill-only', action='store_true', help='Kill processes only, no restart')
    parser.add_argument('--start-only', action='store_true', help='Start processes only, no kill')
    
    args = parser.parse_args()
    
    # Load collectors
    collectors = load_collectors_csv(args.csv)
    config = find_manager_host(collectors, args.host)
    if not config:
        sys.exit(1)
    
    # Connect
    hostname = config['ip']
    username = config.get('username', 'admin')
    password = config.get('password', '') or None
    work_dir = config.get('git_path', 'E:\\Users\\lab-monitor\\scripts\\lab-monitor')
    
    client = ssh_connect(hostname, username, password, args.key)
    
    try:
        if args.list_processes:
            list_processes(client)
        elif args.kill_only:
            kill_processes(client)
        elif args.start_only:
            start_processes(client, work_dir)
        else:
            # Full restart
            list_processes(client)
            kill_processes(client)
            time.sleep(1)
            start_processes(client, work_dir)
            time.sleep(3)
            verify_services(client)
            logger.info("\n✓ Manager/Dashboard restart complete!")
    finally:
        client.close()


if __name__ == '__main__':
    main()
