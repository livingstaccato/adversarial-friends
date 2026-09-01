"""Regression tests for run-owned paths with hostile symlink ancestors."""

import contextlib
import stat

import pytest

from adversarial_friends import childenv, isolation, rounds
from adversarial_friends.ledger import Claim
from adversarial_friends.runstore import RunStore


def _replace_with_symlink(path, target) -> None:
    path.rename(path.with_name(f"{path.name}.original"))
    path.symlink_to(target, target_is_directory=True)


def test_round_creation_refuses_a_run_directory_replaced_by_a_symlink(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(store.run_dir, outside)

    with pytest.raises(OSError):
        store.round_dir(2)

    assert not (outside / "round-2").exists()


def test_checkpoint_write_refuses_a_run_directory_replaced_by_a_symlink(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(store.run_dir, outside)

    with pytest.raises(OSError):
        store.write_run_json({"state": "must stay contained"})

    assert not (outside / "run.json").exists()
    assert not (outside / ".run.json.tmp").exists()


def test_ledger_append_refuses_a_run_directory_replaced_by_a_symlink(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(store.run_dir, outside)
    claim = Claim(
        id="c-0001@1",
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="major",
        claim="must stay contained",
        location=None,
        evidence="reproducer",
        failure_scenario="outside write",
        suggested_fix="use openat",
    )

    with pytest.raises(OSError):
        store.ledger.append(claim)

    assert not (outside / "claims.jsonl").exists()


def test_artifact_copy_refuses_a_run_directory_replaced_by_a_symlink(tmp_path):
    source = tmp_path / "artifact.md"
    source.write_text("private review input\n")
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(store.run_dir, outside)

    with pytest.raises(OSError):
        store.artifact_copy(source)

    assert not (outside / "artifact").exists()


def test_kept_isolation_refuses_a_symlinked_isolation_ancestor(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.run_dir / "isolation").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError), rounds._isolation_root(store, 3, keep=True):
        pass

    assert not (outside / "round-3").exists()


def test_scratch_creation_does_not_follow_a_symlinked_private_root(tmp_path):
    isolation_root = tmp_path / "isolation"
    isolation_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("tmp", "state"):
        child = outside / name
        child.mkdir()
        child.chmod(0o755)
    private_root = isolation_root / "friend.private"
    private_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        childenv.private_dirs(private_root)

    assert stat.S_IMODE((outside / "tmp").stat().st_mode) == 0o755
    assert stat.S_IMODE((outside / "state").stat().st_mode) == 0o755


def test_doc_scope_creation_does_not_follow_an_ancestor_symlink(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("private review input\n")
    isolation_root = tmp_path / "isolation"
    isolation_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (isolation_root / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        isolation.doc_scope_dir(isolation_root / "redirect" / "friend", artifact)

    assert not (outside / "friend").exists()


def test_permission_repair_skips_a_contained_symlink_without_chmodding_outside(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    secret = outside / "secret"
    secret.write_text("do not touch\n")
    secret.chmod(0o644)
    (store.run_dir / "hostile").symlink_to(outside, target_is_directory=True)

    store.repair_permissions()

    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644
    assert secret.read_text() == "do not touch\n"


def test_resume_with_a_symlinked_round_refuses_writes_without_touching_target(tmp_path):
    store = RunStore(tmp_path / "runs", "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.run_dir / "round-1").symlink_to(outside, target_is_directory=True)
    resumed = RunStore(store.root, store.run_id, resume=True)

    with pytest.raises(OSError):
        resumed.round_dir(1)

    assert list(outside.iterdir()) == []
    with contextlib.suppress(OSError):
        resumed._lock_handle and resumed._lock_handle.close()
