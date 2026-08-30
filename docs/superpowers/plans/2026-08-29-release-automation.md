# Adversarial Friends Release Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tag-triggered, trusted-publishing workflow that releases only a version-matched commit already merged into `main`, publishes verified distributions to PyPI, and then creates the matching GitHub release.

**Architecture:** A read-only build job validates the tag, builds and checks immutable distributions, and uploads them as an artifact. A separate `pypi` environment job alone receives an OIDC token and publishes that artifact; a final contents-write job creates the GitHub release from the same files and the matching changelog section.

**Tech Stack:** GitHub Actions, uv, Twine 7.0.0, PyPI Trusted Publishing, pytest, GitHub CLI.

---

## File map

- `.github/workflows/release.yml` — version-tag trigger, validation, isolated build, trusted publish, and GitHub release jobs.
- `tests/test_release_workflow.py` — repository-level contract for the security and ordering invariants in the workflow.
- `docs/superpowers/specs/2026-08-29-release-automation-design.md` — approved release design and external prerequisites.

### Task 1: Lock the release workflow contract

**Files:**
- Create: `tests/test_release_workflow.py`
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write failing workflow-contract tests**

Create focused tests that read `.github/workflows/release.yml` or an empty
string when it is absent. Assert that it:

- triggers on `v*` tag pushes;
- defaults to `contents: read`;
- validates `GITHUB_REF_NAME` against `VERSION` and requires the commit to be
  an ancestor of `origin/main`;
- builds a wheel and source distribution, runs strict Twine metadata checks,
  and smoke-tests the wheel;
- keeps `id-token: write` out of the build job and only in the PyPI job;
- passes the same named artifact through build, publish, and GitHub-release
  jobs;
- makes the GitHub release depend on successful PyPI publication;
- pins every `uses:` reference to a 40-character commit SHA.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run pytest tests/test_release_workflow.py -q
```

Expected: FAIL because `.github/workflows/release.yml` does not exist and none
of the release invariants are present.

- [ ] **Step 3: Implement the minimal release workflow**

Create `.github/workflows/release.yml` with three jobs:

- `build`: pinned checkout/setup-uv/upload-artifact actions, tag/version and
  main-ancestry checks, `uv build --wheel --sdist`,
  `uvx --from twine==7.0.0 twine check --strict dist/*`, exact filename checks,
  and an installed-wheel version smoke test;
- `publish`: `needs: build`, the `pypi` environment, job-local
  `id-token: write`, pinned download-artifact and PyPI publish actions;
- `github-release`: `needs: publish`, job-local `contents: write`, extraction
  of the matching changelog section, and `gh release create` with both files.

Use the current verified action commits:

```text
actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1       # v7.0.1
astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d       # v10.0.1
actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
uv run pytest tests/test_release_workflow.py -q
```

Expected: every workflow-contract test passes.

- [ ] **Step 5: Commit the workflow and contract**

```bash
git add .github/workflows/release.yml tests/test_release_workflow.py
git commit -m "ci: automate trusted package releases"
```

### Task 2: Validate and integrate the automation

**Files:**
- Verify: `.github/workflows/release.yml`
- Verify: repository quality gates

- [ ] **Step 1: Validate workflow parsing and the complete repository**

Run:

```bash
act --list
make quality
git diff --check main...HEAD
```

Expected: workflow enumeration succeeds, all portable quality gates pass, and
the diff has no whitespace errors.

- [ ] **Step 2: Review the committed diff against the approved design**

Verify that only the publish job has OIDC permission, artifact construction is
separate from publication, the release job follows publication, and no secret,
token, or skip-existing behavior was introduced.

- [ ] **Step 3: Merge into `main` and re-run `make quality`**

Fast-forward `main` to the release branch, run the full quality gate from
`main`, and remove the worktree and branch only after it passes.

### Task 3: Publish 0.2.0 through the gated workflow

**Files:**
- External: GitHub `pypi` environment
- External: PyPI trusted-publisher record
- Tag: `v0.2.0`

- [ ] **Step 1: Configure and verify deployment prerequisites**

Create the GitHub `pypi` environment if absent. Confirm the PyPI project trusts
`livingstaccato/adversarial-friends`, workflow `release.yml`, environment
`pypi`. Do not tag while either side is unverified.

- [ ] **Step 2: Push `main` and require hosted CI to pass**

```bash
git push origin main
gh run watch --exit-status
```

Expected: every Python 3.11-3.13 CI matrix job passes for the pushed commit.

- [ ] **Step 3: Tag and monitor the release**

Create annotated tag `v0.2.0`, push it, and watch the Release workflow to a
successful terminal state, including any environment approval.

- [ ] **Step 4: Verify the public release from a clean tool environment**

```bash
uvx --refresh --from adversarial-friends==0.2.0 afriend --version
uvx --refresh --from adversarial-friends==0.2.0 afriend doctor
```

Expected: the version command reports `afriend 0.2.0`; doctor starts and
reports the available local friends without an installation or import error.
