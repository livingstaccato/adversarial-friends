"""End-to-end `--merge orchestrator` and `--resume` (spec §4.2).

The halt/resume cycle is the only place this tool deliberately stops
mid-run, hands a file to something else, and picks up where it left off. The
properties worth pinning are that it stops with the right exit code and a
usable request, that resuming does NOT re-run the round that already ran,
and that the adjudicated merges actually reach the ledger and the report.
"""

import json
import subprocess
import sys

from e2e_helpers import AF, _env, run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with problems.\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _ledger(tmp_path):
    text = (_run_dir(tmp_path) / "claims.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _halt(tmp_path, *modes, mode="report", extra=()):
    args = []
    for m in modes:
        args += ["--friend", f"fake:{m}"]
    return run_af(
        tmp_path, _artifact(tmp_path), *args, "--merge", "orchestrator", *extra, mode=mode
    )


def _respond(tmp_path, merges, round_no=1):
    request = _run_dir(tmp_path) / f"round-{round_no}" / "REQUEST.json"
    data = json.loads(request.read_text())
    data["merges"] = merges
    (request.parent / "RESPONSE.json").write_text(json.dumps(data))
    return data


def _resume(tmp_path):
    return subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            "--resume",
            _run_dir(tmp_path).name,
            "--out",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )


# --- The halt --------------------------------------------------------------


def test_the_run_halts_with_exit_ten(tmp_path):
    """§7.6's "needs orchestrator"."""
    result = _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    assert result.returncode == 10, result.stderr
    assert "waiting for merge adjudication" in result.stderr


def test_the_request_names_every_claim(tmp_path):
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    request = json.loads((_run_dir(tmp_path) / "round-1" / "REQUEST.json").read_text())
    ledger_ids = {r["id"] for r in _ledger(tmp_path) if r["type"] == "claim"}
    assert {c["id"] for c in request["claims"]} == ledger_ids


def test_the_halt_message_names_the_resume_command(tmp_path):
    """A halt nobody knows how to continue is a hang with extra steps."""
    result = _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    assert "--resume" in result.stderr
    assert _run_dir(tmp_path).name in result.stderr


def test_a_halted_run_is_still_readable(tmp_path):
    """run.json and report.md are written before the halt, so a run waiting
    on an orchestrator is not an opaque directory -- and, more importantly,
    a resume can rebuild its configuration from run.json."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    meta = _run_json(tmp_path)
    assert meta["invocation"]["mode"] == "report"
    assert meta["roster"]
    assert (_run_dir(tmp_path) / "report.md").is_file()


def test_exact_merge_does_not_halt(tmp_path):
    """The default has to complete unaided -- that is what makes the
    documented CLI usable from a plain shell (§4.2)."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr


# --- Resuming --------------------------------------------------------------


def test_resuming_applies_the_merges(tmp_path):
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    ids = sorted(r["id"] for r in _ledger(tmp_path) if r["type"] == "claim")
    assert len(ids) == 2, ids
    _respond(tmp_path, [{"canonical": ids[0], "duplicate": ids[1], "rationale": "same defect"}])

    result = _resume(tmp_path)
    assert result.returncode == 0, result.stderr
    aliases = [r for r in _ledger(tmp_path) if r["type"] == "alias"]
    assert [a["source"] for a in aliases] == ["orchestrator"]
    assert aliases[0]["rationale"] == "same defect"


def test_resuming_does_not_rerun_the_critique(tmp_path):
    """Re-running it would spend a full fan-out and produce DIFFERENT claims
    than the ones just adjudicated, so the adjudication would apply to ids
    that no longer exist."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    before = [r["id"] for r in _ledger(tmp_path) if r["type"] == "claim"]
    _respond(tmp_path, [])
    _resume(tmp_path)
    after = [r["id"] for r in _ledger(tmp_path) if r["type"] == "claim"]
    assert after == before


def test_corroboration_survives_the_merge(tmp_path):
    """These are merges of differently worded claims -- exactly where
    independent agreement is the strongest evidence."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    ids = sorted(r["id"] for r in _ledger(tmp_path) if r["type"] == "claim")
    _respond(tmp_path, [{"canonical": ids[0], "duplicate": ids[1], "rationale": "same"}])
    _resume(tmp_path)
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "corroborated by 2 friends" in report


def test_an_empty_response_is_a_real_answer(tmp_path):
    """ "I looked and none of these are duplicates" must complete the run."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    _respond(tmp_path, [])
    result = _resume(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not [r for r in _ledger(tmp_path) if r["type"] == "alias"]


def test_resuming_without_a_response_says_what_to_do(tmp_path):
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    result = _resume(tmp_path)
    assert result.returncode == 2
    assert "RESPONSE.json" in result.stderr


def test_a_response_naming_an_unknown_claim_is_refused(tmp_path):
    """It would write an Alias pointing at nothing."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b")
    ids = sorted(r["id"] for r in _ledger(tmp_path) if r["type"] == "claim")
    _respond(tmp_path, [{"canonical": ids[0], "duplicate": "c-9999@1"}])
    result = _resume(tmp_path)
    assert result.returncode == 2
    assert "not a claim in this run" in result.stderr


def test_resuming_carries_the_original_mode_through(tmp_path):
    """§4.2: the same response must produce the same run. The mode comes
    from the run directory, not from the resuming command line, which does
    not repeat it."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b", mode="crossexam")
    _respond(tmp_path, [])
    result = _resume(tmp_path)
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["mode"] == "crossexam"
    assert meta["claim_states"], "the resumed run should have judged its claims"


def test_a_resumed_crossexam_judges_in_later_rounds(tmp_path):
    """Round 1 is spent; judging must continue at round 2 rather than
    restarting."""
    _halt(tmp_path, "judge_uphold_a", "judge_uphold_b", mode="crossexam")
    _respond(tmp_path, [])
    _resume(tmp_path)
    assert (_run_dir(tmp_path) / "round-2").is_dir()
    assert [r for r in _ledger(tmp_path) if r["type"] == "verdict"]


def test_resuming_an_unknown_run_is_a_usage_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(AF), "run", "--resume", "run-nope", "--out", str(tmp_path / "runs")],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert result.returncode == 2
    assert "no such run" in result.stderr


def test_loop_with_orchestrator_merge_is_refused(tmp_path):
    """A loop would halt once per iteration and resume into mid-iteration
    state this build does not reconstruct. Refusing beats resuming into a
    state nobody has verified."""
    result = _halt(tmp_path, "judge_uphold_a", mode="loop")
    assert result.returncode == 2
    assert "not supported with --mode loop" in result.stderr


# --- §14.2 parse-halt extraction -------------------------------------------


def test_unparseable_output_halts_for_extraction(tmp_path):
    """§14.2: repair is a pure transformation with no model call, so when it
    fails the only thing left that can read the raw text is something with
    judgment. Under --merge=orchestrator that is a halt, not a discard."""
    result = _halt(tmp_path, "offtopic", "judge_uphold_a")
    assert result.returncode == 10, result.stderr
    assert "could not be parsed" in result.stderr
    request = json.loads((_run_dir(tmp_path) / "round-1" / "REQUEST.json").read_text())
    assert request["question"] == "extract"
    assert request["unparseable"][0]["raw"]


def test_a_parseable_friend_in_the_same_round_is_not_lost(tmp_path):
    """The halt is collected and raised AFTER the loop: halting mid-loop
    would strand the claims of friends processed later, whose results exist
    only in memory and would be gone on resume."""
    _halt(tmp_path, "offtopic", "judge_uphold_a")
    claims = [r for r in _ledger(tmp_path) if r["type"] == "claim"]
    assert claims, "the friend that parsed cleanly should already be in the ledger"


def test_extracted_claims_reach_the_ledger_on_resume(tmp_path):
    _halt(tmp_path, "offtopic", "judge_uphold_a")
    request_path = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    data = json.loads(request_path.read_text())
    data["unparseable"][0]["findings"] = [
        {
            "severity": "high",
            "claim": "read out of prose by hand",
            "location": "spec.md:1",
            "evidence": "spec.md:1",
            "failure_scenario": "the design does not say what happens",
            "suggested_fix": "say what happens",
        }
    ]
    (request_path.parent / "RESPONSE.json").write_text(json.dumps(data))

    result = _resume(tmp_path)
    assert result.returncode in (0, 1), result.stderr
    texts = [r["claim"] for r in _ledger(tmp_path) if r["type"] == "claim"]
    assert "read out of prose by hand" in texts


def test_an_extracted_claim_keeps_the_friend_as_its_author(tmp_path):
    """An orchestrator read the friend's words, it did not invent them --
    and judging is decided by origin (§7.1), so authorship has to survive."""
    _halt(tmp_path, "offtopic", "judge_uphold_a")
    request_path = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    data = json.loads(request_path.read_text())
    friend = data["unparseable"][0]["friend"]
    data["unparseable"][0]["findings"] = [
        {
            "severity": "low",
            "claim": "extracted",
            "location": None,
            "evidence": "spec.md:1",
            "failure_scenario": "x",
            "suggested_fix": "y",
        }
    ]
    (request_path.parent / "RESPONSE.json").write_text(json.dumps(data))
    _resume(tmp_path)
    extracted = [r for r in _ledger(tmp_path) if r["type"] == "claim" and r["claim"] == "extracted"]
    assert extracted[0]["origin"] == [friend]


def test_extracted_findings_are_held_to_the_claim_schema(tmp_path):
    """An orchestrator is trusted to read, not to bypass the schema: a
    hand-extracted claim missing failure_scenario is unsubstantiated for
    exactly the reasons §6.1 gives, whoever wrote it."""
    _halt(tmp_path, "offtopic", "judge_uphold_a")
    request_path = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    data = json.loads(request_path.read_text())
    data["unparseable"][0]["findings"] = [{"severity": "high", "claim": "no evidence given"}]
    (request_path.parent / "RESPONSE.json").write_text(json.dumps(data))
    result = _resume(tmp_path)
    assert result.returncode == 2
    assert "not valid claims" in result.stderr


def test_exact_merge_never_halts_for_extraction(tmp_path):
    """Under the default the friend is simply failed, which is what keeps
    the documented CLI usable from a plain shell (§4.2)."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:offtopic")
    assert result.returncode == 1, result.stderr
    assert not (_run_dir(tmp_path) / "round-1" / "REQUEST.json").exists()
