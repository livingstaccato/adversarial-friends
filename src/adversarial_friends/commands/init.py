"""`afriend init`: write a roster reflecting what is actually installed.

§17: it "probes `$PATH`, checks auth, reads each CLI's own config where the
format is known, and writes a commented roster reflecting discovered reality
-- a file to edit, not a wizard to answer."

So this asks nothing. It writes what the machine actually has, with the
reasoning in comments, and leaves the editing to a human who can see it all
at once. `afriend doctor` performs the same probe read-only.

It refuses to overwrite without `--force`, because the file it would replace
is one someone edited by hand.
"""

import argparse
from pathlib import Path
import shutil
import sys

from .. import providerconfig
from ..adapters import load_adapters
from ..authority import AuthorityPolicy
from ..errors import NoFriendsError, UsageError
from ..paths import ADAPTER_DIR
from ..prompt import available_lenses
from ..readiness import ReadinessState, assess_all
from ..rosterfile import default_roster_path, render


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.out) if args.out else default_roster_path()
    if target.exists() and not args.force:
        raise UsageError(
            f"{target} already exists. It is a file you are meant to edit, so "
            "this will not overwrite it; pass --force if that is what you want."
        )

    registry = load_adapters(ADAPTER_DIR)
    policy = providerconfig.load(registry)
    authority_policy = AuthorityPolicy.deny_all()
    readiness = assess_all(
        registry,
        policy,
        which=shutil.which,
        authority_policy=authority_policy,
    )
    eligible = {ReadinessState.READY, ReadinessState.REACHABLE_UNCONFIGURED}
    selected = [name for name, row in readiness.items() if row.state in eligible]
    if not selected:
        raise NoFriendsError(
            "no agent CLIs found on PATH, so there is nothing to write a roster "
            "from. Install at least two (claude, codex, agy, opencode) or run a "
            "local ollama, then try again."
        )

    lenses = available_lenses()
    notes: list[str] = []
    entries = []
    for index, cli in enumerate(selected):
        adapter = registry[cli]
        assessed = readiness[cli]
        lens = lenses[index % len(lenses)]
        entry: dict[str, object] = {
            "name": f"{cli}-{lens}",
            "cli": cli,
            "lens": lens,
            # Same rule the discovery path uses: a CLI with no read-only mode
            # of its own only ever sees the artifact.
            "scope": "repo" if adapter.readonly_argv else "doc",
        }
        if assessed.model is not None:
            entry["model"] = assessed.model
        if adapter.transport == "http" and assessed.model is None:
            # An HTTP friend is a bare model behind an endpoint and has no
            # default -- a roster naming one without a model would fail at
            # dispatch, so the placeholder is written and called out.
            entry["model"] = "CHANGE-ME"
            notes.append(
                f"{cli}: set a model. It is an HTTP endpoint with no default, "
                "so this entry will not run until you name one. It has no "
                "filesystem access of any kind, so it needs no confinement."
            )
        if not adapter.readonly_argv and adapter.transport != "http":
            # Only an adapter that SPAWNS something can be confined. An HTTP
            # friend is a bare model behind an endpoint with no subprocess and
            # no filesystem access at all -- telling the operator it runs
            # under a sandbox would describe a mechanism that never engages.
            notes.append(
                f"{cli}: no read-only mode, so it runs under OS confinement "
                "(§12.2) and is limited to doc scope."
            )
        if adapter.effort_kind == "unverified":
            notes.append(
                f"{cli}: effort cannot be verified -- its effort flag accepts "
                "any value silently, so a --preset makes no promise for it."
            )
        entries.append(entry)

    if len(entries) < 2:
        notes.append(
            "Only one friend was found. Cross-examination needs at least two "
            "independent friends; with one, a run is a single opinion."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(entries, notes), encoding="utf-8")
    print(target)
    print(
        f"wrote {len(entries)} friend(s) from what is installed. Edit it, then "
        f"run `afriend run <artifact>` -- it is picked up automatically.",
        file=sys.stderr,
    )
    return 0
