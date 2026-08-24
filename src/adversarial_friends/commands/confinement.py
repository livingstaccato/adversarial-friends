"""What a run records about friends the OS has to confine -- §12.2, §12.3.

Split from commands/run.py for the line cap. It is also one concern: every
weakened guarantee a confined friend introduces belongs in the artifact a
human reads, not only in the code that decided it.
"""

import argparse

from .. import childenv
from ..adapters import Adapter, FriendSpec


def confinement_downgrades(
    args: argparse.Namespace,
    specs: list[FriendSpec],
    registry: dict[str, Adapter],
    downgrades: list[str],
) -> list[str]:
    """Append confinement notes to `downgrades`; return withheld env names."""
    unconfined = [s for s in specs if s.cli in registry and not registry[s.cli].readonly_argv]
    env_withheld: list[str] = []
    if unconfined:
        # Names only, never values: this list reaches run.json and report.md,
        # and writing a secret into the run directory to report that it was
        # protected would be its own leak.
        env_withheld = childenv.withheld(tuple(args.pass_env))
        dropped = env_withheld
        if dropped:
            downgrades.append(
                f"{len(dropped)} environment variable(s) were withheld from "
                f"confined friends ({', '.join(s.name for s in unconfined)}); "
                "names are recorded, values never are. Pass --pass-env VAR if "
                "a friend needs one."
            )
    if unconfined and args.allow_unsandboxed_friend:
        downgrades.append(
            "--allow-unsandboxed-friend was passed: "
            + ", ".join(s.name for s in unconfined)
            + " may run with no OS confinement at all. The artifact under "
            "review is untrusted text; a friend that follows an instruction "
            "inside it can read anything this user can."
        )
    return env_withheld
