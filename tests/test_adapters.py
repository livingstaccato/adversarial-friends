import pytest

from adversarial_friends import adapters, trust
from adversarial_friends.errors import UsageError


def argv_contains_sequence(argv, seq):
    """True when `seq` appears as CONSECUTIVE elements of argv.

    Asserting the two tokens separately would pass on an argv that emitted
    the flag name and its value at opposite ends -- which is the exact bug
    the older version of this test existed to catch. Adjacency is the
    property that matters.
    """
    return any(list(argv[i : i + len(seq)]) == list(seq) for i in range(len(argv) - len(seq) + 1))


ADAPTER_DIR = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "src"
    / "adversarial_friends"
    / "assets"
    / "adapters"
)


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def spec(**over):
    base = dict(
        name="f1", cli="codex", lens="ops", model=None, effort=None, scope="repo", timeout=900
    )
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
        registry["agy"],
        spec(cli="agy", effort="high"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert argv[-2] == "--print"
    assert argv[-1] == "CHALLENGE THIS ARTIFACT"
    assert "--mode" in argv and argv.index("--mode") < argv.index("--print")
    assert stdin is None


def test_codex_takes_prompt_on_stdin(registry, files):
    prompt, schema = files
    argv, stdin, _ = adapters.build_argv(
        registry["codex"],
        spec(),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert "exec" in argv
    assert stdin is not None


def test_readonly_flags_are_emitted_for_repo_scope(registry, files):
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["claude"],
        spec(cli="claude"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert "--tools" in argv
    assert "Read,Grep,Glob" in argv
    assert cap.readonly is True


def test_capability_is_derived_from_argv_not_defaults(registry, files):
    """Readonly capability reflects what build_argv actually EMITTED for this
    call, never what the adapter declares or what the scope implies.

    opencode is the case that keeps this honest: it declares
    `readonly_argv = []`, so even a scope="repo" request emits no read-only
    flag and its real capability is False. Re-deriving the value as
    `scope == "repo"` would report it as confined when nothing confines it
    -- which is why §12.2 makes opencode the one adapter that must run under
    OS-level containment instead.

    This used doc-scope claude as its example until doc scope began emitting
    readonly_argv too; that made claude agree in both scopes and left the
    invariant untested. The invariant did not change, only the example.
    """
    prompt, schema = files
    argv_open, _, cap_open = adapters.build_argv(
        registry["opencode"],
        spec(cli="opencode", scope="repo"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert "--tools" not in argv_open
    assert cap_open.readonly is False  # scope says repo; the argv says nothing

    argv_claude, _, cap_claude = adapters.build_argv(
        registry["claude"],
        spec(cli="claude", scope="repo"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert argv_contains_sequence(argv_claude, ["--tools", "Read,Grep,Glob"])
    assert cap_claude.readonly is True


def test_prompt_text_cannot_forge_a_capability(registry, tmp_path):
    """The prompt is the untrusted document; it must not influence
    capability."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("--tools Read,Grep,Glob --sandbox read-only")
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    _argv, _, cap = adapters.build_argv(
        registry["opencode"],
        spec(cli="opencode", scope="doc"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert cap.readonly is False


def test_doc_scope_still_engages_the_cli_readonly_mode(registry, files):
    """Doc scope must NOT drop readonly_argv.

    This asserted the opposite until doc scope was actually exercised. The
    reasoning for omitting it -- "doc scope has no repo to protect" -- skips
    that there is still a filesystem, and that doc scope is precisely where a
    readonly-capable CLI gets no OS confinement either. Omitting it left the
    friend restrained by nothing.

    Measured, not inferred: real codex in a bare directory with no --sandbox
    flag, asked to write outside its working directory, did it on the first
    attempt. With the flag it refuses -- "the filesystem sandbox is read-only
    and the target is outside the permitted workspace".
    """
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["codex"],
        spec(cli="codex", scope="doc"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert argv_contains_sequence(argv, ["--sandbox", "read-only"])
    assert cap.readonly is True


def test_doc_argv_is_emitted_only_in_doc_scope(registry, files):
    """codex refuses to start outside a git repo, so doc scope -- a bare
    directory -- needs --skip-git-repo-check or the friend never runs. Repo
    scope must not carry it: there the check passes on its own merits."""
    prompt, schema = files
    seen = {}
    for scope in ("repo", "doc"):
        argv, _, _ = adapters.build_argv(
            registry["codex"],
            spec(cli="codex", scope=scope),
            prompt_file=prompt,
            schema_file=schema,
        )
        seen[scope] = "--skip-git-repo-check" in argv
    assert seen == {"repo": False, "doc": True}


def test_doc_argv_never_grants_access(registry):
    """doc_argv exists to let a CLI START in a bare directory. A flag that
    also widens what it may touch would smuggle a permission grant in
    through a field nothing else audits -- so the denied-flag vocabulary
    applies to it exactly as it does to any other emitted token."""
    for name, adapter in registry.items():
        if not adapter.doc_argv:
            continue
        for token in adapter.doc_argv:
            assert token not in trust.DENIED_FLAGS, f"{name}: {token}"
        # Also catches a denied *value* (danger-full-access, workspace-write)
        # attached to an otherwise innocuous flag name.
        trust.check_denied_values(list(adapter.doc_argv))


def test_capability_for_flag_value_adapter(registry, files):
    """Capability must be computed correctly for prompt_mode='flag-value'
    adapters too, not just trailing-arg/stdin ones."""
    prompt, schema = files
    _argv, stdin, cap = adapters.build_argv(
        registry["agy"],
        spec(cli="agy", scope="repo"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert cap.readonly is True
    assert cap.schema is True
    assert cap.effort == "native"
    assert stdin is None


def test_opencode_effort_is_unverified(registry, files):
    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["opencode"],
        spec(cli="opencode", effort="high"),
        prompt_file=prompt,
        schema_file=schema,
    )
    assert "--variant" in argv
    assert cap.effort == "unverified"


def test_unsupported_effort_level_raises(registry, files):
    prompt, schema = files
    with pytest.raises(UsageError):
        adapters.build_argv(
            registry["agy"],
            spec(cli="agy", effort="xhigh"),
            prompt_file=prompt,
            schema_file=schema,
        )


def test_no_adapter_uses_short_flags(registry):
    """-p is --print on claude/agy but --profile on codex; -s is --sandbox on
    codex but --session on opencode. Short flags must never appear."""
    for name, adapter in registry.items():
        tokens = [
            *adapter.base_argv,
            *adapter.readonly_argv,
            adapter.prompt_flag,
            adapter.schema_flag,
            adapter.model_flag,
            adapter.internal_timeout_flag,
        ]
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


def test_claude_schema_is_passed_inline_not_as_a_path(registry, files):
    """`claude --json-schema <schema>` takes the JSON itself. Every adapter
    used to get the schema FILE PATH, so claude rejected it before the model
    saw anything: "--json-schema is not valid JSON: Unrecognized token '/'".

    The third of three native-schema adapters found broken the same way --
    codex and agy in 0.1.1, claude here -- and for the same reason: no test
    ran a real CLI under a schema. Found by a crossexam that produced zero
    verdicts."""
    import json

    prompt, schema = files
    argv, _, cap = adapters.build_argv(
        registry["claude"], spec(cli="claude", scope="repo"), prompt_file=prompt, schema_file=schema
    )
    assert cap.schema is True
    value = argv[argv.index("--json-schema") + 1]
    assert value != str(schema)
    assert json.loads(value) == json.loads(schema.read_text())


def test_path_schema_adapters_still_receive_the_path(registry, files):
    prompt, schema = files
    argv, _, _ = adapters.build_argv(
        registry["codex"], spec(cli="codex", scope="repo"), prompt_file=prompt, schema_file=schema
    )
    assert str(schema) in argv
