"""Safe user-level default review-profile preference."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field as dataclass_field
import fcntl
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType

from .errors import UsageError
from .jsonio import read_bounded_bytes
from .outcomes import json_node_count
from .reviewprofiles import (
    SAFE_FIELDS,
    get as builtin_profile,
    names as builtin_profile_names,
    resolve as resolve_profile,
    validate_safe_setting,
)

CONFIG_VERSION = 2
_LEGACY_CONFIG_VERSION = 1
DEFAULT_PROFILE = "quick"
MAX_SESSION_CONFIG_BYTES = 256 * 1024
_TOP_LEVEL_KEYS = frozenset({"version", "default_profile", "profiles"})
_LEGACY_TOP_LEVEL_KEYS = frozenset({"version", "default_profile"})
_NO_VALUE = object()


def _empty_profiles() -> Mapping[str, Mapping[str, object]]:
    """Make an immutable, independent custom-profile registry per config."""
    return MappingProxyType({})


@dataclass(frozen=True)
class SessionConfig:
    default_profile: str = DEFAULT_PROFILE
    profiles: Mapping[str, Mapping[str, object]] = dataclass_field(default_factory=_empty_profiles)


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


def _validate_profile_name(path: Path, name: object) -> str:
    if not isinstance(name, str) or not name:
        raise _invalid(path, "profiles", "profile names must be non-empty strings", got=name)
    # These names appear in JSON and CLI output only, but reserving the built-ins
    # makes it impossible for a local configuration to silently replace them.
    if builtin_profile(name) is not None:
        raise _invalid(path, "profiles", f"cannot redefine built-in profile {name!r}")
    if (
        len(name) > 32
        or not name.replace("_", "").replace("-", "").isalnum()
        or not name[0].isalnum()
        or not name.islower()
    ):
        raise _invalid(path, "profiles", f"invalid profile name {name!r}")
    return name


def _validated_profiles(path: Path, value: object) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(value, dict):
        raise _invalid(path, "profiles", "must be an object", got=value)
    result: dict[str, Mapping[str, object]] = {}
    for raw_name, raw_definition in value.items():
        name = _validate_profile_name(path, raw_name)
        if not isinstance(raw_definition, dict):
            raise _invalid(path, f"profiles.{name}", "must be an object", got=raw_definition)
        allowed = {"base", *SAFE_FIELDS}
        unknown = set(raw_definition) - allowed
        if unknown:
            raise _invalid(path, f"profiles.{name}", f"unknown fields {sorted(unknown)}")
        base = raw_definition.get("base")
        if not isinstance(base, str) or not base:
            raise _invalid(path, f"profiles.{name}.base", "must be a non-empty string", got=base)
        validated: dict[str, object] = {"base": base}
        for field, setting in raw_definition.items():
            if field == "base":
                continue
            try:
                checked = validate_safe_setting(field, setting)
            except UsageError as exc:
                raise _invalid(path, f"profiles.{name}.{field}", str(exc)) from exc
            if field == "lenses":
                assert isinstance(checked, tuple)
                validated[field] = list(checked)
            else:
                validated[field] = checked
        result[name] = MappingProxyType(validated)
    known = set(result) | set(builtin_profile_names())
    for name, definition in result.items():
        base = definition["base"]
        if base not in known:
            raise _invalid(path, f"profiles.{name}.base", f"unknown base {base!r}")
        # resolve validates cycle safety and has no external side effects.
        try:
            resolve_profile(name, result)
        except UsageError as exc:
            raise _invalid(path, f"profiles.{name}", str(exc)) from exc
    return MappingProxyType(result)


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
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid(path, "version", "must be an integer", got=version)
    expected_keys = _LEGACY_TOP_LEVEL_KEYS if version == _LEGACY_CONFIG_VERSION else _TOP_LEVEL_KEYS
    if set(data) != expected_keys:
        raise _invalid(
            path,
            "top-level keys",
            f"must be exactly {sorted(expected_keys)}",
            got=sorted(data),
        )
    if version not in (_LEGACY_CONFIG_VERSION, CONFIG_VERSION):
        raise _invalid(
            path, "version", f"must be {_LEGACY_CONFIG_VERSION} or {CONFIG_VERSION}", got=version
        )
    profiles = (
        MappingProxyType({})
        if version == _LEGACY_CONFIG_VERSION
        else _validated_profiles(path, data["profiles"])
    )
    all_names = known_names | set(profiles)
    return SessionConfig(_validate_profile(path, data["default_profile"], all_names), profiles)


def _payload(config: SessionConfig) -> dict[str, object]:
    return {
        "version": CONFIG_VERSION,
        "default_profile": config.default_profile,
        "profiles": {name: dict(profile) for name, profile in config.profiles.items()},
    }


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
    path = config_path(env)
    with _update_lock(env):
        config = load(known, env)
        validated = _validate_profile(path, profile, _known_names(known) | set(config.profiles))
        _write_locked(SessionConfig(default_profile=validated, profiles=config.profiles), env)


def _update_profiles(
    change: Callable[[dict[str, dict[str, object]], SessionConfig], None],
    *,
    env: Mapping[str, str] | None = None,
) -> SessionConfig:
    """Load, validate and atomically replace a profile map under one lock."""
    with _update_lock(env):
        config = load(env=env)
        mutable = {name: dict(definition) for name, definition in config.profiles.items()}
        change(mutable, config)
        # Route prospective state through the same strict JSON contract used
        # for disk input before it can be committed.
        path = config_path(env)
        checked = _validated_profiles(path, mutable)
        default = _validate_profile(
            path, config.default_profile, set(builtin_profile_names()) | set(checked)
        )
        updated = SessionConfig(default_profile=default, profiles=checked)
        _write_locked(updated, env)
        return updated


def create_profile(
    name: str,
    base: str,
    settings: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Create a custom profile that inherits a built-in or custom profile."""

    def change(profiles: dict[str, dict[str, object]], _config: SessionConfig) -> None:
        if name in profiles or builtin_profile(name) is not None:
            raise UsageError(f"review profile {name!r} already exists")
        profiles[name] = {"base": base, **dict(settings)}

    _update_profiles(change, env=env)


def update_profile(
    name: str,
    settings: Mapping[str, object],
    *,
    base: str | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Change just named safe values on one existing custom profile."""

    def change(profiles: dict[str, dict[str, object]], _config: SessionConfig) -> None:
        if name not in profiles:
            raise UsageError(f"unknown custom review profile {name!r}")
        if base is not None:
            profiles[name]["base"] = base
        profiles[name].update(settings)

    _update_profiles(change, env=env)


def delete_profile(name: str, *, env: Mapping[str, str] | None = None) -> None:
    """Delete a custom profile unless it is the selected default or a base."""

    def change(profiles: dict[str, dict[str, object]], config: SessionConfig) -> None:
        if name not in profiles:
            raise UsageError(f"unknown custom review profile {name!r}")
        if config.default_profile == name:
            raise UsageError(f"cannot delete the current default profile {name!r}")
        dependents = sorted(other for other, profile in profiles.items() if profile["base"] == name)
        if dependents:
            raise UsageError(f"cannot delete profile {name!r}; used as a base by {dependents}")
        del profiles[name]

    _update_profiles(change, env=env)
