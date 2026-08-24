"""Tests for effort presets (spec §10, §10.1, §18.8)."""

import pytest

from adversarial_friends import presets
from adversarial_friends.adapters import load_adapters
from adversarial_friends.paths import ADAPTER_DIR


@pytest.fixture
def registry():
    return load_adapters(ADAPTER_DIR)


def test_inherit_emits_nothing(registry):
    """The default, and the whole "inherit, don't override" policy: each CLI
    carries an effort its owner chose, and overriding silently produces
    surprise behaviour and surprise cost."""
    for adapter in registry.values():
        assert presets.effort_for(presets.INHERIT, adapter) is None


def test_thorough_takes_the_best_each_adapter_actually_has(registry):
    """ "Maximum *available* effort per friend" -- uneven by construction, and
    that unevenness is why the report states what each friend received."""
    assert presets.effort_for(presets.THOROUGH, registry["claude"]) == "max"
    assert presets.effort_for(presets.THOROUGH, registry["codex"]) == "xhigh"
    # agy tops out at high; naming a fixed key would refuse to run instead.
    assert presets.effort_for(presets.THOROUGH, registry["agy"]) == "high"


def test_cheap_takes_the_lowest(registry):
    for name in ("claude", "codex", "agy", "opencode"):
        assert presets.effort_for(presets.CHEAP, registry[name]) == "low"


def test_an_adapter_with_no_effort_levels_gets_none(registry):
    """ollama is a bare model behind an endpoint and has no such concept."""
    assert presets.effort_for(presets.THOROUGH, registry["ollama"]) is None


def test_every_selected_level_is_one_the_adapter_declares(registry):
    """A level build_argv does not recognise raises UsageError, so a preset
    that picked one would turn "run thoroughly" into "refuse to run"."""
    for preset in (presets.THOROUGH, presets.CHEAP):
        for adapter in registry.values():
            level = presets.effort_for(preset, adapter)
            assert level is None or level in adapter.effort


def test_gate_defaults_to_thorough():
    """§7's mode table. It is the mode that fails a build."""
    assert presets.default_preset("gate") == presets.THOROUGH


def test_every_other_mode_inherits():
    for mode in ("report", "crossexam", "loop"):
        assert presets.default_preset(mode) == presets.INHERIT


def test_an_unverifiable_adapter_is_called_out(registry):
    """§18.8: opencode's effort flag accepts any string silently, so a
    preset cannot promise anything for it. Reported, not raised -- the run
    should still happen, it just must not claim the preset was honoured."""
    note = presets.unverifiable_note(presets.THOROUGH, registry["opencode"])
    assert note is not None and "unverified" in note


def test_a_verifiable_adapter_needs_no_note(registry):
    assert presets.unverifiable_note(presets.THOROUGH, registry["claude"]) is None


def test_inherit_never_produces_notes(registry):
    for adapter in registry.values():
        assert presets.unverifiable_note(presets.INHERIT, adapter) is None
        assert presets.no_effort_note(presets.INHERIT, adapter) is None


def test_an_adapter_with_no_effort_says_so(registry):
    note = presets.no_effort_note(presets.CHEAP, registry["ollama"])
    assert note is not None and "no effort levels" in note
