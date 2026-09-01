"""Declarative per-CLI records and the argv they produce.

Adapters are data, not code, so adding a friend is adding a TOML file. The
awkward parts encoded here are all verified CLI behaviors rather than
speculation — see the spec's "verified invocation traps" section.
"""

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

from .authority import ExternalToolPolicy, enforce
from .envelopes import Envelope, parse_envelope
from .errors import UsageError
from .workspaceassets import WorkspaceAsset, WorkspaceAssetAudit, parse_workspace_assets


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
    # Substrings of stderr that mean auth failure. Allowed ONLY as a string
    # captured verbatim from a real failure of that CLI, never a guess at
    # what it might say: the first real capture (agy) carried the marker
    # nowhere else -- exit 1, shared with unrelated errors, and empty
    # stdout. See failures.py for why the substring must be the specific
    # one and not "auth".
    stderr: tuple[str, ...] = ()
    remediation: str = ""

    def declared(self) -> bool:
        return bool(self.paths or self.exit_codes or self.stderr)


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
    stderr = tuple(str(s) for s in data.get("stderr_contains", []) if isinstance(s, str) and s)
    return AuthMarkers(
        paths=paths, exit_codes=codes, stderr=stderr, remediation=str(data.get("remediation", ""))
    )


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
    # Whether schema_flag takes the schema TEXT rather than a path to it.
    # claude's `--json-schema <schema>` wants the JSON itself; handed a
    # path it fails before the model sees anything ("--json-schema is not
    # valid JSON: Unrecognized token '/'"). Declared, not inferred, for the
    # same reason as everything else here: the two forms are
    # indistinguishable from the flag's spelling.
    schema_inline: bool = False
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
    # §12.2: paths this CLI must WRITE to in order to start at all, outside
    # its isolation directory. Empty for every shipped adapter, and that is
    # the point: opencode earned one by dying without a writable log
    # directory, and then stopped needing it once `childenv.private_dirs`
    # pointed its state at a private directory beside the isolation
    # directory instead. Redirecting a CLI's own notion of where state
    # lives beats punching a hole in the boundary, so reach for this only
    # when redirection has failed.
    sandbox_write: tuple[str, ...] = ()
    # Opt in to OS confinement even though this CLI has a read-only mode of
    # its own. A read-only flag stops a friend WRITING; it does nothing about
    # what it can read, so a self-confining CLI can still open ~/.ssh. The
    # opt-in is per adapter rather than blanket because confinement breaks a
    # CLI whose credentials the sandbox cannot reach -- claude keeps its own
    # in the macOS Keychain, and granting that would hand a friend every
    # credential the operator has. Set only for a CLI verified to run
    # confined with the grants above.
    sandbox_confine: bool = False
    # §14: where this CLI's own structured output says "not authenticated".
    # Empty means unclassifiable, which is the honest default until someone
    # captures a real auth failure -- guessing at stderr substrings is what
    # §14 explicitly rejects.
    auth: "AuthMarkers" = field(default_factory=lambda: AuthMarkers())
    # §12.2: environment variables this CLI genuinely needs when it runs
    # confined. Its own credentials, essentially -- §12.3 already accepts
    # that a friend can exfiltrate those. Everything else is withheld.
    env_pass: tuple[str, ...] = ()
    # Flags this CLI needs in DOC scope, where its working directory holds a
    # copy of the artifact and nothing else. Some CLIs refuse to start
    # outside a git repository at all, which makes doc scope unusable for
    # them -- and every friend is downgraded to doc scope whenever the
    # artifact is not inside one. Emitted only for doc scope, and never
    # anything that grants access: see the note in codex.toml.
    doc_argv: tuple[str, ...] = ()
    # Provider-managed integrations are a separate authority boundary from
    # filesystem confinement. Missing declarations stay unknown for API and
    # legacy-TOML compatibility; shipped adapters declare one explicitly.
    external_tools: str = "unknown"  # none | deny-argv | uncontrolled | unknown
    deny_external_tools_argv: tuple[str, ...] = ()
    external_tool_sources: tuple[str, ...] = ()
    # A no-model argv that proves this installed executable accepts the deny
    # flags, plus bounded markers expected in its help/version output.
    deny_external_tools_probe_argv: tuple[str, ...] = ()
    deny_external_tools_probe_markers: tuple[str, ...] = ()
    workspace_assets: tuple[WorkspaceAsset, ...] = ()


@dataclass(frozen=True)
class Capability:
    schema: bool
    readonly: bool
    effort: str  # native | unverified | none
    external_tools: str = "unknown"
    external_tool_sources: tuple[str, ...] = ()
    deny_external_tools_argv: tuple[str, ...] = ()
    workspace_assets: tuple[WorkspaceAssetAudit, ...] = ()


@dataclass(frozen=True)
class FriendSpec:
    name: str
    cli: str
    lens: str
    model: str | None
    effort: str | None
    scope: str  # repo | doc
    timeout: int
    independent: bool = True
    host_self_review: bool = False


_MAX_CAPABILITY_PROBE_ARGS = 32
_MAX_CAPABILITY_PROBE_ARG_CHARS = 256
_MAX_CAPABILITY_PROBE_MARKERS = 16
_SAFE_CAPABILITY_PROBE_ACTIONS = {"--help", "--version", "help", "version"}


def _validate_capability_probe(path: Path, probe_argv: list[str], probe_markers: list[str]) -> None:
    """Keep adapter probes bounded and structurally incapable of a model call."""
    if (
        len(probe_argv) > _MAX_CAPABILITY_PROBE_ARGS
        or any(
            len(value) > _MAX_CAPABILITY_PROBE_ARG_CHARS
            or any(character in value for character in ("\x00", "\n", "\r"))
            for value in probe_argv
        )
        or probe_argv[-1] not in _SAFE_CAPABILITY_PROBE_ACTIONS
    ):
        raise UsageError(f"{path}: deny capability probe must be bounded and end in help/version")
    if len(probe_markers) > _MAX_CAPABILITY_PROBE_MARKERS or any(
        len(marker) > _MAX_CAPABILITY_PROBE_ARG_CHARS
        or any(character in marker for character in ("\x00", "\n", "\r"))
        for marker in probe_markers
    ):
        raise UsageError(f"{path}: deny capability probe markers must be bounded")


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
        external_tools = data.get("external_tools", "unknown")
        deny_argv = data.get("deny_external_tools_argv", [])
        tool_sources = data.get("external_tool_sources", [])
        probe_argv = data.get("deny_external_tools_probe_argv", [])
        probe_markers = data.get("deny_external_tools_probe_markers", [])
        transport = data.get("transport", "exec")
        workspace_assets = parse_workspace_assets(
            data.get("workspace_assets", []), transport=transport
        )
        if not isinstance(external_tools, str) or external_tools not in {
            "unknown",
            "none",
            "deny-argv",
            "uncontrolled",
        }:
            raise UsageError(f"{path}: invalid external_tools declaration")
        if not isinstance(deny_argv, list) or not all(
            isinstance(value, str) for value in deny_argv
        ):
            raise UsageError(f"{path}: deny_external_tools_argv must be a list of strings")
        if not isinstance(tool_sources, list) or not all(
            isinstance(value, str) and value for value in tool_sources
        ):
            raise UsageError(f"{path}: external_tool_sources must be a list of strings")
        if not isinstance(probe_argv, list) or not all(
            isinstance(value, str) for value in probe_argv
        ):
            raise UsageError(f"{path}: deny_external_tools_probe_argv must be a list of strings")
        if not isinstance(probe_markers, list) or not all(
            isinstance(value, str) and value for value in probe_markers
        ):
            raise UsageError(f"{path}: deny_external_tools_probe_markers must be a list of strings")
        if external_tools == "deny-argv" and not deny_argv:
            raise UsageError(
                f"{path}: external_tools='deny-argv' requires deny_external_tools_argv"
            )
        if external_tools == "deny-argv" and (not probe_argv or not probe_markers):
            raise UsageError(
                f"{path}: external_tools='deny-argv' requires a capability probe and markers"
            )
        if probe_argv:
            _validate_capability_probe(path, probe_argv, probe_markers)
        if external_tools != "deny-argv" and deny_argv:
            raise UsageError(
                f"{path}: deny_external_tools_argv is only valid with external_tools='deny-argv'"
            )
        if external_tools != "deny-argv" and (probe_argv or probe_markers):
            raise UsageError(
                f"{path}: deny capability probe is only valid with external_tools='deny-argv'"
            )
        registry[name] = Adapter(
            name=name,
            binary=data.get("binary", ""),
            base_argv=list(data.get("base_argv", [])),
            prompt_mode=data.get("prompt_mode", "stdin"),
            prompt_flag=data.get("prompt_flag", ""),
            readonly_argv=list(data.get("readonly_argv", [])),
            schema_flag=data.get("schema_flag", ""),
            schema_inline=bool(data.get("schema_inline", False)),
            model_flag=data.get("model_flag", ""),
            internal_timeout_flag=data.get("internal_timeout_flag", ""),
            effort_kind=data.get("effort_kind", "none"),
            effort={k: list(v) for k, v in data.get("effort", {}).items()},
            transport=transport,
            endpoint=data.get("endpoint", ""),
            sandbox_read=tuple(data.get("sandbox", {}).get("read", [])),
            sandbox_write=tuple(data.get("sandbox", {}).get("write", [])),
            sandbox_confine=bool(data.get("sandbox", {}).get("os_confine", False)),
            auth=parse_auth(data.get("auth")),
            env_pass=tuple(data.get("env", {}).get("pass", [])),
            doc_argv=tuple(data.get("doc_argv", [])),
            structured_output=bool(data.get("structured_output", False)),
            envelope=parse_envelope(data.get("envelope")),
            external_tools=external_tools,
            deny_external_tools_argv=tuple(deny_argv),
            external_tool_sources=tuple(tool_sources),
            deny_external_tools_probe_argv=tuple(probe_argv),
            deny_external_tools_probe_markers=tuple(probe_markers),
            workspace_assets=workspace_assets,
        )
    return registry


def build_argv(
    adapter: Adapter,
    spec: FriendSpec,
    prompt_file: Path,
    schema_file: Path,
    external_tool_policy: ExternalToolPolicy,
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
    authority = enforce(adapter, external_tool_policy)

    # A friend never needs to write, in EITHER scope: it reads the artifact
    # (plus, at repo scope, the checkout) and returns findings on stdout.
    #
    # Doc scope used to omit this on the reasoning that there is no repo to
    # protect. There is still a filesystem. Doc scope is also exactly where
    # a readonly-capable CLI gets no OS confinement either, so omitting it
    # left the friend unconfined by anything at all. Measured against the
    # real codex in a bare directory with no --sandbox flag: asked to write
    # outside its working directory, it did so on the first attempt.
    readonly_emitted = bool(adapter.readonly_argv)
    if readonly_emitted:
        argv += adapter.readonly_argv
    if spec.scope == "doc":
        # Never anything that grants access -- these exist because a CLI may
        # refuse to START outside a git repository, and doc scope is a bare
        # directory. See codex.toml for the one case and its evidence.
        argv += adapter.doc_argv

    schema_emitted = bool(adapter.schema_flag)
    if schema_emitted:
        schema_value = (
            Path(schema_file).read_text(encoding="utf-8")
            if adapter.schema_inline
            else str(schema_file)
        )
        argv += [adapter.schema_flag, schema_value]

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

    # Emitted while argv is still entirely options. Prompt placement below
    # may append an untrusted trailing argument or a prompt flag/value pair.
    argv += authority.argv

    capability = Capability(
        schema=schema_emitted,
        readonly=readonly_emitted,
        effort=adapter.effort_kind,
        external_tools=authority.status,
        external_tool_sources=authority.sources,
        deny_external_tools_argv=authority.argv,
    )

    if adapter.prompt_mode == "stdin":
        return argv, prompt, capability
    if adapter.prompt_mode == "trailing-arg":
        return [*argv, prompt], None, capability
    if adapter.prompt_mode == "flag-value":
        return [*argv, adapter.prompt_flag, prompt], None, capability
    raise UsageError(f"unknown prompt_mode {adapter.prompt_mode!r}")


def place_extra_args(argv: list[str], adapter: Adapter, extra_args: list[str]) -> list[str]:
    """Insert operator flags where a FLAG goes for this adapter's prompt mode.

    Appending them to the end was wrong for two of the five shipped adapters,
    and wrong in the way `build_argv`'s own docstring warns about: the prompt
    is not always last, and a token after it is not a flag any more.

    * `stdin` -- the prompt never enters argv, so the end is a flag position
      and nothing moves. codex.
    * `trailing-arg` -- the prompt IS the last element. Appending put the
      operator's flags after it, turning them into stray positionals and
      displacing the prompt from the position the CLI reads it from. claude.
    * `flag-value` -- the prompt is the value of `prompt_flag`. Appending put
      the flags after that value, where they are positionals rather than
      options. agy.

    So `--unsafe-extra-args` silently did nothing for agy and claude, or
    corrupted the invocation, rather than failing in a way anyone would see.
    Raised as a deadlocked claim by a cross-examination of dispatch.py --
    judges split because neither ran it; the argv settles it.
    """
    if not extra_args:
        return argv
    if adapter.prompt_mode == "trailing-arg" and argv:
        return [*argv[:-1], *extra_args, argv[-1]]
    if adapter.prompt_mode == "flag-value" and adapter.prompt_flag in argv:
        cut = argv.index(adapter.prompt_flag)
        return [*argv[:cut], *extra_args, *argv[cut:]]
    # stdin, or a shape this does not recognise: the end is a flag position.
    return [*argv, *extra_args]


def friend_key(spec: FriendSpec) -> str:
    """A friend's ledger identity: what round 1 writes into a claim's
    `origin`, and what judging matches against to decide who may judge
    what.

    Deliberately NOT `spec.name`: names carry a positional index
    (`codex-ops-0`) that the ledger does not, and a resumed or looped run
    must recognise its own earlier claims. The roster unit is
    `(cli, model, effort, lens)` (§8.1), so model and effort are part of the
    identity whenever they are set: two models on one CLI under one lens
    are two judges. They were one until a crossexam of verdicts.py showed
    what that cost -- quorum counted both, `latest_per_judge` kept one, and
    the order of `--friend` flags decided which, and so whether a gate
    cleared.
    """
    return _friend_key_values(spec.cli, spec.lens, spec.model, spec.effort)


def independent_friend_keys(specs: list[FriendSpec]) -> list[str]:
    """Unique ledger identities allowed to affect judging outcomes."""
    keys: list[str] = []
    for spec in specs:
        key = friend_key(spec)
        if spec.independent and key not in keys:
            keys.append(key)
    return keys


def _friend_key_values(cli: str, lens: str, model: str | None, effort: str | None) -> str:
    key = f"{cli}/{lens}"
    if model:
        key += f"@{model}"
    if effort:
        key += f"+{effort}"
    return key


def validate_roster_uniqueness(specs: list[FriendSpec], *, judging: bool) -> None:
    """Enforce the path-name and ledger-identity invariants of a final roster."""
    rows = [(spec.name, spec.cli, spec.lens, spec.model, spec.effort) for spec in specs]
    _validate_roster_identity_rows(rows, judging=judging)


def validate_roster_entry_uniqueness(entries: list[dict[str, Any]], *, judging: bool) -> None:
    """Enforce final-roster uniqueness before constructing ``FriendSpec`` objects."""
    rows = [
        (entry["name"], entry["cli"], entry["lens"], entry.get("model"), entry.get("effort"))
        for entry in entries
    ]
    _validate_roster_identity_rows(rows, judging=judging)


def _validate_roster_identity_rows(
    rows: list[tuple[str, str, str, str | None, str | None]], *, judging: bool
) -> None:
    names: set[str] = set()
    identities: dict[str, str] = {}
    for name, cli, lens, model, effort in rows:
        if name in names:
            raise UsageError(
                f"duplicate friend name {name!r}: names must be unique because "
                "they become output paths"
            )
        names.add(name)
        if not judging:
            continue
        key = _friend_key_values(cli, lens, model, effort)
        if key in identities:
            raise UsageError(
                f"friends {identities[key]!r} and {name!r} are the same friend -- "
                f"cli {cli!r}, lens {lens!r}, model {model!r}, effort "
                f"{effort!r} -- and would share one ledger identity ({key}); "
                "give one a different lens, model, or effort"
            )
        identities[key] = name
