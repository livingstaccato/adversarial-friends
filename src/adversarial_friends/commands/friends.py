"""Deciding who a run's friends are -- spec §10.1, §13.

Split out of commands/run.py for the line cap. It is also a genuinely
separate decision from running them: four sources can contribute, and §10.1
orders them strongest-last.

    1. the friend's own config   <- default: emit no model/effort flags
    2. --preset
    3. a roster file
    4. --friend

Each layer only fills what the one above left unset, so an operator can keep
a roster and still override a single run from the command line.
"""

import argparse
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil

from .. import rosterfile
from ..adapters import Adapter, FriendSpec
from ..cliargs import _specs_from_flags
from ..errors import NoFriendsError
from ..presets import default_preset, effort_for, no_effort_note, unverifiable_note
from ..prompt import available_lenses
from ..roster import resolve


@dataclass
class ResolvedRoster:
    specs: list[FriendSpec]
    preset: str
    source: str | None = None


def resolve_friends(
    args: argparse.Namespace,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    downgrades: list[str],
) -> ResolvedRoster:
    """Apply §10.1's precedence and return the roster a run will use."""
    # §10.1's precedence, strongest last: adapter defaults, then --preset,
    # then a roster file, then --friend. Each layer only fills what the one
    # above it left unset, so an operator can keep a roster and still
    # override one run from the command line.
    preset = args.preset or default_preset(args.mode)
    roster_source: str | None = None
    if args.friend:
        specs = _specs_from_flags(args.friend, args.timeout, registry, bool(fake_cmd))
        if args.roster:
            downgrades.append(
                "both --friend and --roster were given; --friend replaces the "
                "roster entirely (§10.1), so the roster file was not read."
            )
    else:
        # §13: an explicitly named roster may live anywhere. Only the trusted
        # user-level path is ever picked up on its own -- a cloned repo must
        # not be able to choose who reviews it.
        roster_path = Path(args.roster) if args.roster else rosterfile.discover()
        if roster_path is not None:
            specs = resolve(
                registry,
                available_lenses(),
                os.environ,
                shutil.which,
                include_self=args.include_self,
                overrides=rosterfile.load(roster_path),
                timeout=args.timeout,
            )
            roster_source = str(roster_path)
        else:
            specs = resolve(
                registry,
                available_lenses(),
                os.environ,
                shutil.which,
                include_self=args.include_self,
                timeout=args.timeout,
            )
    if not specs:
        raise NoFriendsError(f"no usable friends for mode {args.mode!r}")

    # The preset fills effort only where nothing stronger set it, so a roster
    # entry's own `effort` wins -- that is what makes preset weaker than
    # roster in §10.1's order rather than merely different.
    if preset != "inherit":
        filled = []
        for spec in specs:
            adapter = registry.get(spec.cli)
            if adapter is None or spec.effort is not None:
                filled.append(spec)
                continue
            for note in (
                unverifiable_note(preset, adapter),
                no_effort_note(preset, adapter),
            ):
                if note and note not in downgrades:
                    downgrades.append(note)
            filled.append(replace(spec, effort=effort_for(preset, adapter)))
        specs = filled
    return ResolvedRoster(specs=specs, preset=preset, source=roster_source)
