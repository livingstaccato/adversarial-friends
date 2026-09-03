# DNS and Failure-Visibility Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify Linux resolver confinement and make a zero-response review visibly incomplete by default without weakening sandbox safety.

**Architecture:** Preserve a resolver bind's host source separately from its namespace destination, so a fake resolver layout can be exercised in real Bubblewrap. A small pure completeness projection turns persisted friend rows into one bounded summary consumed by terminal output, reports, and named-run status.

**Tech Stack:** Python 3.12 standard library, pytest, Bubblewrap on Linux CI, package-data plugin mirror.

---

## File map

- `src/adversarial_friends/sandbox.py` and `tests/test_sandbox.py`: resolver-source binding and Linux `bwrap` regression coverage.
- `src/adversarial_friends/reviewcompleteness.py` and `tests/test_reviewcompleteness.py`: pure incomplete-review projection.
- `src/adversarial_friends/cliargs.py`, `commands/runmeta.py`, `report.py`, and `commands/status.py`: configured display and persisted presentation.
- `README.md`, canonical assets, mirrored plugin assets, and focused tests: unsafe-override contract.

### Task 1: Test resolver target layouts in an actual namespace

**Files:**

- Modify: `src/adversarial_friends/sandbox.py:154-184`
- Modify: `tests/test_sandbox.py:152-190,255-340`

- [ ] **Step 1: Write failing unit tests for all resolver layouts**

Add `test_resolver_bind_uses_real_source_and_original_namespace_path`, parametrized with these `(link, target)` pairs: `("../run/systemd/resolve/stub-resolv.conf", "run/systemd/resolve/stub-resolv.conf")`, `("../run/resolvconf/resolv.conf", "run/resolvconf/resolv.conf")`, and `("../run/NetworkManager/resolv.conf", "run/NetworkManager/resolv.conf")`. Each fixture creates the target under `tmp_path / "root"`, creates `/etc/resolv.conf` as the link, calls `sandbox.linux_argv(policy, root=root)`, and asserts a three-argument bind exactly equal to `["--ro-bind", str(source), f"/{target}"]`.

- [ ] **Step 2: Run the test and verify RED**

Run `uv run pytest tests/test_sandbox.py::test_resolver_bind_uses_real_source_and_original_namespace_path -v`. Expected: FAIL because the existing argv rewrites the source to the namespace path.

- [ ] **Step 3: Implement the minimal resolver fix**

In `_resolver_binds`, return `["--ro-bind", str(source), str(namespace_target)]`. Do not change the safe-target checks and do not bind an entire `/run` directory.

- [ ] **Step 4: Add a real Linux-only Bubblewrap regression test**

Create a fake root with each resolver symlink and target. Run `bwrap` with the fake `/etc`, the helper's precise source/destination bind, necessary host runtime paths, and `/bin/cat /etc/resolv.conf`. Skip only when the platform is not Linux or `bwrap` is absent. Assert exit code zero and the expected nameserver in stdout.

- [ ] **Step 5: Verify and commit**

Run `uv run pytest tests/test_sandbox.py -q`. Expected: PASS; Linux executes the real namespace test and other hosts skip it. Commit with `git add src/adversarial_friends/sandbox.py tests/test_sandbox.py && git commit -m "test: verify resolver binds across Linux layouts"`.

### Task 2: Persist and present a zero-response completeness summary

**Files:**

- Create: `src/adversarial_friends/reviewcompleteness.py`
- Create: `tests/test_reviewcompleteness.py`
- Modify: `src/adversarial_friends/cliargs.py:184-246`
- Modify: `src/adversarial_friends/commands/runmeta.py:657-756`
- Modify: `src/adversarial_friends/report.py:440-565`
- Modify: `src/adversarial_friends/commands/status.py:307-471`
- Modify: `tests/test_cliargs.py`, `tests/test_report.py`, `tests/test_status.py`, and `tests/test_run_end_to_end_basics.py`

- [ ] **Step 1: Write failing pure-projection tests**

Write a test whose rows are one independent `{"name": "codex-security", "status": "failed: DNS temporary failure"}` and one advisory `{"name": "host", "independent": False, "status": "ok"}`. Its expected projection is `{"state": "incomplete", "answered": 0, "dispatched": 1, "reasons": ["codex-security: DNS temporary failure"], "message": "review incomplete: 0/1 friends answered; codex-security: DNS temporary failure"}`. Add a separate test proving an independent `ok` row returns `None`.

- [ ] **Step 2: Run the projection test and verify RED**

Run `uv run pytest tests/test_reviewcompleteness.py -v`. Expected: FAIL because `reviewcompleteness` does not exist.

- [ ] **Step 3: Implement the pure projection**

Implement `from_friends(rows: Iterable[Mapping[str, object]]) -> dict[str, object] | None`. Count only independent terminal rows as dispatched, count `ok`/`succeeded` as answered, and return `None` unless dispatched is positive and answered is zero. Normalize `failed:`/`skipped:` display text, use deterministic ordering, bound count and length through the existing diagnostic policy, and read no files.

- [ ] **Step 4: Write failing integration tests for metadata, report, status, and CLI presentation**

Use an all-failed fake-friend run. Assert exit `1`, stderr contains `review incomplete: 0/1 friends answered`, terminal `run.json` contains `review_completeness.state == "incomplete"`, the report contains the same message, and `status.summarize(...)["review_completeness"]["answered"] == 0`. Add parser/end-to-end coverage for `--failure-summary report-only`, which preserves exit code, metadata, report, and status but omits the terminal line. Assert invalid values are argparse errors.

- [ ] **Step 5: Run the focused suite and verify RED**

Run `uv run pytest tests/test_reviewcompleteness.py tests/test_cliargs.py tests/test_report.py tests/test_status.py tests/test_run_end_to_end_basics.py -q`. Expected: FAIL on the absent argument and absent `review_completeness` fields.

- [ ] **Step 6: Wire the projection through the terminal path**

Add `--failure-summary` with choices `terminal` and `report-only`, default `terminal`. In `finish_run`, derive and persist `review_completeness` from finalized `meta["friends"]` before rendering. Print its `message` through `decide_exit` only when policy is `terminal` and no higher-priority runtime/auth/quorum detail applies. Render `## Review completeness` before `## Friends`, saying no artifact conclusion follows from zero answers. Safely project the mapping in `status.summarize` and human status; bump `STATUS_SCHEMA_VERSION` if required by its compatibility contract.

- [ ] **Step 7: Verify and commit**

Run `uv run pytest tests/test_reviewcompleteness.py tests/test_cliargs.py tests/test_report.py tests/test_status.py tests/test_run_end_to_end_basics.py -q`. Expected: PASS. Commit with `git add src/adversarial_friends/reviewcompleteness.py tests/test_reviewcompleteness.py src/adversarial_friends/cliargs.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/report.py src/adversarial_friends/commands/status.py tests/test_cliargs.py tests/test_report.py tests/test_status.py tests/test_run_end_to_end_basics.py && git commit -m "fix: surface incomplete zero-response reviews"`.

### Task 3: Clarify the unsafe override without changing authority

**Files:**

- Modify: `src/adversarial_friends/cliargs.py:221-224`
- Modify: `src/adversarial_friends/report.py:502-530`
- Modify: `README.md`, `src/adversarial_friends/assets/**`, and `plugins/adversarial-friends/skills/adversarial-friends/**`
- Test: `tests/test_cliargs.py`, `tests/test_report.py`, and `tests/test_plugin_sync.py`

- [ ] **Step 1: Write failing wording tests**

Assert parser help contains `OS confinement` and `same-user filesystem read access`. Assert a report with an unconfined executable friend identifies that retained authority. Do not change the dispatch rule or flag authority.

- [ ] **Step 2: Run focused tests and verify RED**

Run `uv run pytest tests/test_cliargs.py tests/test_report.py -q`. Expected: FAIL because current help has no authority explanation and report wording does not identify the explicit override.

- [ ] **Step 3: Make wording precise and sync assets**

Use help text stating that the flag allows a provider without a read-only mode to run without OS confinement and it may read with the invoking user's filesystem authority. In the report distinguish write protection from lack of OS confinement. In README and canonical skills recommend installing `bwrap`/`sandbox-exec` or choosing a verified self-confining provider, call the flag explicit risk acceptance, and state that a verified read-only provider does not need it. Synchronize the plugin mirror from canonical assets.

- [ ] **Step 4: Verify and commit**

Run `make plugin-sync && uv run pytest tests/test_cliargs.py tests/test_report.py -q`. Expected: PASS. Commit with `git add README.md src/adversarial_friends/cliargs.py src/adversarial_friends/report.py src/adversarial_friends/assets plugins/adversarial-friends/skills/adversarial-friends tests/test_cliargs.py tests/test_report.py && git commit -m "docs: clarify unconfined friend override"`.

### Task 4: Verify the complete hardening change

**Files:**

- Modify only the file exposed by a failing verification gate.

- [ ] **Step 1: Run the portable quality gate**

Run `make quality`. Expected: lint, strict mypy, the 777-line cap, asset/plugin sync, version sync, wheel/isolated installation, and all tests PASS.

- [ ] **Step 2: Verify Linux CI semantics**

Run `make act-ci` when Docker is available; otherwise push the branch and inspect Linux CI before merge. Expected: Bubblewrap resolver coverage executes, rather than skips, in Linux CI.

- [ ] **Step 3: Prepare review**

Run `git diff main...HEAD --check && git log --oneline main..HEAD`. Expected: no whitespace errors; design plus three focused implementation commits. Request final adversarial/code review before integration.

## Plan self-review

- Task 1 covers the DNS claim with layout and actual namespace evidence.
- Task 2 covers classified all-failed visibility, default stderr, configurable quiet output, report, metadata, and status.
- Task 3 covers the override’s user-facing authority contract and plugin mirror.
- Task 4 covers all repository quality gates and Linux-specific evidence.
