"""Everything `cmd_run` settles before the first friend is dispatched.

Split out of commands/run.py when that function crossed the then-current
line cap for the third time. The seam is real rather than arbitrary:
none of this touches the artifact, the run directory, or the round loop --
it resolves the roster, parses the operator's escape hatches, and installs
the process-level state a run needs (signal handlers, the progress
reporter). Everything here is decided once; everything left in cmd_run
happens per iteration.
"""

import argparse
import concurrent.futures
from dataclasses import dataclass, field
import os
import threading
from typing import Any

from ..adapters import Adapter, FriendSpec, load_adapters
from ..authority import ExternalToolPolicy, enforce_extra_args
from ..paths import ADAPTER_DIR
from ..progress import Progress
from ..trust import parse_unsafe_extra_args
from .confinement import confinement_downgrades
from .environment import install_abort_handlers
from .friends import roster_for_run


@dataclass
class RunSetup:
    """The decided-once state a run carries into its round loop."""

    registry: dict[str, Adapter]
    fake_cmd: list[str] | None
    downgrades: list[str]
    extra_args: list[str]
    resolved: Any
    specs: list[FriendSpec]
    env_withheld: Any
    abort_event: threading.Event
    abort_signum: dict[str, int | None]
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None]
    installed_handlers: dict[int, Any] = field(default_factory=dict)
    reporter: Progress = field(default_factory=Progress)
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY


def prepare_run(args: argparse.Namespace) -> RunSetup:
    """Resolve the roster and install process-level state.

    Returns everything `cmd_run` then owns for the rest of the run. The
    caller is responsible for restoring `installed_handlers` and closing
    `reporter` -- both are process-wide, and a library-ish function should
    not leave either permanently changed.
    """
    registry = load_adapters(ADAPTER_DIR)
    external_tool_policy = (
        ExternalToolPolicy.ALLOW
        if getattr(args, "allow_external_tools", False)
        else ExternalToolPolicy.DENY
    )
    # AF_FAKE_FRIEND keeps the end-to-end tests off real CLIs and, critically,
    # off any metered provider. `--friend fake:<mode>` runs
    # `$AF_FAKE_FRIEND <mode>` directly (see dispatch._dispatch); the mode
    # travels in the lens slot of the cli:lens flag syntax.
    fake_env = os.environ.get("AF_FAKE_FRIEND")
    fake_cmd = fake_env.split() if fake_env else None
    downgrades: list[str] = []
    # §13's escape hatch. Parsed early so a bad value fails before any
    # dispatch, and recorded as a downgrade because a run carrying
    # unvalidated flags has weaker guarantees than its friend table implies.
    extra_args = parse_unsafe_extra_args(args.unsafe_extra_args, args.i_accept_unsandboxed)
    enforce_extra_args(external_tool_policy, extra_args)
    if extra_args:
        downgrades.append(
            f"--unsafe-extra-args passed {extra_args} to every friend. These "
            "flags are not validated, so read-only is reported as False for "
            "every friend regardless of what its adapter emitted."
        )

    resolved, specs = roster_for_run(args, registry, fake_cmd, downgrades, external_tool_policy)
    # §12.2: every friend that will run without OS confinement is named in
    # the report, whether that is because the operator overrode the refusal
    # or because the CLI has no read-only mode and one was available. A
    # weakened guarantee has to be visible in the artifact a human reads,
    # not only in the code that decided it.
    env_withheld = confinement_downgrades(args, specs, registry, downgrades)

    # Signal handling: a cancelled or CI-killed run must not leave a
    # metered agent CLI process running unbounded, nor a stale
    # `git worktree` registration behind in the repo under review. Neither
    # SIGTERM nor (reliably, once the main thread is blocked deep inside a
    # C-level wait) SIGINT would otherwise give this function's own
    # `finally` blocks a chance to run at all: SIGTERM's default
    # disposition kills the process immediately, with no Python-level
    # unwinding whatsoever; SIGINT's default handler does raise
    # KeyboardInterrupt, but that exception, once it propagates out of the
    # blocked `pool.map()` call below, immediately re-blocks inside
    # `ThreadPoolExecutor.__exit__`'s own `shutdown(wait=True)`, which
    # waits for the same still-hung worker -- so cleanup never actually
    # runs within any reasonable time either way. Installing explicit
    # handlers for both signals, which only ever set `abort_event` and
    # shut the active pool down without waiting, is what makes the
    # `finally` blocks below reachable promptly. Handlers are restored
    # unconditionally in the outer `finally` -- a library-ish function
    # should not permanently hijack process-wide signal disposition.
    abort_event = threading.Event()
    abort_signum: dict[str, int | None] = {"value": None}
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None] = [None]
    installed_handlers = install_abort_handlers(abort_event, abort_signum, active_pool)
    if len(installed_handlers) < 2:
        downgrades.append(
            "signal-based abort handling is unavailable in this context "
            "(cmd_run was not called from the main thread); Ctrl-C/SIGTERM "
            "cannot cleanly abort this run -- isolation teardown on a kill "
            "signal is not guaranteed."
        )
    # Progress goes to stderr; stdout stays the run directory and nothing
    # else, so `cd "$(afriend run spec.md)"` keeps working. Built before the
    # try because its `finally` closes it.
    reporter = Progress(enabled=not args.no_progress)
    return RunSetup(
        registry=registry,
        fake_cmd=fake_cmd,
        downgrades=downgrades,
        extra_args=extra_args,
        resolved=resolved,
        specs=specs,
        env_withheld=env_withheld,
        abort_event=abort_event,
        abort_signum=abort_signum,
        active_pool=active_pool,
        installed_handlers=installed_handlers,
        reporter=reporter,
        external_tool_policy=external_tool_policy,
    )
