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

from .. import http_transport
from ..adapters import Adapter, FriendSpec, build_argv, load_adapters
from ..claimschema import schema_path
from ..paths import ADAPTER_DIR
from ..roster import discover_clis
from ..runstore import default_root


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


def _rows(registry: dict[str, Adapter], found: list[str], tmp: Path) -> list[dict[str, Any]]:
    prompt_file = tmp / "prompt.txt"
    prompt_file.write_text("", encoding="utf-8")
    schema_file = schema_path(tmp)
    rows = []
    for name, adapter in sorted(registry.items()):
        if adapter.transport == "http":
            # "Available" for an HTTP friend means a reachable endpoint, so
            # this probes rather than checking PATH. The capability comes
            # from http_transport, the same source real dispatch uses --
            # doctor must never compute it a second way.
            cap = http_transport.capability_for(adapter)
            reachable = bool(adapter.endpoint) and http_transport.probe(adapter.endpoint)
            rows.append(
                {
                    "name": name,
                    "status": "found" if reachable else "unreachable",
                    "schema": cap.schema,
                    "readonly": cap.readonly,
                    "effort": cap.effort,
                    "where": adapter.endpoint,
                    "auth_classifiable": adapter.auth.declared(),
                }
            )
            continue
        binary = shutil.which(adapter.binary) if adapter.binary else None
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
        _, _, cap = build_argv(adapter, probe, prompt_file, schema_file)
        rows.append(
            {
                "name": name,
                "status": "found" if name in found else "missing",
                "schema": cap.schema,
                "readonly": cap.readonly,
                "effort": cap.effort,
                "where": binary or "",
                "auth_classifiable": adapter.auth.declared(),
            }
        )
    return rows


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_adapters(ADAPTER_DIR)
    found = discover_clis(registry, shutil.which)
    collected: list[str] = []
    if getattr(args, "gc", False):
        _count, collected = _gc(Path(args.out) if getattr(args, "out", None) else default_root())
    with tempfile.TemporaryDirectory(prefix="af-doctor-") as tmp_str:
        rows = _rows(registry, found, Path(tmp_str))

    if getattr(args, "json", False):
        print(
            json.dumps(
                {"friends": rows, "collected": collected, "usable": len(found)},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for row in rows:
            print(
                f"{row['name']:10} {row['status']:12} "
                f"schema={row['schema']} readonly={row['readonly']} "
                f"effort={row['effort']} {row['where']}"
            )
        for name in collected:
            print(f"collected abandoned run: {name}", file=sys.stderr)

    return 0 if found else 3
