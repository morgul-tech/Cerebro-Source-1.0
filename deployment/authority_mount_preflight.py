"""Read-only Linux custody/mount leaf for the selected Context carrier.

No factory, database connection, credential lookup, provisioning or repair.
The pinned protected composition owns this invocation. Its result establishes
local mount/custody observations, never provider volume identity, restart
continuity, signature authority or permission to operate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

MOUNT = Path('/var/lib/cerebro-pm-authority')
AUTHORITY = MOUNT / 'authority'
FILES = ('authority-config.json', 'principal-authority.sqlite3')
MAX_MOUNTINFO = 1024 * 1024
MAX_CONFIG = 65536


class MountRefused(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise MountRefused(reason)


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def _mount_record(raw, expected):
    """Pure parser; helpers are not alternate production entry points."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_MOUNTINFO,
            'PM_AUTHORITY_MOUNTINFO_BOUND')
    matches = []
    for line in raw.decode('utf-8', errors='strict').splitlines():
        fields = line.split()
        require('-' in fields and len(fields) >= 10, 'PM_AUTHORITY_MOUNTINFO_FORMAT')
        separator = fields.index('-')
        require(separator >= 6 and len(fields) >= separator + 4,
                'PM_AUTHORITY_MOUNTINFO_FORMAT')
        if _unescape(fields[4]) == str(expected):
            matches.append((fields, separator))
    require(len(matches) == 1, 'PM_AUTHORITY_DEDICATED_MOUNT_REQUIRED')
    fields, separator = matches[0]
    filesystem = fields[separator + 1]
    require(filesystem not in {'overlay', 'tmpfs', 'ramfs', 'proc', 'sysfs',
                               'squashfs', 'devtmpfs'},
            'PM_AUTHORITY_EPHEMERAL_OR_SPECIAL_MOUNT')
    require('rw' in fields[5].split(',') and 'rw' in fields[separator+3].split(','),
            'PM_AUTHORITY_WRITABLE_MOUNT_REQUIRED')
    return {'mount_id': fields[0], 'device': fields[2],
            'filesystem': filesystem, 'mountpoint': str(expected),
            'provider_volume_identity': 'NOT_ATTESTED_BY_MOUNTINFO'}


def _directory(path, *, private):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
            and not stat.S_ISLNK(info.st_mode) and not (info.st_mode & 0o022),
            'PM_AUTHORITY_DIRECTORY_CUSTODY')
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o700,
                'PM_AUTHORITY_PRIVATE_DIRECTORY_REQUIRED')
    return info


def _fd_mount_id(fd):
    require(type(fd) is int and fd >= 0, 'PM_AUTHORITY_OWN_FD_REQUIRED')
    with open('/proc/self/fdinfo/' + str(fd), 'rb') as stream:
        raw = stream.read(4097)
    require(len(raw) <= 4096, 'PM_AUTHORITY_FDINFO_BOUND')
    matches = re.findall(rb'^mnt_id:\s*([0-9]+)\s*$', raw, re.MULTILINE)
    require(len(matches) == 1, 'PM_AUTHORITY_FD_MOUNT_ID_REQUIRED')
    return matches[0].decode('ascii')


def _same_mount(fd, mount_id):
    require(type(mount_id) is str and re.fullmatch('[0-9]+', mount_id)
            and _fd_mount_id(fd) == mount_id,
            'PM_AUTHORITY_DESCENDANT_OR_DIFFERENT_MOUNT')


def _file_info(parent, name, *, config, mount_id):
    """FD-relative, no-follow custody/readback. Never reads SQLite body."""
    # Refuse observed special files before opening; NONBLOCK also bounds a
    # regular->FIFO race. fstat remains authoritative for the opened object.
    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    require(stat.S_ISREG(observed.st_mode), 'PM_AUTHORITY_REGULAR_FILE_REQUIRED')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        _same_mount(fd, mount_id)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1,
                'PM_AUTHORITY_PRIVATE_SINGLE_LINK_FILE_REQUIRED')
        result = {'present': True, 'device': before.st_dev, 'inode': before.st_ino,
                  'size': before.st_size, 'mode': '0600', 'uid': 0}
        if config:
            require(0 < before.st_size <= MAX_CONFIG, 'PM_AUTHORITY_CONFIG_BOUND')
            raw = b''
            while len(raw) <= MAX_CONFIG:
                chunk = os.read(fd, min(8192, MAX_CONFIG+1-len(raw)))
                if not chunk:
                    break
                raw += chunk
            after = os.fstat(fd)
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    and len(raw) == before.st_size, 'PM_AUTHORITY_CONFIG_CHANGED')
            # Custody and exact bytes only; no public key or authority inferred.
            result['sha256'] = hashlib.sha256(raw).hexdigest()
        return result
    finally:
        os.close(fd)


def observe_selected_mount():
    require(os.name == 'posix' and os.geteuid() == 0,
            'PM_AUTHORITY_ACTUAL_LINUX_ROOT_REQUIRED')
    # Fixed selected paths. No worker path, env fallback or mkdir/chmod.
    for parent in reversed(MOUNT.parents):
        _directory(parent, private=False)
    _directory(MOUNT, private=False)
    with open('/proc/self/mountinfo', 'rb') as stream:
        mount = _mount_record(stream.read(MAX_MOUNTINFO+1), MOUNT)
    _directory(AUTHORITY, private=True)
    directory = os.open(AUTHORITY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(directory)
        current = AUTHORITY.lstat()
        require((opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino),
                'PM_AUTHORITY_DIRECTORY_CHANGED')
        _same_mount(directory, mount['mount_id'])
        files = {}
        for name in FILES:
            try:
                files[name] = _file_info(directory, name, config=name == FILES[0],
                                         mount_id=mount['mount_id'])
            except FileNotFoundError:
                files[name] = {'present': False}
        require(files[FILES[0]]['present'] or not files[FILES[1]]['present'],
                'PM_AUTHORITY_ORPHAN_LEDGER_REFUSED')
        return {'schema': 'd1-authority-mount-observation/v1',
                'mount': mount, 'directory': str(AUTHORITY), 'files': files,
                'commissioning': ('CONFIG_AND_LEDGER_PRESENT_NOT_AUTHENTICATED'
                                  if all(v['present'] for v in files.values()) else
                                  'NOT_COMMISSIONED'),
                'restart_deployment_continuity': 'NOT_ATTESTED_BY_THIS_READ',
                'permission_or_authority_granted': False, 'effect': 'READ_ONLY'}
    finally:
        os.close(directory)
