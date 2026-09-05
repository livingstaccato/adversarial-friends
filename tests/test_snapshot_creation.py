"""Contracts for creating immutable repository/artifact snapshots."""

import hashlib
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from afriend import isolation
from afriend.errors import UsageError
from afriend.snapshots import SnapshotIdentity


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_create_rejects_digest_that_does_not_match_artifact(monkeypatch, tmp_path):
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"exact\x00bytes")
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    with pytest.raises(UsageError, match=r"artifact hash.*does not match"):
        SnapshotIdentity.create(tmp_path, artifact, "sha256:" + "0" * 64)

    create_commit.assert_not_called()


def test_create_hashes_exact_artifact_bytes(tmp_path):
    artifact = tmp_path / "artifact.bin"
    payload = b"\x00\xff\r\nexact bytes"
    artifact.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    identity = SnapshotIdentity.create(None, artifact, digest)

    assert identity.artifact_hash == digest


def test_repo_snapshot_without_source_artifact_is_unbound_and_roundtrips(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(b"# independently frozen\n")
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()

    identity = SnapshotIdentity.create(repo, frozen, digest)

    assert identity.repo_root == repo
    assert identity.commit is not None
    assert identity.tree is not None
    assert identity.source_path is None
    assert not identity.artifact_bound_to_snapshot
    assert SnapshotIdentity._from_dict(identity.to_dict()) == identity


def test_create_binds_the_snapshot_commit_blob_to_the_frozen_bytes(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    source = repo / "spec.md"
    source.write_bytes(b"# frozen bytes\n")
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(source.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    real_snapshot = isolation.snapshot_commit
    captured: dict[str, str] = {}

    def mutate_before_snapshot(root):
        source.write_bytes(b"# raced bytes\n")
        captured["commit"] = real_snapshot(root)
        return captured["commit"]

    monkeypatch.setattr(isolation, "snapshot_commit", mutate_before_snapshot)

    with pytest.raises(UsageError, match=r"snapshot.*artifact.*does not match.*frozen"):
        SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)

    committed = subprocess.run(
        ["git", "show", f"{captured['commit']}:spec.md"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert committed == b"# raced bytes\n"
    assert frozen.read_bytes() == b"# frozen bytes\n"


def test_create_proves_the_captured_commit_blob_matches_frozen_bytes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    source = repo / "nested" / "spec.md"
    source.parent.mkdir()
    source.write_bytes(b"\x00exact repository artifact\n")
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(source.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()

    identity = SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)

    committed = subprocess.run(
        ["git", "show", f"{identity.commit}:nested/spec.md"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert committed == frozen.read_bytes()
    assert identity.source_path == "nested/spec.md" and identity.artifact_bound_to_snapshot
    assert identity.to_dict()["source_path"] == "nested/spec.md"


def test_explicit_repo_snapshot_remains_unbound_for_an_artifact_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n")
    digest = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()

    identity = SnapshotIdentity.create(repo, outside, digest)

    assert identity.repo_root == repo
    assert identity.commit is not None
    assert identity.tree is not None
    assert identity.source_path is None
    assert not identity.artifact_bound_to_snapshot


def test_create_skips_snapshot_if_automatic_source_now_resolves_outside_repo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"# retargeted outside\n")
    source = repo / "spec.md"
    source.symlink_to(outside)
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(outside.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    identity = SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)

    create_commit.assert_not_called()
    assert identity.repo_root is None
    assert identity.commit is None
    assert identity.tree is None
    assert identity.source_path is None
    assert not identity.artifact_bound_to_snapshot


@pytest.mark.parametrize("repo_root", ["repo", [], {}, 7, True])
def test_create_rejects_non_path_repository_before_read_or_snapshot(
    monkeypatch, tmp_path, repo_root
):
    artifact = tmp_path / "missing-artifact"
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    with pytest.raises(UsageError, match=r"repo_root.*Path"):
        SnapshotIdentity.create(repo_root, artifact, "sha256:" + "0" * 64)

    create_commit.assert_not_called()


@pytest.mark.parametrize("artifact", [None, "artifact", [], {}, 7, True])
def test_create_rejects_non_path_artifact_before_snapshot(monkeypatch, tmp_path, artifact):
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    with pytest.raises(UsageError, match=r"artifact.*Path"):
        SnapshotIdentity.create(tmp_path, artifact, "sha256:" + "0" * 64)

    create_commit.assert_not_called()


@pytest.mark.parametrize("digest", [None, [], {}, 7, True])
def test_create_rejects_non_string_digest_before_read_or_snapshot(monkeypatch, tmp_path, digest):
    artifact = tmp_path / "missing-artifact"
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    with pytest.raises(UsageError, match=r"artifact hash.*string"):
        SnapshotIdentity.create(tmp_path, artifact, digest)

    create_commit.assert_not_called()


@pytest.mark.parametrize(
    "predecessor",
    [[], {}, 7, True, "HEAD", "f" * 39, "sha256:" + "f" * 63],
)
def test_create_rejects_invalid_predecessor_before_read_or_snapshot(
    monkeypatch, tmp_path, predecessor
):
    artifact = tmp_path / "missing-artifact"
    create_commit = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(isolation, "snapshot_commit", create_commit)

    with pytest.raises(UsageError, match=r"predecessor.*commit or artifact hash"):
        SnapshotIdentity.create(
            tmp_path,
            artifact,
            "sha256:" + "0" * 64,
            predecessor=predecessor,
        )

    create_commit.assert_not_called()
