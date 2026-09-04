"""The frozen artifact stays bound to its exact blob in the saved commit."""

import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from e2e_helpers import AF, _env, run_af
import pytest

from adversarial_friends import isolation, snapshots
from adversarial_friends.errors import UsageError
from adversarial_friends.runstore import RunStore
from adversarial_friends.snapshots import SnapshotIdentity, history_from_meta


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _identity(tmp_path, *, nested: bool = True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    source = repo / "nested" / "spec.md" if nested else repo / "spec.md"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"# committed contract\n")
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(source.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    identity = SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)
    return repo, source, frozen, identity


def test_resume_rechecks_frozen_bytes_against_the_saved_commit_blob(tmp_path):
    _repo, _source, frozen, identity = _identity(tmp_path)
    tampered = b"# attacker-controlled replacement\n"
    frozen.write_bytes(tampered)
    coordinated = dataclasses.replace(
        identity,
        artifact_hash="sha256:" + hashlib.sha256(tampered).hexdigest(),
    )

    with pytest.raises(UsageError, match=r"commit artifact.*frozen artifact"):
        coordinated.verify(frozen)


def test_unbound_repo_snapshot_resume_never_reads_a_commit_blob(monkeypatch, tmp_path):
    repo, _source, frozen, _bound_identity = _identity(tmp_path)
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    identity = SnapshotIdentity.create(repo, frozen, digest)

    def unexpected_blob_lookup(*_args):
        raise AssertionError("unbound snapshot read a commit blob")

    monkeypatch.setattr(snapshots, "_resume_commit_blob", unexpected_blob_lookup)

    assert identity.verify(frozen).artifact_bound_to_snapshot is False


def test_old_snapshot_without_binding_field_infers_the_source_binding(tmp_path):
    _repo, _source, frozen, identity = _identity(tmp_path)
    raw = identity.to_dict()
    raw.pop("artifact_bound_to_snapshot")

    restored = SnapshotIdentity._from_dict(raw)

    assert restored.artifact_bound_to_snapshot
    assert restored.verify(frozen) == identity


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"artifact_bound_to_snapshot": True, "source_path": None}, "source_path"),
        ({"artifact_bound_to_snapshot": False}, "source_path"),
        (
            {"artifact_bound_to_snapshot": True, "repo_root": None, "commit": None, "tree": None},
            "repository",
        ),
    ],
)
def test_inconsistent_saved_binding_metadata_is_refused(tmp_path, changes, message):
    repo, _source, _frozen, identity = _identity(tmp_path)
    raw = {**identity.to_dict(), **changes}

    with pytest.raises(UsageError, match=message):
        SnapshotIdentity.from_meta(
            {
                "snapshot": raw,
                "repo_root": str(repo),
                "snapshot_sha": identity.commit,
                "artifact_path": identity.artifact_path,
                "artifact_hash": identity.artifact_hash,
            }
        )


def test_legacy_binding_recovers_deleted_regular_source_from_saved_path(tmp_path):
    repo, source, frozen, identity = _identity(tmp_path, nested=False)
    legacy = {
        "repo_root": str(repo),
        "snapshot_sha": identity.commit,
        "artifact_path": str(source),
        "artifact_hash": identity.artifact_hash,
    }
    source.unlink()

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "spec.md"


def test_symlinked_source_persists_the_bound_target_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    target = repo / "docs" / "contract.md"
    target.parent.mkdir()
    target.write_bytes(b"# target bytes\n")
    source = repo / "spec.md"
    source.symlink_to(target)
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(target.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()

    identity = SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)

    assert identity.source_path == "docs/contract.md"
    source.unlink()
    assert SnapshotIdentity._from_dict(identity.to_dict()).verify(frozen) == identity


def _legacy_symlink_identity(tmp_path, target: str = "docs/a.md"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "a.md").write_bytes(b"# saved target A\n")
    (docs / "b.md").write_bytes(b"# alternate target B\n")
    source = repo / "spec.md"
    source.symlink_to(target)
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes((docs / "a.md").read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    commit = isolation.snapshot_commit(repo)
    legacy = {
        "repo_root": str(repo),
        "snapshot_sha": commit,
        "artifact_path": str(source),
        "artifact_hash": digest,
    }
    return repo, source, frozen, legacy


def _legacy_component_symlink_identity(
    tmp_path: Path,
    links: dict[str, str],
    invocation_path: str,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "a.md").write_bytes(b"# saved target A\n")
    (docs / "b.md").write_bytes(b"# alternate target B\n")
    for path, target in links.items():
        link = repo / path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes((docs / "a.md").read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    commit = isolation.snapshot_commit(repo)
    legacy = {
        "repo_root": str(repo),
        "snapshot_sha": commit,
        "artifact_path": str(repo / invocation_path),
        "artifact_hash": digest,
    }
    return repo, frozen, legacy


def test_legacy_symlink_binding_is_recovered_from_saved_commit_not_live_target(tmp_path):
    repo, source, frozen, legacy = _legacy_symlink_identity(tmp_path)
    source.unlink()
    source.symlink_to(repo / "docs" / "b.md")
    replacement = (repo / "docs" / "b.md").read_bytes()
    frozen.write_bytes(replacement)
    legacy["artifact_hash"] = "sha256:" + hashlib.sha256(replacement).hexdigest()

    with pytest.raises(UsageError, match=r"commit artifact does not match"):
        SnapshotIdentity.from_meta(legacy).verify(frozen)


def test_legacy_symlink_binding_ignores_live_retarget_even_when_bytes_match(tmp_path):
    repo, source, frozen, legacy = _legacy_symlink_identity(tmp_path)
    (repo / "docs" / "b.md").write_bytes((repo / "docs" / "a.md").read_bytes())
    source.unlink()
    source.symlink_to(repo / "docs" / "b.md")

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "docs/a.md"


@pytest.mark.parametrize("live_state", ["missing-link", "missing-target"])
def test_legacy_symlink_binding_does_not_require_live_link_or_target(tmp_path, live_state):
    repo, source, frozen, legacy = _legacy_symlink_identity(tmp_path)
    source.unlink()
    if live_state == "missing-target":
        (repo / "docs" / "a.md").unlink()

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "docs/a.md"


@pytest.mark.parametrize(
    "target, expected",
    [
        ("docs/a.md", "docs/a.md"),
        ("docs/../docs/a.md", "docs/a.md"),
    ],
)
def test_legacy_relative_symlink_targets_follow_saved_git_semantics(tmp_path, target, expected):
    _repo, _source, frozen, legacy = _legacy_symlink_identity(tmp_path, target)

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == expected


@pytest.mark.parametrize("suffix", [("docs", "a.md"), ("docs", "..", "docs", "a.md")])
def test_legacy_absolute_in_repo_symlink_target_is_recovered_from_saved_git(tmp_path, suffix):
    repo = tmp_path / "repo"
    target = str(repo.joinpath(*suffix))
    _repo, _source, frozen, legacy = _legacy_symlink_identity(tmp_path, target)

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "docs/a.md"


@pytest.mark.parametrize(
    ("links", "invocation_path"),
    [
        ({"spec.md": "docs/a.md"}, "spec.md"),
        ({"--spec.md": "docs/a.md"}, "--spec.md"),
        ({":(glob)*": "docs/a.md"}, ":(glob)*"),
        ({"linkdir": "docs"}, "linkdir/a.md"),
        ({"outer": "middle", "middle": "docs"}, "outer/a.md"),
        ({"links/linkdir": "../docs"}, "links/linkdir/a.md"),
        ({"links/linkdir": "../docs/../docs"}, "links/linkdir/a.md"),
    ],
)
def test_legacy_binding_walks_final_intermediate_and_chained_saved_symlinks(
    tmp_path, links, invocation_path
):
    repo, frozen, legacy = _legacy_component_symlink_identity(tmp_path, links, invocation_path)
    # Recovery must use only the immutable commit. The corresponding live
    # paths may disappear entirely after the run was recorded.
    for path in sorted(links, key=lambda value: value.count("/"), reverse=True):
        (repo / path).unlink()

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "docs/a.md"


def test_legacy_binding_walks_an_absolute_intermediate_target_inside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    links = {"linkdir": str(repo / "docs" / ".." / "docs")}
    _repo, frozen, legacy = _legacy_component_symlink_identity(tmp_path, links, "linkdir/a.md")

    verified = SnapshotIdentity.from_meta(legacy).verify(frozen)

    assert verified.source_path == "docs/a.md"


def test_legacy_history_entry_recovers_an_intermediate_symlink_binding(tmp_path):
    _repo, frozen, legacy = _legacy_component_symlink_identity(
        tmp_path, {"linkdir": "docs"}, "linkdir/a.md"
    )
    current = SnapshotIdentity.from_meta(legacy).verify(frozen)
    raw_history = dataclasses.replace(current, source_path=None).to_dict()
    raw_history.pop("artifact_bound_to_snapshot")

    history = history_from_meta({**legacy, "snapshot_history": [raw_history]}, current)

    assert [entry.source_path for entry in history] == ["docs/a.md"]


@pytest.mark.parametrize(
    ("links", "invocation_path", "error"),
    [
        ({"linkdir": "../outside"}, "linkdir/a.md", "outside"),
        ({"linkdir": "/etc"}, "linkdir/passwd", "outside"),
        ({"linkdir": "missing"}, "linkdir/a.md", "unavailable"),
        ({"linkdir": "docs/a.md"}, "linkdir/child.md", "not a directory"),
        ({"a": "b", "b": "a"}, "a/spec.md", "cycle"),
    ],
)
def test_legacy_component_walk_fails_closed_for_unsafe_or_invalid_trees(
    tmp_path, links, invocation_path, error
):
    _repo, frozen, legacy = _legacy_component_symlink_identity(tmp_path, links, invocation_path)

    with pytest.raises(UsageError, match=rf"saved.*{error}"):
        SnapshotIdentity.from_meta(legacy).verify(frozen)


def test_legacy_component_walk_enforces_the_symlink_depth_cap(tmp_path):
    links = {f"link-{index}": f"link-{index + 1}" for index in range(65)}
    links["link-65"] = "docs"
    _repo, frozen, legacy = _legacy_component_symlink_identity(tmp_path, links, "link-0/a.md")

    with pytest.raises(UsageError, match=r"saved.*symlink depth limit"):
        SnapshotIdentity.from_meta(legacy).verify(frozen)


def test_tree_binding_is_checked_before_legacy_component_diagnostics(tmp_path):
    _repo, frozen, legacy = _legacy_component_symlink_identity(
        tmp_path, {"linkdir": "missing"}, "linkdir/a.md"
    )
    recovered = SnapshotIdentity.from_meta(legacy)
    hostile = dataclasses.replace(recovered, tree="f" * 40)

    with pytest.raises(UsageError, match=r"tree does not match commit"):
        hostile.verify(frozen)


def test_legacy_history_entry_recovers_symlink_binding_from_its_saved_commit(tmp_path):
    _repo, source, frozen, legacy = _legacy_symlink_identity(tmp_path)
    current = SnapshotIdentity.from_meta(legacy).verify(frozen)
    source.unlink()
    raw_history = dataclasses.replace(current, source_path=None).to_dict()
    raw_history.pop("artifact_bound_to_snapshot")

    history = history_from_meta({**legacy, "snapshot_history": [raw_history]}, current)

    assert [entry.source_path for entry in history] == ["docs/a.md"]


@pytest.mark.parametrize("target", ["../outside.md", "/etc/passwd", "docs/missing.md"])
def test_legacy_unsafe_or_missing_saved_symlink_target_fails_closed(tmp_path, target):
    _repo, _source, frozen, legacy = _legacy_symlink_identity(tmp_path, target)

    with pytest.raises(UsageError, match=r"saved.*(?:symlink|artifact).*(?:outside|unavailable)"):
        SnapshotIdentity.from_meta(legacy).verify(frozen)


@pytest.mark.parametrize("artifact_path", ["../spec.md", "nested/../spec.md", "spec.md\0x"])
def test_legacy_hostile_invocation_path_fails_before_git_lookup(tmp_path, artifact_path):
    _repo, _source, frozen, legacy = _legacy_symlink_identity(tmp_path)
    legacy["artifact_path"] = artifact_path

    with pytest.raises(UsageError, match=r"source.*path"):
        SnapshotIdentity.from_meta(legacy).verify(frozen)


@pytest.mark.parametrize(
    "source_path",
    ["/etc/passwd", "../spec.md", ".", "nested/../spec.md", "nested//spec.md", "\0spec.md"],
)
def test_hostile_saved_source_binding_is_refused(source_path, tmp_path):
    _repo, _source, _frozen, identity = _identity(tmp_path)
    raw = identity.to_dict()
    raw["source_path"] = source_path

    with pytest.raises(UsageError, match=r"source_path.*repository-relative"):
        SnapshotIdentity.from_meta({"snapshot": raw})


def test_intermediate_legacy_binding_tamper_does_not_rewrite_resume_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    docs = repo / "docs"
    docs.mkdir()
    artifact = docs / "spec.md"
    artifact.write_text("# repository contract\n")
    linkdir = repo / "linkdir"
    linkdir.symlink_to("docs")
    invoked = linkdir / "spec.md"
    halted = run_af(
        tmp_path,
        invoked,
        "--friend",
        "fake:judge_uphold_a",
        "--friend",
        "fake:judge_uphold_b",
        "--merge",
        "orchestrator",
    )
    assert halted.returncode == 10, halted.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    request = run_dir / "round-1" / "REQUEST.json"
    response = json.loads(request.read_text())
    response["merges"] = []
    response_path = request.parent / "RESPONSE.json"
    response_path.write_text(json.dumps(response))
    frozen = next((run_dir / "artifact").iterdir())
    tampered = b"# coordinated replacement\n"
    frozen.write_bytes(tampered)
    digest = "sha256:" + hashlib.sha256(tampered).hexdigest()
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["artifact_hash"] = digest
    meta["artifact_path"] = str(invoked)
    meta["snapshot"]["artifact_hash"] = digest
    meta["snapshot"]["source_path"] = None
    meta["snapshot_history"][-1]["artifact_hash"] = digest
    meta["snapshot_history"][-1]["source_path"] = None
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True))
    before = run_json.read_bytes()
    before_response = response_path.read_bytes()
    report = run_dir / "report.md"
    before_report = report.read_bytes()

    resumed = subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            "--resume",
            run_dir.name,
            "--out",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )

    assert resumed.returncode == 2, resumed.stderr
    assert "artifact_bound_to_snapshot requires source_path" in resumed.stderr
    assert run_json.read_bytes() == before
    assert response_path.read_bytes() == before_response
    assert report.read_bytes() == before_report


def test_legacy_binding_migration_is_not_persisted_before_later_validation(tmp_path):
    repo, source, frozen, identity = _identity(tmp_path, nested=False)
    store = RunStore(tmp_path / "runs", "run-halted")
    copied, _digest = store.artifact_copy(source)
    meta = {
        "repo_root": str(repo),
        "snapshot_sha": identity.commit,
        "artifact_path": str(source),
        "artifact_hash": identity.artifact_hash,
    }
    store.write_run_json(meta)
    before = (store.run_dir / "run.json").read_bytes()

    assert copied.read_bytes() == frozen.read_bytes()
    assert SnapshotIdentity.from_meta(meta).verify(copied).source_path == "spec.md"
    assert (store.run_dir / "run.json").read_bytes() == before
