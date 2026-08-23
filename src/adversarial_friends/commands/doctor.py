"""`afriend doctor`: report which friends are usable and what each can
actually enforce.

Split out of cli.py.
"""

import argparse
from pathlib import Path
import shutil
import tempfile

from ..adapters import FriendSpec, build_argv, load_adapters
from ..claimschema import schema_path
from ..paths import ADAPTER_DIR
from ..roster import discover_clis


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_adapters(ADAPTER_DIR)
    found = discover_clis(registry, shutil.which)
    with tempfile.TemporaryDirectory(prefix="af-doctor-") as tmp_str:
        tmp = Path(tmp_str)
        prompt_file = tmp / "prompt.txt"
        prompt_file.write_text("", encoding="utf-8")
        schema_file = schema_path(tmp)
        for name, adapter in sorted(registry.items()):
            if adapter.transport == "http":
                # HTTP transport (ollama) is declared but not implemented in
                # this build -- `--friend ollama:*` is rejected outright
                # (see cliargs._specs_from_flags). Say so plainly here too,
                # rather than a neutral "reachability not probed" that reads
                # as "supported but unverified."
                print(
                    f"{name:10} {'unimplemented':13} http endpoint={adapter.endpoint} "
                    "(HTTP transport not implemented in this build)"
                )
                continue
            binary = shutil.which(adapter.binary) if adapter.binary else None
            # capability is always what build_argv reports for a
            # repo-scoped probe spec, never re-derived by hand -- this is
            # the same rule commands.run.cmd_run follows for real dispatch
            # (see dispatch._dispatch's docstring): doctor's whole point is
            # to tell the operator what a friend would actually receive.
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
            print(
                f"{name:10} {'found' if name in found else 'missing':8} "
                f"schema={cap.schema} readonly={cap.readonly} effort={cap.effort} "
                f"{binary or ''}"
            )
    return 0 if found else 3
