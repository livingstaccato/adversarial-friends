"""OS confinement for a CLI that already has a read-only mode (§12.2).

A read-only flag stops a friend WRITING and says nothing about what it may
READ. Measured against the real codex on 2026-08-28: under its own
`--sandbox read-only` and nothing else, asked to list `~/.ssh`, it listed the
directory. Under this runner's sandbox the same request came back "the
filesystem sandbox denied access (Operation not permitted)".

The opt-in is per adapter rather than blanket because confinement breaks a
CLI whose credentials the sandbox cannot reach: claude keeps its own in the
macOS Keychain and reports "Not logged in" under any profile that does not
grant `~/Library/Keychains` -- and granting that would hand a friend every
credential the operator has, which is worse than the gap it closes. agy is
left out deliberately; provoking its re-authentication has cost a login before.
"""

from pathlib import Path

from adversarial_friends.adapters import load_adapters
from adversarial_friends.paths import ADAPTER_DIR


def _registry():
    return load_adapters(ADAPTER_DIR)


def test_codex_opts_in_and_declares_what_it_needs():
    codex = _registry()["codex"]
    assert codex.sandbox_confine is True
    # It writes session state and sqlite files under CODEX_HOME on every run
    # and exits if it cannot, so read alone is not enough.
    assert any("codex" in p for p in codex.sandbox_read)
    assert any("codex" in p for p in codex.sandbox_write)


def test_claude_does_not_opt_in():
    """Not an oversight. Confining claude requires granting the Keychain,
    which is a worse outcome than the gap -- see this module's docstring."""
    assert _registry()["claude"].sandbox_confine is False


def test_agy_does_not_opt_in():
    assert _registry()["agy"].sandbox_confine is False


def test_opting_in_is_off_by_default_for_a_new_adapter():
    """An adapter that says nothing about confinement must not be silently
    confined: the whole point of the opt-in is that someone verified that
    CLI actually runs under a sandbox."""
    import tomllib

    from adversarial_friends.adapters import load_adapters

    directory = Path(__file__).parent / "_fresh_adapter"
    directory.mkdir(exist_ok=True)
    (directory / "brandnew.toml").write_text(
        'name = "brandnew"\nbinary = "brandnew"\nprompt_mode = "stdin"\n'
    )
    try:
        fresh = load_adapters(directory)["brandnew"]
        assert fresh.sandbox_confine is False
        assert tomllib is not None
    finally:
        (directory / "brandnew.toml").unlink()
        directory.rmdir()


def test_a_cli_with_no_readonly_mode_is_still_confined_without_opting_in():
    """opencode must keep being confined on the old grounds -- it enforces
    nothing itself -- rather than needing the new flag."""
    opencode = _registry()["opencode"]
    assert not opencode.readonly_argv
    assert opencode.sandbox_confine is False
