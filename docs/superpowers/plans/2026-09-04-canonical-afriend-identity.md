# Canonical afriend Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make afriend the canonical GitHub and PyPI identity, with metadata-only adversarial-friends and afriends compatibility distributions.

**Architecture:** The root project remains the sole runtime package, import, CLI, and plugin. Two independent setuptools projects in compatibility-distributions contain only metadata and an exact dependency on the root version. One verifier builds and inspects all six release artifacts before the release workflow publishes canonical-first.

**Tech Stack:** Python 3.11–3.13, setuptools, uv, pytest, GitHub Actions, PyPI trusted publishing.

---

## File structure

| Path | Responsibility |
| --- | --- |
| compatibility-distributions/adversarial-friends/pyproject.toml | Former-name metadata-only distribution. |
| compatibility-distributions/afriends/pyproject.toml | Typo-reservation metadata-only distribution. |
| ci/verify_release_distributions.sh | Build, archive, and isolated-install verification. |
| tests/test_compatibility_distributions.py | Packaging contracts. |
| scripts/check_version_sync.py | Version/dependency coherence. |
| .github/workflows/release.yml | Ordered release of all artifacts. |

### Task 1: Add the redirect distributions

**Files:**
- Create: compatibility-distributions/adversarial-friends/pyproject.toml
- Create: compatibility-distributions/afriends/pyproject.toml
- Create: tests/test_compatibility_distributions.py

- [ ] **Step 1: Write the failing metadata contract**

~~~python
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_compatibility_distributions_are_exact_metadata_only_aliases() -> None:
    for name in ("adversarial-friends", "afriends"):
        project = ROOT / "compatibility-distributions" / name
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["name"] == name
        assert data["project"]["version"] == VERSION
        assert data["project"]["dependencies"] == [f"afriend=={VERSION}"]
        assert data["tool"]["setuptools"]["packages"] == []
        assert "scripts" not in data["project"]
        assert not any(path.suffix == ".py" for path in project.rglob("*.py"))
~~~

- [ ] **Step 2: Verify red**

Run: uv run pytest tests/test_compatibility_distributions.py -q

Expected: FAIL because the two metadata files do not exist.

- [ ] **Step 3: Add minimal project metadata**

For adversarial-friends, create:

~~~toml
[build-system]
requires = ["setuptools>=77", "wheel>=0.43"]
build-backend = "setuptools.build_meta"

[project]
name = "adversarial-friends"
version = "0.6.1"
description = "Compatibility package for afriend"
requires-python = ">=3.11"
license = "MIT"
dependencies = ["afriend==0.6.1"]

[tool.setuptools]
packages = []
~~~

Create the same structure for afriends, changing name and description. Do not create Python modules, package data, console entry points, or plugin payloads.

- [ ] **Step 4: Verify green and commit**

Run: uv run pytest tests/test_compatibility_distributions.py -q

Expected: 1 passed.

~~~bash
git add compatibility-distributions tests/test_compatibility_distributions.py
git commit -m "feat: add afriend compatibility distributions"
~~~

### Task 2: Prove artifacts and isolated installs

**Files:**
- Create: ci/verify_release_distributions.sh
- Modify: Makefile
- Modify: tests/test_compatibility_distributions.py

- [ ] **Step 1: Write the failing release-verifier test**

~~~python
import subprocess


def test_release_verifier_builds_and_smokes_all_three_distributions() -> None:
    result = subprocess.run(
        ["bash", "ci/verify_release_distributions.sh"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok: verified afriend, adversarial-friends, and afriends" in result.stdout
~~~

- [ ] **Step 2: Verify red**

Run: uv run pytest tests/test_compatibility_distributions.py::test_release_verifier_builds_and_smokes_all_three_distributions -q

Expected: FAIL because the verifier is absent.

- [ ] **Step 3: Implement the verifier**

Use a scratch directory and these three builds:

~~~bash
uv build --wheel --sdist --out-dir "$dist" .
uv build --wheel --sdist --out-dir "$dist" compatibility-distributions/adversarial-friends
uv build --wheel --sdist --out-dir "$dist" compatibility-distributions/afriends
uvx --from twine==7.0.0 twine check --strict "$dist"/*
~~~

Require six files: one wheel and sdist per distribution. Inspect alias wheels with zipfile: every member must be under *.dist-info/; METADATA must contain Requires-Dist: afriend ==<VERSION>. In fresh venvs install canonical, then each alias with --no-index --find-links "$dist". From /tmp, assert the only executable is afriend, afriend --version matches, import afriend works, and importlib.util.find_spec finds neither alias import. Emit the test's final ok: line.

Add target:

~~~make
release-distributions: ## Build and smoke-test canonical and compatibility distributions
	ci/verify_release_distributions.sh
~~~

Add it to .PHONY and quality.

- [ ] **Step 4: Verify green and commit**

Run: uv run pytest tests/test_compatibility_distributions.py -q && make release-distributions

Expected: focused tests pass and all three isolated installs pass.

~~~bash
git add ci/verify_release_distributions.sh Makefile tests/test_compatibility_distributions.py
git commit -m "test: verify compatibility distribution artifacts"
~~~

### Task 3: Enforce release coherence

**Files:**
- Modify: scripts/check_version_sync.py, tests/test_version_sync.py
- Modify: .github/workflows/release.yml, tests/test_release_workflow.py

- [ ] **Step 1: Write failing version and workflow assertions**

Make tests require both compatibility projects to have their own name, VERSION, and precisely one dependency, afriend==<VERSION>. Require the release workflow to build all three projects, invoke the verifier, upload six artifacts, and have serial publish steps ordered canonical afriend, then adversarial-friends, then afriends.

- [ ] **Step 2: Verify red**

Run: uv run pytest tests/test_version_sync.py tests/test_release_workflow.py -q

Expected: FAIL because aliases are not version-checked and publishing is presently bulk/unordered.

- [ ] **Step 3: Implement the coherence gates**

Add this constant to scripts/check_version_sync.py and use tomllib to validate each file:

~~~python
COMPATIBILITY_PROJECTS = (
    ("adversarial-friends", Path("compatibility-distributions/adversarial-friends/pyproject.toml")),
    ("afriends", Path("compatibility-distributions/afriends/pyproject.toml")),
)
~~~

Missing metadata, wrong name/version, or any dependency list except exactly the root's matching pinned distribution must return 1.

In release.yml, build the root and aliases into dist, invoke the verifier before artifact upload, and publish separately staged directories with three pinned pypa/gh-action-pypi-publish actions in that order. Keep main-ancestry validation, OIDC-only auth, the protected pypi environment, and GitHub release dependent on publish. Release all six artifacts.

- [ ] **Step 4: Verify green and commit**

Run: uv run pytest tests/test_version_sync.py tests/test_release_workflow.py -q && python3 scripts/check_version_sync.py

Expected: selected tests pass and checker exits 0.

~~~bash
git add scripts/check_version_sync.py tests/test_version_sync.py .github/workflows/release.yml tests/test_release_workflow.py
git commit -m "ci: publish canonical afriend before compatibility aliases"
~~~

### Task 4: Cut over first-party identity

**Files:**
- Modify: VERSION, CHANGELOG.md, pyproject.toml, README.md, AGENTS.md, docs/README.md
- Modify: plugin marketplace/manifests, current skills, and docs tests
- Rename: docs/images/brand/adversarial-friends-* to docs/images/brand/afriend-*
- Regenerate: architecture images/SVGs where their source changes

- [ ] **Step 1: Write failing current-identity documentation tests**

Extend tests/test_docs.py to require current first-party docs and metadata to use https://github.com/livingstaccato/afriend, raw URLs under /afriend/main/, and afriend-* brand assets. Exclude dated historical plans/reports from this current-state assertion.

- [ ] **Step 2: Verify red**

Run: uv run pytest tests/test_docs.py -q

Expected: FAIL on current old-repository URLs and old brand filenames.

- [ ] **Step 3: Make the update**

Set all root/plugin base versions to 0.6.1; update the changelog with the canonical identity and both aliases. Update package URLs, install/clone/issue links, plugin/local-marketplace metadata, skill docs, raw image links, and names of brand files. Keep only explicit compatibility or dated historical uses of the former name. Do not label current behavior “legacy.” Run make plugin-sync-copy after canonical-asset changes.

- [ ] **Step 4: Regenerate, verify, and commit**

Run: make diagrams && make plugin-sync-copy && uv run pytest tests/test_docs.py -q && python3 scripts/check_version_sync.py && python3 scripts/check_plugin_sync.py

Expected: every command exits 0.

~~~bash
git add VERSION CHANGELOG.md pyproject.toml README.md AGENTS.md docs .agents plugins tests/test_docs.py
git commit -m "docs: make afriend the canonical identity"
~~~

### Task 5: Full verification and external cutover

- [ ] **Step 1: Complete local verification**

Run: make quality

Expected: every portable quality gate passes, including full tests and compatibility artifact checks.

- [ ] **Step 2: Audit current first-party references**

Run: rg -n "livingstaccato/adversarial-friends|uv tool install.*adversarial-friends|git clone.*adversarial-friends" README.md AGENTS.md pyproject.toml docs plugins .agents .github || true

Expected: no current-identity references; retained historical evidence is reviewed individually.

- [ ] **Step 3: Rename GitHub then repoint origin**

~~~bash
gh repo rename afriend --yes
git remote set-url origin https://github.com/livingstaccato/afriend.git
git ls-remote --get-url origin
~~~

Expected: canonical origin, while GitHub retains its automatic old-URL redirect.

- [ ] **Step 4: Configure PyPI before tagging**

Set the existing adversarial-friends publisher to owner livingstaccato, repository afriend, workflow .github/workflows/release.yml, environment pypi. Add pending publishers with exactly that tuple for new afriend and afriends projects. Confirm all three before the tag.

- [ ] **Step 5: Push and release 0.6.1**

~~~bash
git push origin main
git tag -a v0.6.1 -m "afriend 0.6.1"
git push origin v0.6.1
~~~

Monitor until builds, all three PyPI publishes, and GitHub release creation succeed. Confirm published metadata and isolated install for all three distributions and six GitHub release assets. Leave failed v0.6.0 immutable; a later correction is a new coordinated version.
