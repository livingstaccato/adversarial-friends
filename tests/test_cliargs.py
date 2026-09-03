"""Tests for --friend parsing (cliargs._specs_from_flags)."""

from pathlib import Path

import pytest

from adversarial_friends import adapters, cliargs
from adversarial_friends.commands.runmeta import _RESUMABLE_ARGS
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


def test_pass_env_help_names_every_exec_friend_not_only_os_confined_friends():
    parser = cliargs.build_parser()
    subcommands = next(
        action
        for action in parser._actions
        if isinstance(action, cliargs.argparse._SubParsersAction)
    )
    help_text = subcommands.choices["run"].format_help()

    assert "every executable friend process" in help_text
    assert "confined friends" not in help_text


def test_unsandboxed_friend_help_names_lost_confinement_and_read_authority():
    parser = cliargs.build_parser()
    subcommands = next(
        action
        for action in parser._actions
        if isinstance(action, cliargs.argparse._SubParsersAction)
    )
    help_text = " ".join(subcommands.choices["run"].format_help().split())

    assert "OS confinement" in help_text
    assert "same-user filesystem read access" in help_text
    assert "only when no OS confinement mechanism is available" in help_text
    assert "never disables an available bwrap or sandbox-exec" in help_text


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


def test_provider_selection_overrides_are_repeatable():
    args = cliargs.build_parser().parse_args(
        [
            "run",
            "spec.md",
            "--enable-provider",
            "ollama",
            "--enable-provider",
            "codex",
            "--disable-provider",
            "claude",
        ]
    )
    assert args.enable_provider == ["ollama", "codex"]
    assert args.disable_provider == ["claude"]


def test_profile_and_explicit_mode_are_parsed_separately():
    parser = cliargs.build_parser()

    defaulted = parser.parse_args(["run", "spec.md", "--profile", "balanced"])
    explicit = parser.parse_args(["run", "spec.md", "--profile", "balanced", "--mode", "gate"])

    assert defaulted.profile == "balanced"
    assert defaulted.mode == "report"
    assert defaulted._mode_explicit is False
    assert explicit.profile == "balanced"
    assert explicit.mode == "gate"
    assert explicit._mode_explicit is True


def test_host_provider_parses_and_is_resumable():
    args = cliargs.build_parser().parse_args(["run", "spec.md", "--host-provider", "wrapper-agent"])
    assert args.host_provider == "wrapper-agent"
    assert "host_provider" in _RESUMABLE_ARGS


def test_failure_summary_defaults_to_terminal_and_accepts_report_only():
    parser = cliargs.build_parser()

    assert parser.parse_args(["run", "spec.md"]).failure_summary == "terminal"
    assert (
        parser.parse_args(["run", "spec.md", "--failure-summary", "report-only"]).failure_summary
        == "report-only"
    )


def test_failure_summary_rejects_unknown_policy():
    with pytest.raises(SystemExit) as raised:
        cliargs.build_parser().parse_args(["run", "spec.md", "--failure-summary", "quiet"])

    assert raised.value.code == 2
