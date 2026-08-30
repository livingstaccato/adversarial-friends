from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""


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
    text = workflow_text()

    assert 'tags: ["v*"]' in text
    assert "workflow_dispatch:" not in text
    assert "branches:" not in text


def test_release_workflow_rejects_mismatched_or_unmerged_tags():
    build = job_block("build")

    assert '"${GITHUB_REF_NAME}" = "v${version}"' in build
    assert 'git merge-base --is-ancestor "${GITHUB_SHA}" "origin/main"' in build
    assert "fetch-depth: 0" in build


def test_release_build_verifies_both_distributions_and_installed_cli():
    build = job_block("build")

    assert "uv build --wheel --sdist" in build
    assert "twine==7.0.0 twine check --strict dist/*" in build
    assert "adversarial_friends-${version}-py3-none-any.whl" in build
    assert "adversarial_friends-${version}.tar.gz" in build
    assert ' --version)" = "afriend ${version}"' in build
    assert "/venv/bin/afriend" in build


def test_release_workflow_keeps_publish_identity_out_of_build_job():
    text = workflow_text()
    build = job_block("build")
    publish = job_block("publish")

    assert "permissions:\n  contents: read" in text
    assert "id-token: write" not in build
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
