"""Declarative per-CLI records and the argv they produce.

Adapters are data, not code, so adding a friend is adding a TOML file. The
awkward parts encoded here are all verified CLI behaviors rather than
speculation — see the spec's "verified invocation traps" section.
"""

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

from .errors import UsageError
from .normalize import Envelope, parse_envelope


@dataclass(frozen=True)
class AuthMarkers:
    """Where an adapter's structured output says "not authenticated".

    `paths` are dotted paths into the parsed payload, each with the value
    that means auth failure -- e.g. `("error.type", "authentication_error")`.
    `exit_codes` are statuses this CLI uses exclusively for auth.

    Both empty means "unclassifiable", which is the honest default until
    someone captures a real auth failure from that CLI.
    """

    paths: tuple[tuple[str, str], ...] = ()
    exit_codes: tuple[int, ...] = ()
    remediation: str = ""

    def declared(self) -> bool:
        return bool(self.paths or self.exit_codes)


def parse_auth(data: dict[str, Any] | None) -> AuthMarkers:
    """Build AuthMarkers from an adapter TOML's `[auth]` table."""
    if not data:
        return AuthMarkers()
    paths = tuple(
        (str(rule["path"]), str(rule["equals"]))
        for rule in data.get("markers", [])
        if isinstance(rule, dict) and "path" in rule and "equals" in rule
    )
    codes = tuple(int(c) for c in data.get("exit_codes", []) if isinstance(c, int))
    return AuthMarkers(paths=paths, exit_codes=codes, remediation=str(data.get("remediation", "")))


@dataclass(frozen=True)
class Adapter:
    name: str
    binary: str
    base_argv: list[str]
    prompt_mode: str  # stdin | trailing-arg | flag-value
    prompt_flag: str
    readonly_argv: list[str]
    schema_flag: str
    model_flag: str
    internal_timeout_flag: str
    effort_kind: str  # native | unverified | none
    effort: dict[str, list[str]] = field(default_factory=dict)
    transport: str = "exec"  # exec | http
    endpoint: str = ""
    # Whether this CLI is asked (via a flag in base_argv, e.g.
    # --output-format json) to wrap its answer in structured output of its
    # own. Declared explicitly rather than inferred from base_argv/schema_flag
    # so that "did this adapter ask for structured output" never has to be
    # guessed by pattern-matching flag spellings -- see normalize.py's
    # `structured_output` parameter for what this drives.
    structured_output: bool = False
    # Declarative "where the answer lives inside the wrapper" (see
    # normalize.Envelope). None means the shape is unknown/unverified;
    # normalize() falls back to scanning stdout directly rather than
    # guessing one.
    envelope: Envelope | None = None
    # §12.2: paths this CLI genuinely needs to read when it runs under OS
    # confinement -- its configuration and credential locations. Declared
    # per-adapter rather than guessed, because a sandbox missing a
    # credential path does not fail loudly: the CLI starts, fails to
    # authenticate, and looks like a broken friend. `~` is expanded at
    # policy-construction time so these stay portable between machines.
    #
    # Empty is meaningful: an adapter with a real readonly mode is trusted
    # to confine itself (§11) and never reaches the sandbox at all.
    sandbox_read: tuple[str, ...] = ()
    # §14: where this CLI's own structured output says "not authenticated".
    # Empty means unclassifiable, which is the honest default until someone
    # captures a real auth failure -- guessing at stderr substrings is what
    # §14 explicitly rejects.
    auth: "AuthMarkers" = field(default_factory=lambda: AuthMarkers())


@dataclass(frozen=True)
class Capability:
    schema: bool
    readonly: bool
    effort: str  # native | unverified | none


@dataclass(frozen=True)
class FriendSpec:
    name: str
    cli: str
    lens: str
    model: str | None
    effort: str | None
    scope: str  # repo | doc
    timeout: int


def load_adapters(directory: Path) -> dict[str, Adapter]:
    directory = Path(directory)
    if not directory.is_dir():
        raise UsageError(f"adapter directory not found: {directory}")

    registry: dict[str, Adapter] = {}
    sources: dict[str, Path] = {}
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "name" not in data:
            raise UsageError(f"{path}: adapter TOML is missing required field 'name'")
        name = data["name"]
        if name in registry:
            raise UsageError(
                f"duplicate adapter name {name!r}: declared in both {sources[name]} and {path}"
            )
        sources[name] = path
        registry[name] = Adapter(
            name=name,
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
            sandbox_read=tuple(data.get("sandbox", {}).get("read", [])),
            auth=parse_auth(data.get("auth")),
            structured_output=bool(data.get("structured_output", False)),
            envelope=parse_envelope(data.get("envelope")),
        )
    return registry


def build_argv(
    adapter: Adapter, spec: FriendSpec, prompt_file: Path, schema_file: Path
) -> tuple[list[str], str | None, Capability]:
    """Return (argv, stdin_text, capability).

    Flag order matters: for adapters whose prompt is a flag *value*, every
    other flag must precede it, because a flag appearing after the prompt flag
    is swallowed as the prompt and the real prompt becomes an ignored
    positional — with a zero exit status.

    Capability is computed from the flags this function actually decides to
    emit, never by scanning the finished argv. The prompt text placed into
    that argv is the untrusted document under review; a document that
    happens to contain a flag's literal text (e.g. "Read,Grep,Glob") must
    not be able to forge a capability by being present in the argv list.
    """
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    argv = [adapter.binary, *adapter.base_argv]

    readonly_emitted = bool(spec.scope == "repo" and adapter.readonly_argv)
    if readonly_emitted:
        argv += adapter.readonly_argv

    schema_emitted = bool(adapter.schema_flag)
    if schema_emitted:
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

    capability = Capability(
        schema=schema_emitted,
        readonly=readonly_emitted,
        effort=adapter.effort_kind,
    )

    if adapter.prompt_mode == "stdin":
        return argv, prompt, capability
    if adapter.prompt_mode == "trailing-arg":
        return [*argv, prompt], None, capability
    if adapter.prompt_mode == "flag-value":
        return [*argv, adapter.prompt_flag, prompt], None, capability
    raise UsageError(f"unknown prompt_mode {adapter.prompt_mode!r}")
