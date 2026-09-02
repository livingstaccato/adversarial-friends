# Confinement DNS and Scope Warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Restore DNS for bwrap-confined friends on systemd-resolved hosts and warn operators before automatic doc-scope runs begin.

**Architecture:** Keep resolver-target discovery in \`sandbox.py\`, which already owns bwrap argument construction and supports a fake host root for portable tests. Keep the semantic warning at the \`cmd_run\` boundary after snapshot scope is reconciled and before any round begins; metadata continues using the existing downgrade list. Document the artifact-location contract in canonical assets and synchronize the plugin mirror.

**Tech Stack:** Python 3.11 standard library, pytest, ruff, mypy, Make/uv.

---

### Task 1: Bind a systemd-resolved resolver target narrowly

**Files:**
- Modify: \`src/adversarial_friends/sandbox.py:123-140,291-317\`
- Test: \`tests/test_sandbox.py:104-176\`

- [ ] **Step 1: Write failing resolver-bind tests**

Add these tests after \`test_bwrap_binds_system_paths_read_only\`:

\`\`\`python
def test_bwrap_binds_a_resolved_resolver_target(tmp_path, policy):
    root = tmp_path / "root"
    target = root / "run/systemd/resolve/stub-resolv.conf"
    target.parent.mkdir(parents=True)
    target.write_text("nameserver 127.0.0.53\n")
    (root / "etc").mkdir()
    (root / "etc/resolv.conf").symlink_to("../run/systemd/resolve/stub-resolv.conf")

    argv = sandbox.linux_argv(policy, root=root)

    assert ["--ro-bind", "/run/systemd/resolve/stub-resolv.conf",
            "/run/systemd/resolve/stub-resolv.conf"] in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]


def test_bwrap_skips_a_regular_or_broken_resolver_target(tmp_path, policy):
    regular = tmp_path / "regular"
    (regular / "etc").mkdir(parents=True)
    (regular / "etc/resolv.conf").write_text("nameserver 1.1.1.1\n")
    broken = tmp_path / "broken"
    (broken / "etc").mkdir(parents=True)
    (broken / "etc/resolv.conf").symlink_to("../run/missing")

    assert "/run/systemd/resolve/stub-resolv.conf" not in sandbox.linux_argv(policy, root=regular)
    assert "/run/missing" not in sandbox.linux_argv(policy, root=broken)
\`\`\`

- [ ] **Step 2: Run test and verify it fails**

Run: \`uv run pytest tests/test_sandbox.py -k resolver -q\`

Expected: FAIL because \`linux_argv\` has no resolver-target bind.

- [ ] **Step 3: Implement the root-safe resolver helper**

Add \`_resolver_target_bind(root: Path) -> list[str]\` beside \`system_binds\`. It must inspect \`root / "etc/resolv.conf"\`; return \`[]\` unless it is a symlink resolving to a readable regular file beneath \`root\`. Convert the resolved host-root-relative path to the bwrap namespace path and return:

\`\`\`python
["--ro-bind", namespace_path, namespace_path]
\`\`\`

Append its result immediately after \`system_binds(root)\` in \`linux_argv\`. Do not bind a directory under \`/run\` and do not change network namespace flags.

- [ ] **Step 4: Run focused tests and formatter**

Run: \`uv run pytest tests/test_sandbox.py -q && uv run ruff format --check src/adversarial_friends/sandbox.py tests/test_sandbox.py\`

Expected: PASS.

- [ ] **Step 5: Commit the isolated change**

\`\`\`bash
git add src/adversarial_friends/sandbox.py tests/test_sandbox.py
git commit -m "fix: bind resolved DNS config in bwrap"
\`\`\`

### Task 2: Emit an unconditional preflight scope warning

**Files:**
- Modify: \`src/adversarial_friends/commands/environment.py:60-82\`
- Modify: \`src/adversarial_friends/commands/run.py:155-164\`
- Test: \`tests/test_run_end_to_end_basics.py\`

- [ ] **Step 1: Write a failing end-to-end warning test**

Add a test using an artifact under \`tmp_path\` and \`--no-progress\`:

\`\`\`python
def test_outside_repo_warns_before_doc_scope_dispatch(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")

    result = run_af(
        tmp_path, artifact, "--no-progress", "--friend", "fake:good"
    )

    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "doc scope" in result.stderr
    assert "repository snapshot" in result.stderr
\`\`\`

Add a loop assertion that the warning occurs once, not once per iteration, using the existing fake loop fixture/helpers.

- [ ] **Step 2: Run test and verify it fails**

Run: \`uv run pytest tests/test_run_end_to_end_basics.py -k "outside_repo or doc_scope" -q\`

Expected: FAIL because reconciliation records the downgrade but writes no preflight stderr warning.

- [ ] **Step 3: Add one semantic warning at the run boundary**

Add an environment helper returning the existing downgrade note only when a no-repository identity forced a scope change. Immediately after \`reconcile_snapshot_scope(...)\` in \`cmd_run\`, write one flushed line to \`sys.stderr\`:

\`\`\`python
print(
    "afriend: WARNING: <artifact> is outside a Git repository; friends will "
    "receive doc scope only, not a repository snapshot. Place the artifact "
    "inside the target repository to review its code.",
    file=sys.stderr,
    flush=True,
)
\`\`\`

Guard it with a local boolean so loop reconciliation cannot repeat it. Do not route this through \`Progress\`, since \`--no-progress\` must not suppress a semantic warning.

- [ ] **Step 4: Run focused end-to-end tests**

Run: \`uv run pytest tests/test_run_end_to_end_basics.py -k "outside_repo or doc_scope" -q\`

Expected: PASS, including \`--no-progress\` and exactly-one-warning assertions.

- [ ] **Step 5: Commit the warning change**

\`\`\`bash
git add src/adversarial_friends/commands/environment.py src/adversarial_friends/commands/run.py tests/test_run_end_to_end_basics.py
git commit -m "fix: warn before automatic doc scope"
\`\`\`

### Task 3: Document automatic scope and synchronize the plugin

**Files:**
- Modify: \`README.md:181\`
- Modify: \`src/adversarial_friends/assets/SKILL.md\`
- Modify: \`src/adversarial_friends/assets/references/troubleshooting.md\`
- Modify: \`plugins/adversarial-friends/skills/adversarial-friends/SKILL.md\`
- Modify: \`plugins/adversarial-friends/skills/adversarial-friends/references/troubleshooting.md\`
- Test: \`tests/test_docs.py\`

- [ ] **Step 1: Write failing documentation-contract assertions**

Extend the existing docs tests to require the README and canonical skill text to state that artifact location selects automatic repository scope, and require troubleshooting to distinguish untracked non-ignored artifacts from ignored artifacts.

\`\`\`python
assert "outside a Git repository" in readme
assert "repository snapshot" in canonical_skill
assert "untracked" in troubleshooting
assert "ignored" in troubleshooting
\`\`\`

- [ ] **Step 2: Run docs tests and verify they fail**

Run: \`uv run pytest tests/test_docs.py -q\`

Expected: FAIL because the current docs only describe the effect of doc scope, not the artifact-location decision or ignored-artifact behavior.

- [ ] **Step 3: Update canonical documentation and sync the mirror**

Add concise user-facing guidance: an artifact inside the target repository gets a snapshot; outside it yields visible doc scope; ordinary untracked files are included; gitignored files are excluded deliberately and must not be reviewed through a stale \`HEAD\` fallback. Run \`make plugin-sync\` to copy canonical assets into the plugin mirror.

- [ ] **Step 4: Run documentation and sync checks**

Run: \`uv run pytest tests/test_docs.py -q && make plugin-sync\`

Expected: PASS; plugin sync reports no drift after synchronization.

- [ ] **Step 5: Run complete quality and commit**

Run: \`make quality\`

Expected: all portable checks pass, including plugin sync, version sync, wheel installation, formatting, lint, mypy, and tests.

\`\`\`bash
git add README.md src/adversarial_friends/assets plugins/adversarial-friends tests/test_docs.py
git commit -m "docs: explain artifact scope selection"
\`\`\`

## Plan self-review

- Spec coverage: Task 1 covers the narrow resolver bind and safe missing-target behavior; Task 2 covers unconditional, once-only semantic warning behavior; Task 3 covers the explicit scope and ignored-artifact documentation contract and mirror sync.
- Placeholder scan: no deferred work or unspecified implementation steps remain within the approved scope.
- Type consistency: \`_resolver_target_bind\` returns the same \`list[str]\` argument fragments as \`system_binds\`; the run warning is independent of \`Progress\` and retained downgrade metadata.

