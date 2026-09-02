# Resume Authority and Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make resumed runs durable across adapter-bundle changes without weakening explicit authority re-authorization, and make the user-facing isolation and environment documentation match runtime behavior.

**Architecture:** A resumed run must reconstruct its authority policy from the exact, syntax-valid grants the user reasserts on the resume CLI. Fresh runs continue to validate grants against the current adapter registry. Scope selects staged inputs, while adapter controls and OS confinement are separate enforcement layers.

**Tech Stack:** Python 3.11+, pytest, PlantUML, Markdown.

---

### Task 1: Make restored authority policy load-bearing and adapter-drift tolerant

**Files:**
- Modify: `tests/test_authority_resume_external_tools.py`
- Modify: `src/adversarial_friends/commands/runmeta_restore.py`
- Modify: `src/adversarial_friends/commands/setup.py`

- [ ] **Step 1: Write failing regression tests.** Replace the unknown-provider rejection tests with a test that resumes a run whose saved and reasserted grant is `["future"]`, then asserts the restored policy allows only `future` even with an empty current registry. Retain a test proving a fresh invocation with `["future"]` is rejected against that registry.

- [ ] **Step 2: Run the regression tests before implementation.**

Run: `uv run pytest tests/test_authority_resume_external_tools.py -q`

Expected: FAIL because restore currently revalidates the historical grant against current bundled adapters, or setup rebuilds that invalid policy.

- [ ] **Step 3: Implement the minimal policy handoff.** In `restore_args`, validate `allow_external_tools` syntax through `AuthorityPolicy` without consulting the mutable adapter registry, after exact normalized reassertion. Attach that immutable policy to restored arguments. In `prepare_run`, use this restored policy when present; keep `AuthorityPolicy.from_grants(..., registry)` for fresh invocations.

- [ ] **Step 4: Run the regression tests after implementation.**

Run: `uv run pytest tests/test_authority_resume_external_tools.py -q`

Expected: PASS.

### Task 2: Correct active documentation, help, and architecture source

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/run-flow.puml`
- Modify: `src/adversarial_friends/assets/references/modes.md`
- Modify: `src/adversarial_friends/cliargs.py`
- Regenerate: `docs/architecture/run-flow.png`
- Regenerate: `docs/architecture/run-flow.svg`

- [ ] **Step 1: Correct active prose.** State that scope selects a repo worktree or artifact-only staged input, with adapter read-only controls and OS confinement enforced separately. Refer to the provider as Antigravity in active user-facing prose. State that `--pass-env` permits variables to every exec-based friend, subject to each adapter's environment filtering.

- [ ] **Step 2: Correct the diagram source.** Make its isolation branch depend on `spec.scope`, not adapter readonly capability, and identify the staged reviewer as Antigravity while preserving the `agy` adapter name where it is a CLI identifier.

- [ ] **Step 3: Regenerate and verify diagrams.**

Run: `make diagrams && git diff --check && make plugin-sync`

Expected: regenerated PNG/SVG match source; no whitespace errors; canonical skill assets and plugin mirror remain synchronized.

### Task 3: Full verification and final review

**Files:**
- Verify only

- [ ] **Step 1: Run targeted tests.**

Run: `uv run pytest tests/test_authority_resume_external_tools.py tests/test_authority.py tests/test_runmeta_migration.py -q`

Expected: PASS.

- [ ] **Step 2: Run repository quality gate.**

Run: `make quality`

Expected: exit 0.

- [ ] **Step 3: Review the final diff.** Confirm fresh runs remain registry-strict, resumes require exact reassertion, and active docs neither overstate isolation nor narrow `--pass-env` exposure.
