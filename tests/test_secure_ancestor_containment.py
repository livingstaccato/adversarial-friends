"""Regression tests for run-owned paths with hostile symlink ancestors."""

import contextlib
import os
import stat
from types import SimpleNamespace

import pytest

from afriend import childenv, isolation, rounds, secureio
from afriend.ledger import Claim
from afriend.runstore import RunStore


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


@pytest.mark.parametrize("op", [secureio.secure_open_write, secureio.secure_open_append])
def test_secure_open_closes_descriptor_when_post_open_chmod_fails(tmp_path, monkeypatch, op):
    target = tmp_path / "capture"
    captured = []
    real_open = secureio.os.open

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        captured.append(descriptor)
        return descriptor

    monkeypatch.setattr(secureio.os, "open", recording_open)
    monkeypatch.setattr(
        secureio.os,
        "fchmod",
        lambda _fd, _mode: (_ for _ in ()).throw(OSError("boom")),
    )

    with pytest.raises(OSError, match="boom"):
        op(target, root=tmp_path)

    with pytest.raises(OSError):
        os.fstat(captured[-1])


def test_secure_read_closes_descriptor_when_post_open_fstat_fails(tmp_path, monkeypatch):
    target = tmp_path / "capture"
    target.write_text("payload")
    captured = []
    real_open = secureio.os.open

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        captured.append(descriptor)
        return descriptor

    real_fstat = secureio.os.fstat

    def failing_fstat(descriptor):
        if descriptor == captured[-1] and len(captured) > 1:
            raise OSError("boom")
        return real_fstat(descriptor)

    monkeypatch.setattr(secureio.os, "open", recording_open)
    monkeypatch.setattr(secureio.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="boom"):
        secureio.secure_open_read(target, root=tmp_path)
    with pytest.raises(OSError):
        real_fstat(captured[-1])


def test_secure_directory_open_closes_descriptor_when_fstat_fails(tmp_path, monkeypatch):
    captured = []
    real_open = secureio.os.open
    real_fstat = secureio.os.fstat

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        captured.append(descriptor)
        return descriptor

    monkeypatch.setattr(secureio.os, "open", recording_open)
    monkeypatch.setattr(
        secureio.os,
        "fstat",
        lambda _fd: (_ for _ in ()).throw(OSError("directory fstat boom")),
    )
    with pytest.raises(OSError, match="directory fstat boom"):
        secureio.secure_open_directory(tmp_path, root=tmp_path)
    with pytest.raises(OSError):
        real_fstat(captured[-1])


def test_prompt_prune_refuses_replaced_run_ancestor_without_unlinking_outside(tmp_path):
    store = RunStore(tmp_path / "runs", "run-prune")
    prompt = store.friend_prompt_path(1, "friend-ops-0")
    store.write_sensitive(prompt, "prompt")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_prompt = outside / "round-1" / prompt.name
    outside_prompt.parent.mkdir()
    outside_prompt.write_text("outside")
    _replace_with_symlink(store.run_dir, outside)

    with pytest.raises(OSError):
        rounds.prune_undispatched_prompts(
            [SimpleNamespace(name="friend-ops-0")],
            {"friend-ops-0": prompt},
            [],
            store,
        )

    assert outside_prompt.read_text() == "outside"
