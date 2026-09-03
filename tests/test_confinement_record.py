"""What a run records about withheld environment variables (§12.2).

The list reaches run.json and report.md as the run's evidence that secrets
were kept from executable friends, so it has to describe what dispatch will
actually do rather than an approximation of it. A crossexam of
commands/run.py found it doing neither.
"""

import argparse
import dataclasses

import pytest

from adversarial_friends.adapters import Adapter, FriendSpec
from adversarial_friends.commands.confinement import confinement_downgrades


def _adapter(name, *, readonly=(), env_pass=()):
    return Adapter(
        name=name,
        binary=name,
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=list(readonly),
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        env_pass=tuple(env_pass),
    )


def _spec(cli):
    return FriendSpec(
        name=f"{cli}-ops-0", cli=cli, lens="ops", model=None, effort=None, scope="doc", timeout=9
    )


def _args(**kw):
    kw.setdefault("pass_env", [])
    kw.setdefault("allow_unsandboxed_friend", False)
    return argparse.Namespace(**kw)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("os.environ", {"SECRET_TOKEN": "x", "OPENAI_API_KEY": "y", "PATH": "/bin"})


def test_a_variable_the_adapter_passes_is_not_reported_as_withheld(env, monkeypatch):
    """`--pass-env` was handed to `withheld` in the ADAPTER slot, so the
    adapter's own pass list was never consulted: opencode declares six API
    keys in `pass`, dispatch hands all six to the child, and all six were
    reported withheld. The record asserted a protection that had not
    happened."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    registry = {"opencode": _adapter("opencode", env_pass=("OPENAI_API_KEY",))}
    downgrades: list[str] = []
    withheld = confinement_downgrades(_args(), [_spec("opencode")], registry, downgrades)
    assert "OPENAI_API_KEY" not in withheld, withheld
    assert "SECRET_TOKEN" in withheld


def test_no_mechanism_means_no_filesystem_confinement_but_still_a_filtered_env(env, monkeypatch):
    """With no mechanism the child's FILESYSTEM is unconfined, and the run
    says so. Its environment is filtered regardless: that is `subprocess`
    passing an explicit `env`, not something the sandbox does, and reporting
    otherwise would understate the protection as badly as the original
    defect overstated it."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: None)
    registry = {"opencode": _adapter("opencode")}
    downgrades: list[str] = []
    withheld = confinement_downgrades(_args(), [_spec("opencode")], registry, downgrades)
    assert "SECRET_TOKEN" in withheld
    assert any("is not confined" in d for d in downgrades), downgrades
    assert any("environment is still filtered" in d for d in downgrades), downgrades


def test_explicit_unsandboxed_override_records_retained_read_authority(env, monkeypatch):
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: None)
    registry = {"opencode": _adapter("opencode")}
    downgrades: list[str] = []

    confinement_downgrades(
        _args(allow_unsandboxed_friend=True), [_spec("opencode")], registry, downgrades
    )

    override = next(d for d in downgrades if d.startswith("--allow-unsandboxed-friend"))
    assert "fallback only when no OS confinement mechanism is available" in override
    assert "never disables an available bwrap or sandbox-exec" in override
    assert "no OS confinement" in override
    assert "same-user filesystem read access" in override


def test_override_with_available_confinement_records_no_unconfined_warning(env, monkeypatch):
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    registry = {"opencode": _adapter("opencode")}
    downgrades: list[str] = []

    confinement_downgrades(
        _args(allow_unsandboxed_friend=True), [_spec("opencode")], registry, downgrades
    )

    assert not any(d.startswith("--allow-unsandboxed-friend") for d in downgrades)
    assert not any("not confined" in d for d in downgrades)


def test_a_variable_only_one_adapter_receives_is_named_not_folded_in(env, monkeypatch):
    """Withheld means no executable friend got it. One that a single adapter's
    pass list lets through is reported separately rather than counted as
    kept back from everyone."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    registry = {
        "opencode": _adapter("opencode", env_pass=("OPENAI_API_KEY",)),
        "other": _adapter("other"),
    }
    downgrades: list[str] = []
    withheld = confinement_downgrades(
        _args(), [_spec("opencode"), _spec("other")], registry, downgrades
    )
    assert "OPENAI_API_KEY" not in withheld
    assert any("passed to others" in d for d in downgrades), downgrades


def test_a_self_confining_friend_still_has_its_environment_filtered(env, monkeypatch):
    """Confinement keys on the adapter having no read-only mode of its own
    (§12.2), but environment filtering does not: a read-only flag stops a
    CLI writing files and does nothing about what it reads out of its own
    environment. The two were gated on one condition, so codex, claude and
    agy inherited every exported secret while the run recorded nothing."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    registry = {"codex": _adapter("codex", readonly=("--sandbox", "read-only"))}
    downgrades: list[str] = []
    withheld = confinement_downgrades(_args(), [_spec("codex")], registry, downgrades)
    assert "SECRET_TOKEN" in withheld, withheld
    # It confines itself, so no sandbox note is due -- only the env record.
    assert not any("is not confined" in d for d in downgrades), downgrades


def test_an_http_friend_has_no_child_environment_to_filter(env, monkeypatch):
    """ollama is reached over HTTP: there is no child process, so it cannot
    be named in a record about what a child was denied."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    http = _adapter("ollama")
    registry = {"ollama": dataclasses.replace(http, transport="http")}
    downgrades: list[str] = []
    assert confinement_downgrades(_args(), [_spec("ollama")], registry, downgrades) == []
    assert downgrades == []
