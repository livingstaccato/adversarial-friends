# Afriend Activation, Host Friends, and Harness Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to execute this plan task by task,
> with a fresh implementer followed by specification and code-quality review.

**Goal:** Make `afriend to X` a narrow Codex skill invocation, make Codex a
visible but non-independent default friend when Codex hosts the run, and make
Antigravity usable through generic, provider-scoped authority and workspace
asset mechanisms that future harnesses can reuse.

**Architecture:** Keep adapter-local enforcement as the existing binary
`DENY`/`ALLOW` decision, but derive that decision from a new immutable
run-level `AuthorityPolicy`. Extend declarative adapters with digest-pinned
workspace assets staged through descriptor-rooted secure I/O into each
friend's private isolation directory. Model host participation explicitly on
`FriendSpec`, persist its advisory status, and exclude host verdicts from
independent quorum calculations without excluding host findings from reports.

**Tech Stack:** Python 3.11+, stdlib (`argparse`, `dataclasses`, `hashlib`,
`pathlib`, `tomllib`), pytest, mypy strict, Ruff, setuptools package data,
Codex skill/plugin assets.

---

## Task 1: Replace the global authority toggle with scoped provider grants

**Files:**

- Modify: `src/adversarial_friends/authority.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/setup.py`
- Modify: `src/adversarial_friends/commands/friends.py`
- Modify: `src/adversarial_friends/roster.py`
- Modify: `src/adversarial_friends/readiness.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/critique.py`
- Modify: `src/adversarial_friends/commands/crossexam.py`
- Modify: `src/adversarial_friends/commands/resume.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/commands/checkpoint.py`
- Modify: `src/adversarial_friends/commands/doctor.py`
- Modify: `src/adversarial_friends/commands/init.py`
- Modify: `src/adversarial_friends/rounds.py`
- Test: `tests/test_authority.py`
- Test: `tests/test_run_end_to_end_flags.py`
- Test: `tests/test_runmeta_migration.py`
- Test: `tests/test_readiness.py`
- Test: `tests/test_resume_findings.py`

### Steps

- [ ] Add failing unit tests for an immutable `AuthorityPolicy` that defaults
  to deny, accepts one or more known provider names, accepts `*` only alone,
  rejects unknown/duplicate/mixed grants, and returns `ExternalToolPolicy`
  from `for_provider(name)`.
- [ ] Add failing parser tests proving `--allow-external-tools` requires a
  value, is repeatable, and normalizes to a deterministic grant tuple before
  any readiness probe or dispatch.
- [ ] Add failing integration tests proving a grant for `agy` does not allow
  another provider, an explicit friend does not bypass denial, and
  `--unsafe-extra-args` requires the explicit global `*` grant at both setup
  and dispatch boundaries.
- [ ] Run the focused red tests:

  ```bash
  uv run pytest tests/test_authority.py tests/test_run_end_to_end_flags.py \
    tests/test_readiness.py -q
  ```

  Expected: failures showing the boolean/global authority model cannot meet
  the scoped contract.

- [ ] Implement `AuthorityPolicy` in `authority.py`; keep
  `ExternalToolPolicy` as the adapter-level decision and change
  `enforce_extra_args` to require `policy.allows_all`.
- [ ] Change the CLI flag to required-value `action="append"`; build and
  validate the run policy after loading the adapter registry and before any
  provider contact.
- [ ] Thread `AuthorityPolicy` through selection, readiness, run orchestration,
  resume, and round dispatch. At the adapter boundary call
  `policy.for_provider(spec.cli)` so `build_argv`, `enforce`, `_dispatch`, and
  each friend sidecar retain an exact provider-local decision.
- [ ] Make doctor/init use `AuthorityPolicy.deny_all()` and preserve per-provider
  deny probes even when another provider is granted.
- [ ] Bump run metadata schema and replace the persisted boolean grant with
  normalized `external_tool_grants: [string]` plus summary policy
  `deny|scoped-allow|allow`. Migrate an old true boolean to `['*']` for audit
  only; require an identical explicit current grant set to resume.
- [ ] Run focused green tests, then all tests touching authority/resume:

  ```bash
  uv run pytest tests/test_authority.py tests/test_run_end_to_end_flags.py \
    tests/test_runmeta_migration.py tests/test_readiness.py \
    tests/test_resume_findings.py -q
  ```

- [ ] Commit:

  ```bash
  git add src tests
  git commit -m "feat: scope external tool authority by provider"
  ```

## Task 2: Make the Codex host a default advisory friend

**Files:**

- Modify: `src/adversarial_friends/adapters.py`
- Modify: `src/adversarial_friends/cliargs.py`
- Modify: `src/adversarial_friends/commands/friends.py`
- Modify: `src/adversarial_friends/commands/crossexam.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/roster.py`
- Modify: `src/adversarial_friends/report.py`
- Modify: `src/adversarial_friends/trust.py`
- Test: `tests/test_roster.py`
- Test: `tests/test_run_end_to_end_roster.py`
- Test: `tests/test_run_end_to_end_crossexam.py`
- Test: `tests/test_verdicts.py`
- Test: `tests/test_report.py`
- Test: `tests/test_runmeta_migration.py`

### Steps

- [ ] Add failing parser/roster tests for a mutually exclusive
  `--include-self`/`--exclude-self` pair whose unset behavior includes Codex
  when `detect_host()` returns `codex`, excludes other detected hosts, and
  respects either explicit override.
- [ ] Add failing run tests proving every selected friend matching the
  detected host is persisted with `host_self_review=true` and
  `independent=false`, including explicitly selected host friends and resumed
  roster entries that predate these fields.
- [ ] Add failing judging tests proving host findings remain reportable but
  host verdicts cannot satisfy the two-independent-friend minimum, claim
  quorum, loop convergence, or gate clearance.
- [ ] Run the red tests:

  ```bash
  uv run pytest tests/test_roster.py tests/test_run_end_to_end_roster.py \
    tests/test_run_end_to_end_crossexam.py tests/test_verdicts.py \
    tests/test_report.py -q
  ```

- [ ] Extend `FriendSpec` with backward-compatible defaulted
  `independent=True` and `host_self_review=False` fields. Resolve the effective
  default only after host detection, then mark host specs regardless of how
  they entered the roster.
- [ ] Count only independent specs for judging-mode admission. Filter verdict
  judge identities and loop/gate settlement through the independent roster;
  continue dispatching the host and retaining its claims and advisory verdict
  rows for audit.
- [ ] Persist and render role/independence fields in friend rows and reports;
  allow old resume metadata to omit both fields safely.
- [ ] Run the focused tests green and commit:

  ```bash
  git add src tests
  git commit -m "feat: make codex host an advisory default friend"
  ```

## Task 3: Add generic digest-pinned workspace assets to adapters

**Files:**

- Create: `src/adversarial_friends/workspaceassets.py`
- Modify: `src/adversarial_friends/adapters.py`
- Modify: `src/adversarial_friends/readiness.py`
- Modify: `src/adversarial_friends/rounds.py`
- Modify: `src/adversarial_friends/report.py`
- Modify: `pyproject.toml`
- Create: `tests/test_workspaceassets.py`
- Modify: `tests/test_adapters.py`
- Modify: `tests/test_round_audit.py`
- Modify: `tests/test_report.py`

### Steps

- [ ] Add failing tests for `[[workspace_assets]]` parsing and validation:
  source and target must be relative, remain under their declared roots,
  contain no `..`, use unique targets, match a required SHA-256 digest, and
  be rejected on HTTP adapters.
- [ ] Add filesystem attack tests proving staging rejects a symlink at every
  parent depth, a symlink leaf, any pre-existing target (including a regular
  file), a changed source digest, and a source symlink escape.
- [ ] Add a dispatch test proving assets are staged only inside the friend's
  run-local isolation directory before provider contact, are audited in the
  friend sidecar, and disappear during ordinary cleanup. A staging failure
  must refuse only the affected friend and never contact its provider.
- [ ] Run the red tests:

  ```bash
  uv run pytest tests/test_workspaceassets.py tests/test_adapters.py \
    tests/test_round_audit.py -q
  ```

- [ ] Implement a frozen workspace-asset declaration and validation/staging
  module. Read sources beneath canonical package assets with descriptor-rooted
  secure reads; create target parents and target leaf with the existing
  `secureio` descriptor walk, `O_NOFOLLOW`, and exclusive creation.
- [ ] Extend `Adapter` and TOML loading generically. Validate packaged assets
  during readiness without writing, stage them after isolation exists and
  before argv construction, and represent staging failure as that friend's
  auditable failed outcome.
- [ ] Extend `Capability`/sidecar/report metadata with source digest, target,
  and staging status. Expand setuptools package data to include harness asset
  files recursively enough for wheels and editable installs.
- [ ] Run focused tests green and commit:

  ```bash
  git add src tests pyproject.toml
  git commit -m "feat: stage generic adapter workspace assets"
  ```

## Task 4: Configure Antigravity through the generic harness contract

**Files:**

- Create: `src/adversarial_friends/assets/harnesses/agy/afriend-reviewer.md`
- Modify: `src/adversarial_friends/assets/adapters/agy.toml`
- Modify: `tests/test_adapters.py`
- Modify: `tests/test_authority.py`
- Create: `tests/test_agy_harness.py`

### Steps

- [ ] Add failing adapter tests proving Agy declares the controlled agent as
  a digest-pinned workspace asset, selects `afriend-reviewer`, disables slash
  commands, requests plan mode and provider sandboxing, and still advertises
  `external_tools=uncontrolled`.
- [ ] Add a hermetic fake-CLI run proving Agy remains policy-blocked by
  default, becomes runnable only with `--allow-external-tools=agy`, stages the
  declared agent, and is stamped `external_tools=explicitly-allowed` without
  affecting the deny decision for any other provider.
- [ ] Run the red tests:

  ```bash
  uv run pytest tests/test_agy_harness.py tests/test_adapters.py \
    tests/test_authority.py -q
  ```

- [ ] Add the no-tools reviewer agent definition under packaged harness
  assets, calculate its SHA-256, and declare it in `agy.toml`. Add the verified
  Agy flags to adapter argv in option position before `--print`.
- [ ] Keep the grant language and capability truthful: the controlled agent
  is defense in depth, not proof that inherited tools are absent.
- [ ] Run focused tests green and commit:

  ```bash
  git add src tests
  git commit -m "feat: run agy with a controlled reviewer agent"
  ```

## Task 5: Narrow the Codex skill trigger and update operator documentation

**Files:**

- Modify: `src/adversarial_friends/assets/SKILL.md`
- Modify: `src/adversarial_friends/assets/references/modes.md`
- Modify: `src/adversarial_friends/assets/references/troubleshooting.md`
- Modify: `evals/evals.json`
- Modify: `README.md`
- Modify: `docs/architecture/README.md`
- Modify: `docs/architecture/*.puml` only where behavior is represented
- Mirror mechanically: `plugins/adversarial-friends/skills/adversarial-friends/`
- Modify tests under: `tests/test_docs.py`, `tests/test_skill_layer.py`,
  `tests/test_plugin.py`, and/or the existing closest equivalents

### Steps

- [ ] Add failing contract tests/evals for positive triggers `afriend ...`,
  `afriend to ...`, the full product name, and direct skill selection; add
  negative cases for generic `review`, `challenge`, and `poke holes` requests.
- [ ] Add doc tests covering Codex host friend+orchestrator semantics, the
  default round count and mode costs, scoped provider grants, explicit `*`,
  Agy's best-effort controlled agent, and the distinction between external
  tool authority and filesystem confinement.
- [ ] Update canonical `SKILL.md` so report mode remains the default, generic
  review wording no longer activates the skill, and shorthand target
  resolution is deterministic and bounded.
- [ ] Update README, references, architecture prose/diagrams, and evals to the
  shipped behavior. Do not describe Codex as independent and do not claim Agy
  tools are denied.
- [ ] Synchronize the canonical asset tree into the plugin mirror:

  ```bash
  make plugin-sync-copy
  make plugin-sync
  ```

- [ ] Validate the skill and focused documentation tests:

  ```bash
  python3 /Users/tim/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
    src/adversarial_friends/assets
  uv run pytest tests -q -k 'docs or skill or plugin'
  ```

- [ ] Commit:

  ```bash
  git add README.md docs evals src/adversarial_friends/assets plugins tests
  git commit -m "docs: define explicit afriend activation and harness policy"
  ```

## Task 6: Verify real packaging, live Agy behavior, and local Codex install

**Files:**

- Modify only if verification exposes a defect: implementation/tests/docs
- Inspect: built wheel contents and local Codex plugin cache

### Steps

- [ ] Run the portable release gates from a clean-enough worktree and fix any
  failure at its source:

  ```bash
  make quality
  ```

  Expected: Ruff format/lint, mypy strict, 777-line cap, plugin/version sync,
  wheel asset/install verification, and the complete pytest suite all pass.

- [ ] Inspect the wheel to prove the controlled-agent asset is package data,
  then install the wheel in an isolated environment and re-run `afriend
  doctor`/help smoke checks if the existing gate does not already cover the
  new grant syntax.
- [ ] Run one bounded Agy initialization smoke from a temporary staged
  workspace with `stream-json`; record the observed initialization tool list
  in the handoff. Treat non-empty tools as an honest limitation, not a reason
  to relabel capability as denied.
- [ ] Run one bounded end-to-end report through the local source:

  ```bash
  uv run afriend run \
    docs/superpowers/specs/2026-09-01-afriend-activation-host-friends-harness-authority-design.md \
    --mode report --friend agy:security --allow-external-tools=agy \
    --max-calls 1 --timeout 300
  ```

  Expected: Agy is contacted, the provider-scoped grant and staged asset are
  audited, and no other provider gains authority.

- [ ] Run the requesting-code-review workflow against the complete diff, fix
  findings, then rerun the affected focused tests and `make quality`.
- [ ] Refresh the local Codex plugin using the plugin-creator development
  cachebuster/reinstall workflow, verify the installed cache contains the
  new skill/harness assets, and perform a direct `afriend to ...` skill smoke.
- [ ] Commit any verification fixes. Leave publishing a new PyPI release as a
  separate explicit release action; local implementation and installation do
  not silently publish.

## Final self-review checklist

- [ ] Every design requirement has at least one named test or verification
  step; no implementation step depends on Agy-specific Python branching.
- [ ] Every signature change is carried through source, tests, metadata,
  resume, fake dispatch, and report paths; no placeholder or pseudocode is
  left in production files.
- [ ] Host claims remain useful while host verdicts are excluded from all
  independent judging calculations.
- [ ] Scoped grants remain provider-local at readiness, dispatch, audit, and
  resume boundaries; unsafe extra argv still requires global authority.
- [ ] Workspace staging validates source and destination at both static and
  run-time boundaries and never writes to caller home or checkout.
- [ ] Canonical assets, plugin mirror, wheel contents, installed plugin cache,
  docs, and evals agree.
