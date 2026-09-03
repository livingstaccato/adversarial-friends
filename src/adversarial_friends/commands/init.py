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
from collections.abc import Iterator
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .. import providerconfig, reviewprofiles, sessionconfig
from ..adapters import Adapter, load_adapters
from ..authority import AuthorityPolicy
from ..errors import NoFriendsError, UsageError
from ..paths import ADAPTER_DIR
from ..prompt import available_lenses
from ..readiness import ReadinessState, assess_all, detect_host, effective_host_inclusion
from ..rosterfile import default_roster_path, render

_GUIDED_SETUP_SCHEMA_VERSION = 1


def cmd_init(args: argparse.Namespace) -> int:
    if getattr(args, "apply", False) and not getattr(args, "guided", False):
        raise UsageError("--apply requires --guided")
    if getattr(args, "guided", False):
        return _cmd_guided_init(args)

    target, count = _write_roster(args)
    print(target)
    print(
        f"wrote {count} friend(s) from what is installed. Edit it, then "
        f"run `afriend run <artifact>` -- it is picked up automatically.",
        file=sys.stderr,
    )
    return 0


def _roster_target(args: argparse.Namespace) -> Path:
    target = Path(args.out) if args.out else default_roster_path()
    if target.exists() and not args.force:
        raise UsageError(
            f"{target} already exists. It is a file you are meant to edit, so "
            "this will not overwrite it; pass --force if that is what you want."
        )
    return target


def _write_roster(args: argparse.Namespace, *, target: Path | None = None) -> tuple[Path, int]:
    """Write the normal discovered roster without choosing a user-facing stream."""
    target = _roster_target(args) if target is None else target
    contents, count = _render_roster(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return target, count


def _render_roster(args: argparse.Namespace) -> tuple[str, int]:
    """Perform normal discovery and return a roster before anything is written."""

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

    return render(entries, notes), len(entries)


@contextmanager
def _staged_roster(target: Path, contents: str) -> Iterator[Path]:
    """Durably stage a roster beside its target without making it visible yet."""
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary
    except OSError as exc:
        raise UsageError(f"{target}: cannot stage roster: {exc}") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


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

    applying = bool(getattr(args, "apply", False))
    target = _roster_target(args) if applying else None
    files_before: dict[Path, bool] = {}
    if default_profile is not None:
        session_path = sessionconfig.config_path()
        files_before[session_path] = session_path.exists()
    if provider_changes:
        provider_path = providerconfig.config_path()
        files_before[provider_path] = provider_path.exists()
    if target is not None:
        files_before[target] = target.exists()

    if applying:
        # Discovery happens first: an empty or otherwise invalid roster is a
        # refusal before any preference document can be changed.
        roster_contents, _ = _render_roster(args)
        assert target is not None
        with _staged_roster(target, roster_contents) as staged:
            # Parse every configuration document needed by this transaction
            # before changing either file. A malformed pre-existing config is
            # therefore a safe refusal, never a reason to apply only the
            # earlier half.
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

            # The roster is staged before any setter runs, so discovery or
            # staging failure cannot persist setup. The two existing config
            # APIs atomically replace their individual files, but cannot make
            # a cross-file transaction with this final rename: an OS failure
            # during a later config write or this rename may leave already
            # committed selected settings without a new roster. We do not
            # roll back by overwriting a concurrent user edit.
            try:
                staged.replace(target)
                _fsync_directory(target.parent)
            except OSError as exc:
                raise UsageError(f"{target}: cannot finalize roster: {exc}") from exc

    policy = providerconfig.load(known)
    profiles = [
        {"name": name, "mode": reviewprofiles.builtins()[name].mode}
        for name in reviewprofiles.names()
    ]
    host = _guided_host_role()
    providers = _guided_provider_rows(registry, policy)
    payload = {
        "schema_version": _GUIDED_SETUP_SCHEMA_VERSION,
        "guided": True,
        "apply": applying,
        "changes": changes,
        "external_tools": "denied",
        "profiles": profiles,
        "host": host,
        "providers": providers,
    }
    created: list[str] = []
    changed: list[str] = []
    if applying:
        for path, existed in files_before.items():
            if path.exists():
                (changed if existed else created).append(str(path))
        payload["created_files"] = sorted(created)
        payload["changed_files"] = sorted(changed)
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    phase = "applied" if payload["apply"] else "preview"
    print(f"guided setup {phase}:", file=sys.stderr)
    print(f"  schema version: {_GUIDED_SETUP_SCHEMA_VERSION}", file=sys.stderr)
    profile_text = ", ".join(f"{item['name']} ({item['mode']})" for item in profiles)
    print(f"  profiles: {profile_text}", file=sys.stderr)
    if host is not None:
        print(
            f"  host: {host['provider']} ({host['role']}; advisory={host['advisory']}; "
            f"independent={host['independent']})",
            file=sys.stderr,
        )
    print("  providers:", file=sys.stderr)
    for row in providers:
        print(
            f"    {row['name']}: {row['readiness']} "
            f"(enabled={row['enabled']}; discovered={row['discovered']})",
            file=sys.stderr,
        )
    if default_profile is not None:
        print(f"  default profile: {default_profile}", file=sys.stderr)
    for name in sorted(enabled):
        print(f"  enable provider: {name}", file=sys.stderr)
    for name in sorted(disabled):
        print(f"  disable provider: {name}", file=sys.stderr)
    if ollama_model is not None:
        print(f"  Ollama model: {ollama_model}", file=sys.stderr)
    if not changes:
        print("  no configuration changes selected", file=sys.stderr)
    print(
        "  external tools remain denied; no external tools were enabled or used",
        file=sys.stderr,
    )
    if not payload["apply"]:
        print(
            "  no files were written; rerun with --apply to persist these changes", file=sys.stderr
        )
    else:
        for output_path in created:
            print(f"  created: {output_path}", file=sys.stderr)
        for output_path in changed:
            print(f"  changed: {output_path}", file=sys.stderr)
        print("  first review: afriend run <artifact>", file=sys.stderr)
    return 0


def _guided_host_role() -> dict[str, object] | None:
    """Describe host advisory status without discovering or running a provider."""
    host = detect_host(os.environ)
    if host is None:
        return None
    return {
        "provider": host,
        "role": "host-self-review" if host == "codex" else "host-provider",
        "advisory": host == "codex",
        "independent": False,
    }


def _guided_provider_rows(
    registry: dict[str, Adapter], policy: providerconfig.ProviderPolicy
) -> list[dict[str, object]]:
    """Give setup static discovery details without spawning or probing providers."""
    rows: list[dict[str, object]] = []
    for name, adapter in sorted(registry.items()):
        setting = policy.setting(name)
        discovered = bool(adapter.binary and shutil.which(adapter.binary))
        if not setting.enabled:
            readiness = "disabled"
        elif adapter.transport == "http":
            readiness = "not-probed"
        else:
            readiness = "available" if discovered else "unavailable"
        rows.append(
            {
                "name": name,
                "enabled": setting.enabled,
                "model": setting.model,
                "transport": adapter.transport,
                "discovered": discovered,
                "readiness": readiness,
            }
        )
    return rows
