"""Contracts for immutable repository/artifact identity across resume."""

import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adversarial_friends import isolation
from adversarial_friends.commands.environment import freeze_revision
from adversarial_friends.errors import UsageError
from adversarial_friends.runstore import RunStore
from adversarial_friends.snapshots import (
    SnapshotIdentity,
    history_from_meta,
    record_snapshot,
    resume_frozen_artifact,
    select_snapshot,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def halted_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# frozen contract\n", encoding="utf-8")
    store = RunStore(tmp_path / "runs", "run-halted")
    frozen, digest = store.artifact_copy(artifact)
    commit = isolation.snapshot_commit(repo)
    meta = {
        "repo_root": str(repo),
        "snapshot_sha": commit,
        "artifact_path": str(artifact),
        "artifact_hash": digest,
    }
    run_json = store.run_dir / "run.json"
    store.write_run_json(meta)
    return SimpleNamespace(
        repo=repo,
        artifact=artifact,
        frozen=frozen,
        meta=meta,
        run_json=run_json,
        store=store,
    )


@pytest.fixture
def v020_meta(halted_run):
    return dict(halted_run.meta)


def test_resume_uses_recorded_snapshot_without_creating_another(monkeypatch, halted_run):
    monkeypatch.setattr(
        isolation, "snapshot_commit", Mock(side_effect=AssertionError("new snapshot"))
    )
    identity = SnapshotIdentity.from_meta(halted_run.meta)
    assert identity.verify(halted_run.frozen).commit == halted_run.meta["snapshot_sha"]


def test_resume_selection_never_creates_a_replacement_snapshot(monkeypatch, halted_run):
    monkeypatch.setattr(
        isolation, "snapshot_commit", Mock(side_effect=AssertionError("new snapshot"))
    )
    selected = select_snapshot(
        halted_run.repo,
        halted_run.frozen,
        halted_run.meta["artifact_hash"],
        halted_run.meta,
    )
    assert selected.commit == halted_run.meta["snapshot_sha"]


def test_missing_saved_commit_refuses_resume_without_rewriting_run_json(halted_run):
    before = halted_run.run_json.read_bytes()
    with pytest.raises(UsageError, match=r"saved snapshot.*missing"):
        SnapshotIdentity.from_meta({**halted_run.meta, "snapshot_sha": "0" * 40}).verify(
            halted_run.frozen
        )
    assert halted_run.run_json.read_bytes() == before


@pytest.mark.parametrize(
    "layout, message",
    [
        ("missing", r"frozen artifact.*directory.*unavailable"),
        ("empty", r"frozen artifact.*exactly one"),
        ("multiple", r"frozen artifact.*exactly one"),
        ("directory", r"frozen artifact.*regular file"),
        ("symlink", r"frozen artifact.*regular file"),
    ],
)
def test_resume_frozen_artifact_layout_is_strict_and_actionable(tmp_path, layout, message):
    run_dir = tmp_path / "run"
    artifact_dir = run_dir / "artifact"
    if layout != "missing":
        artifact_dir.mkdir(parents=True)
    if layout == "multiple":
        (artifact_dir / "one.md").write_text("one")
        (artifact_dir / "two.md").write_text("two")
    elif layout == "directory":
        (artifact_dir / "spec.md").mkdir()
    elif layout == "symlink":
        target = tmp_path / "target.md"
        target.write_text("target")
        (artifact_dir / "spec.md").symlink_to(target)

    with pytest.raises(UsageError, match=message):
        resume_frozen_artifact(run_dir)


def test_resume_frozen_artifact_selects_the_only_regular_file(tmp_path):
    artifact_dir = tmp_path / "run" / "artifact"
    artifact_dir.mkdir(parents=True)
    expected = artifact_dir / "spec.md"
    expected.write_text("# frozen\n")
    assert resume_frozen_artifact(tmp_path / "run") == expected


def test_v020_snapshot_fields_migrate_to_identity(v020_meta):
    identity = SnapshotIdentity.from_meta(v020_meta)
    assert identity.commit == v020_meta["snapshot_sha"]
    assert identity.tree is None
    assert identity.artifact_path == v020_meta["artifact_path"]


@pytest.mark.parametrize("meta", [None, [], "metadata", 7, True, {1: "not a string key"}])
def test_outer_snapshot_metadata_must_be_a_mapping(meta):
    with pytest.raises(UsageError, match=r"snapshot metadata.*object"):
        SnapshotIdentity.from_meta(meta)


@pytest.mark.parametrize(
    "nested", [None, [], "incomplete", {"tree": None}, {1: "not a string key"}]
)
def test_unusable_nested_snapshot_recovers_from_complete_legacy_fields(halted_run, nested):
    identity = SnapshotIdentity.from_meta({**halted_run.meta, "snapshot": nested})
    assert identity.commit == halted_run.meta["snapshot_sha"]
    assert identity.artifact_hash == halted_run.meta["artifact_hash"]


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("repo_root", []),
        ("commit", {}),
        ("tree", []),
        ("artifact_path", None),
        ("artifact_hash", 7),
        ("predecessor", ["not", "a", "reference"]),
    ],
)
@pytest.mark.parametrize("complete_shape", [False, True], ids=["partial", "complete"])
def test_invalid_nested_field_recovers_complete_valid_legacy(
    halted_run, field, invalid, complete_shape
):
    nested = (
        SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen).to_dict()
        if complete_shape
        else {}
    )
    nested[field] = invalid

    identity = SnapshotIdentity.from_meta({**halted_run.meta, "snapshot": nested})

    legacy = SnapshotIdentity.from_meta(halted_run.meta)
    assert identity == legacy


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("repo_root", []),
        ("commit", {}),
        ("tree", []),
        ("artifact_path", None),
        ("artifact_hash", 7),
        ("predecessor", ["not", "a", "reference"]),
    ],
)
def test_invalid_partial_nested_field_without_legacy_is_contextual(field, invalid):
    with pytest.raises(UsageError, match=field):
        SnapshotIdentity.from_meta({"snapshot": {field: invalid}})


def test_partial_nested_snapshot_recovers_from_legacy_artifact_name(halted_run):
    meta = {
        **halted_run.meta,
        "artifact": halted_run.meta["artifact_path"],
        "snapshot": {"repo_root": halted_run.meta["repo_root"]},
    }
    del meta["artifact_path"]
    assert SnapshotIdentity.from_meta(meta).artifact_path == meta["artifact"]


def test_valid_legacy_artifact_name_recovers_from_unusable_artifact_path(halted_run):
    meta = {
        **halted_run.meta,
        "artifact_path": [halted_run.meta["artifact_path"]],
        "artifact": halted_run.meta["artifact_path"],
        "snapshot": None,
    }
    assert SnapshotIdentity.from_meta(meta).artifact_path == meta["artifact"]


def test_partial_nested_conflict_with_legacy_is_refused(halted_run):
    with pytest.raises(UsageError, match=r"snapshot.*conflicts.*commit"):
        SnapshotIdentity.from_meta(
            {
                **halted_run.meta,
                "snapshot": {"commit": "f" * 40},
            }
        )


def test_complete_nested_conflict_with_legacy_is_refused(halted_run):
    nested = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen).to_dict()
    with pytest.raises(UsageError, match=r"snapshot.*conflicts.*commit"):
        SnapshotIdentity.from_meta(
            {
                **halted_run.meta,
                "snapshot_sha": "f" * 40,
                "snapshot": nested,
            }
        )


def test_unusable_nested_snapshot_without_complete_legacy_is_contextual():
    with pytest.raises(UsageError, match=r"saved snapshot.*object"):
        SnapshotIdentity.from_meta({"snapshot": None})


def test_unusable_nested_snapshot_preserves_malformed_legacy_field_error(halted_run):
    with pytest.raises(UsageError, match=r"commit.*40 hexadecimal"):
        SnapshotIdentity.from_meta(
            {
                **halted_run.meta,
                "snapshot_sha": "HEAD",
                "snapshot": None,
            }
        )


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


def test_create_preserves_an_unbound_repo_snapshot_for_an_artifact_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n")
    digest = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()

    identity = SnapshotIdentity.create(repo, outside, digest, source_artifact=outside)

    assert identity.repo_root == repo
    assert identity.commit is not None
    assert identity.tree is not None
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


@pytest.mark.parametrize(
    "commit",
    [
        "abc123",
        "HEAD",
        "f" * 39,
        "f" * 41,
        "--help",
        "0" * 39 + ";",
        "0" * 39 + "\n",
    ],
)
def test_invalid_commit_is_rejected_before_any_git_subprocess(monkeypatch, halted_run, commit):
    git = Mock(side_effect=AssertionError("git invoked"))
    monkeypatch.setattr("adversarial_friends.snapshots.subprocess.run", git)
    with pytest.raises(UsageError, match="40 hexadecimal"):
        SnapshotIdentity.from_meta({**halted_run.meta, "snapshot_sha": commit}).verify(
            halted_run.frozen
        )
    git.assert_not_called()


def test_artifact_hash_mismatch_fails_before_git(monkeypatch, halted_run):
    git = Mock(side_effect=AssertionError("git invoked"))
    monkeypatch.setattr("adversarial_friends.snapshots.subprocess.run", git)
    identity = SnapshotIdentity.from_meta(
        {**halted_run.meta, "artifact_hash": "sha256:" + "1" * 64}
    )
    with pytest.raises(UsageError, match=r"artifact hash.*saved snapshot"):
        identity.verify(halted_run.frozen)
    git.assert_not_called()


def test_unavailable_saved_repository_is_actionable(halted_run, tmp_path):
    identity = dataclasses.replace(
        SnapshotIdentity.from_meta(halted_run.meta),
        repo_root=tmp_path / "missing-repository",
    )
    with pytest.raises(UsageError, match=r"saved snapshot.*repository.*unavailable"):
        identity.verify(halted_run.frozen)


def test_repository_filesystem_error_is_translated(monkeypatch, halted_run):
    identity = SnapshotIdentity.from_meta(halted_run.meta)
    real_is_dir = Path.is_dir

    def hostile_is_dir(path):
        if path == identity.repo_root:
            raise OSError("hostile repository path")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", hostile_is_dir)
    with pytest.raises(UsageError, match=r"repository.*unavailable.*hostile"):
        identity.verify(halted_run.frozen)


def test_git_launch_filesystem_error_is_translated(monkeypatch, halted_run):
    identity = SnapshotIdentity.from_meta(halted_run.meta)
    monkeypatch.setattr(
        "adversarial_friends.snapshots.subprocess.run",
        Mock(side_effect=OSError("argument list too long")),
    )
    with pytest.raises(UsageError, match=r"saved snapshot.*unavailable.*argument list"):
        identity.verify(halted_run.frozen)


def test_saved_repository_must_still_be_its_recorded_root(halted_run):
    nested = halted_run.repo / "nested"
    nested.mkdir()
    identity = dataclasses.replace(SnapshotIdentity.from_meta(halted_run.meta), repo_root=nested)
    with pytest.raises(UsageError, match=r"saved snapshot.*repository root.*does not match"):
        identity.verify(halted_run.frozen)


def test_tree_mismatch_refuses_the_saved_identity(halted_run):
    identity = dataclasses.replace(SnapshotIdentity.from_meta(halted_run.meta), tree="f" * 40)
    with pytest.raises(UsageError, match="saved snapshot tree does not match"):
        identity.verify(halted_run.frozen)


@pytest.mark.parametrize(
    "snapshot, message",
    [
        (None, "snapshot must be an object"),
        ([], "snapshot must be an object"),
        ({}, "repo_root"),
        (
            {
                "repo_root": ["/tmp/repo"],
                "commit": None,
                "tree": None,
                "artifact_path": "spec.md",
                "artifact_hash": "sha256:" + "0" * 64,
                "predecessor": None,
            },
            "repo_root",
        ),
        (
            {
                "repo_root": None,
                "commit": None,
                "tree": None,
                "artifact_path": {"path": "spec.md"},
                "artifact_hash": "sha256:" + "0" * 64,
                "predecessor": None,
            },
            "artifact_path",
        ),
        (
            {
                "repo_root": None,
                "commit": None,
                "tree": None,
                "artifact_path": "spec.md",
                "artifact_hash": ["sha256:" + "0" * 64],
                "predecessor": None,
            },
            "artifact_hash",
        ),
        (
            {
                "repo_root": None,
                "commit": None,
                "tree": None,
                "artifact_path": "spec.md",
                "artifact_hash": "sha256:" + "0" * 64,
            },
            "predecessor",
        ),
    ],
)
def test_malformed_nested_snapshot_fields_are_refused(snapshot, message):
    with pytest.raises(UsageError, match=message):
        SnapshotIdentity.from_meta({"snapshot": snapshot})


def test_repo_and_commit_must_be_present_together(halted_run):
    raw = {
        "repo_root": str(halted_run.repo),
        "commit": None,
        "tree": None,
        "artifact_path": str(halted_run.artifact),
        "artifact_hash": halted_run.meta["artifact_hash"],
        "predecessor": None,
    }
    with pytest.raises(UsageError, match=r"repo_root.*commit"):
        SnapshotIdentity.from_meta({"snapshot": raw})


def test_legacy_commit_derives_tree_and_can_be_persisted_complete(halted_run):
    verified = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    assert verified.tree == _git(
        halted_run.repo, "rev-parse", f"{halted_run.meta['snapshot_sha']}^{{tree}}"
    )

    meta = dict(halted_run.meta)
    history = history_from_meta(meta, verified)
    record_snapshot(meta, verified, history)
    halted_run.store.write_run_json(meta)

    persisted = json.loads(halted_run.run_json.read_text(encoding="utf-8"))
    assert persisted["snapshot"]["tree"] == verified.tree
    assert persisted["snapshot_history"] == [persisted["snapshot"]]


def test_partially_migrated_history_receives_the_derived_current_tree(halted_run):
    legacy = SnapshotIdentity.from_meta(halted_run.meta)
    meta = {
        "snapshot": legacy.to_dict(),
        "snapshot_history": [legacy.to_dict()],
    }

    verified = SnapshotIdentity.from_meta(meta).verify(halted_run.frozen)
    history = history_from_meta(meta, verified)

    assert history == [verified]
    assert history[0].tree is not None


def _repo_successor(identity):
    return dataclasses.replace(
        identity,
        commit="f" * 40,
        tree="e" * 40,
        artifact_path="artifact/revised.md",
        artifact_hash="sha256:" + "1" * 64,
        predecessor=identity.commit,
    )


@pytest.mark.parametrize("history", [None, [], {}, "history"])
def test_present_invalid_snapshot_history_is_not_treated_as_absent(halted_run, history):
    current = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    with pytest.raises(UsageError, match=r"snapshot_history"):
        history_from_meta({"snapshot_history": history}, current)


def test_absent_snapshot_history_is_the_only_legacy_migration_case(halted_run):
    current = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    assert history_from_meta({}, current) == [current]


def test_snapshot_history_requires_predecessor_linkage_and_current_final(halted_run):
    first = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    current = _repo_successor(first)
    broken = dataclasses.replace(current, predecessor="0" * 40)

    with pytest.raises(UsageError, match=r"snapshot_history.*predecessor"):
        history_from_meta({"snapshot_history": [first.to_dict(), broken.to_dict()]}, current)
    with pytest.raises(UsageError, match=r"snapshot_history.*current.*final"):
        history_from_meta({"snapshot_history": [first.to_dict(), current.to_dict()]}, first)


@pytest.mark.parametrize("positions", [(0, 0), (0, 1, 0)])
def test_snapshot_history_rejects_adjacent_and_nonadjacent_duplicate_identities(
    halted_run, positions
):
    first = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    second = _repo_successor(first)
    identities = [first if position == 0 else second for position in positions]
    current = identities[-1]

    with pytest.raises(UsageError, match=r"snapshot_history.*duplicate"):
        history_from_meta(
            {"snapshot_history": [identity.to_dict() for identity in identities]}, current
        )


def test_non_repo_snapshot_history_uses_artifact_hash_predecessors():
    first = SnapshotIdentity(None, None, None, "artifact/spec.md", "sha256:" + "1" * 64)
    current = SnapshotIdentity(
        None,
        None,
        None,
        "artifact/revised.md",
        "sha256:" + "2" * 64,
        predecessor=first.artifact_hash,
    )
    meta = {"snapshot_history": [first.to_dict(), current.to_dict()]}

    assert history_from_meta(meta, current) == [first, current]

    broken = dataclasses.replace(current, predecessor="sha256:" + "3" * 64)
    with pytest.raises(UsageError, match=r"snapshot_history.*predecessor"):
        history_from_meta({"snapshot_history": [first.to_dict(), broken.to_dict()]}, broken)


def test_record_snapshot_rejects_duplicate_identity_tokens(halted_run):
    first = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    second = _repo_successor(first)
    meta: dict[str, object] = {}

    with pytest.raises(UsageError, match=r"snapshot_history.*duplicate"):
        record_snapshot(meta, first, [first, second, first])

    assert meta == {}


def test_snapshot_fields_and_history_have_deterministic_order(halted_run):
    identity = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    meta: dict[str, object] = {}
    record_snapshot(meta, identity, [identity])

    assert list(meta) == ["snapshot", "snapshot_history", "repo_root", "snapshot_sha"]
    assert list(meta["snapshot"]) == [
        "repo_root",
        "commit",
        "tree",
        "artifact_path",
        "artifact_hash",
        "predecessor",
        "source_path",
        "artifact_bound_to_snapshot",
    ]
    assert meta["snapshot_history"] == [identity.to_dict()]
    assert meta["repo_root"] == str(halted_run.repo)
    assert meta["snapshot_sha"] == identity.commit


def test_unchanged_loop_revision_creates_no_successor(monkeypatch, halted_run):
    identity = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    create = Mock(side_effect=AssertionError("successor created"))
    monkeypatch.setattr(SnapshotIdentity, "create", create)

    revision = freeze_revision(
        halted_run.store,
        halted_run.artifact,
        halted_run.frozen,
        halted_run.meta["artifact_hash"],
        False,
        None,
        identity,
        2,
    )

    assert revision.identity == identity
    create.assert_not_called()


def test_changed_loop_revision_creates_one_successor_pointing_to_predecessor(
    monkeypatch, halted_run
):
    identity = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    halted_run.artifact.write_text("# revised contract\n", encoding="utf-8")
    real_create = SnapshotIdentity.create
    create = Mock(wraps=real_create)
    monkeypatch.setattr(SnapshotIdentity, "create", create)

    revision = freeze_revision(
        halted_run.store,
        halted_run.artifact,
        halted_run.frozen,
        halted_run.meta["artifact_hash"],
        False,
        halted_run.meta["artifact_hash"],
        identity,
        2,
    )

    assert create.call_count == 1
    assert revision.identity.commit != identity.commit
    assert revision.identity.predecessor == identity.commit
    assert revision.identity.artifact_hash == revision.digest
    assert revision.identity.tree == _git(
        halted_run.repo, "rev-parse", f"{revision.identity.commit}^{{tree}}"
    )


def test_first_fresh_loop_revision_detects_change_since_initial_identity(monkeypatch, halted_run):
    identity = SnapshotIdentity.from_meta(halted_run.meta).verify(halted_run.frozen)
    halted_run.artifact.write_text("# changed before iteration one\n", encoding="utf-8")
    real_create = SnapshotIdentity.create
    create = Mock(wraps=real_create)
    monkeypatch.setattr(SnapshotIdentity, "create", create)

    revision = freeze_revision(
        halted_run.store,
        halted_run.artifact,
        halted_run.frozen,
        halted_run.meta["artifact_hash"],
        False,
        None,
        identity,
        1,
    )

    assert create.call_count == 1
    assert revision.identity.predecessor == identity.commit
    assert revision.identity.artifact_hash == revision.digest
    assert revision.identity.artifact_hash != identity.artifact_hash
