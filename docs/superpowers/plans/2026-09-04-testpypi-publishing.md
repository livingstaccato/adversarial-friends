# TestPyPI Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual, OIDC-only TestPyPI rehearsal workflow for all three public distribution names.

**Architecture:** A new top-level GitHub Actions workflow builds the exact same verified six-artifact bundle as production. Three serial, least-privilege publishing jobs stage and upload one distribution each in canonical-first order; this follows PyPA's one-publisher-action-per-job limitation and has no GitHub release job.

**Tech Stack:** GitHub Actions, PyPA gh-action-pypi-publish, pytest.

---

### Task 1: Lock the TestPyPI workflow contract

**Files:**
- Modify: `tests/test_release_workflow.py`
- Create: `.github/workflows/test-release.yml`

- [ ] **Step 1: Write the failing test**

Add a `TEST_WORKFLOW` reader and a `test_test_release_workflow_is_manual_and_testpypi_only` test that requires `workflow_dispatch`, the `Build and verify six release artifacts` step, `repository-url: https://test.pypi.org/legacy/`, all three distinct TestPyPI environments, exactly three `id-token: write` permissions, and no `github-release` job.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `uv run pytest tests/test_release_workflow.py -k test_test_release -q`

Expected: FAIL because `.github/workflows/test-release.yml` does not exist.

- [ ] **Step 3: Implement the minimal workflow**

Create a manual `test-release.yml` with a build job that checks out the selected revision, installs uv, runs `ci/verify_release_distributions.sh dist`, and stores `python-package-distributions`. Add one serial publishing job per distribution: `publish-afriend` in `testpypi-afriend`, `publish-adversarial-friends` in `testpypi-adversarial-friends`, and `publish-afriends` in `testpypi-afriends`. Each job stages and publishes exactly one distribution. Pin every action to a 40-character SHA and provide `repository-url: https://test.pypi.org/legacy/` to every publishing action.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `uv run pytest tests/test_release_workflow.py -k test_test_release -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/test-release.yml tests/test_release_workflow.py
git commit -m "ci: add manual TestPyPI publishing workflow"
```

### Task 2: Verify the repository and configure publishers

**Files:**
- Modify: `README.md` only if its release documentation needs a TestPyPI entry.

- [ ] **Step 1: Run the complete portable gate**

Run: `make quality`

Expected: PASS, including release workflow contract tests and six-artifact installation checks.

- [ ] **Step 2: Push the committed workflow to main**

Run: `git push origin main`

Expected: remote `main` accepts the commit, making `test-release.yml` a valid trusted-publisher identity.

- [ ] **Step 3: Register TestPyPI trusted publishers**

In the authenticated TestPyPI session, add pending publishers:

| Project | Repository | Workflow | Environment |
| --- | --- | --- | --- |
| `afriend` | `livingstaccato/afriend` | `test-release.yml` | `testpypi-afriend` |
| `adversarial-friends` | `livingstaccato/afriend` | `test-release.yml` | `testpypi-adversarial-friends` |
| `afriends` | `livingstaccato/afriend` | `test-release.yml` | `testpypi-afriends` |

- [ ] **Step 4: Inspect the resulting TestPyPI publisher records**

Read each project’s publishing-settings page and confirm its repository,
workflow, and environment exactly match the table. Do not run the workflow:
the existing version may already exist on TestPyPI and a TestPyPI release is a
separate, deliberate action.
