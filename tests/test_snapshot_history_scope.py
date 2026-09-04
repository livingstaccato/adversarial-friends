"""Repository-scope-aware snapshot history validation."""

from pathlib import Path

from adversarial_friends.snapshots import SnapshotIdentity, history_from_meta


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
