"""Read and mutate only the safe, declarative named review profiles."""

import argparse
from collections.abc import Mapping
import json

from .. import reviewprofiles, sessionconfig
from ..errors import UsageError

_SETTING_NAMES = (
    "mode",
    "preset",
    "max_friends",
    "require_friends",
    "timeout",
    "max_rounds",
    "max_calls",
    "max_wall_clock",
    "max_loop_iterations",
)


def _settings(args: argparse.Namespace) -> dict[str, object]:
    values = {
        name: getattr(args, name) for name in _SETTING_NAMES if getattr(args, name) is not None
    }
    if args.lens is not None:
        values["lenses"] = args.lens
    return values


def _custom_payload(name: str, definition: dict[str, object]) -> dict[str, object]:
    return {"name": name, **definition}


def cmd_profiles(args: argparse.Namespace) -> int:
    """Execute a named-profile query or atomic configuration mutation."""
    action = args.profiles_command
    config = sessionconfig.load()
    if action == "list":
        rows: list[dict[str, object]] = [
            {"name": name, "kind": "built-in", "mode": reviewprofiles.builtins()[name].mode}
            for name in reviewprofiles.names()
        ]
        for name, definition in sorted(config.profiles.items()):
            row: dict[str, object] = {"name": name, "kind": "custom"}
            row.update(definition)
            rows.append(row)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            for row in rows:
                detail = row["mode"] if "mode" in row else f"inherits {row['base']}"
                print(f"{row['name']}  {row['kind']}  {detail}")
        return 0
    if action == "show":
        builtin = reviewprofiles.get(args.name)
        if builtin is not None:
            payload: dict[str, object] = {
                "name": builtin.name,
                "kind": "built-in",
                "mode": builtin.mode,
            }
        else:
            custom_definition: Mapping[str, object] | None = config.profiles.get(args.name)
            if custom_definition is None:
                raise UsageError(f"unknown review profile {args.name!r}")
            payload = _custom_payload(args.name, dict(custom_definition))
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for key, value in payload.items():
                print(f"{key}: {value}")
        return 0
    if action == "create":
        values = _settings(args)
        if args.base is None:
            raise UsageError("profiles create requires --base NAME")
        sessionconfig.create_profile(args.name, args.base, values)
        print(f"created review profile {args.name}")
        return 0
    if action == "update":
        values = _settings(args)
        if args.base is None and not values:
            raise UsageError("profiles update requires --base or a safe setting")
        sessionconfig.update_profile(args.name, values, base=args.base)
        print(f"updated review profile {args.name}")
        return 0
    if action == "delete":
        sessionconfig.delete_profile(args.name)
        print(f"deleted review profile {args.name}")
        return 0
    if action == "set-default":
        sessionconfig.set_default(args.name)
        print(f"default review profile: {args.name}")
        return 0
    raise AssertionError(f"unhandled profiles action {action!r}")
