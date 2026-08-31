"""Tests for --friend parsing (cliargs._specs_from_flags)."""

from pathlib import Path

import pytest

from adversarial_friends import adapters, cliargs
from adversarial_friends.errors import UsageError

ADAPTER_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "adversarial_friends" / "assets" / "adapters"
)


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def test_two_part_friend_leaves_the_model_unset(registry):
    """cli:lens keeps working unchanged -- the model slot is optional."""
    specs = cliargs._specs_from_flags(["codex:ops"], 900, registry, fake_enabled=False)
    assert specs[0].cli == "codex"
    assert specs[0].lens == "ops"
    assert specs[0].model is None


def test_third_slot_sets_the_model(registry):
    specs = cliargs._specs_from_flags(["codex:ops:gpt-5.6-sol"], 900, registry, fake_enabled=False)
    assert specs[0].lens == "ops"
    assert specs[0].model == "gpt-5.6-sol"


def test_model_may_contain_colons(registry):
    """ollama tags are `name:tag`, so the model slot has to survive the
    partition that split cli and lens off the front."""
    specs = cliargs._specs_from_flags(
        ["ollama:security:qwen3:0.6b"], 900, registry, fake_enabled=False
    )
    assert specs[0].cli == "ollama"
    assert specs[0].lens == "security"
    assert specs[0].model == "qwen3:0.6b"


def test_friend_name_excludes_the_model(registry):
    """Friend names become path components under the run directory (ids.py),
    and a model tag can contain characters that have no business in one."""
    specs = cliargs._specs_from_flags(
        ["ollama:security:qwen3:0.6b"], 900, registry, fake_enabled=False
    )
    assert specs[0].name == "ollama-security-0"


def test_invalid_model_is_rejected(registry):
    """The model reaches argv through the adapter's model_flag, so it gets
    the same validation a roster entry does rather than a weaker one."""
    with pytest.raises(UsageError, match="invalid model"):
        cliargs._specs_from_flags(
            ["codex:ops:--dangerously-skip-permissions"], 900, registry, fake_enabled=False
        )


def test_http_adapter_is_always_doc_scope(registry):
    """A bare model behind an endpoint has no filesystem access to
    constrain, so repo scope would claim an enforcement that never
    happened."""
    specs = cliargs._specs_from_flags(
        ["ollama:security:qwen3:0.6b"], 900, registry, fake_enabled=False
    )
    assert specs[0].scope == "doc"


def test_http_adapter_is_no_longer_rejected(registry):
    """It used to raise "HTTP transport ... not implemented in this build"."""
    specs = cliargs._specs_from_flags(
        ["ollama:security:qwen3:0.6b"], 900, registry, fake_enabled=False
    )
    assert specs[0].cli == "ollama"


def test_fake_scope_suffix_still_wins_over_model_parsing(registry):
    """`fake:<mode>:repo` predates the model slot and is handled in its own
    branch, so the third slot keeps meaning scope there and never leaks into
    the model field."""
    specs = cliargs._specs_from_flags(["fake:cwd_probe:repo"], 900, registry, fake_enabled=True)
    assert specs[0].scope == "repo"
    assert specs[0].model is None


@pytest.mark.parametrize(
    ("argv", "action", "name", "model", "json_output"),
    [
        (["providers", "list"], "list", None, None, False),
        (["providers", "list", "--json"], "list", None, None, True),
        (["providers", "enable", "codex"], "enable", "codex", None, False),
        (["providers", "disable", "ollama"], "disable", "ollama", None, False),
        (
            ["providers", "set-model", "ollama", "qwen3:0.6b"],
            "set-model",
            "ollama",
            "qwen3:0.6b",
            False,
        ),
        (["providers", "clear-model", "codex"], "clear-model", "codex", None, False),
    ],
)
def test_provider_subcommands_parse_exact_forms(argv, action, name, model, json_output):
    args = cliargs.build_parser().parse_args(argv)
    assert args.command == "providers"
    assert args.provider_command == action
    assert getattr(args, "name", None) == name
    assert getattr(args, "model", None) == model
    assert getattr(args, "json", False) is json_output
