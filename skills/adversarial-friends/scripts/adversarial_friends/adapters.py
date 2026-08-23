"""Declarative per-CLI records and the argv they produce.

Adapters are data, not code, so adding a friend is adding a TOML file. The
awkward parts encoded here are all verified CLI behaviors rather than
speculation — see the spec's "verified invocation traps" section.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError


@dataclass(frozen=True)
class Adapter:
    name: str
    binary: str
    base_argv: list[str]
    prompt_mode: str            # stdin | trailing-arg | flag-value
    prompt_flag: str
    readonly_argv: list[str]
    schema_flag: str
    model_flag: str
    internal_timeout_flag: str
    effort_kind: str            # native | unverified | none
    effort: dict[str, list[str]] = field(default_factory=dict)
    transport: str = "exec"     # exec | http
    endpoint: str = ""


@dataclass(frozen=True)
class Capability:
    schema: bool
    readonly: bool
    effort: str                 # native | unverified | none


@dataclass(frozen=True)
class FriendSpec:
    name: str
    cli: str
    lens: str
    model: str | None
    effort: str | None
    scope: str                  # repo | doc
    timeout: int


def load_adapters(directory: Path) -> dict[str, Adapter]:
    registry: dict[str, Adapter] = {}
    for path in sorted(Path(directory).glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        registry[data["name"]] = Adapter(
            name=data["name"],
            binary=data.get("binary", ""),
            base_argv=list(data.get("base_argv", [])),
            prompt_mode=data.get("prompt_mode", "stdin"),
            prompt_flag=data.get("prompt_flag", ""),
            readonly_argv=list(data.get("readonly_argv", [])),
            schema_flag=data.get("schema_flag", ""),
            model_flag=data.get("model_flag", ""),
            internal_timeout_flag=data.get("internal_timeout_flag", ""),
            effort_kind=data.get("effort_kind", "none"),
            effort={k: list(v) for k, v in data.get("effort", {}).items()},
            transport=data.get("transport", "exec"),
            endpoint=data.get("endpoint", ""),
        )
    return registry


def build_argv(adapter: Adapter, spec: FriendSpec, prompt_file: Path,
               schema_file: Path) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text).

    Flag order matters: for adapters whose prompt is a flag *value*, every
    other flag must precede it, because a flag appearing after the prompt flag
    is swallowed as the prompt and the real prompt becomes an ignored
    positional — with a zero exit status.
    """
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    argv = [adapter.binary, *adapter.base_argv]

    if spec.scope == "repo" and adapter.readonly_argv:
        argv += adapter.readonly_argv
    if adapter.schema_flag:
        argv += [adapter.schema_flag, str(schema_file)]
    if spec.model and adapter.model_flag:
        argv += [adapter.model_flag, spec.model]
    if spec.effort:
        if spec.effort not in adapter.effort:
            raise UsageError(
                f"{adapter.name} does not support effort {spec.effort!r} "
                f"(available: {sorted(adapter.effort) or 'none'})"
            )
        argv += adapter.effort[spec.effort]
    if adapter.internal_timeout_flag:
        # The CLI's own timeout is set explicitly rather than inherited, so it
        # cannot silently disagree with the runner's kill deadline.
        argv += [adapter.internal_timeout_flag, f"{spec.timeout}s"]

    if adapter.prompt_mode == "stdin":
        return argv, prompt
    if adapter.prompt_mode == "trailing-arg":
        return argv + [prompt], None
    if adapter.prompt_mode == "flag-value":
        return argv + [adapter.prompt_flag, prompt], None
    raise UsageError(f"unknown prompt_mode {adapter.prompt_mode!r}")


def capability_for(adapter: Adapter, argv: list[str]) -> Capability:
    """Capabilities come from the argv actually used, never from defaults."""
    readonly = bool(adapter.readonly_argv) and all(
        token in argv for token in adapter.readonly_argv
    )
    schema = bool(adapter.schema_flag) and adapter.schema_flag in argv
    return Capability(schema=schema, readonly=readonly, effort=adapter.effort_kind)
