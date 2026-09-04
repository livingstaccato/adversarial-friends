"""Repository-scope-aware snapshot history validation."""

from pathlib import Path

import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.snapshots import (
    SnapshotIdentity,
    history_from_meta,
    validate_repository_scope,
)


def test_legacy_unbound_repo_history_keeps_commit_linked_predecessors():
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


@pytest.mark.parametrize(
    "interim_meta",
    [
        {
            "repository_scope_audit": (
                "repository scope selected explicitly; frozen artifact independently "
                "bound (not Git-blob-bound)."
            )
        },
        {
            "downgrades": [
                "repository scope selected explicitly; frozen artifact independently "
                "bound (not Git-blob-bound)."
            ]
        },
    ],
)
def test_interim_explicit_unbound_history_uses_artifact_hash_predecessors(interim_meta):
    first = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )
    current = SnapshotIdentity(
        Path("/repo"),
        "1" * 40,
        "a" * 40,
        "artifact/revised.md",
        "sha256:" + "2" * 64,
        predecessor=first.artifact_hash,
    )
    meta = {**interim_meta, "snapshot_history": [first.to_dict(), current.to_dict()]}

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


def test_scope_mode_rejects_a_tampered_automatic_unbound_snapshot_but_keeps_legacy():
    unbound = SnapshotIdentity(
        Path("/repo"), "1" * 40, "a" * 40, "artifact/spec.md", "sha256:" + "1" * 64
    )

    with pytest.raises(UsageError, match=r"automatic.*Git-blob-bound"):
        validate_repository_scope({"repository_scope_mode": "automatic"}, unbound, [unbound])
    validate_repository_scope({}, unbound, [unbound])
