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
from ..adapters import Adapter, FriendSpec, friend_key
from ..cliargs import _specs_from_flags
from ..errors import NoFriendsError, UsageError
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

    # §8.1: --lens restricts which lenses discovery assigns. Unknown names
    # are refused rather than silently ignored -- a typo would otherwise
    # quietly shrink the run to whichever lenses happened to match.
    lenses = available_lenses()
    if getattr(args, "lens", None):
        known = set(lenses)
        unknown = [name for name in args.lens if name not in known]
        if unknown:
            raise UsageError(f"unknown lens(es) {sorted(unknown)}; available: {sorted(known)}")
        lenses = list(args.lens)
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
                lenses,
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
    # §17's --max-friends. Applied after resolution so it caps whatever
    # source produced the roster, and reported: a silently shortened roster
    # is a run with fewer independent judges than the operator thinks.
    limit = getattr(args, "max_friends", None)
    if limit is not None and len(specs) > limit:
        dropped = [s.name for s in specs[limit:]]
        specs = specs[:limit]
        downgrades.append(
            f"--max-friends={limit} dropped {dropped}; this run has fewer "
            "independent judges than the roster named."
        )

    # §10.1 layer 4: invocation flags outrank the roster and the preset.
    model = getattr(args, "model", None)
    effort = getattr(args, "effort", None)
    if model or effort:
        specs = [
            replace(
                s,
                model=model or s.model,
                effort=effort or s.effort,
            )
            for s in specs
        ]

    _refuse_duplicate_identities(specs, args.mode)
    return ResolvedRoster(specs=specs, preset=preset, source=roster_source)


def _refuse_duplicate_identities(specs: list[FriendSpec], mode: str) -> None:
    """Two entries that are the same (cli, lens, model, effort) are one
    ledger identity (§8.1). Where friends judge, that identity would cast
    two verdicts: quorum would count both, `latest_per_judge` keep one, and
    flag order decide which -- so it is refused before anything is spent,
    rather than downgraded into a run that cannot settle those claims. A
    `report` run has no judging, and asking the same friend twice there is a
    legitimate way to sample its variance.

    Called LAST, on the roster the run will actually use. Called before the
    preset filled efforts and before §10.1 layer 4's `--model`/`--effort`
    override, it missed every collision those layers create -- `--friend
    codex:ops:gpt-5 --friend codex:ops --model gpt-5` resolves to two
    friends with one identity -- and refused rosters whose duplicate entry
    `--max-friends` would have dropped before the run.
    """
    if mode == "report":
        return
    seen: dict[str, str] = {}
    for spec in specs:
        key = friend_key(spec)
        if key in seen:
            raise UsageError(
                f"friends {seen[key]!r} and {spec.name!r} are the same friend -- "
                f"cli {spec.cli!r}, lens {spec.lens!r}, model {spec.model!r}, effort "
                f"{spec.effort!r} -- and would share one ledger identity ({key}); "
                "give one a different lens, model, or effort"
            )
        seen[key] = spec.name
