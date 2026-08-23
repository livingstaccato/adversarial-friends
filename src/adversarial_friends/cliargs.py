"""Argument parsing: the `afriend` parser itself, and turning repeated
--friend cli:lens flags into FriendSpecs.

Split out of cli.py.
"""

import argparse

from . import __version__
from .adapters import Adapter, FriendSpec
from .errors import UsageError
from .ids import validate_friend_name
from .trust import MODEL_RE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afriend")
    parser.add_argument("--version", action="version", version=f"afriend {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run")
    run_p.add_argument("artifact")
    run_p.add_argument("--mode", default="report", choices=["report", "crossexam", "gate", "loop"])
    run_p.add_argument("--preset", default="inherit", choices=["inherit", "thorough", "cheap"])
    run_p.add_argument(
        "--friend",
        action="append",
        default=[],
        help="cli:lens[:model], repeatable; overrides discovery",
    )
    run_p.add_argument("--include-self", action="store_true")
    run_p.add_argument("--timeout", type=int, default=900)
    run_p.add_argument("--out", default=None)

    sub.add_parser("doctor")
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
