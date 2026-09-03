"""Read-only discovery of unresolved claims before a human resolution."""

import argparse
import json

import pytest

from adversarial_friends.cliargs import build_parser
from adversarial_friends.commands.resolve import cmd_resolve
from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import Claim, Ledger, Resolution


def _claim(claim_id: str, severity: str, *, location: str = "src/app.py:12") -> Claim:
    return Claim(
        id=claim_id,
        supersedes=None,
        origin=["fake/security"],
        lens="security",
        round=1,
        advisory=False,
        severity=severity,
        claim=f"{severity} problem in the guard",
        location=location,
        evidence=f"{location} demonstrates the problem",
        failure_scenario="untrusted input reaches the handler",
        suggested_fix="validate before dispatch",
    )


def _run(tmp_path, claims: list[Claim], *, states: object = None) -> tuple[object, Ledger]:
    run = tmp_path / "run-discovery"
    run.mkdir()
    metadata: dict[str, object] = {}
    if states is not None:
        metadata["claim_states"] = states
    (run / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    ledger = Ledger(run / "claims.jsonl")
    for claim in claims:
        ledger.append(claim)
    return run, ledger


def _args(run, *, list_claims: bool = False, next_claim: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        run_id=str(run),
        out=None,
        list=list_claims,
        next=next_claim,
        claim=None,
        disposition=None,
        evidence=None,
        author=None,
    )


def test_resolve_list_renders_unresolved_claims_in_priority_order_without_appending(
    tmp_path, capsys
):
    claims = [
        _claim("c-0003@1", "low"),
        _claim("c-0002@1", "high"),
        _claim("c-0001@1", "critical"),
    ]
    run, _ledger = _run(
        tmp_path,
        claims,
        states={claim.id: "settled-upheld" for claim in claims},
    )
    before = (run / "claims.jsonl").read_bytes()

    assert cmd_resolve(_args(run, list_claims=True)) == 0

    output = capsys.readouterr().out
    assert output.index("c-0001@1") < output.index("c-0002@1") < output.index("c-0003@1")
    assert "critical problem in the guard" in output
    assert "location: src/app.py:12" in output
    assert "--disposition fixed|rejected|accepted-risk" in output
    assert "--evidence PATH[:LINE]" in output
    assert (run / "claims.jsonl").read_bytes() == before


def test_resolve_next_prints_the_only_highest_priority_claim_without_appending(tmp_path, capsys):
    high = _claim("c-0001@1", "high")
    low = _claim("c-0002@1", "low")
    run, _ledger = _run(
        tmp_path, [low, high], states={low.id: "settled-upheld", high.id: "unproven"}
    )
    before = (run / "claims.jsonl").read_bytes()

    assert cmd_resolve(_args(run, next_claim=True)) == 0

    output = capsys.readouterr().out
    assert "c-0001@1" in output
    assert "c-0002@1" not in output
    assert f"afriend resolve {run} --claim c-0001@1" in output
    assert (run / "claims.jsonl").read_bytes() == before


def test_resolve_next_refuses_an_ambiguous_highest_priority_claim_without_appending(tmp_path):
    first = _claim("c-0001@1", "high")
    second = _claim("c-0002@1", "high")
    run, _ledger = _run(
        tmp_path, [first, second], states={first.id: "unproven", second.id: "unproven"}
    )
    before = (run / "claims.jsonl").read_bytes()

    with pytest.raises(UsageError, match="choose --claim"):
        cmd_resolve(_args(run, next_claim=True))

    assert (run / "claims.jsonl").read_bytes() == before


def test_resolve_list_supports_legacy_run_metadata_without_claim_states(tmp_path, capsys):
    run, _ledger = _run(tmp_path, [_claim("c-0001@1", "medium")])

    assert cmd_resolve(_args(run, list_claims=True)) == 0

    assert "c-0001@1" in capsys.readouterr().out


def test_resolve_discovery_rejects_invalid_claim_state_metadata_without_appending(tmp_path):
    claim = _claim("c-0001@1", "high")
    run, _ledger = _run(tmp_path, [claim], states=[claim.id])
    before = (run / "claims.jsonl").read_bytes()

    with pytest.raises(UsageError, match="claim_states"):
        cmd_resolve(_args(run, list_claims=True))

    assert (run / "claims.jsonl").read_bytes() == before


def test_resolve_parser_accepts_discovery_without_write_fields():
    parsed = build_parser().parse_args(["resolve", "run-1", "--list"])

    assert parsed.list is True
    assert parsed.claim is None


def test_resolve_parser_keeps_complete_write_contract_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["resolve", "run-1", "--claim", "c-0001@1"])


def test_resolve_list_omits_already_resolved_claims(tmp_path, capsys):
    resolved = _claim("c-0001@1", "high")
    pending = _claim("c-0002@1", "medium")
    run, ledger = _run(
        tmp_path,
        [resolved, pending],
        states={resolved.id: "settled-upheld", pending.id: "settled-upheld"},
    )
    ledger.append(
        Resolution(
            claim_id=resolved.id,
            disposition="accepted-risk",
            author="test",
            evidence="src/app.py:12",
            round=1,
            verified="location-unchanged",
        )
    )

    assert cmd_resolve(_args(run, list_claims=True)) == 0

    output = capsys.readouterr().out
    assert pending.id in output
    assert resolved.id not in output


def test_resolve_discovery_does_not_change_run_directory_permissions(tmp_path, capsys):
    claim = _claim("c-0001@1", "high")
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})
    run.chmod(0o755)
    before_mode = run.stat().st_mode

    assert cmd_resolve(_args(run, list_claims=True)) == 0

    assert "c-0001@1" in capsys.readouterr().out
    assert run.stat().st_mode == before_mode
