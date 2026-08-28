"""Who may judge what, and what they were told the others said.

The rules a cross-examination round applies before and after dispatch, kept
apart from the loop that drives it: reading a judge's output into verdicts,
deciding which claims it is allowed to rule on, capping a dispatch to the
run's remaining wall clock, and assembling the prior verdicts a judge is
shown.

Every one of these enforces something §5.1 or §7.1 states and none of them
needs the round loop to do it, which is why they are testable on their own.

Split out of crossexam.py, which had grown past this repo's 500-line cap.
"""

import dataclasses
from typing import Any

from .. import verdicts as vd
from ..adapters import FriendSpec, friend_key
from ..dispatch import KILL_GRACE_S
from ..ledger import Claim, Verdict


def _parse_verdicts(payload: dict[str, Any], judge: str, round_no: int) -> list[Verdict]:
    """Turn one judge's validated payload into Verdict records.

    The payload has already passed VERDICT_CONTRACT.validate inside
    normalize, so the required fields are present and well-typed; this only
    shapes them. `judge` comes from the runner, never from the payload -- a
    friend does not get to say who it is.
    """
    out = []
    # See critique.run_critique: nullable container, so `or []`.
    for entry in payload.get("verdicts") or []:
        out.append(
            Verdict(
                claim_id=entry["claim_id"],
                judge=judge,
                round=round_no,
                verdict=entry["verdict"],
                confidence=entry["confidence"],
                evidence_assessment=entry.get("evidence_assessment") or "",
                reasoning=entry["reasoning"],
                counter_evidence=entry.get("counter_evidence"),
                amended_claim=entry.get("amended_claim"),
            )
        )
    return out


def _slice_for(spec: FriendSpec, contested: list[Claim]) -> list[Claim]:
    """The claims this friend may judge: everything contested it did not
    originate (§7.1). An empty slice means this friend wrote every claim
    still open, and it is simply not dispatched this round."""
    key = friend_key(spec)
    return [claim for claim in contested if key not in claim.origin]


def _within_deadline(specs: list[FriendSpec], seconds_left: float) -> list[FriendSpec]:
    """Every spec, with its timeout capped at what remains of the run's
    wall-clock ceiling. A friend dispatched just under the ceiling used to
    run for its own full timeout past it.

    The cap subtracts `KILL_GRACE_S`, because dispatch hands `run_process` a
    kill deadline of `spec.timeout + KILL_GRACE_S` -- a full extra minute. A
    cap that ignored it made the wall-clock ceiling a ceiling only for
    friends that behaved: a single hung friend overshot it by that minute,
    plus the group's own escalation windows. Reserving the grace up front
    costs a well-behaved friend some of its timeout and makes the ceiling
    mean what it says, which is the trade the ceiling exists to make.

    Below one whole second of usable time, nothing is dispatched at all. `int()` floors, so
    0.6s remaining became a timeout of 0 -- a friend launched only to be
    killed the instant it started, which still spends a call from the budget
    and still reports as a failure that marks the run incomplete. There is no
    honest dispatch left in under a second: an agent CLI needs seconds to
    reach its model. Returning nothing lets the caller's withheld path say so
    plainly instead.
    """
    remaining = int(seconds_left) - KILL_GRACE_S
    if remaining < 1:
        return []
    return [dataclasses.replace(s, timeout=min(s.timeout, remaining)) for s in specs]


def _never_reported(missing: dict[str, set[str]], spec: FriendSpec, claims: list[Claim]) -> None:
    """Record `spec` as a judge that did not report on `claims` this round."""
    for claim in claims:
        missing.setdefault(claim.id, set()).add(friend_key(spec))


def _prior_verdicts_by_claim(
    all_verdicts: list[Verdict],
    claim_ids: set[str],
    exclude_judge: str | None = None,
) -> dict[str, list[Verdict]]:
    """One verdict per judge per claim -- `latest_per_judge`, the same
    reduction `state_for` and `verdict_set_signature` apply.

    §5.1 strips the judge and carries no round, so two verdicts from one
    judge render as two anonymous reviewers: from round 4 a judge weighing
    "what did the others conclude" reads a consensus that does not exist.
    The file's own comment listed the three sites already fixed; this was
    the fourth.

    `exclude_judge` drops the recipient's own verdicts, and is the fifth. One
    result used to be built per round and handed to every judge, so a judge
    read its OWN earlier opinion back as an anonymous reviewer -- §5.1 having
    stripped the name that would have given it away. That is worse than
    leaking identity: it manufactures corroboration in the direction each
    judge already leans, which is the consensus blind presentation exists to
    prevent. A claim left with nothing but the recipient's own verdict drops
    out entirely rather than arriving as an empty block, which would read as
    "the others said nothing" when in truth nobody else has spoken yet."""
    prior: dict[str, list[Verdict]] = {}
    for verdict in all_verdicts:
        if verdict.claim_id not in claim_ids:
            continue
        if exclude_judge is not None and verdict.judge == exclude_judge:
            continue
        prior.setdefault(verdict.claim_id, []).append(verdict)
    return {cid: vd.latest_per_judge(cast) for cid, cast in prior.items() if cast}
