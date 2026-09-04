"""Repository-scope provenance in the human-readable report."""

import pytest
from report_helpers import meta

from adversarial_friends.report import render
from adversarial_friends.reviewstate import ReviewState

_COMMIT = "a" * 40
_TREE = "b" * 40
_DIGEST = "sha256:" + "c" * 64


def _snapshot(*, bound: bool) -> dict[str, object]:
    return {
        "repo_root": "/work/repo",
        "commit": _COMMIT,
        "tree": _TREE,
        "artifact_path": "/runs/artifact/plan.md",
        "artifact_hash": _DIGEST,
        "predecessor": None,
        "source_path": "docs/plan.md" if bound else None,
        "artifact_bound_to_snapshot": bound,
    }


def _render(run_meta: dict[str, object]) -> str:
    return render(ReviewState.replay([]), run_meta)


def test_report_renders_explicit_repository_scope_from_validated_metadata():
    out = _render(
        meta(
            repository_scope_mode="explicit",
            repository_scope_audit="untrusted historical prose",
            snapshot=_snapshot(bound=False),
        )
    )

    assert "## Repository snapshot" in out
    assert "Repository scope: explicit" in out
    assert "Repository root: `/work/repo`" in out
    assert f"Snapshot commit: `{_COMMIT}`" in out
    assert f"Snapshot tree: `{_TREE}`" in out
    assert f"Artifact digest: `{_DIGEST}`" in out
    assert "Artifact binding: independently frozen; not Git-blob-bound" in out
    assert "untrusted historical prose" not in out


def test_report_renders_automatic_repository_scope_as_git_blob_bound():
    out = _render(
        meta(
            repository_scope_mode="automatic",
            repository_scope_audit="Repository scope: explicit",
            snapshot=_snapshot(bound=True),
        )
    )

    assert "Repository scope: automatic" in out
    assert "Artifact binding: Git-blob-bound" in out


def test_report_avoids_repository_identity_for_doc_only_or_malformed_metadata():
    doc_only = _render(meta(repository_scope_mode="automatic"))
    malformed = _render(
        meta(
            repository_scope_mode="explicit",
            repository_scope_audit="repository scope selected explicitly",
            snapshot={"repo_root": "/invented"},
        )
    )

    assert "## Repository snapshot" not in doc_only
    assert "## Repository snapshot" not in malformed
    assert "/invented" not in malformed


@pytest.mark.parametrize("marker_field", ["repository_scope_audit", "downgrades"])
def test_report_does_not_infer_explicit_scope_from_prose(marker_field):
    marker = (
        "repository scope selected explicitly; frozen artifact independently "
        "bound (not Git-blob-bound)."
    )
    run_meta = meta(snapshot=_snapshot(bound=False))
    run_meta[marker_field] = marker if marker_field == "repository_scope_audit" else [marker]

    out = _render(run_meta)

    assert "## Repository snapshot" not in out
    assert "Repository scope: explicit" not in out
