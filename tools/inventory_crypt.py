#!/usr/bin/env python3
"""
Lab Monitor - keep collectors.csv encrypted with a passphrase.

collectors.csv holds SSH logins (and possibly passwords) for every system. This tool keeps it
as an encrypted file, collectors.csv.enc, that the fleet tools read directly: pass
--csv collectors.csv.enc and they ask for the passphrase and decrypt it in memory only.

Commands:
    encrypt  collectors.csv [-o collectors.csv.enc]   one-time switch (keeps the plain file;
                                                      delete it yourself afterwards)
    show     collectors.csv.enc                       print the table, passwords hidden
    edit     collectors.csv.enc [--editor PATH]       decrypt to a private temporary copy, edit it,
                                                      check it, encrypt again (Windows only)
    set-password collectors.csv.enc --only HOST       change one host's password at a hidden prompt
    passphrase   collectors.csv.enc                   change the passphrase
    decrypt  collectors.csv.enc -o plain.csv          write a plain copy (escape hatch, asks first)

The passphrase is only ever read from a hidden prompt: never from the command line, a file or an
environment variable. A forgotten passphrase can't be recovered.

Saving (edit, set-password, passphrase) keeps the previous encrypted file as <file>.bak and
replaces the file in one step, so an interruption leaves either the old or the new version.

File format: an 8-byte magic 'LMINV1\\n\\0', then a JSON header line (scrypt parameters, salt and
nonce), then AES-256-GCM ciphertext of the UTF-8 CSV. The header is authenticated as associated
data, so a changed header, a wrong passphrase and a damaged file all fail the same way.

Exit codes: 0 done, 1 refused or failed (wrong passphrase, bad file, cancelled).

Requires the 'cryptography' package (already installed with paramiko).
"""

import argparse
import base64
import csv
import getpass
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAGIC = b'LMINV1\n\0'
FORMAT_VERSION = 1
# scrypt cost: 2**17 * 8 * 128 bytes = 128 MiB and about half a second per unlock.
SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P = 17, 8, 1
MIN_PASSPHRASE = 10
REQUIRED_COLUMNS = ('ip', 'username')
MASK = '****'


class InventoryError(Exception):
    """A problem with the inventory file or its contents; the message is safe to print."""


# Prompts, replaceable in tests. The passphrase and passwords only ever come from _getpass.
_getpass = getpass.getpass
_input = input


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise InventoryError("the 'cryptography' package is required: pip install -r requirements.txt")
    return AESGCM, Scrypt


def _derive_key(passphrase: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    _, Scrypt = _crypto()
    if not (14 <= n_log2 <= 22 and 1 <= r <= 32 and 1 <= p <= 16):
        raise InventoryError('unsupported encryption parameters in the file header')
    return Scrypt(salt=salt, length=32, n=2 ** n_log2, r=r, p=p).derive(passphrase.encode('utf-8'))


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_bytes(plaintext: bytes, passphrase: str, n_log2: int = None) -> bytes:
    AESGCM, _ = _crypto()
    n_log2 = SCRYPT_N_LOG2 if n_log2 is None else n_log2
    salt, nonce = os.urandom(16), os.urandom(12)
    header = json.dumps({'v': FORMAT_VERSION, 'kdf': 'scrypt', 'n_log2': n_log2, 'r': SCRYPT_R,
                         'p': SCRYPT_P, 'salt': base64.b64encode(salt).decode('ascii'),
                         'cipher': 'aes-256-gcm', 'nonce': base64.b64encode(nonce).decode('ascii')},
                        sort_keys=True).encode('ascii') + b'\n'
    key = _derive_key(passphrase, salt, n_log2, SCRYPT_R, SCRYPT_P)
    return MAGIC + header + AESGCM(key).encrypt(nonce, plaintext, MAGIC + header)


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    AESGCM, _ = _crypto()
    if not is_encrypted(data):
        raise InventoryError('not an encrypted inventory file')
    end = data.find(b'\n', len(MAGIC))
    if end < 0:
        raise InventoryError('damaged file: no header')
    header_bytes = data[len(MAGIC):end + 1]
    try:
        h = json.loads(header_bytes)
        if h.get('v') != FORMAT_VERSION or h.get('kdf') != 'scrypt' or h.get('cipher') != 'aes-256-gcm':
            raise InventoryError(f"unsupported file version or cipher (v={h.get('v')})")
        salt, nonce = base64.b64decode(h['salt']), base64.b64decode(h['nonce'])
        n_log2, r, p = int(h['n_log2']), int(h['r']), int(h['p'])
    except InventoryError:
        raise
    except (ValueError, KeyError, TypeError):
        raise InventoryError('damaged file: unreadable header')
    key = _derive_key(passphrase, salt, n_log2, r, p)
    try:
        from cryptography.exceptions import InvalidTag
        return AESGCM(key).decrypt(nonce, data[end + 1:], MAGIC + header_bytes)
    except InvalidTag:
        raise InventoryError('wrong passphrase, or the file is damaged')


# ---------------------------------------------------------------------------
# CSV text <-> rows
# ---------------------------------------------------------------------------

def parse_csv_text(text: str) -> tuple:
    """(rows, fieldnames) as _ssh.read_inventory returns them: headers stripped, missing cells ''."""
    if text.startswith('﻿'):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text, newline=''))
    fieldnames = [(n or '').strip() for n in (reader.fieldnames or [])]
    reader.fieldnames = fieldnames
    rows = [{k: (v if v is not None else '') for k, v in row.items() if k is not None} for row in reader]
    return rows, fieldnames


def rows_to_csv_text(rows: list, fieldnames: list) -> str:
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=fieldnames, lineterminator='\r\n', extrasaction='ignore')
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k, '') for k in fieldnames})
    return out.getvalue()


def validate(rows: list, fieldnames: list) -> list:
    """Problems that would make the tools misbehave; [] when the inventory is fine."""
    problems = []
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        problems.append(f"missing column(s): {', '.join(missing)}")
    dup_cols = sorted({c for c in fieldnames if fieldnames.count(c) > 1})
    if dup_cols:
        problems.append(f"column(s) listed twice: {', '.join(dup_cols)}")
    if '' in fieldnames:
        problems.append('a column has no name (an extra comma in the header line?)')
    if not rows:
        problems.append('no systems listed')
    seen = {}
    for i, row in enumerate(rows, start=2):
        ip = row.get('ip', '').strip()
        if not ip:
            problems.append(f'line {i}: empty ip')
        elif ip.lower() in seen:
            problems.append(f'line {i}: {ip} is already listed on line {seen[ip.lower()]}')
        else:
            seen[ip.lower()] = i
        if 'username' in fieldnames and not row.get('username', '').strip():
            problems.append(f"line {i}: {ip or '?'} has an empty username")
    return problems


def describe_changes(old_rows: list, old_fields: list, new_rows: list, new_fields: list) -> list:
    """Changes by host, never showing a password: what was added, removed or changed."""
    changes = []
    added_cols = [c for c in new_fields if c not in old_fields]
    removed_cols = [c for c in old_fields if c not in new_fields]
    if added_cols:
        changes.append(f"  + column(s) {', '.join(added_cols)}")
    if removed_cols:
        changes.append(f"  - column(s) {', '.join(removed_cols)}")
    old = {r.get('ip', '').strip().lower(): r for r in old_rows}
    new = {r.get('ip', '').strip().lower(): r for r in new_rows}
    for key, r in new.items():
        if key not in old:
            pw = 'password set' if r.get('password', '').strip() else 'no password'
            changes.append(f"  + new host   {r.get('ip', '').strip()}  (user {r.get('username', '').strip()}, {pw})")
    for key, r in new.items():
        if key in old:
            o = old[key]
            diff = [c for c in sorted(set(old_fields) | set(new_fields))
                    if c in old_fields and c in new_fields and o.get(c, '') != r.get(c, '')]
            if diff:
                changes.append(f"  ~ changed    {r.get('ip', '').strip()}  {', '.join(diff)}")
    for key, r in old.items():
        if key not in new:
            changes.append(f"  - removed    {r.get('ip', '').strip()}")
    if not changes and [r.get('ip', '').strip().lower() for r in old_rows] != list(new):
        changes.append('  ~ order of the hosts')
    return changes


def masked_table(rows: list, fieldnames: list) -> str:
    shown = [{c: (MASK if c == 'password' and r.get(c, '').strip() else r.get(c, '')) for c in fieldnames}
             for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in shown]) for c in fieldnames}
    lines = ['  '.join(c.ljust(widths[c]) for c in fieldnames).rstrip(),
             '  '.join('-' * widths[c] for c in fieldnames)]
    lines += ['  '.join(r[c].ljust(widths[c]) for c in fieldnames).rstrip() for r in shown]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def ask_passphrase(path, confirm: bool = False, prompt: str = None) -> str:
    """Read a passphrase at a hidden prompt (twice when confirm=True, for a new one)."""
    name = Path(path).name
    p1 = _getpass(prompt or f'Passphrase for {name}: ')
    if confirm:
        if len(p1) < MIN_PASSPHRASE:
            raise InventoryError(f'the passphrase must be at least {MIN_PASSPHRASE} characters')
        if _getpass('Type it again: ') != p1:
            raise InventoryError('the two passphrases differ')
    return p1


def load(path, passphrase: str = None) -> tuple:
    """Decrypt an inventory file: (rows, fieldnames, passphrase). Asks for the passphrase if None."""
    data = Path(path).read_bytes()
    if not is_encrypted(data):
        raise InventoryError(f'{path} is not an encrypted inventory file')
    if passphrase is None:
        passphrase = ask_passphrase(path)
    plain = decrypt_bytes(data, passphrase)
    try:
        text = plain.decode('utf-8')
    except UnicodeDecodeError:
        raise InventoryError('decrypted contents are not UTF-8 text')
    rows, fieldnames = parse_csv_text(text)
    return rows, fieldnames, passphrase


def save(path, text: str, passphrase: str, keep_backup: bool = True):
    """Encrypt and write in one step: the old file is kept as <path>.bak, then replaced atomically."""
    path = Path(path)
    data = encrypt_bytes(text.encode('utf-8'), passphrase)
    if decrypt_bytes(data, passphrase).decode('utf-8') != text:
        raise InventoryError('encryption check failed; nothing written')
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if keep_backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + '.bak'))
    os.replace(tmp, path)


def wipe(path):
    """Overwrite a file with zeros, then delete it. Best effort: SSDs may keep old blocks."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def edit_dir() -> Path:
    """Private folder for the temporary plain copy: in the user's local profile, never synced."""
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise InventoryError('LOCALAPPDATA is not set; edit needs a private local folder (Windows only)')
    return Path(base) / 'lab-monitor' / 'inventory-edit'


def _launch_editor(editor: str, path: Path):
    """Open the file without waiting: many editors hand off to a running window and return at once."""
    exe = shutil.which(editor) or editor
    subprocess.Popen([exe, str(path)])


def _yes(prompt: str) -> bool:
    return _input(prompt).strip().lower() in ('y', 'yes')


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_encrypt(args) -> int:
    src = Path(args.file)
    dest = Path(args.output) if args.output else src.with_name(src.name + '.enc')
    raw = src.read_bytes()
    if is_encrypted(raw):
        raise InventoryError(f'{src} is already encrypted')
    if dest.exists():
        raise InventoryError(f'{dest} already exists; move it away or choose another name with -o')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise InventoryError(f'{src} is not UTF-8 text; save it as UTF-8 (Notepad: Save As, Encoding UTF-8)')
    rows, fieldnames = parse_csv_text(text)
    problems = validate(rows, fieldnames)
    if problems:
        raise InventoryError('the CSV has problems:\n    ' + '\n    '.join(problems))
    passphrase = ask_passphrase(dest, confirm=True, prompt=f'New passphrase for {dest.name}: ')
    save(dest, rows_to_csv_text(rows, fieldnames), passphrase, keep_backup=False)
    load(dest, passphrase)  # read back from disk
    print(f'Encrypted {len(rows)} systems into {dest}.')
    print(f'Use it with: --csv {dest}')
    print(f'Next: delete {src} yourself. If it was in Dropbox (or another synced folder), also delete')
    print('      its earlier versions there, and change the passwords that were in it.')
    return 0


def cmd_show(args) -> int:
    rows, fieldnames, _ = load(args.file)
    print(masked_table(rows, fieldnames))
    print(f'\n{len(rows)} systems.')
    return 0


def _commit(path, old_rows, old_fields, new_rows, new_fields, passphrase) -> int:
    changes = describe_changes(old_rows, old_fields, new_rows, new_fields)
    if not changes:
        print('No changes; nothing saved.')
        return 0
    print('Changes:')
    print('\n'.join(changes))
    if not _yes('Save these changes? [y/N]: '):
        print('Not saved.')
        return 1
    save(path, rows_to_csv_text(new_rows, new_fields), passphrase)
    print(f'Saved {path} (previous version kept as {Path(path).name}.bak).')
    return 0


def cmd_edit(args) -> int:
    if os.name != 'nt':
        raise InventoryError('edit is for Windows only')
    path = Path(args.file)
    folder = edit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    leftovers = sorted(folder.glob('*.csv'))
    if leftovers:
        print(f'A temporary plain copy from an earlier edit is still in {folder}:')
        for p in leftovers:
            print(f'    {p.name}')
        if _yes('Delete it now? [y/N]: '):
            for p in leftovers:
                wipe(p)
        else:
            raise InventoryError('delete or keep it first; edit stops here')
    old_rows, old_fields, passphrase = load(path)
    tmp = folder / f'{path.stem}-edit-{os.getpid()}.csv'
    try:
        tmp.write_text(rows_to_csv_text(old_rows, old_fields), encoding='utf-8', newline='')
        while True:
            _launch_editor(args.editor, tmp)
            print(f'Opened a temporary copy in {args.editor}. Save it and close the editor, then press Enter here.')
            _input('')
            try:
                text = tmp.read_bytes().decode('utf-8-sig')
            except UnicodeDecodeError:
                problems = ['the file is not UTF-8 text any more (save it with Encoding: UTF-8)']
            else:
                new_rows, new_fields = parse_csv_text(text)
                problems = validate(new_rows, new_fields)
            if not problems:
                break
            print('The edited file has problems:')
            print('    ' + '\n    '.join(problems))
            if not _yes('Open it again to fix them? (no = discard all changes) [y/N]: '):
                print('Discarded; nothing saved.')
                return 1
        return _commit(path, old_rows, old_fields, new_rows, new_fields, passphrase)
    finally:
        wipe(tmp)
        print('Temporary copy deleted.')


def cmd_set_password(args) -> int:
    import _ssh  # select_rows: the same host matching as --only in the fleet tools
    path = Path(args.file)
    rows, fieldnames, passphrase = load(path)
    selected, unmatched = _ssh.select_rows(rows, args.only)
    if unmatched:
        raise InventoryError(f"--only matched no system: {', '.join(unmatched)}")
    new_fields = list(fieldnames) if 'password' in fieldnames else list(fieldnames) + ['password']
    new_rows = [dict(r) for r in rows]
    chosen = {id(r) for r in selected}
    for old, new in zip(rows, new_rows):
        if id(old) not in chosen:
            continue
        host = f"{old.get('username', '').strip()}@{old.get('ip', '').strip()}"
        pw = _getpass(f'New password for {host} (Enter alone = no password, use keys): ')
        if pw and _getpass('Type it again: ') != pw:
            raise InventoryError(f'the two passwords for {host} differ; nothing saved')
        new['password'] = pw
    return _commit(path, rows, fieldnames, new_rows, new_fields, passphrase)


def cmd_passphrase(args) -> int:
    path = Path(args.file)
    rows, fieldnames, _ = load(path, ask_passphrase(path, prompt=f'Current passphrase for {path.name}: '))
    new = ask_passphrase(path, confirm=True, prompt=f'New passphrase for {path.name}: ')
    save(path, rows_to_csv_text(rows, fieldnames), new)
    print(f'Passphrase changed. The previous file, still on the OLD passphrase, is {path.name}.bak;')
    print('delete it once the new passphrase works.')
    return 0


def cmd_decrypt(args) -> int:
    dest = Path(args.output)
    if dest.exists():
        raise InventoryError(f'{dest} already exists')
    rows, fieldnames, _ = load(args.file)
    print(f'This writes every login and password in plain text to {dest.resolve()}.')
    if not _yes('Continue? [y/N]: '):
        print('Nothing written.')
        return 1
    with open(dest, 'x', encoding='utf-8', newline='') as f:
        f.write(rows_to_csv_text(rows, fieldnames))
    print(f'Wrote {dest}. Delete it when done (and from any synced folder\'s history).')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Keep collectors.csv encrypted. The passphrase is only read from a hidden prompt.')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('encrypt', help='encrypt a plain collectors.csv (keeps the plain file)')
    p.add_argument('file')
    p.add_argument('-o', '--output', help='encrypted file to write (default: <file>.enc)')
    p.set_defaults(func=cmd_encrypt)
    p = sub.add_parser('show', help='print the inventory, passwords hidden')
    p.add_argument('file')
    p.set_defaults(func=cmd_show)
    p = sub.add_parser('edit', help='edit in a temporary private copy, then encrypt again (Windows)')
    p.add_argument('file')
    p.add_argument('--editor', default='notepad.exe', help='editor to open (default notepad.exe; not Excel)')
    p.set_defaults(func=cmd_edit)
    p = sub.add_parser('set-password', help="change hosts' passwords at a hidden prompt")
    p.add_argument('file')
    p.add_argument('--only', action='append', required=True, metavar='HOST',
                   help='host (as in the ip column, or its first name label); repeat for more')
    p.set_defaults(func=cmd_set_password)
    p = sub.add_parser('passphrase', help='change the passphrase')
    p.add_argument('file')
    p.set_defaults(func=cmd_passphrase)
    p = sub.add_parser('decrypt', help='write a plain copy (asks first)')
    p.add_argument('file')
    p.add_argument('-o', '--output', required=True, help='plain CSV to write (must not exist)')
    p.set_defaults(func=cmd_decrypt)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InventoryError as e:
        print(f'ERROR: {e}')
        return 1
    except FileNotFoundError as e:
        print(f'ERROR: file not found: {e.filename}')
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
