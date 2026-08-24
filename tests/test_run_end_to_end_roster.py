"""End-to-end `--roster`, `--preset`, and `afriend init` (spec §10.1, §13, §17).

**These assert on roster RESOLUTION, not on a friend running successfully.**
The test-only `fake` cli has no adapter in the registry on purpose -- routing
it through roster.resolve's validation would mean either fabricating an
adapter or special-casing the trust boundary, and neither belongs there. So a
roster can only name real CLIs, which are absent from these tests' safe PATH
and therefore fail to launch.

That is the right level anyway: the roster path converges with the --friend
path the moment specs exist, and everything after that is covered by every
other end-to-end file. What is unique to a roster is which friends get
resolved, with what settings, from which file -- which is what these check,
via run.json's friend table rather than via exit status.
"""

import json
import subprocess
import sys

from e2e_helpers import AF, _env, run_af

ROSTER = """
[[friend]]
name = "codex-ops"
cli = "codex"
lens = "ops"
scope = "doc"
"""


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n")
    return path


def _run_json(tmp_path):
    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    return json.loads((run_dir / "run.json").read_text())


def _roster(tmp_path, text=ROSTER):
    path = tmp_path / "roster.toml"
    path.write_text(text)
    return path


# --- --roster --------------------------------------------------------------


def test_a_roster_file_replaces_discovery(tmp_path):
    run_af(tmp_path, _artifact(tmp_path), "--roster", str(_roster(tmp_path)))
    meta = _run_json(tmp_path)
    assert [f["name"] for f in meta["friends"]] == ["codex-ops"]
    assert meta["roster_source"] == str(_roster(tmp_path))


def test_friend_flags_beat_a_roster(tmp_path):
    """§10.1's precedence, strongest last: --friend is the invocation flag
    and outranks a roster file."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--roster",
        str(_roster(tmp_path)),
        "--friend",
        "fake:cwd_probe",
    )
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert [f["name"] for f in meta["friends"]] == ["fake-cwd_probe-0"]
    assert any("--friend replaces the roster" in d for d in meta["downgrades"])


def test_a_missing_roster_is_a_usage_error(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(tmp_path / "nope.toml"))
    assert result.returncode == 2
    assert "not found" in result.stderr


def test_a_roster_naming_an_unknown_cli_is_refused(tmp_path):
    bad = _roster(tmp_path, '[[friend]]\nname = "x"\ncli = "nope"\nlens = "ops"\n')
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(bad))
    assert result.returncode != 0
    assert "nope" in result.stderr


def test_a_roster_cannot_smuggle_arbitrary_flags(tmp_path):
    """§13: a roster supplies values only, for a fixed set of keys. There is
    no mechanism for a file to inject a flag."""
    bad = _roster(
        tmp_path,
        '[[friend]]\nname = "x"\ncli = "codex"\nlens = "ops"\nextra_args = "--yolo"\n',
    )
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(bad))
    assert result.returncode == 2
    assert "extra_args" in result.stderr


def test_a_repo_local_roster_is_not_picked_up_on_its_own(tmp_path, monkeypatch):
    """§13: repo-local `.adversarial-friends/` is untrusted. A cloned repo
    must not be able to choose who reviews it."""
    hostile = tmp_path / ".adversarial-friends"
    hostile.mkdir()
    (hostile / "roster.toml").write_text(ROSTER)
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        env_extra={"XDG_CONFIG_HOME": str(tmp_path / "empty")},
    )
    assert result.returncode == 0, result.stderr
    assert _run_json(tmp_path)["roster_source"] is None


def test_the_user_config_roster_is_picked_up(tmp_path):
    """The trusted half of §13: this is the operator's own machine-wide
    configuration, and using it is the point of writing one."""
    config = tmp_path / "config" / "adversarial-friends"
    config.mkdir(parents=True)
    (config / "roster.toml").write_text(ROSTER)
    run_af(tmp_path, _artifact(tmp_path), env_extra={"XDG_CONFIG_HOME": str(tmp_path / "config")})
    meta = _run_json(tmp_path)
    assert [f["name"] for f in meta["friends"]] == ["codex-ops"]
    assert meta["roster_source"] == str(config / "roster.toml")


# --- afriend init ----------------------------------------------------------


def _init(tmp_path, *extra):
    return subprocess.run(
        [sys.executable, str(AF), "init", "--out", str(tmp_path / "roster.toml"), *extra],
        capture_output=True,
        text=True,
        env=_env(),
    )


def test_init_writes_a_roster_from_what_is_installed(tmp_path):
    """The safe PATH contains only git, so nothing is discoverable -- which
    is the honest outcome to report, not an empty file."""
    result = _init(tmp_path)
    assert result.returncode == 3
    assert "no agent CLIs found" in result.stderr


def test_init_refuses_to_clobber_without_force(tmp_path):
    """It is a file you are meant to edit by hand."""
    (tmp_path / "roster.toml").write_text("# mine\n")
    result = _init(tmp_path)
    assert result.returncode == 2
    assert "--force" in result.stderr
    assert (tmp_path / "roster.toml").read_text() == "# mine\n"


# --- --preset --------------------------------------------------------------


def test_a_preset_is_recorded_as_used(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--preset", "cheap")
    assert result.returncode == 0, result.stderr
    assert _run_json(tmp_path)["preset"] == "cheap"


def test_a_roster_effort_beats_the_preset(tmp_path):
    """§10.1: roster outranks preset. The preset fills only what nothing
    stronger set, which is what makes it weaker rather than merely
    different."""
    roster = _roster(
        tmp_path,
        '[[friend]]\nname = "codex-ops"\ncli = "codex"\nlens = "ops"\neffort = "medium"\n',
    )
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(roster), "--preset", "thorough")
    # codex is not installed under the safe PATH, so the friend fails -- but
    # the effort it was given is recorded either way.
    meta = _run_json(tmp_path)
    assert meta["friends"][0]["effort"] == "medium", result.stderr
