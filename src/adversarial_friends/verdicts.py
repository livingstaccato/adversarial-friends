"""Claim state from a round's verdicts -- spec §7.1 and §7.2.

Pure functions over records. Nothing here dispatches, reads the filesystem,
or knows what a round costs; that belongs to commands/crossexam.py. Keeping
the decision logic separate is deliberate: these rules are where the design
was wrong twice, and they are only cheap to test while they stay pure.

The two corrections worth knowing before changing anything here:

**The originator casts no verdict and is not dispositive** (§7.1, finding
H1). An earlier design gave the claim's author a standing implicit `upheld`
*inside* the dispositive set, which made `settled-refuted` -- the only state
that clears a gate without hand-written work -- unreachable for every claim
in every roster. The originator's position is provenance and a tie-break,
never a vote.

**Deadlock is terminal for loop purposes** (§7.3). Excluding it meant a
single genuine disagreement -- precisely the outcome this tool exists to
surface -- disabled loop termination permanently and forced every run to a
ceiling.
"""

from collections import Counter
from collections.abc import Iterable

from .ledger import Claim, Verdict

# A claim's verdicts reduced to a comparable, order-independent shape:
# (judge, verdict) pairs, sorted. See verdict_set_signature.
_Signature = tuple[tuple[str, str], ...]

# Verdicts that decide a claim. `unproven` and `out-of-scope` are recorded
# and reported but never dispositive -- a judge saying "I could not verify
# this" is information, not a decision.
DISPOSITIVE = frozenset({"upheld", "refuted", "amended"})
NON_DISPOSITIVE = frozenset({"unproven", "out-of-scope"})
VALID_VERDICTS = DISPOSITIVE | NON_DISPOSITIVE

SETTLED_UPHELD = "settled-upheld"
SETTLED_REFUTED = "settled-refuted"
SUPERSEDED = "superseded"
CONTESTED = "contested"
DEADLOCKED = "deadlocked"
UNPROVEN = "unproven"
INCOMPLETE = "incomplete"
DISCARDED = "discarded"

# §7.2. `contested`, `unproven`, and `incomplete` are the non-terminal ones:
# they are the states a further round could still change.
TERMINAL_STATES = frozenset({SETTLED_UPHELD, SETTLED_REFUTED, SUPERSEDED, DEADLOCKED, DISCARDED})

# Only settled-refuted clears a gate on its own. settled-upheld means the
# judges agreed the defect is real, which needs a Resolution, not a pass.
GATE_CLEARING_STATES = frozenset({SETTLED_REFUTED, SUPERSEDED, DISCARDED})


def judges_for(claim: Claim, roster: Iterable[str]) -> list[str]:
    """Roster minus the claim's originators (§7.1).

    `origin` is a list because an amended claim carries both its author and
    its amender, and corroborated claims accumulate every friend that raised
    them -- all of them are excluded, not just the first.
    """
    origins = set(claim.origin)
    return [friend for friend in roster if friend not in origins]


def quorum_for(judges: Iterable[str]) -> int:
    """min(2, |judges|) -- §7.1.

    A one-judge roster still reaches a decision, but only through the
    stricter unanimity-plus-agreement branch in `state_for`.
    """
    return min(2, len(list(judges)))


def _dispositive(verdicts: Iterable[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.verdict in DISPOSITIVE]


def state_for(
    claim: Claim,
    verdicts: Iterable[Verdict],
    roster: Iterable[str],
    round_no: int,
    max_rounds: int,
    required_missing: bool = False,
) -> str:
    """The claim's state at the end of a round -- §7.1's decision table.

    `required_missing` is the run-level signal that a required friend failed
    this round (§7.2's M12 rule): below quorum *because a judge never
    reported* is `incomplete`, which is a different problem from below
    quorum because judges declined to decide, which is `unproven`.
    """
    judges = judges_for(claim, roster)
    quorum = quorum_for(judges)
    cast = [v for v in verdicts if v.claim_id == claim.id and v.judge in set(judges)]
    dispositive = _dispositive(cast)

    if len(dispositive) < quorum:
        return INCOMPLETE if required_missing else UNPROVEN

    kinds = {v.verdict for v in dispositive}
    unanimous = len(kinds) == 1

    if len(judges) == 1:
        # A single judge cannot outvote the author: with only one, there is
        # no way to tell a wrong author from a wrong judge, so disagreement
        # is a deadlock rather than a settlement (§7.1). Agreement with the
        # originator's position is required, and the originator's position
        # on its own claim is that it stands -- i.e. `upheld`.
        if unanimous and kinds == {"upheld"}:
            return SETTLED_UPHELD
        return DEADLOCKED if round_no >= max_rounds else CONTESTED

    if unanimous:
        only = next(iter(kinds))
        if only == "upheld":
            return SETTLED_UPHELD
        if only == "refuted":
            return SETTLED_REFUTED
        return SUPERSEDED  # unanimous `amended`: the successor carries it on

    return DEADLOCKED if round_no >= max_rounds else CONTESTED


def verdict_set_signature(verdicts: Iterable[Verdict], claim_id: str) -> _Signature:
    """A comparable fingerprint of one claim's verdicts.

    §7.2's discard rule needs "unchanged verdict set across two consecutive
    rounds", which requires comparing rounds without caring about ordering
    or which round number each verdict carries.
    """
    relevant = [v for v in verdicts if v.claim_id == claim_id]
    return tuple(sorted((v.judge, v.verdict) for v in relevant))


def should_discard(previous_signature: _Signature | None, current_signature: _Signature) -> bool:
    """Whether an `unproven` claim has stopped being worth relitigating.

    A claim whose `evidence` names a path that does not exist draws the same
    non-dispositive verdict from every judge, every round, identically --
    identical work at full cost until max_rounds. Two consecutive rounds
    with an unchanged verdict set makes it terminal `discarded`, reported
    separately because "no judge could verify this" is worth seeing and is
    not the same as "refuted".
    """
    return previous_signature is not None and previous_signature == current_signature


def downgrade_late_amendment(verdict: Verdict, round_no: int, max_rounds: int) -> Verdict:
    """§7.2: an `amended` verdict in the final round produces a successor
    with no round left to judge it, leaving both versions non-terminal
    forever. In the final round it becomes `upheld`, with the proposed
    wording preserved in `reasoning` so nothing the judge wrote is lost.
    """
    if verdict.verdict != "amended" or round_no < max_rounds:
        return verdict
    note = f"[late amendment, downgraded to upheld] proposed: {verdict.amended_claim}"
    reasoning = f"{verdict.reasoning}\n{note}" if verdict.reasoning else note
    return Verdict(
        claim_id=verdict.claim_id,
        judge=verdict.judge,
        round=verdict.round,
        verdict="upheld",
        confidence=verdict.confidence,
        evidence_assessment=verdict.evidence_assessment,
        reasoning=reasoning,
        counter_evidence=verdict.counter_evidence,
        amended_claim=verdict.amended_claim,
    )


def round_is_dry(all_claims_were_aliases: bool, every_required_friend_ok: bool) -> bool:
    """§7.3. A round is dry when every required friend completed successfully
    *and* every claim it produced was an alias of one already known -- i.e.
    the round cost a full fan-out and learned nothing new."""
    return every_required_friend_ok and all_claims_were_aliases


def next_streak(previous: int, failed: bool, dry: bool) -> int:
    """§7.3's streak arithmetic. A failed round resets rather than counting:
    a round that did not complete is not evidence of convergence."""
    if failed:
        return 0
    return previous + 1 if dry else 0


def loop_should_terminate(streak: int, claim_states: Iterable[str]) -> bool:
    """§7.3: two dry rounds AND every non-advisory claim terminal.

    Deadlocked counts as terminal here -- see this module's docstring for
    why excluding it broke termination entirely.
    """
    return streak >= 2 and all(state in TERMINAL_STATES for state in claim_states)


def summarize(states: Iterable[str]) -> Counter[str]:
    """Counts per state, for the report header and the gate decision."""
    return Counter(states)


def gate_blocked(states: Iterable[str]) -> bool:
    """A gate passes only when nothing is left that needs a human.

    Non-terminal states block because the run has not finished deciding
    them; `settled-upheld` blocks because the judges agreed the defect is
    real and it needs a Resolution, not a pass.
    """
    return any(state not in GATE_CLEARING_STATES for state in states)
