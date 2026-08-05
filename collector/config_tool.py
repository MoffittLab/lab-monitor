#!/usr/bin/env python3
"""
Lab Monitor Collector — Config Tool

Read and write collector config.json fields from the command line.
Designed to be called remotely via paramiko for fleet-wide management.

Usage:
    # Read a single field
    python config_tool.py --config config.json get active
    → false

    # Write a field (adds it if missing, updates if present)
    python config_tool.py --config config.json set active false

    # Dump all fields (auth tokens masked by default)
    python config_tool.py --config config.json list
    python config_tool.py --config config.json list --show-secrets

    # Structured JSON output for programmatic use (e.g. paramiko)
    python config_tool.py --config config.json get active --json
    python config_tool.py --config config.json set active true --json

Exit codes:
    0  Success
    1  Error (bad args, file not found, parse failure, etc.)
    2  Field not found (get only)
"""

import sys
import os
import json
import argparse
import tempfile

# Fields that contain secrets — masked in 'list' unless --show-secrets
SECRET_FIELDS = {'manager_token', 'auth_token', 'token', 'password', 'secret'}


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Load config.json; raises on missing or unparseable file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, 'r') as f:
        return json.load(f)


def save_config(path: str, config: dict):
    """Atomically write config back — temp file + rename to avoid corruption."""
    dir_ = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=2)
            f.write('\n')
        os.replace(tmp, path)   # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

def coerce(value: str):
    """
    Convert a CLI string to the appropriate Python type:
      'true' / 'false'  → bool
      integer string    → int
      float string      → float
      anything else     → str
    """
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def out_plain(value):
    """Print a value in human-readable form."""
    if isinstance(value, bool):
        print(str(value).lower())
    elif value is None:
        print('null')
    else:
        print(value)


def out_json(obj):
    """Print a dict as compact JSON."""
    print(json.dumps(obj, separators=(',', ':')))


def mask_secrets(config: dict) -> dict:
    """Return a copy of config with secret fields replaced by '***'."""
    masked = {}
    for k, v in config.items():
        if any(s in k.lower() for s in SECRET_FIELDS):
            masked[k] = '***'
        else:
            masked[k] = v
    return masked


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_get(config: dict, field: str, as_json: bool) -> int:
    """Print the value of a single config field."""
    if field not in config:
        if as_json:
            out_json({'status': 'error', 'error': f"Field '{field}' not found"})
        else:
            print(f"Field '{field}' not found", file=sys.stderr)
        return 2

    value = config[field]
    if as_json:
        out_json({'status': 'ok', 'field': field, 'value': value})
    else:
        out_plain(value)
    return 0


def cmd_set(config: dict, field: str, raw_value: str, config_path: str, as_json: bool) -> int:
    """Set a config field (add if missing) and write back atomically."""
    new_value = coerce(raw_value)
    old_value = config.get(field, '__missing__')

    config[field] = new_value
    save_config(config_path, config)

    if as_json:
        payload = {'status': 'ok', 'field': field, 'new': new_value}
        if old_value != '__missing__':
            payload['old'] = old_value
        out_json(payload)
    else:
        if old_value == '__missing__':
            print(f"Added '{field}': {json.dumps(new_value)}")
        else:
            print(f"Updated '{field}': {json.dumps(old_value)} → {json.dumps(new_value)}")
    return 0


def cmd_list(config: dict, show_secrets: bool, as_json: bool) -> int:
    """Dump all config fields."""
    display = config if show_secrets else mask_secrets(config)

    if as_json:
        out_json({'status': 'ok', 'config': display})
    else:
        for k, v in display.items():
            print(f"{k}: {json.dumps(v)}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Read and write Lab Monitor collector config.json',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--config', required=True, metavar='PATH',
                        help='Path to config.json')
    parser.add_argument('--json', action='store_true',
                        help='Emit structured JSON output (for programmatic use)')
    parser.add_argument('--show-secrets', action='store_true',
                        help='Include auth tokens in list output (default: masked)')

    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    sub.required = True

    p_get = sub.add_parser('get', help='Read a config field')
    p_get.add_argument('field', help='Field name')
    p_get.add_argument('--json', action='store_true', help='JSON output')

    p_set = sub.add_parser('set', help='Write a config field (adds if missing)')
    p_set.add_argument('field', help='Field name')
    p_set.add_argument('value', help='New value (true/false coerced to bool, numbers to int/float)')
    p_set.add_argument('--json', action='store_true', help='JSON output')

    p_list = sub.add_parser('list', help='List all config fields')
    p_list.add_argument('--show-secrets', action='store_true', help='Reveal masked fields')
    p_list.add_argument('--json', action='store_true', help='JSON output')

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        if args.json:
            out_json({'status': 'error', 'error': str(e)})
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        if args.json:
            out_json({'status': 'error', 'error': f"JSON parse error: {e}"})
        else:
            print(f"ERROR: JSON parse error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == 'get':
            code = cmd_get(config, args.field, args.json)
        elif args.command == 'set':
            code = cmd_set(config, args.field, args.value, args.config, args.json)
        elif args.command == 'list':
            code = cmd_list(config, args.show_secrets, args.json)
        else:
            code = 1
    except Exception as e:
        if args.json:
            out_json({'status': 'error', 'error': str(e)})
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(code)


if __name__ == '__main__':
    main()
