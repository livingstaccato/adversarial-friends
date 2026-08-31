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

from .. import providerconfig, rosterfile
from ..adapters import Adapter, FriendSpec, friend_key
from ..cliargs import _specs_from_flags
from ..errors import NoFriendsError, UsageError
from ..ids import validate_friend_name
from ..presets import default_preset, effort_for, no_effort_note, unverifiable_note
from ..prompt import available_lenses
from ..roster import DEGRADED_MODES, apply_capacity, resolve


@dataclass
class ResolvedRoster:
    specs: list[FriendSpec]
    preset: str
    source: str | None = None


def _validated_selection_args(
    args: argparse.Namespace, registry: dict[str, Adapter]
) -> tuple[set[str], set[str], str | None, list[str]]:
    """Validate selection controls without assessing current providers."""
    enabled = set(getattr(args, "enable_provider", []))
    disabled = set(getattr(args, "disable_provider", []))
    contradictory = enabled & disabled
    if contradictory:
        raise UsageError(
            f"provider(s) {sorted(contradictory)} were passed to both "
            "--enable-provider and --disable-provider"
        )
    provider_unknown = (enabled | disabled) - set(registry)
    if provider_unknown:
        raise UsageError(
            f"unknown provider(s) {sorted(provider_unknown)}; known: {sorted(registry)}"
        )
    host_provider = getattr(args, "host_provider", None)
    if host_provider is not None and host_provider not in registry:
        raise UsageError(f"unknown --host-provider {host_provider!r}; known: {sorted(registry)}")

    lenses = available_lenses()
    if getattr(args, "lens", None):
        known = set(lenses)
        unknown = [name for name in args.lens if name not in known]
        if unknown:
            raise UsageError(f"unknown lens(es) {sorted(unknown)}; available: {sorted(known)}")
        lenses = list(args.lens)
    return enabled, disabled, host_provider, lenses


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
    enabled, disabled, host_provider, lenses = _validated_selection_args(args, registry)
    provider_policy = providerconfig.load(registry, os.environ)
    if enabled or disabled:
        settings = dict(provider_policy.providers)
        for name in enabled | disabled:
            current = provider_policy.setting(name)
            settings[name] = providerconfig.ProviderSetting(
                enabled=name in enabled,
                model=current.model,
            )
        provider_policy = providerconfig.ProviderPolicy(settings)
    invocation_model = getattr(args, "model", None)
    if invocation_model is not None:
        # Invocation flags are §10.1's strongest layer. Apply the global
        # model before readiness so a reachable HTTP provider is not rejected
        # as unconfigured before that stronger layer gets a chance to fill it.
        provider_policy = providerconfig.ProviderPolicy(
            {
                name: providerconfig.ProviderSetting(
                    enabled=provider_policy.setting(name).enabled,
                    model=invocation_model,
                )
                for name in registry
            }
        )

    # §8.1: --lens restricts which lenses discovery assigns. Unknown names
    # are refused rather than silently ignored -- a typo would otherwise
    # quietly shrink the run to whichever lenses happened to match.
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
                provider_policy=provider_policy,
                host_provider=host_provider,
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
                provider_policy=provider_policy,
                host_provider=host_provider,
            )
    if not specs:
        raise NoFriendsError(f"no usable friends for mode {args.mode!r}")
    # Capacity applies to the resolved, ready roster before per-friend
    # diagnostics. A discarded friend never runs, so its preset limitations
    # must not be reported as limitations of the run that remains.
    limit = getattr(args, "max_friends", None)
    specs, dropped_specs = apply_capacity(specs, limit)
    if dropped_specs:
        dropped = [spec.name for spec in dropped_specs]
        downgrades.append(
            f"--max-friends={limit} dropped {dropped}; this run has fewer "
            "independent judges than the roster named."
        )
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


def roster_for_run(
    args: argparse.Namespace,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    downgrades: list[str],
) -> tuple[ResolvedRoster, list[FriendSpec]]:
    """The roster this run will actually dispatch, and the refusals that
    come with it.

    Separated from `cmd_run` when it crossed the then-current line cap. It
    is also one concern: which friends run, decided in one place, including
    the two rules that can stop a run before anything is spent -- §8.3's
    minimum and a resumed run's recorded roster.
    """
    # A resumed run judges with the roster its ledger was written against.
    resume_roster = getattr(args, "_resume_roster", None)
    if resume_roster is not None:
        _validated_selection_args(args, registry)
        specs = list(resume_roster)
        resume_meta = getattr(args, "_resume_meta", {}) or {}
        resolved = ResolvedRoster(
            specs=specs,
            preset=args.preset or default_preset(args.mode),
            source=resume_meta.get("roster_source"),
        )
    else:
        resolved = resolve_friends(args, registry, fake_cmd, downgrades)
        specs = resolved.specs

    for spec in specs:
        validate_friend_name(spec.name)

    if len(specs) < 2:
        # §8.3. --friend REPLACES the roster rather than augmenting
        # discovery (see cliargs._specs_from_flags), so a single --friend
        # flag -- or discovery itself resolving to one friend -- produces a
        # run that cannot cross-examine anything.
        #
        # `report` is allowed to run and say so. Every other mode is
        # refused, because "cross-examination with one participant is a
        # different and weaker thing wearing the same name": with no judge
        # independent of any claim, a `gate` run settles nothing, blocks on
        # nothing, and exits 0 -- CI reads "gate clear" from a run that
        # structurally could not check anything. This was a downgrade note
        # for every mode until a crossexam of this file found the exit-0
        # gate and the DEGRADED_MODES constant that was wired to nothing.
        if args.mode not in DEGRADED_MODES:
            raise NoFriendsError(
                f"only one friend ({specs[0].name}) resolved, and mode "
                f"{args.mode!r} needs at least two independent friends "
                "(§8.3). Install a second agent CLI, add a local model "
                "(`--friend ollama:<lens>:<model>`), or use --mode report "
                "for a single reviewer's opinion."
            )
        downgrades.append(
            f"only one friend ({specs[0].name}) resolved for this run; "
            "cross-examination needs at least two independent friends, so "
            "this report reflects a single reviewer's opinion, not "
            "disagreement between several."
        )
    return resolved, specs
