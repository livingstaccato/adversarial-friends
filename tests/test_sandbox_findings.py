"""Sandbox findings from running this tool on its own source.

Split from test_sandbox.py for the line cap. These are regressions for
defects the tool found when pointed at `sandbox.py` with a real roster --
including one this fix itself introduced and the containment tests caught
within a minute.
"""

from pathlib import Path

from adversarial_friends import sandbox


def test_the_install_root_is_readable_when_the_binary_is_under_bin(tmp_path):
    """agy's finding, and it was right: "a binary's runtime dependencies are
    located in the same directory as the executable itself" is an assumption,
    and it is false for every package-manager layout.

    opencode keeps a 61MB node_modules/ beside bin/. Granting only bin/ left
    the one adapter this sandbox actually applies to unable to read itself.
    """
    root = tmp_path / "app"
    (root / "bin").mkdir(parents=True)
    (root / "node_modules").mkdir()
    exe = root / "bin" / "tool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    policy = sandbox.policy_for(tmp_path / "iso", str(exe), ())
    assert root in policy.read_paths, "the install root beside bin/ must be readable"


def test_the_unresolved_path_directory_is_readable(tmp_path):
    """codex's `which` returns a symlink; the process executes THAT path, and
    resolving it requires reading the directory it lives in."""
    real_root = tmp_path / "real"
    (real_root / "bin").mkdir(parents=True)
    target = real_root / "bin" / "tool"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)

    link_dir = tmp_path / "shim"
    link_dir.mkdir()
    (link_dir / "tool").symlink_to(target)

    policy = sandbox.policy_for(tmp_path / "iso", str(link_dir / "tool"), ())
    assert link_dir in policy.read_paths, "the symlink's own directory must be readable"
    assert target.parent in policy.read_paths, "the real executable's directory too"


def test_a_general_purpose_bin_does_not_grant_its_parent(tmp_path):
    """The correction to the fix above. `~/.local/bin` is not an install
    root; treating its parent as one would hand the whole of `~/.local` --
    every application's data -- to a friend being confined."""
    local = tmp_path / ".local"
    (local / "bin").mkdir(parents=True)
    (local / "share" / "app" / "versions").mkdir(parents=True)
    target = local / "share" / "app" / "versions" / "tool"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    (local / "bin" / "tool").symlink_to(target)

    policy = sandbox.policy_for(tmp_path / "iso", str(local / "bin" / "tool"), ())
    assert local not in policy.read_paths, "must not grant the whole of ~/.local"
    assert local / "bin" in policy.read_paths


def test_a_system_bin_never_grants_the_filesystem_root():
    """The bug the previous fix introduced, caught by the containment tests
    within a minute.

    `cat` lives in `/bin`, so "the install root is one level above bin/"
    granted `/` -- read access to the entire filesystem, and the sandbox
    stopped confining anything at all. This is why those tests run a real
    process instead of asserting about a profile string.
    """

    policy = sandbox.policy_for(Path("/tmp/iso"), "cat", ())
    assert Path("/") not in policy.read_paths
    for granted in policy.read_paths:
        assert str(granted) != "/", "the sandbox must never grant the filesystem root"


def test_a_confined_process_really_cannot_see_withheld_secrets(tmp_path):
    """The end-to-end proof, not an assertion about a dict.

    Runs `env` under the real sandbox with the filtered environment and
    checks the secret is absent from what the process itself reports. A unit
    test on childenv.build proves the filter computes the right set; only
    this proves the filtered set is what the child actually receives.
    """
    import os
    import shutil
    import subprocess

    mechanism = sandbox.detect()
    if mechanism is None:
        import pytest

        pytest.skip("no OS sandbox mechanism on this machine")

    from adversarial_friends import childenv

    workdir = tmp_path / "iso"
    workdir.mkdir()
    parent = {**os.environ, "AF_TEST_FAKE_SECRET": "leaked-value-should-not-appear"}
    child = childenv.build(environ=parent)

    policy = sandbox.policy_for(workdir, "env", ())
    argv = sandbox.wrap(
        [shutil.which("env") or "/usr/bin/env"], mechanism, policy, tmp_path / "p.sb"
    )
    result = subprocess.run(argv, capture_output=True, text=True, env=child)

    assert result.returncode == 0, result.stderr
    assert "leaked-value-should-not-appear" not in result.stdout
    assert "AF_TEST_FAKE_SECRET" not in result.stdout
    # And it is still a usable environment, not an empty one.
    assert "PATH=" in result.stdout
