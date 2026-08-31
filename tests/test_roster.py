import pytest

from adversarial_friends import adapters, roster
from adversarial_friends.errors import NoFriendsError, UsageError
from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

ADAPTER_DIR = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "src"
    / "adversarial_friends"
    / "assets"
    / "adapters"
)
LENSES = ["assumptions", "security", "ops"]

# Every test here controls discovery through an injected `which`, which
# only governs the exec transport. Without this, a developer running
# ollama locally is discovered over HTTP regardless, and these tests pass
# or fail depending on whether their server happens to be up.
NO_HTTP = {"AF_NO_HTTP_DISCOVERY": "1"}


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def which_all(name):
    return f"/usr/local/bin/{name}"


def which_none(name):
    return None


def test_detects_claude_code_host():
    assert roster.detect_host({"CLAUDECODE": "1"}) == "claude"


def test_detects_codex_host():
    assert roster.detect_host({"CODEX_SANDBOX": "seatbelt"}) == "codex"


def test_detects_current_codex_host_markers():
    assert roster.detect_host({"CODEX_SESSION_ID": "session"}) == "codex"
    assert roster.detect_host({"CODEX_THREAD_ID": "thread"}) == "codex"


def test_no_host_detected_when_env_is_bare():
    assert roster.detect_host({}) is None


def test_host_cli_is_excluded_by_default(registry):
    friends = roster.resolve(registry, LENSES, {**NO_HTTP, "CLAUDECODE": "1"}, which_all)
    assert all(f.cli != "claude" for f in friends)


def test_include_self_keeps_the_host(registry):
    friends = roster.resolve(
        registry, LENSES, {**NO_HTTP, "CLAUDECODE": "1"}, which_all, include_self=True
    )
    assert any(f.cli == "claude" for f in friends)


def test_current_codex_host_is_excluded_unless_include_self(registry):
    env = {**NO_HTTP, "CODEX_SESSION_ID": "session"}
    excluded = roster.resolve(registry, LENSES, env, which_all)
    included = roster.resolve(registry, LENSES, env, which_all, include_self=True)
    assert all(friend.cli != "codex" for friend in excluded)
    assert any(friend.cli == "codex" for friend in included)


def test_explicit_host_provider_marks_wrapper_host(registry):
    friends = roster.resolve(
        registry,
        LENSES,
        NO_HTTP,
        which_all,
        host_provider="opencode",
    )
    assert all(friend.cli != "opencode" for friend in friends)


def test_lenses_are_assigned_round_robin(registry):
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all)
    assigned = [f.lens for f in friends]
    assert assigned[:3] == LENSES[:3]
    assert len(set(assigned[:3])) == 3


def test_capacity_is_applied_after_readiness(registry):
    specs = roster.resolve(
        registry,
        ["ops"],
        {},
        which=lambda name: f"/bin/{name}" if name == "opencode" else None,
        probe=lambda _: True,
        provider_policy=ProviderPolicy({"ollama": ProviderSetting(enabled=True, model=None)}),
        max_friends=1,
    )
    assert [spec.cli for spec in specs] == ["opencode"]


def test_provider_model_preference_flows_into_selected_friend(registry):
    specs = roster.resolve(
        registry,
        ["ops"],
        {},
        which=lambda _: None,
        probe=lambda _: True,
        provider_policy=ProviderPolicy(
            {"ollama": ProviderSetting(enabled=True, model="qwen3:0.6b")}
        ),
    )
    assert [(spec.cli, spec.model) for spec in specs] == [("ollama", "qwen3:0.6b")]


def test_default_timeout_is_used_when_none_is_passed(registry):
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all)
    assert all(f.timeout == roster.DEFAULT_TIMEOUT for f in friends)


def test_auto_discovered_friends_receive_the_passed_timeout(registry):
    """Task 12 review, Finding 2: --timeout was silently ignored for every
    auto-discovered friend because resolve() had no timeout parameter at
    all -- a flag that silently does nothing is worse than no flag."""
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all, timeout=30)
    assert friends, "fixture produced no friends to check"
    assert all(f.timeout == 30 for f in friends)
    assert all(f.timeout != roster.DEFAULT_TIMEOUT for f in friends)


def test_override_without_its_own_timeout_key_falls_back_to_the_passed_timeout(registry):
    """The passed `timeout` is also the fallback default for an override
    entry that doesn't set its own `timeout` key -- an override's own
    explicit key still wins (see the next test)."""
    friends = roster.resolve(
        registry,
        LENSES,
        {},
        which_all,
        timeout=42,
        overrides=[{"name": "codex-ops", "cli": "codex", "lens": "ops"}],
    )
    assert friends[0].timeout == 42


def test_override_s_own_timeout_key_still_wins_over_the_passed_timeout(registry):
    friends = roster.resolve(
        registry,
        LENSES,
        {},
        which_all,
        timeout=42,
        overrides=[{"name": "codex-ops", "cli": "codex", "lens": "ops", "timeout": 7}],
    )
    assert friends[0].timeout == 7


def test_opencode_defaults_to_doc_scope(registry):
    """opencode has no read-only mode, so repo scope needs an explicit opt-in."""
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all)
    opencode = next(f for f in friends if f.cli == "opencode")
    assert opencode.scope == "doc"


def test_no_binaries_raises_no_friends(registry):
    with pytest.raises(NoFriendsError):
        roster.resolve(registry, LENSES, NO_HTTP, which_none)


def test_overrides_replace_discovery(registry):
    friends = roster.resolve(
        registry,
        LENSES,
        {},
        which_all,
        overrides=[
            {
                "name": "codex-ops",
                "cli": "codex",
                "lens": "ops",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        ],
    )
    assert len(friends) == 1
    assert friends[0].name == "codex-ops"
    assert friends[0].model == "gpt-5.6-sol"


# --- Adversarial attacks -----------------------------------------------
#
# Each test below documents an attempt to break resolve()/discover_clis()/
# detect_host() with a hostile or malformed input. Every one is annotated
# with whether the resulting failure is clean (a UsageError/NoFriendsError
# with an actionable message) or was, before a fix, confusing (an
# unrelated stdlib exception or silent wrong output).


def test_override_unknown_cli_raises_no_friends(registry):
    """Attack: an override names a cli with no adapter in the registry.

    Fails cleanly: NoFriendsError, unchanged from the brief's own code."""
    with pytest.raises(NoFriendsError, match="unknown cli"):
        roster.resolve(
            registry,
            LENSES,
            {},
            which_all,
            overrides=[{"name": "x", "cli": "no-such-cli", "lens": "ops"}],
        )


@pytest.mark.parametrize("bad_name", ["Codex-Ops", "codex/ops", "../../etc", "a" * 40])
def test_override_invalid_friend_name_raises_usage_error(registry, bad_name):
    """Attack: an override's friend name fails ids.validate_friend_name.

    Fails cleanly: UsageError, unchanged from the brief's own code (routed
    through trust.validate_roster_entry as required)."""
    with pytest.raises(UsageError, match="invalid friend name"):
        roster.resolve(
            registry,
            LENSES,
            {},
            which_all,
            overrides=[{"name": bad_name, "cli": "codex", "lens": "ops"}],
        )


def test_override_missing_name_raises_usage_error(registry):
    """Attack: an override omits the required name key entirely."""
    with pytest.raises(UsageError, match="missing required key: name"):
        roster.resolve(
            registry, LENSES, NO_HTTP, which_all, overrides=[{"cli": "codex", "lens": "ops"}]
        )


def test_empty_lens_list_raises_usage_error_not_zero_division(registry):
    """Attack: an empty lens list reaches the discovery round-robin.

    Was confusing before the fix: `lenses[index % len(lenses)]` raised a
    bare ZeroDivisionError with no mention of roster, lenses, or friends.
    Reproduced directly against the pre-fix code (see task-10-report.md).
    Now fails cleanly with UsageError."""
    with pytest.raises(UsageError, match="no lenses configured"):
        roster.resolve(registry, [], NO_HTTP, which_all)


def test_override_with_empty_lens_list_is_unaffected(registry):
    """The empty-lens guard must not fire on the overrides path, which
    never reads the `lenses` parameter at all."""
    friends = roster.resolve(
        registry,
        [],
        {},
        which_all,
        overrides=[{"name": "codex-ops", "cli": "codex", "lens": "ops"}],
    )
    assert len(friends) == 1
    assert friends[0].lens == "ops"


def test_lens_list_shorter_than_discovered_clis_round_robins(registry):
    """Attack: fewer lenses than discovered CLIs (4 exec adapters ship:
    agy, claude, codex, opencode; ollama is http-only and skipped).

    Passes cleanly: the modulo round-robin terminates and cycles, and
    because each discovered cli is distinct, the (cli, lens) name pairing
    stays unique even when lenses repeat."""
    friends = roster.resolve(registry, ["a", "b"], NO_HTTP, which_all)
    assert len(friends) == 4
    assert [f.lens for f in friends] == ["a", "b", "a", "b"]
    assert len({f.name for f in friends}) == 4  # no name collisions


@pytest.mark.parametrize("value", ["0", "false", "False", "no", " "])
def test_host_env_var_with_falsy_looking_value_still_detected(value):
    """Attack: CLAUDECODE set to a value that LOOKS like "off".

    Not a bug: detect_host is presence-only (matches real Claude Code,
    which only ever sets CLAUDECODE=1), but any non-empty string --
    including "0" or "false" -- is truthy in Python and is treated as
    "host present". Documented here rather than "fixed": there is no
    documented convention where these CLIs emit a falsy sentinel value,
    and env.get(marker) truthiness is the same test the plan specifies."""
    assert roster.detect_host({"CLAUDECODE": value}) == "claude"


def test_host_env_var_empty_string_is_not_detected():
    assert roster.detect_host({"CLAUDECODE": ""}) is None


def test_multiple_host_markers_set_at_once_is_deterministic():
    """Attack: several host env markers set simultaneously (e.g. nested
    sessions, or a test harness that copies the whole environment).

    Fails cleanly (not at all, in fact): detect_host iterates
    HOST_ENV_MARKERS in its fixed declaration order (claude markers first,
    then codex, then opencode), independent of the input dict's own key
    order, so the result is deterministic regardless of which markers are
    also set."""
    assert roster.detect_host({"CLAUDECODE": "1", "CODEX_SANDBOX": "seatbelt"}) == "claude"
    assert (
        roster.detect_host({"CODEX_SANDBOX": "seatbelt", "OPENCODE_SERVER_PASSWORD": "x"})
        == "codex"
    )
    # Reversed insertion order in the input env changes nothing.
    assert roster.detect_host({"OPENCODE_SERVER_PASSWORD": "x", "CLAUDECODE": "1"}) == "claude"


def test_which_returning_unverified_path_is_trusted_by_discover_clis(registry, tmp_path):
    """Attack: `which` reports a path for a binary that is not executable.

    This is a documented trust boundary, not a bug: discover_clis has no
    independent os.access(X_OK) check and fully trusts its `which`
    argument. Adding one would break the brief's own which_all/which_none
    test doubles, which return fabricated paths that do not exist on disk
    at all. The production default (shutil.which) already enforces
    executability -- proven below by pointing PATH at a real,
    non-executable file and confirming it is NOT discovered."""
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(0o644)  # not executable

    def which_lying(name):
        candidate = tmp_path / name
        return str(candidate) if candidate.exists() else None

    found = roster.discover_clis(registry, which_lying)
    assert "codex" in found  # discover_clis trusted the lying which

    import shutil

    assert shutil.which("codex", path=str(tmp_path)) is None  # real which does not lie


def test_duplicate_override_names_raise_usage_error(registry):
    """Attack: two override entries share the same `name`.

    Was confusing before the fix: resolve() silently returned two
    FriendSpec objects with an identical name and no error. Since friend
    names become output-path components (ids.py), this would let the
    second friend's spawn output silently overwrite the first's -- data
    loss with zero diagnostic. Reproduced directly against the pre-fix
    code (see task-10-report.md). Now fails cleanly with UsageError."""
    overrides = [
        {"name": "dup", "cli": "codex", "lens": "ops"},
        {"name": "dup", "cli": "claude", "lens": "security"},
    ]
    with pytest.raises(UsageError, match="duplicate friend name"):
        roster.resolve(registry, LENSES, NO_HTTP, which_all, overrides=overrides)


def test_three_way_duplicate_override_names_raise_usage_error(registry):
    """The duplicate-name guard must generalize past exactly two entries."""
    overrides = [
        {"name": "dup", "cli": "codex", "lens": "ops"},
        {"name": "other", "cli": "claude", "lens": "security"},
        {"name": "dup", "cli": "agy", "lens": "assumptions"},
    ]
    with pytest.raises(UsageError, match="duplicate friend name"):
        roster.resolve(registry, LENSES, NO_HTTP, which_all, overrides=overrides)


def test_unique_override_names_are_unaffected(registry):
    """Control case for the duplicate-name guard: distinct names must
    still resolve normally, including two entries sharing the same cli
    under different names/lenses/models."""
    overrides = [
        {"name": "agy-security", "cli": "agy", "lens": "security", "model": "gemini-3.1-pro-high"},
        {"name": "agy-ops", "cli": "agy", "lens": "ops", "model": "claude-sonnet-4-6"},
    ]
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all, overrides=overrides)
    assert [f.name for f in friends] == ["agy-security", "agy-ops"]


def test_friend_count_not_cli_count_for_degraded_mode(registry):
    """Design-decision check: degraded mode (§8.3) must be judged on
    len(friends), not on the number of distinct CLI binaries. Two
    overrides on the same cli (agy hosting multiple model families) must
    both count -- resolve() must not deduplicate by cli."""
    overrides = [
        {"name": "agy-a", "cli": "agy", "lens": "assumptions", "model": "gemini-3.1-pro-high"},
        {"name": "agy-b", "cli": "agy", "lens": "security", "model": "gpt-oss"},
    ]
    friends = roster.resolve(registry, LENSES, NO_HTTP, which_all, overrides=overrides)
    assert len(friends) == 2
    assert all(f.cli == "agy" for f in friends)
    assert len(friends) >= 2  # would NOT trigger degraded mode


def test_degraded_modes_constant():
    assert frozenset({"report"}) == roster.DEGRADED_MODES
