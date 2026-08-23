import pytest

from adversarial_friends import adapters
from adversarial_friends.errors import UsageError

ADAPTER_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "skills" / "adversarial-friends" / "adapters"


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def spec(**over):
    base = dict(name="f1", cli="codex", lens="ops", model=None, effort=None,
                scope="repo", timeout=900)
    base.update(over)
    return adapters.FriendSpec(**base)


@pytest.fixture
def files(tmp_path):
    """build_argv reads the prompt off disk, so it must actually exist."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("CHALLENGE THIS ARTIFACT")
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    return prompt, schema


def test_all_shipped_adapters_load(registry):
    assert set(registry) >= {"claude", "codex", "agy", "opencode", "ollama"}


def test_agy_prompt_is_the_last_argument(registry, files):
    """agy's -p takes the prompt as its value; anything after it is ignored."""
    prompt, schema = files
    argv, stdin = adapters.build_argv(
        registry["agy"], spec(cli="agy", effort="high"),
        prompt_file=prompt, schema_file=schema,
    )
    assert argv[-2] == "-p"
    assert argv[-1] == "CHALLENGE THIS ARTIFACT"
    assert "--mode" in argv and argv.index("--mode") < argv.index("-p")
    assert stdin is None


def test_codex_takes_prompt_on_stdin(registry, files):
    prompt, schema = files
    argv, stdin = adapters.build_argv(
        registry["codex"], spec(), prompt_file=prompt, schema_file=schema,
    )
    assert "exec" in argv
    assert stdin is not None


def test_readonly_flags_are_emitted_for_repo_scope(registry, files):
    prompt, schema = files
    argv, _ = adapters.build_argv(
        registry["claude"], spec(cli="claude"), prompt_file=prompt,
        schema_file=schema,
    )
    assert "--tools" in argv
    assert "Read,Grep,Glob" in argv


def test_capability_is_derived_from_argv_not_defaults(registry):
    cap = adapters.capability_for(registry["claude"],
                                  ["claude", "-p", "--output-format", "json"])
    assert cap.readonly is False  # no --tools in this argv
    cap2 = adapters.capability_for(registry["claude"],
                                   ["claude", "--tools", "Read,Grep,Glob"])
    assert cap2.readonly is True


def test_opencode_effort_is_unverified(registry):
    cap = adapters.capability_for(registry["opencode"],
                                  ["opencode", "run", "--variant", "high"])
    assert cap.effort == "unverified"


def test_unsupported_effort_level_raises(registry, files):
    prompt, schema = files
    with pytest.raises(UsageError):
        adapters.build_argv(
            registry["agy"], spec(cli="agy", effort="xhigh"),
            prompt_file=prompt, schema_file=schema,
        )
