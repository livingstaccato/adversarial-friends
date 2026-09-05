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

from collections.abc import Iterable
import dataclasses

from .ids import bump_claim_id
from .ledger import Claim, Verdict

# A claim's verdicts reduced to a comparable, order-independent shape:
# (judge, verdict, evidence assessment, counter-evidence, amendment) tuples,
# sorted. See verdict_set_signature.
_Signature = tuple[tuple[str, str, str, str, str], ...]

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

# §7's gate: only settled-refuted clears a gate on its own. settled-upheld
# means the judges agreed the defect is real, which needs a Resolution, not
# a pass. discarded means nobody could check -- two rounds of judges unable
# to verify the evidence -- and a gate that passes on the strength of nobody
# having looked is the failure this tool exists to prevent, so it needs a
# Resolution too. This set held discarded until a crossexam of this file
# pointed out that the comment above it, the spec, and the code gave three
# different answers.
GATE_CLEARING_STATES = frozenset({SETTLED_REFUTED})

# superseded neither clears a gate nor blocks one: the claim was rewritten
# and its successor carries the question (§7.2 marks it "n/a"), so counting
# the original as well would demand two resolutions for one defect.
GATE_EXEMPT_STATES = frozenset({SUPERSEDED})


def judges_for(claim: Claim, roster: Iterable[str]) -> list[str]:
    """Roster minus the claim's originators (§7.1).

    `origin` is a list because an amended claim carries both its author and
    its amender, and corroborated claims accumulate every friend that raised
    them -- all of them are excluded, not just the first.

    One entry per identity. A roster entry repeated verbatim is refused
    before a run starts, but the count here must never exceed what
    `latest_per_judge` can keep -- one verdict per identity -- or quorum
    becomes unreachable and every such claim ends `unproven`.
    """
    origins = set(claim.origin)
    judges: list[str] = []
    for friend in roster:
        if friend not in origins and friend not in judges:
            judges.append(friend)
    return judges


def quorum_for(judges: Iterable[str]) -> int:
    """min(2, |judges|) -- §7.1.

    A one-judge roster still reaches a decision, but only through the
    stricter unanimity-plus-agreement branch in `state_for`.
    """
    return min(2, len(list(judges)))


def _dispositive(verdicts: Iterable[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.verdict in DISPOSITIVE]


def _normalized_amendment(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _amendments_agree(verdicts: Iterable[Verdict]) -> bool:
    amendments = {
        _normalized_amendment(verdict.amended_claim)
        for verdict in verdicts
        if verdict.verdict == "amended"
    }
    return len(amendments) == 1 and "" not in amendments


def latest_per_judge(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """One verdict per judge -- the most recent round it spoke in.

    **A judge gets one vote, however many times it is asked.** A claim that
    stays non-terminal is re-judged next round, so a judge that answers in
    round 2 and again in round 3 leaves two verdicts on the same claim
    version. Counted naively, that judge alone satisfies a quorum of two:
    unanimity with itself.

    Seen in a real run before this existed. One friend failed its judging
    round, leaving a claim below quorum and therefore non-terminal; the next
    round asked the remaining judge again, and its second identical verdict
    settled the claim as though two independent judges had agreed. The
    ledger showed the same judge twice on the same claim id.

    The newest verdict wins rather than the first: a judge that changed its
    mind after seeing the other side's reasoning (which round 3 shows it)
    has said something newer, and the discard rule in §7.2 already depends
    on a verdict set that can change between rounds.
    """
    newest: dict[str, Verdict] = {}
    for verdict in verdicts:
        current = newest.get(verdict.judge)
        if current is None or verdict.round >= current.round:
            newest[verdict.judge] = verdict
    return list(newest.values())


def state_for(
    claim: Claim,
    verdicts: Iterable[Verdict],
    roster: Iterable[str],
    round_no: int,
    max_rounds: int,
    required_missing: bool = False,
) -> str:
    """The claim's state at the end of a round -- §7.1's decision table.

    `required_missing` says one of THIS claim's judges never reported this
    round (§7.2's M12 rule, per claim): below quorum *because a judge never
    reported* is `incomplete`, which is a different problem from below
    quorum because judges declined to decide, which is `unproven`.
    """
    judges = judges_for(claim, roster)
    if not judges:
        # Nobody on the roster is independent of this claim -- every friend
        # is in its `origin`. Without this the zero-judge case falls through
        # to the disagreement branch below (quorum is 0, so "below quorum" is
        # false, and no verdicts means not unanimous) and reports `contested`
        # or, at max_rounds, `deadlocked` -- both of which assert that judges
        # disagreed when there were none.
        #
        # Reachable two ways, and observed through the second: a roster where
        # every friend co-authored a claim, and an orchestrator merge that
        # unions two friends' origins onto one surviving claim.
        return INCOMPLETE if required_missing else UNPROVEN
    quorum = quorum_for(judges)
    cast = latest_per_judge(
        v for v in verdicts if v.claim_id == claim.id and v.judge in set(judges)
    )
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
        if kinds == {"upheld"}:
            return SETTLED_UPHELD
        if kinds == {"amended"} and _amendments_agree(dispositive):
            # "Real, but wrongly worded" from the only judge there is: the
            # rewrite becomes a successor like any other unanimous
            # amendment, even though that successor -- authored by both
            # friends of a two-friend roster -- will have no judge left,
            # which the run reports. The alternative, deadlocking every
            # round and then counting the final round's amendment as
            # `upheld`, reported the judge as agreeing with wording it had
            # just rejected.
            return SUPERSEDED
        return DEADLOCKED if round_no >= max_rounds else CONTESTED

    if unanimous:
        only = next(iter(kinds))
        if only == "upheld":
            return SETTLED_UPHELD
        if only == "refuted":
            return SETTLED_REFUTED
        if _amendments_agree(dispositive):
            return SUPERSEDED
        return DEADLOCKED if round_no >= max_rounds else CONTESTED

    return DEADLOCKED if round_no >= max_rounds else CONTESTED


def verdict_set_signature(verdicts: Iterable[Verdict], claim_id: str) -> _Signature:
    """A comparable fingerprint of one claim's verdicts.

    §7.2's discard rule needs "unchanged verdict set across two consecutive
    rounds", which requires comparing rounds without caring about ordering
    or which round number each verdict carries.

    Reduced to one verdict per judge for the same reason `state_for` is (see
    latest_per_judge). Without that the signature simply grows every round --
    round 3 holds round 2's verdicts plus its own -- so two consecutive
    rounds could never compare equal and nothing would ever be discarded.
    The rule exists precisely to stop a claim nobody can verify from costing
    a full fan-out every round until max_rounds, so silently never firing
    would have been an expensive kind of broken.
    """
    relevant = latest_per_judge(v for v in verdicts if v.claim_id == claim_id)
    return tuple(
        sorted(
            (
                verdict.judge,
                verdict.verdict,
                verdict.evidence_assessment,
                _normalized_optional(verdict.counter_evidence),
                _normalized_amendment(verdict.amended_claim),
            )
            for verdict in relevant
        )
    )


def _normalized_optional(value: str | None) -> str:
    return " ".join((value or "").split())


def should_discard(previous_signature: _Signature | None, current_signature: _Signature) -> bool:
    """Whether an `unproven` claim has stopped being worth relitigating.

    A claim whose `evidence` names a path that does not exist draws the same
    non-dispositive verdict from every judge, every round, identically --
    identical work at full cost until max_rounds. Two consecutive rounds
    with an unchanged verdict set makes it terminal `discarded`, reported
    separately because "no judge could verify this" is worth seeing and is
    not the same as "refuted".

    Only the caller knows the claim's state: `_settle_round` consults this
    for a claim `state_for` has just called `unproven`, and clears the
    stored signature for every other state. An empty signature -- a claim
    nobody judged, because every friend is in its origin -- never discards:
    "judges looked twice and could not verify" must not be said of a claim
    no judge was ever shown. It was, until a crossexam of this file noticed
    that `() == ()`.
    """
    return bool(previous_signature) and previous_signature == current_signature


def downgrade_unverifiable(verdict: Verdict) -> Verdict:
    """§6.5's evidence symmetry: a dispositive verdict whose judge could not
    locate or evaluate the cited evidence is downgraded to `unproven`.

    A judge that says "refuted, but I could not find the evidence" has not
    refuted anything -- it has reported that it could not check. Left
    dispositive, two such judges would unanimously settle a claim on the
    strength of not having looked, which is the exact failure this tool
    exists to prevent. The original verdict is preserved in `reasoning` so
    the report can still show what the judge's opinion would have been.
    """
    if verdict.verdict not in DISPOSITIVE or verdict.evidence_assessment != "unverifiable":
        return verdict
    note = (
        f"[evidence unverifiable, downgraded from {verdict.verdict!r} to 'unproven' "
        "per the evidence-symmetry rule]"
    )
    reasoning = f"{verdict.reasoning}\n{note}" if verdict.reasoning else note
    return dataclasses.replace(verdict, verdict=UNPROVEN, reasoning=reasoning)


def _free_successor_id(claim_id: str, taken: set[str] | None) -> str:
    """The next version of `claim_id` that nothing already holds.

    `bump_claim_id` derives an id purely from (number, version + 1) and knows
    nothing about the ledger, which is fine while every amendment of a claim
    happens once. A loop breaks that: `run_rounds` receives `prior=None` when
    the artifact changed mid-run, so every accumulated claim -- including one
    already `superseded` -- is re-seeded `contested` and can be amended a
    second time, producing the same successor id again. The ledger then holds
    two different claims under one id, and every later reference to it is
    ambiguous.

    Bumping past what is taken fixes it where the id is chosen, rather than
    relying on a caller to preserve state it deliberately discards: the
    revision reset exists so claims settled against the old text get judged
    against the new one, which is the right behaviour to keep.
    """
    candidate = bump_claim_id(claim_id)
    if not taken:
        return candidate
    while candidate in taken:
        candidate = bump_claim_id(candidate)
    return candidate


def build_successor(
    claim: Claim,
    amendments: list[Verdict],
    round_no: int,
    taken: set[str] | None = None,
) -> tuple[Claim, str | None]:
    """The `c-0007@2` a unanimous `amended` produces -- §6.1.

    Direct callers receive a ValueError unless every amender supplied the
    same substantive wording. `state_for` normally prevents this function
    from being reached for conflicting proposals.

    `origin` is the union of the prior version's origin and every amender
    (§6.1): none of them is independent of the successor's wording, so all
    are excluded from judging it (§7.1). With a small roster this can leave
    a successor with no judges at all -- that is a real, visible outcome
    (it lands below quorum as `unproven`), not something to paper over by
    letting an author judge its own rewrite.
    """
    ordered = sorted(amendments, key=lambda v: v.judge)
    if not _amendments_agree(ordered):
        raise ValueError(f"amenders supplied conflicting wording for {claim.id}")

    origin = list(claim.origin)
    for verdict in ordered:
        if verdict.judge not in origin:
            origin.append(verdict.judge)

    successor = dataclasses.replace(
        claim,
        id=_free_successor_id(claim.id, taken),
        supersedes=claim.id,
        origin=origin,
        round=round_no,
        claim=_normalized_amendment(ordered[0].amended_claim),
    )
    return successor, None


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

    `claim_states` must already exclude advisory claims: this sees only
    state strings and cannot tell. The runner filters through
    `runmeta.unresolved_loop_states`, tested on its own. A caller that forgets
    drives the loop to its iteration ceiling, not into a hang.

    Deadlocked counts as terminal here -- see this module's docstring for
    why excluding it broke termination entirely.
    """
    return streak >= 2 and all(state in TERMINAL_STATES for state in claim_states)
