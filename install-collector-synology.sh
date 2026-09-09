#!/bin/bash
#
# Lab Monitor - Synology NAS Collector Installation Script
#
# Usage:
#   ssh your-admin-user@your-nas.local
#     (e.g., ssh jeff@nas.local, ssh john@nas.local, etc.)
#   curl -O https://raw.githubusercontent.com/MoffittLab/lab-monitor/main/install-collector-synology.sh
#   chmod +x install-collector-synology.sh
#   ./install-collector-synology.sh
#
# Prerequisites:
#   - SSH as ANY admin user (user must have read/write to /volume1)
#   - Git installed (Control Panel → Package Center → Git Server)
#   - Python 3 installed (Control Panel → Package Center → Python)
#
# This script:
# 1. Creates directory structure
# 2. Clones or updates lab-monitor repository
# 3. Sets up Python virtual environment
# 4. Installs dependencies
# 5. Creates config.json from template
# 6. Sets up Task Scheduler jobs (via SSH command)
#

set -e  # Exit on error

echo "================================================"
echo "Lab Monitor - Synology NAS Collector Install"
echo "================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
VOLUME="/volume1"
LAB_MONITOR_DIR="$VOLUME/lab-monitor"
SCRIPTS_DIR="$LAB_MONITOR_DIR/scripts"
VENV_DIR="$LAB_MONITOR_DIR/lab-monitor-env"
DATA_DIR="$LAB_MONITOR_DIR/data"
LOGS_DIR="$LAB_MONITOR_DIR/logs"
REPO_URL="https://github.com/MoffittLab/lab-monitor.git"

# Check if user has write permission to /volume1/lab-monitor (admin users do)
CURRENT_USER=$(whoami)
if [ "$CURRENT_USER" = "root" ]; then
    echo -e "${RED}ERROR: This script must NOT be run as root${NC}"
    echo "SSH as your admin user (e.g., jeff, john, admin) and try again"
    exit 1
fi

# Create lab-monitor base directory and verify write permission
mkdir -p "$LAB_MONITOR_DIR" 2>/dev/null || {
    echo -e "${RED}ERROR: Could not create or access $LAB_MONITOR_DIR${NC}"
    echo "User '$CURRENT_USER' does not have write permission"
    echo "Ensure your user has 'Read & Write' permissions via Control Panel → Shared Folder → lab-monitor"
    exit 1
}

# Verify write permission to lab-monitor directory
if ! touch "$LAB_MONITOR_DIR/.test-write" 2>/dev/null; then
    echo -e "${RED}ERROR: User '$CURRENT_USER' does not have write permission to $LAB_MONITOR_DIR${NC}"
    echo "This script must be run as an admin user with access to $LAB_MONITOR_DIR"
    echo "Ensure your user has 'Read & Write' permissions via Control Panel → Shared Folder → lab-monitor"
    exit 1
fi
rm -f "$LAB_MONITOR_DIR/.test-write"

echo -e "${GREEN}Running as user: $CURRENT_USER${NC}"

echo -e "${YELLOW}Step 1: Creating directory structure${NC}"
mkdir -p "$DATA_DIR"
mkdir -p "$LOGS_DIR"
mkdir -p "$SCRIPTS_DIR"
echo "[OK] Directories created"
echo ""

echo -e "${YELLOW}Step 2: Checking Python installation${NC}"
if ! python3 --version > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Python 3 not found${NC}"
    echo "Install Python via Control Panel → Package Center"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1)
echo "[OK] $PYTHON_VERSION found"
echo ""

echo -e "${YELLOW}Step 2b: Checking git installation${NC}"
if ! command -v git > /dev/null 2>&1; then
    echo -e "${RED}ERROR: git not found${NC}"
    echo "Install Git Server via Control Panel → Package Center → Git Server"
    exit 1
fi
echo "[OK] git found"
echo ""

echo -e "${YELLOW}Step 3: Cloning/updating lab-monitor repository${NC}"
if [ -d "$SCRIPTS_DIR/lab-monitor" ]; then
    echo "Repository already exists, pulling latest..."
    cd "$SCRIPTS_DIR/lab-monitor"
    git pull origin main 2>/dev/null || echo "Warning: Could not pull (offline?)"
else
    cd "$SCRIPTS_DIR"
    git clone "$REPO_URL" || {
        echo -e "${RED}ERROR: Could not clone repository${NC}"
        echo "Ensure git is installed: Control Panel → Package Center → Git Server"
        exit 1
    }
fi
echo "[OK] Repository ready at $SCRIPTS_DIR/lab-monitor"
echo ""

echo -e "${YELLOW}Step 4: Creating Python environment${NC}"
# DSM's Package Center Python frequently ships a broken/absent venv module.
# Try venv first; if it fails, fall back to a --user install against the
# system python3. USE_VENV tracks which path we took so later steps and the
# printed Task Scheduler commands use the correct interpreter.
USE_VENV=0
# A prior failed run can leave a broken venv whose bin/python3 exists but
# has no working pip. Require pip to import before trusting an existing venv.
if [ -x "$VENV_DIR/bin/python3" ] && "$VENV_DIR/bin/python3" -m pip --version > /dev/null 2>&1; then
    echo "[OK] Virtual environment already exists"
    USE_VENV=1
elif rm -rf "$VENV_DIR" 2>/dev/null; python3 -m venv "$VENV_DIR" > /tmp/venv-create.log 2>&1 && "$VENV_DIR/bin/python3" -m pip --version > /dev/null 2>&1; then
    echo "[OK] Virtual environment created"
    USE_VENV=1
else
    echo -e "${YELLOW}Warning: could not create a virtualenv (common on DSM).${NC}"
    echo "Falling back to a per-user install against system python3."
    echo "(venv error log: /tmp/venv-create.log)"
    rm -rf "$VENV_DIR" 2>/dev/null || true
    USE_VENV=0
fi

# Resolve the python interpreter used for all subsequent steps and for the
# scheduled tasks the user creates at the end.
if [ "$USE_VENV" = "1" ]; then
    COLLECTOR_PYTHON="$VENV_DIR/bin/python3"
else
    COLLECTOR_PYTHON="$(command -v python3)"
fi
echo ""

echo -e "${YELLOW}Step 5: Installing dependencies${NC}"
# Use the lightweight collector-only requirements (requests + psutil).
# The repo-root requirements.txt also pulls Flask/paramiko, which the
# collector does not need and which can fail to build on a NAS.
REQ_FILE="$SCRIPTS_DIR/lab-monitor/collector/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    # Fallback for older checkouts without the component requirements file.
    REQ_FILE="$SCRIPTS_DIR/lab-monitor/requirements.txt"
fi

cd "$SCRIPTS_DIR/lab-monitor"
if [ "$USE_VENV" = "1" ]; then
    # Install into the venv without relying on 'source activate' (which can
    # exit the script under 'set -e' if activation misbehaves).
    "$COLLECTOR_PYTHON" -m pip install --upgrade pip > /dev/null 2>&1 || true
    "$COLLECTOR_PYTHON" -m pip install -r "$REQ_FILE" > /tmp/pip-install.log 2>&1 || {
        echo -e "${RED}ERROR: Could not install dependencies into venv${NC}"
        echo "See /tmp/pip-install.log"
        exit 1
    }
else
    # Per-user install against system python3.
    "$COLLECTOR_PYTHON" -m pip install --user --upgrade pip > /dev/null 2>&1 || true
    "$COLLECTOR_PYTHON" -m pip install --user -r "$REQ_FILE" > /tmp/pip-install.log 2>&1 || {
        echo -e "${RED}ERROR: Could not install dependencies (--user)${NC}"
        echo "See /tmp/pip-install.log"
        echo "If pip is missing, install it: python3 -m ensurepip --user"
        exit 1
    }
fi
echo "[OK] Dependencies installed (requests, psutil) using $COLLECTOR_PYTHON"
echo ""

echo -e "${YELLOW}Step 6: Gathering configuration${NC}"
mkdir -p "$SCRIPTS_DIR/lab-monitor/collector/local"
CONFIG_FILE="$SCRIPTS_DIR/lab-monitor/collector/local/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    # Auto-detect NAS hostname for name and id
    NAS_HOSTNAME=$(hostname | tr '[:upper:]' '[:lower:]')
    NAS_ID="synology-${NAS_HOSTNAME}"
    
    echo "Auto-detected values:"
    echo "  - NAS name: $NAS_HOSTNAME"
    echo "  - NAS id: $NAS_ID"
    echo ""
    
    # Prompt for Manager URL
    read -p "Enter Manager URL (e.g., http://atlantis.med.harvard.edu:5000): " MANAGER_URL
    if [ -z "$MANAGER_URL" ]; then
        echo -e "${RED}ERROR: Manager URL cannot be empty${NC}"
        exit 1
    fi
    if echo "$MANAGER_URL" | grep -q '^https://'; then
        echo -e "${YELLOW}Warning: URL starts with https:// but the Manager runs plain HTTP. Did you mean http://?${NC}"
        read -p "Continue with https:// anyway? [y/N]: " CONFIRM_HTTPS
        if [ "$CONFIRM_HTTPS" != "y" ] && [ "$CONFIRM_HTTPS" != "Y" ]; then
            echo "Please re-run and enter the correct URL."
            exit 1
        fi
    fi

    # Prompt for Manager Token
    read -p "Enter Manager Token (from Manager config): " MANAGER_TOKEN
    if [ -z "$MANAGER_TOKEN" ]; then
        echo -e "${RED}ERROR: Manager Token cannot be empty${NC}"
        exit 1
    fi

    # Prompt for Device Type
    read -p "Enter Device Type [NAS/NAS-Instrument/Server/NAS-Backup]: " DEVICE_TYPE
    if [ -z "$DEVICE_TYPE" ]; then
        echo -e "${RED}ERROR: Device Type cannot be empty${NC}"
        exit 1
    fi
    if [ "$DEVICE_TYPE" != "NAS" ] && [ "$DEVICE_TYPE" != "NAS-Instrument" ] && [ "$DEVICE_TYPE" != "Server" ] && [ "$DEVICE_TYPE" != "NAS-Backup" ]; then
        echo -e "${YELLOW}Warning: '$DEVICE_TYPE' is not a recognised device type (expected NAS, NAS-Instrument, Server, or NAS-Backup). Continuing anyway.${NC}"
    fi

    echo ""
    echo "Specify scan paths to monitor:"
    echo "  Examples:"
    echo "    /volume1/*             (monitor each subfolder of /volume1 separately)"
    echo "    /volume1/Data          (monitor /volume1/Data as a single total)"
    echo "    /volume2/*             (monitor each subfolder of /volume2 separately)"
    read -p "Enter scan paths (comma-separated, or press Enter to use volume roots): " SCAN_PATHS_INPUT
    
    if [ -n "$SCAN_PATHS_INPUT" ]; then
        # User provided custom scan paths
        SCAN_PATHS_JSON=""
        IFS=',' read -ra PATHS <<< "$SCAN_PATHS_INPUT"
        for path in "${PATHS[@]}"; do
            path=$(echo "$path" | xargs)  # trim whitespace
            [ -n "$SCAN_PATHS_JSON" ] && SCAN_PATHS_JSON="$SCAN_PATHS_JSON, "
            SCAN_PATHS_JSON="$SCAN_PATHS_JSON\"$path\""
        done
        echo "[OK] Scan paths: $SCAN_PATHS_INPUT"
    else
        # Default: one level deep in each data volume (match /volumeN only)
        SCAN_PATHS_JSON=""
        for vol in /volume[0-9]*; do
            [ -d "$vol" ] || continue
            case "$vol" in
                *\**) continue ;;
            esac
            [ -n "$SCAN_PATHS_JSON" ] && SCAN_PATHS_JSON="$SCAN_PATHS_JSON, "
            SCAN_PATHS_JSON="$SCAN_PATHS_JSON\"$vol/*\""
        done
        if [ -z "$SCAN_PATHS_JSON" ]; then
            SCAN_PATHS_JSON="\"/volume1/*\""
        fi
        echo "[OK] Defaulting to one level deep in each volume: $SCAN_PATHS_JSON"
    fi
    
    # Auto-detect data volumes present on this NAS. Match /volume<number>
    # only, skipping /volumeUSB*, /volumeSATA*, and the literal glob when
    # nothing matches (nullglob is not reliable in DSM's ash/bash).
    VOLUMES_JSON=""
    for vol in /volume[0-9]*; do
        # Guard against the no-match case where the glob stays literal.
        [ -d "$vol" ] || continue
        case "$vol" in
            *\**) continue ;;  # literal '/volume[0-9]*' — no volumes matched
        esac
        [ -n "$VOLUMES_JSON" ] && VOLUMES_JSON="$VOLUMES_JSON, "
        VOLUMES_JSON="$VOLUMES_JSON\"$vol\""
    done
    if [ -z "$VOLUMES_JSON" ]; then
        echo -e "${YELLOW}Warning: no /volumeN data volumes auto-detected.${NC}"
        echo "Defaulting to /volume1; edit the config afterward if that is wrong."
        VOLUMES_JSON="\"/volume1\""
    fi
    echo "Auto-detected volumes: $VOLUMES_JSON"
    echo ""

    cat > "$CONFIG_FILE" << EOF
{
  "name": "${NAS_HOSTNAME}",
  "id": "${NAS_ID}",
  "device_type": "${DEVICE_TYPE}",
  "manager_url": "${MANAGER_URL}",
  "manager_token": "${MANAGER_TOKEN}",
  "volumes": [${VOLUMES_JSON}],
  "scan_paths": [${SCAN_PATHS_JSON}],
  "data_dir": "/volume1/lab-monitor/data",
  "log_file": "/volume1/lab-monitor/logs/collector.log",
  "log_level": "INFO",
  "timeout_seconds": 3600,
  "request_timeout_seconds": 30
}
EOF
    echo "[OK] Config file created at $CONFIG_FILE"
    echo ""
    echo "Configuration saved with:"
    echo "  - Device Type:   $DEVICE_TYPE"
    echo "  - Volumes:       $VOLUMES_JSON"
    echo "  - Scan Paths:    $SCAN_PATHS_JSON"
    echo "  - Manager URL:   $MANAGER_URL"
    echo "  - Manager Token: (set)"
    echo ""
    echo "To modify later:"
    echo "  nano $CONFIG_FILE"
    echo ""
else
    echo "[OK] Config file already exists at $CONFIG_FILE"
    echo "   (Edit manually if needed: nano $CONFIG_FILE)"
fi
echo ""

echo -e "${YELLOW}Step 7: Testing collector (metrics mode)${NC}"
cd "$SCRIPTS_DIR/lab-monitor/collector"
echo "Testing metrics collection..."
if "$COLLECTOR_PYTHON" collector.py --config local/config.json --mode metrics > /tmp/collector-test.log 2>&1; then
    echo "[OK] Metrics collection test passed"
else
    echo -e "${RED}⚠️  Metrics collection test failed${NC}"
    echo "Log: /tmp/collector-test.log"
    echo "This may be normal if Manager isn't running yet"
fi
echo ""

echo "=========================================="
echo -e "${GREEN}[OK] Synology Collector Installation Complete${NC}"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Edit configuration:"
echo "   nano $CONFIG_FILE"
echo ""
echo "2. Schedule jobs via Synology Control Panel:"
echo ""
echo "   Control Panel → Task Scheduler → Create → Scheduled Task → User-defined Script"
echo ""
echo "   Task 1: Lab Monitor - Disk Collection"
echo "   - Task name: 'Lab Monitor - Disk Collection'"
echo "   - User: your admin user (e.g., 'jeff')"
echo "   - Schedule: Daily at 02:00 (2 AM)"
echo "   - User-defined script:"
echo "     cd $SCRIPTS_DIR/lab-monitor/collector && $COLLECTOR_PYTHON collector.py --config local/config.json --mode disk"
echo ""
echo "   Task 2: Lab Monitor - Metrics Collection"
echo "   - Task name: 'Lab Monitor - Metrics Collection'"
echo "   - User: your admin user (e.g., 'jeff')"
echo "   - Schedule: Daily at 00:00, repeat every 5 minutes"
echo "   - User-defined script:"
echo "     cd $SCRIPTS_DIR/lab-monitor/collector && $COLLECTOR_PYTHON collector.py --config local/config.json --mode metrics"
echo ""
echo "3. Verify jobs run:"
echo "   tail -f $LOGS_DIR/collector.log"
echo ""
