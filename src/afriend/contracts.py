"""What a well-formed friend payload looks like, as data.

`normalize` does the hard part of reading untrusted agent stdout: stripping
terminal control codes, unwrapping a CLI's own JSON envelope, scanning for
balanced objects inside prose, repairing trailing commas, and ranking every
candidate it finds so a substantive-but-broken critique always outranks a
trivially clean scrap. None of that is specific to *claims* -- but all of it
was written against the claim schema, so a second payload kind (a judge's
verdicts, in crossexam) would have had to either duplicate that machinery or
go without it.

A PayloadContract is the small part that genuinely differs between payload
kinds: what validates, what counts as success, how to rank a candidate, and
which key marks "this looks like the right kind of object at all". Everything
else in normalize stays shared.

Kept in its own module rather than in normalize.py so that claimschema and
verdictschema can each build one without importing normalize (which imports
claimschema, and would make that a cycle).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PayloadContract:
    """The payload-kind-specific hooks `normalize` needs.

    `tier` ranks a parsed candidate object, lower being preferred, so that
    the best candidate across every extraction source wins rather than the
    first one encountered. It receives the candidate's own validation
    errors so it need not recompute them. Tier 0 is special: `extract_json`
    returns immediately on one, so tier 0 must mean "nothing can beat this".

    `container_key` is the key whose *absence* means the object found was
    probably the CLI's own wrapper rather than the friend's answer -- it
    drives normalize's "the adapter may need an envelope path" hint.
    """

    name: str
    validate: Callable[[dict[str, Any]], list[str]]
    is_successful: Callable[[dict[str, Any]], bool]
    tier: Callable[[dict[str, Any], list[str]], int]
    container_key: str
    # What to report when a payload validates but says nothing at all. This
    # is a *failure*, not an empty success: a friend that returns nothing and
    # does not say so is failed (spec §7.3), and the message has to name the
    # marker that would have made it a success for this payload kind.
    empty_message: str
