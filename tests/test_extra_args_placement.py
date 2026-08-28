"""Operator flags must land in a flag position, not after the prompt (c-0005).

Dispatch appended `--unsafe-extra-args` to the end of argv, which is a flag
position only when the prompt never enters argv at all. For the other two
prompt modes the prompt is at or near the end, so appended flags became stray
positionals -- silently doing nothing, or displacing the prompt from where
the CLI reads it. That is the exact trap `build_argv`'s docstring documents,
in the one place that bypasses `build_argv`.

Raised as a deadlocked claim: judges split because neither side ran it. The
argv settles it, which is why this file exists.
"""

from pathlib import Path
import tempfile

import pytest

from adversarial_friends.adapters import (
    FriendSpec,
    build_argv,
    load_adapters,
    place_extra_args,
)
from adversarial_friends.paths import ADAPTER_DIR

EXTRA = ["-c", "model_reasoning_effort=high"]


@pytest.fixture
def files():
    directory = Path(tempfile.mkdtemp())
    prompt = directory / "p"
    prompt.write_text("REVIEW THIS")
    schema = directory / "s"
    schema.write_text("{}")
    return prompt, schema


def _argv_for(name: str, files):
    registry = load_adapters(ADAPTER_DIR)
    adapter = registry[name]
    spec = FriendSpec(
        name=name, cli=name, lens="ops", model=None, effort=None, scope="repo", timeout=60
    )
    argv, _stdin, _cap = build_argv(adapter, spec, files[0], files[1])
    return adapter, argv


def test_a_trailing_arg_adapter_keeps_the_prompt_last(files):
    """claude. The prompt IS the last element, so appending displaced it."""
    adapter, argv = _argv_for("claude", files)
    placed = place_extra_args(argv, adapter, EXTRA)
    assert placed[-1] == "REVIEW THIS"
    assert placed[-3:-1] == EXTRA


def test_a_flag_value_adapter_gets_them_before_the_prompt_flag(files):
    """agy. The prompt is the VALUE of --print, so anything after it is a
    positional rather than an option."""
    adapter, argv = _argv_for("agy", files)
    placed = place_extra_args(argv, adapter, EXTRA)
    assert placed.index(EXTRA[0]) < placed.index(adapter.prompt_flag)
    assert placed[-1] == "REVIEW THIS"


def test_a_stdin_adapter_is_unchanged(files):
    """codex. The prompt never enters argv, so the end really is a flag
    position and nothing needs to move."""
    adapter, argv = _argv_for("codex", files)
    assert place_extra_args(argv, adapter, EXTRA) == [*argv, *EXTRA]


def test_no_extra_args_leaves_argv_identical(files):
    adapter, argv = _argv_for("agy", files)
    assert place_extra_args(argv, adapter, []) == argv


def test_the_prompt_text_is_never_duplicated_or_dropped(files):
    """The failure this would show up as: a prompt appearing twice, or not at
    all, because it was moved rather than kept."""
    for name in ("claude", "agy", "codex"):
        adapter, argv = _argv_for(name, files)
        placed = place_extra_args(argv, adapter, EXTRA)
        assert placed.count("REVIEW THIS") == argv.count("REVIEW THIS")
        assert len(placed) == len(argv) + len(EXTRA)
