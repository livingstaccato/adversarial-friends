"""Repository-scope-aware snapshot history validation."""

from pathlib import Path

import pytest

from afriend.errors import UsageError
from afriend.snapshots import (
    SnapshotIdentity,
    history_from_meta,
    validate_repository_scope,
)


def test_pre_feature_unbound_repo_history_keeps_commit_linked_predecessors():
    """Pre-explicit-scope histories identify an unbound repository by commit."""
    first = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )
    current = SnapshotIdentity(
        Path("/repo"),
        "2" * 40,
        "b" * 40,
        "artifact/revised.md",
        "sha256:" + "2" * 64,
        predecessor=first.commit,
    )
    meta = {"snapshot_history": [first.to_dict(), current.to_dict()]}

    assert history_from_meta(meta, current) == [first, current]


@pytest.mark.parametrize("marker_field", ["repository_scope_audit", "downgrades"])
def test_pre_feature_history_does_not_infer_explicit_scope_from_prose(marker_field):
    first = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )
    current = SnapshotIdentity(
        Path("/repo"),
        "2" * 40,
        "b" * 40,
        "artifact/revised.md",
        "sha256:" + "2" * 64,
        predecessor=first.commit,
    )
    marker = (
        "repository scope selected explicitly; frozen artifact independently "
        "bound (not Git-blob-bound)."
    )
    meta: dict[str, object] = {
        "snapshot_history": [first.to_dict(), current.to_dict()],
        marker_field: marker if marker_field == "repository_scope_audit" else [marker],
    }

    assert history_from_meta(meta, current) == [first, current]


def test_explicit_unbound_history_allows_a_repeated_artifact_revision():
    """An explicit loop may review A, then B, then the restored A."""
    first = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )
    second = SnapshotIdentity(
        Path("/repo"),
        "1" * 40,
        "a" * 40,
        "artifact/revised.md",
        "sha256:" + "2" * 64,
        predecessor=first.artifact_hash,
    )
    current = SnapshotIdentity(
        Path("/repo"),
        "1" * 40,
        "a" * 40,
        "artifact/restored.md",
        first.artifact_hash,
        predecessor=second.artifact_hash,
    )
    meta = {
        "repository_scope_mode": "explicit",
        "snapshot_history": [first.to_dict(), second.to_dict(), current.to_dict()],
    }

    assert history_from_meta(meta, current) == [first, second, current]


def test_scope_mode_rejects_a_tampered_explicit_bound_snapshot():
    bound = SnapshotIdentity(
        Path("/repo"),
        "1" * 40,
        "a" * 40,
        "artifact/spec.md",
        "sha256:" + "1" * 64,
        source_path="artifact/spec.md",
        artifact_bound_to_snapshot=True,
    )

    with pytest.raises(UsageError, match=r"explicit.*independently frozen"):
        validate_repository_scope({"repository_scope_mode": "explicit"}, bound, [bound])


def test_scope_mode_rejects_a_tampered_automatic_unbound_snapshot_but_keeps_pre_feature():
    unbound = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )

    with pytest.raises(UsageError, match=r"automatic.*Git-blob-bound"):
        validate_repository_scope({"repository_scope_mode": "automatic"}, unbound, [unbound])
    validate_repository_scope({}, unbound, [unbound])
