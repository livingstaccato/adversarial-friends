"""`afriend doctor`: report which friends are usable and what each can
actually enforce.

Split out of cli.py.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from .. import http_transport, providerconfig
from ..adapters import Adapter, Capability, FriendSpec, build_argv, load_adapters
from ..authority import AuthorityPolicy, enforce
from ..claimschema import schema_path
from ..paths import ADAPTER_DIR
from ..readiness import FriendReadiness, ReadinessState, assess_all
from ..runstore import default_root


def _legacy_status(row: FriendReadiness, adapter: Adapter) -> str:
    if row.ready or row.state is ReadinessState.REACHABLE_UNCONFIGURED:
        return "found"
    if row.state is ReadinessState.UNAVAILABLE:
        return "unreachable" if adapter.transport == "http" else "missing"
    return row.state.value


def _gc(root: Path) -> tuple[int, list[str]]:
    """§17: remove worktrees and run directories left by abandoned runs.

    A run directory is abandoned when it holds no report.md -- every path
    out of cmd_run writes one, including the orchestrator halt and every
    failure mode, so its absence means the process died before finishing.
    A halted run therefore survives GC, which is the point: it is waiting
    for a RESPONSE.json, not abandoned.

    Kept isolation directories (--keep) go with their run: they only exist
    inside one, and keeping the run while deleting what it kept would be
    the wrong half.
    """
    removed: list[str] = []
    if not root.is_dir():
        return 0, removed
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("run-"):
            continue
        if (entry / "report.md").is_file():
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    return len(removed), removed


def _rows(
    registry: dict[str, Adapter],
    readiness: dict[str, FriendReadiness],
    tmp: Path,
    authority_policy: AuthorityPolicy,
) -> list[dict[str, Any]]:
    prompt_file = tmp / "prompt.txt"
    prompt_file.write_text("", encoding="utf-8")
    schema_file = schema_path(tmp)
    rows = []
    for name, adapter in sorted(registry.items()):
        assessed = readiness[name]
        if assessed.state is ReadinessState.DISABLED:
            # Readiness deliberately decides disabled rows before authority
            # and availability. Preserve that decision: constructing argv
            # here would both contradict the state and crash on an
            # uncontrolled adapter. With no invocation there is no enforced
            # decision, so report the conservative declaration itself.
            cap = Capability(
                False,
                False,
                adapter.effort_kind,
                external_tools=adapter.external_tools,
                external_tool_sources=adapter.external_tool_sources,
                deny_external_tools_argv=adapter.deny_external_tools_argv,
            )
            rows.append(
                {
                    "name": name,
                    "status": _legacy_status(assessed, adapter),
                    "state": assessed.state.value,
                    "reason": assessed.reason,
                    "schema": cap.schema,
                    "readonly": cap.readonly,
                    "effort": cap.effort,
                    "external_tools": cap.external_tools,
                    "where": assessed.where,
                    "model": assessed.model,
                    "auth_classifiable": adapter.auth.declared(),
                }
            )
            continue
        if assessed.state is ReadinessState.POLICY_BLOCKED:
            cap = Capability(False, False, adapter.effort_kind)
            rows.append(
                {
                    "name": name,
                    "status": _legacy_status(assessed, adapter),
                    "state": assessed.state.value,
                    "reason": assessed.reason,
                    "schema": cap.schema,
                    "readonly": cap.readonly,
                    "effort": cap.effort,
                    "external_tools": cap.external_tools,
                    "where": assessed.where,
                    "model": assessed.model,
                    "auth_classifiable": adapter.auth.declared(),
                }
            )
            continue
        if adapter.transport == "http":
            # Capability comes from the same source real dispatch uses;
            # readiness was already assessed once before this projection.
            cap = http_transport.capability_for(
                adapter, enforce(adapter, authority_policy.for_provider(name))
            )
            rows.append(
                {
                    "name": name,
                    "status": _legacy_status(assessed, adapter),
                    "state": assessed.state.value,
                    "reason": assessed.reason,
                    "schema": cap.schema,
                    "readonly": cap.readonly,
                    "effort": cap.effort,
                    "external_tools": cap.external_tools,
                    "where": assessed.where,
                    "model": assessed.model,
                    "auth_classifiable": adapter.auth.declared(),
                }
            )
            continue
        # capability is always what build_argv reports for a repo-scoped
        # probe spec, never re-derived by hand -- the same rule real
        # dispatch follows. doctor's whole point is to tell the operator
        # what a friend would actually receive.
        probe = FriendSpec(
            name=f"doctor-{name}",
            cli=name,
            lens="doctor",
            model=None,
            effort=None,
            scope="repo",
            timeout=1,
        )
        _, _, cap = build_argv(
            adapter, probe, prompt_file, schema_file, authority_policy.for_provider(name)
        )
        rows.append(
            {
                "name": name,
                "status": _legacy_status(assessed, adapter),
                "state": assessed.state.value,
                "reason": assessed.reason,
                "schema": cap.schema,
                "readonly": cap.readonly,
                "effort": cap.effort,
                "external_tools": cap.external_tools,
                "where": assessed.where,
                "model": assessed.model,
                "auth_classifiable": adapter.auth.declared(),
            }
        )
    return rows


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_adapters(ADAPTER_DIR)
    policy = providerconfig.load(registry)
    authority_policy = AuthorityPolicy.deny_all()
    readiness = assess_all(
        registry,
        policy,
        which=shutil.which,
        authority_policy=authority_policy,
    )
    collected: list[str] = []
    if getattr(args, "gc", False):
        _count, collected = _gc(Path(args.out) if getattr(args, "out", None) else default_root())
    with tempfile.TemporaryDirectory(prefix="af-doctor-") as tmp_str:
        rows = _rows(registry, readiness, Path(tmp_str), authority_policy)
    usable = sum(row.ready for row in readiness.values())

    if getattr(args, "json", False):
        print(
            json.dumps(
                {"friends": rows, "collected": collected, "usable": usable},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for row in rows:
            print(
                f"{row['name']:10} {row['status']:12} state={row['state']} "
                f"schema={row['schema']} readonly={row['readonly']} "
                f"effort={row['effort']} external_tools={row['external_tools']} "
                f"where={row['where']} "
                f"model={row['model']} reason={row['reason']}"
            )
        for name in collected:
            print(f"collected abandoned run: {name}", file=sys.stderr)

    return 0 if usable else 3
