"""Command line entry point. Subcommands are added by later tasks."""
import argparse
import sys

from . import __version__
from .errors import AfError, UsageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="af", add_help=True)
    parser.add_argument("--version", action="version", version=f"af {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    # argparse exits 2 on unknown args, which matches our usage-error code.
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            parser.print_help()
            return 0
        raise UsageError(f"unknown command: {args.command}")
    except AfError as exc:
        print(f"af: {exc}", file=sys.stderr)
        return exc.exit_code
