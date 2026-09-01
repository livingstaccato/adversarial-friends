"""End-to-end tests for `afriend run --mode report`: lens wiring (Task 13),
single-friend visibility, one-friend's-exception isolation (C3), the E2BIG
prompt-size downgrade, and friend stderr capture/sanitization (I1,
Regression 3).

See tests/e2e_helpers.py for the safe-PATH subprocess harness this file (and
its siblings test_run_end_to_end_basics.py and
test_run_end_to_end_isolation.py) share.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from e2e_helpers import FAKE, _safe_path_dir, run_af
import pytest

from adversarial_friends import adapters, cli, dispatch
from adversarial_friends.commands import friends as friends_module

# --- Lens wiring (Task 13 coordinator finding) ----------------------------
#
# Before this fix, cmd_run built exactly one prompt.txt (PROMPT_HEADER +
# artifact) before the dispatch loop and handed the same Path to every
# friend regardless of --friend cli:lens. LENS_DIR/available_lenses() only
# ever harvested filename stems for round-robin assignment and bookkeeping
# (friend naming, claim origin/lens fields) -- no code path ever read a
# lens file's prose into a prompt. Every friend therefore received a
# byte-identical, lens-blind prompt: the only diversity in a run was model
# diversity, and the lens name on a claim was decorative. These tests prove
# the fix by reading the actual <friend>.prompt files a real run writes to
# disk, not by inspecting the runner's internals.


def test_two_friends_with_different_lenses_get_demonstrably_different_prompts(tmp_path):
    """The core fix. security and ops are both real, shipped lens files
    with genuinely different prose. Each friend's prompt must carry its own
    lens's body -- frontmatter stripped, not appended whole -- while both
    still carry the shared contract header and the artifact."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    round_dir = runs[0] / "round-1"

    security_prompt = (round_dir / "fake-security-0.prompt").read_text()
    ops_prompt = (round_dir / "fake-ops-1.prompt").read_text()

    assert security_prompt != ops_prompt

    # Each prompt carries its own lens's distinctive prose, and not the
    # other lens's...
    assert "Attack the design as written" in security_prompt
    assert "Attack the design as written" not in ops_prompt
    assert "Ask what happens at 3am" in ops_prompt
    assert "Ask what happens at 3am" not in security_prompt

    # ...with the YAML frontmatter stripped, not carried into the prompt...
    assert "requires_failure_scenario:" not in security_prompt
    assert "requires_failure_scenario:" not in ops_prompt

    # ...and both still carry the shared contract header and the artifact.
    assert "Return ONLY a JSON object" in security_prompt
    assert "Return ONLY a JSON object" in ops_prompt
    assert "A design with a missing guard" in security_prompt
    assert "A design with a missing guard" in ops_prompt


def test_friend_with_unknown_lens_falls_back_to_generic_prompt_and_records_a_downgrade(tmp_path):
    """fake:good's lens slot is "good", which has no lenses/good.md file --
    every other fake-friend test in this file already relies on exactly
    this fallback (fake:offtopic, fake:hang, fake:cwd_probe, ...) and must
    keep working unchanged. The fallback must be explicit and visible, not
    silent: no --- LENS --- section in the written prompt, and a downgrade
    naming the friend and the missing lens in run.json."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    prompt_text = (runs[0] / "round-1" / "fake-good-0.prompt").read_text()
    assert "--- LENS ---" not in prompt_text
    assert "Return ONLY a JSON object" in prompt_text

    meta = json.loads((runs[0] / "run.json").read_text())
    assert any(
        "fake-good-0" in note and "good" in note and "lens" in note.lower()
        for note in meta["downgrades"]
    ), meta["downgrades"]


def test_advisory_flag_is_set_from_the_lens_requires_failure_scenario(tmp_path):
    """lenses/scope.md is the one shipped lens with
    requires_failure_scenario: false. A claim produced under it must come
    back with advisory=True -- previously hardcoded False for every claim
    regardless of lens. report.py already renders an *(advisory)* marker;
    only the runner ever needed to set the field truthfully."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:scope")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
    claims = [r for r in ledger if r["type"] == "claim"]
    assert claims and all(c["advisory"] is True for c in claims)

    report = (runs[0] / "report.md").read_text()
    assert "(advisory)" in report


def test_advisory_flag_is_false_for_a_lens_that_requires_a_failure_scenario(tmp_path):
    """The converse of the above: security.md sets
    requires_failure_scenario: true (the default for every lens but
    scope), so its claims must come back non-advisory."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
    claims = [r for r in ledger if r["type"] == "claim"]
    assert claims and all(c["advisory"] is False for c in claims)


# --- Single-friend visibility (Task 13 coordinator review, round 2) -------
#
# --friend REPLACES the whole roster rather than augmenting default
# discovery (cmd_run branches `if args.friend: _specs_from_flags(...) else
# resolve(...)` -- there is no path that layers a --friend override on top
# of discovery). A single --friend flag therefore produces a single-friend
# run, which cannot cross-examine anything (design doc §8.3's "degraded
# single-friend mode"). That reduced guarantee must be visible in run.json
# and report.md, the same rule already applied to every other downgrade
# this runner records (repo-scope, missing lens, degraded signal handling).


def test_single_friend_run_via_friend_flag_records_a_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any(
        "one friend" in note.lower() and "cross-examin" in note.lower()
        for note in meta["downgrades"]
    ), meta["downgrades"]
    report = (runs[0] / "report.md").read_text()
    assert "cross-examin" in report.lower()


def test_two_friend_run_does_not_record_the_single_friend_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert not any("cross-examin" in note.lower() for note in meta["downgrades"]), meta[
        "downgrades"
    ]


# --- C3: one friend's unexpected exception must not end the whole run -----
#
# spawn.run_process previously caught only FileNotFoundError/PermissionError
# from Popen(); any other OSError (E2BIG from an oversized prompt in one
# argv element, ENOEXEC from a broken shim -- see test_spawn.py for the
# unit-level proof of both) escaped the worker thread, was not an AfError,
# and killed the WHOLE run with a raw traceback -- losing every other
# friend's already-succeeded result along with it. Fixed at two layers:
# spawn.run_process now catches OSError broadly, and cmd_run's own
# per-friend dispatch wrapper (_run_one, inside cmd_run) catches any OTHER
# unexpected exception too, so nothing short of a deliberate AfError can
# end a run.


def test_enoexec_friend_does_not_prevent_a_second_friend_from_being_reported(tmp_path):
    """End-to-end version of test_spawn.py's ENOEXEC unit test: a real
    adapter ('codex') resolves, via PATH, to a broken shim (executable bit
    set, but not a valid executable format at all) instead of the real
    codex CLI. This must fail as a clean per-friend result, not take down
    the dispatch of a second, working friend (fake:good)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")

    broken_dir = Path(tempfile.mkdtemp(prefix="af-broken-bin-"))
    broken = broken_dir / "codex"
    broken.write_bytes(b"")  # empty file: no shebang, no recognizable format
    broken.chmod(0o755)

    combined_path = f"{_safe_path_dir()}{os.pathsep}{broken_dir}"
    result = run_af(
        tmp_path,
        artifact,
        "--friend",
        "codex:ops",
        "--friend",
        "fake:good",
        # This test targets dispatch-time ENOEXEC isolation, so grant tool
        # authority explicitly instead of stopping at deny-argv preflight.
        "--allow-external-tools=codex",
        env_extra={"PATH": combined_path},
    )
    assert result.returncode == 0, result.stderr

    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()
    assert "the guard is missing" in report  # fake:good's finding still came through

    meta = json.loads((runs[0] / "run.json").read_text())
    codex_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("codex"))
    assert "failed" in codex_status.lower()
    fake_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("fake"))
    assert fake_status == "ok"


def test_unexpected_exception_in_one_friends_dispatch_does_not_end_the_run(monkeypatch, tmp_path):
    """Simulates a bug unrelated to process-spawning entirely (something
    spawn.run_process's own OSError handling could never catch, since it
    never even reaches Popen()) by monkeypatching build_argv to raise for
    exactly one friend's cli ('codex'), while a second friend ('fake:good',
    which never calls build_argv at all -- see dispatch._dispatch)
    succeeds normally. cli.cmd_run is called in-process (not via
    subprocess) because the patch target is an internal module name."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("PATH", str(_safe_path_dir()))
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda binary: f"/bin/{binary}" if binary == "codex" else None,
    )
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")

    real_build_argv = adapters.build_argv

    def _boom(adapter, spec, prompt_file, schema_file, external_tool_policy):
        if spec.cli == "codex":
            raise RuntimeError("simulated unexpected bug in adapter wiring")
        return real_build_argv(adapter, spec, prompt_file, schema_file, external_tool_policy)

    monkeypatch.setattr(dispatch, "build_argv", _boom)
    parser = cli.build_parser()
    parsed = parser.parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "codex:ops",
            "--friend",
            "fake:good",
            # Reach the monkeypatched dispatch path under the broken shim.
            "--allow-external-tools=codex",
        ]
    )
    returncode = cli.cmd_run(parsed)
    assert returncode == 0

    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "the guard is missing" in report  # fake:good's finding survived
    meta = json.loads((runs[0] / "run.json").read_text())
    codex_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("codex"))
    assert "unexpected error" in codex_status.lower()
    assert "simulated unexpected bug" in codex_status
    fake_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("fake"))
    assert fake_status == "ok"


def test_oversized_prompt_for_a_non_stdin_adapter_records_an_e2big_downgrade(tmp_path):
    """claude places the whole prompt in one argv element (prompt_mode
    'trailing-arg'); Linux commonly caps a single argv element near 128KB
    (the limit varies by OS -- this test itself may run on macOS), so a
    large artifact can make the real dispatch fail with E2BIG. This is detected
    and recorded up front (see dispatch.PROMPT_ARGV_WARN_BYTES's check
    inside cmd_run), not solved -- switching prompt modes is a design
    change. The downgrade must appear regardless of whether the friend
    dispatches. A tiny executable shim makes Claude pass explicit preflight;
    the fake friend still provides the usable result this test needs."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n" + ("x" * 150_000) + "\n")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    claude = binary_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 1\n")
    claude.chmod(0o755)
    result = run_af(
        tmp_path,
        artifact,
        "--friend",
        "claude:ops",
        "--friend",
        "fake:good",
        # This test targets the dispatch-time argv-size downgrade.
        "--allow-external-tools=claude",
        env_extra={"PATH": f"{binary_dir}{os.pathsep}{_safe_path_dir()}"},
    )
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any(
        "E2BIG" in note or "Argument list too long" in note for note in meta["downgrades"]
    ), meta["downgrades"]
    assert any("claude" in note for note in meta["downgrades"])


def test_small_prompt_does_not_record_an_e2big_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nshort\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert not any("E2BIG" in note for note in meta["downgrades"]), meta["downgrades"]


# --- I1: friend stderr is captured and persisted, not thrown away ---------
#
# SpawnResult.stderr was populated by spawn.run_process but referenced
# nowhere in cmd_run: an unauthenticated friend showed up as "failed: exit
# 1" with a 0-byte .raw and no diagnosis anywhere, while troubleshooting.md
# sent the operator to `afriend doctor`, which only calls shutil.which and
# never probes auth. fake:crash (fake_friend.py) prints "boom" to stderr
# and exits 1 -- a real, if minimal, stand-in for that
# unauthenticated-friend shape.


def test_failed_friends_stderr_is_written_to_its_own_err_file(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:crash", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    err_text = (runs[0] / "round-1" / "fake-crash-0.err").read_text()
    assert err_text.strip() == "boom"


def test_successful_friends_err_file_still_exists_and_is_empty(tmp_path):
    """A stable, always-present file beats one that only sometimes exists --
    an operator grepping round-1/*.err should never have to first check
    which friends failed before knowing which files are there to read."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    err_path = runs[0] / "round-1" / "fake-good-0.err"
    assert err_path.exists()
    assert err_path.read_text() == ""


def test_failed_friends_status_carries_a_short_stderr_tail_and_points_at_the_err_file(tmp_path):
    """The report/run.json status column must show enough to diagnose an
    unauthenticated/misconfigured friend without a second file open --
    'failed: exit 1' alone (the pre-fix behavior) gave no clue at all."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:crash", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    crash_status = next(f["status"] for f in meta["friends"] if f["name"] == "fake-crash-0")
    assert "boom" in crash_status
    assert "fake-crash-0.err" in crash_status
    report = (runs[0] / "report.md").read_text()
    assert "boom" in report
    assert "fake-crash-0.err" in report


def test_successful_friends_status_does_not_carry_a_stderr_tail(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert meta["friends"][0]["status"] == "ok"


# --- Regression 3 (whole-branch re-review): the stderr tail is untrusted
# text on a new path into the report. report._escape_cell alone neutralizes
# only `\`, `|`, and newlines (enough to keep the table structure intact),
# not inline Markdown/HTML (`**bold**`, `[text](url)`, `` `code` ``, a raw
# `<script>`/autolink) -- those still render as real emphasis, a real
# clickable link, or raw HTML once inside a cell. fake:hostile_stderr
# (fake_friend.py) prints exactly that shape to stderr and exits 1.


def test_stderr_tail_strips_inline_markdown_significant_characters():
    """Unit-level proof at the source: dispatch._stderr_tail must not
    merely cap length, it must strip the characters that make
    emphasis/links/code spans/raw HTML possible in the first place --
    report._escape_cell, applied later to the whole status string, never
    touches these."""
    hostile = (
        "auth failed: **please** [login](http://evil.example) `token` <script>alert(1)</script>"
    )
    tail = cli._stderr_tail(hostile)
    for char in "`*_[]<>":
        assert char not in tail, f"{char!r} survived stripping: {tail!r}"
    assert "auth failed" in tail and "login" in tail  # content otherwise preserved


def test_hostile_stderr_does_not_render_as_markdown_in_the_report(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:hostile_stderr", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    # Isolate exactly the stderr-tail excerpt cmd_run inserted (between
    # "(stderr: " and the following "; full text in ...") -- the rest of
    # that table row (the friend name, and the "full text in
    # round-1/<name>.err" reference cmd_run itself generates) legitimately
    # contains "_" and other characters that have nothing to do with the
    # sanitizer being tested here.
    status_line = next(ln for ln in report.splitlines() if ln.startswith("| fake-hostile_stderr"))
    tail_excerpt = status_line.split("(stderr: ", 1)[1].split("; full text in", 1)[0]
    for char in "`*_[]<>":
        assert char not in tail_excerpt, f"{char!r} leaked into: {tail_excerpt!r}"
    assert "auth failed" in tail_excerpt  # the diagnostic content still came through
    assert "the guard is missing" in report  # the second friend's finding still came through


@pytest.mark.skipif(shutil.which("cmark") is None, reason="cmark not installed on this machine")
def test_hostile_stderr_produces_no_link_or_emphasis_under_cmark(tmp_path):
    """report.md legitimately uses **bold** labels ("**Claim:**", etc.) for
    every real finding, which correctly render as <strong> -- a blanket "no
    <strong> anywhere" assertion would be wrong. What must NOT appear is
    the hostile content specifically forming a real link, real emphasis, or
    raw HTML: the "evil.example" URL as a clickable <a href>, "please" (from
    "**please**") wrapped in <strong>, or the literal <script> tag surviving
    unescaped."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:hostile_stderr", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report_text = (runs[0] / "report.md").read_text()
    html = subprocess.run(
        ["cmark"], input=report_text, capture_output=True, text=True, check=True
    ).stdout
    assert "evil.example" in html  # the diagnostic text still made it through...
    assert "<a href" not in html  # ...but never as a real, clickable link
    assert "<strong>please" not in html
    assert "<script>alert" not in html
