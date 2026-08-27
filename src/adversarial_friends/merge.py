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

Corroboration -- several friends independently raising the same claim -- is
the strongest signal this tool produces, and exact-match dedup is exactly
where it would otherwise be lost: without tracking it, a duplicate collapses
into a single alias record and the fact that N friends agreed is gone. Every
claim's `origin` field names which (cli, lens, model, effort) identities -- see adapters.friend_key raised it; when a claim
is aliased away, its origin is merged into whichever claim it aliased so
that claim's `origin` list still reflects everyone who actually raised it.
"""

from dataclasses import replace

from .ledger import Alias, Claim


def _key(claim: Claim) -> tuple[str, str]:
    return (" ".join(claim.claim.split()).casefold(), (claim.location or "").strip())


def _merge_origin(current: list[str], addition: list[str]) -> list[str]:
    merged = list(current)
    for value in addition:
        if value not in merged:
            merged.append(value)
    return merged


def exact_merge(
    existing: list[Claim], incoming: list[Claim], round_no: int
) -> tuple[list[Claim], list[Alias], list[Claim]]:
    """Split `incoming` into claims to keep and claims that alias an
    existing or earlier-kept claim, by exact (whitespace/case-insensitive)
    match on (text, location). Returns `(kept, aliases, updated_existing)`.

    Neither argument is mutated. When two incoming claims are mutual exact
    duplicates of each other (and match nothing in `existing`), the first
    one encountered (in `incoming` order) is kept and becomes canonical for
    the rest -- so the choice of canonical among purely-incoming duplicates
    depends on `incoming`'s order, even though the resulting *set* of kept
    claims does not.

    Whenever an incoming claim aliases a canonical claim, that canonical's
    `origin` grows to include the duplicate's own origin (deduplicated,
    order preserved) -- whether the canonical came from `kept` (an earlier
    claim in THIS `incoming` batch) or from `existing` (a claim a previous
    round/friend already contributed). `kept` claims carry their merged
    origin directly; for a canonical that came from `existing`, the caller
    cannot see that origin grow any other way -- `existing` is read-only
    here -- so `updated_existing` returns a fresh (never the original)
    `Claim` for every `existing` entry whose origin actually changed, in the
    order each change was first detected while scanning `incoming` (NOT
    `existing`'s own order -- a later `existing` entry can be the first one
    that happens to get aliased into). It is the caller's job to fold these
    back into whatever list of claims it is tracking (e.g. for later merge
    calls and for rendering); this module has no ledger/storage concept of
    its own.
    """
    seen: dict[tuple[str, str], str] = {}
    origin_of: dict[str, list[str]] = {}
    existing_by_id: dict[str, Claim] = {}
    for claim in existing:
        seen[_key(claim)] = claim.id
        origin_of[claim.id] = list(claim.origin)
        existing_by_id[claim.id] = claim

    kept: list[Claim] = []
    aliases: list[Alias] = []
    changed_existing_ids: list[str] = []

    for claim in incoming:
        key = _key(claim)
        canonical_id = seen.get(key)
        if canonical_id is None:
            seen[key] = claim.id
            origin_of[claim.id] = list(claim.origin)
            kept.append(claim)
        else:
            before = origin_of.get(canonical_id, [])
            after = _merge_origin(before, claim.origin)
            if after != before:
                origin_of[canonical_id] = after
                if canonical_id in existing_by_id and canonical_id not in changed_existing_ids:
                    changed_existing_ids.append(canonical_id)
            aliases.append(
                Alias(
                    canonical=canonical_id,
                    duplicate=claim.id,
                    round=round_no,
                    source="exact",
                    rationale="identical claim text and location",
                )
            )

    kept = [
        replace(c, origin=origin_of[c.id]) if origin_of[c.id] != list(c.origin) else c for c in kept
    ]
    updated_existing = [
        replace(existing_by_id[cid], origin=origin_of[cid]) for cid in changed_existing_ids
    ]
    return kept, aliases, updated_existing


def canonical_claims(records: list[object]) -> list[Claim]:
    """Rebuild the live claim set from an append-only ledger.

    A resumed run has to reconstruct what the original process held in
    memory, and the ledger deliberately does not store it directly. Two
    things have to be undone:

    * **Aliased duplicates are still present as claim records.** They are
      written on purpose -- an Alias's `duplicate` id must resolve to a real
      claim record or the ledger has a dangling reference -- but they are not
      part of the live set.
    * **Every claim record's `origin` is frozen as first written.** When a
      later friend's claim aliased an earlier one, the earlier record was
      never rewritten (the ledger is append-only), so reading it back
      under-counts corroboration. The alias graph is what carries that, and
      folding it back in here is the only way a resumed run reports the same
      corroboration the original would have.

    Superseded claims are kept: a successor carries `supersedes`, and both
    versions remain part of the record. It is the state machine's job to
    decide which is live, not this function's.
    """
    claims = [r for r in records if isinstance(r, Claim)]
    aliases = [r for r in records if isinstance(r, Alias)]
    by_id = {c.id: c for c in claims}

    origins: dict[str, list[str]] = {c.id: list(c.origin) for c in claims}
    aliased: set[str] = set()
    for alias in aliases:
        aliased.add(alias.duplicate)
        duplicate = by_id.get(alias.duplicate)
        if duplicate is None or alias.canonical not in origins:
            # A dangling alias. Recorded rather than repaired: this function
            # reconstructs, it does not adjudicate.
            continue
        origins[alias.canonical] = _merge_origin(origins[alias.canonical], duplicate.origin)

    return [
        replace(c, origin=origins[c.id]) if origins[c.id] != list(c.origin) else c
        for c in claims
        if c.id not in aliased
    ]
