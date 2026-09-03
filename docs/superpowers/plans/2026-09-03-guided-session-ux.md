# Guided Session UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a guided first-review session, lifecycle feedback, durable run status, safe default/named profiles, and claim-resolution discovery.

**Architecture:** Keep host-conversation policy in the `/afriend` skill and put durable, scriptable facts in the CLI. A new user-owned session-config module resolves profiles without touching provider or authority configuration; an append-only event stream makes live status observable without parsing stderr. Read-only `status` and resolution discovery rebuild their view from existing run artifacts, so legacy runs remain useful.

**Tech Stack:** Python 3.11+ standard library, argparse, JSON/JSONL, pytest, package assets projected into the Codex plugin.

---

### Task 1: Add safe session preferences and built-in profiles

**Files:**
- Create: `src/adversarial_friends/sessionconfig.py`
- Create: `src/adversarial_friends/reviewprofiles.py`
- Create: `tests/test_sessionconfig.py`
- Modify: `src/adversarial_friends/paths.py`

- [ ] **Step 1: Write the failing session-config tests.**

```python
def test_missing_session_config_defaults_to_quick(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sessionconfig.load().default_profile == "quick"


def test_set_default_profile_is_atomic_and_rejects_unknown_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sessionconfig.set_default("balanced", known=reviewprofiles.builtin_names())
    assert sessionconfig.load().default_profile == "balanced"
    with pytest.raises(UsageError, match="unknown profile"):
        sessionconfig.set_default("unsafe", known=reviewprofiles.builtin_names())


def test_builtin_profiles_do_not_contain_provider_or_authority_controls():
    forbidden = {"friend", "enable_provider", "allow_external_tools", "pass_env"}
    assert all(forbidden.isdisjoint(profile.as_run_defaults()) for profile in reviewprofiles.builtins())
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_sessionconfig.py -q`

Expected: FAIL because `sessionconfig` and `reviewprofiles` do not exist.

- [ ] **Step 3: Implement the smallest durable config boundary.**

Create `reviewprofiles.py` with frozen `ReviewProfile` values named `quick`, `balanced`, and `thorough`. `quick` sets `mode="report"`, `balanced` sets `mode="crossexam"`, and `thorough` sets `mode="loop"`; each starts with no other run-option overrides. Create `sessionconfig.py` with a strict, versioned `session.json` schema containing `version` and `default_profile`, a 256 KiB read bound, atomic same-directory replacement, a lock file, and the same absolute-XDG path behavior as `providerconfig.py`. Do not store this setting in `config.json`.

- [ ] **Step 4: Run the green tests and static checks.**

Run: `uv run pytest tests/test_sessionconfig.py -q && uv run mypy src`

Expected: PASS; strict type checking succeeds.

- [ ] **Step 5: Commit the config foundation.**

```bash
git add src/adversarial_friends/sessionconfig.py src/adversarial_friends/reviewprofiles.py \
  src/adversarial_friends/paths.py tests/test_sessionconfig.py
git commit -m "feat: add safe review profile defaults"
```

### Task 2: Resolve an effective profile for every fresh run

**Files:**
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/commands/setup.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `tests/test_cliargs.py`
- Modify: `tests/test_runmeta_migration.py`
- Create: `tests/test_reviewprofiles.py`

- [ ] **Step 1: Write failing precedence and persistence tests.**

```python
def test_profile_supplies_mode_when_mode_was_not_explicit():
    args = build_parser().parse_args(["run", "spec.md", "--profile", "balanced"])
    resolved = reviewprofiles.resolve_run_profile(args, default_profile="quick")
    assert resolved.name == "balanced"
    assert args.mode == "crossexam"


def test_explicit_mode_wins_over_the_selected_profile():
    args = build_parser().parse_args(
        ["run", "spec.md", "--profile", "thorough", "--mode", "report"]
    )
    reviewprofiles.resolve_run_profile(args, default_profile="quick")
    assert args.mode == "report"


def test_run_metadata_freezes_effective_profile(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    snapshot = SnapshotIdentity(None, None, None, str(artifact), "sha256:" + "1" * 64)
    meta = runmeta._base_meta(
        SimpleNamespace(mode="crossexam", merge="exact", friend=[]), artifact,
        snapshot.artifact_hash, [], [], [], snapshot, [snapshot], DENY_ALL,
        profile="balanced",
    )
    assert meta["profile"] == "balanced"
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_reviewprofiles.py tests/test_cliargs.py tests/test_runmeta_migration.py -q`

Expected: FAIL because `--profile`, explicit-mode tracking, and metadata are absent.

- [ ] **Step 3: Implement profile resolution without changing explicit flags.**

Add `--profile NAME` to `run`. Record whether `--mode` was supplied before normalizing args, so only an absent mode is replaced by the selected profile. Resolve the configured default for fresh runs in `prepare_run`; resumes use the profile recorded in `run.json` only as descriptive history and retain saved invocation settings. Add `profile` to `_base_meta`, migration readers, and the report metadata path. Unknown profile names are usage errors before a run directory is created.

- [ ] **Step 4: Run the green tests.**

Run: `uv run pytest tests/test_reviewprofiles.py tests/test_cliargs.py tests/test_runmeta_migration.py -q`

Expected: PASS, including old run metadata that has no `profile` field.

- [ ] **Step 5: Commit profile application.**

```bash
git add src/adversarial_friends/cliargs.py src/adversarial_friends/commands/{run.py,runmeta.py,setup.py} \
  tests/test_cliargs.py tests/test_runmeta_migration.py tests/test_reviewprofiles.py
git commit -m "feat: apply a default review profile to runs"
```

### Task 3: Make `afriend init --guided` a no-write preview plus exact apply

**Files:**
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/init.py`
- Modify: `tests/test_run_end_to_end_roster.py`
- Create: `tests/test_guided_init.py`

- [ ] **Step 1: Write failing guided-setup tests.**

```python
def test_guided_init_preview_writes_no_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cmd_init(_args(guided=True, apply=False, json=True)) == 0
    assert not sessionconfig.session_config_path().exists()
    assert json.loads(capsys.readouterr().out)["default_profile"] == "quick"


def test_guided_init_apply_changes_only_requested_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cmd_init(_args(guided=True, apply=True, default_profile="balanced", enable=["claude"]))
    assert sessionconfig.load().default_profile == "balanced"


def test_existing_plain_init_still_writes_a_roster(tmp_path):
    assert cmd_init(_args(guided=False, out=str(tmp_path / "roster.toml"))) == 0
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_guided_init.py tests/test_run_end_to_end_roster.py -q`

Expected: FAIL because guided flags and preview/apply behavior do not exist.

- [ ] **Step 3: Implement exact, noninteractive guided options.**

Add `init --guided`, `--apply`, `--default-profile NAME`, repeatable `--enable-provider NAME`, repeatable `--disable-provider NAME`, and `--ollama-model MODEL`. Reject `--apply` without `--guided`, conflicting provider selections, unknown profile/provider names, and an Ollama model without an explicit Ollama selection. Preview emits a versioned JSON object when requested and readable stderr otherwise; it lists readiness, host role, external-tool denial, built-ins, and exact pending changes. Apply calls the existing locked provider setters and the new locked session setter. Preserve plain `init` and its roster overwrite semantics exactly.

- [ ] **Step 4: Run the green tests.**

Run: `uv run pytest tests/test_guided_init.py tests/test_run_end_to_end_roster.py -q`

Expected: PASS; preview creates no files and apply changes only named fields.

- [ ] **Step 5: Commit guided setup.**

```bash
git add src/adversarial_friends/cliargs.py src/adversarial_friends/commands/init.py \
  tests/test_guided_init.py tests/test_run_end_to_end_roster.py
git commit -m "feat: add guided setup preview and apply"
```

### Task 4: Persist safe lifecycle events with the run

**Files:**
- Create: `src/adversarial_friends/events.py`
- Modify: `src/adversarial_friends/runstore.py`
- Modify: `src/adversarial_friends/progress.py`
- Modify: `src/adversarial_friends/commands/{setup.py,run.py,critique.py,crossexam.py}`
- Modify: `tests/test_progress.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Write failing event-contract tests.**

```python
def test_event_writer_appends_only_allowed_lifecycle_fields(tmp_path):
    writer = events.EventWriter(RunStore(tmp_path, "run-events"))
    writer.friend_finished("claude-security-0", "claude", "security", 1, 1.5, "success")
    record = json.loads((tmp_path / "run-events" / "events.jsonl").read_text())
    assert record["type"] == "friend_finished"
    assert {"prompt", "raw", "stderr", "environment", "argv"}.isdisjoint(record["payload"])


def test_event_reader_ignores_an_unterminated_final_jsonl_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"version":1,"type":"run_started","payload":{}}\n{"version"')
    assert len(events.read_complete(path)) == 1
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_events.py tests/test_progress.py -q`

Expected: FAIL because no event writer/reader exists.

- [ ] **Step 3: Implement bounded append-only events.**

Define frozen, versioned event records and validate event type/payload keys before appending one JSON object plus newline through a secure 0600 append. Add `RunStore.events_path()` and a store-owned writer. Attach it to `Progress` without changing stdout; emit `run_started`, `round_finished`, and friend-completion/failure events beside existing human progress. Thread provider, lens, round, duration, and status only through critique and judging results. Emit terminal `run_finished` from every normal, halted, and handled-error finish path, with a derived next action. Never emit raw output, prompts, credentials, argv, environment, or authority-grant values.

- [ ] **Step 4: Run the green tests and event-focused end-to-end tests.**

Run: `uv run pytest tests/test_events.py tests/test_progress.py tests/test_run_end_to_end_basics.py -q`

Expected: PASS; event order follows dispatch completion and every terminal run has one final event.

- [ ] **Step 5: Commit lifecycle events.**

```bash
git add src/adversarial_friends/{events.py,progress.py,runstore.py} \
  src/adversarial_friends/commands/{setup.py,run.py,critique.py,crossexam.py} \
  tests/test_events.py tests/test_progress.py tests/test_run_end_to_end_basics.py
git commit -m "feat: record review lifecycle events"
```

### Task 5: Add read-only `afriend status` and watch mode

**Files:**
- Create: `src/adversarial_friends/commands/status.py`
- Modify: `src/adversarial_friends/cli.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/runstore.py`
- Create: `tests/test_status.py`
- Modify: `tests/test_cli_entry.py`

- [ ] **Step 1: Write failing status tests.**

```python
def test_status_summarizes_a_terminal_run_without_dispatching(tmp_path, capsys):
    run = _terminal_run(tmp_path, mode="report", profile="quick")
    assert cmd_status(_args(run_id=str(run), json=False, watch=False, out=None)) == 0
    assert "completed" in capsys.readouterr().out


def test_status_json_is_versioned_and_uses_legacy_artifacts_when_events_are_absent(tmp_path, capsys):
    run = _legacy_run_without_events(tmp_path)
    cmd_status(_args(run_id=str(run), json=True, watch=False, out=None))
    assert json.loads(capsys.readouterr().out)["version"] == 1


def test_watch_ignores_a_torn_tail_then_stops_at_run_finished(tmp_path):
    observed = list(status.watch_events(_events_with_torn_then_terminal_tail(tmp_path)))
    assert [event.type for event in observed] == ["run_started", "run_finished"]
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_status.py tests/test_cli_entry.py -q`

Expected: FAIL because the command and event-derived summary do not exist.

- [ ] **Step 3: Implement a read-only status command.**

Add `status RUN_ID_OR_PATH [--out DIR] [--json] [--watch]`. Resolve a bare run ID only below the default/`--out` root; accept an explicit directory after validating it as a contained run root. Rebuild the summary from `run.json`, `claims.jsonl`, friend metadata, and complete events. State whether a run is live, halted, terminal, or legacy; count claims by status; surface downgrades; and derive one next action. `--watch` writes new event renderings to stderr, polls at a constant bounded interval, ignores only an incomplete final line, and returns after `run_finished`. Do not create, chmod, repair, or rewrite any run artifact.

- [ ] **Step 4: Run the green status tests.**

Run: `uv run pytest tests/test_status.py tests/test_cli_entry.py -q`

Expected: PASS; no dispatch calls occur and legacy runs remain inspectable.

- [ ] **Step 5: Commit the status command.**

```bash
git add src/adversarial_friends/commands/status.py src/adversarial_friends/{cli.py,cliargs.py,runstore.py} \
  tests/test_status.py tests/test_cli_entry.py
git commit -m "feat: add read-only run status"
```

### Task 6: Add named, constrained user profiles

**Files:**
- Modify: `src/adversarial_friends/sessionconfig.py`
- Modify: `src/adversarial_friends/reviewprofiles.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/cli.py`
- Create: `src/adversarial_friends/commands/profiles.py`
- Modify: `tests/test_sessionconfig.py`
- Modify: `tests/test_reviewprofiles.py`
- Create: `tests/test_profiles_command.py`

- [ ] **Step 1: Write failing named-profile tests.**

```python
def test_custom_profile_inherits_a_builtin_and_overrides_safe_options(tmp_path, monkeypatch):
    sessionconfig.create_profile("release", base="balanced", values={"max_friends": 3})
    assert reviewprofiles.resolve("release").mode == "crossexam"
    assert reviewprofiles.resolve("release").max_friends == 3


@pytest.mark.parametrize("field", ["friend", "enable_provider", "allow_external_tools", "pass_env"])
def test_custom_profile_rejects_authority_or_roster_fields(field):
    with pytest.raises(UsageError, match="not allowed in a profile"):
        sessionconfig.create_profile("bad", base="quick", values={field: "x"})


def test_profiles_set_default_updates_only_session_config(capsys):
    assert cmd_profiles(_args(action="set-default", name="release")) == 0
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_sessionconfig.py tests/test_reviewprofiles.py tests/test_profiles_command.py -q`

Expected: FAIL because named profiles and the command do not exist.

- [ ] **Step 3: Implement profile CRUD and strict validation.**

Extend `session.json` to version 2 with a `profiles` map. Accept only `base`, `mode`, `preset`, `lenses`, `max_friends`, `require_friends`, `timeout`, `max_rounds`, `max_calls`, `max_wall_clock`, and `max_loop_iterations`, with the same scalar/list validation as `run`. Reject unknown bases, inheritance cycles, unknown keys, empty names, and all authority/provider/process fields. Add `afriend profiles list`, `show NAME`, `create NAME --base NAME [safe options]`, `update NAME [safe options]`, `delete NAME`, and `set-default NAME`; refuse deletion of the current default. All mutations use the existing session-config lock and atomic writer.

- [ ] **Step 4: Run the green profile tests.**

Run: `uv run pytest tests/test_sessionconfig.py tests/test_reviewprofiles.py tests/test_profiles_command.py -q`

Expected: PASS; custom values are inherited deterministically and unsafe options cannot be stored.

- [ ] **Step 5: Commit named profiles.**

```bash
git add src/adversarial_friends/{sessionconfig.py,reviewprofiles.py,cli.py,cliargs.py} \
  src/adversarial_friends/commands/profiles.py tests/test_sessionconfig.py \
  tests/test_reviewprofiles.py tests/test_profiles_command.py
git commit -m "feat: add named review profiles"
```

### Task 7: Add resolution discovery without weakening writes

**Files:**
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/resolve.py`
- Create: `tests/test_resolve_discovery.py`
- Modify: `tests/test_resolutions.py`

- [ ] **Step 1: Write failing resolution-discovery tests.**

```python
def test_resolve_list_renders_unresolved_claims_without_appending(tmp_path, capsys):
    run = _run_with_two_unresolved_claims(tmp_path)
    assert cmd_resolve(_args(run_id=str(run), list=True, next=False)) == 0
    assert "c-0001@1" in capsys.readouterr().out
    assert list(Ledger(run / "claims.jsonl").records()) == _original_records


def test_resolve_next_refuses_an_ambiguous_choice(tmp_path):
    with pytest.raises(UsageError, match="choose --claim"):
        cmd_resolve(_args(run_id=str(_run_with_two_equal_claims(tmp_path)), next=True))


def test_resolve_write_still_requires_disposition_and_evidence():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["resolve", "run-1", "--claim", "c-0001@1"])
```

- [ ] **Step 2: Run the red tests.**

Run: `uv run pytest tests/test_resolve_discovery.py tests/test_resolutions.py -q`

Expected: FAIL because `--list`/`--next` do not exist and write args are always required.

- [ ] **Step 3: Implement mutually exclusive inspect and write forms.**

Make `--list` and `--next` mutually exclusive, read-only forms that do not require `--claim`, `--disposition`, or `--evidence`. Retain those three as a complete required set for the write form. Reuse ledger replay and recorded claim-state metadata to render only blocking/unresolved canonical claims, ordered severity (`critical`, `high`, `medium`, `low`) then claim ID. `--next` selects only a unique highest-priority item; otherwise it exits with choices. Both discovery forms reject malformed runs and never append a ledger record.

- [ ] **Step 4: Run the green resolution tests.**

Run: `uv run pytest tests/test_resolve_discovery.py tests/test_resolutions.py -q`

Expected: PASS; recorded resolutions still require disposition and evidence.

- [ ] **Step 5: Commit resolution UX.**

```bash
git add src/adversarial_friends/cliargs.py src/adversarial_friends/commands/resolve.py \
  tests/test_resolve_discovery.py tests/test_resolutions.py
git commit -m "feat: add unresolved claim discovery"
```

### Task 8: Update focused skills, current docs, diagrams, and evaluations

**Files:**
- Modify: `src/adversarial_friends/assets/entrypoints/afriend/SKILL.md`
- Modify: `src/adversarial_friends/assets/entrypoints/status/SKILL.md`
- Modify: `src/adversarial_friends/assets/entrypoints/configure/SKILL.md`
- Modify: `src/adversarial_friends/assets/entrypoints/resolve/SKILL.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/README.md`
- Modify: `docs/architecture/skill-routing.puml`
- Regenerate: `docs/architecture/skill-routing.{png,svg}`
- Modify: `evals/evals.json`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_skill_layer.py`

- [ ] **Step 1: Write failing documentation and skill-contract tests.**

```python
def test_router_requires_a_first_session_preflight_and_reports_completion():
    router = asset_text("entrypoints/afriend/SKILL.md")
    assert "About to start Adversarial Friends" in router
    assert "first review request in a host task" in router
    assert "when each friend finishes" in router


def test_status_skill_routes_named_runs_to_the_status_cli():
    assert "afriend status <run-id>" in asset_text("entrypoints/status/SKILL.md")


def test_docs_describe_quick_as_the_default_profile():
    assert "`quick`" in ROOT.joinpath("README.md").read_text()
```

- [ ] **Step 2: Run the red documentation tests.**

Run: `uv run pytest tests/test_docs.py tests/test_skill_layer.py -q`

Expected: FAIL because current docs describe only provider configuration and manual run-directory inspection.

- [ ] **Step 3: Update canonical assets and current documentation.**

Document exactly five skills, with `/afriend` presenting the first-session and iteration preflight, accepting a task-only selection, and reporting lifecycle completion. Route readiness to `doctor`, named-run inspection to `status`, defaults/profiles to `configure`, and claim discovery/write to `resolve`. Update CLI examples, profile precedence, `init --guided` preview/apply, events, status watch, and resolution discovery. Revise the routing diagram to show the session preflight, event stream, and read-only status path. Keep all docs written as current behavior; do not add migration prose.

- [ ] **Step 4: Project canonical assets and regenerate diagrams.**

Run: `make diagrams && make plugin-sync-copy && python3 scripts/check_plugin_sync.py`

Expected: regenerated `png`/`svg` match `puml`, and the plugin projection is byte-identical to the canonical assets.

- [ ] **Step 5: Update activation evaluations and run green docs tests.**

Add cases for direct `/afriend` first-review setup, task-only override, incidental friend non-activation, status/readiness routing, profile safety, and resolution listing. Then run:

`uv run pytest tests/test_docs.py tests/test_skill_layer.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the product surface.**

```bash
git add README.md AGENTS.md docs evals src/adversarial_friends/assets plugins \
  tests/test_docs.py tests/test_skill_layer.py
git commit -m "docs: describe guided adversarial friend sessions"
```

### Task 9: Verify the integrated UX and prepare handoff

**Files:**
- Verify only: all modified source, assets, plugin projection, docs, and tests

- [ ] **Step 1: Run focused behavioral tests.**

Run:

```bash
uv run pytest tests/test_sessionconfig.py tests/test_reviewprofiles.py \
  tests/test_guided_init.py tests/test_events.py tests/test_status.py \
  tests/test_profiles_command.py tests/test_resolve_discovery.py \
  tests/test_docs.py tests/test_skill_layer.py -q
```

Expected: PASS.

- [ ] **Step 2: Inspect user-visible help and safe default behavior.**

Run:

```bash
uv run afriend --help
uv run afriend init --guided --json
uv run afriend providers list
uv run afriend profiles list
```

Expected: all commands describe their boundary; `init --guided --json` writes no configuration and reports `quick` as the default profile.

- [ ] **Step 3: Run complete portable quality verification.**

Run: `PYTEST_ADDOPTS='--color=no' make quality`

Expected: every portable gate passes, including exact wheel contents and the full test suite.

- [ ] **Step 4: Inspect final worktree state.**

Run:

```bash
git diff --check main...HEAD
git status --short
git log --oneline main..HEAD
```

Expected: no whitespace errors; only intended committed changes; each slice is represented by a focused commit.
