"""Deterministic claim deduplication.

Exact merge under-merges on purpose. Two friends describing one defect in
different words will produce two claims, which costs a round; the alternative
-- guessing at equivalence -- corrupts termination arithmetic, which is
worse. Semantic merging is the orchestrator's job and is opt-in.

The dedup key normalizes only whitespace runs and case, plus a bare strip on
location, treating a missing location (``None``) the same as an empty one
(``""``): both mean "no location was given," so two otherwise-identical
claims should not be kept apart on that distinction alone. Nothing else is
normalized. In particular, this key does *not* apply Unicode normalization
(NFC/NFD), so two claims whose text differs only in composed vs. decomposed
accented characters will not be merged -- that is an accepted instance of
the same under-merge tradeoff, not an oversight; adding it would be exactly
the kind of normalization-beyond-the-spec this module deliberately avoids.
"""
from .ledger import Alias, Claim


def _key(claim: Claim) -> tuple[str, str]:
    return (" ".join(claim.claim.split()).casefold(), (claim.location or "").strip())


def exact_merge(existing: list[Claim], incoming: list[Claim],
                 round_no: int) -> tuple[list[Claim], list[Alias]]:
    """Split `incoming` into claims to keep and claims that alias an
    existing or earlier-kept claim, by exact (whitespace/case-insensitive)
    match on (text, location).

    Neither argument is mutated. When two incoming claims are mutual exact
    duplicates of each other (and match nothing in `existing`), the first
    one encountered (in `incoming` order) is kept and becomes canonical for
    the rest -- so the choice of canonical among purely-incoming duplicates
    depends on `incoming`'s order, even though the resulting *set* of kept
    claims does not.
    """
    seen: dict[tuple[str, str], str] = {_key(c): c.id for c in existing}
    kept: list[Claim] = []
    aliases: list[Alias] = []
    for claim in incoming:
        key = _key(claim)
        canonical = seen.get(key)
        if canonical is None:
            seen[key] = claim.id
            kept.append(claim)
        else:
            aliases.append(Alias(
                canonical=canonical, duplicate=claim.id, round=round_no,
                source="exact", rationale="identical claim text and location",
            ))
    return kept, aliases
