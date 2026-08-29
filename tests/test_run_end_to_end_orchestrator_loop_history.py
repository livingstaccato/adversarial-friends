"""c-0003 / c-0006 from cross-examining `commands/resume.py`: a halt mid-loop
rendered a report that had forgotten what an EARLIER iteration -- in a
process that has since exited -- had already produced.

`--mode loop --merge orchestrator` halts once per iteration. `all_aliases`
and the verdict states/reasoning handed to the report renderer both used to
come from state that lives only in the process that built it: a
process-local accumulator for aliases, and `carry_over` never reaching the
halt's own `render()` call for verdicts. Each resume starts a new process,
so a SECOND halt -- reached after a resume has already carried the run
into its next iteration -- rendered a report that had silently dropped the
first iteration's merges and verdict states, even though both were sitting
in the ledger and in `carry_over` the whole time.

Both fixed the same way: read from what actually persists (the ledger for
aliases, `carry_over` for verdicts) instead of a process-local variable
that resets on every resume.
"""

import json

from e2e_helpers import _env, run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with problems.\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _report(tmp_path):
    return (_run_dir(tmp_path) / "report.md").read_text()


def _halt(tmp_path, *modes, mode="loop", extra=()):
    args = []
    for m in modes:
        args += ["--friend", f"fake:{m}"]
    return run_af(
        tmp_path, _artifact(tmp_path), *args, "--merge", "orchestrator", *extra, mode=mode
    )


def _respond(tmp_path, merges, round_no):
    request = _run_dir(tmp_path) / f"round-{round_no}" / "REQUEST.json"
    data = json.loads(request.read_text())
    data["merges"] = merges
    (request.parent / "RESPONSE.json").write_text(json.dumps(data))


def _resume(tmp_path):
    import subprocess
    import sys

    from e2e_helpers import AF

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


# --- c-0003: merged duplicates survive a resume into the next iteration ----


def test_an_earlier_iterations_merge_survives_into_a_later_halts_report(tmp_path):
    """Two `good` friends in round 1 produce byte-identical claims, which
    `exact_merge` aliases together BEFORE the orchestrator halt is even
    reached -- so this alias exists in the ledger from iteration 1's very
    first round, well before any human response is written.

    Iteration 1 resumes and (with nothing left to merge) proceeds into
    iteration 2, whose own round-1 critique produces a SECOND exact-merge
    alias and halts again. That second halt's report is rendered by a
    process that never itself ran iteration 1's critique -- the only way
    it can know about iteration 1's alias is by reading the ledger, which
    is exactly what c-0003 was about.
    """
    # Different lenses (an unrecognised mode string falls back to "good"'s
    # behaviour -- see fake_friend.py's MODES.get(mode, MODES["good"])) so
    # the two friends have distinct ledger identities, while still
    # producing byte-identical claim text for exact_merge to alias.
    result = _halt(
        tmp_path,
        "good",
        "good_twin",
        extra=("--max-rounds", "2", "--max-loop-iterations", "2"),
    )
    assert result.returncode == 10, result.stderr
    first_report = _report(tmp_path)
    assert "Merged duplicates" in first_report, "exact_merge should have produced one already"

    _respond(tmp_path, [], round_no=1)
    second_halt = _resume(tmp_path)

    assert second_halt.returncode == 10, second_halt.stderr
    report = _report(tmp_path)
    assert "Merged duplicates" in report
    merged_section = report.split("## Merged duplicates")[1]
    # c-0002@1 is iteration 1's alias, written by a process that has since
    # exited -- the exact one the old, process-local accumulator lost. Its
    # presence here is the whole claim. Iteration 2's own round-3 critique
    # also merges (into the same canonical, since exact_merge prefers an
    # already-known claim as canonical over a fresh one) and is present too.
    assert "`c-0002@1` merged into `c-0001@1`" in merged_section, merged_section
    assert merged_section.count("merged into") >= 2, merged_section


# --- c-0006: verdict states/reasoning survive into a later halt's report ---


def test_an_earlier_iterations_verdicts_survive_into_a_later_halts_report(tmp_path):
    """Iteration 1 settles a claim (`judge_uphold_a`/`judge_uphold_b` both
    uphold it). Iteration 2's own round-1 critique then halts for its own
    adjudication. That second halt's report is rendered by
    `write_halt` -- which used to call `render()` with no `states=` or
    `verdicts=` at all, so it showed raw findings with none of the
    reasoning iteration 1's judges had already produced, even though
    `carry_over` (passed into `write_halt`) held it directly.
    """
    result = _halt(
        tmp_path,
        "judge_uphold_a",
        "judge_uphold_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "2"),
    )
    assert result.returncode == 10, result.stderr
    _respond(tmp_path, [], round_no=1)
    judging_halt = _resume(tmp_path)
    # Iteration 1 now judges (round 2) and settles, then iteration 2's
    # round-1 critique halts for ITS merge adjudication.
    assert judging_halt.returncode == 10, judging_halt.stderr

    report = _report(tmp_path)
    assert "## Cross-examination" in report, "verdict section is missing entirely"
    assert "settled-upheld" in report, report
