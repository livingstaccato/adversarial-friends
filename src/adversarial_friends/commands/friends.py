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
from collections.abc import Callable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil

from .. import providerconfig, rosterfile
from ..adapters import Adapter, FriendSpec, validate_roster_uniqueness
from ..authority import ExternalToolPolicy, enforce
from ..cliargs import _specs_from_flags
from ..errors import NoFriendsError, UsageError
from ..ids import validate_friend_name
from ..presets import default_preset, effort_for, no_effort_note, unverifiable_note
from ..prompt import available_lenses
from ..readiness import DenyProbeResult, ReadinessState, assess_all
from ..roster import DEGRADED_MODES, apply_capacity, resolve


@dataclass
class ResolvedRoster:
    specs: list[FriendSpec]
    preset: str
    source: str | None = None


def validate_resume_capabilities(
    specs: list[FriendSpec],
    registry: dict[str, Adapter],
    external_tool_policy: ExternalToolPolicy,
    *,
    which: Callable[[str], str | None] = shutil.which,
    capability_probe: Callable[[Adapter, str], DenyProbeResult] | None = None,
) -> None:
    """Revalidate mutable executable authority for a frozen resume roster.

    Identity, model and ordering remain frozen. Provider enablement, host
    exclusion and capacity are discovery policy and deliberately do not run
    again. The executable and its deny flags are mutable local facts, though,
    so a resume must prove them again before opening the run for mutation.
    """
    names = dict.fromkeys(spec.cli for spec in specs if spec.cli != "fake")
    if not names:
        return
    selected: dict[str, Adapter] = {}
    for name in names:
        adapter = registry.get(name)
        if adapter is None:
            raise UsageError(f"unknown cli in saved roster: {name!r}")
        selected[name] = adapter
    rows = assess_all(
        selected,
        providerconfig.ProviderPolicy({}),
        env=os.environ,
        which=which,
        include_self=True,
        external_tool_policy=external_tool_policy,
        selection_policy=False,
        capability_probe=capability_probe,
    )
    models = {spec.cli: spec.model for spec in specs}
    for name, row in rows.items():
        configured_http = (
            row.state is ReadinessState.REACHABLE_UNCONFIGURED and models[name] is not None
        )
        if not row.ready and not configured_http:
            raise UsageError(
                f"cannot resume: saved provider {name!r} is {row.state.value}: {row.reason}"
            )


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
    external_tool_policy: ExternalToolPolicy | None = None,
) -> ResolvedRoster:
    """Apply §10.1's precedence and return the roster a run will use."""
    if external_tool_policy is None:
        external_tool_policy = (
            ExternalToolPolicy.ALLOW
            if getattr(args, "allow_external_tools", False)
            else ExternalToolPolicy.DENY
        )
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
    explicit = bool(args.friend)
    if explicit:
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
                external_tool_policy=external_tool_policy,
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
                external_tool_policy=external_tool_policy,
            )
    if not specs:
        raise NoFriendsError(f"no usable friends for mode {args.mode!r}")
    if explicit:
        # Naming a friend overrides automatic enabled/host/discovery
        # selection, not whether that friend can actually be dispatched.
        # Assess every explicitly named provider exactly once before capacity:
        # an unavailable prefix must not hide a ready friend later in the
        # operator's ordered roster.
        explicit_names = dict.fromkeys(spec.cli for spec in specs if spec.cli != "fake")
        readiness = assess_all(
            {name: registry[name] for name in explicit_names},
            provider_policy,
            env=os.environ,
            which=shutil.which,
            include_self=True,
            external_tool_policy=external_tool_policy,
            selection_policy=False,
        )
        checked: list[FriendSpec] = []
        rejected: list[str] = []
        for spec in specs:
            if spec.cli == "fake":
                checked.append(spec)
                continue
            row = readiness[spec.cli]
            effective_model = invocation_model or spec.model or row.model
            configured_http = (
                row.state is ReadinessState.REACHABLE_UNCONFIGURED and effective_model is not None
            )
            if not row.ready and not configured_http:
                if row.state is ReadinessState.POLICY_BLOCKED:
                    raise UsageError(row.reason)
                rejected.append(f"{spec.name} ({spec.cli}): {row.reason}")
                continue
            checked.append(replace(spec, model=effective_model))
        if rejected and not checked:
            raise NoFriendsError("explicit friend preflight failed: " + "; ".join(rejected))
        if rejected:
            downgrades.append(
                "explicit friend preflight skipped unavailable entries: " + "; ".join(rejected)
            )
        specs = checked
    # Capacity applies to the dispatch-ready roster. A discarded or unready
    # friend never runs, so it cannot consume capacity or contribute preset
    # limitations to the surviving run.
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

    validate_roster_uniqueness(specs, judging=args.mode != "report")
    for spec in specs:
        if spec.cli != "fake":
            enforce(registry[spec.cli], external_tool_policy)
    return ResolvedRoster(specs=specs, preset=preset, source=roster_source)


def roster_for_run(
    args: argparse.Namespace,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    downgrades: list[str],
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY,
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
        validate_resume_capabilities(specs, registry, external_tool_policy)
    else:
        resolved = resolve_friends(args, registry, fake_cmd, downgrades, external_tool_policy)
        specs = resolved.specs

    # The concrete final roster, whether freshly resolved or restored from
    # run.json, owns output paths and (in judging modes) ledger identities.
    # Frozen resume data must not bypass either invariant.
    validate_roster_uniqueness(specs, judging=args.mode != "report")
    for spec in specs:
        validate_friend_name(spec.name)
        if spec.cli != "fake":
            adapter = registry.get(spec.cli)
            if adapter is None and registry:
                raise UsageError(f"unknown cli in saved roster: {spec.cli!r}")
            if adapter is not None:
                enforce(adapter, external_tool_policy)

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
