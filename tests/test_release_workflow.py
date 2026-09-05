from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION = ROOT / "VERSION"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""


def top_level_block(name: str) -> str:
    lines = workflow_text().splitlines()
    marker = f"{name}:"
    try:
        start = lines.index(marker)
    except ValueError:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
    return "\n".join(lines[start:end]).rstrip()


def job_block(name: str) -> str:
    lines = workflow_text().splitlines()
    marker = f"  {name}:"
    try:
        start = lines.index(marker)
    except ValueError:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
        if line.startswith("  ") and not line.startswith("    "):
            end = index
            break
    return "\n".join(lines[start:end])


def test_release_workflow_is_triggered_only_by_version_tags():
    assert top_level_block("on") == 'on:\n  push:\n    tags: ["v*"]'


def test_release_workflow_rejects_mismatched_or_unmerged_tags():
    build = job_block("build")

    assert '"${GITHUB_REF_NAME}" = "v${version}"' in build
    assert 'git merge-base --is-ancestor "${GITHUB_SHA}" "origin/main"' in build
    assert "fetch-depth: 0" in build


def test_current_version_has_nonempty_changelog_section():
    version = VERSION.read_text(encoding="utf-8").strip()
    changelog = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(version)}\n(?P<body>.*?)(?=^## |\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )

    assert match is not None
    assert match.group("body").strip()


def test_release_build_validates_changelog_before_artifact_upload():
    build = job_block("build")

    validation = build.index("Extract and verify release notes")
    upload = build.index("Store release distributions")
    assert validation < upload
    assert "CHANGELOG.md" in build[validation:upload]
    assert "test -s release-notes.md" in build[validation:upload]


def test_release_build_verifies_both_distributions_and_installed_cli():
    build = job_block("build")

    assert "uv build --wheel --sdist" in build
    assert "twine==7.0.0 twine check --strict dist/*" in build
    assert "afriend-${version}-py3-none-any.whl" in build
    assert "afriend-${version}.tar.gz" in build
    assert "-name '*.whl'" in build
    assert "-name '*.tar.gz'" in build
    assert ' --version)" = "afriend ${version}"' in build
    assert "/venv/bin/afriend" in build


def test_release_build_smokes_every_supported_python_before_publish():
    """A tag must not publish a wheel that fails on a declared Python version."""
    build = job_block("build")

    assert "Verify installed wheel on supported Python versions" in build
    assert "for python_version in 3.11 3.12 3.13; do" in build
    assert 'uv python install "${python_version}"' in build
    assert 'uv venv --python "${python_version}"' in build


def test_release_workflow_keeps_publish_identity_out_of_build_job():
    text = workflow_text()
    build = job_block("build")
    publish = job_block("publish")
    release = job_block("github-release")

    assert "permissions:\n  contents: read" in text
    assert text.count("id-token: write") == 1
    assert "id-token: write" not in build
    assert "id-token: write" not in release
    assert "needs: build" in publish
    assert "name: pypi" in publish
    assert "id-token: write" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "password:" not in publish
    assert "skip-existing:" not in publish


def test_github_release_uses_published_artifact_and_changelog():
    text = workflow_text()
    release = job_block("github-release")

    assert text.count("name: python-package-distributions") == 3
    assert "needs: publish" in release
    assert "contents: write" in release
    assert "CHANGELOG.md" in release
    assert "gh release create" in release
    assert "dist/*" in release


def test_every_action_is_pinned_to_a_full_commit_sha():
    references = re.findall(r"^\s*uses:\s+[^@\s]+@([^\s#]+)", workflow_text(), re.MULTILINE)

    assert references
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references)


def test_wheel_asset_verifier_ignores_stale_intermediate_assets():
    """A deleted asset in setuptools' build/lib must not leak into a wheel."""
    stale_asset = ROOT / "build/lib/afriend/assets/SKILL.md"
    stale_asset.parent.mkdir(parents=True, exist_ok=True)
    stale_asset.write_text("stale legacy asset\n", encoding="utf-8")

    completed = subprocess.run(
        ["bash", "ci/verify_wheel_assets.sh"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not stale_asset.exists()
