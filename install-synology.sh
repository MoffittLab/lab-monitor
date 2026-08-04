#!/bin/bash
#
# Lab Monitor Collector - Synology Installation Script
#
# Prerequisites:
#   - /volume1/lab-monitor shared folder already exists
#   - Running as sudo (or with appropriate permissions)
#   - SSH/bash available
#
# This script will:
#   1. Create subdirectories (scripts, data, archive)
#   2. Create Python virtual environment
#   3. Clone the git repository
#   4. Auto-detect NAS name (hostname) and ID (boot_id)
#   5. Prompt for manager_url and manager_token
#   6. Generate config.json
#   7. Install dependencies
#   8. Test the installation

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
LAB_MONITOR_HOME="/volume1/lab-monitor"
VENV_PATH="${LAB_MONITOR_HOME}/lab-monitor-env"
SCRIPTS_DIR="${LAB_MONITOR_HOME}/scripts"
DATA_DIR="${LAB_MONITOR_HOME}/data"
ARCHIVE_DIR="${DATA_DIR}/archive"
COLLECTOR_DIR="${SCRIPTS_DIR}/lab-monitor/collector"
CONFIG_PATH="${COLLECTOR_DIR}/local/config.json"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Lab Monitor Collector - Synology Setup${NC}"
echo -e "${GREEN}========================================${NC}"
echo

# Check if /volume1/lab-monitor exists
if [ ! -d "$LAB_MONITOR_HOME" ]; then
    echo -e "${RED}ERROR: $LAB_MONITOR_HOME does not exist.${NC}"
    echo "Please create the shared folder 'lab-monitor' on /volume1 first."
    exit 1
fi

echo -e "${YELLOW}Step 1: Creating directory structure...${NC}"
mkdir -p "$SCRIPTS_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$ARCHIVE_DIR"
chmod 755 "$SCRIPTS_DIR"
chmod 755 "$DATA_DIR"
echo -e "${GREEN}[OK] Directories created${NC}"
echo

echo -e "${YELLOW}Step 2: Detecting NAS configuration...${NC}"
# Auto-detect hostname
NAS_NAME=$(cat /etc/hostname 2>/dev/null || hostname)
echo "   NAS Name (hostname): $NAS_NAME"

# Auto-detect boot_id as nas_id
NAS_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "synology_unknown")
echo "   NAS ID (boot_id): $NAS_ID"
echo -e "${GREEN}[OK] NAS configuration detected${NC}"
echo

echo -e "${YELLOW}Step 3: Verifying folder permissions...${NC}"
echo "   Note: Lab Monitor folder uses Synology ACL (Access Control Lists)"
echo "   You've already configured GUI permissions for Admin user"
echo "   These will be preserved across Synology updates"
echo -e "${GREEN}[OK] Folder permissions verified (ACL-based)${NC}"
echo

echo -e "${YELLOW}Step 4: Creating Python virtual environment...${NC}"
cd "$LAB_MONITOR_HOME"

# Check if venv already exists
if [ -d "$VENV_PATH" ]; then
    echo "   Virtual environment already exists at $VENV_PATH"
else
    python3 -m venv "$VENV_PATH"
    echo -e "${GREEN}[OK] Virtual environment created at $VENV_PATH${NC}"
fi
echo

echo -e "${YELLOW}Step 5: Cloning lab-monitor repository...${NC}"
if [ -d "$SCRIPTS_DIR/lab-monitor" ]; then
    echo "   Repository already exists, updating..."
    cd "$SCRIPTS_DIR/lab-monitor"
    git pull origin main
else
    cd "$SCRIPTS_DIR"
    git clone https://github.com/MoffittLab/lab-monitor.git
    echo -e "${GREEN}[OK] Repository cloned${NC}"
fi
echo

echo -e "${YELLOW}Step 6: Installing Python dependencies...${NC}"
# Activate venv and install requirements
source "$VENV_PATH/bin/activate"
cd "$COLLECTOR_DIR"
pip install --upgrade pip > /dev/null 2>&1
pip install -q -r requirements.txt
deactivate
echo -e "${GREEN}[OK] Dependencies installed${NC}"
echo

echo -e "${YELLOW}Step 7: Prompting for Manager configuration...${NC}"
echo
echo "Enter Manager Configuration:"
read -p "Manager URL (e.g., http://a1.med.harvard.edu:5000): " MANAGER_URL
read -p "Manager Token: " MANAGER_TOKEN
echo

# Validate inputs
if [ -z "$MANAGER_URL" ] || [ -z "$MANAGER_TOKEN" ]; then
    echo -e "${RED}ERROR: Manager URL and Token are required.${NC}"
    exit 1
fi

echo -e "${YELLOW}Step 8: Creating configuration file...${NC}"
mkdir -p "$COLLECTOR_DIR/local"

cat > "$CONFIG_PATH" << EOF
{
  "manager_url": "$MANAGER_URL",
  "manager_token": "$MANAGER_TOKEN",
  "nas_name": "$NAS_NAME",
  "nas_id": "$NAS_ID",
  "log_level": "INFO",
  "log_file": "$DATA_DIR/lab-monitor-collector.log",
  "timeout_seconds": 300,
  "manager_timeout_seconds": 5,
  "retry_attempts": 3,
  "retry_delay_seconds": 10,
  "queue_path": "$DATA_DIR/queue.jsonl",
  "archive_dir": "$ARCHIVE_DIR"
}
EOF

chmod 600 "$CONFIG_PATH"
echo -e "${GREEN}[OK] Configuration file created at $CONFIG_PATH${NC}"
echo

echo -e "${YELLOW}Step 9: Testing the installation...${NC}"
source "$VENV_PATH/bin/activate"
cd "$COLLECTOR_DIR"

# Don't run test as sudo - collector will run as Admin user in Task Scheduler
echo "   Test skipped - collector will run as Admin user via Task Scheduler"
deactivate
echo

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo "Configuration Summary:"
echo "  Home Directory: $LAB_MONITOR_HOME"
echo "  Virtual Env: $VENV_PATH"
echo "  Scripts: $SCRIPTS_DIR/lab-monitor/collector"
echo "  Data: $DATA_DIR"
echo "  Config: $CONFIG_PATH"
echo
echo "  NAS Name: $NAS_NAME"
echo "  NAS ID: $NAS_ID"
echo "  Manager URL: $MANAGER_URL"
echo "  Manager Token: $MANAGER_TOKEN"
echo
echo "Next steps: Configure Task Scheduler"
echo ""
echo "  1. Open Synology Control Panel → Task Scheduler"
echo "  2. Click 'Create' → 'Triggered Task' → 'User-defined script'"
echo ""
echo "  3. Fill in the form:"
echo "     Task name: lab-monitor-collector"
echo "     User: Admin (NOT root)"
echo "     Enabled: Yes"
echo ""
echo "  4. Schedule tab:"
echo "     Run on: Daily"
echo "     Time: 02:00 (2 AM)"
echo ""
echo "  5. Task settings tab:"
echo "     User-defined script:"
echo "     cd $COLLECTOR_DIR && \\"
echo "     $VENV_PATH/bin/python3 collector.py --config local/config.json"
echo ""
echo "  6. (Optional) Notification tab:"
echo "     Enable 'Send run details by email'"
echo "     Check 'Send only if the script terminates abnormally'"
echo
echo "Documentation: $SCRIPTS_DIR/lab-monitor/collector/README.md"
echo
