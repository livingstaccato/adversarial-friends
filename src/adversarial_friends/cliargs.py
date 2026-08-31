"""Argument parsing: the `afriend` parser itself, and turning repeated
--friend cli:lens flags into FriendSpecs.

Split out of cli.py.
"""

import argparse

from . import __version__
from .adapters import Adapter, FriendSpec
from .ceilings import (
    DEFAULT_MAX_LOOP_ITERATIONS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_WALL_CLOCK_S,
)
from .errors import UsageError
from .ids import validate_friend_name
from .presets import PRESETS
from .resolutions import DISPOSITIONS
from .trust import MODEL_RE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afriend")
    parser.add_argument("--version", action="version", version=f"afriend {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run")
    # Optional: `--resume` names a run directory that already knows its
    # artifact, so requiring one again would invite passing a different
    # file than the run actually reviewed.
    run_p.add_argument("artifact", nargs="?", default=None)
    run_p.add_argument("--mode", default="report", choices=["report", "crossexam", "gate", "loop"])
    # §10.1: the default depends on the mode (gate defaults to thorough), so
    # it is resolved after parsing rather than baked in here -- None means
    # "the operator did not say".
    run_p.add_argument("--preset", default=None, choices=list(PRESETS))
    run_p.add_argument(
        "--friend",
        action="append",
        default=[],
        help="cli:lens[:model], repeatable; overrides discovery",
    )
    run_p.add_argument("--include-self", action="store_true")
    # §4.2. `exact` always reaches a terminal state unaided, which is what
    # makes the documented CLI usable from a plain shell; `orchestrator`
    # halts with exit 10 for judgment the runner cannot make.
    run_p.add_argument("--merge", default="exact", choices=["exact", "orchestrator"])
    # §13: an explicitly named roster may live anywhere, including inside the
    # repository -- naming it is the operator's act. Only the trusted
    # user-level path is ever picked up automatically.
    run_p.add_argument("--roster", default=None, metavar="FILE")
    # §10.1's layer 4: invocation flags outrank everything, including a
    # roster entry's own values.
    run_p.add_argument("--model", default=None, help="override every friend's model")
    run_p.add_argument("--effort", default=None, help="override every friend's effort")
    # §8.1: shape discovery without naming individual friends.
    run_p.add_argument(
        "--lens", action="append", default=[], help="restrict discovery to these lenses"
    )
    run_p.add_argument("--max-friends", type=int, default=None, metavar="N")
    # A floor, not a ceiling. Without it, a run where 1 of 50 friends
    # answered (everyone else misconfigured, rate-limited, or down) exits 0
    # the same as a run where 50 of 50 did -- the report says plainly that
    # it reflects one opinion rather than disagreement between several, but
    # nothing in the exit code carries that, so a CI wrapper reading only
    # the exit code cannot tell the two apart. Opt-in and unenforced by
    # default: a fresh checkout with one CLI installed is a normal use of
    # this tool, not a degraded one, and a floor nobody asked for would
    # fail that case for no reason.
    run_p.add_argument(
        "--require-friends",
        type=int,
        default=None,
        metavar="N",
        help="fail the run (exit 12) if fewer than N friends produce a usable answer",
    )
    # §12.4: worktrees and the run directory are removed at run end unless
    # asked otherwise. Keeping them is how you inspect what a friend saw.
    run_p.add_argument("--keep", action="store_true", help="keep friend worktrees for inspection")
    run_p.add_argument("--json", action="store_true", help="print run.json instead of the path")
    # §13's escape hatch, and the only way arbitrary flags ever reach a
    # friend. Command line ONLY -- never from any file -- and only together
    # with the acknowledgement below.
    run_p.add_argument(
        "--unsafe-extra-args",
        default=None,
        metavar="'...'",
        # Use the = form: argparse only accepts a dash-leading VALUE when it
        # contains a space, so `--unsafe-extra-args --foo` is parsed as two
        # flags while `--unsafe-extra-args '--foo --bar'` happens to work.
        # Saying so here beats letting an operator discover it.
        help="extra flags for every friend, e.g. --unsafe-extra-args='--foo'; "
        "requires --i-accept-unsandboxed",
    )
    run_p.add_argument("--i-accept-unsandboxed", action="store_true")
    # §12.2: a confined friend gets an allowlisted environment. This is for
    # the operator who knows a variable their CLI needs that its adapter
    # does not declare -- the alternative is a friend that fails to
    # authenticate with no useful error.
    run_p.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="VAR",
        help="also pass VAR to confined friends (repeatable)",
    )
    run_p.add_argument(
        "--resume",
        default=None,
        metavar="RUN_ID",
        help="continue a run that halted for the orchestrator (exit 10)",
    )
    # §12.2. A friend with no read-only mode of its own is refused when the
    # OS offers no way to confine it; this accepts that risk explicitly and
    # stamps every affected friend in the report.
    run_p.add_argument("--allow-unsandboxed-friend", action="store_true")
    run_p.add_argument("--timeout", type=int, default=900)
    run_p.add_argument("--out", default=None)
    # Progress is ON by default and goes to stderr. A crossexam is silent
    # for tens of minutes otherwise, and a silent run cannot be told from a
    # hung one -- measured here at 357s for a single friend in a single
    # round, against a 900s default timeout.
    #
    # Default-on is safe for scripts because stdout is untouched: it still
    # carries the run directory and nothing else. Opting out exists for a
    # caller that captures both streams together and wants only the result.
    run_p.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress per-friend progress on stderr (stdout is unaffected)",
    )
    run_p.add_argument(
        "--attributed",
        action="store_true",
        help="show judges who wrote each claim (§5 defaults to blind)",
    )
    # §7.4's ceilings. --max-calls defaults to None rather than a number
    # because its default is DERIVED from the roster size (see
    # ceilings.derive_max_calls): a constant here is exactly the bug §7.4
    # calls out, where the shipped default tripped its own ceiling mid-run.
    run_p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    run_p.add_argument("--max-calls", type=int, default=None)
    run_p.add_argument("--max-wall-clock", type=int, default=DEFAULT_MAX_WALL_CLOCK_S)
    run_p.add_argument("--max-loop-iterations", type=int, default=DEFAULT_MAX_LOOP_ITERATIONS)

    # §7.5. Appends a Resolution to a finished run's ledger and re-reports
    # the gate. Separate from `run` because resolving happens after a human
    # has gone and changed something, which may be days later.
    resolve_p = sub.add_parser("resolve")
    resolve_p.add_argument("run_id", help="run directory name, or a path to one")
    resolve_p.add_argument("--claim", required=True, help="claim id, e.g. c-0007@2")
    resolve_p.add_argument("--disposition", required=True, choices=list(DISPOSITIONS))
    resolve_p.add_argument(
        "--evidence",
        required=True,
        help="a location the fix touched, e.g. src/auth.py:38 -- §6.4 requires one",
    )
    resolve_p.add_argument("--author", default=None, help="defaults to $USER")
    resolve_p.add_argument("--out", default=None, help="run root, if not the default")

    # §17. Writes a roster from what is actually installed; asks nothing.
    init_p = sub.add_parser("init")
    init_p.add_argument("--force", action="store_true", help="overwrite an existing roster")
    init_p.add_argument("--out", default=None, help="write somewhere other than the default")

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--json", action="store_true", help="machine-readable output")
    doctor_p.add_argument(
        "--gc", action="store_true", help="remove run directories left by abandoned runs"
    )
    doctor_p.add_argument("--out", default=None, help="run root, if not the default")

    providers_p = sub.add_parser("providers")
    provider_sub = providers_p.add_subparsers(dest="provider_command", required=True)
    list_p = provider_sub.add_parser("list")
    list_p.add_argument("--json", action="store_true", help="machine-readable output")
    for action in ("enable", "disable", "clear-model"):
        action_p = provider_sub.add_parser(action)
        action_p.add_argument("name", metavar="NAME")
    set_model_p = provider_sub.add_parser("set-model")
    set_model_p.add_argument("name", metavar="NAME")
    set_model_p.add_argument("model", metavar="MODEL")
    return parser


def _specs_from_flags(
    values: list[str], timeout: int, registry: dict[str, Adapter], fake_enabled: bool
) -> list[FriendSpec]:
    """Build FriendSpecs directly from repeated --friend cli:lens flags.

    Deliberately bypasses roster.resolve(overrides=...) entirely, rather
    than converting each flag into an override dict and routing it through
    resolve(). Two reasons:

    1. roster.resolve(..., overrides=[]) treats an *explicit* empty list the
       same as "no overrides given" and falls through to full
       auto-discovery (a landmine inherited from Task 10, documented in
       test_roster.py's neighbors). --friend's own default is `[]`
       (argparse action="append", default=[]) precisely when the caller
       requests auto-discovery, so the two meanings of "empty" would
       collide if this function routed through resolve() at all: an
       explicit but empty override intent would be indistinguishable from
       "no --friend flags given". Building specs directly here means empty
       is only ever the "not given" case (handled by cmd_run choosing the
       discovery branch instead of calling this function), never something
       that can silently expand into every discovered friend.
    2. It is the cleanest available seam for the "fake" test-only cli (see
       the module docstring on tests/test_run_end_to_end.py): "fake" has no
       adapter in the registry at all, so routing it through
       roster.resolve's overrides validation (which requires
       registry[entry["cli"]] to exist) would need either a fabricated
       adapter or a special case inside roster.py -- a Task 10 file this
       task does not own.

    An unknown `cli` therefore raises UsageError (exit 2) here directly,
    the same fix Task 10 needed for roster.resolve's own overrides path
    (a config typo is a usage error, not "no friends available" -- see
    errors.NoFriendsError's exit code 3 vs UsageError's exit 2).
    """
    specs = []
    for index, value in enumerate(values):
        cli, sep, lens = value.partition(":")
        if not sep or not cli or not lens:
            raise UsageError(f"--friend must be formatted as cli:lens, got {value!r}")
        model: str | None = None
        if cli == "fake":
            if not fake_enabled:
                raise UsageError(
                    "cli 'fake' is only available when AF_FAKE_FRIEND is set "
                    "(it exists for tests, not real runs)"
                )
            # fake:<mode> defaults to doc scope, same as always. A test that
            # specifically needs to exercise the repo-scope worktree path
            # (which no real adapter can reach in a test environment with
            # no agent CLI on PATH -- the whole point of that PATH
            # restriction) may instead write fake:<mode>:repo to request
            # it explicitly. This suffix is only recognized for the
            # test-only "fake" cli; it has no effect on any real adapter.
            lens, _, scope_suffix = lens.partition(":")
            if scope_suffix and scope_suffix not in ("repo", "doc"):
                raise UsageError(
                    f"fake friend scope suffix must be 'repo' or 'doc', got {scope_suffix!r}"
                )
            scope = scope_suffix or "doc"
        else:
            adapter = registry.get(cli)
            if adapter is None:
                raise UsageError(f"unknown cli: {cli!r} (known: {sorted(registry) or 'none'})")
            # An optional third slot names the model: `cli:lens:model`. The
            # spec defines a friend as (cli, model, effort, lens) -- §8.1 --
            # and without this the only way to set one is a roster file,
            # which has no flag to load it yet. That made the whole HTTP
            # transport unreachable from the CLI, since ollama has no
            # default model and must be told which to run.
            lens, _, model_suffix = lens.partition(":")
            model = model_suffix or None
            # The model reaches argv through the adapter's model_flag, so it
            # crosses the same trust boundary a roster entry does and gets
            # the same validation rather than a weaker one.
            if model is not None and MODEL_RE.fullmatch(model) is None:
                raise UsageError(f"invalid model {model!r}: must match {MODEL_RE.pattern!r}")
            if adapter.transport == "http":
                # An HTTP friend is a bare model behind an endpoint: no
                # filesystem access to constrain, so no readonly flag exists
                # to emit and repo scope would be a claim about enforcement
                # that never happened. Doc scope always -- containment comes
                # from handing it only the artifact text.
                scope = "doc"
            else:
                scope = "repo" if adapter.readonly_argv else "doc"
        name = f"{cli}-{lens}-{index}"
        validate_friend_name(name)
        specs.append(
            FriendSpec(
                name=name,
                cli=cli,
                lens=lens,
                model=model,
                effort=None,
                scope=scope,
                timeout=timeout,
            )
        )
    return specs
