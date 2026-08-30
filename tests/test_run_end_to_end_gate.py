"""End-to-end `--mode gate` and `afriend resolve` (spec §6.4, §7.5).

The gate is the mode with teeth: it is the one that fails a build. These
check that it fails for the right reasons, clears for the right reasons, and
that a resolution is recorded as the attestation §6.4 says it is rather than
as proof of anything.

Two fixture details matter and are easy to get wrong:

* The artifact lives INSIDE a git repository, because a resolution is
  verified against the run's repository snapshot. An artifact in a bare
  tmp_path has no snapshot, so every location would come back
  `unverifiable` and the tests would pass while checking nothing.
* Friends are requested as `fake:<mode>:repo`. A snapshot is only taken when
  some friend actually needs repo scope, so a roster of doc-scope fakes
  produces a run with `snapshot_sha: null` -- same vacuous outcome.
"""

import json
from pathlib import Path
import subprocess
import sys

from e2e_helpers import AF, _env, _git_commit, _git_repo, run_af


def _repo(tmp_path):
    """A git repo holding the artifact under review and a file to 'fix'."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "spec.md").write_text("# spec\n\nA design with problems.\n")
    (repo / "auth.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "base")
    return repo


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _ledger(tmp_path):
    text = (_run_dir(tmp_path) / "claims.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _gate(tmp_path, repo, *modes, extra=()):
    args = []
    for mode in modes:
        args += ["--friend", f"fake:{mode}:repo"]
    return run_af(tmp_path, repo / "spec.md", *args, *extra, mode="gate")


def _resolve(tmp_path, repo, claim, disposition, evidence):
    return subprocess.run(
        [
            sys.executable,
            str(AF),
            "resolve",
            str(_run_dir(tmp_path)),
            "--claim",
            claim,
            "--disposition",
            disposition,
            "--evidence",
            evidence,
        ],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=str(repo),
    )


# --- The gate itself -------------------------------------------------------


def test_an_upheld_claim_blocks_the_gate(tmp_path):
    """settled-upheld is not a pass: the judges agreed the defect is real,
    which needs a Resolution rather than silence."""
    repo = _repo(tmp_path)
    result = _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    assert result.returncode == 1, result.stderr
    assert "gate blocked" in result.stderr
    meta = _run_json(tmp_path)
    assert meta["gate_blocked"] is True
    assert meta["gate_blocking_claims"]


def test_a_refuted_claim_does_not_block(tmp_path):
    """settled-refuted is the one state that clears a gate unaided -- the
    judges agreed the claim was wrong, so there is nothing to fix.

    Three friends, because a claim needs two independent judges to settle:
    with only one judge, §7.1's single-judge branch requires agreement with
    the author, so a lone refutation deadlocks instead."""
    repo = _repo(tmp_path)
    result = _gate(tmp_path, repo, "judge_refute_a", "judge_refute_b", "judge_refute_c")
    assert _run_json(tmp_path)["gate_blocking_claims"] == []
    assert result.returncode == 0, result.stderr


def test_the_blocking_claims_are_named_on_stderr(tmp_path):
    """A gate that fails without saying what failed is a gate nobody can
    act on."""
    repo = _repo(tmp_path)
    result = _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    for cid in _run_json(tmp_path)["gate_blocking_claims"]:
        assert cid in result.stderr


def test_the_run_records_what_a_resolution_will_need(tmp_path):
    """A resolution is verified against the run's snapshot, which can only
    happen if the run remembered which snapshot it took."""
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    meta = _run_json(tmp_path)
    assert meta["snapshot_sha"]
    assert meta["repo_root"]
    assert meta["artifact_path"] == str((repo / "spec.md").absolute())


def test_a_doc_scope_roster_still_gets_a_snapshot_under_gate(tmp_path):
    """Found running a real gate against ollama, whose friends are all
    doc-scope because an HTTP friend has no filesystem to constrain.

    The snapshot used to be taken only when some friend needed repo scope --
    but a resolution is verified against that same snapshot, and those two
    needs do not coincide. Every resolution came back `unverifiable` for a
    file sitting in the repository, which silently downgraded the one check
    the runner can make: `fixed` at an unchanged location was accepted
    instead of refused.
    """
    repo = _repo(tmp_path)
    result = run_af(
        tmp_path,
        repo / "spec.md",
        "--friend",
        "fake:judge_uphold_a",  # no :repo suffix -- doc scope
        "--friend",
        "fake:judge_uphold_b",
        mode="gate",
    )
    assert result.returncode == 1, result.stderr
    meta = _run_json(tmp_path)
    assert meta["snapshot_sha"], "a gate run must snapshot even with no repo-scope friend"

    # And the consequence that actually matters: the refusal works.
    cid = meta["gate_blocking_claims"][0]
    refused = _resolve(tmp_path, repo, cid, "fixed", "auth.py")
    assert refused.returncode == 2
    assert "has not changed" in refused.stderr


# --- afriend resolve -------------------------------------------------------


def test_resolving_every_claim_clears_the_gate(tmp_path):
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    blocking = _run_json(tmp_path)["gate_blocking_claims"]
    assert blocking

    # Something actually changed, which is what §6.4 verifies.
    (repo / "auth.py").write_text("fixed\n")
    last = None
    for cid in blocking:
        last = _resolve(tmp_path, repo, cid, "fixed", "auth.py")
    assert last is not None
    assert last.returncode == 0, last.stderr
    assert "gate clear" in last.stdout


def test_a_partial_resolution_still_blocks(tmp_path):
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    blocking = _run_json(tmp_path)["gate_blocking_claims"]
    assert len(blocking) > 1, "this test needs more than one blocking claim"

    (repo / "auth.py").write_text("fixed\n")
    result = _resolve(tmp_path, repo, blocking[0], "fixed", "auth.py")
    assert result.returncode == 1
    assert "still need a resolution" in result.stderr


def test_a_resolution_is_appended_to_the_ledger(tmp_path):
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    (repo / "auth.py").write_text("fixed\n")
    _resolve(tmp_path, repo, cid, "fixed", "auth.py")

    recorded = [r for r in _ledger(tmp_path) if r["type"] == "resolution"]
    assert len(recorded) == 1
    assert recorded[0]["claim_id"] == cid
    assert recorded[0]["disposition"] == "fixed"
    assert recorded[0]["verified"] == "location-changed"


def test_fixed_naming_an_unchanged_location_is_refused(tmp_path):
    """The one attestation the runner can positively contradict."""
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    result = _resolve(tmp_path, repo, cid, "fixed", "auth.py")
    assert result.returncode == 2, result.stdout
    assert "has not changed" in result.stderr
    assert not [r for r in _ledger(tmp_path) if r["type"] == "resolution"]


def test_a_fix_that_landed_outside_the_artifact_is_accepted(tmp_path):
    """§6.4 is explicit: a resolution is never rejected merely because the
    reviewed artifact is unchanged. A valid fix for a claim about a design
    doc frequently lands in source, and requiring the artifact to change
    would force dummy edits to clear a gate."""
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    (repo / "auth.py").write_text("fixed\n")  # spec.md deliberately untouched
    result = _resolve(tmp_path, repo, cid, "fixed", "auth.py")
    assert result.returncode in (0, 1), result.stderr
    assert "location-changed" in result.stdout


def test_accepted_risk_does_not_require_a_change(tmp_path):
    """Neither `rejected` nor `accepted-risk` asserts that anything was
    edited, so an unchanged location is exactly what you would expect."""
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    result = _resolve(tmp_path, repo, cid, "accepted-risk", "auth.py")
    assert result.returncode in (0, 1), result.stderr
    assert "accepted-risk" in result.stdout


def test_evidence_with_no_location_is_refused(tmp_path):
    """§6.4: evidence must name a location. Prose alone leaves nothing to
    verify, and recording it would make every resolution look equally
    well-supported."""
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    result = _resolve(tmp_path, repo, cid, "fixed", "I fixed it")
    assert result.returncode == 2
    assert "must name a location" in result.stderr


def test_an_unknown_claim_id_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    result = _resolve(tmp_path, repo, "c-9999@1", "fixed", "auth.py")
    assert result.returncode == 2
    assert "no claim" in result.stderr


def test_fixed_at_an_unreconstructible_location_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _gate(tmp_path, repo, "judge_uphold_a", "judge_uphold_b")
    cid = _run_json(tmp_path)["gate_blocking_claims"][0]
    result = _resolve(tmp_path, repo, cid, "fixed", str(tmp_path / "outside.py"))
    assert result.returncode == 2
    assert "accepted-risk" in result.stderr
    recorded = [r for r in _ledger(tmp_path) if r["type"] == "resolution"]
    assert recorded == []


def test_a_missing_run_directory_is_a_usage_error(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(AF),
            "resolve",
            str(Path(tmp_path) / "no-such-run"),
            "--claim",
            "c-0001@1",
            "--disposition",
            "fixed",
            "--evidence",
            "a.py",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert result.returncode == 2
    assert "no such run" in result.stderr
