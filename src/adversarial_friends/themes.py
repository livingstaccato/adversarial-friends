"""Conservative semantic-theme proposals for loop novelty.

Theme comparison is deliberately separate from durable ledger identity.
Only claims at the same nonempty source anchor are eligible for fuzzy
comparison, and even then both the overall wording and failure mechanism
must clear a high threshold.  Exact identity remains the only fallback for
claims without a comparable anchor.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import difflib
import math
import re

from .errors import UsageError
from .ids import parse_claim_id
from .ledger import Claim

THEME_THRESHOLD = 0.82

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_SYNONYMS = {
    "absent": "missing",
    "expiry": "expiration",
}


@dataclass(frozen=True)
class ThemeProposal:
    """An advisory possible-duplicate relationship, never a ledger alias."""

    canonical: str
    duplicate: str
    score: float
    anchor: str

    def __post_init__(self) -> None:
        for name in ("canonical", "duplicate"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"theme proposal {name} must be a nonempty string")
            parse_claim_id(value)
        if self.canonical == self.duplicate:
            raise ValueError("theme proposal canonical and duplicate must differ")
        if type(self.score) is not float or not math.isfinite(self.score):
            raise ValueError("theme proposal score must be a finite float")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("theme proposal score must be between 0 and 1")
        if type(self.anchor) is not str or not self.anchor:
            raise ValueError("theme proposal anchor must be a nonempty string")
        if normalized_anchor(self.anchor) != self.anchor:
            raise ValueError("theme proposal anchor must already be normalized")

    def to_dict(self) -> dict[str, str | float]:
        return {
            "canonical": self.canonical,
            "duplicate": self.duplicate,
            "score": self.score,
            "anchor": self.anchor,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ThemeProposal":
        if type(value) is not dict:
            raise UsageError("saved theme proposal must be an object")
        proposal = value
        expected = {"canonical", "duplicate", "score", "anchor"}
        if set(proposal) != expected:
            raise UsageError("saved theme proposal has an invalid shape")
        try:
            return cls(
                canonical=proposal["canonical"],
                duplicate=proposal["duplicate"],
                score=proposal["score"],
                anchor=proposal["anchor"],
            )
        except (TypeError, ValueError, UsageError) as exc:
            raise UsageError(f"saved theme proposal is invalid: {exc}") from exc


def normalized_anchor(location: str | None) -> str | None:
    """Normalize only case and whitespace; punctuation remains identity."""
    if location is None:
        return None
    if type(location) is not str:
        raise UsageError("theme anchor must be a string or null")
    normalized = " ".join(location.casefold().split())
    return normalized or None


def _tokens(text: str) -> set[str]:
    if type(text) is not str:
        raise UsageError("theme comparison text must be a string")
    return {
        _SYNONYMS.get(token, token)
        for token in _TOKEN_RE.findall(text.casefold())
        if len(token) > 2
    }


def similarity(left: str, right: str) -> float:
    """Return deterministic token-set similarity in the closed interval 0..1."""
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = difflib.SequenceMatcher(
        None,
        " ".join(sorted(left_tokens)),
        " ".join(sorted(right_tokens)),
    ).ratio()
    return max(overlap, sequence)


def _validated_claim(claim: object, context: str) -> Claim:
    if not isinstance(claim, Claim):
        raise UsageError(f"{context} must be a Claim")
    for name in ("id", "claim", "failure_scenario"):
        value = getattr(claim, name)
        if type(value) is not str or not value:
            raise UsageError(f"{context} {name} must be a nonempty string")
    try:
        parse_claim_id(claim.id)
    except UsageError as exc:
        raise UsageError(f"{context} has invalid id: {exc}") from exc
    if claim.location is not None and type(claim.location) is not str:
        raise UsageError(f"{context} location must be a string or null")
    return claim


def _exact_identity(claim: Claim) -> tuple[str, str]:
    """Mirror exact_merge's public identity without changing merge.py."""
    return (
        " ".join(claim.claim.split()).casefold(),
        (claim.location or "").strip(),
    )


def compare_theme(canonical: Claim, candidate: Claim) -> ThemeProposal | None:
    canonical = _validated_claim(canonical, "canonical claim")
    candidate = _validated_claim(candidate, "candidate claim")
    anchor = normalized_anchor(canonical.location)
    if anchor is None or anchor != normalized_anchor(candidate.location):
        return None
    claim_score = similarity(canonical.claim, candidate.claim)
    failure_score = similarity(canonical.failure_scenario, candidate.failure_scenario)
    score = (claim_score + failure_score) / 2
    if score < THEME_THRESHOLD or failure_score < THEME_THRESHOLD:
        return None
    return ThemeProposal(canonical.id, candidate.id, round(score, 4), anchor)


def classify_novel(
    existing: Sequence[Claim], incoming: Sequence[Claim]
) -> tuple[set[str], list[ThemeProposal]]:
    """Classify incoming claims in stable order; first canonical always wins."""
    candidates = [
        _validated_claim(item, f"existing claim[{index}]") for index, item in enumerate(existing)
    ]
    novel: set[str] = set()
    proposals: list[ThemeProposal] = []
    for index, raw_claim in enumerate(incoming):
        claim = _validated_claim(raw_claim, f"incoming claim[{index}]")
        exact = next(
            (prior for prior in candidates if _exact_identity(prior) == _exact_identity(claim)),
            None,
        )
        if exact is not None:
            continue
        proposal = next(
            (match for prior in candidates if (match := compare_theme(prior, claim)) is not None),
            None,
        )
        if proposal is None:
            novel.add(claim.id)
            candidates.append(claim)
        else:
            proposals.append(proposal)
    return novel, proposals
