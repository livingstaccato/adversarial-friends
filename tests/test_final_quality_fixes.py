"""Regression tests for the final crash-boundary quality review."""

import argparse
import hashlib
import json
from pathlib import Path
import threading

import pytest

from afriend import orchestrator
from afriend.adapters import FriendSpec
from afriend.ceilings import Budget
from afriend.commands import crossexam as crossexam_mod, resume as resume_mod
from afriend.commands.critique import build_prompts
from afriend.errors import UsageError
from afriend.ledger import Claim
from afriend.report import _escape_block, _escape_cell
from afriend.reviewstate import ReviewState
from afriend.runstore import RunStore


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        mode="report",
        max_rounds=1,
        attributed=False,
        allow_unsandboxed_friend=False,
        _resume_meta={},
    )


def _resume(store: RunStore, round_no: int = 1):
    return resume_mod.resume_round_one(
        _args(),
        store,
        ReviewState.replay(store.ledger.records()),
        [],
        {},
        None,
        Path("artifact.md"),
        "artifact",
        None,
        None,
        threading.Event(),
        Budget(max_calls=10, started=0.0),
        round_no,
        lambda _pool: None,
    )


def _merge_response() -> bytes:
    return b'{"version": 1, "merges": []}'


def test_identical_live_and_applying_without_checkpoint_recovers(tmp_path):
    store = RunStore(tmp_path, "pre-checkpoint")
    round_dir = store.round_dir(1)
    orchestrator.write_request(round_dir, store.run_id, 1, [])
    payload = _merge_response()
    store.create_owned_bytes(round_dir / "RESPONSE.json", payload)
    store.create_owned_bytes(round_dir / "RESPONSE.json.applying", payload)

    _resume(store)

    assert (round_dir / "RESPONSE.json.applied").read_bytes() == payload
    assert not (round_dir / "RESPONSE.json").exists()
    assert not (round_dir / "RESPONSE.json.applying").exists()
    checkpoint = json.loads((store.run_dir / "run.json").read_text())
    assert checkpoint["applied_response"]["sha256"] == (
        "sha256:" + hashlib.sha256(payload).hexdigest()
    )


def test_applying_swap_is_never_promoted_to_applied(tmp_path, monkeypatch):
    store = RunStore(tmp_path, "applying-swap")
    round_dir = store.round_dir(1)
    orchestrator.write_request(round_dir, store.run_id, 1, [])
    payload = _merge_response()
    live = round_dir / "RESPONSE.json"
    applying = round_dir / "RESPONSE.json.applying"
    live.write_bytes(payload)
    original_checkpoint = resume_mod._checkpoint_response_preparation

    def swap_then_checkpoint(*args, **kwargs):
        applying.write_bytes(b"attacker-controlled")
        return original_checkpoint(*args, **kwargs)

    monkeypatch.setattr(resume_mod, "_checkpoint_response_preparation", swap_then_checkpoint)

    _resume(store)

    assert (round_dir / "RESPONSE.json.applied").read_bytes() == payload
    assert not applying.exists()


def test_invalid_response_does_not_repair_existing_round_permissions(tmp_path):
    store = RunStore(tmp_path, "invalid-zero-mutation")
    round_dir = store.round_dir(1)
    orchestrator.write_request(round_dir, store.run_id, 1, [])
    response = round_dir / "RESPONSE.json"
    response.write_bytes(b'{"version": 1, "merges": "not-a-list"}')
    round_dir.chmod(0o755)
    response.chmod(0o644)
    before = (round_dir.stat().st_mode, response.stat().st_mode, response.stat().st_mtime_ns)

    with pytest.raises(UsageError):
        _resume(store)

    after = (round_dir.stat().st_mode, response.stat().st_mode, response.stat().st_mtime_ns)
    assert after == before
    assert not list(round_dir.glob("RESPONSE.json.*"))
    assert not (store.run_dir / "run.json").exists()


def _spec(name: str, lens: str) -> FriendSpec:
    return FriendSpec(name, "fake", lens, None, None, "doc", 30)


def test_critique_prompt_batch_cleans_first_file_when_second_write_fails(tmp_path, monkeypatch):
    store = RunStore(tmp_path, "critique-prompt-failure")
    specs = [_spec("first-ops-0", "ops"), _spec("second-security-0", "security")]
    original = store.write_sensitive
    writes = 0

    def fail_second(path: Path, text: str):
        nonlocal writes
        if path.suffix == ".prompt":
            writes += 1
            if writes == 2:
                raise OSError("second prompt failed")
        return original(path, text)

    monkeypatch.setattr(store, "write_sensitive", fail_second)
    with pytest.raises(OSError, match="second prompt failed"):
        build_prompts(specs, "artifact", store, {}, 1)
    assert not list(store.run_dir.rglob("*.prompt"))


def test_judge_prompt_batch_cleans_first_file_when_second_write_fails(tmp_path, monkeypatch):
    store = RunStore(tmp_path, "judge-prompt-failure")
    claim = Claim(
        "c-0001@1",
        None,
        ["third/ops"],
        "ops",
        1,
        False,
        "high",
        "missing guard",
        None,
        "evidence",
        "failure",
        "fix",
    )
    store.ledger.append(claim)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact")
    specs = [_spec("first-ops-0", "ops"), _spec("second-security-0", "security")]
    original = store.write_sensitive
    writes = 0

    def fail_second(path: Path, text: str):
        nonlocal writes
        if path.suffix == ".prompt":
            writes += 1
            if writes == 2:
                raise OSError("second prompt failed")
        return original(path, text)

    monkeypatch.setattr(store, "write_sensitive", fail_second)
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not start"),
    )
    with pytest.raises(OSError, match="second prompt failed"):
        crossexam_mod.run_rounds(
            specs,
            [claim],
            store,
            ReviewState.replay(store.ledger.records()),
            {},
            None,
            tmp_path / "schema.json",
            artifact,
            "artifact",
            None,
            None,
            threading.Event(),
            Budget(max_calls=10, started=0.0),
            2,
            now=lambda: 0.0,
        )
    assert not list(store.run_dir.rglob("*.prompt"))
    assert not list(store.run_dir.rglob("*.meta"))


def test_symlinked_root_is_canonicalized_once_for_owned_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    store = RunStore(alias, "canonical")
    prompt = store.friend_prompt_path(1, "friend-ops-0")
    store.write_sensitive(prompt, "prompt")

    assert store.root == real.resolve()
    assert prompt.read_text() == "prompt"


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("visit www.example.com now", "www .example.com"),
        ("mail attacker@example.com now", "attacker @example.com"),
    ],
)
def test_report_prose_and_cells_defang_gfm_bare_autolinks(value, fragment):
    assert fragment in _escape_block(value)
    assert fragment in _escape_cell(value)
