# Contract-First Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship adversarial-friends 0.2.1 with shared readiness, provider-policy, external-authority, snapshot, novelty, and terminal-outcome contracts that close every defect in the 0.2.0 dogfood report.

**Architecture:** Add small stdlib-only domain modules at the boundaries and make existing commands consume them. Provider resolution and `doctor` share one readiness assessment; dispatch consumes an explicit external-tool policy; resume consumes a verified snapshot identity; and all terminal projections consume one `RunOutcome`. Exact ledger identities remain unchanged while a separate conservative theme tracker controls loop novelty.

**Tech Stack:** Python 3.11+, standard library only, argparse, dataclasses, JSON/TOML, pytest, mypy strict, Ruff, Make, Git worktrees, Codex plugin payload mirror.

**Design:** `docs/superpowers/specs/2026-08-30-contract-first-hardening-design.md`

---

## File Map

New focused modules:

- `src/adversarial_friends/providerconfig.py` — user-owned provider preferences and atomic updates.
- `src/adversarial_friends/readiness.py` — the canonical `FriendReadiness` assessment used by discovery and doctor.
- `src/adversarial_friends/authority.py` — external-tool policy values and adapter enforcement checks.
- `src/adversarial_friends/snapshots.py` — immutable snapshot identity creation, migration, and verification.
- `src/adversarial_friends/outcomes.py` — terminal stop reason, exit selection, lifecycle, and metadata projection.
- `src/adversarial_friends/themes.py` — conservative loop theme signatures and advisory duplicate proposals.
- `src/adversarial_friends/commands/providers.py` — `afriend providers` management command.

Existing integration points:

- `src/adversarial_friends/cliargs.py`, `cli.py` — new provider and authority CLI surface.
- `src/adversarial_friends/roster.py`, `commands/friends.py`, `commands/doctor.py`, `commands/setup.py` — readiness and provider policy.
- `src/adversarial_friends/adapters.py`, `dispatch.py`, `rounds.py`, `commands/critique.py`, `commands/crossexam.py`, `commands/resume.py` — authority enforcement and coherent dispatch audit.
- `src/adversarial_friends/commands/run.py`, `commands/runmeta.py`, `commands/environment.py`, `commands/haltstate.py`, `commands/exits.py` — snapshot and terminal outcome integration.
- `src/adversarial_friends/report.py` — authority, gate, diagnostic, duplicate-proposal, and escaping output.
- `src/adversarial_friends/assets/adapters/*.toml` and plugin mirror — declarative authority strategies.
- `src/adversarial_friends/assets/SKILL.md` and references — operator-facing provider and security behavior.

Do not grow any Python file beyond the repository's 777-line limit. Prefer moving a complete concern to one of the new modules over adding helpers to `commands/run.py`.

## Task 1: Raise the Repository Line Cap to 777

**Files:**

- Modify: `scripts/check_max_loc.py`
- Modify: `Makefile`
- Create: `tests/test_max_loc.py`
- Modify: current line-cap references under `src/`, `tests/`, `docs/`, and `CHANGELOG.md`

- [ ] **Step 1: Write a failing cap contract test**

Load `scripts/check_max_loc.py` without executing its CLI and pin both the
constant and boundary behavior:

```python
def load_check_max_loc():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_max_loc.py"
    spec = importlib.util.spec_from_file_location("check_max_loc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_file_cap_is_777():
    module = load_check_max_loc()
    assert module.MAX_LINES == 777


def test_violation_boundary_is_strictly_above_777(tmp_path):
    module = load_check_max_loc()
    allowed = tmp_path / "allowed.py"
    rejected = tmp_path / "rejected.py"
    allowed.write_text("x\n" * 777)
    rejected.write_text("x\n" * 778)
    assert module.find_violations([tmp_path]) == [(str(rejected), 778)]
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/test_max_loc.py -q
```

Expected: `MAX_LINES` is still 500 and `find_violations` does not exist.

- [ ] **Step 3: Implement the 777-line enforcement boundary**

Set `MAX_LINES = 777` and extract the script's scan into the testable helper:

```python
def find_violations(directories: Iterable[Path]) -> list[tuple[str, int]]:
    violations = []
    for directory in directories:
        for path in directory.rglob("*.py"):
            count = len(path.read_text().splitlines())
            if count > MAX_LINES:
                violations.append((str(path), count))
    return sorted(violations)
```

Have `main()` call `find_violations(Path(name) for name in DIRS)` and retain
the existing CLI output and exit behavior.

- [ ] **Step 4: Update every semantic reference to the active cap**

Change current enforcement/help/test/source documentation from 500 to 777.
For historical changelog prose, replace the obsolete numeric description
with “the then-current line cap” so history does not falsely claim that 777
was active for an old release. Do not alter unrelated `500` values such as
HTTP status fixtures, byte limits, time examples, or captured CLI output.

- [ ] **Step 5: Verify the focused test, gate, and reference scan**

```bash
uv run pytest tests/test_max_loc.py -q
make max-loc
rg -n "500[- ]line|500 lines|MAX_LINES\\s*=\\s*500|under 500|500-line-per" \
  Makefile scripts src tests docs CHANGELOG.md --glob '!tests/fixtures/**' \
  --glob '!docs/superpowers/plans/2026-08-30-contract-first-hardening.md'
```

Expected: tests and `make max-loc` pass; the reference scan returns no
matches.

- [ ] **Step 6: Commit the cap change**

```bash
git add scripts/check_max_loc.py Makefile tests/test_max_loc.py src tests docs CHANGELOG.md
git commit -m "chore: raise Python file cap to 777 lines"
```

## Task 2: User-Owned Provider Preferences

**Files:**

- Create: `src/adversarial_friends/providerconfig.py`
- Create: `src/adversarial_friends/commands/providers.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/cli.py`
- Create: `tests/test_providerconfig.py`
- Modify: `tests/test_cliargs.py`
- Modify: `tests/test_cli_entry.py`

- [ ] **Step 1: Write failing path, validation, and atomic-update tests**

Add tests that isolate configuration with `XDG_CONFIG_HOME` and prove defaults, valid round trips, invalid JSON/keys/types, model validation, and replacement failure safety:

```python
def test_missing_config_defaults_every_known_provider_to_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    policy = providerconfig.load(["codex", "ollama"])
    assert policy.setting("codex") == providerconfig.ProviderSetting(enabled=True, model=None)


def test_disabled_provider_and_model_round_trip_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    providerconfig.set_enabled("ollama", False, known={"ollama"})
    providerconfig.set_model("ollama", "qwen3:0.6b", known={"ollama"})
    assert providerconfig.load(["ollama"]).setting("ollama") == providerconfig.ProviderSetting(
        enabled=False, model="qwen3:0.6b"
    )


def test_invalid_config_names_the_file_and_field(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "providers": {"ollama": {"enabled": "yes"}}}')
    with pytest.raises(UsageError, match=r"config.json.*providers.ollama.enabled"):
        providerconfig.load(["ollama"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_providerconfig.py tests/test_cliargs.py tests/test_cli_entry.py -q
```

Expected: collection/import failures for `providerconfig` and parse failures for the absent `providers` subcommand.

- [ ] **Step 3: Implement the narrow JSON configuration contract**

Create these public shapes and functions:

```python
CONFIG_VERSION = 1

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
    environ = os.environ if env is None else env
    base = environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "adversarial-friends" / "config.json"


def load(known: Iterable[str], env: Mapping[str, str] | None = None) -> ProviderPolicy:
    names = set(known)
    path = config_path(env)
    if not path.exists():
        return ProviderPolicy({name: ProviderSetting() for name in names})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path}: invalid provider configuration: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"version", "providers"}:
        raise UsageError(f"{path}: expected only 'version' and 'providers'")
    if raw.get("version") != CONFIG_VERSION or not isinstance(raw.get("providers", {}), dict):
        raise UsageError(f"{path}: version must be {CONFIG_VERSION} and providers must be an object")
    settings = {name: ProviderSetting() for name in names}
    for name, value in raw.get("providers", {}).items():
        if name not in names or not isinstance(value, dict) or set(value) - {"enabled", "model"}:
            raise UsageError(f"{path}: invalid providers.{name}")
        enabled = value.get("enabled", True)
        model = value.get("model")
        if not isinstance(enabled, bool):
            raise UsageError(f"{path}: providers.{name}.enabled must be boolean")
        if model is not None and (not isinstance(model, str) or MODEL_RE.fullmatch(model) is None):
            raise UsageError(f"{path}: providers.{name}.model is invalid")
        settings[name] = ProviderSetting(enabled=enabled, model=model)
    return ProviderPolicy(settings)


def _write(policy: ProviderPolicy, env: Mapping[str, str] | None = None) -> Path:
    path = config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CONFIG_VERSION,
        "providers": {
            name: {"enabled": value.enabled, "model": value.model}
            for name, value in sorted(policy.providers.items())
        },
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def set_enabled(name: str, enabled: bool, *, known: set[str]) -> Path:
    if name not in known:
        raise UsageError(f"unknown provider {name!r}; known: {sorted(known)}")
    policy = load(known)
    settings = dict(policy.providers)
    settings[name] = dataclasses.replace(settings[name], enabled=enabled)
    return _write(ProviderPolicy(settings))


def set_model(name: str, model: str | None, *, known: set[str]) -> Path:
    if name not in known:
        raise UsageError(f"unknown provider {name!r}; known: {sorted(known)}")
    if model is not None and MODEL_RE.fullmatch(model) is None:
        raise UsageError(f"invalid model {model!r}: must match {MODEL_RE.pattern!r}")
    policy = load(known)
    settings = dict(policy.providers)
    settings[name] = dataclasses.replace(settings[name], model=model)
    return _write(ProviderPolicy(settings))
```

Validate the exact top-level keys `version` and `providers`, accept only version 1, accept only known provider names, accept only `enabled: bool` and `model: str | null`, validate models with `MODEL_RE`, and write a temporary sibling followed by `Path.replace`. Do not read configuration from the current repository.

- [ ] **Step 4: Add the provider-management command**

Add argparse children with these exact forms:

```text
afriend providers list [--json]
afriend providers enable NAME
afriend providers disable NAME
afriend providers set-model NAME MODEL
afriend providers clear-model NAME
```

`cmd_providers` loads adapter names from `ADAPTER_DIR`, performs the requested atomic update, and lists effective `enabled` and `model` values. Wire it through `cli.main` without changing existing command dispatch.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_providerconfig.py tests/test_cliargs.py tests/test_cli_entry.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the provider preference unit**

```bash
git add src/adversarial_friends/providerconfig.py src/adversarial_friends/commands/providers.py src/adversarial_friends/cliargs.py src/adversarial_friends/cli.py tests/test_providerconfig.py tests/test_cliargs.py tests/test_cli_entry.py
git commit -m "feat: add configurable provider defaults"
```

## Task 3: Shared Readiness and Host-Aware Selection

**Files:**

- Create: `src/adversarial_friends/readiness.py`
- Modify: `src/adversarial_friends/roster.py`
- Modify: `src/adversarial_friends/commands/friends.py`
- Modify: `src/adversarial_friends/commands/doctor.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Create: `tests/test_readiness.py`
- Modify: `tests/test_roster.py`
- Modify: `tests/test_run_end_to_end_flags.py`

- [ ] **Step 1: Write failing readiness and selection tests**

Cover every state plus ordering. In particular:

```python
def test_current_codex_markers_detect_codex_host():
    assert detect_host({"CODEX_SESSION_ID": "s"}) == "codex"
    assert detect_host({"CODEX_THREAD_ID": "t"}) == "codex"


def test_disabled_http_provider_is_not_probed(registry):
    probes: list[str] = []
    rows = assess_all(
        registry,
        ProviderPolicy({"ollama": ProviderSetting(enabled=False)}),
        env={}, which=lambda _: None, probe=lambda endpoint: probes.append(endpoint) or True,
    )
    assert rows["ollama"].state == ReadinessState.DISABLED
    assert probes == []


def test_reachable_ollama_without_model_is_not_ready(registry):
    rows = assess_all(
        registry,
        ProviderPolicy({"ollama": ProviderSetting(enabled=True)}),
        env={}, which=lambda _: None, probe=lambda _: True,
    )
    assert rows["ollama"].state == ReadinessState.REACHABLE_UNCONFIGURED


def test_capacity_is_applied_after_readiness(registry):
    specs = resolve(
        registry,
        ["ops"],
        {},
        which=lambda name: f"/bin/{name}" if name == "opencode" else None,
        probe=lambda endpoint: True,
        provider_policy=ProviderPolicy(
            {"ollama": ProviderSetting(enabled=True, model=None)}
        ),
        max_friends=1,
    )
    assert [spec.cli for spec in specs] == ["opencode"]  # unusable ollama did not consume slot
```

Also prove per-run enable/disable overrides persistent settings, contradictory flags are rejected, explicit `--friend codex:ops` still works in Codex, and auto-discovery excludes Codex unless `--include-self` is supplied.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_readiness.py tests/test_roster.py tests/test_run_end_to_end_flags.py -q
```

Expected: missing readiness API, missing current Codex markers, and model-less Ollama incorrectly reported usable.

- [ ] **Step 3: Implement the canonical readiness result**

Create:

```python
class ReadinessState(StrEnum):
    READY = "ready"
    REACHABLE_UNCONFIGURED = "reachable-unconfigured"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    HOST_EXCLUDED = "host-excluded"
    POLICY_BLOCKED = "policy-blocked"

@dataclass(frozen=True)
class FriendReadiness:
    provider: str
    state: ReadinessState
    reason: str
    where: str
    model: str | None

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY
```

`assess_all` must skip all executable/HTTP probes for disabled providers, use configured models for HTTP transports, distinguish reachable-unconfigured from unavailable, and classify the detected host separately. Add `CODEX_SESSION_ID` and `CODEX_THREAD_ID` to host detection. Support `--host-provider NAME` for wrappers; persist it in `_RESUMABLE_ARGS`.

- [ ] **Step 4: Make resolution and doctor consume the same assessment**

Load `ProviderPolicy` once in `resolve_friends`, apply repeatable `--enable-provider` and `--disable-provider` overrides, reject the same provider in both lists, and pass assessments into roster construction. Automatic selection must assign lenses and apply `--max-friends` only after filtering to `ready`. Explicit `--friend` bypasses enabled/host policy but still undergoes dispatchability and security validation in later tasks.

Replace `doctor`'s independently computed `found` list with the readiness rows. Text and JSON output must expose the state and reason, and `usable` must count only `ready` rows.

- [ ] **Step 5: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_readiness.py tests/test_roster.py tests/test_run_end_to_end_flags.py tests/test_cliargs.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the readiness unit**

```bash
git add src/adversarial_friends/readiness.py src/adversarial_friends/roster.py src/adversarial_friends/commands/friends.py src/adversarial_friends/commands/doctor.py src/adversarial_friends/cliargs.py src/adversarial_friends/commands/runmeta.py tests/test_readiness.py tests/test_roster.py tests/test_run_end_to_end_flags.py tests/test_cliargs.py
git commit -m "fix: select only ready non-host providers"
```

## Task 4: Deny External Tools by Default

**Files:**

- Create: `src/adversarial_friends/authority.py`
- Modify: `src/adversarial_friends/adapters.py`
- Modify: `src/adversarial_friends/dispatch.py`
- Modify: `src/adversarial_friends/rounds.py`
- Modify: `src/adversarial_friends/commands/setup.py`
- Modify: `src/adversarial_friends/commands/critique.py`
- Modify: `src/adversarial_friends/commands/crossexam.py`
- Modify: `src/adversarial_friends/commands/resume.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/assets/adapters/agy.toml`
- Modify: `src/adversarial_friends/assets/adapters/claude.toml`
- Modify: `src/adversarial_friends/assets/adapters/codex.toml`
- Modify: `src/adversarial_friends/assets/adapters/ollama.toml`
- Modify: `src/adversarial_friends/assets/adapters/opencode.toml`
- Create: `tests/test_authority.py`
- Modify: `tests/test_adapters.py`
- Modify: `tests/test_dispatch_findings.py`
- Modify: `tests/test_run_end_to_end_flags.py`

- [ ] **Step 1: Capture installed CLI evidence before declaring strategies**

Run read-only help/version probes for each installed executable adapter and save the exact command/output summary in comments beside its TOML authority declaration:

```bash
codex --version
codex exec --help
claude --version
claude --help
agy --version
agy --help
opencode --version
opencode --help
```

Expected: Codex exposes `--ignore-user-config`, `--disable apps`, and `--disable plugins`. For every other CLI, declare a deny strategy only when its installed help proves the necessary config/plugin/MCP neutralization. An adapter without proof must be `uncontrolled`, not guessed safe.

- [ ] **Step 2: Write failing authority parsing, argv, and fail-closed tests**

```python
def test_codex_denial_flags_precede_the_prompt(codex, prompt, schema):
    argv, _, cap = build_argv(codex, spec("codex"), prompt, schema, ExternalToolPolicy.DENY)
    assert "--ignore-user-config" in argv
    assert argv.index("--ignore-user-config") < argv.index("-")
    assert cap.external_tools == "denied"


def test_uncontrolled_adapter_is_blocked_under_default_policy(adapter):
    adapter = replace(adapter, external_tools="uncontrolled", deny_external_tools_argv=())
    with pytest.raises(PolicyError, match="cannot deny external tools"):
        enforce(adapter, ExternalToolPolicy.DENY)


def test_allow_is_explicit_and_recorded(adapter):
    decision = enforce(adapter, ExternalToolPolicy.ALLOW)
    assert decision.status == "explicitly-allowed"
```

Add an end-to-end fake-adapter test proving default denial reaches every critique and judging dispatch and `--allow-external-tools` is persisted in invocation metadata for resume.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run pytest tests/test_authority.py tests/test_adapters.py tests/test_dispatch_findings.py tests/test_run_end_to_end_flags.py -q
```

Expected: missing policy types/TOML fields and no deny flags in constructed argv.

- [ ] **Step 4: Implement the authority contract and adapter declarations**

Create:

```python
class ExternalToolPolicy(StrEnum):
    DENY = "deny"
    ALLOW = "allow"

@dataclass(frozen=True)
class AuthorityDecision:
    policy: ExternalToolPolicy
    status: str                 # denied | explicitly-allowed | policy-blocked
    argv: tuple[str, ...]
    sources: tuple[str, ...]
    reason: str = ""

class PolicyError(UsageError):
    pass


def enforce(adapter: Adapter, policy: ExternalToolPolicy) -> AuthorityDecision:
    if policy is ExternalToolPolicy.ALLOW:
        return AuthorityDecision(policy, "explicitly-allowed", (), adapter.external_tool_sources)
    if adapter.transport == "http" or adapter.external_tools == "none":
        return AuthorityDecision(policy, "denied", (), adapter.external_tool_sources)
    if adapter.external_tools == "deny-argv" and adapter.deny_external_tools_argv:
        return AuthorityDecision(
            policy, "denied", adapter.deny_external_tools_argv, adapter.external_tool_sources
        )
    raise PolicyError(
        f"{adapter.name} cannot deny external tools with this installed adapter; "
        "pass --allow-external-tools to opt in explicitly"
    )
```

Extend `Adapter` with `external_tools`, `deny_external_tools_argv`, and `external_tool_sources`. Extend `Capability` with an `external_tools` field defaulting to `denied` so existing three-argument constructions remain compatible. Treat HTTP adapters that expose no tool protocol as `none`; treat unverified executable adapters as `uncontrolled`; fail closed under `deny`.

Feed `enforce` into `readiness.assess_all`: under deny policy, catch
`PolicyError` and return `FriendReadiness(state=POLICY_BLOCKED, reason=str(exc))`.
An explicit friend is still refused before run-directory creation unless the
operator supplied `--allow-external-tools`; explicit roster selection does
not bypass the authority contract.

- [ ] **Step 5: Thread one policy through every dispatch path**

Add `--allow-external-tools` to `run`, include it in `_RESUMABLE_ARGS`, derive one enum in `prepare_run`, and pass it through critique, crossexam, resume, `dispatch_round`, `_dispatch`, and `build_argv`. Do not read this grant from provider configuration or repository files. Record `external_tool_policy`, decision status, and known sources in each friend row and top-level metadata.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_authority.py tests/test_adapters.py tests/test_dispatch_findings.py tests/test_run_end_to_end_flags.py tests/test_run_end_to_end_crossexam.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the authority unit**

```bash
git add src/adversarial_friends/authority.py src/adversarial_friends/adapters.py src/adversarial_friends/dispatch.py src/adversarial_friends/rounds.py src/adversarial_friends/commands src/adversarial_friends/cliargs.py src/adversarial_friends/assets/adapters tests/test_authority.py tests/test_adapters.py tests/test_dispatch_findings.py tests/test_run_end_to_end_flags.py
git commit -m "feat: deny provider tools by default"
```

## Task 5: Immutable Snapshot Identity on Resume

**Files:**

- Create: `src/adversarial_friends/snapshots.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/commands/environment.py`
- Modify: `src/adversarial_friends/commands/haltstate.py`
- Create: `tests/test_snapshot_identity.py`
- Modify: `tests/test_resume_findings.py`
- Modify: `tests/test_resume_crash_safety.py`

- [ ] **Step 1: Write failing snapshot creation, migration, and resume tests**

```python
def test_resume_uses_recorded_snapshot_without_creating_another(monkeypatch, halted_run):
    monkeypatch.setattr(isolation, "snapshot_commit", Mock(side_effect=AssertionError("new snapshot")))
    identity = SnapshotIdentity.from_meta(halted_run.meta)
    assert identity.verify().commit == halted_run.meta["snapshot_sha"]


def test_missing_saved_commit_refuses_resume_without_rewriting_run_json(halted_run):
    before = halted_run.run_json.read_bytes()
    with pytest.raises(UsageError, match="saved snapshot.*missing"):
        SnapshotIdentity.from_meta({**halted_run.meta, "snapshot_sha": "0" * 40}).verify()
    assert halted_run.run_json.read_bytes() == before


def test_v020_snapshot_fields_migrate_to_identity(v020_meta):
    identity = SnapshotIdentity.from_meta(v020_meta)
    assert identity.commit == v020_meta["snapshot_sha"]
```

Also test artifact hash mismatch, repo mismatch, snapshot history ordering, and a deliberate loop revision creating a successor identity that points to its predecessor.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_snapshot_identity.py tests/test_resume_findings.py tests/test_resume_crash_safety.py -q
```

Expected: missing snapshot API and existing resume path calls `snapshot_commit`.

- [ ] **Step 3: Implement snapshot identity and verification**

Create:

```python
@dataclass(frozen=True)
class SnapshotIdentity:
    repo_root: Path | None
    commit: str | None
    tree: str | None
    artifact_path: str
    artifact_hash: str
    predecessor: str | None = None

    @classmethod
    def create(cls, repo_root: Path | None, artifact: Path, digest: str) -> SnapshotIdentity:
        commit = isolation.snapshot_commit(repo_root) if repo_root is not None else None
        tree = git_tree(repo_root, commit) if repo_root is not None and commit is not None else None
        return cls(repo_root, commit, tree, str(artifact), digest)

    @classmethod
    def from_meta(cls, meta: Mapping[str, object]) -> SnapshotIdentity:
        raw = meta.get("snapshot")
        if isinstance(raw, dict):
            return cls(
                Path(str(raw["repo_root"])) if raw.get("repo_root") else None,
                str(raw["commit"]) if raw.get("commit") else None,
                str(raw["tree"]) if raw.get("tree") else None,
                str(raw["artifact_path"]),
                str(raw["artifact_hash"]),
                str(raw["predecessor"]) if raw.get("predecessor") else None,
            )
        return cls(
            Path(str(meta["repo_root"])) if meta.get("repo_root") else None,
            str(meta["snapshot_sha"]) if meta.get("snapshot_sha") else None,
            None,
            str(meta.get("artifact_path", meta.get("artifact", ""))),
            str(meta.get("artifact_hash", "")),
        )

    def verify(self, frozen: Path) -> SnapshotIdentity:
        actual_hash = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
        if actual_hash != self.artifact_hash:
            raise UsageError("cannot resume: frozen artifact hash does not match saved snapshot")
        if self.repo_root is not None and self.commit is not None:
            verify_commit(self.repo_root, self.commit)
            actual_tree = git_tree(self.repo_root, self.commit)
            if self.tree is not None and actual_tree != self.tree:
                raise UsageError("cannot resume: saved snapshot tree does not match commit")
            return dataclasses.replace(self, tree=actual_tree)
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "commit": self.commit,
            "tree": self.tree,
            "artifact_path": self.artifact_path,
            "artifact_hash": self.artifact_hash,
            "predecessor": self.predecessor,
        }
```

Use these checked Git helpers; they turn missing commits into actionable
resume errors rather than leaking `CalledProcessError`:

```python
def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git lookup failed"
        raise UsageError(f"cannot resume: saved snapshot is unavailable: {detail}")
    return result.stdout.strip()


def verify_commit(repo: Path, commit: str) -> None:
    _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")


def git_tree(repo: Path, commit: str) -> str:
    return _git(repo, "rev-parse", f"{commit}^{{tree}}")
```

Use `git cat-file -e <commit>^{commit}` and `git rev-parse <commit>^{tree}` for verification, and hash the frozen artifact bytes. A legacy 0.2.0 record migrates from `repo_root`, `snapshot_sha`, `artifact_path`, and `artifact_hash`. Never derive a replacement when any recorded identity field is inconsistent.

- [ ] **Step 4: Integrate one snapshot lifecycle**

Move fresh/resume selection out of the nearly-full `commands/run.py` into `snapshots.py`. Fresh runs call `SnapshotIdentity.create(repo_root, frozen, digest)`; resumed runs call `SnapshotIdentity.from_meta(args._resume_meta).verify(frozen)` and do not call `isolation.snapshot_commit`. Store `snapshot`, `snapshot_history`, and compatibility keys `repo_root`/`snapshot_sha`. `freeze_revision` creates a successor only when a live loop artifact actually changes.

- [ ] **Step 5: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_snapshot_identity.py tests/test_resume_findings.py tests/test_resume_crash_safety.py tests/test_run_end_to_end_loop.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the snapshot unit**

```bash
git add src/adversarial_friends/snapshots.py src/adversarial_friends/commands/run.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/commands/environment.py src/adversarial_friends/commands/haltstate.py tests/test_snapshot_identity.py tests/test_resume_findings.py tests/test_resume_crash_safety.py
git commit -m "fix: preserve snapshot identity across resume"
```

## Task 6: One Terminal RunOutcome

**Files:**

- Create: `src/adversarial_friends/outcomes.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/commands/exits.py`
- Modify: `src/adversarial_friends/commands/haltstate.py`
- Modify: `src/adversarial_friends/report.py`
- Create: `tests/test_outcomes.py`
- Modify: `tests/test_ceiling_reach.py`
- Modify: `tests/test_exits.py`
- Modify: `tests/test_run_end_to_end_loop.py`
- Modify: `tests/test_run_end_to_end_gate.py`
- Modify: `tests/test_resume_budget_findings.py`

- [ ] **Step 1: Write failing pure outcome and end-to-end exhaustion tests**

```python
def test_iteration_exhaustion_is_a_ceiling_exit():
    outcome = terminal_outcome(
        mode="loop", converged=False, loop_exhausted=True, budget_reason=None,
        blocking_ids=[], any_success=True, unresolved=False,
    )
    assert outcome.stop_reason == StopReason.MAX_LOOP_ITERATIONS
    assert outcome.ceiling_hit == "max-loop-iterations"
    assert outcome.exit_code == 11


def test_gate_blockers_are_part_of_the_outcome():
    outcome = terminal_outcome(
        mode="gate", converged=True, loop_exhausted=False, budget_reason=None,
        blocking_ids=["c-0002@1"], any_success=True, unresolved=False,
    )
    assert outcome.gate_decision == "blocked"
    assert outcome.blocker_ids == ("c-0002@1",)
    assert outcome.exit_code == 1


def test_terminal_meta_keeps_checkpoint_spend_and_tracker(loop_run):
    assert loop_run.meta["spent_calls"] == loop_run.checkpoint["spent_calls"]
    assert loop_run.meta["repeat_tracker"] == loop_run.checkpoint["repeat_tracker"]
```

Add assertions for `started_at`, `finished_at`, nonnegative `duration_s`, `exit_code`, every specified stop reason, and exit precedence.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_outcomes.py tests/test_ceiling_reach.py tests/test_exits.py tests/test_run_end_to_end_loop.py tests/test_run_end_to_end_gate.py tests/test_resume_budget_findings.py -q
```

Expected: natural loop exhaustion exits 0/1 without ceiling metadata and lifecycle fields are absent.

- [ ] **Step 3: Implement the immutable terminal projection**

Create:

```python
class StopReason(StrEnum):
    COMPLETED = "completed"
    GATE_BLOCKED = "gate-blocked"
    MAX_LOOP_ITERATIONS = "max-loop-iterations"
    MAX_CALLS = "max-calls"
    MAX_WALL_CLOCK = "max-wall-clock"
    AUTH_ABORT = "auth-abort"
    INCOMPLETE = "incomplete"
    INTERRUPTED = "interrupted"
    RUNTIME_ERROR = "runtime-error"

@dataclass(frozen=True)
class RunOutcome:
    started_at: str
    finished_at: str
    duration_s: float
    stop_reason: StopReason
    exit_code: int
    converged: bool
    gate_decision: str | None
    blocker_ids: tuple[str, ...]
    ceiling_hit: str | None
    attempted_calls: int
    spent_calls: int
    iterations_run: int
    rounds_run: int
    dry_streak: int
    repeat_tracker: dict[str, object]

    def apply(self, meta: dict[str, Any]) -> dict[str, Any]:
        meta.update(
            {
                "schema_version": 2,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_s": self.duration_s,
                "stop_reason": self.stop_reason.value,
                "exit_code": self.exit_code,
                "converged": self.converged,
                "gate_decision": self.gate_decision,
                "gate_blocking_claims": list(self.blocker_ids),
                "ceiling_hit": self.ceiling_hit,
                "attempted_calls": self.attempted_calls,
                "spent_calls": self.spent_calls,
                "iterations_run": self.iterations_run,
                "rounds_run": self.rounds_run,
                "dry_streak": self.dry_streak,
                "repeat_tracker": self.repeat_tracker,
            }
        )
        return meta


def terminal_outcome(
    *,
    mode: str,
    converged: bool,
    loop_exhausted: bool,
    budget_reason: str | None,
    blocking_ids: Sequence[str],
    any_success: bool,
    unresolved: bool,
    auth_abort: bool = False,
    abort_signum: int | None = None,
    started_at: str = "1970-01-01T00:00:00Z",
    finished_at: str = "1970-01-01T00:00:00Z",
    duration_s: float = 0.0,
    attempted_calls: int = 0,
    spent_calls: int = 0,
    iterations_run: int = 0,
    rounds_run: int = 0,
    dry_streak: int = 0,
    repeat_tracker: Mapping[str, object] | None = None,
) -> RunOutcome:
    gate_decision = None if mode != "gate" else ("blocked" if blocking_ids else "clear")
    if abort_signum is not None:
        reason, exit_code, ceiling = StopReason.INTERRUPTED, 128 + abort_signum, None
    elif budget_reason is not None:
        reason = (
            StopReason.MAX_WALL_CLOCK
            if "wall-clock" in budget_reason
            else StopReason.MAX_CALLS
        )
        exit_code, ceiling = 11, reason.value
    elif loop_exhausted and not converged:
        reason, exit_code, ceiling = StopReason.MAX_LOOP_ITERATIONS, 11, "max-loop-iterations"
    elif auth_abort:
        reason, exit_code, ceiling = StopReason.AUTH_ABORT, 1, None
    elif not any_success or unresolved:
        reason, exit_code, ceiling = StopReason.INCOMPLETE, 1, None
    elif mode == "gate" and blocking_ids:
        reason, exit_code, ceiling = StopReason.GATE_BLOCKED, 1, None
    else:
        reason, exit_code, ceiling = StopReason.COMPLETED, 0, None
    return RunOutcome(
        started_at, finished_at, duration_s, reason, exit_code, converged,
        gate_decision, tuple(blocking_ids), ceiling, attempted_calls, spent_calls,
        iterations_run, rounds_run, dry_streak, dict(repeat_tracker or {}),
    )
```

Keep decision logic pure. The run loop supplies observed facts; `terminal_outcome` applies documented precedence and returns one object. `decide_exit` becomes output-only compatibility glue around `RunOutcome.exit_code` rather than recomputing conditions.

- [ ] **Step 4: Detect natural loop exhaustion explicitly**

Track whether the loop broke because `loop_is_done` returned true. If the `for` range ends at `max_loop_iterations` without convergence, abort, auth failure, or another budget ceiling, pass `loop_exhausted=True` to outcome construction. Do not infer this from `iterations_run` alone on resumes.

- [ ] **Step 5: Render and persist only after outcome construction**

Compute blockers and `RunOutcome`, apply it to base metadata, then write `run.json`, report, and console/JSON output. Include `schema_version`, lifecycle fields, stop reason, exit code, call counts, iterations, dry streak, repeat tracker, gate decision, and blocker IDs. Halt metadata retains the same checkpoint fields and marks its lifecycle state as `waiting-for-orchestrator` without fabricating a terminal exit.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_outcomes.py tests/test_ceiling_reach.py tests/test_exits.py tests/test_run_end_to_end_loop.py tests/test_run_end_to_end_gate.py tests/test_resume_budget_findings.py -q
```

Expected: all selected tests pass, including natural exhaustion exit 11.

- [ ] **Step 7: Commit the terminal outcome unit**

```bash
git add src/adversarial_friends/outcomes.py src/adversarial_friends/commands/run.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/commands/exits.py src/adversarial_friends/commands/haltstate.py src/adversarial_friends/report.py tests/test_outcomes.py tests/test_ceiling_reach.py tests/test_exits.py tests/test_run_end_to_end_loop.py tests/test_run_end_to_end_gate.py tests/test_resume_budget_findings.py
git commit -m "fix: derive terminal state from one outcome"
```

## Task 7: Conservative Theme Novelty

**Files:**

- Create: `src/adversarial_friends/themes.py`
- Modify: `src/adversarial_friends/commands/critique.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/report.py`
- Create: `tests/test_themes.py`
- Modify: `tests/test_run_end_to_end_loop.py`
- Modify: `tests/test_merge.py`

- [ ] **Step 1: Write failing theme boundary tests**

```python
def test_obvious_wording_variant_at_same_anchor_is_same_theme():
    first = claim("c-0001@1", location="src/auth.py:42", claim="expiry guard is missing")
    second = claim("c-0002@1", location="src/auth.py:42", claim="missing expiration guard")
    proposal = compare_theme(first, second)
    assert proposal is not None and proposal.score >= THEME_THRESHOLD


def test_different_failure_mechanisms_at_same_anchor_remain_novel():
    auth = claim(
        "c-0001@1", location="src/auth.py:42", claim_text="expiry guard missing",
        failure_scenario="expired token passes",
    )
    race = claim(
        "c-0002@1", location="src/auth.py:42", claim_text="refresh update unsafe",
        failure_scenario="concurrent refresh loses update",
    )
    assert compare_theme(auth, race) is None


def test_claims_without_shared_anchor_fall_back_to_exact_identity():
    first = claim(
        "c-0001@1", location=None, claim_text="expiry guard missing",
        failure_scenario="expired token passes",
    )
    second = claim(
        "c-0002@1", location=None, claim_text="missing expiration guard",
        failure_scenario="expired tokens are accepted",
    )
    assert compare_theme(first, second) is None
```

Add an end-to-end loop test where round two returns a same-anchor wording variant: exact ledger IDs both remain, a proposal is persisted, and the variant does not reset the dry streak.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_themes.py tests/test_run_end_to_end_loop.py tests/test_merge.py -q
```

Expected: missing theme API and wording variants count as newly learned claims.

- [ ] **Step 3: Implement conservative theme comparison**

Create:

```python
THEME_THRESHOLD = 0.82

@dataclass(frozen=True)
class ThemeProposal:
    canonical: str
    duplicate: str
    score: float
    anchor: str

def normalized_anchor(location: str | None) -> str | None:
    if not location or not location.strip():
        return None
    return " ".join(location.casefold().split())


_SYNONYMS = {
    "absent": "missing",
    "expiry": "expiration",
}


def _tokens(text: str) -> set[str]:
    return {
        _SYNONYMS.get(token, token)
        for token in re.findall(r"[a-z0-9_]+", text.casefold())
        if len(token) > 2
    }


def similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = difflib.SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    return max(overlap, sequence)


def compare_theme(canonical: Claim, candidate: Claim) -> ThemeProposal | None:
    anchor = normalized_anchor(canonical.location)
    if anchor is None or anchor != normalized_anchor(candidate.location):
        return None
    claim_score = similarity(canonical.claim, candidate.claim)
    failure_score = similarity(canonical.failure_scenario, candidate.failure_scenario)
    score = (claim_score + failure_score) / 2
    if score < THEME_THRESHOLD or failure_score < THEME_THRESHOLD:
        return None
    return ThemeProposal(canonical.id, candidate.id, round(score, 4), anchor)


def classify_novel(
    existing: Sequence[Claim], incoming: Sequence[Claim]
) -> tuple[set[str], list[ThemeProposal]]:
    novel: set[str] = set()
    proposals: list[ThemeProposal] = []
    candidates = list(existing)
    for claim in incoming:
        proposal = next(
            (match for prior in candidates if (match := compare_theme(prior, claim)) is not None),
            None,
        )
        if proposal is None:
            novel.add(claim.id)
            candidates.append(claim)
        else:
            proposals.append(proposal)
    return novel, proposals
```

Normalize case, whitespace, punctuation, and a deliberately small pinned synonym set (`absent`/`missing`, `expiry`/`expiration`). Require the same nonempty normalized source anchor. Compare both claim text and failure scenario with token-set overlap plus `difflib.SequenceMatcher`; require the threshold in both the combined score and failure-mechanism score. Without a shared anchor, use only the existing exact merge identity.

- [ ] **Step 4: Separate ledger identity from loop novelty**

Keep `exact_merge` unchanged. Extend `CritiqueOutcome` with `produced_new_themes` and `theme_proposals`. Calculate proposals before exact merge, persist them in terminal/checkpoint metadata, and change loop dryness to use `not critique.produced_new_themes` plus the existing successful-round requirement. Render proposals as advisory, never as durable aliases.

- [ ] **Step 5: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_themes.py tests/test_run_end_to_end_loop.py tests/test_merge.py -q
```

Expected: all selected tests pass; exact merge behavior remains unchanged.

- [ ] **Step 6: Commit the theme novelty unit**

```bash
git add src/adversarial_friends/themes.py src/adversarial_friends/commands/critique.py src/adversarial_friends/commands/run.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/report.py tests/test_themes.py tests/test_run_end_to_end_loop.py tests/test_merge.py
git commit -m "feat: track loop novelty by conservative themes"
```

## Task 8: Coherent Diagnostics, Skip Audits, and Reports

**Files:**

- Modify: `src/adversarial_friends/rounds.py`
- Modify: `src/adversarial_friends/commands/critique.py`
- Modify: `src/adversarial_friends/commands/crossexam.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/report.py`
- Create: `tests/test_round_audit.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_report_verdicts.py`
- Modify: `tests/test_run_end_to_end_basics.py`
- Modify: `tests/test_run_end_to_end_loop.py`

- [ ] **Step 1: Write failing diagnostics and audit tests**

```python
def test_successful_stderr_is_visible_but_not_a_failure(store, successful_outcome):
    successful_outcome = dataclasses.replace(
        successful_outcome, stderr="cache warning: stale index"
    )
    row = persist_result(
        store, 1, friend_spec(), Capability(False, True, "none"),
        successful_outcome, "exec",
    )
    assert row["status"].startswith("ok (diagnostics:")
    assert row["diagnostics"] == "cache warning: stale index"


def test_repeat_disabled_friend_has_skip_record_and_no_prompt(run_dir):
    assert (run_dir / "round-3" / "broken.meta").read_text().startswith("status=skipped")
    assert not (run_dir / "round-3" / "broken.prompt").exists()


def test_read_exposed_names_are_stably_deduplicated():
    run_meta = meta(friends=[same_friend_round_1, same_friend_round_2])
    out = render_review(ReviewState(), run_meta)
    sentence = next(line for line in out.splitlines() if line.startswith("**Filesystem"))
    assert sentence.count("claude-security") == 1
```

Add golden assertions for an explicit gate decision and blocker IDs, external-tool authority language distinct from OS confinement, inline backtick escaping, bounded diagnostics, and one prompt per actual dispatch.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_round_audit.py tests/test_report.py tests/test_report_verdicts.py tests/test_run_end_to_end_basics.py tests/test_run_end_to_end_loop.py -q
```

Expected: successful diagnostics are hidden, disabled friend prompt exists without dispatch metadata, gate section is absent, and names repeat.

- [ ] **Step 3: Filter disabled friends before prompt construction**

Add a shared partition helper in `rounds.py`:

```python
@dataclass(frozen=True)
class SkippedFriend:
    spec: FriendSpec
    reason: str

def partition_dispatchable(
    specs: Sequence[FriendSpec], tracker: RepeatTracker | None
) -> tuple[list[FriendSpec], list[SkippedFriend]]:
    if tracker is None:
        return list(specs), []
    ready: list[FriendSpec] = []
    skipped: list[SkippedFriend] = []
    for spec in specs:
        if tracker.is_disabled(spec.name):
            skipped.append(SkippedFriend(spec, tracker.note(spec.name)))
        else:
            ready.append(spec)
    return ready, skipped


def persist_skip(store: RunStore, round_no: int, skipped: SkippedFriend) -> dict[str, Any]:
    _, _, meta_path = store.friend_paths(round_no, skipped.spec.name)
    meta_path.write_text(f"status=skipped\nreason={skipped.reason}\n", encoding="utf-8")
    return {
        "name": skipped.spec.name,
        "model": skipped.spec.model,
        "effort": skipped.spec.effort,
        "transport": "not-dispatched",
        "write_protected": False,
        "declared_scope": skipped.spec.scope,
        "os_confined": False,
        "round": round_no,
        "status": f"skipped: {skipped.reason}",
    }
```

Call it before critique and judging prompt builders. Persist `status=skipped` metadata and a friend row, append the downgrade once, and remove the late filtering from `dispatch_round`. A prompt path may be created only for the dispatchable list.

- [ ] **Step 4: Surface bounded successful diagnostics**

When stderr is nonempty on success, add a sanitized `_stderr_tail` summary to the status, store it separately as `diagnostics`, and point to the full `.err`. Preserve failure wording and orphan markers. Never include unbounded stderr in `run.json` or report cells.

- [ ] **Step 5: Complete report projections**

Add:

- `## Gate decision` with `clear`/`blocked`, ordered blocker IDs, stop reason, and ceiling/partial-evidence caveat;
- `## External tool authority` with `denied`, `explicitly-allowed`, or `legacy-unknown` language separate from filesystem confinement;
- `## Possible semantic duplicates` from theme proposals;
- stable first-seen deduplication of read-exposed names;
- corrected escaping for inline backtick runs and hostile diagnostic text.

Keep `render` pure and preserve exact claims/aliases.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_round_audit.py tests/test_report.py tests/test_report_verdicts.py tests/test_run_end_to_end_basics.py tests/test_run_end_to_end_loop.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit diagnostics and reporting**

```bash
git add src/adversarial_friends/rounds.py src/adversarial_friends/commands/critique.py src/adversarial_friends/commands/crossexam.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/report.py tests/test_round_audit.py tests/test_report.py tests/test_report_verdicts.py tests/test_run_end_to_end_basics.py tests/test_run_end_to_end_loop.py
git commit -m "fix: make run diagnostics and reports auditable"
```

## Task 9: Legacy Metadata, Documentation, and Plugin Payload

**Files:**

- Create: `tests/fixtures/run_meta_v020_terminal.json`
- Create: `tests/fixtures/run_meta_v020_halted.json`
- Create: `tests/test_runmeta_migration.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/assets/SKILL.md`
- Modify: `src/adversarial_friends/assets/references/modes.md`
- Modify: `src/adversarial_friends/assets/references/troubleshooting.md`
- Modify: `README.md`
- Mirror: `plugins/adversarial-friends/skills/adversarial-friends/`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_skill_layer.py`

- [ ] **Step 1: Add real-shape 0.2.0 migration fixtures and failing tests**

Fixtures must represent one terminal run and one orchestrator halt using the fields actually written by 0.2.0. Tests prove:

```python
def test_v020_terminal_meta_is_readable_and_marks_unknowns():
    migrated = migrate_meta(load_fixture("run_meta_v020_terminal.json"))
    assert migrated["schema_version"] == 2
    assert migrated["external_tool_policy"] == "legacy-unknown"
    assert migrated["started_at"] is None
    assert migrated["exit_code"] is None


def test_v020_halt_preserves_budget_tracker_and_snapshot():
    migrated = migrate_meta(load_fixture("run_meta_v020_halted.json"))
    assert migrated["spent_calls"] == 4
    assert migrated["repeat_tracker"]["disabled"]
    assert migrated["snapshot"]["commit"] == migrated["snapshot_sha"]
```

- [ ] **Step 2: Run migration tests and verify RED**

```bash
uv run pytest tests/test_runmeta_migration.py tests/test_resume_findings.py -q
```

Expected: missing migration API and legacy authority/lifecycle fields absent.

- [ ] **Step 3: Implement explicit schema migration**

Add `CURRENT_SCHEMA_VERSION = 2` and this migration in
`commands/runmeta.py`. Schema-less metadata is version 1. Preserve
compatibility keys and never invent a historical exit code, timestamp, or
authority guarantee:

```python
CURRENT_SCHEMA_VERSION = 2


def migrate_meta(raw: Mapping[str, Any]) -> dict[str, Any]:
    meta = copy.deepcopy(dict(raw))
    version = meta.get("schema_version", 1)
    if not isinstance(version, int) or version < 1 or version > CURRENT_SCHEMA_VERSION:
        raise UsageError(f"unsupported run metadata schema {version!r}")
    if version == CURRENT_SCHEMA_VERSION:
        return meta
    meta["schema_version"] = CURRENT_SCHEMA_VERSION
    meta.setdefault("started_at", None)
    meta.setdefault("finished_at", None)
    meta.setdefault("duration_s", None)
    meta.setdefault("exit_code", None)
    meta.setdefault("stop_reason", None)
    meta.setdefault("external_tool_policy", "legacy-unknown")
    meta.setdefault("attempted_calls", meta.get("spent_calls", 0))
    meta.setdefault("spent_calls", 0)
    meta.setdefault("repeat_tracker", {"last": {}, "count": {}, "disabled": {}})
    if "snapshot" not in meta:
        meta["snapshot"] = {
            "repo_root": meta.get("repo_root"),
            "commit": meta.get("snapshot_sha"),
            "tree": None,
            "artifact_path": meta.get("artifact_path", meta.get("artifact", "")),
            "artifact_hash": meta.get("artifact_hash", ""),
            "predecessor": None,
        }
    meta.setdefault("snapshot_history", [meta["snapshot"]])
    return meta
```

Call `migrate_meta` immediately after reading `run.json` in `_restore_args`,
before any field is interpreted.

- [ ] **Step 4: Document provider and authority behavior**

Update the canonical skill and references with:

- host-is-orchestrator default and `--include-self` override;
- provider list/enable/disable/model commands and per-run overrides;
- disabled providers are not probed;
- deny-by-default external tools and explicit per-run opt-in;
- readiness states and `doctor` remediation;
- exit 11 for loop-iteration exhaustion;
- resume snapshot verification and legacy-unknown authority reporting.

Update README examples accordingly.

- [ ] **Step 5: Synchronize the canonical asset tree to the plugin mirror**

```bash
make plugin-sync-copy
make plugin-sync
```

Expected: mirror copy completes and `plugin-sync` exits 0.

- [ ] **Step 6: Run migration and documentation tests**

```bash
uv run pytest tests/test_runmeta_migration.py tests/test_resume_findings.py tests/test_docs.py tests/test_skill_layer.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit migration and documentation**

```bash
git add src/adversarial_friends/commands/runmeta.py src/adversarial_friends/assets plugins/adversarial-friends/skills/adversarial-friends README.md tests/fixtures/run_meta_v020_terminal.json tests/fixtures/run_meta_v020_halted.json tests/test_runmeta_migration.py tests/test_docs.py tests/test_skill_layer.py
git commit -m "docs: document contract-first provider hardening"
```

## Task 10: Release Verification, Dogfood, and Codex Installation

**Files:**

- Modify: `VERSION`
- Modify: `plugins/adversarial-friends/.codex-plugin/plugin.json`
- Modify: `plugins/marketplace.json` if its manifest carries the version
- Create: `docs/superpowers/reviews/2026-08-30-v0.2.1-dogfood-report.md`
- Modify only if dogfood exposes a verified defect: relevant source/tests above

- [ ] **Step 1: Bump synchronized development version to 0.2.1**

Change `VERSION` and every plugin manifest version to `0.2.1`, then run:

```bash
make version-sync
```

Expected: version synchronization exits 0.

- [ ] **Step 2: Run the complete portable quality gate**

```bash
make quality
```

Expected: Ruff, mypy strict, 777-line cap, plugin sync, version sync, wheel assets, isolated wheel install, and the entire pytest suite all pass.

- [ ] **Step 3: Install the built wheel in a clean temporary environment**

```bash
dist_dir=$(mktemp -d)
uv build --wheel --out-dir "$dist_dir"
venv_dir=$(mktemp -d)
uv venv "$venv_dir"
uv pip install --python "$venv_dir/bin/python" "$dist_dir"/adversarial_friends-0.2.1-py3-none-any.whl
"$venv_dir/bin/afriend" --version
"$venv_dir/bin/afriend" doctor --json
```

Expected: version prints `0.2.1`; doctor reports Codex as host-excluded in a Codex environment, disabled providers as disabled, model-less reachable providers as unconfigured, and usable count equal to ready providers only.

- [ ] **Step 4: Run bounded live dogfood using effective enabled non-host providers**

First record the effective roster:

```bash
afriend providers list --json
afriend doctor --json
```

Then run the approved staged-hardening artifact through `report`, `crossexam`, `gate`, and a tightly bounded `loop`. Do not pass explicit friends that override the user's provider policy. Keep external tools denied. Set ceilings that bound cost and wall time, and record exact commands, exits, run paths, provider states, and artifact hashes in the dogfood report.

Expected:

- no host Codex dispatch unless explicitly requested;
- no disabled provider probe or dispatch;
- no model-less provider counted ready;
- gate report includes decision and blockers;
- loop convergence or exit 11 agrees across process status, `run.json`, and report;
- resuming a controlled halt preserves snapshot identity;
- prompt and dispatch audit artifacts correspond one-to-one.

- [ ] **Step 5: Fix only verified dogfood regressions with TDD**

For each observed mismatch, first add one minimal failing regression test to the closest existing test module, run it to verify RED, implement the smallest correction, rerun the focused test to GREEN, and commit with `fix:`. If no mismatch is observed, make no source change in this step.

- [ ] **Step 6: Re-run the full quality gate after dogfood**

```bash
make quality
git diff --check
git status --short
```

Expected: all gates pass, no whitespace errors, and only the intended dogfood report/version changes remain uncommitted.

- [ ] **Step 7: Install the updated local plugin through the supported cachebuster flow**

Use the `plugin-creator` skill's update/install workflow against `plugins/adversarial-friends`, then verify the installed cache contains the synchronized 0.2.1 skill and that a fresh Codex task can resolve it. Do not edit the plugin cache directly.

- [ ] **Step 8: Commit the verified release state**

```bash
git add VERSION plugins/adversarial-friends/.codex-plugin/plugin.json plugins/marketplace.json docs/superpowers/reviews/2026-08-30-v0.2.1-dogfood-report.md
git commit -m "chore: prepare adversarial-friends 0.2.1"
```

If a listed manifest path does not exist or does not carry a version, omit it from `git add`; do not create redundant manifests.

## Spec Coverage Matrix

| Requirement | Implementation task |
|---|---|
| Repository-wide 777-line Python cap | Task 1 |
| Persistent provider enable/disable/model policy | Task 2 |
| Current Codex host detection and host exclusion | Task 3 |
| Reachable versus dispatch-ready provider distinction | Task 3 |
| Readiness before `--max-friends`; shared doctor status | Task 3 |
| External tools denied by default and explicit opt-in | Task 4 |
| Fail-closed adapters and authority reporting data | Task 4 |
| Immutable halt snapshot reused and verified on resume | Task 5 |
| Lifecycle, spending, tracker, stop reason, and exit consistency | Task 6 |
| Natural iteration exhaustion becomes ceiling exit 11 | Task 6 |
| Conservative theme novelty without ledger auto-merge | Task 7 |
| Successful stderr diagnostics | Task 8 |
| Disabled-friend skip audit without prompt-only artifacts | Task 8 |
| Gate decision and blocker IDs in Markdown | Task 8 |
| Stable exposed-name dedupe and Markdown repair | Task 8 |
| 0.2.0 migration and `legacy-unknown` authority | Task 9 |
| Canonical docs/plugin synchronization | Task 9 |
| Full quality, live dogfood, wheel, and Codex install | Task 10 |

## Final SDD Review Gate

After all tasks pass their per-task spec and quality reviews:

- [ ] Dispatch a fresh holistic spec reviewer over the design, this plan, the complete branch diff, and the dogfood report.
- [ ] Return every substantive finding to an implementer, then repeat spec review until approved.
- [ ] Dispatch a fresh holistic code-quality reviewer over the final diff and verification evidence.
- [ ] Return every substantive finding to an implementer, then repeat code-quality review until approved.
- [ ] Run `make quality` once more after the final review fixes.
- [ ] Use `superpowers:verification-before-completion` before claiming the release is fixed.
- [ ] Use `superpowers:finishing-a-development-branch` to present integration options; do not merge or push without the user's direction.
