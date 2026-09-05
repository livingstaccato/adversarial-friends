"""Command line entry point.

Wires the modules under this package into working subcommands:
`afriend run` (modes report/crossexam/gate/loop), `afriend resolve`,
`afriend doctor`, and `afriend status`. The actual work lives in
cliargs.py (parsing), dispatch.py (running one friend), prompt.py (building
one friend's prompt), and commands/run.py + commands/doctor.py (the two
subcommands); this file is the thin entry point that ties them together.

Several names below are re-exported (not just imported for local use) so
that `afriend.cli.<name>` keeps resolving to the same names it
always has -- both for external callers and for this project's own tests,
several of which reach into cli.py's namespace directly (e.g.
`cli.build_parser()`, `cli._dispatch(...)`, `cli.KILL_GRACE_S`).
"""

import sys

from .cliargs import _specs_from_flags, build_parser
from .commands.doctor import cmd_doctor

# _resolve_repo_root moved to commands/environment.py when run.py was
# split for the line cap; re-exported from its original name because
# tests and external callers reach into cli.py's namespace directly.
from .commands.environment import _resolve_repo_root
from .commands.init import cmd_init
from .commands.profiles import cmd_profiles
from .commands.providers import cmd_providers
from .commands.resolve import cmd_resolve
from .commands.run import cmd_run
from .commands.status import cmd_status
from .dispatch import (
    _FAKE_CAPABILITY,
    _UNKNOWN_CAPABILITY,
    KILL_GRACE_S,
    PROMPT_ARGV_WARN_BYTES,
    _dispatch,
    _exception_outcome,
    _stderr_tail,
)
from .errors import AfError
from .prompt import PROMPT_HEADER, _build_friend_prompt, _load_lens, available_lenses

__all__ = [
    "KILL_GRACE_S",
    "PROMPT_ARGV_WARN_BYTES",
    "PROMPT_HEADER",
    "_FAKE_CAPABILITY",
    "_UNKNOWN_CAPABILITY",
    "_build_friend_prompt",
    "_dispatch",
    "_exception_outcome",
    "_load_lens",
    "_resolve_repo_root",
    "_specs_from_flags",
    "_stderr_tail",
    "available_lenses",
    "build_parser",
    "cmd_doctor",
    "cmd_init",
    "cmd_profiles",
    "cmd_providers",
    "cmd_resolve",
    "cmd_run",
    "cmd_status",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "init":
            return cmd_init(args)
        if args.command == "resolve":
            return cmd_resolve(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "providers":
            return cmd_providers(args)
        if args.command == "profiles":
            return cmd_profiles(args)
        parser.print_help()
        return 0
    except AfError as exc:
        print(f"afriend: {exc}", file=sys.stderr)
        return exc.exit_code
