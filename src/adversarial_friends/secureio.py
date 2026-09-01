"""Private filesystem primitives for run-owned artifacts."""

import errno
import os
from pathlib import Path
import stat

DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False) -> Path:
    target = Path(path)
    target.mkdir(mode=DIR_MODE, parents=parents, exist_ok=exist_ok)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ELOOP, "secure directory must not be a symlink", str(target))
    target.chmod(DIR_MODE, follow_symlinks=False)
    return target


def secure_write_bytes(path: Path, payload: bytes) -> Path:
    target = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("secure write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def secure_write_text(path: Path, text: str) -> Path:
    return secure_write_bytes(path, text.encode("utf-8"))


def secure_copy(source: Path, target: Path) -> Path:
    destination = Path(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
        with Path(source).open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("secure copy made no progress")
                    view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination


def repair_private(path: Path, *, directory: bool = False) -> None:
    """Repair one known run-owned path without following a symlink."""
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    expected = stat.S_IFDIR if directory else stat.S_IFREG
    if stat.S_IFMT(info.st_mode) != expected:
        raise OSError(errno.ELOOP, "run-owned path has unsafe file type", str(target))
    target.chmod(DIR_MODE if directory else FILE_MODE, follow_symlinks=False)


def repair_private_tree(root: Path) -> None:
    """Repair a validated run tree without following any contained symlink."""
    base = Path(root)
    repair_private(base, directory=True)
    for current, directories, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if not child.is_symlink():
                repair_private(child, directory=True)
        for name in files:
            child = current_path / name
            if child.is_symlink():
                continue
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.ELOOP, "run-owned path has unsafe file type", str(child))
            # Preserve executability for temporary checked-out tools. Private
            # traversal still comes from the enclosing 0700 run directory.
            mode = 0o700 if info.st_mode & 0o111 else FILE_MODE
            child.chmod(mode, follow_symlinks=False)
