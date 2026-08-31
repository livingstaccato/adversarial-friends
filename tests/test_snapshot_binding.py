"""The frozen artifact stays bound to its exact blob in the saved commit."""

import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from e2e_helpers import AF, _env, run_af
import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.runstore import RunStore
from adversarial_friends.snapshots import SnapshotIdentity


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


def test_coordinated_frozen_and_metadata_tamper_does_not_rewrite_run_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    artifact = repo / "spec.md"
    artifact.write_text("# repository contract\n")
    halted = run_af(
        tmp_path,
        artifact,
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
    (request.parent / "RESPONSE.json").write_text(json.dumps(response))
    frozen = next((run_dir / "artifact").iterdir())
    tampered = b"# coordinated replacement\n"
    frozen.write_bytes(tampered)
    digest = "sha256:" + hashlib.sha256(tampered).hexdigest()
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["artifact_hash"] = digest
    meta["snapshot"]["artifact_hash"] = digest
    meta["snapshot_history"][-1]["artifact_hash"] = digest
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True))
    before = run_json.read_bytes()

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
    assert "commit artifact does not match" in resumed.stderr
    assert run_json.read_bytes() == before


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
