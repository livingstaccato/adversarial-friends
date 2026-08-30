"""Deterministic in-memory state derived from the append-only review ledger."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .errors import UsageError
from .ledger import Alias, Claim, Record, Resolution, Verdict


def _union(left: list[str], right: list[str]) -> list[str]:
    out = list(left)
    for value in right:
        if value not in out:
            out.append(value)
    return out


@dataclass
class ReviewState:
    claims_by_id: dict[str, Claim] = field(default_factory=dict)
    origins_by_id: dict[str, list[str]] = field(default_factory=dict)
    aliased_ids: set[str] = field(default_factory=set)
    aliases: list[Alias] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    transition_warnings: list[str] = field(default_factory=list)

    @classmethod
    def replay(cls, records: Iterable[Record]) -> ReviewState:
        state = cls()
        for record in records:
            state.apply(record)
        return state

    def apply(self, record: Record) -> None:
        if isinstance(record, Claim):
            self._apply_claim(record)
            return
        if isinstance(record, Alias):
            self._apply_alias(record)
            return
        if record.claim_id not in self.claims_by_id:
            kind = type(record).__name__.lower()
            raise UsageError(f"{kind} names unknown claim {record.claim_id!r}")
        if isinstance(record, Verdict):
            self.verdicts.append(record)
        else:
            self.resolutions.append(record)

    def _apply_claim(self, record: Claim) -> None:
        prior = self.claims_by_id.get(record.id)
        if prior is not None and prior != record:
            raise UsageError(f"duplicate claim id {record.id!r} has different content")
        if prior is not None:
            return
        if record.supersedes is not None and record.supersedes not in self.claims_by_id:
            raise UsageError(
                f"successor {record.id!r} supersedes unknown claim {record.supersedes!r}"
            )
        ancestor = record.supersedes
        seen = {record.id}
        while ancestor is not None:
            if ancestor in seen:
                raise UsageError(f"successor cycle reaches {ancestor!r}")
            seen.add(ancestor)
            predecessor = self.claims_by_id.get(ancestor)
            ancestor = predecessor.supersedes if predecessor is not None else None
        self.claims_by_id[record.id] = record
        self.origins_by_id[record.id] = list(record.origin)

    def _apply_alias(self, record: Alias) -> None:
        self.aliases.append(record)
        self.aliased_ids.add(record.duplicate)
        if (
            record.canonical not in self.claims_by_id
            or record.duplicate not in self.claims_by_id
        ):
            self.transition_warnings.append(
                f"alias {record.duplicate!r} -> {record.canonical!r} has a missing endpoint"
            )
            return
        if record.canonical == record.duplicate or record.canonical in self.aliased_ids:
            self.transition_warnings.append(
                f"alias {record.duplicate!r} -> {record.canonical!r} "
                "is self-referential or non-topological"
            )
            return
        self.origins_by_id[record.canonical] = _union(
            self.origins_by_id[record.canonical],
            self.origins_by_id[record.duplicate],
        )

    @property
    def claims(self) -> list[Claim]:
        return [
            replace(claim, origin=self.origins_by_id[claim.id])
            for claim in self.claims_by_id.values()
            if claim.id not in self.aliased_ids
        ]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.claim_id == claim_id]

    def latest_verdicts_for(self, claim_id: str) -> list[Verdict]:
        from .verdicts import latest_per_judge

        return latest_per_judge(self.verdicts_for(claim_id))

    def claim_state(
        self,
        claim: Claim,
        roster: Iterable[str],
        round_no: int,
        max_rounds: int,
        *,
        required_missing: bool = False,
    ) -> str:
        from .verdicts import state_for

        return state_for(
            claim,
            self.verdicts_for(claim.id),
            roster,
            round_no,
            max_rounds,
            required_missing=required_missing,
        )

    def blocking(self, states: dict[str, str]) -> list[Claim]:
        from .resolutions import blocking_claims

        return blocking_claims(self.claims, states, self.resolutions)

    def copy_transition_warnings(self, downgrades: list[str]) -> None:
        """Surface tolerated historical transitions without duplicating notes."""
        for warning in self.transition_warnings:
            note = f"ledger compatibility warning: {warning}"
            if note not in downgrades:
                downgrades.append(note)
