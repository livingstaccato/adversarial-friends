"""Tests for resolution verification and the gate rule (spec §6.4, §7.5).

Weighted toward what §6.4 says a resolution is *not*: it is an attestation,
not proof, and the runner's only real check is whether the location the
author named actually changed. Two rules carry the weight — a resolution is
never rejected merely because the reviewed artifact is unchanged, and a
`fixed` naming an unchanged location is the one case the runner can
positively contradict.
"""

import subprocess

import pytest

from afriend import resolutions, verdicts
from afriend.ledger import Claim, Resolution


def claim(cid="c-0001@1", advisory=False):
    return Claim(
        id=cid,
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=advisory,
        severity="high",
        claim="the guard is missing",
        location="src/auth.py:42",
        evidence="src/auth.py:38",
        failure_scenario="expired token reaches the handler",
        suggested_fix="check exp before dispatch",
    )


def resolution(cid="c-0001@1", disposition="fixed"):
    return Resolution(
        claim_id=cid,
        disposition=disposition,
        author="tim",
        evidence="src/auth.py:38",
        round=3,
        verified=resolutions.LOCATION_CHANGED,
    )


# --- Parsing a location out of evidence ------------------------------------


def test_a_bare_path_is_a_location():
    assert resolutions.parse_location("src/auth.py") == resolutions.Location("src/auth.py")


def test_a_path_with_a_line_narrows_the_comparison():
    loc = resolutions.parse_location("src/auth.py:38")
    assert loc == resolutions.Location("src/auth.py", 38, None)


def test_a_line_range_is_understood():
    loc = resolutions.parse_location("src/auth.py:38-42")
    assert loc == resolutions.Location("src/auth.py", 38, 42)


def test_trailing_prose_after_the_location_is_ignored():
    loc = resolutions.parse_location("src/auth.py:38 now checks exp before dispatch")
    assert loc == resolutions.Location("src/auth.py", 38, None)


def test_prose_with_no_location_is_rejected():
    """§6.4 requires evidence to name a location. Prose alone leaves nothing
    to verify, and treating a bare word as a filename would produce a
    confusing 'unverifiable' instead of an honest 'you named no location'."""
    assert resolutions.parse_location("I fixed it") is None


def test_empty_evidence_is_rejected():
    assert resolutions.parse_location("   ") is None


# --- Verification against the repository snapshot --------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (root / "auth.py").write_text("line1\nline2\nline3\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    return root, sha


def test_an_edited_file_reports_location_changed(repo, monkeypatch):
    root, sha = repo
    monkeypatch.chdir(root)
    (root / "auth.py").write_text("line1\nCHANGED\nline3\n")
    verified = resolutions.verify_location(resolutions.Location("auth.py"), root, sha)
    assert verified == resolutions.LOCATION_CHANGED


def test_an_untouched_file_reports_location_unchanged(repo, monkeypatch):
    root, sha = repo
    monkeypatch.chdir(root)
    verified = resolutions.verify_location(resolutions.Location("auth.py"), root, sha)
    assert verified == resolutions.LOCATION_UNCHANGED


def test_repo_relative_location_ignores_invocation_cwd(repo, monkeypatch, tmp_path):
    root, sha = repo
    monkeypatch.chdir(tmp_path)

    verified = resolutions.verify_location(resolutions.Location("auth.py"), root, sha)

    assert verified == resolutions.LOCATION_UNCHANGED


def test_a_line_range_ignores_changes_elsewhere_in_the_file(repo, monkeypatch):
    """A fix to line 38 of a 900-line file should not be judged by whether
    anything else in the file moved."""
    root, sha = repo
    monkeypatch.chdir(root)
    (root / "auth.py").write_text("line1\nline2\nEDITED\n")
    unchanged = resolutions.verify_location(resolutions.Location("auth.py", 1, 2), root, sha)
    changed = resolutions.verify_location(resolutions.Location("auth.py", 3), root, sha)
    assert unchanged == resolutions.LOCATION_UNCHANGED
    assert changed == resolutions.LOCATION_CHANGED


def test_a_newly_created_file_reports_location_changed(repo, monkeypatch):
    root, sha = repo
    monkeypatch.chdir(root)
    (root / "new.py").write_text("brand new\n")
    verified = resolutions.verify_location(resolutions.Location("new.py"), root, sha)
    assert verified == resolutions.LOCATION_CHANGED


def test_a_deleted_file_reports_location_changed(repo, monkeypatch):
    root, sha = repo
    monkeypatch.chdir(root)
    (root / "auth.py").unlink()
    verified = resolutions.verify_location(resolutions.Location("auth.py"), root, sha)
    assert verified == resolutions.LOCATION_CHANGED


def test_a_location_with_no_snapshot_is_unverifiable(tmp_path):
    """Not invalid -- §6.4 is explicit that a location the runner cannot
    reconstruct is unverifiable, and the report labels it an attestation."""
    verified = resolutions.verify_location(resolutions.Location("anything.py"), None, None)
    assert verified == resolutions.UNVERIFIABLE


def test_a_location_outside_the_repository_is_unverifiable(repo, monkeypatch, tmp_path):
    root, sha = repo
    monkeypatch.chdir(root)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x\n")
    verified = resolutions.verify_location(resolutions.Location(str(outside)), root, sha)
    assert verified == resolutions.UNVERIFIABLE


def test_the_reviewed_artifact_is_verified_against_its_frozen_copy(tmp_path, monkeypatch):
    """The artifact has a frozen copy of its own, so it can be verified even
    with no repository at all."""
    frozen = tmp_path / "frozen" / "spec.md"
    frozen.parent.mkdir()
    frozen.write_text("original\n")
    live = tmp_path / "spec.md"
    live.write_text("revised\n")
    monkeypatch.chdir(tmp_path)
    verified = resolutions.verify_location(
        resolutions.Location("spec.md"),
        None,
        None,
        frozen_artifact=frozen,
        artifact_path=live,
    )
    assert verified == resolutions.LOCATION_CHANGED


def test_the_reviewed_artifact_uses_its_recorded_path_from_another_cwd(tmp_path, monkeypatch):
    live = tmp_path / "project" / "spec.md"
    live.parent.mkdir()
    live.write_text("revised\n")
    frozen = tmp_path / "run" / "artifact" / "spec.md"
    frozen.parent.mkdir(parents=True)
    frozen.write_text("original\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    verified = resolutions.verify_location(
        resolutions.Location("spec.md"),
        None,
        None,
        frozen_artifact=frozen,
        artifact_path=live,
    )

    assert verified == resolutions.LOCATION_CHANGED


def test_repo_containment_resolves_a_symlinked_root(repo, monkeypatch, tmp_path):
    root, sha = repo
    link = tmp_path / "linked-repo"
    link.symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    verified = resolutions.verify_location(resolutions.Location("auth.py"), link, sha)

    assert verified == resolutions.LOCATION_UNCHANGED


# --- The one rule the runner can enforce -----------------------------------


def test_fixed_naming_an_unchanged_location_is_rejected():
    """The single case the runner can positively contradict."""
    reason = resolutions.rejection_reason("fixed", resolutions.LOCATION_UNCHANGED)
    assert reason is not None
    assert "has not changed" in reason


def test_fixed_naming_a_changed_location_is_accepted():
    assert resolutions.rejection_reason("fixed", resolutions.LOCATION_CHANGED) is None


def test_fixed_with_an_unverifiable_location_is_rejected():
    reason = resolutions.rejection_reason("fixed", resolutions.UNVERIFIABLE)
    assert reason is not None
    assert "accepted-risk" in reason


@pytest.mark.parametrize("disposition", ["rejected", "accepted-risk"])
def test_dispositions_that_claim_no_change_accept_an_unchanged_location(disposition):
    """Neither asserts that anything was edited, so an unchanged location is
    exactly what you would expect."""
    assert resolutions.rejection_reason(disposition, resolutions.LOCATION_UNCHANGED) is None


# --- §7.5 the gate ---------------------------------------------------------


def test_a_settled_upheld_claim_blocks_until_resolved():
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.SETTLED_UPHELD}, resolutions=[]
    )
    assert [c.id for c in blocking] == ["c-0001@1"]


def test_a_resolution_clears_the_claim():
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.SETTLED_UPHELD}, [resolution()]
    )
    assert blocking == []


def test_settled_refuted_clears_without_a_resolution():
    """The only state that clears a gate on its own -- the judges agreed the
    claim was wrong, so there is nothing to fix."""
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.SETTLED_REFUTED}, resolutions=[]
    )
    assert blocking == []


def test_a_discarded_claim_blocks_the_gate():
    """Nobody could check it: two rounds of judges unable to verify the
    evidence. A gate that passed on that would pass on the strength of
    nobody having looked, which is the failure this tool exists to prevent.
    The set used to clear it; a crossexam of verdicts.py found the comment
    above the set, the spec, and the code giving three different answers."""
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.DISCARDED}, resolutions=[]
    )
    assert [c.id for c in blocking] == ["c-0001@1"]


def test_a_superseded_claim_is_exempt_rather_than_cleared():
    """Rewritten; its successor carries the question (spec §7.2: "n/a").
    Blocking the original too would demand two resolutions for one defect."""
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.SUPERSEDED}, resolutions=[]
    )
    assert blocking == []


def test_an_advisory_claim_never_blocks():
    """Its lens deliberately does not demand a failure scenario; gating on
    "this is more than you need" would silence the lens entirely."""
    blocking = resolutions.blocking_claims(
        [claim(advisory=True)], {"c-0001@1": verdicts.SETTLED_UPHELD}, resolutions=[]
    )
    assert blocking == []


def test_a_non_terminal_claim_blocks():
    """`contested` is not a pass -- the run had not finished deciding it."""
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.CONTESTED}, resolutions=[]
    )
    assert [c.id for c in blocking] == ["c-0001@1"]


def test_a_deadlocked_claim_blocks():
    """Terminal, but it needs a human: the judges disagreed and the gate is
    exactly where that has to be settled."""
    blocking = resolutions.blocking_claims(
        [claim()], {"c-0001@1": verdicts.DEADLOCKED}, resolutions=[]
    )
    assert [c.id for c in blocking] == ["c-0001@1"]
