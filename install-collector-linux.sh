#!/usr/bin/env bash
#
# Lab Monitor - Linux (Ubuntu / Debian) Collector Installation Script
#
# Usage (from a checkout of the repository, as an administrator):
#   git clone https://github.com/MoffittLab/lab-monitor.git && cd lab-monitor
#   sudo ./install-collector-linux.sh                    # asks for the settings
#
#   sudo ./install-collector-linux.sh --non-interactive \
#        --manager-url http://atlantis.med.harvard.edu:5000 --generate-token \
#        --scan-paths '/home/*,/data/*'
#
#   sudo ./install-collector-linux.sh --dry-run ...      # print the actions only
#   ./install-collector-linux.sh --print-config ...      # print the config it would write
#   sudo /opt/lab-monitor/install-collector-linux.sh --uninstall [--purge]
#
# Supported: Ubuntu 22.04 / 24.04, Debian 12 (systemd, apt). Run with sudo.
#
# This script:
#   1. Installs python3-venv with apt when it is missing
#   2. Creates the system user 'labmon' (no login shell, home /var/lib/lab-monitor)
#   3. Installs the collector code to /opt/lab-monitor (from this checkout,
#      --source DIR, or a git clone of --repo/--ref), owned by root
#   4. Creates the venv /opt/lab-monitor/venv and installs
#      collector/requirements.txt (+ requirements-gpu.txt when nvidia-smi exists);
#      offline, it falls back to the Debian packages python3-requests/python3-psutil
#   5. Writes /etc/lab-monitor/collector.json (0640 root:labmon) with Python's
#      json module; an existing config is kept, flags change only the keys they name
#   6. Installs and enables the systemd units (collector/systemd/):
#        lab-monitor-metrics.timer  every 5 minutes
#        lab-monitor-disk.timer     daily at 02:00 (+ up to 15 min)
#   7. Runs one metrics collection and shows its result
#
# Re-running it upgrades in place: code, venv and units are refreshed and the
# config is kept.
#
set -euo pipefail
umask 022    # code and venv must be readable by the service user, whatever root's umask

# ------------------------------------------------------------------------------
# Layout (fixed: the unit files in collector/systemd/ refer to these paths)
# ------------------------------------------------------------------------------
PREFIX=/opt/lab-monitor
CODE_DIR="$PREFIX/collector"
VENV_DIR="$PREFIX/venv"
CONFIG_DIR=/etc/lab-monitor
CONFIG_FILE="$CONFIG_DIR/collector.json"
DATA_DIR=/var/lib/lab-monitor
LOG_DIR=/var/log/lab-monitor
LOG_FILE="$LOG_DIR/collector.log"
UNIT_DIR=/etc/systemd/system
SERVICE_USER=labmon
UNITS=(lab-monitor-metrics.service lab-monitor-metrics.timer
       lab-monitor-disk.service lab-monitor-disk.timer)
TIMERS=(lab-monitor-metrics.timer lab-monitor-disk.timer)
SERVICES=(lab-monitor-metrics.service lab-monitor-disk.service)
DISK_DROPIN_DIR="$UNIT_DIR/lab-monitor-disk.service.d"
DISK_DROPIN="$DISK_DROPIN_DIR/50-timeout.conf"
NAME_RE='^[A-Za-z0-9_.-]{1,64}$'
DEVICE_TYPES="Server NAS NAS-Instrument NAS-Backup"
REQUIRED_PYTHON="3.8"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO=https://github.com/MoffittLab/lab-monitor.git
# Python run as root never imports from the current directory or PYTHON* env vars
PY=(python3 -I)
# Root-only directory (/run is 0755 root): a lock in the world-writable
# /run/lock could be pre-created or held by any local user to block installs
INSTALL_LOCK=/run/lab-monitor-install.lock
# apt-get, unlike apt, does not wait for a package-manager lock by default, and
# unattended-upgrades holds it for minutes after a fresh VM boots
APT_LOCK_WAIT=300
APT_RETRY_INTERVAL=10

# ------------------------------------------------------------------------------
# Options
# ------------------------------------------------------------------------------
OPT_MANAGER_URL=""
OPT_TOKEN=""
OPT_GENERATE_TOKEN=0
OPT_NAME=""
OPT_ID=""
OPT_DEVICE_TYPE=""
OPT_SCAN_PATHS=""
OPT_SCAN_PATHS_SET=0
OPT_VOLUMES=""
OPT_VOLUMES_SET=0
OPT_NON_INTERACTIVE=0
OPT_UNINSTALL=0
OPT_PURGE=0
OPT_DRY_RUN=0
OPT_PRINT_CONFIG=0
OPT_REPO=""
OPT_REF=""
OPT_SOURCE=""

GENERATED_TOKEN=""
TMP_DIRS=()
TIMERS_STOPPED=0

if [[ -t 1 ]]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; BOLD=""; NC=""
fi

usage() {
    cat <<'EOF'
Usage: sudo ./install-collector-linux.sh [options]

Settings (prompted for when missing, unless --non-interactive):
  --manager-url URL     Manager URL, e.g. http://atlantis.med.harvard.edu:5000
  --token TOKEN         This device's collector token (visible in ps / shell history;
                        prefer the prompt, --token-file or --generate-token)
  --token-file FILE     Read the token from the first line of FILE
  --generate-token      Create a new random token and print the collector_tokens
                        entry to add to the Manager config
  --name NAME           Device name shown on the Dashboard (default: short hostname);
                        1-64 characters from A-Z a-z 0-9 _ . -
  --id ID               Stable device id (default: linux-<name>)
  --device-type TYPE    Server (default), NAS, NAS-Instrument or NAS-Backup
  --scan-paths LIST     Comma-separated folders to measure; DIR/* = each subfolder
                        (default for a new config: /home/*, plus /data/* if it exists;
                        'none' = report volume capacity only)
  --volumes LIST        Comma-separated mount points to report capacity for
                        ('auto' or empty = auto-discover real filesystems)

Modes:
  --non-interactive     Never prompt; fail if a required setting is missing
  --dry-run             Print the actions without changing anything
  --print-config        Print the config JSON that would be written, then exit
                        (no root needed)
  --uninstall           Remove units and code; keep config, data and logs
  --purge               With --uninstall: also delete config, data (archives), logs
                        and the labmon user

Source of the code (default: the checkout this script is in):
  --source DIR          A checkout containing collector/collector.py
  --repo URL [--ref REF]  git clone URL (and check out REF: branch, tag or commit)

  -h, --help            Show this help

Re-running upgrades in place: code, venv and units are refreshed, the config is
kept (flags change only the keys they name).
EOF
}

# ------------------------------------------------------------------------------
# Output and command helpers
# ------------------------------------------------------------------------------
step() { printf '\n%s==> %s%s\n' "$YELLOW" "$*" "$NC"; }
ok()   { printf '%s[OK]%s %s\n' "$GREEN" "$NC" "$*"; }
info() { printf '%s\n' "$*"; }
warn() { printf '%sWarning:%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }
usage_error() { printf '%sERROR:%s %s\n' "$RED" "$NC" "$*" >&2; echo "Run with --help for usage." >&2; exit 2; }

# run CMD ARGS...: execute, or with --dry-run only print the command
run() {
    if (( OPT_DRY_RUN )); then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# write_file PATH MODE: write stdin to PATH atomically (temp file + rename)
write_file() {
    local path=$1 mode=$2 tmp
    if (( OPT_DRY_RUN )); then
        printf '[dry-run] write %s (mode %s):\n' "$path" "$mode"
        sed 's/^/    | /'
        return 0
    fi
    tmp=$(mktemp "$(dirname "$path")/.lab-monitor.XXXXXX")
    cat >"$tmp"
    chmod "$mode" "$tmp"
    mv -f "$tmp" "$path"
}

cleanup() {
    local d
    for d in "${TMP_DIRS[@]}"; do
        [[ -n $d && -d $d ]] && rm -rf -- "$d"
    done
    # An upgrade that failed half-way must not leave collection switched off
    if (( TIMERS_STOPPED )); then
        warn "Restarting the timers stopped for the upgrade."
        systemctl start "${TIMERS[@]}" 2>/dev/null || true
    fi
    return 0
}
trap cleanup EXIT

interactive() { (( ! OPT_NON_INTERACTIVE )) && [[ -t 0 ]]; }

# ask VAR PROMPT [DEFAULT]: read a line into VAR (DEFAULT on empty input)
ask() {
    local __var=$1 __prompt=$2 __default=${3-} __reply=""
    [[ -n $__default ]] && __prompt+=" [$__default]"
    read -r -p "$__prompt: " __reply || die "No answer (end of input); pass the settings as flags with --non-interactive."
    [[ -z $__reply ]] && __reply=$__default
    printf -v "$__var" '%s' "$__reply"
}

# confirm PROMPT: yes/no question, default no. --non-interactive answers yes;
# without a terminal (and without --non-interactive) it refuses.
confirm() {
    local reply=""
    if (( OPT_NON_INTERACTIVE )); then
        return 0
    fi
    [[ -t 0 ]] || die "No terminal to confirm \"$1\"; re-run with --non-interactive to proceed."
    read -r -p "$1 [y/N]: " reply || reply=""
    [[ $reply =~ ^[Yy]([Ee][Ss])?$ ]]
}

# ------------------------------------------------------------------------------
# Validation (also used by the tests)
# ------------------------------------------------------------------------------
# LC_ALL=C: in a UTF-8 locale [A-Za-z] can also match letters such as 'ï'
valid_name() { local LC_ALL=C; [[ $1 =~ $NAME_RE ]]; }

valid_url() { [[ $1 =~ ^https?://[^[:space:]/]+ ]] && [[ ! $1 =~ [[:space:]] ]]; }

# Same rules the Manager applies: >= 16 characters, no placeholder text
token_problem() {
    local t=$1 upper
    upper=$(printf '%s' "$t" | tr '[:lower:]' '[:upper:]')
    if [[ -z $t ]]; then
        echo "the token is empty"
    elif (( ${#t} < 16 )); then
        echo "the token is shorter than 16 characters (the Manager refuses those)"
    elif [[ $t =~ [[:space:]] ]]; then
        echo "the token contains whitespace"
    elif [[ $upper == *CHANGE-ME* || $upper == *YOUR-* || $upper == *GENERATE-RANDOM* || $upper == *SAME-TOKEN* ]]; then
        echo "the token is a template placeholder"
    fi
}

generate_token() {
    "${PY[@]}" -c 'import secrets; print(secrets.token_urlsafe(32))'
}

default_name() {
    local n
    n=$(hostname -s 2>/dev/null || uname -n)
    printf '%s' "${n%%.*}"
}

default_scan_paths() {
    local paths="/home/*"
    [[ -d /data ]] && paths+=",/data/*"
    printf '%s' "$paths"
}

# The exact entry the Manager config needs for this device
print_manager_token_entry() {
    local name=$1 token=$2
    LM_NAME="$name" LM_TOKEN="$token" "${PY[@]}" -c '
import json, os
name, token = os.environ["LM_NAME"], os.environ["LM_TOKEN"]
print("  Add this device to the Manager config (manager/config.json), then restart the Manager:")
print()
print("    " + json.dumps("collector_tokens") + ": " + json.dumps({name: token}))
print()
print("  or, if it already has a collector_tokens object, add this entry to it:")
print()
print("    " + json.dumps(name) + ": " + json.dumps(token))
'
}

# ------------------------------------------------------------------------------
# Config: written by Python's json module. Every value travels in an
# environment variable (never spliced into code), so quotes, backslashes or $()
# in a path or token cannot break the JSON or run anything.
#
# write_config OUT [EXISTING]
#   OUT       destination file, or - for stdout
#   EXISTING  existing config to start from (only the keys given change)
# Values come from LM_* variables; see the PYCONFIG program.
# ------------------------------------------------------------------------------
write_config() {
    local out=$1 existing=${2-}
    LM_OUT="$out" LM_CONFIG_IN="$existing" \
    LM_DATA_DIR="$DATA_DIR" LM_LOG_FILE="$LOG_FILE" \
    "${PY[@]}" - <<'PYCONFIG'
import grp
import json
import os
import re
import sys
import tempfile

env = os.environ
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
PLACEHOLDERS = ('CHANGE-ME', 'YOUR-', 'GENERATE-RANDOM', 'SAME-TOKEN')


def split_list(text):
    """Comma- or newline-separated list; one wrapping pair of quotes is removed."""
    items = []
    for part in re.split(r'[,\n]', text):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] and part[0] in '"\'':
            part = part[1:-1].strip()
        if part:
            items.append(part)
    return items


existing = env.get('LM_CONFIG_IN', '')
if existing:
    with open(existing) as f:
        config = json.load(f)
    if not isinstance(config, dict):
        sys.exit(f"ERROR: {existing} is not a JSON object")
else:
    config = {
        'name': None,
        'id': None,
        'device_type': 'Server',
        'platform': 'linux',
        'manager_url': None,
        'manager_token': None,
        # 'volumes' left out: real filesystems are discovered from the mount table
        'scan_paths': ['/home/*'],
        'include_network_mounts': False,
        'cross_filesystems': False,
        'data_dir': env['LM_DATA_DIR'],
        'log_file': env['LM_LOG_FILE'],
        'log_level': 'INFO',
        'timeout_seconds': 3600,
        # Fixed (not scaled with scan_paths) so the disk unit's TimeoutStartSec
        # can be sized from it
        'total_timeout_seconds': 10800,
        'request_timeout_seconds': 30,
        'max_batch_messages': 100,
        'max_queue_messages': 50000,
        'exclude_users': [],
    }

for key, var in (('name', 'LM_NAME'), ('id', 'LM_ID'), ('device_type', 'LM_DEVICE_TYPE'),
                 ('manager_url', 'LM_MANAGER_URL'), ('manager_token', 'LM_MANAGER_TOKEN')):
    value = env.get(var, '').strip()
    if value:
        config[key] = value
if isinstance(config.get('manager_url'), str):
    config['manager_url'] = config['manager_url'].rstrip('/')
if not config.get('id') and config.get('name'):
    config['id'] = 'linux-' + str(config['name']).lower()

if env.get('LM_SCAN_PATHS_SET') == '1':
    paths = split_list(env.get('LM_SCAN_PATHS', ''))
    config['scan_paths'] = [] if [p.lower() for p in paths] == ['none'] else paths
if env.get('LM_VOLUMES_SET') == '1':
    volumes = split_list(env.get('LM_VOLUMES', ''))
    if not volumes or [v.lower() for v in volumes] == ['auto']:
        config.pop('volumes', None)
    else:
        config['volumes'] = volumes

problems = []
if not NAME_RE.fullmatch(str(config.get('name') or '')):
    problems.append(f"name {config.get('name')!r} must be 1-64 characters from A-Z a-z 0-9 _ . -")
if not re.fullmatch(r'https?://[^\s/]+\S*', str(config.get('manager_url') or '')):
    problems.append(f"manager_url {config.get('manager_url')!r} must start with http:// or https://")
token = str(config.get('manager_token') or '')
if len(token) < 16 or any(c.isspace() for c in token) or any(p in token.upper() for p in PLACEHOLDERS):
    problems.append('manager_token must be at least 16 characters, without spaces or placeholder text')
for key in ('scan_paths', 'volumes'):
    value = config.get(key)
    if value is None:
        continue
    if not isinstance(value, list):
        problems.append(f"{key} must be a list")
        continue
    for path in value:
        if not isinstance(path, str) or not path.startswith('/'):
            problems.append(f"{key} entry {path!r} must be an absolute path")
if problems:
    for p in problems:
        print(f"ERROR: {p}", file=sys.stderr)
    sys.exit(2)

text = json.dumps(config, indent=2) + '\n'
out = env.get('LM_OUT', '-')
if out == '-':
    if env.get('LM_REDACT') == '1':
        text = json.dumps({**config, 'manager_token': '<redacted>'}, indent=2) + '\n'
    sys.stdout.write(text)
else:
    group = env.get('LM_GROUP', '')
    fd, tmp = tempfile.mkstemp(prefix='.collector.', suffix='.json', dir=os.path.dirname(out))
    try:
        os.fchmod(fd, 0o640)        # the token: root writes, the service group reads
        if group:
            os.fchown(fd, -1, grp.getgrnam(group).gr_gid)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
PYCONFIG
}

# config_get FILE KEY: print a config value (lists comma-separated, missing = empty)
config_get() {
    "${PY[@]}" -c '
import json, sys
value = json.load(open(sys.argv[1])).get(sys.argv[2])
if isinstance(value, list):
    print(",".join(str(v) for v in value))
else:
    print("" if value is None else value)
' "$1" "$2"
}

# disk_timeout_seconds FILE: TimeoutStartSec for the disk unit = the collector's
# overall scan budget (total_timeout_seconds, or its default: max(3, number of
# scan_paths entries) x timeout_seconds, see _default_total_timeout in
# collector.py) + 1 h for volume capacity and the upload. Values are read the
# way the collector's _cfg_number reads them: float(value), so "36000" counts;
# booleans, values below 1 and anything float() rejects mean "the default".
disk_timeout_seconds() {
    "${PY[@]}" -c '
import json, math, sys
config = json.load(open(sys.argv[1]))
def number(key, default):
    value = config.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    # nan/inf: the collector would take them, but no unit timeout can follow
    if not math.isfinite(value) or value < 1:
        return default
    return value
timeout = number("timeout_seconds", 3600)
entries = len(config.get("scan_paths") or config.get("volumes") or [])
total = number("total_timeout_seconds", max(3, entries) * timeout)
print(int(total) + 3600)
' "$1"
}

# Linux volumes the collector will auto-discover (uses the collector's own code)
detect_volumes() {
    local code_dir=$1
    "${PY[@]}" -c '
import sys
sys.path.insert(0, sys.argv[1])
from lib.disk_usage import discover_linux_volumes
print(", ".join(discover_linux_volumes()))
' "$code_dir" 2>/dev/null || true
}

# ------------------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------------------
parse_args() {
    local arg value
    while (( $# )); do
        arg=$1
        value=""
        case $arg in
            --*=*) value=${arg#*=}; arg=${arg%%=*} ;;
        esac
        case $arg in
            --manager-url|--token|--token-file|--name|--id|--device-type|--scan-paths|--volumes|--repo|--ref|--source)
                if [[ $1 != *=* ]]; then
                    (( $# >= 2 )) || usage_error "$arg needs a value"
                    value=$2
                    shift
                fi
                ;;
        esac
        case $arg in
            --manager-url)     OPT_MANAGER_URL=$value ;;
            --token)           OPT_TOKEN=$value ;;
            --token-file)
                [[ -r $value ]] || usage_error "cannot read --token-file $value"
                IFS= read -r OPT_TOKEN <"$value" || true
                OPT_TOKEN=${OPT_TOKEN%$'\r'}
                ;;
            --generate-token)  OPT_GENERATE_TOKEN=1 ;;
            --name)            OPT_NAME=$value ;;
            --id)              OPT_ID=$value ;;
            --device-type)     OPT_DEVICE_TYPE=$value ;;
            --scan-paths)      OPT_SCAN_PATHS=$value; OPT_SCAN_PATHS_SET=1 ;;
            --volumes)         OPT_VOLUMES=$value; OPT_VOLUMES_SET=1 ;;
            --repo)            OPT_REPO=$value ;;
            --ref)             OPT_REF=$value ;;
            --source)          OPT_SOURCE=$value ;;
            --non-interactive) OPT_NON_INTERACTIVE=1 ;;
            --uninstall)       OPT_UNINSTALL=1 ;;
            --purge)           OPT_PURGE=1 ;;
            --dry-run)         OPT_DRY_RUN=1 ;;
            --print-config)    OPT_PRINT_CONFIG=1; OPT_NON_INTERACTIVE=1 ;;
            -h|--help)         usage; exit 0 ;;
            *)                 usage_error "unknown option: $1" ;;
        esac
        shift
    done

    if [[ -n $OPT_TOKEN ]] && (( OPT_GENERATE_TOKEN )); then
        usage_error "use either --token/--token-file or --generate-token, not both"
    fi
    if (( OPT_PURGE && ! OPT_UNINSTALL )); then
        usage_error "--purge only goes with --uninstall"
    fi
    if [[ -n $OPT_REF && -z $OPT_REPO ]]; then
        usage_error "--ref needs --repo"
    fi
    if [[ -n $OPT_REPO && -n $OPT_SOURCE ]]; then
        usage_error "use either --repo or --source, not both"
    fi
    if [[ -n $OPT_NAME ]] && ! valid_name "$OPT_NAME"; then
        usage_error "--name '$OPT_NAME' must be 1-64 characters from A-Z a-z 0-9 _ . - (the Manager rejects anything else)"
    fi
    if [[ -n $OPT_MANAGER_URL ]] && ! valid_url "$OPT_MANAGER_URL"; then
        usage_error "--manager-url must start with http:// or https:// (got: $OPT_MANAGER_URL)"
    fi
    if [[ -n $OPT_TOKEN ]]; then
        local problem
        problem=$(token_problem "$OPT_TOKEN")
        [[ -z $problem ]] || usage_error "--token: $problem"
    fi
    return 0
}

# Any setting given on the command line?
settings_flags_given() {
    [[ -n $OPT_MANAGER_URL || -n $OPT_TOKEN || -n $OPT_NAME || -n $OPT_ID || -n $OPT_DEVICE_TYPE ]] \
        || (( OPT_GENERATE_TOKEN || OPT_SCAN_PATHS_SET || OPT_VOLUMES_SET ))
}

require_root() {
    if (( EUID != 0 )); then
        die "Run this script with sudo (or as root). To preview it unprivileged, add --dry-run."
    fi
}

# ------------------------------------------------------------------------------
# Settings: flags, prompts, defaults -> LM_* variables for write_config
# ------------------------------------------------------------------------------
check_device_type() {
    local t
    for t in $DEVICE_TYPES; do
        [[ $1 == "$t" ]] && return 0
    done
    warn "'$1' is not a recognised device type (expected one of: $DEVICE_TYPES). Continuing anyway."
}

# gather_settings EXISTING_CONFIG_OR_EMPTY
gather_settings() {
    local existing=${1-} name="" url="" token="" dtype="" scan="" vols="" detected="" problem=""

    export LM_NAME="" LM_ID="$OPT_ID" LM_DEVICE_TYPE="" LM_MANAGER_URL="" LM_MANAGER_TOKEN=""
    export LM_SCAN_PATHS="" LM_SCAN_PATHS_SET=0 LM_VOLUMES="" LM_VOLUMES_SET=0

    if [[ -n $existing ]]; then
        # Re-run: keep the config; change only what the flags (or answers) name
        name=$OPT_NAME url=$OPT_MANAGER_URL token=$OPT_TOKEN dtype=$OPT_DEVICE_TYPE
        if (( OPT_GENERATE_TOKEN )); then
            GENERATED_TOKEN=$(generate_token)
            token=$GENERATED_TOKEN
        fi
        if ! settings_flags_given && interactive && \
                confirm "The existing configuration is kept. Change some of its settings now?"; then
            ask url "Manager URL" "$(config_get "$existing" manager_url)"
            read -r -s -p "Collector token (Enter = keep the current one): " token || token=""
            echo
            ask name "Device name" "$(config_get "$existing" name)"
            ask dtype "Device type ($DEVICE_TYPES)" "$(config_get "$existing" device_type)"
            ask scan "Folders to measure (comma-separated, DIR/* = each subfolder, 'none')" \
                "$(config_get "$existing" scan_paths)"
            ask vols "Volumes for capacity (comma-separated, 'auto')" \
                "$(config_get "$existing" volumes)"
            LM_SCAN_PATHS=$scan LM_SCAN_PATHS_SET=1
            LM_VOLUMES=${vols:-auto} LM_VOLUMES_SET=1
        fi
    else
        name=${OPT_NAME:-} url=$OPT_MANAGER_URL token=$OPT_TOKEN dtype=$OPT_DEVICE_TYPE
        if interactive; then
            if [[ -z $url ]]; then
                info "The Manager serves plain http on port 5000 (e.g. http://atlantis.med.harvard.edu:5000);"
                info "use an https:// URL only if it sits behind a TLS proxy."
                while :; do
                    ask url "Manager URL"
                    valid_url "$url" && break
                    warn "The Manager URL must start with http:// or https://"
                done
            fi
            if [[ -z $token ]] && (( ! OPT_GENERATE_TOKEN )); then
                while :; do
                    read -r -s -p "Collector token for this device (Enter = generate a new one): " token || token=""
                    echo
                    if [[ -z $token ]]; then
                        OPT_GENERATE_TOKEN=1
                        break
                    fi
                    problem=$(token_problem "$token")
                    [[ -z $problem ]] && break
                    warn "$problem"
                done
            fi
            if [[ -z $name ]]; then
                while :; do
                    ask name "Device name" "$(default_name)"
                    valid_name "$name" && break
                    warn "The device name must be 1-64 characters from A-Z a-z 0-9 _ . -"
                done
            fi
            [[ -n $dtype ]] || ask dtype "Device type ($DEVICE_TYPES)" "Server"
            if (( ! OPT_VOLUMES_SET )); then
                detected=$(detect_volumes "${CODE_SOURCE_DIR:-$SCRIPT_DIR/collector}")
                ask vols "Volumes to report capacity for (comma-separated, Enter = auto-detect${detected:+: $detected})"
                OPT_VOLUMES=${vols:-auto} OPT_VOLUMES_SET=1
            fi
            if (( ! OPT_SCAN_PATHS_SET )); then
                ask scan "Folders to measure (comma-separated, DIR/* = each subfolder, 'none')" \
                    "$(default_scan_paths)"
                OPT_SCAN_PATHS=$scan OPT_SCAN_PATHS_SET=1
            fi
        fi
        [[ -n $name ]] || name=$(default_name)
        if (( OPT_GENERATE_TOKEN )); then
            GENERATED_TOKEN=$(generate_token)
            token=$GENERATED_TOKEN
        fi
        [[ -n $url ]] || usage_error "--manager-url is required for a new install"
        [[ -n $token ]] || usage_error "a token is required for a new install (--token, --token-file or --generate-token)"
        if (( ! OPT_SCAN_PATHS_SET )); then
            OPT_SCAN_PATHS=$(default_scan_paths) OPT_SCAN_PATHS_SET=1
        fi
        LM_SCAN_PATHS=$OPT_SCAN_PATHS LM_SCAN_PATHS_SET=$OPT_SCAN_PATHS_SET
        LM_VOLUMES=$OPT_VOLUMES LM_VOLUMES_SET=$OPT_VOLUMES_SET
    fi
    if [[ -n $existing ]] && (( OPT_SCAN_PATHS_SET )); then
        LM_SCAN_PATHS=$OPT_SCAN_PATHS LM_SCAN_PATHS_SET=1
    fi
    if [[ -n $existing ]] && (( OPT_VOLUMES_SET )); then
        LM_VOLUMES=$OPT_VOLUMES LM_VOLUMES_SET=1
    fi

    if [[ -n $name ]] && ! valid_name "$name"; then
        usage_error "device name '$name' must be 1-64 characters from A-Z a-z 0-9 _ . - (use --name)"
    fi
    if [[ -n $url ]]; then
        valid_url "$url" || usage_error "the Manager URL must start with http:// or https:// (got: $url)"
        [[ $url == http://* ]] && warn "$url is plain http:// - the token travels unencrypted on the network."
    fi
    if [[ -n $token ]]; then
        problem=$(token_problem "$token")
        [[ -z $problem ]] || usage_error "$problem"
    fi
    [[ -z $dtype ]] || check_device_type "$dtype"

    LM_NAME=$name LM_MANAGER_URL=$url LM_MANAGER_TOKEN=$token LM_DEVICE_TYPE=$dtype
}

# ------------------------------------------------------------------------------
# --print-config
# ------------------------------------------------------------------------------
print_config() {
    local existing=""
    if [[ -f $CONFIG_FILE ]]; then
        if [[ -r $CONFIG_FILE ]]; then
            existing=$CONFIG_FILE
            info "# Based on the existing $CONFIG_FILE" >&2
        else
            warn "$CONFIG_FILE exists but is not readable by $(id -un); showing a new config instead"
        fi
    fi
    gather_settings "$existing"
    write_config - "$existing"
    if [[ -n $GENERATED_TOKEN ]]; then
        print_manager_token_entry "$LM_NAME" "$GENERATED_TOKEN" >&2
    fi
}

# ------------------------------------------------------------------------------
# Install steps
# ------------------------------------------------------------------------------
# apt_get ARGS...: apt-get that waits (up to APT_LOCK_WAIT s) while another
# package manager holds its lock. DPkg::Lock::Timeout covers the dpkg lock of
# "install"; "update" has no such option for the package-lists lock (checked
# with apt 2.8), so a lock error is retried here.
apt_get() {
    local deadline=$(( SECONDS + APT_LOCK_WAIT )) errfile rc said=0
    errfile=$(mktemp)
    while :; do
        rc=0
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout="$APT_LOCK_WAIT" "$@" \
            2>"$errfile" || rc=$?
        if (( rc == 0 || SECONDS >= deadline )) || \
                ! grep -qE 'Could not get lock|Unable to (lock|acquire)' "$errfile"; then
            break
        fi
        if (( ! said )); then
            info "Another package manager holds the apt lock (unattended-upgrades after boot?); waiting up to $(( APT_LOCK_WAIT / 60 )) min..."
            said=1
        fi
        sleep "$APT_RETRY_INTERVAL"
    done
    cat "$errfile" >&2
    rm -f -- "$errfile"
    return "$rc"
}

apt_install() {
    command -v apt-get >/dev/null 2>&1 || return 1
    if (( OPT_DRY_RUN )); then
        run apt-get install -y --no-install-recommends "$@"
        return 0
    fi
    apt_get install -y --no-install-recommends "$@" && return 0
    info "Refreshing the package lists and retrying..."
    apt_get update && apt_get install -y --no-install-recommends "$@"
}

preflight() {
    step "Step 1: Checking the system"
    if [[ ! -d /run/systemd/system ]]; then
        die "systemd is not running (container or WSL without systemd?); the collector is scheduled with systemd timers."
    fi
    local id="" id_like="" pretty=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        id=$(. /etc/os-release && echo "${ID:-}")
        # shellcheck disable=SC1091
        id_like=$(. /etc/os-release && echo "${ID_LIKE:-}")
        # shellcheck disable=SC1091
        pretty=$(. /etc/os-release && echo "${PRETTY_NAME:-}")
    fi
    case " $id $id_like " in
        *" ubuntu "*|*" debian "*) ok "${pretty:-$id}" ;;
        *) warn "Untested distribution '${pretty:-unknown}' (supported: Ubuntu 22.04/24.04, Debian 12)." ;;
    esac

    if ! command -v python3 >/dev/null 2>&1; then
        info "python3 not found; installing it with apt"
        apt_install python3 || die "Could not install python3"
        (( OPT_DRY_RUN )) && return 0
    fi
    if ! "${PY[@]}" -c "import sys; sys.exit(sys.version_info < tuple(map(int, '$REQUIRED_PYTHON'.split('.'))))"; then
        die "Python $REQUIRED_PYTHON or newer is required (found: $(python3 --version 2>&1))"
    fi
    ok "$(python3 --version 2>&1)"

    if ! "${PY[@]}" -c 'import ensurepip, venv' >/dev/null 2>&1; then
        info "python3-venv is missing (no ensurepip); installing it with apt"
        if apt_install python3-venv; then
            ok "python3-venv installed"
        else
            warn "Could not install python3-venv (offline, or apt still locked?). The venv will be created without pip and use the Debian packages python3-requests / python3-psutil instead."
        fi
    else
        ok "python3-venv available"
    fi
}

ensure_user() {
    step "Step 2: Service user '$SERVICE_USER'"
    if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
        run groupadd --system "$SERVICE_USER"
    fi
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        ok "User $SERVICE_USER exists"
    else
        run useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin --comment "Lab Monitor collector" "$SERVICE_USER"
        ok "Created system user $SERVICE_USER (no login shell, home $DATA_DIR)"
    fi
}

# Sets CODE_SOURCE (a checkout with collector/collector.py) and CODE_VERSION
resolve_source() {
    local tmp
    if [[ -n $OPT_REPO ]]; then
        if ! command -v git >/dev/null 2>&1; then
            info "git not found; installing it with apt"
            apt_install git || die "Could not install git (needed for --repo)"
        fi
        tmp=$(mktemp -d /tmp/lab-monitor-src.XXXXXX)
        TMP_DIRS+=("$tmp")
        info "Cloning $OPT_REPO${OPT_REF:+ ($OPT_REF)}"
        if (( OPT_DRY_RUN )); then
            run git clone --quiet "$OPT_REPO" "$tmp/src"
            [[ -z $OPT_REF ]] || run git -C "$tmp/src" checkout --quiet --detach "$OPT_REF"
            CODE_SOURCE=$SCRIPT_DIR
        else
            git clone --quiet "$OPT_REPO" "$tmp/src" || die "Could not clone $OPT_REPO"
            if [[ -n $OPT_REF ]]; then
                git -C "$tmp/src" checkout --quiet --detach "$OPT_REF" || die "Could not check out $OPT_REF"
            fi
            CODE_SOURCE="$tmp/src"
        fi
    elif [[ -n $OPT_SOURCE ]]; then
        CODE_SOURCE=$(cd "$OPT_SOURCE" && pwd) || die "--source $OPT_SOURCE is not a directory"
    else
        CODE_SOURCE=$SCRIPT_DIR
    fi
    [[ -f $CODE_SOURCE/collector/collector.py && -d $CODE_SOURCE/collector/systemd ]] || \
        die "No collector (collector/collector.py, collector/systemd/) under $CODE_SOURCE; run this script from a checkout, or use --source DIR or --repo $DEFAULT_REPO"
    CODE_SOURCE_DIR="$CODE_SOURCE/collector"
    if [[ $CODE_SOURCE == "$PREFIX" && -r $PREFIX/VERSION ]]; then
        CODE_VERSION=$(head -n 1 "$PREFIX/VERSION")    # re-run of the installed copy
    else
        # safe.directory: root reading a checkout owned by the operator
        CODE_VERSION=$(git -c safe.directory="$CODE_SOURCE" -C "$CODE_SOURCE" \
                       describe --always --dirty --tags 2>/dev/null || echo unknown)
    fi
}

install_code() {
    step "Step 3: Installing the collector code to $CODE_DIR"
    local staging="$PREFIX/.collector.new" old="$PREFIX/.collector.old"
    local installer_src="$CODE_SOURCE/install-collector-linux.sh"
    local installer_dst="$PREFIX/install-collector-linux.sh"

    run install -d -m 0755 -o root -g root "$PREFIX"
    run rm -rf -- "$staging" "$old"
    run cp -R -- "$CODE_SOURCE/collector" "$staging"
    # Developer leftovers: local configs (tokens!) and bytecode
    run rm -rf -- "$staging/local"
    run find "$staging" \( -name __pycache__ -prune -o -name '*.pyc' \) -exec rm -rf -- {} +
    # Root-owned and read-only for the service user: it can't modify its own code
    run chown -R root:root "$staging"
    run chmod -R u=rwX,go=rX "$staging"
    if [[ -d $CODE_DIR ]]; then
        run mv -- "$CODE_DIR" "$old"
    fi
    run mv -- "$staging" "$CODE_DIR"
    run rm -rf -- "$old"

    # Keep a copy of this installer for upgrades and --uninstall. Replaced by
    # rename, so a copy that is running right now keeps reading its own file.
    if [[ -f $installer_src && "$(realpath "$installer_src")" != "$(realpath -m "$installer_dst")" ]]; then
        run install -m 0755 -o root -g root "$installer_src" "$installer_dst.new"
        run mv -f -- "$installer_dst.new" "$installer_dst"
    fi
    printf '%s\n' "$CODE_VERSION" | write_file "$PREFIX/VERSION" 0644
    ok "Collector $CODE_VERSION installed from $CODE_SOURCE"
}

venv_healthy() {
    [[ -x $VENV_DIR/bin/python ]] || return 1
    local want have
    want=$("${PY[@]}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    have=$("$VENV_DIR/bin/python" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || return 1
    [[ $want == "$have" ]] || return 1
    # A venv made without pip (offline fallback) is rebuilt once pip can be had
    if ! "$VENV_DIR/bin/python" -I -m pip --version >/dev/null 2>&1 && \
            "${PY[@]}" -c 'import ensurepip' >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

setup_venv() {
    step "Step 4: Python environment $VENV_DIR"
    local vpy="$VENV_DIR/bin/python" pip_ok=0 reused=0
    local pip_args=(-m pip install --disable-pip-version-check --no-input --timeout 20 --retries 2)

    if venv_healthy; then
        reused=1
        ok "Reusing the existing venv"
    else
        [[ -e $VENV_DIR ]] && info "Rebuilding the venv (missing, broken, or made for another Python version)"
        run rm -rf -- "$VENV_DIR"
        if "${PY[@]}" -c 'import ensurepip' >/dev/null 2>&1 || (( OPT_DRY_RUN )); then
            run "${PY[@]}" -m venv "$VENV_DIR" || die "Could not create the venv $VENV_DIR"
        fi
    fi

    if (( OPT_DRY_RUN )); then
        run "$vpy" "${pip_args[@]}" -r "$CODE_DIR/requirements.txt"
        pip_ok=1
    elif [[ -x $vpy ]] && "$vpy" -I -m pip --version >/dev/null 2>&1; then
        info "Installing $CODE_DIR/requirements.txt"
        if "$vpy" -I "${pip_args[@]}" -q -r "$CODE_DIR/requirements.txt"; then
            pip_ok=1
            ok "requests and psutil installed (pinned versions)"
        fi
    fi

    if (( pip_ok )) && command -v nvidia-smi >/dev/null 2>&1; then
        info "NVIDIA GPU tools found; installing $CODE_DIR/requirements-gpu.txt"
        if run "$vpy" -I "${pip_args[@]}" -q -r "$CODE_DIR/requirements-gpu.txt"; then
            ok "nvidia-ml-py installed (GPU stats enabled)"
        else
            warn "Could not install nvidia-ml-py; GPU stats will be unavailable"
        fi
    fi

    if (( ! pip_ok && reused )) && "$vpy" -I -c 'import requests' 2>/dev/null; then
        # Upgrade while offline: what worked before keeps working
        warn "pip could not refresh the collector requirements (offline?); keeping the existing venv as it is."
        pip_ok=1
    fi
    if (( ! pip_ok )); then
        warn "pip could not install the collector requirements (offline, or no pip in the venv)."
        warn "Falling back to the Debian packages python3-requests and python3-psutil (distribution versions, not the pinned ones)."
        apt_install python3-requests python3-psutil || \
            warn "apt could not install them either (offline, or apt still locked?); the collector needs at least python3-requests"
        run rm -rf -- "$VENV_DIR"
        run "${PY[@]}" -m venv --system-site-packages --without-pip "$VENV_DIR" || \
            die "Could not create the venv $VENV_DIR"
    fi

    (( OPT_DRY_RUN )) && return 0
    "$vpy" -I -c 'import requests' 2>/dev/null || \
        die "The venv cannot import 'requests' (required). Fix network access to PyPI or install python3-requests, then re-run."
    if "$vpy" -I -c 'import psutil' 2>/dev/null; then
        ok "psutil available (full metrics, per-user stats)"
    else
        warn "psutil is not available: CPU/RAM/network come from /proc, and there are no per-user metrics."
    fi
    "$vpy" -I -m compileall -q "$CODE_DIR" >/dev/null 2>&1 || true
}

setup_dirs() {
    step "Step 5: Directories"
    run install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
    run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
    # Files left by a manual run as another user (e.g. sudo) must stay writable
    run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
    ok "$CONFIG_DIR (0750 root:$SERVICE_USER), $DATA_DIR and $LOG_DIR (0750 $SERVICE_USER)"
}

# Settings are gathered and validated before anything is changed.
# Sets CONFIG_EXISTING ("" for a new config) and CONFIG_CHANGED.
CONFIG_EXISTING=""
CONFIG_CHANGED=0
prepare_config() {
    step "Settings"
    CONFIG_EXISTING="" CONFIG_CHANGED=0
    if [[ -f $CONFIG_FILE ]]; then
        if [[ ! -r $CONFIG_FILE ]]; then
            # Only possible in --dry-run as a normal user
            info "Existing $CONFIG_FILE is kept (not readable by $(id -un), so not shown)"
            return 0
        fi
        "${PY[@]}" -c 'import json, sys; assert isinstance(json.load(open(sys.argv[1])), dict)' \
            "$CONFIG_FILE" 2>/dev/null || die "$CONFIG_FILE is not a valid JSON object; fix it (or move it away to start over)."
        CONFIG_EXISTING=$CONFIG_FILE
        info "Existing $CONFIG_FILE is kept (flags change only the keys they name)."
    fi
    gather_settings "$CONFIG_EXISTING"
    if [[ -z $CONFIG_EXISTING ]] || settings_flags_given || \
            [[ $LM_SCAN_PATHS_SET == 1 || $LM_VOLUMES_SET == 1 || -n $LM_MANAGER_URL || -n $LM_MANAGER_TOKEN ]]; then
        CONFIG_CHANGED=1
        # Validates the merged config (exit 2 on a bad value) without writing it
        write_config - "$CONFIG_EXISTING" >/dev/null
    fi
    ok "Settings valid"
}

configure() {
    step "Step 6: Configuration $CONFIG_FILE"
    local data_dir log_file
    if (( OPT_DRY_RUN )); then
        if (( CONFIG_CHANGED )); then
            info "[dry-run] would write $CONFIG_FILE (0640 root:$SERVICE_USER):"
            LM_REDACT=1 write_config - "$CONFIG_EXISTING" | sed 's/^/    | /'
        else
            info "[dry-run] $CONFIG_FILE unchanged"
        fi
        return 0
    fi
    if (( CONFIG_CHANGED )); then
        LM_GROUP=$SERVICE_USER write_config "$CONFIG_FILE" "$CONFIG_EXISTING" || die "Could not write $CONFIG_FILE"
        ok "Config written"
    else
        ok "Config unchanged"
    fi
    # The units run as $SERVICE_USER: it must read the config, nobody else may (token)
    chown "root:$SERVICE_USER" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    info "  - Name:        $(config_get "$CONFIG_FILE" name)"
    info "  - Manager URL: $(config_get "$CONFIG_FILE" manager_url)"
    info "  - Token:       (set)"
    info "  - Volumes:     $(config_get "$CONFIG_FILE" volumes | sed 's/^$/(auto-discovered)/')"
    info "  - Scan paths:  $(config_get "$CONFIG_FILE" scan_paths | sed 's/^$/(none: volume capacity only)/')"
    info "  - Permissions: 0640 root:$SERVICE_USER"

    data_dir=$(config_get "$CONFIG_FILE" data_dir)
    log_file=$(config_get "$CONFIG_FILE" log_file)
    case "$data_dir/" in
        "$DATA_DIR"/*) ;;
        *) warn "data_dir is $data_dir: the units may only write to $DATA_DIR and $LOG_DIR. Add it with 'systemctl edit lab-monitor-metrics.service lab-monitor-disk.service' (ReadWritePaths=$data_dir)." ;;
    esac
    case "$log_file" in
        "$LOG_DIR"/*|"") ;;
        *) warn "log_file is $log_file: the units may only write to $DATA_DIR and $LOG_DIR (add ReadWritePaths= in a drop-in)." ;;
    esac
}

install_units() {
    step "Step 7: systemd units"
    local unit seconds
    for unit in "${UNITS[@]}"; do
        run install -m 0644 -o root -g root "$CODE_DIR/systemd/$unit" "$UNIT_DIR/$unit"
    done
    if [[ -r $CONFIG_FILE ]]; then
        seconds=$(disk_timeout_seconds "$CONFIG_FILE")
    else
        seconds=14400
    fi
    run install -d -m 0755 -o root -g root "$DISK_DROPIN_DIR"
    write_file "$DISK_DROPIN" 0644 <<EOF
# Written by install-collector-linux.sh from $CONFIG_FILE: the collector's
# overall scan budget (total_timeout_seconds) + 3600 s for volume capacity and
# the upload. Re-run the installer after changing scan_paths or the timeouts.
[Service]
TimeoutStartSec=$seconds
EOF
    run systemctl daemon-reload
    run systemctl enable --now "${TIMERS[@]}"
    TIMERS_STOPPED=0
    ok "Units installed; timers enabled (disk unit TimeoutStartSec=${seconds}s)"
}

# Number of messages waiting in this device's queue (empty if unknown)
queue_length() {
    local data_dir name
    data_dir=$(config_get "$CONFIG_FILE" data_dir)
    name=$(config_get "$CONFIG_FILE" name)
    # Root reading a file in a directory labmon can write: never through a
    # symlink, and never blocking on a FIFO put in its place
    "${PY[@]}" -c '
import json, os, stat, sys
path = os.path.join(sys.argv[1], sys.argv[2], "queue.json")
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except FileNotFoundError:
    print(0)
    sys.exit(0)
with os.fdopen(fd, "rb") as f:
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        sys.exit(1)
    print(len(json.loads(f.read())))
' "${data_dir:-$DATA_DIR}" "$name" 2>/dev/null || true
}

test_run() {
    step "Step 8: Running one metrics collection now"
    if (( OPT_DRY_RUN )); then
        run systemctl start lab-monitor-metrics.service
        return 0
    fi
    local since status=0 result queued url
    url=$(config_get "$CONFIG_FILE" manager_url)
    if "$VENV_DIR/bin/python" -I -c '
import sys, requests
sys.exit(0 if requests.get(sys.argv[1].rstrip("/") + "/health", timeout=10).ok else 1)
' "$url" 2>/dev/null; then
        ok "Manager $url is reachable"
    else
        warn "Manager $url is not reachable from here (/health failed); data stays queued until it is."
    fi

    since=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl start lab-monitor-metrics.service || status=$?
    result=$(systemctl show -p Result --value lab-monitor-metrics.service 2>/dev/null || echo unknown)
    info ""
    info "Log of this run (journalctl -u lab-monitor-metrics.service):"
    journalctl -u lab-monitor-metrics.service --since "$since" --no-pager -o cat 2>/dev/null | tail -n 25 | sed 's/^/    /' || true
    queued=$(queue_length)
    info ""
    if (( status == 0 )) && [[ $result == success && ${queued:-1} == 0 ]]; then
        ok "Metrics collected and delivered: the Manager acknowledged everything (queue drained)."
    else
        warn "The test run did not deliver its data (result: $result, messages queued: ${queued:-unknown})."
        info "  It is kept in the queue and sent on the next successful run. Common causes:"
        info "  - HTTP 401: the Manager does not know this token yet (add the collector_tokens entry below, restart the Manager)"
        info "  - HTTP 403: the Manager's collector_tokens key for this token is not this device name"
        info "  - Network error: check manager_url and the firewall"
        info "  Retry with: sudo systemctl start lab-monitor-metrics.service"
    fi
}

summary() {
    local name
    if [[ -r $CONFIG_FILE ]]; then
        name=$(config_get "$CONFIG_FILE" name)
    else
        name=${LM_NAME:-<name>}
    fi
    echo ""
    echo "=========================================="
    echo "${GREEN}[OK] Linux Collector Installation Complete${NC}"
    echo "=========================================="
    echo ""
    echo "  Code:    $CODE_DIR (venv $VENV_DIR)"
    echo "  Config:  $CONFIG_FILE"
    echo "  Data:    $DATA_DIR/$name (archive + queue)"
    echo "  Logs:    $LOG_FILE and the journal"
    echo ""
    if (( ! OPT_DRY_RUN )); then
        systemctl list-timers --all --no-pager "${TIMERS[@]}" 2>/dev/null | sed 's/^/  /' || true
        echo ""
    fi
    echo "Useful commands:"
    echo "  systemctl list-timers 'lab-monitor-*'               # next runs"
    echo "  journalctl -u lab-monitor-metrics.service -f        # metrics log (every 5 min)"
    echo "  journalctl -u lab-monitor-disk.service              # disk scan log (daily 02:00)"
    echo "  tail -f $LOG_FILE"
    echo "  sudo systemctl start lab-monitor-metrics.service    # collect metrics now"
    echo "  sudo systemctl start --no-block lab-monitor-disk.service   # scan disks now"
    echo "  sudoedit $CONFIG_FILE                 # then re-run this installer if scan_paths/timeouts changed"
    echo "  sudo $PREFIX/install-collector-linux.sh --uninstall [--purge]"
    if [[ -n $GENERATED_TOKEN ]]; then
        echo ""
        echo "${BOLD}A new collector token was generated for '$name'.${NC}"
        print_manager_token_entry "$name" "$GENERATED_TOKEN"
    elif [[ -z $CONFIG_EXISTING ]]; then
        echo ""
        echo "The Manager config must list this device's token under its name:"
        echo "  \"collector_tokens\": {\"$name\": \"<the token you entered>\"}"
    fi
}

# One install/upgrade/uninstall at a time. The lock file is in /run (writable
# by root only), so no other user can create, hold or replace it.
take_install_lock() {
    exec 9>"$INSTALL_LOCK"
    flock -n 9 || die "Another run of this installer is in progress ($INSTALL_LOCK is locked)"
}

install_main() {
    if (( OPT_DRY_RUN )); then
        info "${BOLD}Dry run: nothing is changed; the commands are printed instead.${NC}"
    else
        require_root
        take_install_lock
    fi
    echo "================================================"
    echo "Lab Monitor - Linux Collector Install"
    echo "================================================"

    preflight
    resolve_source
    prepare_config
    ensure_user
    # Upgrade: no new runs while code and venv are replaced
    if [[ -f $UNIT_DIR/lab-monitor-metrics.timer ]]; then
        run systemctl stop "${TIMERS[@]}" || true
        (( OPT_DRY_RUN )) || TIMERS_STOPPED=1
        if systemctl is-active --quiet lab-monitor-disk.service 2>/dev/null; then
            warn "A disk scan is running; it finishes with the code it started with."
        fi
    fi
    install_code
    setup_venv
    setup_dirs
    configure
    install_units
    test_run
    summary
}

uninstall_main() {
    if (( ! OPT_DRY_RUN )); then
        require_root
        take_install_lock
    fi
    local what="units and $PREFIX"
    if (( OPT_PURGE )); then
        what+=", and DELETE $CONFIG_DIR, $DATA_DIR (the local archives), $LOG_DIR and the user $SERVICE_USER"
    fi
    confirm "Remove the Lab Monitor collector ($what)?" || { info "Nothing removed."; exit 0; }

    step "Removing the Lab Monitor collector"
    local unit
    run systemctl disable --now "${TIMERS[@]}" 2>/dev/null || true
    run systemctl stop "${SERVICES[@]}" 2>/dev/null || true
    for unit in "${UNITS[@]}"; do
        run rm -f -- "$UNIT_DIR/$unit"
    done
    run rm -f -- "$DISK_DROPIN"
    if [[ -d $DISK_DROPIN_DIR ]]; then
        run rmdir -- "$DISK_DROPIN_DIR" 2>/dev/null || warn "Kept $DISK_DROPIN_DIR (it has other drop-ins)"
    fi
    run systemctl daemon-reload || true
    run systemctl reset-failed "${SERVICES[@]}" 2>/dev/null || true
    run rm -rf -- "${PREFIX:?}"
    ok "Units and $PREFIX removed"

    if (( OPT_PURGE )); then
        run rm -rf -- "${CONFIG_DIR:?}" "${DATA_DIR:?}" "${LOG_DIR:?}"
        if id -u "$SERVICE_USER" >/dev/null 2>&1; then
            run userdel "$SERVICE_USER" || warn "Could not remove the user $SERVICE_USER"
        fi
        if getent group "$SERVICE_USER" >/dev/null 2>&1; then
            run groupdel "$SERVICE_USER" 2>/dev/null || true
        fi
        ok "Config, data, logs and the user $SERVICE_USER removed"
    else
        info "Kept: $CONFIG_FILE, $DATA_DIR (archives and queue), $LOG_DIR and the user $SERVICE_USER."
        info "Remove them too with: --uninstall --purge"
    fi
}

main() {
    parse_args "$@"
    if (( OPT_PRINT_CONFIG )); then
        print_config
    elif (( OPT_UNINSTALL )); then
        uninstall_main
    else
        install_main
    fi
}

# Run only when executed (the tests source this file to call its functions)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
    exit $?
fi
