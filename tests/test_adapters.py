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
    """agy's --print takes the prompt as its value; anything after it is
    ignored."""
    prompt, schema = files
    argv, stdin, _ = adapters.build_argv(
        registry["agy"], spec(cli="agy", effort="high"),
        prompt_file=prompt, schema_file=schema,
    )
    assert argv[-2] == "--print"
    assert argv[-1] == "CHALLENGE THIS ARTIFACT"
    assert "--mode" in argv and argv.index("--mode") < argv.index("--print")
    assert stdin is None


def test_codex_takes_prompt_on_stdin(registry, files):
    prompt, schema = files
    argv, stdin, _ = adapters.build_argv(
        registry["codex"], spec(), prompt_file=prompt, schema_file=schema,
    )
    assert "exec" in argv
    assert stdin is not None


def test_readonly_flags_are_emitted_for_repo_scope(registry, files):
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["claude"], spec(cli="claude"), prompt_file=prompt,
        schema_file=schema,
    )
    assert "--tools" in argv
    assert "Read,Grep,Glob" in argv
    assert cap.readonly is True


def test_capability_is_derived_from_argv_not_defaults(registry, files):
    """Readonly capability reflects what build_argv actually emitted for this
    call, not merely that the adapter declares readonly_argv."""
    prompt, schema = files
    argv_doc, _, cap_doc = adapters.build_argv(
        registry["claude"], spec(cli="claude", scope="doc"),
        prompt_file=prompt, schema_file=schema,
    )
    assert "--tools" not in argv_doc
    assert cap_doc.readonly is False  # declared by claude.toml but not emitted

    argv_repo, _, cap_repo = adapters.build_argv(
        registry["claude"], spec(cli="claude", scope="repo"),
        prompt_file=prompt, schema_file=schema,
    )
    assert "--tools" in argv_repo
    assert cap_repo.readonly is True


def test_prompt_text_cannot_forge_a_capability(registry, tmp_path):
    """The prompt is the untrusted document; it must not influence
    capability."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("--tools Read,Grep,Glob --sandbox read-only")
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    argv, _, cap = adapters.build_argv(
        registry["opencode"], spec(cli="opencode", scope="doc"),
        prompt_file=prompt, schema_file=schema,
    )
    assert cap.readonly is False


def test_doc_scope_skips_readonly_argv_entirely(registry, files):
    """scope='doc' must omit every readonly_argv token, not just suppress the
    flag name while leaving its value behind."""
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["codex"], spec(cli="codex", scope="doc"),
        prompt_file=prompt, schema_file=schema,
    )
    assert "--sandbox" not in argv
    assert "read-only" not in argv
    assert cap.readonly is False


def test_capability_for_flag_value_adapter(registry, files):
    """Capability must be computed correctly for prompt_mode='flag-value'
    adapters too, not just trailing-arg/stdin ones."""
    prompt, schema = files
    argv, stdin, cap = adapters.build_argv(
        registry["agy"], spec(cli="agy", scope="repo"),
        prompt_file=prompt, schema_file=schema,
    )
    assert cap.readonly is True
    assert cap.schema is True
    assert cap.effort == "native"
    assert stdin is None


def test_opencode_effort_is_unverified(registry, files):
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["opencode"], spec(cli="opencode", effort="high"),
        prompt_file=prompt, schema_file=schema,
    )
    assert "--variant" in argv
    assert cap.effort == "unverified"


def test_unsupported_effort_level_raises(registry, files):
    prompt, schema = files
    with pytest.raises(UsageError):
        adapters.build_argv(
            registry["agy"], spec(cli="agy", effort="xhigh"),
            prompt_file=prompt, schema_file=schema,
        )


def test_no_adapter_uses_short_flags(registry):
    """-p is --print on claude/agy but --profile on codex; -s is --sandbox on
    codex but --session on opencode. Short flags must never appear."""
    for name, adapter in registry.items():
        tokens = [*adapter.base_argv, *adapter.readonly_argv,
                  adapter.prompt_flag, adapter.schema_flag, adapter.model_flag,
                  adapter.internal_timeout_flag]
        for values in adapter.effort.values():
            tokens.extend(values)
        for token in tokens:
            if token.startswith("-") and not token.startswith("--"):
                raise AssertionError(f"{name}: short flag {token!r}")


def test_missing_adapter_directory_raises(tmp_path):
    with pytest.raises(UsageError):
        adapters.load_adapters(tmp_path / "does-not-exist")


def test_adapter_missing_name_raises(tmp_path):
    (tmp_path / "broken.toml").write_text('binary = "x"\n')
    with pytest.raises(UsageError):
        adapters.load_adapters(tmp_path)


def test_duplicate_adapter_name_raises(tmp_path):
    (tmp_path / "a.toml").write_text('name = "dup"\nbinary = "x"\n')
    (tmp_path / "b.toml").write_text('name = "dup"\nbinary = "y"\n')
    with pytest.raises(UsageError):
        adapters.load_adapters(tmp_path)
