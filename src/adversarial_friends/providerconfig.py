"""User-level enabled/model defaults for provider adapters."""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .errors import UsageError
from .trust import MODEL_RE

CONFIG_VERSION = 1
_TOP_LEVEL_KEYS = frozenset({"version", "providers"})
_PROVIDER_KEYS = frozenset({"enabled", "model"})
_NO_VALUE = object()


@dataclass(frozen=True)
class ProviderSetting:
    enabled: bool = True
    model: str | None = None


@dataclass(frozen=True)
class ProviderPolicy:
    providers: dict[str, ProviderSetting]

    def setting(self, name: str) -> ProviderSetting:
        return self.providers.get(name, ProviderSetting())


def config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    configured = source.get("XDG_CONFIG_HOME")
    fallback = Path.home() / ".config"
    candidate = Path(configured).expanduser() if configured else fallback
    root = candidate if candidate.is_absolute() else fallback
    return root / "adversarial-friends" / "config.json"


def _invalid(path: Path, field: str, detail: str, *, got: object = _NO_VALUE) -> UsageError:
    suffix = "" if got is _NO_VALUE else f"; got {got!r}"
    return UsageError(f"{path}: {field}: {detail}{suffix}")


def _validate_model(path: Path, field: str, model: object) -> str | None:
    if model is None:
        return None
    if not isinstance(model, str) or MODEL_RE.fullmatch(model) is None:
        raise _invalid(
            path,
            field,
            f"must be null or match {MODEL_RE.pattern!r}",
            got=model,
        )
    return model


def load(known: Iterable[str], env: Mapping[str, str] | None = None) -> ProviderPolicy:
    known_names = set(known)
    path = config_path(env)
    defaults = {name: ProviderSetting() for name in sorted(known_names)}
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProviderPolicy(defaults)
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path}: invalid provider configuration: {exc}") from exc
    except OSError as exc:
        raise UsageError(f"{path}: cannot read configuration: {exc}") from exc

    try:
        data = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: malformed JSON: {exc.msg}") from exc
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
    raw_providers = data["providers"]
    if not isinstance(raw_providers, dict):
        raise _invalid(path, "providers", "must be an object", got=raw_providers)

    settings = defaults
    for name, raw_setting in raw_providers.items():
        field = f"providers.{name}"
        if name not in known_names:
            raise _invalid(path, field, "unknown provider", got=name)
        if not isinstance(raw_setting, dict):
            raise _invalid(path, field, "must be an object", got=raw_setting)
        unexpected = set(raw_setting) - _PROVIDER_KEYS
        if unexpected:
            key = sorted(unexpected)[0]
            raise _invalid(
                path, f"{field}.{key}", "unexpected provider keys", got=sorted(unexpected)
            )
        enabled = raw_setting.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _invalid(path, f"{field}.enabled", "must be a boolean", got=enabled)
        model = _validate_model(path, f"{field}.model", raw_setting.get("model"))
        settings[name] = ProviderSetting(enabled=enabled, model=model)
    return ProviderPolicy(settings)


def _payload(policy: ProviderPolicy) -> dict[str, object]:
    return {
        "version": CONFIG_VERSION,
        "providers": {
            name: {"enabled": setting.enabled, "model": setting.model}
            for name, setting in sorted(policy.providers.items())
        },
    }


def _write_locked(policy: ProviderPolicy, env: Mapping[str, str] | None = None) -> None:
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
            json.dump(_payload(policy), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UsageError(f"{path}: cannot write configuration: {exc}") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


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
        raise UsageError(f"{lock_path}: cannot lock provider configuration: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _known_provider(name: str, known: set[str], path: Path) -> None:
    if name not in known:
        raise _invalid(path, "provider", f"must be one of {sorted(known)}", got=name)


def set_enabled(
    name: str,
    enabled: bool,
    *,
    known: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> None:
    known_names = set(known)
    path = config_path(env)
    _known_provider(name, known_names, path)
    if not isinstance(enabled, bool):
        raise _invalid(path, f"providers.{name}.enabled", "must be a boolean", got=enabled)
    with _update_lock(env):
        policy = load(known_names, env)
        settings = dict(policy.providers)
        settings[name] = ProviderSetting(enabled=enabled, model=policy.setting(name).model)
        _write_locked(ProviderPolicy(settings), env)


def set_model(
    name: str,
    model: str | None,
    *,
    known: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> None:
    known_names = set(known)
    path = config_path(env)
    _known_provider(name, known_names, path)
    validated = _validate_model(path, f"providers.{name}.model", model)
    with _update_lock(env):
        policy = load(known_names, env)
        settings = dict(policy.providers)
        settings[name] = ProviderSetting(enabled=policy.setting(name).enabled, model=validated)
        _write_locked(ProviderPolicy(settings), env)
