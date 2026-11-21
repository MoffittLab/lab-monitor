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

    # Keys of a nested block are written block.key; values starting with
    # [ { or " (and null) are read as JSON
    python config_tool.py --config config.json set sessions.timeout_seconds 30
    python config_tool.py --config config.json set disk_io_exclude '["sdz", "usb*"]'

    # Check the values of the keys the collector validates (lib/settings.py)
    python config_tool.py --config config.json check

Validation: `set` of a key the collector validates (perf_counters,
user_metrics, disk_io_exclude, sessions.timeout_seconds, ...) with a value
the collector would reject stores the key's default instead, with a warning,
just as the collector itself would fall back to it (for sessions.ports /
extra_ports / exclude_clients: the valid entries). Setting a whole block
(sessions, storage_health, storage_breakdown) checks each key in it the same
way; a key the collector doesn't know is stored but warned about.

Exit codes:
    0  Success
    1  Error (bad args, file not found, parse failure, etc.)
    2  Field not found (get only)
    3  Invalid values found (check only)
"""

import sys
import os
import json
import argparse
import tempfile

# The collector's lib/ (for lib/settings.py), appended like collector.py does
_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOL_DIR not in sys.path:
    sys.path.append(_TOOL_DIR)

from lib import settings  # noqa: E402

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
      '[...]', '{...}', '"..."', 'null' → parsed as JSON (lists, nested blocks)
      anything else     → str
    """
    stripped = value.strip()
    if stripped == 'null' or stripped[:1] in ('[', '{', '"'):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
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
    elif isinstance(value, (dict, list)):
        print(json.dumps(value))
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


_MISSING = object()


def get_path(config: dict, field: str):
    """The value of field ('block.key' looks inside a nested block), or _MISSING."""
    if field in config:
        return config[field]
    node = config
    for part in field.split('.'):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def set_path(config: dict, field: str, value):
    """Set field, creating nested blocks (also for a null one) for 'block.key'; ValueError if a block isn't an object."""
    if '.' not in field or field in config:
        config[field] = value
        return
    parts = field.split('.')
    node = config
    for i, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:            # "sessions": null is a missing block, as the collector reads it
            child = node[part] = {}
        if not isinstance(child, dict):
            raise ValueError(f"'{'.'.join(parts[:i + 1])}' is not a block (JSON object)")
        node = child
    node[parts[-1]] = value


def _invalid(field: str, value, checked) -> str:
    if checked == settings.SCHEMA[field][1]:
        return (f"Invalid {field}={value!r} (the collector would ignore it); "
                f"storing the default {checked!r} instead")
    return (f"Invalid {field}={value!r} (the collector would drop the invalid entries); "
            f"storing {checked!r} instead")


def _unknown(field: str) -> str:
    return f"Unknown key {field}: this collector ignores it"


def _in_block(field: str) -> bool:
    return '.' in field and field.split('.', 1)[0] in settings.BLOCKS


def validate(field: str, value):
    """
    (value to store, [warnings]): a value the collector would reject
    becomes its default; a whole block has each of its keys checked.
    """
    if field in settings.BLOCKS:
        if value is None:
            return value, []
        if not isinstance(value, dict):
            return {}, [f"Invalid {field}={value!r} (must be a JSON object; the collector would ignore it); "
                        f"storing {{}} (all defaults) instead"]
        stored, warnings = dict(value), []
        for key, raw in value.items():
            sub = f"{field}.{key}"
            if sub not in settings.SCHEMA:
                warnings.append(_unknown(sub))
                continue
            ok, checked = settings.check(sub, raw)
            if not ok:
                stored[key] = checked
                warnings.append(_invalid(sub, raw, checked))
        return stored, warnings
    if field not in settings.SCHEMA:
        return value, [_unknown(field)] if _in_block(field) else []
    ok, checked = settings.check(field, value)
    if ok:
        return value, []
    return checked, [_invalid(field, value, checked)]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_get(config: dict, field: str, as_json: bool) -> int:
    """Print the value of a single config field."""
    value = get_path(config, field)
    if value is _MISSING:
        if as_json:
            out_json({'status': 'error', 'error': f"Field '{field}' not found"})
        else:
            print(f"Field '{field}' not found", file=sys.stderr)
        return 2

    if as_json:
        out_json({'status': 'ok', 'field': field, 'value': value})
    else:
        out_plain(value)
    return 0


def cmd_set(config: dict, field: str, raw_value: str, config_path: str, as_json: bool) -> int:
    """Set a config field (add if missing) and write back atomically."""
    new_value, warnings = validate(field, coerce(raw_value))
    old_value = get_path(config, field)
    if old_value is _MISSING:
        old_value = '__missing__'

    set_path(config, field, new_value)
    save_config(config_path, config)

    if as_json:
        payload = {'status': 'ok', 'field': field, 'new': new_value}
        if old_value != '__missing__':
            payload['old'] = old_value
        if warnings:
            payload['warning'] = '; '.join(warnings)
        out_json(payload)
    else:
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        if old_value == '__missing__':
            print(f"Added '{field}': {json.dumps(new_value)}")
        else:
            print(f"Updated '{field}': {json.dumps(old_value)} → {json.dumps(new_value)}")
    return 0


def cmd_check(config: dict, as_json: bool) -> int:
    """
    Report every validated key whose value the collector would replace by
    its default, blocks that are not JSON objects, and keys inside a block
    the collector doesn't know (typos it would silently ignore).
    """
    problems = []
    for block in settings.BLOCKS:
        value = config.get(block)
        if value is not None and not isinstance(value, dict):
            problems.append({'field': block, 'value': value, 'default': {}})
        for key in sorted(value) if isinstance(value, dict) else ():
            if f"{block}.{key}" not in settings.SCHEMA:
                problems.append({'field': f"{block}.{key}", 'value': value[key], 'default': None,
                                 'unknown': True})
    for field in sorted(settings.SCHEMA):
        raw = settings.raw_value(config, field)
        ok, default = settings.check(field, raw)
        if not ok:
            problems.append({'field': field, 'value': raw, 'default': default})
    if as_json:
        out_json({'status': 'ok' if not problems else 'invalid', 'invalid': problems})
    elif problems:
        for p in problems:
            if p.get('unknown'):
                print(f"Unknown {p['field']}={p['value']!r}: the collector ignores it")
            else:
                print(f"Invalid {p['field']}={p['value']!r}: the collector uses {p['default']!r}")
    else:
        print("All validated settings are valid")
    return 3 if problems else 0


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

    p_check = sub.add_parser('check', help="Check the values the collector validates")
    p_check.add_argument('--json', action='store_true', help='JSON output')

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
        elif args.command == 'check':
            code = cmd_check(config, args.json)
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
