"""What a run records about friends the OS has to confine -- §12.2, §12.3.

Split from commands/run.py for the line cap. It is also one concern: every
weakened guarantee a confined friend introduces belongs in the artifact a
human reads, not only in the code that decided it.
"""

import argparse

from .. import childenv, sandbox
from ..adapters import Adapter, FriendSpec


def confinement_downgrades(
    args: argparse.Namespace,
    specs: list[FriendSpec],
    registry: dict[str, Adapter],
    downgrades: list[str],
) -> list[str]:
    """Append confinement notes to `downgrades`; return withheld env names.

    The returned list is the run's record that secrets were kept from
    confined friends, so it must describe what dispatch will actually do
    (dispatch.py builds the child environment as
    `childenv.build(adapter.env_pass, pass_env)`, and only when a
    confinement mechanism exists). Computed any other way it is worse than
    no record: a crossexam of this file found `--pass-env` being passed in
    `withheld`'s *adapter* slot, so every name an adapter declares in its
    own `pass` list -- six API keys, for opencode -- was reported as
    withheld while being handed to the child. Nothing checked whether a
    mechanism existed either, so an unsandboxed run that filtered nothing
    still produced a full withheld list for a reader to trust.
    """
    unconfined = [s for s in specs if s.cli in registry and not registry[s.cli].readonly_argv]
    env_withheld: list[str] = []
    if unconfined and sandbox.detect() is None:
        # No mechanism, so dispatch passes `env=None` and the child inherits
        # everything. Whether the run proceeds at all is decided elsewhere
        # (`--allow-unsandboxed-friend`); what must not happen either way is
        # a withheld list implying a filter that did not run.
        downgrades.append(
            "no OS confinement mechanism is available here, so the environment of "
            + ", ".join(s.name for s in unconfined)
            + " was NOT filtered: each inherits every variable exported to this "
            "run. No environment variable was withheld from them."
        )
    elif unconfined:
        # Per friend, from the same inputs dispatch uses. A name counts as
        # withheld only if NO confined friend received it; one that some
        # adapter's own pass list lets through is named separately rather
        # than being folded into a list that claims it was kept back.
        per_friend = {
            s.name: set(childenv.withheld(registry[s.cli].env_pass, tuple(args.pass_env)))
            for s in unconfined
        }
        kept_from_all = set.intersection(*per_friend.values())
        # Names only, never values: this list reaches run.json and report.md,
        # and writing a secret into the run directory to report that it was
        # protected would be its own leak.
        env_withheld = sorted(kept_from_all)
        passed_to_some = sorted(set.union(*per_friend.values()) - kept_from_all)
        if env_withheld:
            downgrades.append(
                f"{len(env_withheld)} environment variable(s) were withheld from "
                f"confined friends ({', '.join(s.name for s in unconfined)}); "
                "names are recorded, values never are. Pass --pass-env VAR if "
                "a friend needs one."
            )
        if passed_to_some:
            downgrades.append(
                "these variables were withheld from some confined friends but "
                "passed to others, because an adapter declares them in its own "
                f"pass list: {', '.join(passed_to_some)}. They are NOT in this "
                "run's withheld record."
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
