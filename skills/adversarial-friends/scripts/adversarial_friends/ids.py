"""Claim identity and friend-name validation.

Claim ids carry a version (`c-0007@2`) so that a verdict can never be
ambiguous about which wording of a claim it judged. Friend names become path
components under the run directory, so a name that escapes the run directory
is a security problem rather than a typo.
"""
import re

from .errors import UsageError

CLAIM_ID_RE = re.compile(r"^c-(\d{4,})@(\d+)$")
FRIEND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def format_claim_id(n: int, version: int = 1) -> str:
    return f"c-{n:04d}@{version}"


def parse_claim_id(cid: str) -> tuple[int, int]:
    match = CLAIM_ID_RE.match(cid)
    if match is None:
        raise UsageError(f"malformed claim id: {cid!r} (expected e.g. c-0007@1)")
    return int(match.group(1)), int(match.group(2))


def bump_claim_id(cid: str) -> str:
    number, version = parse_claim_id(cid)
    return format_claim_id(number, version + 1)


def base_claim_id(cid: str) -> str:
    number, _ = parse_claim_id(cid)
    return f"c-{number:04d}"


def validate_friend_name(name: str) -> str:
    if FRIEND_NAME_RE.match(name) is None:
        raise UsageError(
            f"invalid friend name {name!r}: must match {FRIEND_NAME_RE.pattern}"
        )
    return name
