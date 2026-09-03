"""Safe user-level default review-profile preference."""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .errors import UsageError
from .jsonio import read_bounded_bytes
from .outcomes import json_node_count
from .reviewprofiles import names as builtin_profile_names

CONFIG_VERSION = 1
DEFAULT_PROFILE = "quick"
MAX_SESSION_CONFIG_BYTES = 256 * 1024
_TOP_LEVEL_KEYS = frozenset({"version", "default_profile"})
_NO_VALUE = object()


@dataclass(frozen=True)
class SessionConfig:
    default_profile: str = DEFAULT_PROFILE


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the dedicated session preference file outside provider config."""
    source = os.environ if env is None else env
    configured = source.get("XDG_CONFIG_HOME")
    fallback = Path.home() / ".config"
    candidate = Path(configured).expanduser() if configured else fallback
    root = candidate if candidate.is_absolute() else fallback
    return root / "adversarial-friends" / "session.json"


def _invalid(path: Path, field: str, detail: str, *, got: object = _NO_VALUE) -> UsageError:
    suffix = "" if got is _NO_VALUE else f"; got {got!r}"
    return UsageError(f"{path}: {field}: {detail}{suffix}")


def _known_names(known: Iterable[str]) -> set[str]:
    return set(known)


def _validate_profile(path: Path, value: object, known: set[str]) -> str:
    if not isinstance(value, str):
        raise _invalid(path, "default_profile", "must be a string", got=value)
    if value not in known:
        raise _invalid(
            path,
            "default_profile",
            f"must be one of {sorted(known)}",
            got=value,
        )
    return value


def load(
    known: Iterable[str] = builtin_profile_names(),
    env: Mapping[str, str] | None = None,
) -> SessionConfig:
    """Load a strict, versioned preference document; absent means ``quick``."""
    known_names = _known_names(known)
    path = config_path(env)
    try:
        payload = read_bounded_bytes(
            path,
            label="session configuration",
            max_bytes=MAX_SESSION_CONFIG_BYTES,
        )
    except FileNotFoundError:
        return SessionConfig()
    except UsageError:
        raise
    except OSError as exc:
        raise UsageError(f"{path}: cannot read session configuration: {exc}") from exc
    try:
        contents = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path}: invalid session configuration: {exc}") from exc
    try:
        data = json.loads(contents)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if not isinstance(exc, json.JSONDecodeError):
            raise UsageError(f"{path}: malformed JSON within bounds: {exc}") from exc
        raise UsageError(f"{path}: malformed JSON: {exc.msg}") from exc
    try:
        json_node_count(data, "session configuration")
    except (RecursionError, TypeError, ValueError) as exc:
        raise UsageError(f"{path}: session configuration exceeds JSON bounds: {exc}") from exc
    if not isinstance(data, dict):
        raise _invalid(path, "top-level", "must be an object", got=data)
    if set(data) != _TOP_LEVEL_KEYS:
        raise _invalid(
            path,
            "top-level keys",
            f"must be exactly {sorted(_TOP_LEVEL_KEYS)}",
            got=sorted(data),
        )
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CONFIG_VERSION:
        raise _invalid(path, "version", f"must be {CONFIG_VERSION}", got=version)
    return SessionConfig(_validate_profile(path, data["default_profile"], known_names))


def _payload(config: SessionConfig) -> dict[str, object]:
    return {"version": CONFIG_VERSION, "default_profile": config.default_profile}


def _fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _write_locked(config: SessionConfig, env: Mapping[str, str] | None = None) -> None:
    path = config_path(env)
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(_payload(config), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UsageError(f"{path}: cannot write session configuration: {exc}") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


@contextmanager
def _update_lock(env: Mapping[str, str] | None = None) -> Iterator[None]:
    path = config_path(env)
    lock_path = path.with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
    except OSError as exc:
        raise UsageError(f"{lock_path}: cannot lock session configuration: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def set_default(
    profile: str,
    *,
    known: Iterable[str] = builtin_profile_names(),
    env: Mapping[str, str] | None = None,
) -> None:
    """Persist one known default profile using an atomic locked update."""
    known_names = _known_names(known)
    path = config_path(env)
    validated = _validate_profile(path, profile, known_names)
    with _update_lock(env):
        load(known_names, env)
        _write_locked(SessionConfig(default_profile=validated), env)
