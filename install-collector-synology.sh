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
# 5. Creates config.json (readable only by the installing user — it holds the token)
# 6. Prints the Task Scheduler jobs to create
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
# The collector needs 'requests' (required) and optionally 'psutil'.
# On Synology armv7l, psutil has no prebuilt wheel and must be compiled,
# which fails without gcc. psutil is therefore installed best-effort: if it
# can't be installed, the collector uses pure-Python /proc fallbacks for
# CPU, RAM, uptime, and network. Only 'requests' is treated as fatal.

# Choose install-mode flags once (venv vs. system --user).
if [ "$USE_VENV" = "1" ]; then
    PIP_USER_FLAG=""
else
    PIP_USER_FLAG="--user"
fi

cd "$SCRIPTS_DIR/lab-monitor"
# Upgrade pip (best-effort; don't fail the install if this can't run).
"$COLLECTOR_PYTHON" -m pip install $PIP_USER_FLAG --upgrade pip > /dev/null 2>&1 || true

# Versions come from collector/requirements.txt (single source of truth).
# The packages are installed one at a time because psutil is best-effort.
REQ_FILE="$SCRIPTS_DIR/lab-monitor/collector/requirements.txt"
req_spec() {
    # Print the requirement line for package $1 (e.g. "requests==X.Y.Z"),
    # or the bare name if collector/requirements.txt doesn't list it.
    local spec
    spec=$(grep -iE "^[[:space:]]*$1[[:space:]]*([=<>!~]|$)" "$REQ_FILE" 2>/dev/null | head -n 1 | sed -e 's/#.*//' -e 's/[[:space:]]//g')
    if [ -n "$spec" ]; then echo "$spec"; else echo "$1"; fi
}
REQUESTS_SPEC=$(req_spec requests)
PSUTIL_SPEC=$(req_spec psutil)

# --- requests (REQUIRED) ---
if ! "$COLLECTOR_PYTHON" -m pip install $PIP_USER_FLAG "$REQUESTS_SPEC" > /tmp/pip-install.log 2>&1; then
    echo -e "${RED}ERROR: Could not install 'requests' (required)${NC}"
    echo "See /tmp/pip-install.log"
    [ -z "$PIP_USER_FLAG" ] || echo "If pip is missing, install it: python3 -m ensurepip --user"
    exit 1
fi
echo "[OK] $REQUESTS_SPEC installed"

# --- psutil (OPTIONAL, best-effort) ---
if "$COLLECTOR_PYTHON" -m pip install $PIP_USER_FLAG "$PSUTIL_SPEC" >> /tmp/pip-install.log 2>&1; then
    echo "[OK] psutil installed (full metrics)"
else
    echo -e "${YELLOW}Note: psutil could not be installed (no ARM wheel / no compiler).${NC}"
    echo "      The collector will use pure-Python /proc fallbacks for CPU,"
    echo "      RAM, uptime, and network. This is expected on Synology armv7l."
fi
echo "[OK] Dependencies ready using $COLLECTOR_PYTHON"
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
    
    # Prompt for Manager URL (http:// or https:// — whatever the Manager is served on)
    echo "The Manager itself serves plain http (e.g., http://atlantis.med.harvard.edu:5000);"
    echo "use an https:// URL only if it sits behind a TLS proxy."
    read -r -p "Enter Manager URL: " MANAGER_URL
    if [ -z "$MANAGER_URL" ]; then
        echo -e "${RED}ERROR: Manager URL cannot be empty${NC}"
        exit 1
    fi
    case "$MANAGER_URL" in
        https://*) ;;
        http://*)
            echo -e "${YELLOW}Warning: $MANAGER_URL is plain http:// — the Manager token will travel unencrypted on the network.${NC}"
            ;;
        *)
            echo -e "${YELLOW}Warning: the Manager URL should start with http:// or https:// (got: $MANAGER_URL)${NC}"
            ;;
    esac

    # Prompt for Manager Token (this device's collector token from the Manager config)
    read -r -p "Enter Manager Token (this device's collector token from the Manager config): " MANAGER_TOKEN
    if [ -z "$MANAGER_TOKEN" ]; then
        echo -e "${RED}ERROR: Manager Token cannot be empty${NC}"
        exit 1
    fi

    # Prompt for Device Type
    read -r -p "Enter Device Type [NAS/NAS-Instrument/Server/NAS-Backup]: " DEVICE_TYPE
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
    read -r -p "Enter scan paths (comma-separated, or press Enter to use volume roots): " SCAN_PATHS_INPUT

    # Auto-detect data volumes present on this NAS. Match /volume<number>
    # only, skipping /volumeUSB*, /volumeSATA*, and the literal glob when
    # nothing matches (nullglob is not reliable in DSM's ash/bash).
    VOLUMES_LIST=""
    for vol in /volume[0-9]*; do
        # Guard against the no-match case where the glob stays literal.
        [ -d "$vol" ] || continue
        case "$vol" in
            *\**) continue ;;  # literal '/volume[0-9]*' — no volumes matched
        esac
        VOLUMES_LIST="$VOLUMES_LIST$vol
"
    done
    if [ -z "$VOLUMES_LIST" ]; then
        echo -e "${YELLOW}Warning: no /volumeN data volumes auto-detected.${NC}"
        echo "Defaulting to /volume1; edit the config afterward if that is wrong."
        VOLUMES_LIST="/volume1
"
    fi
    echo ""

    # config.json is built by Python's json module: every answer is passed
    # through the environment (never spliced into code or a heredoc), so
    # quotes, backslashes or apostrophes in a path or token can't break the
    # JSON. Scan paths are split on commas and trimmed; one wrapping pair of
    # quotes is removed. Empty input → "<volume>/*" for each volume.
    # umask 077 + chmod 600: the file holds the Manager token, so only this
    # user (who must also be the Task Scheduler user) can read it.
    echo "Configuration saved with:"
    echo "  - Device Type:   $DEVICE_TYPE"
    if ! (umask 077 && \
          LM_CONFIG_FILE="$CONFIG_FILE" \
          LM_NAME="$NAS_HOSTNAME" \
          LM_ID="$NAS_ID" \
          LM_DEVICE_TYPE="$DEVICE_TYPE" \
          LM_MANAGER_URL="$MANAGER_URL" \
          LM_MANAGER_TOKEN="$MANAGER_TOKEN" \
          LM_VOLUMES="$VOLUMES_LIST" \
          LM_SCAN_PATHS_INPUT="$SCAN_PATHS_INPUT" \
          LM_DATA_DIR="$DATA_DIR" \
          LM_LOG_FILE="$LOGS_DIR/collector.log" \
          "$COLLECTOR_PYTHON" - <<'PYCONFIG'
import json
import os

env = os.environ


def clean(path):
    path = path.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in '"\'':
        path = path[1:-1].strip()
    return path


volumes = [v for v in env['LM_VOLUMES'].split('\n') if v.strip()]
scan_paths = [p for p in (clean(p) for p in env.get('LM_SCAN_PATHS_INPUT', '').split(',')) if p]
if not scan_paths:
    scan_paths = [v + '/*' for v in volumes]

config = {
    'name': env['LM_NAME'],
    'id': env['LM_ID'],
    'device_type': env['LM_DEVICE_TYPE'],
    'manager_url': env['LM_MANAGER_URL'],
    'manager_token': env['LM_MANAGER_TOKEN'],
    'volumes': volumes,
    'scan_paths': scan_paths,
    'data_dir': env['LM_DATA_DIR'],
    'log_file': env['LM_LOG_FILE'],
    'log_level': 'INFO',
    'timeout_seconds': 3600,
    # total_timeout_seconds is left unset: the default scales with the number
    # of scan_paths entries (see collector/README.md, "Time budgets")
    'request_timeout_seconds': 30,
    'max_batch_messages': 100,
    'max_queue_messages': 50000,
    # Interval counters, per-user metrics (active users only on DSM), who is
    # connected, storage health and the storage breakdown, spelled out with
    # their defaults (collector/lib/settings.py, config.synology.example.json)
    'perf_counters': True,
    'counter_max_window_seconds': 86400,
    'user_metrics': 'auto',
    'user_metrics_min_cores': 0.02,
    'user_metrics_min_ram_bytes': 1073741824,
    'user_cpu_count_children': True,
    'disk_io': True,
    'disk_io_exclude': [],
    'sessions': {
        'enabled': True, 'detail': 'full', 'history': 'changes', 'list_heartbeat_seconds': 3600,
        'max_entries': 200, 'ports': None, 'extra_ports': {}, 'exclude_clients': [],
        'timeout_seconds': 20,
    },
    'storage_health': {
        'enabled': True, 'interval_seconds': 3600, 'timeout_seconds': 90,
        'temperature_c': {'hdd': [50, 60], 'ssd': [70, 80]}, 'ignore': [],
    },
    'storage_breakdown': {
        'enabled': True, 'owners': 'file', 'top_owners': 10, 'device_top_owners': 25,
        'max_rows': 5000, 'cold_days': [365, 1095], 'name_lookup_budget_seconds': 30,
        'max_timeout_fraction': 0.6,
    },
}
path = env['LM_CONFIG_FILE']
with open(path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
os.chmod(path, 0o600)
print('  - Volumes:       ' + ', '.join(volumes))
print('  - Scan Paths:    ' + ', '.join(scan_paths))
PYCONFIG
    ); then
        echo -e "${RED}ERROR: Could not write $CONFIG_FILE${NC}"
        exit 1
    fi
    echo "  - Manager URL:   $MANAGER_URL"
    echo "  - Manager Token: (set)"
    echo ""
    echo "[OK] Config file created at $CONFIG_FILE (permissions 600)"
    echo ""
    echo "To modify later:"
    echo "  nano $CONFIG_FILE"
    echo ""
else
    echo "[OK] Config file already exists at $CONFIG_FILE"
    echo "   (Edit manually if needed: nano $CONFIG_FILE)"
    # It holds the Manager token, so it should be readable by its owner only.
    # But tightening it locks out every other account that reads it (the DSM
    # tasks may run as another user; tools may SSH in as one), so ask first.
    CONFIG_MODE=$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || echo "")
    CONFIG_OWNER=$(stat -c '%U' "$CONFIG_FILE" 2>/dev/null || echo "")
    case "$CONFIG_MODE" in
        600|400)
            echo "   Permissions: $CONFIG_MODE (readable only by its owner '$CONFIG_OWNER')"
            ;;
        *)
            echo ""
            echo -e "${YELLOW}$CONFIG_FILE contains the Manager token and is readable by other users (mode ${CONFIG_MODE:-unknown}, owner ${CONFIG_OWNER:-unknown}).${NC}"
            echo "   Restricting it to 600 means BOTH 'Lab Monitor' tasks in DSM Task Scheduler, and any"
            echo "   tool that runs the collector over SSH, must run as '${CONFIG_OWNER:-its owner}' or root."
            echo "   Tasks under any other user would fail with 'Permission denied' and stop collecting."
            echo "   Check the 'User' column of those tasks before answering."
            read -r -p "   Restrict config.json to 600 now? [y/N]: " TIGHTEN_CONFIG || TIGHTEN_CONFIG=""
            case "$TIGHTEN_CONFIG" in
                [Yy]|[Yy][Ee][Ss])
                    if chmod 600 "$CONFIG_FILE" 2>/dev/null; then
                        echo "   Permissions set to 600: run both tasks as '${CONFIG_OWNER:-its owner}' or root"
                    else
                        echo -e "${YELLOW}Warning: could not chmod 600 $CONFIG_FILE (owned by another user?).${NC}"
                    fi
                    ;;
                *)
                    echo "   Left unchanged. To restrict it later: chmod 600 $CONFIG_FILE"
                    ;;
            esac
            ;;
    esac
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
# What storage health can read as this (non-root) account: RAID state from
# md, disk state from sysfs, read-only volumes, temperatures where hwmon has
# them. Nothing is sent (--dry-run).
echo "Checking storage health as $CURRENT_USER (dry run, nothing sent)..."
if "$COLLECTOR_PYTHON" collector.py --config local/config.json --mode health --dry-run 2>/dev/null \
        | "$COLLECTOR_PYTHON" -c 'import json, sys
d = json.load(sys.stdin)[0]["data"]
print("   Storage health: %s (%s)" % (d["overall_status"], ", ".join("%s %s" % kv for kv in sorted(d["sources"].items()))))
for reason in d["status_reasons"]:
    print("   - " + reason)'; then
    :
else
    echo "   Storage health could not be checked (see $LOGS_DIR/collector.log)"
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
echo "   Run both tasks as the owner of config.json ('$(stat -c '%U' "$CONFIG_FILE" 2>/dev/null || echo "$CURRENT_USER")') or root:"
echo "   with mode 600 (the default for new installs) no other user can read it."
echo ""
echo "   Task 1: Lab Monitor - Disk Collection"
echo "   - Task name: 'Lab Monitor - Disk Collection'"
echo "   - User: $CURRENT_USER"
echo "   - Schedule: Daily at 02:00 (2 AM)"
echo "   - User-defined script:"
echo "     cd $SCRIPTS_DIR/lab-monitor/collector && $COLLECTOR_PYTHON collector.py --config local/config.json --mode disk"
echo ""
echo "   Task 2: Lab Monitor - Metrics Collection"
echo "   - Task name: 'Lab Monitor - Metrics Collection'"
echo "   - User: $CURRENT_USER"
echo "   - Schedule: Daily at 00:00, repeat every 5 minutes"
echo "   - User-defined script:"
echo "     cd $SCRIPTS_DIR/lab-monitor/collector && $COLLECTOR_PYTHON collector.py --config local/config.json --mode metrics"
echo ""
echo "3. Verify jobs run:"
echo "   tail -f $LOGS_DIR/collector.log"
echo ""
echo "Without root this collector reports who is connected by client IP for SMB, NFS, AFP, SSH,"
echo "DSM and the other services (SSH logins by name where DSM keeps utmp), RAID state from md,"
echo "and disk state from sysfs. SMB usernames and disk SMART data need root and are not"
echo "collected in this release."
echo ""
