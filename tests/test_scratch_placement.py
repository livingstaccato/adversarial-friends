"""Where a confined friend's scratch and state live (spec §12.2, §12.3).

The runner takes a snapshot of the repository and gives each repo-scope
friend its own `git worktree` of it, so every friend reviews the same
pristine tree. Pointing that friend's `$TMPDIR` and `$XDG_*` at its own
working directory undid half of that: the runner dirtied the tree it had
just gone to the trouble of isolating. A friend orienting itself with
`git status` saw two untracked directories that were not in the commit it
was reviewing, and the CLI's own config sat among the files it was asked to
critique.

Raised as a claim that deadlocked -- judges split on whether "inside the
isolation directory" meant the worktree or the tree that contains it. The
layout settles it: scratch goes BESIDE the working directory, under the
round's isolation root, which is torn down with everything else.

The bottom half runs a real sandboxed process. A grant that merely looks
right in a profile is worth nothing when the question is whether a friend
can actually write where it needs to and nowhere else.
"""

from pathlib import Path
import shutil
import subprocess

import pytest

from adversarial_friends import childenv, sandbox

_MECHANISM = sandbox.detect()
_REAL = pytest.mark.skipif(
    _MECHANISM is None,
    reason="no OS sandbox mechanism on this machine (install bubblewrap on linux)",
)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """A round's isolation root holding one friend's worktree, plus the
    private root the runner picks for it.

    The private root comes from `private_root_for`, not from a path this
    file spells out. That is the whole point: the defect was not in
    `private_dirs`, which has always written under whatever root it was
    handed -- it was in the caller handing it the working directory. A test
    that named the sibling itself would pass against the broken code.
    """
    iso_root = tmp_path / "af-isolation-r1"
    workdir = iso_root / "codex-security-0"
    workdir.mkdir(parents=True)
    return workdir, childenv.private_root_for(workdir)


def test_scratch_does_not_land_in_the_working_directory(tmp_path):
    """The defect itself. A repo-scope friend's working directory IS the
    worktree under review, so anything created there is a file the friend
    did not expect and the snapshot did not contain."""
    workdir, private_root = _layout(tmp_path)
    childenv.private_dirs(private_root)
    assert list(workdir.iterdir()) == []


def test_every_redirected_variable_points_outside_the_working_directory(tmp_path):
    """Not just the two directories it creates -- every variable it hands
    out. One stray `$XDG_CONFIG_HOME` pointing back inside would restore the
    whole problem for whichever CLI happens to use that one."""
    workdir, private_root = _layout(tmp_path)
    env = childenv.private_dirs(private_root)
    assert env, "expected the redirected variables"
    for name, value in env.items():
        assert not Path(value).is_relative_to(workdir), f"{name} -> {value}"
        assert Path(value).is_relative_to(private_root), f"{name} -> {value}"


def test_the_private_root_is_a_sibling_not_a_parent(tmp_path):
    """It must not be the isolation root itself. That directory holds every
    other friend's worktree, and granting one friend write access to it is
    the same mistake the original `$TMPDIR` grant made -- the one this whole
    mechanism exists to undo."""
    workdir, private_root = _layout(tmp_path)
    childenv.private_dirs(private_root)
    assert private_root.parent == workdir.parent
    assert private_root != workdir.parent


def test_the_directories_are_created_not_merely_named(tmp_path):
    """A CLI that finds `$TMPDIR` missing falls back to the real one, which
    is silent and defeats the redirection entirely."""
    _workdir, private_root = _layout(tmp_path)
    env = childenv.private_dirs(private_root)
    for value in set(env.values()):
        assert Path(value).is_dir(), value


# --- Does the sandbox actually allow it? -----------------------------------


def _run_confined(workdir: Path, private_root: Path, script: str, profile: Path):
    """Run `sh -c script` under the policy dispatch builds for a confined
    friend: the workdir writable, the private root granted, nothing else."""
    env = childenv.private_dirs(private_root)
    policy = sandbox.policy_for(workdir, "sh", (), (str(private_root),))
    argv = sandbox.wrap(
        [shutil.which("sh") or "/bin/sh", "-c", script],
        _MECHANISM,
        policy,
        profile,
    )
    return subprocess.run(
        argv, capture_output=True, text=True, cwd=str(workdir), env={**env, "PATH": "/usr/bin:/bin"}
    )


@_REAL
def test_a_confined_friend_can_write_to_its_redirected_tmpdir(tmp_path):
    """The half that must keep working. Moving scratch out of the worktree
    is only a fix if the friend can still use it -- opencode writes a log on
    every run and fails without one."""
    workdir, private_root = _layout(tmp_path)
    result = _run_confined(
        workdir, private_root, 'echo scratch > "$TMPDIR/probe"', tmp_path / "p.sb"
    )
    assert result.returncode == 0, result.stderr
    assert (private_root / "tmp" / "probe").read_text().strip() == "scratch"


@_REAL
def test_the_reviewed_worktree_is_left_clean(tmp_path):
    """The claim, run rather than asserted about: after a friend has used
    its scratch space, the tree it was reviewing contains nothing new."""
    workdir, private_root = _layout(tmp_path)
    (workdir / "artifact.md").write_text("under review\n")
    before = sorted(p.name for p in workdir.iterdir())
    result = _run_confined(
        workdir,
        private_root,
        'echo a > "$TMPDIR/a"; echo b > "$XDG_STATE_HOME/b"',
        tmp_path / "p.sb",
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in workdir.iterdir()) == before


def test_dispatch_redirects_scratch_outside_the_working_directory(tmp_path):
    """The call site, not the helper.

    `private_dirs` was never wrong -- it wrote under whatever root it was
    handed. `_dispatch` handed it `cwd`. So this drives the real dispatch
    path with a confinable adapter and reads back the environment the child
    would have received.
    """
    from adversarial_friends import adapters, dispatch

    workdir = tmp_path / "iso" / "unconfinable-ops-0"
    workdir.mkdir(parents=True)
    prompt = workdir.parent / "p.prompt"
    prompt.write_text("hi")

    seen: dict[str, str] = {}
    real = childenv.private_dirs

    def _record(root: Path) -> dict[str, str]:
        seen["root"] = str(root)
        return real(root)

    # Hand-built rather than taken from the registry, for the reason
    # test_sandbox.py gives: the only unconfinable shipped adapter is
    # opencode, and whether it is installed differs between a developer
    # machine and CI.
    adapter = adapters.Adapter(
        name="unconfinable",
        binary="true",
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        external_tools="none",
        external_tool_sources=("test executable",),
    )
    spec = adapters.FriendSpec(
        name="unconfinable-ops-0",
        cli="unconfinable",
        lens="ops",
        model=None,
        effort=None,
        scope="doc",
        timeout=5,
    )
    original = dispatch.childenv.private_dirs
    dispatch.childenv.private_dirs = _record  # type: ignore[assignment]
    try:
        dispatch._dispatch(
            spec,
            workdir,
            {"unconfinable": adapter},
            None,
            prompt,
            tmp_path / "s.json",
            allow_unsandboxed=True,
        )
    finally:
        dispatch.childenv.private_dirs = original  # type: ignore[assignment]

    if not seen:
        pytest.skip("no sandbox mechanism, so dispatch never reaches the redirect")
    assert not Path(seen["root"]).is_relative_to(workdir)
    assert Path(seen["root"]).parent == workdir.parent


@_REAL
def test_the_grant_does_not_reach_another_friend_isolation_tree(tmp_path):
    """The reason the grant names one directory instead of its parent. A
    sibling worktree in the same round belongs to a different friend, and
    one friend writing into another's tree would corrupt a review without
    leaving a trace anyone would look for."""
    workdir, private_root = _layout(tmp_path)
    neighbour = workdir.parent / "claude-scope-0"
    neighbour.mkdir()
    result = _run_confined(
        workdir, private_root, f'echo intruder > "{neighbour}/planted"', tmp_path / "p.sb"
    )
    assert result.returncode != 0
    assert not (neighbour / "planted").exists()
