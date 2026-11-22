"""
Disk usage measurement (cross-platform).

Measures folder trees and volume capacity for the collector's disk mode.

For a volume or folder like /volume1 or E:/Users, discover_folders() lists the
immediate subdirectories; scan_folders() measures each one's total size
(including all nested contents).

Example:
  Folder:   /volume1
  Discovers: /volume1/JeffMoffitt, /volume1/OtherUser, ...
  Measures: Total size of each (all contents within)
  Reports:  {'sizes': {'/volume1/JeffMoffitt': 5000000, ...},
             'timed_out': [...], 'errors': [...]}

Cross-platform approach:
- Windows: PowerShell Get-ChildItem (native Win32 batch enumeration, faster than os.scandir)
- Linux/Synology: Python native os.scandir (iterative, no recursion limit)
- Linux: volumes come from the mount table; a folder's measurement stays on
  that folder's filesystem (like du -x) and never enters pseudo filesystems
  (see ScanPolicy)
- Handles permission errors gracefully
- Per-folder timeout plus an optional overall deadline for large trees (100TB+)
- A folder that times out or fails is reported as such, never as 0 bytes
"""
import contextlib
import contextvars
import os
import shutil
import logging
import stat
import subprocess
import time
import re
import sys
from typing import List, Dict, Tuple, Optional, NamedTuple

logger = logging.getLogger(__name__)

# stat.FILE_ATTRIBUTE_REPARSE_POINT only exists on Windows builds of Python
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)

# How many directory entries to process between deadline checks
_DEADLINE_CHECK_EVERY = 1000

# PowerShell reads the folder from this environment variable, so the path is
# never spliced into the script text (no quoting/injection issues).
PS_PATH_ENV_VAR = 'LM_SCAN_PATH'
PS_MEASURE_SCRIPT = (
    "$r = Get-ChildItem -LiteralPath $env:" + PS_PATH_ENV_VAR + " "
    "-Recurse -Force -File -ErrorAction SilentlyContinue "
    "| Measure-Object -Property Length -Sum; "
    "Write-Output ('{0} {1}' -f [int64]$r.Sum, [int64]$r.Count)"
)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PLATFORMS = ('synology', 'linux', 'windows')

# Present on every DSM (Synology) install; any other POSIX host is 'linux'
SYNOLOGY_MARKERS = ('/etc/synoinfo.conf', '/etc.defaults/synoinfo.conf', '/usr/syno')


def detect_platform() -> str:
    """'windows', 'synology' (a DSM marker file exists) or 'linux' (anything else)."""
    if sys.platform == 'win32':
        return 'windows'
    if any(os.path.exists(marker) for marker in SYNOLOGY_MARKERS):
        return 'synology'
    return 'linux'


# ---------------------------------------------------------------------------
# Linux mount table
# ---------------------------------------------------------------------------

# Real, persistent local filesystems: auto-discovered as volumes on Linux
LINUX_VOLUME_FS_TYPES = frozenset({
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs',
    'bcachefs', 'jfs', 'reiserfs', 'nilfs2',
})
# Volumes only with "include_network_mounts": true; never entered otherwise
# (also not as a scan_paths entry). Cluster filesystems count: every node
# walking a shared Lustre/GPFS/BeeGFS tree is the same problem as NFS.
NETWORK_FS_TYPES = frozenset({'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'ceph',
                              'lustre', 'gpfs', 'beegfs', 'glusterfs', 'afs'})
# Pseudo / virtual / ephemeral filesystems: never volumes, never scanned
# (plus every fuse.* and cgroup* type, see is_pseudo_fs)
PSEUDO_FS_TYPES = frozenset({
    'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'devpts', 'ramfs', 'overlay', 'aufs',
    'squashfs', 'efivarfs', 'securityfs', 'debugfs', 'tracefs', 'pstore', 'bpf',
    'configfs', 'fusectl', 'mqueue', 'hugetlbfs', 'autofs', 'binfmt_misc',
    'rpc_pipefs', 'nsfs', 'selinuxfs', 'nfsd', 'rootfs', 'shiftfs', 'fuse',
})
# Kernel/runtime trees: never scanned, even if the mount table can't be read
LINUX_PSEUDO_ROOTS = frozenset({'/proc', '/sys', '/dev', '/run'})
# Mount points under these are container/snap internals, never volumes
LINUX_VOLUME_EXCLUDED_ROOTS = tuple(sorted(LINUX_PSEUDO_ROOTS)) + (
    '/snap', '/var/snap', '/var/lib/snapd', '/var/lib/docker',
    '/var/lib/containers', '/var/lib/kubelet',
)
# Directory names skipped on Linux (folder discovery and at every level of a scan)
LINUX_SKIP_NAMES = frozenset({'lost+found', '.snapshots'})


class Mount(NamedTuple):
    source: str       # device (/dev/sda2), dataset (tank/home), server:/export, ...
    mountpoint: str
    fstype: str


_MOUNT_ESCAPE_RE = re.compile(r'\\([0-7]{3})')


def _unescape_mount_field(field: str) -> str:
    """/proc/mounts writes space, tab, newline and backslash as \\040 \\011 \\012 \\134."""
    return _MOUNT_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mounts(text: str) -> List[Mount]:
    """Parse /proc/self/mounts or /etc/fstab (same format) into Mount entries, in order."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and not fields[0].startswith('#'):
            mounts.append(Mount(_unescape_mount_field(fields[0]),
                                _unescape_mount_field(fields[1]), fields[2]))
    return mounts


def read_mounts(path: str = '/proc/self/mounts') -> List[Mount]:
    """The mount table of this process, or [] (with a warning) if it can't be read."""
    try:
        with open(path, encoding='utf-8', errors='surrogateescape') as f:
            return parse_mounts(f.read())
    except OSError as e:
        logger.warning(f"Cannot read the mount table {path}: {e}")
        return []


def read_fstab(path: str = '/etc/fstab') -> List[Mount]:
    """/etc/fstab entries (used to tell what an automount point will mount), or [] if unreadable."""
    try:
        with open(path, encoding='utf-8', errors='surrogateescape') as f:
            return parse_mounts(f.read())
    except OSError as e:
        logger.debug(f"Cannot read {path}: {e}")
        return []


def topmost_mounts(mounts: List[Mount]) -> List[Mount]:
    """
    One entry per mount point: the last one in the table, i.e. the filesystem
    a path lookup actually reaches. E.g. a systemd automount shows up as
    "systemd-1 /mnt/usb autofs" and, once triggered, "/dev/sdb1 /mnt/usb ext4"
    on top of it: /mnt/usb is the ext4 filesystem. In table order.
    """
    last = {m.mountpoint: i for i, m in enumerate(mounts)}
    return [m for i, m in enumerate(mounts) if last[m.mountpoint] == i]


def is_pseudo_fs(fstype: str) -> bool:
    """True for pseudo/virtual/ephemeral filesystems (proc, tmpfs, overlay, squashfs, fuse.*, ...)."""
    return fstype in PSEUDO_FS_TYPES or fstype.startswith(('fuse.', 'cgroup'))


def _is_under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r.rstrip('/') + '/') for r in roots)


def _device_of(path: str) -> Optional[int]:
    """st_dev of a mount point, or None if it can't be stat'ed."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def discover_linux_volumes(mounts: Optional[List[Mount]] = None,
                           include_network: bool = False) -> List[str]:
    """
    Mount points of the real, persistent filesystems in the mount table, one
    per filesystem, in mount order.

    Kept: ext2/3/4, xfs, btrfs, zfs, f2fs, ... (LINUX_VOLUME_FS_TYPES), and
    nfs/nfs4/cifs/smb3 only if include_network. Everything else (proc, sysfs,
    tmpfs, devtmpfs, overlay, squashfs snaps, cgroup*, efivarfs, the vfat
    /boot/efi, fuse.*, autofs, ...) is left out, as are mounts under /proc,
    /sys, /dev, /run, /snap and container runtimes' directories.

    A filesystem mounted more than once (btrfs subvolumes of one device, bind
    mounts) is reported once, under its first mount point: their capacity is
    the same, and reporting each would count it twice in total_usage.

    Only the topmost entry at each mount point counts (topmost_mounts): an
    automount (autofs) with its filesystem mounted on top is that
    filesystem; one not mounted right now is left out (measuring its
    capacity would mount it).
    """
    if mounts is None:
        mounts = read_mounts()
    allowed = LINUX_VOLUME_FS_TYPES | (NETWORK_FS_TYPES if include_network else frozenset())
    volumes, sources, devices = [], set(), set()
    for m in topmost_mounts(mounts):
        if m.fstype not in allowed or not m.mountpoint.startswith('/'):
            continue
        if _is_under(m.mountpoint, LINUX_VOLUME_EXCLUDED_ROOTS):
            continue
        if m.mountpoint in volumes or m.source in sources:
            continue
        if m.fstype not in NETWORK_FS_TYPES:
            # Same filesystem under another device name (bind mount). Network
            # mounts are never stat'ed here: a dead server would hang the run.
            dev = _device_of(m.mountpoint)
            if dev is not None:
                if dev in devices:
                    continue
                devices.add(dev)
        sources.add(m.source)
        volumes.append(m.mountpoint)
    return volumes


class ScanPolicy:
    """
    How measure_folder_recursive() and discover_folders() treat
    platform-specific names and mount points. ScanPolicy() is the historical
    (Synology) behaviour; the collector activates the right one for the host
    with `with scan_policy(...)` around a disk run.

    platform 'linux':
      - Synology system names (@eaDir, #recycle, .@*, *.hbk) are NOT skipped
        (they are ordinary names on Linux); lost+found and .snapshots are.
      - Nothing under /proc, /sys, /dev or /run is measured or listed.
    mount_aware (Linux, or one_filesystem): filesystems that are never
      entered, neither below a scanned folder nor as a scan_paths entry or
      glob root itself (refusal_for):
      - pseudo filesystems (tmpfs, overlay, snap squashfs, fuse.*, ...);
      - network mounts (NETWORK_FS_TYPES), unless include_network;
      - automount points (autofs) not mounted right now, unless /etc/fstab
        says they mount a local filesystem (or a network one and
        include_network): those are entered, which mounts them.
      Each mount point is judged by its topmost mount (topmost_mounts), so a
      triggered automount is the filesystem mounted on it, not autofs.
    one_filesystem (Linux default; "cross_filesystems": false): a folder's
      measurement stays on the folder's own filesystem, like du -x: a
      directory with another st_dev (a mount point, a nested btrfs subvolume)
      or that is itself a mount point (a bind mount of the same filesystem)
      is not entered. This keeps a separately mounted volume from being
      counted inside another folder too.
    """

    def __init__(self, platform: str = 'synology', one_filesystem: bool = False,
                 mounts: Optional[List[Mount]] = None, include_network: bool = False,
                 fstab: Optional[List[Mount]] = None):
        self.platform = platform
        self.skip_synology_names = platform != 'linux'
        self.skip_names = LINUX_SKIP_NAMES if platform == 'linux' else frozenset()
        self.one_filesystem = bool(one_filesystem) and platform != 'windows'
        self.mount_aware = platform == 'linux' or self.one_filesystem
        self.include_network = bool(include_network)
        if mounts is None:
            mounts = read_mounts() if self.mount_aware else []
        self.mounts = list(mounts)
        # mount point -> the mount a lookup of that path reaches
        self.top = {m.mountpoint: m for m in topmost_mounts(self.mounts)}
        self.mount_points = frozenset(self.top)
        # Untriggered automount point -> fstype from /etc/fstab (None: unknown)
        self.automount_types: Dict[str, Optional[str]] = {}
        self._refusals: Dict[str, str] = {}
        if self.mount_aware:
            pending = [mp for mp, m in self.top.items() if m.fstype == 'autofs']
            if pending:
                entries = read_fstab() if fstab is None else fstab
                fstab_types = {os.path.normpath(e.mountpoint): e.fstype
                               for e in entries if e.mountpoint.startswith('/')}
                self.automount_types = {mp: fstab_types.get(mp) for mp in pending}
            for mp, m in self.top.items():
                reason = self._filesystem_refusal(m)
                if reason:
                    self._refusals[mp] = reason
        never = set(self._refusals)
        if platform == 'linux':
            never |= LINUX_PSEUDO_ROOTS
        self.never_enter = frozenset(never) if self.mount_aware else frozenset()
        # Automount points that may be entered (entering mounts them)
        self.automounts = frozenset(mp for mp in self.automount_types if mp not in self._refusals)

    def _filesystem_refusal(self, m: Mount) -> Optional[str]:
        """Why the filesystem mounted at m is never entered, or None."""
        fstype, how = m.fstype, ''
        if fstype == 'autofs':
            fstype, how = self.automount_types.get(m.mountpoint), ', automounted'
            if fstype is None or fstype == 'autofs':
                return ("an automount point (autofs) that is not mounted and has no /etc/fstab "
                        "entry, so what it would mount is unknown")
        if fstype in NETWORK_FS_TYPES:
            if self.include_network:
                return None
            return (f"a network filesystem ({fstype}{how}) - set \"include_network_mounts\": "
                    f"true to measure it")
        if is_pseudo_fs(fstype):
            return f"a pseudo filesystem ({fstype}{how})"
        return None

    def mount_point_of(self, path: str) -> Optional[str]:
        """The mount point whose filesystem holds the absolute, normalized `path` (longest prefix)."""
        p = path
        while True:
            if p in self.top:
                return p
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent

    def reason_for(self, mount_point: str) -> Optional[str]:
        """Why the filesystem at mount_point is never entered (None: it may be)."""
        return self._refusals.get(mount_point)

    def refusal_for(self, path: str) -> Optional[str]:
        """
        Why `path` (absolute, normalized) must not be listed or measured at
        all, or None. Pure lookup: never touches the filesystem, so a dead
        network mount can't hang it.
        """
        if self.platform == 'linux' and _is_under(path, LINUX_PSEUDO_ROOTS):
            # e.g. /proc/kcore "is" 128 TiB: these sizes mean nothing
            return (f"{path} is in a kernel/runtime filesystem "
                    f"({', '.join(sorted(LINUX_PSEUDO_ROOTS))}); not measured")
        if not self.mount_aware:
            return None
        mount_point = self.mount_point_of(path)
        reason = self._refusals.get(mount_point) if mount_point is not None else None
        if reason is None:
            return None
        where = path if path == mount_point else f"{path} (on {mount_point})"
        return f"{where} is {reason}; not measured"


def scan_root_refusal(path: str) -> Optional[str]:
    """
    Why the active ScanPolicy refuses a scan_paths entry or glob root, judged
    from the path as written (abspath; nothing is stat'ed, so a dead network
    mount can't hang the check). None = go ahead. The real path (symlinks
    resolved) is checked again when the folder is listed or measured.
    """
    policy = current_policy()
    if not policy.mount_aware:
        return None
    return policy.refusal_for(os.path.abspath(path))


def _trigger_automount(path: str) -> None:
    """Mount an automount point by opening it (stat() alone does not); OSError if that fails."""
    with os.scandir(path):
        pass


_LEGACY_POLICY = ScanPolicy()
_active_policy = contextvars.ContextVar('lab_monitor_scan_policy', default=_LEGACY_POLICY)


def current_policy() -> ScanPolicy:
    """The ScanPolicy in effect (the historical one unless scan_policy() set another)."""
    return _active_policy.get()


@contextlib.contextmanager
def scan_policy(policy: ScanPolicy):
    """Use `policy` for every folder discovery / measurement inside the with-block."""
    token = _active_policy.set(policy)
    try:
        yield policy
    finally:
        _active_policy.reset(token)


def format_bytes(b: int) -> str:
    """Human-readable byte string (B … PB), e.g. '1.5 GB'."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def measure_folder_powershell(path: str, timeout: float = 3600) -> Tuple[int, int]:
    """
    Measure folder size on Windows using PowerShell Get-ChildItem.

    Uses native Win32 batch enumeration (FindFirstFileEx with LARGE_FETCH),
    which is significantly faster than Python's per-entry os.scandir on
    Windows filesystems with large file counts.

    Args:
        path: Folder path to measure (passed via $env:LM_SCAN_PATH)
        timeout: Max seconds to wait for PowerShell process

    Returns: Tuple of (total_bytes, files_counted)

    Raises: TimeoutError if PowerShell exceeds the timeout (the process is
            killed); FileNotFoundError / PermissionError if the folder itself
            is missing or can't be listed; RuntimeError if PowerShell is
            unavailable or its output can't be parsed.
    """
    env = dict(os.environ)
    env[PS_PATH_ENV_VAR] = path
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_MEASURE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.001),
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"PowerShell measurement of {path} exceeded {timeout:.0f}s")
    except OSError as e:
        raise RuntimeError(f"Could not run PowerShell: {e}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"PowerShell returned empty output for {path}")
    try:
        parts = output.split()
        total_bytes = int(parts[0])
        files_counted = int(parts[1]) if len(parts) > 1 else -1
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"Could not parse PowerShell output for {path}: {e}")
    if files_counted == 0:
        # -ErrorAction SilentlyContinue makes Get-ChildItem print "0 0" for an
        # empty folder, a missing one and one it can't list (access denied)
        # alike. Listing it here tells them apart: the last two raise, so the
        # folder goes in _errors instead of being reported as 0 bytes.
        if not os.path.isdir(path):
            raise FileNotFoundError(f"Not a directory: {path}")
        with os.scandir(path):
            pass
    return total_bytes, files_counted


def _is_reparse_dir(entry: os.DirEntry) -> bool:
    """
    True for Windows directory reparse points (junctions, volume mount points,
    directory symlinks). Following them double-counts data or loops forever.
    Reparse-point *files* (e.g. Data Deduplication, cloud placeholders) are
    still counted by the caller; file symlinks are skipped separately.
    """
    is_junction = getattr(entry, 'is_junction', None)   # Python 3.12+
    if is_junction is not None and is_junction():
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    

def measure_folder_recursive(path: str, timeout: float = 3600,
                             deadline: Optional[float] = None,
                             policy: Optional[ScanPolicy] = None) -> Tuple[int, int]:
    """
    Measure folder size by walking the whole tree (cross-platform).

    Iterative (explicit stack) so arbitrarily deep trees can't hit Python's
    recursion limit. Hard-linked files are counted once: (st_dev, st_ino) is
    remembered only for files with st_nlink > 1, so memory stays proportional
    to the number of multiply-linked files, not the size of the tree.
    On Windows os.scandir reports st_nlink = st_ino = 0, so each file is
    lstat()ed once more to get them (this scanner is only the fallback there;
    the PowerShell scan counts every link, as Explorer does).
    
    Args:
        path: Folder path to measure
        timeout: Max seconds to spend measuring this folder
        deadline: Optional absolute time.monotonic() value that also ends the scan
        policy: ScanPolicy for names and mount points (default: current_policy())
    
    Returns: Tuple of (total_bytes, files_counted)

    Raises:
        TimeoutError if the time budget runs out (never a partial total)
        OSError (FileNotFoundError, PermissionError, ...) if path itself
        can't be read. Unreadable subdirectories are skipped and logged.
    """
    limit = time.monotonic() + timeout
    if deadline is not None:
        limit = min(limit, deadline)

    policy = policy or current_policy()
    if policy.mount_aware:
        # Before any stat(): a network mount's server may be down
        reason = policy.refusal_for(os.path.abspath(path))
        if reason:
            raise OSError(reason)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Not a directory: {path}")

    root = path
    root_dev = None                # set when the walk must stay on one filesystem
    inner_mounts = never_enter = frozenset()
    if policy.mount_aware:
        # Walk the canonical path so entry paths compare equal to mount points
        root = os.path.realpath(path)
        reason = policy.refusal_for(root)
        if reason:
            raise OSError(reason if root == os.path.abspath(path) else f"{path} -> {reason}")
        if root in policy.automounts:
            # Mount it first: until then stat() reports the autofs placeholder
            _trigger_automount(root)
        prefix = root.rstrip(os.sep) + os.sep
        never_enter = frozenset(m for m in policy.never_enter if m.startswith(prefix))
        if policy.one_filesystem:
            root_dev = os.stat(root).st_dev
            inner_mounts = frozenset(m for m in policy.mount_points if m.startswith(prefix))
    check_mounts = root_dev is not None or bool(never_enter)

    total_bytes = 0
    files_counted = 0
    unreadable_dirs = 0
    entries_seen = 0
    skipped_mounts = 0
    skipped_examples = []
    linked_inodes = set()     # (dev, ino) of files with st_nlink > 1
    skip_reparse = sys.platform == 'win32'
    stat_for_links = sys.platform == 'win32'
    stack = [root]

    while stack:
        if time.monotonic() > limit:
            raise TimeoutError(f"Measurement of {path} exceeded {timeout:.0f}s")
        dir_path = stack.pop()
        try:
            it = os.scandir(dir_path)
        except OSError as e:
            if dir_path == root:
                raise   # the folder itself is unreadable: an error, not 0 bytes
            unreadable_dirs += 1
            logger.debug(f"Cannot access {dir_path}: {e}")  # Permission denied on system folders is expected
            continue

        with it:
            for entry in it:
                entries_seen += 1
                if entries_seen % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() > limit:
                    raise TimeoutError(f"Measurement of {path} exceeded {timeout:.0f}s")
                try:
                    # Skip symlinks to avoid loops
                    if entry.is_symlink():
                        continue

                    # Skip Synology system directories and backup vaults at every level
                    # (not on Linux, where these are ordinary names)
                    name = entry.name
                    if policy.skip_synology_names:
                        if name.startswith('@') or name == '#recycle' or name.startswith('.@'):
                            continue
                        # Skip Hyper Backup vault packages (.hbk are directories, not files)
                        if name.endswith('.hbk'):
                            continue

                    if entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        if st.st_nlink == 0 and stat_for_links:
                            # Windows: scandir's stat has no link count or file ID
                            # (both 0); os.lstat opens the file and has them.
                            try:
                                st = os.lstat(entry.path)
                            except OSError:
                                pass
                        if st.st_nlink > 1:
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key in linked_inodes:
                                continue   # hard link already counted
                            linked_inodes.add(inode_key)
                        total_bytes += st.st_size
                        files_counted += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if skip_reparse and _is_reparse_dir(entry):
                            continue
                        if name in policy.skip_names:
                            continue
                        if check_mounts and (
                                entry.path in never_enter or
                                (root_dev is not None and (
                                    entry.path in inner_mounts or
                                    entry.stat(follow_symlinks=False).st_dev != root_dev))):
                            skipped_mounts += 1
                            if len(skipped_examples) < 5:
                                skipped_examples.append(entry.path)
                            continue
                        stack.append(entry.path)
                except OSError:
                    continue

    if unreadable_dirs:
        logger.info(f"{path}: skipped {unreadable_dirs} unreadable subfolder(s)")
    if skipped_mounts:
        logger.info(
            f"{path}: did not enter {skipped_mounts} other filesystem(s) / mount point(s) "
            f"(pseudo, network, or not on this folder's filesystem; see cross_filesystems): "
            f"{', '.join(skipped_examples)}{' ...' if skipped_mounts > len(skipped_examples) else ''}"
        )
    return total_bytes, files_counted


def should_skip_folder(folder_name: str, nas_type: str = "synology") -> bool:
    """
    Check if folder should be skipped based on NAS type and naming conventions.
    
    Always skips:
    - Folders starting with '.' (hidden/system folders like .git, .pids, .venv)
    
    Synology NAS additionally skips:
    - @* system directories
    - #recycle (trash)
    
    Windows additionally skips:
    - $RECYCLE.BIN (Windows recycle bin)
    - ServiceProfiles (Windows service account directories)

    Linux additionally skips lost+found (and .snapshots, already hidden).
    
    Args:
        folder_name: Name of the folder to check
        nas_type: 'synology', 'linux' or 'windows'
    
    Returns: True if folder should be skipped, False otherwise
    """
    # Skip hidden/system folders (start with '.')
    if folder_name.startswith('.'):
        return True
    
    if nas_type.lower() == "synology":
        # Skip Synology system directories
        if folder_name.startswith('@'):
            return True
        # Skip trash
        if folder_name == '#recycle':
            return True

    if nas_type.lower() == "linux" and folder_name in LINUX_SKIP_NAMES:
        return True
    
    if nas_type.lower() == "windows" or sys.platform == "win32":
        # Skip Windows recycle bin
        if folder_name == '$RECYCLE.BIN':
            return True
        # Skip Windows service profile directories
        if 'ServiceProfiles' in folder_name:
            return True
    
    return False


def is_valid_volume_path(path: str, system: str = None) -> bool:
    """
    Validate that path is a proper volume/mount point.
    
    Args:
        path: Path to validate
        system: 'windows', 'synology', 'linux', or None (auto-detect)
    
    Returns: True if valid
    """
    if system is None:
        system = detect_platform()
    
    if system == "windows":
        # Windows: Should be drive letter (D:, E:, etc.)
        if not re.match(r'^[A-Z]:', path):
            logger.warning(f"Windows path should start with drive letter: {path}")
            return False
    elif system == "linux":
        # Linux: any mount point (/, /home, /data, /srv/storage, ...)
        if not os.path.isabs(path):
            logger.warning(f"Linux path should be absolute: {path}")
            return False
    else:
        # Synology: Should be /volume* or mount point
        if not (path.startswith('/volume') or path.startswith('/mnt')):
            logger.warning(f"Synology path should be /volume* or /mnt: {path}")
            return False
    
    # Check it exists
    if not os.path.exists(path):
        logger.warning(f"Path does not exist: {path}")
        return False
    
    # Check it's a directory
    if not os.path.isdir(path):
        logger.warning(f"Path is not a directory: {path}")
        return False
    
    return True


def discover_folders(volume_path: str, nas_type: str = "synology",
                     validate_volume: bool = True, raise_errors: bool = False) -> List[str]:
    """
    Discover immediate subdirectories on a volume (one level only).
    
    For /volume1, returns: [/volume1/user1, /volume1/user2, ...]
    For E:/Users, returns: [E:/Users/Admin, E:/Users/Guest, ...]
    
    Does NOT recurse deeper - you measure each subdirectory's full tree,
    but you only report top-level subdirectory sizes.
    
    Args:
        volume_path: Root volume path (e.g., /volume1 or E:)
        nas_type: 'synology', 'linux' or 'windows'. On Linux, subfolders that
            are pseudo-filesystem mounts (/proc, /sys, /dev, /run, tmpfs, ...)
            or, unless the active ScanPolicy includes them, network mounts are
            left out. A subfolder that is another real local filesystem is
            kept: it is measured on its own (within its filesystem).
        validate_volume: require a volume-style path (/volume*, /mnt*, X:; any
            absolute path on Linux).
            Pass False for an explicitly configured scan_paths glob root,
            which may legitimately live anywhere (Linux /data, UNC paths).
        raise_errors: re-raise the OSError if volume_path itself can't be
            listed, so the caller can report it (in _errors) instead of
            getting an empty list that looks like "no subfolders".
    
    Returns: List of immediate subdirectory paths (one level only)
    """
    folders = []
    
    if validate_volume and not is_valid_volume_path(volume_path, system=nas_type):
        return folders

    never_enter = frozenset()
    policy = current_policy()
    if nas_type.lower() == 'linux':
        never_enter = LINUX_PSEUDO_ROOTS | (policy.never_enter if policy.mount_aware else frozenset())
        # Judged as written first (nothing stat'ed on a network mount), then
        # with symlinks resolved
        reason = policy.refusal_for(os.path.abspath(volume_path)) if policy.mount_aware else None
        if reason is None:
            real_root = os.path.realpath(volume_path)
            if policy.mount_aware:
                reason = policy.refusal_for(real_root)
            if reason is None and _is_under(real_root, LINUX_PSEUDO_ROOTS):
                reason = (f"{volume_path} is in a kernel/runtime filesystem "
                          f"({', '.join(sorted(LINUX_PSEUDO_ROOTS))}); not measured")
        if reason:
            error = OSError(f"{reason} (nor are its subfolders)")
            if raise_errors:
                raise error
            logger.warning(str(error))
            return folders
    
    try:
        for entry in os.scandir(volume_path):
            if entry.is_dir(follow_symlinks=False):
                folder_name = entry.name
                
                # Skip system folders on Synology
                if should_skip_folder(folder_name, nas_type):
                    logger.debug(f"Skipping system folder: {entry.path}")
                    continue
                if never_enter and os.path.join(real_root, folder_name) in never_enter:
                    mount_point = os.path.join(real_root, folder_name)
                    reason = policy.reason_for(mount_point) or 'a kernel/runtime filesystem'
                    # An automount that would hold real data deserves a warning
                    log = logger.warning if mount_point in policy.automount_types else logger.info
                    log(f"Skipping {entry.path}: it is {reason}")
                    continue
                
                # Add this immediate subdirectory only
                folders.append(entry.path)
    except OSError as e:
        if raise_errors:
            raise
        logger.debug(f"Cannot read {volume_path}: {e}")
    
    return folders


def measure_volume_capacity(volume_path: str) -> dict:
    """
    Get total and free bytes for a volume (cross-platform).

    Uses shutil.disk_usage which works on Synology (/volume1) and
    Windows (E:) with no platform branching required.

    Args:
        volume_path: Volume root path (e.g. /volume1 or E:)

    Returns: dict with 'total_bytes' and 'free_bytes', or empty dict on error
    """
    try:
        usage = shutil.disk_usage(volume_path)
        return {
            'total_bytes': usage.total,
            'free_bytes':  usage.free,
        }
    except Exception as e:
        logger.warning(f"Could not get capacity for {volume_path}: {e}")
        return {}


def discover_volumes(nas_type: str = None, mounts: Optional[List[Mount]] = None,
                     include_network: bool = False) -> List[str]:
    """
    Auto-discover storage volumes.
    
    Args:
        nas_type: 'synology', 'linux', 'windows', or None (auto-detect)
        mounts, include_network: Linux only, see discover_linux_volumes()
    
    Returns: List of volume paths
    """
    if nas_type is None:
        nas_type = detect_platform()
    
    volumes = []
    
    if nas_type == "windows":
        # Windows: Find all drive letters
        import string
        for drive in string.ascii_uppercase:
            path = f"{drive}:"
            if os.path.exists(path):
                volumes.append(path)
        logger.info(f"Discovered Windows drives: {volumes}")

    elif nas_type == "linux":
        # Real filesystems from the mount table (not /volumeN)
        volumes = discover_linux_volumes(mounts or None, include_network=include_network)
        logger.info(f"Discovered Linux volumes: {volumes}")
    
    else:
        # Synology/Linux: Look for /volume* directories
        for i in range(1, 20):  # Check /volume1 through /volume19
            path = f"/volume{i}"
            if os.path.exists(path) and os.path.isdir(path):
                volumes.append(path)
        logger.info(f"Discovered Synology volumes: {volumes}")
    
    return volumes


def _measure_one_folder(folder_path: str, timeout: float, use_powershell: bool) -> Tuple[int, int]:
    """
    Measure a single folder, using PowerShell on Windows when requested.

    Falls back to the Python scanner (with whatever is left of the budget) if
    PowerShell fails — but not if it timed out, which would only spend the
    same budget again in the slower scanner, nor if the folder itself is
    missing or unreadable.

    Returns: (bytes_used, files_counted)
    Raises: TimeoutError, or OSError/RuntimeError on failure
    """
    deadline = time.monotonic() + timeout
    if use_powershell:
        try:
            return measure_folder_powershell(folder_path, timeout=timeout)
        except OSError:
            raise   # TimeoutError, or the folder itself is missing / can't be listed
        except Exception as ps_err:
            logger.warning(
                f"PowerShell measurement failed for {folder_path} ({ps_err}); "
                f"falling back to Python scanner"
            )
    return measure_folder_recursive(folder_path, timeout=max(0.0, deadline - time.monotonic()),
                                    deadline=deadline)


def scan_folders(folders: List[str], timeout: float = 3600,
                 deadline: Optional[float] = None) -> Dict:
    """
    Measure each folder in a pre-discovered list.

    On Windows, uses PowerShell Get-ChildItem for faster native enumeration.
    Falls back to the Python os.scandir walker if PowerShell is unavailable
    or returns an error.

    Args:
        folders:  Folder paths to measure (e.g. from discover_folders)
        timeout:  Max seconds per folder
        deadline: Optional overall time.monotonic() deadline shared by all
                  folders. Folders not reached before it are reported as
                  timed out rather than silently dropped.

    Returns: {'sizes': {path: bytes}, 'timed_out': [paths], 'errors': [paths]}
             A timed-out or failed folder is never reported with a size.
    """
    sizes: Dict[str, int] = {}
    timed_out: List[str] = []
    errors: List[str] = []
    use_powershell = sys.platform == 'win32'

    if use_powershell and folders:
        logger.info("Windows detected — using PowerShell for folder size measurement")

    for i, folder_path in enumerate(folders):
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            remaining = list(folders[i:])
            logger.warning(
                f"Overall disk-scan time budget exhausted: {len(remaining)} folder(s) not "
                f"measured, reported as timed out: {remaining}"
            )
            timed_out.extend(remaining)
            break
        folder_timeout = timeout if deadline is None else min(timeout, deadline - now)

        logger.info(f"Measuring ... {folder_path}")
        folder_start = time.monotonic()
        try:
            bytes_used, files_counted = _measure_one_folder(folder_path, folder_timeout, use_powershell)
        except TimeoutError as e:
            logger.warning(f"Timed out measuring {folder_path}: {e} (reported as timed out, not 0)")
            timed_out.append(folder_path)
            continue
        except Exception as e:
            logger.error(f"Failed to measure {folder_path}: {e}")
            errors.append(folder_path)
            continue

        sizes[folder_path] = bytes_used
        folder_elapsed = time.monotonic() - folder_start
        count_str = f"{files_counted:,} files" if files_counted >= 0 else "file count n/a"
        logger.info(f"[{folder_elapsed:.1f}s] {folder_path}: {format_bytes(bytes_used)} ({count_str})")

    return {'sizes': sizes, 'timed_out': timed_out, 'errors': errors}
