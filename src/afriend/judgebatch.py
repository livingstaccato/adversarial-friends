"""Durable, authenticated judging-result batches for crash replay."""

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .adapters import FriendSpec, friend_key
from .errors import UsageError
from .jsonio import MAX_JSON_FILE_BYTES, decode_json_object
from .ledger import Verdict, record_from_dict, record_to_dict
from .rounds import recover_result_audit
from .runstore import RunStore


@dataclass(frozen=True)
class RecoveredJudgeBatch:
    row: dict[str, Any]
    verdicts: tuple[Verdict, ...]
    omitted_claim_ids: tuple[str, ...]


def persist_judging_batch(
    store: RunStore,
    round_no: int,
    spec: FriendSpec,
    row: dict[str, Any],
    shown_claim_ids: Sequence[str],
    omitted_claim_ids: Sequence[str],
    verdicts: Sequence[Verdict],
) -> None:
    """Upgrade the result audit to a complete replayable batch."""
    path = store.friend_audit_path(round_no, spec.name)
    payload = store.read_owned_bytes(path, max_bytes=MAX_JSON_FILE_BYTES)
    base = decode_json_object(payload, path=path, label="persisted friend audit")
    if set(base) != {"version", "round", "name", "row", "captures"}:
        raise UsageError("persisted friend audit has an invalid shape")
    if base["version"] != 1 or base["round"] != round_no or base["name"] != spec.name:
        raise UsageError("persisted friend audit has the wrong identity")
    if base["row"] != row:
        raise UsageError("persisted friend audit row changed before judging commit")
    judging = {
        "complete": True,
        "judge": friend_key(spec),
        "shown_claim_ids": list(shown_claim_ids),
        "omitted_claim_ids": list(omitted_claim_ids),
        "verdicts": [record_to_dict(verdict) for verdict in verdicts],
    }
    parsed_path = store.friend_paths(round_no, spec.name)[1]
    parsed_batch = {"row": row, "judging": judging}
    parsed_payload = json.dumps(parsed_batch, sort_keys=True)
    store.write_sensitive_atomic(parsed_path, parsed_payload)
    captures = dict(base["captures"])
    captures["parsed"] = "sha256:" + hashlib.sha256(parsed_payload.encode("utf-8")).hexdigest()
    data = {**base, "version": 2, "captures": captures, "judging": judging}
    store.write_sensitive_atomic(path, json.dumps(data, sort_keys=True))


def recover_judging_batch(
    store: RunStore,
    round_no: int,
    spec: FriendSpec,
    shown_claim_ids: Sequence[str],
    prompt_text: str,
    *,
    legacy_complete: bool = False,
) -> RecoveredJudgeBatch | None:
    """Authenticate and return a complete captured batch, if one exists."""
    path = store.friend_audit_path(round_no, spec.name)
    if not store.owned_regular_exists(path):
        return None
    payload = store.read_owned_bytes(path, max_bytes=MAX_JSON_FILE_BYTES)
    data = decode_json_object(payload, path=path, label="persisted friend audit")
    if data.get("version") == 1:
        if legacy_complete:
            return None
        raise UsageError(
            "cannot recover judging: incomplete legacy judging audit has no "
            "authenticated complete verdict batch"
        )
    if data.get("version") != 2:
        return None
    if set(data) != {"version", "round", "name", "row", "captures", "judging"}:
        raise UsageError("persisted judging audit has an invalid shape")
    if data["round"] != round_no or data["name"] != spec.name:
        raise UsageError("persisted judging audit has the wrong identity")
    judging = data["judging"]
    keys = {"complete", "judge", "shown_claim_ids", "omitted_claim_ids", "verdicts"}
    if type(judging) is not dict or set(judging) != keys or judging.get("complete") is not True:
        raise UsageError("persisted judging audit has an incomplete batch")
    expected_shown = list(shown_claim_ids)
    if judging.get("judge") != friend_key(spec) or judging.get("shown_claim_ids") != expected_shown:
        raise UsageError("persisted judging audit does not match the reconstructed prompt slice")
    omitted = judging.get("omitted_claim_ids")
    raw_verdicts = judging.get("verdicts")
    if (
        type(omitted) is not list
        or not all(type(item) is str for item in omitted)
        or len(set(omitted)) != len(omitted)
        or type(raw_verdicts) is not list
    ):
        raise UsageError("persisted judging audit has an invalid verdict batch")
    verdicts: list[Verdict] = []
    for raw in raw_verdicts:
        record = record_from_dict(raw)
        if not isinstance(record, Verdict):
            raise UsageError("persisted judging audit contains a non-verdict record")
        if record.judge != friend_key(spec) or record.round != round_no:
            raise UsageError("persisted judging audit verdict has the wrong identity")
        verdicts.append(record)
    identities = [verdict.claim_id for verdict in verdicts]
    if len(set(identities)) != len(identities) or any(
        item not in expected_shown for item in identities
    ):
        raise UsageError("persisted judging audit verdicts do not match the prompt slice")
    expected_omitted = [claim_id for claim_id in expected_shown if claim_id not in identities]
    if omitted != expected_omitted:
        raise UsageError("persisted judging audit omissions do not complete its prompt slice")
    captures = data.get("captures")
    expected_prompt = "sha256:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    if type(captures) is not dict or captures.get("prompt") != expected_prompt:
        raise UsageError("persisted judging audit prompt disagrees with reconstructed bytes")
    parsed_path = store.friend_paths(round_no, spec.name)[1]
    parsed_payload = store.read_owned_bytes(parsed_path, max_bytes=MAX_JSON_FILE_BYTES)
    if captures.get("parsed") != "sha256:" + hashlib.sha256(parsed_payload).hexdigest():
        raise UsageError("persisted judging audit parsed batch capture was modified")
    parsed = decode_json_object(
        parsed_payload, path=parsed_path, label="persisted normalized judging batch"
    )
    if parsed != {"row": data["row"], "judging": judging}:
        raise UsageError("persisted judging audit disagrees with its parsed batch")
    row = recover_result_audit(store, round_no, spec)
    return RecoveredJudgeBatch(row, tuple(verdicts), tuple(omitted))
