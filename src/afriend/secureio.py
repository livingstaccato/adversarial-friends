"""Private filesystem primitives for run-owned artifacts."""

from collections.abc import Iterator
import contextlib
import errno
import os
from pathlib import Path
import stat

DIR_MODE = 0o700
FILE_MODE = 0o600

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _relative_parts(root: Path, target: Path) -> tuple[str, ...]:
    """Return a lexical path below root without resolving any symlink."""
    anchor = Path(root).absolute()
    candidate = Path(target).absolute()
    try:
        relative = candidate.relative_to(anchor)
    except ValueError as exc:
        raise OSError(errno.EPERM, "secure path escapes its trusted root", str(target)) from exc
    if any(part in ("", ".", "..") for part in relative.parts):
        raise OSError(errno.EPERM, "invalid secure path component", str(target))
    return relative.parts


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise OSError(errno.ENOTDIR, "secure path component is not a directory", str(name))
    return descriptor


@contextlib.contextmanager
def _directory_fd(
    root: Path,
    target: Path,
    *,
    create: bool = False,
    chmod_target: bool = False,
) -> Iterator[int]:
    """Open target beneath root while refusing every symlink component.

    Each component is opened relative to the already-open parent. Renaming a
    traversed directory and replacing its pathname with a symlink therefore
    cannot redirect a later operation: the kernel continues from the held
    descriptor rather than resolving the pathname again.
    """
    parts = _relative_parts(root, target)
    descriptor = _open_directory(Path(root).absolute())
    try:
        for part in parts:
            try:
                child = _open_directory(part, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, DIR_MODE, dir_fd=descriptor)
                child = _open_directory(part, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if chmod_target:
            os.fchmod(descriptor, DIR_MODE)
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _parent_fd(root: Path, target: Path) -> Iterator[tuple[int, str]]:
    parts = _relative_parts(root, target)
    if not parts:
        raise OSError(errno.EISDIR, "secure file path names a directory", str(target))
    parent = Path(root).absolute().joinpath(*parts[:-1])
    with _directory_fd(root, parent) as descriptor:
        yield descriptor, parts[-1]


def secure_mkdir(
    path: Path,
    *,
    parents: bool = False,
    exist_ok: bool = False,
    root: Path | None = None,
) -> Path:
    target = Path(path)
    if root is not None:
        parts = _relative_parts(root, target)
        if not parts:
            with _directory_fd(root, target, chmod_target=True):
                return target
        parent = Path(root).absolute().joinpath(*parts[:-1])
        with _directory_fd(root, parent, create=parents) as descriptor:
            try:
                os.mkdir(parts[-1], DIR_MODE, dir_fd=descriptor)
            except FileExistsError:
                if not exist_ok:
                    raise
            child = _open_directory(parts[-1], dir_fd=descriptor)
            try:
                os.fchmod(child, DIR_MODE)
            finally:
                os.close(child)
        return target
    target.mkdir(mode=DIR_MODE, parents=parents, exist_ok=exist_ok)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ELOOP, "secure directory must not be a symlink", str(target))
    target.chmod(DIR_MODE, follow_symlinks=False)
    return target


def secure_init_root(path: Path) -> Path:
    """Open or create a storage root without changing caller-owned modes.

    Existing path components are validation boundaries, not run-owned data.
    Walk them by descriptor so a concurrent symlink swap cannot redirect root
    creation. Only components created by this call are forced private.
    """
    target = Path(path).absolute()
    anchor = Path(target.anchor)
    parts = target.parts[1:]
    descriptor = _open_directory(anchor)
    try:
        for part in parts:
            created = False
            try:
                child = _open_directory(part, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, DIR_MODE, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = _open_directory(part, dir_fd=descriptor)
            if created:
                os.fchmod(child, DIR_MODE)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return target


def secure_write_bytes(path: Path, payload: bytes, *, root: Path | None = None) -> Path:
    target = Path(path)
    anchor = target.parent if root is None else root
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _parent_fd(anchor, target) as parent:
        descriptor = os.open(parent[1], flags, FILE_MODE, dir_fd=parent[0])
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


def secure_open_write(path: Path, *, root: Path) -> int:
    """Open a private file for replacement while holding its safe parent."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with _parent_fd(root, path) as (parent, name):
        descriptor = os.open(name, flags, FILE_MODE, dir_fd=parent)
    try:
        os.fchmod(descriptor, FILE_MODE)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def secure_open_append(path: Path, *, root: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with _parent_fd(root, path) as (parent, name):
        descriptor = os.open(name, flags, FILE_MODE, dir_fd=parent)
    try:
        os.fchmod(descriptor, FILE_MODE)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def secure_open_read(path: Path, *, root: Path) -> int:
    with _parent_fd(root, path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    try:
        info = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise OSError(errno.ELOOP, "secure file must be regular", str(path))
    return descriptor


def secure_read_bytes(path: Path, *, root: Path, max_bytes: int) -> bytes:
    descriptor = secure_open_read(path, root=root)
    try:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise OSError(errno.EFBIG, "secure file exceeds byte limit", str(path))
        return payload
    finally:
        os.close(descriptor)


def secure_regular_exists(path: Path, *, root: Path) -> bool:
    try:
        descriptor = secure_open_read(path, root=root)
    except FileNotFoundError:
        return False
    else:
        os.close(descriptor)
        return True


def secure_create_bytes(path: Path, payload: bytes, *, root: Path) -> Path:
    """Durably create one private file without replacing an existing name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with _parent_fd(root, path) as (parent, name):
        descriptor = os.open(name, flags, FILE_MODE, dir_fd=parent)
        try:
            os.fchmod(descriptor, FILE_MODE)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("secure create made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return Path(path)


def secure_open_directory(path: Path, *, root: Path) -> int:
    """Return a descriptor for an existing directory below root."""
    parts = _relative_parts(root, path)
    descriptor = _open_directory(Path(root).absolute())
    try:
        for part in parts:
            child = _open_directory(part, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def secure_write_text(path: Path, text: str, *, root: Path | None = None) -> Path:
    return secure_write_bytes(path, text.encode("utf-8"), root=root)


def secure_copy(source: Path, target: Path, *, root: Path | None = None) -> Path:
    destination = Path(target)
    anchor = destination.parent if root is None else root
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with _parent_fd(anchor, destination) as parent:
        descriptor = os.open(parent[1], flags, FILE_MODE, dir_fd=parent[0])
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


def secure_replace(source: Path, target: Path, *, root: Path) -> Path:
    """Atomically replace target without resolving either parent by name."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.parent != target_path.parent:
        raise OSError(errno.EXDEV, "secure replacement requires one directory")
    with _parent_fd(root, source_path) as (parent, source_name):
        os.replace(source_name, target_path.name, src_dir_fd=parent, dst_dir_fd=parent)
    return Path(target)


def secure_unlink(path: Path, *, root: Path, missing_ok: bool = False) -> None:
    with _parent_fd(root, path) as (descriptor, name):
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise


def secure_read_text(path: Path, *, root: Path) -> str:
    with _parent_fd(root, path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.ELOOP, "secure file must be regular", str(path))
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)


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

    def repair(descriptor: int, display: Path) -> None:
        os.fchmod(descriptor, DIR_MODE)
        for name in os.listdir(descriptor):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child_display = display / name
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                child = _open_directory(name, dir_fd=descriptor)
                try:
                    repair(child, child_display)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise OSError(
                    errno.ELOOP, "run-owned path has unsafe file type", str(child_display)
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                mode = 0o700 if info.st_mode & 0o111 else FILE_MODE
                os.fchmod(child, mode)
            finally:
                os.close(child)

    with _directory_fd(base.parent, base) as root_descriptor:
        repair(root_descriptor, base)
