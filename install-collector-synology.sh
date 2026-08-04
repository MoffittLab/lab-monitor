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

# Check if user has write permission to /volume1 (admin users do)
CURRENT_USER=$(whoami)
if [ "$CURRENT_USER" = "root" ]; then
    echo -e "${RED}ERROR: This script must NOT be run as root${NC}"
    echo "SSH as your admin user (e.g., jeff, john, admin) and try again"
    exit 1
fi

# Verify write permission to /volume1
if ! touch "$VOLUME/.test-write" 2>/dev/null; then
    echo -e "${RED}ERROR: User '$CURRENT_USER' does not have write permission to $VOLUME${NC}"
    echo "This script must be run as an admin user with access to $VOLUME"
    echo "Ensure your user has 'Read & Write' permissions via Control Panel → Shared Folder → lab-monitor"
    exit 1
fi
rm -f "$VOLUME/.test-write"

echo -e "${GREEN}Running as user: $CURRENT_USER${NC}"

echo -e "${YELLOW}Step 1: Creating directory structure${NC}"
mkdir -p "$DATA_DIR"
mkdir -p "$LOGS_DIR"
mkdir -p "$SCRIPTS_DIR"
echo "✓ Directories created"
echo ""

echo -e "${YELLOW}Step 2: Checking Python installation${NC}"
if ! python3 --version > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Python 3 not found${NC}"
    echo "Install Python via Control Panel → Package Center"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1)
echo "✓ $PYTHON_VERSION found"
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
echo "✓ Repository ready at $SCRIPTS_DIR/lab-monitor"
echo ""

echo -e "${YELLOW}Step 4: Creating Python virtual environment${NC}"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR" || {
        echo -e "${RED}ERROR: Could not create virtual environment${NC}"
        exit 1
    }
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi
echo ""

echo -e "${YELLOW}Step 5: Installing dependencies${NC}"
# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Upgrade pip
pip install --upgrade pip > /dev/null 2>&1

# Install requirements
cd "$SCRIPTS_DIR/lab-monitor/collector"
pip install -r requirements.txt > /dev/null 2>&1 || {
    echo -e "${RED}ERROR: Could not install dependencies${NC}"
    deactivate
    exit 1
}
echo "✓ Dependencies installed (requests, psutil)"

deactivate
echo ""

echo -e "${YELLOW}Step 6: Creating configuration${NC}"
mkdir -p "$SCRIPTS_DIR/lab-monitor/collector/local"
CONFIG_FILE="$SCRIPTS_DIR/lab-monitor/collector/local/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    # Auto-detect NAS hostname for name and id
    NAS_HOSTNAME=$(hostname | tr '[:upper:]' '[:lower:]')
    NAS_ID="synology-${NAS_HOSTNAME}"
    
    cat > "$CONFIG_FILE" << EOF
{
  "name": "${NAS_HOSTNAME}",
  "id": "${NAS_ID}",
  "manager_url": "http://atlantis.med.harvard.edu:5000",
  "manager_token": "CHANGE-ME-TO-MANAGER-TOKEN",
  "volumes": ["/volume1"],
  "data_dir": "/volume1/lab-monitor/data",
  "log_file": "/volume1/lab-monitor/logs/collector.log",
  "log_level": "INFO",
  "timeout_seconds": 3600
}
EOF
    echo "✓ Config file created at $CONFIG_FILE"
    echo ""
    echo -e "${YELLOW}⚠️  IMPORTANT: Edit the configuration:${NC}"
    echo "   nano $CONFIG_FILE"
    echo ""
    echo "   Auto-detected values:"
    echo "   - \"name\": $NAS_HOSTNAME (auto-detected from hostname)"
    echo "   - \"id\": $NAS_ID (auto-generated)"
    echo ""
    echo "   REQUIRED changes:"
    echo "   - \"manager_token\": Copy from your Manager config (same for all collectors)"
    echo ""
    echo "   OPTIONAL changes:"
    echo "   - \"volumes\": Add additional volumes if needed (e.g., ['/volume1', '/volume2'])"
    echo ""
else
    echo "✓ Config file already exists at $CONFIG_FILE"
    echo "   (Edit manually if needed: nano $CONFIG_FILE)"
fi
echo ""

echo -e "${YELLOW}Step 7: Testing collector (disk mode)${NC}"
cd "$SCRIPTS_DIR/lab-monitor/collector"
echo "Testing disk collection (this may take a minute)..."
source "$VENV_DIR/bin/activate"
if $VENV_DIR/bin/python3 collector.py --config local/config.json --mode disk > /tmp/collector-test.log 2>&1; then
    echo "✓ Disk collection test passed"
else
    echo -e "${RED}⚠️  Disk collection test failed${NC}"
    echo "Log: /tmp/collector-test.log"
    echo "This may be normal if Manager isn't running yet"
fi
deactivate
echo ""

echo "=========================================="
echo -e "${GREEN}✓ Synology Collector Installation Complete${NC}"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Edit configuration:"
echo "   nano $CONFIG_FILE"
echo ""
echo "2. Schedule jobs via Synology Control Panel:"
echo "   - Control Panel → Task Scheduler"
echo "   - Create → Scheduled Task → Custom Script"
echo ""
echo "   Job 1: Disk Collection (Daily at 2 AM)"
echo "   ├─ General: name='Lab Monitor - Disk', user='Admin'"
echo "   ├─ Schedule: Daily, 02:00"
echo "   └─ Task: cd $SCRIPTS_DIR/lab-monitor/collector && $VENV_DIR/bin/python3 collector.py --config local/config.json --mode disk"
echo ""
echo "   Job 2: Metrics Collection (Every 5 minutes)"
echo "   ├─ General: name='Lab Monitor - Metrics', user='Admin'"
echo "   ├─ Schedule: Daily, 00:00, repeat every 5 minutes"
echo "   └─ Task: cd $SCRIPTS_DIR/lab-monitor/collector && $VENV_DIR/bin/python3 collector.py --config local/config.json --mode metrics"
echo ""
echo "3. Verify jobs run:"
echo "   tail -f $LOGS_DIR/collector.log"
echo ""
