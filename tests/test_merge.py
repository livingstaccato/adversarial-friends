from afriend.ledger import Alias, Claim
from afriend.merge import canonical_claims, exact_merge
from afriend.themes import classify_novel


def claim(cid, text, location="src/a.py:1", origin=None, failure="f"):
    return Claim(
        id=cid,
        supersedes=None,
        origin=origin or ["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="high",
        claim=text,
        location=location,
        evidence="e",
        failure_scenario=failure,
        suggested_fix="s",
    )


def test_identical_text_and_location_is_aliased():
    existing = [claim("c-0001@1", "the guard is missing")]
    incoming = [claim("c-0002@1", "the guard is missing")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert kept == []
    assert aliases[0].canonical == "c-0001@1"
    assert aliases[0].duplicate == "c-0002@1"
    assert aliases[0].source == "exact"


def test_whitespace_and_case_differences_still_alias():
    existing = [claim("c-0001@1", "The Guard Is Missing")]
    incoming = [claim("c-0002@1", "  the guard is missing  ")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert kept == [] and len(aliases) == 1


def test_different_location_is_not_aliased():
    existing = [claim("c-0001@1", "the guard is missing", "src/a.py:1")]
    incoming = [claim("c-0002@1", "the guard is missing", "src/b.py:9")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert len(kept) == 1 and aliases == []


def test_paraphrase_is_not_merged():
    """Exact merge under-merges on purpose: safer than corrupting termination."""
    existing = [claim("c-0001@1", "timeout leaves MCP children running")]
    incoming = [claim("c-0002@1", "child processes survive timeout")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert len(kept) == 1 and aliases == []


def test_theme_proposal_never_changes_exact_merge_identity():
    existing = [
        claim(
            "c-0001@1",
            "expiry guard is missing",
            location="src/auth.py:42",
            failure="expired token passes",
        )
    ]
    incoming = [
        claim(
            "c-0002@1",
            "missing expiration guard",
            location="src/auth.py:42",
            failure="expired token passes",
        )
    ]

    novel, proposals = classify_novel(existing, incoming)
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)

    assert novel == set() and len(proposals) == 1
    assert kept == incoming
    assert aliases == []


# --- adversarial / break-it cases -----------------------------------------


def test_nbsp_vs_regular_space_still_aliases():
    """A non-breaking space is whitespace to str.split(), same as a regular
    space, so this collapses the same way ordinary whitespace does."""
    nbsp = " "  # noqa: RUF001 -- deliberate non-breaking space, this is the thing under test
    existing = [claim("c-0001@1", "the guard is missing")]
    incoming = [claim("c-0002@1", f"the{nbsp}guard{nbsp}is{nbsp}missing")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert kept == [] and len(aliases) == 1


def test_unicode_normalization_forms_are_not_merged():
    """NFC vs. NFD renderings of the same accented text are NOT aliased.

    This is a deliberate limitation, not a bug: the key only casefolds and
    collapses whitespace, it does not apply Unicode normalization. Adding
    that would be exactly the kind of normalization-beyond-the-spec the
    module is designed to avoid, so two claims differing only in composed
    vs. decomposed accents produce two kept claims (an accepted instance of
    the documented under-merge tradeoff).
    """
    import unicodedata

    nfc = unicodedata.normalize("NFC", "café is stale")
    nfd = unicodedata.normalize("NFD", "café is stale")
    assert nfc != nfd  # sanity: these really are different code point sequences
    existing = [claim("c-0001@1", nfc)]
    incoming = [claim("c-0002@1", nfd)]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert len(kept) == 1 and aliases == []


def test_location_none_and_empty_string_are_equivalent():
    """None and "" both mean "no location given," so they key the same."""
    existing = [claim("c-0001@1", "the guard is missing", location=None)]
    incoming = [claim("c-0002@1", "the guard is missing", location="")]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert kept == [] and len(aliases) == 1


def test_two_identical_incoming_claims_second_aliases_the_first():
    """With no matching existing claim, the first-seen incoming claim (in
    incoming's order) is kept and becomes canonical for the rest."""
    existing: list = []
    incoming = [
        claim("c-0002@1", "the guard is missing"),
        claim("c-0003@1", "the guard is missing"),
    ]
    kept, aliases, _updated = exact_merge(existing, incoming, round_no=1)
    assert [c.id for c in kept] == ["c-0002@1"]
    assert len(aliases) == 1
    assert aliases[0].canonical == "c-0002@1"
    assert aliases[0].duplicate == "c-0003@1"


def test_empty_existing_list_keeps_all_incoming():
    kept, aliases, _updated = exact_merge(
        [], [claim("c-0001@1", "a"), claim("c-0002@1", "b")], round_no=1
    )
    assert [c.id for c in kept] == ["c-0001@1", "c-0002@1"]
    assert aliases == []


def test_canonical_choice_among_incoming_duplicates_depends_on_order():
    """The merged *set* is order-independent, but which of two mutually
    duplicate incoming claims becomes canonical is not: reversing the
    incoming list flips which one is kept vs. aliased."""
    a = claim("c-0002@1", "the guard is missing")
    b = claim("c-0003@1", "the guard is missing")

    kept1, aliases1, _u1 = exact_merge([], [a, b], round_no=1)
    kept2, aliases2, _u2 = exact_merge([], [b, a], round_no=1)

    assert [c.id for c in kept1] == ["c-0002@1"]
    assert [c.id for c in kept2] == ["c-0003@1"]
    assert aliases1[0].canonical == "c-0002@1" and aliases1[0].duplicate == "c-0003@1"
    assert aliases2[0].canonical == "c-0003@1" and aliases2[0].duplicate == "c-0002@1"


def test_does_not_mutate_inputs():
    existing = [claim("c-0001@1", "the guard is missing")]
    incoming = [claim("c-0002@1", "the guard is missing"), claim("c-0003@1", "something else")]
    existing_snapshot = list(existing)
    incoming_snapshot = list(incoming)
    exact_merge(existing, incoming, round_no=1)
    assert existing == existing_snapshot
    assert incoming == incoming_snapshot


# --- I2: origin merging on alias (corroboration must not be lost) ---------


def test_alias_into_an_existing_claim_merges_origin_into_updated_existing():
    """When an incoming claim aliases a claim from `existing` (a different
    friend's own claim from an earlier dispatch), the corroborating origin
    must be recoverable -- `existing` itself is never mutated (it's a plain
    list the caller owns), so it comes back as a fresh Claim in
    `updated_existing`, not as a change visible on the original object."""
    original = claim("c-0001@1", "the guard is missing", origin=["codex/ops"])
    existing = [original]
    incoming = [claim("c-0002@1", "the guard is missing", origin=["agy/security"])]
    kept, _aliases, updated_existing = exact_merge(existing, incoming, round_no=1)
    assert kept == []
    assert len(updated_existing) == 1
    assert updated_existing[0].id == "c-0001@1"
    assert updated_existing[0].origin == ["codex/ops", "agy/security"]
    # existing itself is untouched -- the caller decides how/whether to fold
    # updated_existing back into its own bookkeeping.
    assert existing[0] is original
    assert existing[0].origin == ["codex/ops"]


def test_alias_into_a_kept_incoming_claim_merges_origin_directly():
    """Two claims in the SAME incoming batch that are exact duplicates of
    each other (no existing claim involved at all): the first-kept one's
    own returned origin must include the second's, with no separate
    'updated_existing' entry needed since it never came from `existing`."""
    incoming = [
        claim("c-0002@1", "the guard is missing", origin=["codex/ops"]),
        claim("c-0003@1", "the guard is missing", origin=["agy/security"]),
    ]
    kept, _aliases, updated_existing = exact_merge([], incoming, round_no=1)
    assert [c.id for c in kept] == ["c-0002@1"]
    assert kept[0].origin == ["codex/ops", "agy/security"]
    assert updated_existing == []


def test_origin_merge_deduplicates_repeated_origins():
    """The same friend re-raising its own claim (e.g. two lenses landing on
    identical text/location by coincidence) must not pad origin with
    repeats of the same (cli, lens) pair."""
    existing = [claim("c-0001@1", "the guard is missing", origin=["codex/ops"])]
    incoming = [claim("c-0002@1", "the guard is missing", origin=["codex/ops"])]
    _kept, _aliases, updated_existing = exact_merge(existing, incoming, round_no=1)
    assert updated_existing == []  # origin didn't actually change


def test_non_aliased_existing_claims_are_never_in_updated_existing():
    existing = [claim("c-0001@1", "the guard is missing", origin=["codex/ops"])]
    incoming = [claim("c-0002@1", "an unrelated defect", origin=["agy/security"])]
    kept, _aliases, updated_existing = exact_merge(existing, incoming, round_no=1)
    assert updated_existing == []
    assert [c.id for c in kept] == ["c-0002@1"]


def chained_alias_records():
    a = claim("c-0001@1", "same defect", origin=["friend-a"])
    b = claim("c-0002@1", "same defect reworded", origin=["friend-b"])
    c = claim("c-0003@1", "same defect again", origin=["friend-c"])
    return [
        a,
        b,
        Alias("c-0001@1", "c-0002@1", 1, "exact", "same"),
        c,
        Alias("c-0003@1", "c-0001@1", 2, "orchestrator", "same defect"),
    ]


def test_canonical_reconstruction_preserves_transitive_origins():
    rebuilt = canonical_claims(chained_alias_records())

    assert [(item.id, item.origin) for item in rebuilt] == [
        ("c-0003@1", ["friend-c", "friend-a", "friend-b"])
    ]
