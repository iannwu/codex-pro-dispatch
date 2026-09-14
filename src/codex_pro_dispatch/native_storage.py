"""Descriptor-relative cooperative storage and wire-v1 canonical encoding.

This protects cooperating processes from accidental substitution, not hostile
code running as the same UID. Errors deliberately contain no file contents.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat


class IntegrityError(ValueError):
    pass


def read_evidence(path, limit=4 * 1024 * 1024):
    """Read an owned private artifact without following any path component."""
    path = Path(path)
    with Directory(path.parent) as directory:
        if stat.S_IMODE(os.fstat(directory.fd).st_mode) != 0o700:
            raise IntegrityError('evidence_parent_mode')
        fd = directory.open(path.name)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if stat.S_IMODE(before.st_mode) != 0o600:
                raise IntegrityError('evidence_file_mode')
            raw = source.read(limit + 1)
            after = os.fstat(source.fileno())
            check(after)
            current = os.stat(path.name, dir_fd=directory.fd, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if any(getattr(before, k) != getattr(after, k) or getattr(after, k) != getattr(current, k) for k in fields):
                raise IntegrityError('evidence_identity_changed')
            directory.revalidate()
            if stat.S_IMODE(os.fstat(directory.fd).st_mode) != 0o700:
                raise IntegrityError('evidence_parent_mode')
        if len(raw) > limit:
            raise IntegrityError('evidence_too_large')
        return raw


def canonical(value):
    def validate(v):
        if v is None or type(v) is bool:
            return
        if type(v) is int and 0 <= v <= 9007199254740991:
            return
        if type(v) is str:
            if any(0xD800 <= ord(c) <= 0xDFFF for c in v):
                raise IntegrityError("non_scalar_string")
            return
        if type(v) is list:
            for item in v:
                validate(item)
            return
        if type(v) is dict and all(type(k) is str for k in v):
            for k, item in v.items():
                validate(k)
                validate(item)
            return
        raise IntegrityError("non_canonical_type")
    validate(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw, *, frozen=False):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise IntegrityError("duplicate_key")
            result[k] = v
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        encoded = canonical(value)
        if frozen and raw != encoded + b"\n":
            raise IntegrityError("non_canonical_record")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise IntegrityError("record_recovery_required") from None


def identity(info):
    return info.st_dev, info.st_ino


def check(info, *, directory=False, private=True):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise IntegrityError("unexpected_storage_type")
    if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise IntegrityError("storage_not_private")
    if not directory and info.st_nlink != 1:
        raise IntegrityError("hardlink_rejected")


class Directory:
    """Pin every component, including ancestors; never resolve symlinks.

    A failed write retains its exclusive temporary file. A later operation
    refuses stale temporary files instead of guessing whether an effect ran.
    """
    def __init__(self, path, *, create=False, private=True):
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise IntegrityError("physical_absolute_path_required")
        self.chain = []
        self.fd = -1
        self.private = private
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for index, name in enumerate(self.path.parts[1:]):
                final = index == len(self.path.parts) - 2
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                self.chain.append((fd, name, identity(os.fstat(child))))
                fd = child
                check(os.fstat(fd), directory=True, private=private and final)
            self.fd = fd
            self.revalidate()
        except BaseException:
            os.close(fd)
            for parent, _, _ in self.chain:
                os.close(parent)
            self.chain = []
            raise

    def revalidate(self):
        for parent, name, expected in self.chain:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or identity(info) != expected:
                raise IntegrityError("root_identity_changed")
        if self.fd >= 0:
            check(os.fstat(self.fd), directory=True, private=self.private)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        for parent, _, _ in self.chain:
            os.close(parent)
        self.chain = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def name(self, name):
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise IntegrityError("invalid_storage_name")
        return name

    def open(self, name, flags=os.O_RDONLY, *, create=False):
        self.revalidate()
        fd = os.open(self.name(name), flags | os.O_NOFOLLOW | os.O_NONBLOCK |
                     (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=self.fd)
        try:
            check(os.fstat(fd))
            self.revalidate()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name, limit=32 * 1024 * 1024):
        self.name(name)
        self.revalidate()
        if any(n.startswith('.'+name+'.') and n.endswith('.tmp') for n in os.listdir(self.fd)):
            raise IntegrityError('stale_temp_recovery_required')
        fd = self.open(name)
        with os.fdopen(fd, "rb") as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise IntegrityError("record_too_large")
        self.revalidate()
        return raw

    def exists(self, name):
        try:
            fd = self.open(name)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def write(self, name, raw, *, immutable=False):
        self.name(name)
        self.revalidate()
        if any(n.startswith("." + name + ".") and n.endswith(".tmp") for n in os.listdir(self.fd)):
            raise IntegrityError("stale_temp_recovery_required")
        present = self.exists(name)
        if present and immutable:
            if self.read(name) != raw:
                raise IntegrityError("immutable_record_conflict")
            return
        temporary = "." + name + "." + secrets.token_hex(16) + ".tmp"
        fd = self.open(temporary, os.O_WRONLY, create=True)
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        self.revalidate()
        if present:
            self.exists(name)
        os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self.revalidate()

    def remove(self, name):
        if self.exists(name):
            self.revalidate()
            os.unlink(self.name(name), dir_fd=self.fd)
            os.fsync(self.fd)
