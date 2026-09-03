"""End-to-end tests for `afriend run --mode report`: core mechanics, CLI
argument validation, and the kill-grace timeout arithmetic (I4).

See tests/e2e_helpers.py for the safe-PATH subprocess harness this file (and
its siblings test_run_end_to_end_isolation.py and
test_run_end_to_end_lenses.py) share.
"""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import subprocess
import sys
import threading
import time
from typing import ClassVar

from e2e_helpers import AF, FAKE, _env, _git_commit, _git_repo, run_af
import pytest

from adversarial_friends import adapters, cli, dispatch
from adversarial_friends.adapters import FriendSpec
from adversarial_friends.commands import friends as friends_module, setup as run_setup_module
from adversarial_friends.paths import ADAPTER_DIR


class _OllamaStub(BaseHTTPRequestHandler):
    """A real HTTP boundary for Ollama CLI tests, without a local model."""

    captured: ClassVar[dict] = {}

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).captured = json.loads(self.rfile.read(length).decode("utf-8"))
        findings = {
            "findings": [
                {
                    "severity": "high",
                    "claim": "the guard is missing",
                    "location": "spec.md:1",
                    "evidence": "the contract has no guard",
                    "failure_scenario": "an unchecked request reaches the handler",
                    "suggested_fix": "validate the request before dispatch",
                }
            ]
        }
        payload = json.dumps({"response": json.dumps(findings), "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def stubbed_ollama(monkeypatch, tmp_path):
    """Point the real HTTP transport at an isolated Ollama-shaped server."""
    server = HTTPServer(("127.0.0.1", 0), _OllamaStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry = adapters.load_adapters(ADAPTER_DIR)
    endpoint = f"http://127.0.0.1:{server.server_port}/api/generate"
    ollama = replace(registry["ollama"], endpoint=endpoint)
    monkeypatch.setattr(run_setup_module, "load_adapters", lambda _path: {"ollama": ollama})
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _OllamaStub.captured = {}
    try:
        yield _OllamaStub
    finally:
        server.shutdown()
        server.server_close()


def test_report_run_produces_ledger_and_report(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    assert ledger, "ledger should not be empty"
    assert json.loads(ledger[0])["type"] == "claim"
    assert "# Adversarial review" in (runs[0] / "report.md").read_text()


def test_report_run_writes_ordered_safe_lifecycle_events(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [event["type"] for event in events] == [
        "run_started",
        "friend_finished",
        "round_finished",
        "run_finished",
    ]
    assert events[0]["payload"] == {
        "mode": "report",
        "profile": "quick",
        "scope": "doc",
        "status": "started",
    }
    assert events[0]["schema_version"] == 1
    assert events[0]["run_id"] == run_dir.name
    assert events[0]["timestamp"].endswith("Z")
    assert events[1]["payload"]["provider"] == "fake"
    assert events[1]["payload"]["friend"] == "fake-good-0"
    assert events[1]["payload"]["lens"] == "configured"
    assert events[-1]["payload"]["next_action"] == "inspect_report"
    serialized = (run_dir / "events.jsonl").read_text()
    assert "missing guard" not in serialized
    assert "argv" not in serialized


def test_arbitrary_roster_lens_cannot_abort_or_leak_into_lifecycle_events(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    secret_lens = "token=super-secret"
    monkeypatch.setattr(
        friends_module,
        "_specs_from_flags",
        lambda *_args: [
            FriendSpec(
                name="fake-secret-0",
                cli="fake",
                lens=secret_lens,
                model=None,
                effort=None,
                scope="doc",
                timeout=900,
            )
        ],
    )

    assert (
        cli.main(["run", str(artifact), "--out", str(tmp_path / "runs"), "--friend", "fake:good"])
        == 0
    )
    run_dir = next((tmp_path / "runs").iterdir())
    serialized = (run_dir / "events.jsonl").read_text()
    assert secret_lens not in serialized
    friend_event = next(
        json.loads(line)
        for line in serialized.splitlines()
        if json.loads(line)["type"] == "friend_finished"
    )
    assert friend_event["payload"]["lens"] == "configured"


def test_failed_friend_is_reported_not_silently_dropped(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:offtopic")
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()


def test_zero_friends_exits_3(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact)
    assert result.returncode == 3


def test_missing_artifact_exits_2(tmp_path):
    result = run_af(tmp_path, tmp_path / "nope.md", "--friend", "fake:good")
    assert result.returncode == 2


# --- Adversarial break-it attempts beyond the brief's four required tests -


def test_a_two_friend_gate_blocks_rather_than_passing(tmp_path):
    """This test used to assert `--mode gate` exited 2 as unimplemented.
    Gate now runs, and the interesting property is that it does not pass:
    an unresolved claim is not a cleared gate.

    Two friends, because §8.3 now refuses a judging mode with one (exit 3,
    see test_one_friend_is_refused_for_every_mode_that_judges) -- and a
    single-friend gate was the wrong shape for this assertion anyway: it
    blocked on claims no judge had been able to look at."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            str(artifact),
            "--mode",
            "gate",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:judge_uphold_a",
            "--friend",
            "fake:judge_uphold_b",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert result.returncode == 1
    assert "gate blocked" in result.stderr


def test_unknown_cli_in_friend_flag_exits_2_not_3(tmp_path):
    """Landmine #2 (inherited from Task 10): a config typo naming an
    unknown cli must be a usage error (exit 2), not 'no usable friends'
    (exit 3). This test exercises the --friend flag path in cliargs.py,
    which is Task 12's own code -- it never calls roster.resolve's
    overrides parameter (see e2e_helpers module docstring), so it cannot
    inherit Task 10's 'overrides=[] silently falls through to
    auto-discovery' bug either."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "no-such-cli:ops")
    assert result.returncode == 2, result.stderr
    assert "no-such-cli" in result.stderr


def test_ollama_without_a_model_is_diagnosed_while_ready_explicit_friend_runs(
    tmp_path, stubbed_ollama
):
    """ollama has no default model and its own error for an omitted one
    explains nothing, so the runner refuses before dispatch and names the
    remedy. Supersedes the old "HTTP transport is not implemented" rejection:
    the transport ships now, but a model is still required.

    Explicit intent bypasses selection policy, not readiness. Unready entries
    are diagnosed and filtered before capacity while a ready peer still runs.
    """
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = cli.main(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "ollama:ops",
            "--friend",
            "fake:good",
        ]
    )
    assert result == 0
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    assert [friend["name"] for friend in meta["friends"]] == ["fake-good-1"]
    assert any("no model is configured" in note for note in meta["downgrades"])


def test_ollama_friend_carries_the_model_from_the_third_slot(tmp_path, stubbed_ollama):
    """`cli:lens:model` is the only way to name a model from the CLI, and
    ollama is the adapter that cannot run without one."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = cli.main(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "ollama:security:qwen3:0.6b",
        ]
    )
    assert result == 0
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert meta["friends"][0]["model"] == "qwen3:0.6b"
    assert meta["friends"][0]["name"] == "ollama-security-0"
    assert stubbed_ollama.captured["model"] == "qwen3:0.6b"


def test_a_preset_reaches_run_json(tmp_path):
    """This used to assert --preset was refused as unimplemented. It now
    selects effort per §10.1, so what matters is that the preset actually
    used is recorded -- a report claiming `thorough` while running like
    `inherit` would misrepresent what happened, which was the original
    reason for refusing it."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--preset", "thorough", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["preset"] == "thorough"


def test_gate_defaults_to_the_thorough_preset(tmp_path):
    """§7's mode table: gate defaults to --preset thorough. It is the mode
    that fails a build, so spending more per friend is right there and
    nowhere else."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    # Two friends: §8.3 refuses a judging mode with one.
    run_af(tmp_path, artifact, "--friend", "fake:good_a", "--friend", "fake:good_b", mode="gate")
    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["preset"] == "thorough"


def test_preset_inherit_is_accepted(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--preset", "inherit", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr


def test_artifact_outside_git_repo_downgrades_every_friend_to_doc_scope(tmp_path):
    """The artifact lives directly under tmp_path, which is never a git
    repository in these tests -- this is already exercised implicitly by
    every test above, but this test asserts the required, user-visible
    consequence: the report header says so."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "not inside a git repository" in report.lower()
    assert "doc scope" in report.lower()


def test_artifact_outside_git_repo_prints_a_scope_warning_to_stderr_by_default(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    assert "warning: doc scope only" in result.stderr
    assert "no repository was detected" in result.stderr
    assert "explicitly repo-scope" not in result.stderr


def test_artifact_outside_git_repo_scope_warning_prints_with_no_progress(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--no-progress")
    assert result.returncode == 0, result.stderr
    assert "warning: doc scope only" in result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "events.jsonl").is_file()


def test_artifact_in_a_nested_subdirectory_of_a_repo_resolves_the_real_root(tmp_path):
    """isolation.snapshot_commit requires a repository ROOT and raises for
    a nested subdirectory. cmd_run must resolve the real root itself
    (via the artifact's enclosing git repo) rather than handing
    snapshot_commit the artifact's own (nested) directory."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    nested = repo / "docs" / "specs"
    nested.mkdir(parents=True)
    artifact = nested / "spec.md"
    artifact.write_text("# spec nested three levels deep\n")

    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "not inside a git repository" not in report.lower()


def test_a_slow_friend_timing_out_does_not_prevent_others_from_being_reported(
    monkeypatch, tmp_path
):
    """One friend hangs past the timeout; a second succeeds. The run must
    still exit 0 (at least one friend produced a usable result) and the
    report must show both outcomes -- the timeout must not silently drop
    either friend's row.

    Run in-process (not via the `run_af` subprocess helper every other test
    in this file uses) specifically so dispatch.KILL_GRACE_S can be
    monkeypatched down: I4 (spec 11.3) makes the real kill deadline
    `--timeout + KILL_GRACE_S` (60s in production), so a subprocess run
    with `--timeout 2` would now take 62+ real seconds to observe the same
    timeout behavior this test only needs to confirm the MECHANISM for, not
    the exact production grace window (asserted separately, cheaply, in
    test_kill_grace_period_constant_is_sixty_seconds below)."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setattr(dispatch, "KILL_GRACE_S", 1)
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    parser = cli.build_parser()
    parsed = parser.parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--timeout",
            "2",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:good",
            "--friend",
            "fake:hang",
        ]
    )
    returncode = cli.cmd_run(parsed)
    assert returncode == 0
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "timeout" in report.lower() or "failed" in report.lower()
    assert "# c-0001" in report or "c-0001" in report


# --- I4: the runner's kill deadline must be strictly greater than --timeout
#
# The kill deadline previously equaled the CLI's internal timeout exactly
# (spec 11.3 requires strictly greater), so a friend with its own internal
# timeout (agy --print-timeout) could be killed by the runner at the exact
# instant it was trying to report its own timeout cleanly, mid-write.


def test_kill_grace_period_constant_is_sixty_seconds():
    assert cli.KILL_GRACE_S == 60


def test_kill_deadline_is_strictly_greater_than_the_configured_timeout(monkeypatch, tmp_path):
    """Direct proof of the arithmetic (not just "eventually times out"):
    with KILL_GRACE_S monkeypatched down to 1s (see the test above this
    section for why), a friend that hangs must survive strictly past
    --timeout alone (1s) and be killed only once timeout + KILL_GRACE_S
    (2s) has elapsed."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setattr(dispatch, "KILL_GRACE_S", 1)
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    parser = cli.build_parser()
    parsed = parser.parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--timeout",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:hang",
        ]
    )
    started = time.monotonic()
    returncode = cli.cmd_run(parsed)
    elapsed = time.monotonic() - started
    assert returncode == 1  # the only dispatched friend timed out
    assert elapsed >= 2.0, (
        f"killed after {elapsed:.2f}s -- expected >= timeout(1) + KILL_GRACE_S(1) == 2s"
    )


def test_all_friends_failing_exits_1_and_says_so(tmp_path):
    """Every dispatched friend fails (offtopic output both times): the run
    mechanism itself still completes and writes a report, but nothing
    usable came back. Distinct from test_zero_friends_exits_3 (which never
    even resolves any friends to run) -- here two friends actually run.
    Exit 1 ('gate blocked or incomplete') is used to distinguish this from
    exit 0's implicit claim that at least one friend's verdict is
    trustworthy."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:offtopic", "--friend", "fake:offtopic")
    assert result.returncode == 1, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()


def test_a_run_directory_that_already_exists_fails_cleanly(tmp_path):
    """Simulates a run-id collision by pre-creating the directory the CLI
    would otherwise pick; since afriend generates its own run id internally
    the only reachable way to force this from outside is --out pointing at
    a path that is itself already occupied, so this drives
    runstore.RunStore directly instead -- see
    test_runstore.test_reusing_a_run_id_fails_cleanly_instead_of_mixing_ledgers
    for the unit-level check. This test instead confirms --out pointing at
    an existing plain FILE (not a directory) fails cleanly (a handled
    UsageError, exit 2) rather than crashing with a raw, unhandled
    NotADirectoryError traceback -- reproduced directly against a first
    version of RunStore.__init__ that called mkdir(parents=True) with no
    try/except around it."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    out_path = tmp_path / "runs"
    out_path.write_text("not a directory")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 2, result.stderr
    assert "afriend:" in result.stderr
    assert "Traceback" not in result.stderr


def test_two_friends_with_identical_cli_and_lens_get_distinct_names(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    names = [f["name"] for f in meta["friends"]]
    assert len(names) == len(set(names)) == 2


def test_report_mode_allows_the_same_friend_twice(tmp_path):
    """A judging mode refuses two friends that share one ledger identity --
    one identity casting two verdicts breaks quorum, and flag order decides
    which survives. `report` has no judging, and asking the same friend
    twice there is a legitimate way to sample its variance, so the guard
    exempts it."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr


# --- §8.3 degraded single-friend mode --------------------------------------


def test_one_friend_is_refused_for_every_mode_that_judges(tmp_path):
    """§8.3: "cross-examination with one participant is a different and
    weaker thing wearing the same name", so it hard-errors (exit 3).

    It used to append a downgrade and run. With one friend no judge is
    independent of any claim, so a `gate` run settles nothing, blocks on
    nothing, and exits 0 -- CI reads "gate clear" from a run that could not
    check anything. Found by a crossexam of cmd_run, which also noticed the
    DEGRADED_MODES constant wired to nothing."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    for mode in ("crossexam", "gate", "loop"):
        result = run_af(tmp_path, artifact, "--friend", "fake:good", mode=mode)
        assert result.returncode == 3, (mode, result.returncode, result.stderr)
        assert "at least two independent friends" in result.stderr, result.stderr


def test_one_friend_still_runs_a_report_and_says_what_it_is(tmp_path):
    """The one mode §8.3 allows: it runs, exits 0, and the reduced guarantee
    is in the artifact a human reads."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    meta = json.loads((sorted((tmp_path / "runs").iterdir())[0] / "run.json").read_text())
    assert any("only one friend" in d for d in meta["downgrades"]), meta["downgrades"]
