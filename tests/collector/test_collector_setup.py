"""Logging, import path, requirements and installer config generation."""
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys

import pytest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COLLECTOR_DIR = os.path.join(REPO_DIR, 'collector')
SYNOLOGY_INSTALLER = os.path.join(REPO_DIR, 'install-collector-synology.sh')


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield root
    for h in root.handlers:
        if h not in saved[1]:
            h.close()
    root.handlers[:] = saved[1]
    root.setLevel(saved[0])


def test_invalid_log_level_defaults_to_info(collector, tmp_path, clean_root_logger, caplog):
    root = collector.setup_logging(str(tmp_path / 'logs' / 'c.log'), 'LOUD')
    assert root.level == logging.INFO
    assert "Invalid log_level 'LOUD'" in caplog.text
    handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 5 * 1024 * 1024 and handlers[0].backupCount == 5


def test_valid_log_level_is_used(collector, clean_root_logger):
    assert collector.setup_logging(None, 'debug').level == logging.DEBUG


def test_log_rotation_keeps_backups(collector, tmp_path):
    path = str(tmp_path / 'c.log')
    h = collector._MultiProcessRotatingFileHandler(path, maxBytes=200, backupCount=2)
    h.setFormatter(logging.Formatter('%(message)s'))
    try:
        for i in range(30):
            h.emit(logging.makeLogRecord({'msg': f'line {i:02d} ' + 'x' * 40}))
    finally:
        h.close()
    assert sorted(os.listdir(tmp_path)) == ['c.log', 'c.log.1', 'c.log.2']
    with open(path) as f:
        assert 'line 29' in f.read()


def test_log_rotation_failure_keeps_logging_and_backups(collector, tmp_path, monkeypatch):
    # On Windows the live log can't be renamed while another collector process
    # (e.g. a long disk scan) has it open.
    path = str(tmp_path / 'c.log')
    with open(path + '.1', 'w') as f:
        f.write('older backup\n')
    h = collector._MultiProcessRotatingFileHandler(path, maxBytes=100, backupCount=3)
    h.setFormatter(logging.Formatter('%(message)s'))
    real_replace = os.replace

    def replace(src, dst):
        if src == path:
            raise PermissionError('file in use')
        return real_replace(src, dst)
    monkeypatch.setattr(collector.os, 'replace', replace)
    try:
        for i in range(10):
            h.emit(logging.makeLogRecord({'msg': f'line {i} ' + 'x' * 40}))
    finally:
        monkeypatch.undo()
        h.close()
    with open(path) as f:
        assert [l.split()[1] for l in f] == [str(i) for i in range(10)]   # nothing dropped
    assert sorted(os.listdir(tmp_path)) == ['c.log', 'c.log.1']           # backups not shifted
    with open(path + '.1') as f:
        assert f.read() == 'older backup\n'


def test_lib_dir_is_not_put_ahead_of_stdlib(collector):
    assert os.path.join(COLLECTOR_DIR, 'lib') not in sys.path
    stdlib = os.path.dirname(os.__file__)
    if COLLECTOR_DIR in sys.path and stdlib in sys.path:
        assert sys.path.index(COLLECTOR_DIR) > sys.path.index(stdlib)
    assert collector.local_queue.__file__.startswith(os.path.join(COLLECTOR_DIR, 'lib'))


def test_planted_module_in_lib_is_not_imported(tmp_path):
    # Old collectors did sys.path.insert(0, 'lib'), so e.g. lib/requests.py or
    # lib/psutil.py would shadow the real package.
    copy = tmp_path / 'collector'
    shutil.copytree(COLLECTOR_DIR, copy, ignore=shutil.ignore_patterns('__pycache__', 'local'))
    marker = tmp_path / 'PLANTED-RAN'
    for mod in ('requests', 'psutil', 'uuid'):
        (copy / 'lib' / f'{mod}.py').write_text(
            f"open({str(marker)!r}, 'a').write({mod!r})\nraise ImportError('planted')\n")
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({
        'name': 'planted', 'id': 'x', 'manager_url': 'http://127.0.0.1:9',
        'manager_token': 'collector-token-0123456789', 'data_dir': str(tmp_path / 'data'),
        'request_timeout_seconds': 2,
    }))
    proc = subprocess.run([sys.executable, str(copy / 'collector.py'), '--config', str(cfg),
                           '--mode', 'metrics'], capture_output=True, text=True, timeout=120)
    assert not marker.exists(), proc.stderr
    assert 'Archived' in proc.stderr          # ran far enough to import everything


def test_collector_requirements_are_pinned():
    with open(os.path.join(COLLECTOR_DIR, 'requirements.txt')) as f:
        reqs = [l.strip() for l in f if l.strip() and not l.lstrip().startswith('#')]
    assert reqs == ['requests==2.32.3', 'psutil==5.9.8']


# ---------------------------------------------------------------------------
# install-collector-synology.sh
# ---------------------------------------------------------------------------

def test_synology_installer_syntax():
    subprocess.run(['bash', '-n', SYNOLOGY_INSTALLER], check=True)


def _installer_config_writer():
    with open(SYNOLOGY_INSTALLER) as f:
        text = f.read()
    match = re.search(r"<<'PYCONFIG'\n(.*?)\nPYCONFIG\n", text, re.S)
    assert match, 'config writer heredoc not found'
    return match.group(1)


def test_synology_installer_writes_valid_private_json(tmp_path):
    path = tmp_path / 'config.json'
    nasty_token = 'tok"en\\with\'quotes$(touch pwned)`id`'
    env = dict(os.environ,
               LM_CONFIG_FILE=str(path), LM_NAME='triton', LM_ID='synology-triton',
               LM_DEVICE_TYPE='NAS "quoted"', LM_MANAGER_URL='https://mgr:5000',
               LM_MANAGER_TOKEN=nasty_token, LM_VOLUMES='/volume1\n/volume2\n',
               LM_SCAN_PATHS_INPUT=" /volume1/Bob's Data , \"/volume2/My Stuff\" ,, /volume1/*",
               LM_DATA_DIR='/volume1/lab-monitor/data',
               LM_LOG_FILE='/volume1/lab-monitor/logs/collector.log')
    proc = subprocess.run([sys.executable, '-', ], input=_installer_config_writer(), env=env,
                          capture_output=True, text=True, cwd=tmp_path, check=True)
    config = json.loads(path.read_text())
    assert config['manager_token'] == nasty_token
    assert config['device_type'] == 'NAS "quoted"'
    assert config['volumes'] == ['/volume1', '/volume2']
    assert config['scan_paths'] == ["/volume1/Bob's Data", '/volume2/My Stuff', '/volume1/*']
    # Unset, so the default scales with the number of scan_paths entries
    assert 'total_timeout_seconds' not in config and config['max_batch_messages'] == 100
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not (tmp_path / 'pwned').exists()
    assert 'Scan Paths' in proc.stdout


def test_synology_installer_defaults_scan_paths_to_volume_globs(tmp_path):
    path = tmp_path / 'config.json'
    env = dict(os.environ, LM_CONFIG_FILE=str(path), LM_NAME='n', LM_ID='i', LM_DEVICE_TYPE='NAS',
               LM_MANAGER_URL='http://m', LM_MANAGER_TOKEN='t', LM_VOLUMES='/volume1\n',
               LM_SCAN_PATHS_INPUT='', LM_DATA_DIR='/d', LM_LOG_FILE='/l')
    subprocess.run([sys.executable, '-'], input=_installer_config_writer(), env=env,
                   capture_output=True, text=True, check=True)
    assert json.loads(path.read_text())['scan_paths'] == ['/volume1/*']


def test_synology_installer_reads_versions_from_collector_requirements():
    with open(SYNOLOGY_INSTALLER) as f:
        text = f.read()
    assert 'collector/requirements.txt' in text
    assert not re.search(r"(requests|psutil)==\d", text)       # no hardcoded pins
    assert 'read -p' not in text                               # always read -r


def test_installers_do_not_suggest_https_on_the_managers_plain_http_port():
    # The Manager serves plain http on :5000; an https:// example there gives
    # an SSLError on every POST.
    for name in ('install-collector-synology.sh', 'install-collector-windows.ps1'):
        with open(os.path.join(REPO_DIR, name)) as f:
            assert 'https://atlantis.med.harvard.edu:5000' not in f.read(), name


def test_synology_installer_asks_before_tightening_an_existing_config():
    with open(SYNOLOGY_INSTALLER) as f:
        text = f.read()
    rerun = text[text.index('Config file already exists'):text.index('Step 7')]
    assert 'Restrict config.json to 600 now? [y/N]' in rerun
    assert rerun.index('read -r -p') < rerun.index('chmod 600 "$CONFIG_FILE"')


@pytest.mark.parametrize('name, key', [('Lab PC', 'Lab_PC'), ('Atlas 2 (old)', 'Atlas_2__old_')])
def test_name_outside_pattern_warns_without_telling_to_rename(collector, caplog, name, key):
    # A name the previous Manager registered keeps working on the new Manager;
    # renaming would start a new device and split its history.
    with caplog.at_level(logging.WARNING):
        assert collector._identity({'name': name, 'id': 'x'}, logging.getLogger('test'))[0] == name
    text = caplog.text
    assert 'exactly this spelling' in text and 'new device' in text and repr(key) in text
    assert "Change 'name'" not in text and 'will reject' not in text


def test_valid_name_does_not_warn(collector, caplog):
    with caplog.at_level(logging.WARNING):
        collector._identity({'name': 'lab.ws01', 'id': 'x'}, logging.getLogger('test'))
    assert caplog.text == ''
