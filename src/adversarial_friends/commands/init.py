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
import json
import os
from pathlib import Path
import shutil
import sys

from .. import providerconfig, reviewprofiles, sessionconfig
from ..adapters import load_adapters
from ..authority import AuthorityPolicy
from ..errors import NoFriendsError, UsageError
from ..paths import ADAPTER_DIR
from ..prompt import available_lenses
from ..readiness import ReadinessState, assess_all, detect_host, effective_host_inclusion
from ..rosterfile import default_roster_path, render


def cmd_init(args: argparse.Namespace) -> int:
    if getattr(args, "apply", False) and not getattr(args, "guided", False):
        raise UsageError("--apply requires --guided")
    if getattr(args, "guided", False):
        return _cmd_guided_init(args)

    target = Path(args.out) if args.out else default_roster_path()
    if target.exists() and not args.force:
        raise UsageError(
            f"{target} already exists. It is a file you are meant to edit, so "
            "this will not overwrite it; pass --force if that is what you want."
        )

    registry = load_adapters(ADAPTER_DIR)
    policy = providerconfig.load(registry)
    authority_policy = AuthorityPolicy.deny_all()
    include_self = effective_host_inclusion(detect_host(os.environ))
    readiness = assess_all(
        registry,
        policy,
        which=shutil.which,
        include_self=include_self,
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


def _cmd_guided_init(args: argparse.Namespace) -> int:
    """Preview or persist only explicitly selected, local setup defaults."""
    registry = load_adapters(ADAPTER_DIR)
    known = set(registry)
    default_profile = getattr(args, "default_profile", None)
    enabled = set(getattr(args, "enable_provider", []))
    disabled = set(getattr(args, "disable_provider", []))
    ollama_model = getattr(args, "ollama_model", None)

    if default_profile is not None and reviewprofiles.get(default_profile) is None:
        raise UsageError(
            f"default profile must be one of {list(reviewprofiles.names())}; got {default_profile!r}"
        )
    for name in sorted(enabled | disabled):
        if name not in known:
            raise UsageError(f"provider must be one of {sorted(known)}; got {name!r}")
    conflict = enabled & disabled
    if conflict:
        raise UsageError(f"provider cannot be both enable and disable: {sorted(conflict)}")
    if ollama_model is not None:
        if "ollama" not in enabled:
            raise UsageError("--ollama-model requires --enable-provider ollama")
        # Validate before any configuration write, using the provider config's
        # established model contract rather than accepting a guided-only form.
        providerconfig._validate_model(
            providerconfig.config_path(), "providers.ollama.model", ollama_model
        )

    provider_changes: dict[str, dict[str, object]] = {}
    for name in sorted(enabled):
        provider_changes[name] = {"enabled": True}
    for name in sorted(disabled):
        provider_changes[name] = {"enabled": False}
    if ollama_model is not None:
        provider_changes["ollama"]["model"] = ollama_model
    changes: dict[str, object] = {}
    if default_profile is not None:
        changes["session"] = {"default_profile": default_profile}
    if provider_changes:
        changes["providers"] = provider_changes

    if getattr(args, "apply", False):
        # Parse every configuration document needed by this transaction before
        # changing either file. A malformed pre-existing config is therefore
        # a safe refusal, never a reason to apply only the earlier half.
        if default_profile is not None:
            sessionconfig.load(reviewprofiles.names())
        if provider_changes:
            providerconfig.load(known)
        if default_profile is not None:
            sessionconfig.set_default(default_profile, known=reviewprofiles.names())
        for name in sorted(enabled):
            providerconfig.set_enabled(name, True, known=known)
        for name in sorted(disabled):
            providerconfig.set_enabled(name, False, known=known)
        if ollama_model is not None:
            providerconfig.set_model("ollama", ollama_model, known=known)

    payload = {
        "guided": True,
        "apply": bool(getattr(args, "apply", False)),
        "changes": changes,
        "external_tools": "denied",
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    phase = "applied" if payload["apply"] else "preview"
    print(f"guided setup {phase}:")
    if default_profile is not None:
        print(f"  default profile: {default_profile}")
    for name in sorted(enabled):
        print(f"  enable provider: {name}")
    for name in sorted(disabled):
        print(f"  disable provider: {name}")
    if ollama_model is not None:
        print(f"  Ollama model: {ollama_model}")
    if not changes:
        print("  no configuration changes selected")
    print("  external tools remain denied; no external tools were enabled or used")
    if not payload["apply"]:
        print("  no files were written; rerun with --apply to persist these changes")
    return 0
