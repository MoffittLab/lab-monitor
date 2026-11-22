"""tools/inventory_crypt.py: encrypted collectors.csv, and the fleet tools reading it."""
import os
from pathlib import Path

import pytest

import _ssh
import inventory_crypt as ic

PASS = 'correct horse battery'
SECRET_A = 'pw-alpha-7731'
SECRET_B = 'pw-bravo-0042'
PLAIN = (
    'ip,username,password,git_path,python_path,key_filename\r\n'
    f'nas1.example.org,alice,{SECRET_A},/volume1/lab-monitor/scripts/lab-monitor,/usr/bin/python3,\r\n'
    f'192.0.2.10,bob,{SECRET_B},/opt/lab-monitor,/opt/env/bin/python,\r\n'
    'win1.example.org,carol,,C:/lab-monitor,C:/env/python.exe,~/.ssh/id_ed25519\r\n'
)


class Prompts:
    """Scripted answers for the hidden prompts and the y/N questions."""

    def __init__(self, monkeypatch, secrets=(), answers=()):
        self.secrets, self.answers, self.asked = list(secrets), list(answers), []
        monkeypatch.setattr(ic, '_getpass', self._getpass)
        monkeypatch.setattr(ic, '_input', self._input)

    def _getpass(self, prompt=''):
        self.asked.append(prompt)
        assert self.secrets, f'unexpected hidden prompt: {prompt}'
        return self.secrets.pop(0)

    def _input(self, prompt=''):
        self.asked.append(prompt)
        assert self.answers, f'unexpected question: {prompt}'
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(ic, 'SCRYPT_N_LOG2', 14)


@pytest.fixture
def enc(tmp_path):
    path = tmp_path / 'collectors.csv.enc'
    path.write_bytes(ic.encrypt_bytes(PLAIN.encode('utf-8'), PASS))
    return path


@pytest.fixture
def editdir(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    return tmp_path / 'localappdata' / 'lab-monitor' / 'inventory-edit'


def fake_editor(monkeypatch, *edits):
    """Each launch applies the next edit (a function text -> text) to the temporary copy."""
    edits = list(edits)
    seen = []

    def launch(editor, path):
        seen.append(Path(path))
        path.write_text(edits.pop(0)(path.read_text(encoding='utf-8')), encoding='utf-8', newline='')
    monkeypatch.setattr(ic, '_launch_editor', launch)
    return seen


# --- format ---------------------------------------------------------------

def test_round_trip_and_wrong_passphrase():
    data = ic.encrypt_bytes(b'ip,username\r\nh,u\r\n', PASS)
    assert ic.is_encrypted(data) and b'username' not in data
    assert ic.decrypt_bytes(data, PASS) == b'ip,username\r\nh,u\r\n'
    with pytest.raises(ic.InventoryError, match='wrong passphrase'):
        ic.decrypt_bytes(data, PASS + 'x')


def test_tampering_is_detected():
    data = bytearray(ic.encrypt_bytes(b'ip,username\r\nh,u\r\n', PASS))
    flipped = bytearray(data)
    flipped[-3] ^= 1
    with pytest.raises(ic.InventoryError):
        ic.decrypt_bytes(bytes(flipped), PASS)
    # the header (cost parameters, salt, nonce) is authenticated too
    head_end = data.index(b'\n', len(ic.MAGIC))
    changed = bytes(data[:head_end]).replace(b'"p": 1', b'"p": 2') + bytes(data[head_end:])
    assert changed != bytes(data)
    with pytest.raises(ic.InventoryError):
        ic.decrypt_bytes(changed, PASS)


# --- encrypt --------------------------------------------------------------

def test_encrypt_writes_enc_keeps_plain_and_fleet_tools_read_it(tmp_path, monkeypatch, capsys):
    plain = tmp_path / 'collectors.csv'
    plain.write_text(PLAIN, encoding='utf-8', newline='')
    Prompts(monkeypatch, secrets=[PASS, PASS])
    assert ic.main(['encrypt', str(plain)]) == 0
    enc = tmp_path / 'collectors.csv.enc'
    assert plain.exists() and enc.exists()
    assert SECRET_A.encode() not in enc.read_bytes()
    out = capsys.readouterr().out
    assert SECRET_A not in out and 'delete' in out

    expected = _ssh.read_inventory(plain)
    p = Prompts(monkeypatch, secrets=[PASS])
    assert _ssh.read_inventory(enc) == expected
    assert len(p.asked) == 1


def test_plain_csv_still_read_without_a_prompt(tmp_path, monkeypatch):
    plain = tmp_path / 'collectors.csv'
    plain.write_text('\ufeff' + PLAIN, encoding='utf-8', newline='')
    Prompts(monkeypatch)  # any prompt would fail the test
    rows, fields = _ssh.read_inventory(plain)
    assert fields[0] == 'ip' and rows[0]['password'] == SECRET_A


def test_fleet_tools_exit_1_on_wrong_passphrase(enc, monkeypatch, capsys):
    Prompts(monkeypatch, secrets=['not the passphrase'])
    with pytest.raises(SystemExit) as e:
        _ssh.read_inventory(enc)
    assert e.value.code == 1
    assert 'wrong passphrase' in capsys.readouterr().out


@pytest.mark.parametrize('secrets, problem', [
    (['short', 'short'], 'at least'),
    ([PASS, PASS + '!'], 'differ'),
])
def test_encrypt_refuses_weak_or_mistyped_passphrase(tmp_path, monkeypatch, capsys, secrets, problem):
    plain = tmp_path / 'collectors.csv'
    plain.write_text(PLAIN, encoding='utf-8', newline='')
    Prompts(monkeypatch, secrets=secrets)
    assert ic.main(['encrypt', str(plain)]) == 1
    assert problem in capsys.readouterr().out
    assert not (tmp_path / 'collectors.csv.enc').exists()


def test_encrypt_refuses_existing_output_and_bad_csv(tmp_path, monkeypatch, capsys):
    plain = tmp_path / 'collectors.csv'
    plain.write_text(PLAIN + 'NAS1.example.org,dave,,/x,/y,\r\n', encoding='utf-8', newline='')
    Prompts(monkeypatch)
    assert ic.main(['encrypt', str(plain)]) == 1
    assert 'already listed' in capsys.readouterr().out
    plain.write_text(PLAIN, encoding='utf-8', newline='')
    (tmp_path / 'collectors.csv.enc').write_bytes(b'something')
    assert ic.main(['encrypt', str(plain)]) == 1
    assert 'already exists' in capsys.readouterr().out
    assert (tmp_path / 'collectors.csv.enc').read_bytes() == b'something'


# --- show -----------------------------------------------------------------

def test_show_hides_passwords(enc, monkeypatch, capsys):
    Prompts(monkeypatch, secrets=[PASS])
    assert ic.main(['show', str(enc)]) == 0
    out = capsys.readouterr().out
    assert 'nas1.example.org' in out and 'alice' in out and ic.MASK in out
    assert SECRET_A not in out and SECRET_B not in out
    assert '3 systems' in out


# --- edit -----------------------------------------------------------------

def test_edit_saves_changes_keeps_bak_and_deletes_the_copy(enc, editdir, monkeypatch, capsys):
    before = enc.read_bytes()
    seen = fake_editor(monkeypatch, lambda t: t.replace(SECRET_B, 'pw-new-1') +
                       'nas2.example.org,erin,pw-new-2,/volume1/x,/usr/bin/python3,\r\n')
    Prompts(monkeypatch, secrets=[PASS], answers=['', 'y'])
    assert ic.main(['edit', str(enc)]) == 0
    out = capsys.readouterr().out
    assert '+ new host   nas2.example.org' in out and '~ changed    192.0.2.10  password' in out
    for secret in (SECRET_A, SECRET_B, 'pw-new-1', 'pw-new-2'):
        assert secret not in out
    assert seen and seen[0].parent == editdir and not seen[0].exists()
    assert list(editdir.iterdir()) == []
    assert (enc.parent / 'collectors.csv.enc.bak').read_bytes() == before
    rows, _, _ = ic.load(enc, PASS)
    assert [r['ip'] for r in rows][-1] == 'nas2.example.org'
    assert rows[1]['password'] == 'pw-new-1'


def test_edit_answer_no_changes_nothing(enc, editdir, monkeypatch):
    before = enc.read_bytes()
    fake_editor(monkeypatch, lambda t: t.replace('alice', 'mallory'))
    Prompts(monkeypatch, secrets=[PASS], answers=['', 'n'])
    assert ic.main(['edit', str(enc)]) == 1
    assert enc.read_bytes() == before
    assert not (enc.parent / 'collectors.csv.enc.bak').exists()
    assert list(editdir.iterdir()) == []


def test_edit_without_changes_saves_nothing(enc, editdir, monkeypatch, capsys):
    before = enc.read_bytes()
    fake_editor(monkeypatch, lambda t: t)
    Prompts(monkeypatch, secrets=[PASS], answers=[''])
    assert ic.main(['edit', str(enc)]) == 0
    assert 'No changes' in capsys.readouterr().out
    assert enc.read_bytes() == before


def test_edit_reports_problems_and_reopens(enc, editdir, monkeypatch, capsys):
    fake_editor(monkeypatch,
                lambda t: t + ',nobody,,/x,/y,\r\n',                       # empty ip
                lambda t: t.replace(',nobody,', 'nas3.example.org,nobody,'))
    Prompts(monkeypatch, secrets=[PASS], answers=['', 'y', '', 'y'])
    assert ic.main(['edit', str(enc)]) == 0
    assert 'empty ip' in capsys.readouterr().out
    rows, _, _ = ic.load(enc, PASS)
    assert rows[-1]['ip'] == 'nas3.example.org'
    assert list(editdir.iterdir()) == []


def test_edit_problems_discarded(enc, editdir, monkeypatch):
    before = enc.read_bytes()
    fake_editor(monkeypatch, lambda t: t.replace('ip,username', 'host,user'))
    Prompts(monkeypatch, secrets=[PASS], answers=['', 'n'])
    assert ic.main(['edit', str(enc)]) == 1
    assert enc.read_bytes() == before
    assert list(editdir.iterdir()) == []


def test_edit_wrong_passphrase_writes_no_copy(enc, editdir, monkeypatch):
    fake_editor(monkeypatch)
    Prompts(monkeypatch, secrets=['wrong passphrase!'])
    assert ic.main(['edit', str(enc)]) == 1
    assert list(editdir.iterdir()) == []


def test_edit_finds_a_leftover_copy(enc, editdir, monkeypatch):
    editdir.mkdir(parents=True)
    leftover = editdir / 'collectors.csv-edit-999.csv'
    leftover.write_text(PLAIN, encoding='utf-8')
    Prompts(monkeypatch, answers=['n'])
    assert ic.main(['edit', str(enc)]) == 1
    assert leftover.exists()
    fake_editor(monkeypatch, lambda t: t)
    Prompts(monkeypatch, secrets=[PASS], answers=['y', ''])
    assert ic.main(['edit', str(enc)]) == 0
    assert list(editdir.iterdir()) == []


# --- set-password, passphrase, decrypt ------------------------------------

def test_set_password_changes_only_that_host(enc, monkeypatch, capsys):
    Prompts(monkeypatch, secrets=[PASS, 'pw-fresh-55', 'pw-fresh-55'], answers=['y'])
    assert ic.main(['set-password', str(enc), '--only', 'nas1']) == 0
    out = capsys.readouterr().out
    assert '~ changed    nas1.example.org  password' in out and 'pw-fresh-55' not in out
    rows, _, _ = ic.load(enc, PASS)
    assert [r['password'] for r in rows] == ['pw-fresh-55', SECRET_B, '']


def test_set_password_mistyped_or_unknown_host(enc, monkeypatch, capsys):
    before = enc.read_bytes()
    Prompts(monkeypatch, secrets=[PASS, 'pw-1', 'pw-2'])
    assert ic.main(['set-password', str(enc), '--only', 'nas1']) == 1
    Prompts(monkeypatch, secrets=[PASS])
    assert ic.main(['set-password', str(enc), '--only', 'nosuchhost']) == 1
    assert 'matched no system' in capsys.readouterr().out
    assert enc.read_bytes() == before


def test_change_passphrase(enc, monkeypatch):
    new = 'a different long passphrase'
    Prompts(monkeypatch, secrets=[PASS, new, new])
    assert ic.main(['passphrase', str(enc)]) == 0
    assert ic.load(enc, new)[0][0]['password'] == SECRET_A
    with pytest.raises(ic.InventoryError):
        ic.load(enc, PASS)
    assert ic.load(enc.parent / 'collectors.csv.enc.bak', PASS)[0][0]['ip'] == 'nas1.example.org'


def test_decrypt_asks_and_never_overwrites(enc, tmp_path, monkeypatch):
    out = tmp_path / 'plain.csv'
    Prompts(monkeypatch, secrets=[PASS], answers=['n'])
    assert ic.main(['decrypt', str(enc), '-o', str(out)]) == 1
    assert not out.exists()
    Prompts(monkeypatch, secrets=[PASS], answers=['y'])
    assert ic.main(['decrypt', str(enc), '-o', str(out)]) == 0
    assert _ssh.read_inventory(out) == ic.load(enc, PASS)[:2]
    Prompts(monkeypatch)
    assert ic.main(['decrypt', str(enc), '-o', str(out)]) == 1


def test_save_is_atomic_on_failure(enc, monkeypatch):
    before = enc.read_bytes()

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError):
        ic.save(enc, 'ip,username\r\nh,u\r\n', PASS)
    assert enc.read_bytes() == before
