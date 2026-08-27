"""What a run records about withheld environment variables (§12.2).

The list reaches run.json and report.md as the run's evidence that secrets
were kept from confined friends, so it has to describe what dispatch will
actually do rather than an approximation of it. A crossexam of
commands/run.py found it doing neither.
"""

import argparse

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


def test_nothing_is_reported_withheld_when_nothing_can_confine(env, monkeypatch):
    """With no mechanism, dispatch passes `env=None` and the child inherits
    everything. A withheld list here would tell a reader auditing the run
    that secrets were filtered when nothing filtered them."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: None)
    registry = {"opencode": _adapter("opencode")}
    downgrades: list[str] = []
    withheld = confinement_downgrades(_args(), [_spec("opencode")], registry, downgrades)
    assert withheld == []
    assert any("was NOT filtered" in d for d in downgrades), downgrades


def test_a_variable_only_one_adapter_receives_is_named_not_folded_in(env, monkeypatch):
    """Withheld means no confined friend got it. One that a single adapter's
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


def test_a_friend_with_its_own_readonly_mode_is_not_counted_as_confined(env, monkeypatch):
    """Confinement keys on the adapter having no read-only mode of its own
    (§12.2), so a CLI that does is not part of this record at all."""
    monkeypatch.setattr("adversarial_friends.sandbox.detect", lambda *a, **k: "sandbox-exec")
    registry = {"codex": _adapter("codex", readonly=("--sandbox", "read-only"))}
    downgrades: list[str] = []
    assert confinement_downgrades(_args(), [_spec("codex")], registry, downgrades) == []
    assert downgrades == []
