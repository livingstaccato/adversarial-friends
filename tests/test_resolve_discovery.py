"""Read-only discovery of unresolved claims before a human resolution."""

import argparse
from dataclasses import replace
import json
import shlex

import pytest

from afriend.cliargs import build_parser
from afriend.commands.resolve import cmd_resolve
from afriend.errors import UsageError
from afriend.ledger import MAX_LEDGER_LINE_BYTES, Claim, Ledger, Resolution


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


def test_resolve_discovery_rejects_an_oversized_ledger_record_without_mutating(tmp_path):
    claim = _claim("c-0001@1", "high")
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})
    oversized = b"x" * (MAX_LEDGER_LINE_BYTES + 1) + b"\n"
    (run / "claims.jsonl").write_bytes(oversized)
    run.chmod(0o755)
    before_mode = run.stat().st_mode

    with pytest.raises(UsageError, match="line 1 exceeds"):
        cmd_resolve(_args(run, list_claims=True))

    assert (run / "claims.jsonl").read_bytes() == oversized
    assert run.stat().st_mode == before_mode


@pytest.mark.parametrize(
    "claim_id",
    ["c-0001@1 extra", "c-0001@1\nsecond-line", "c-0001@1; echo unsafe"],
)
def test_resolve_discovery_rejects_malformed_claim_ids_without_mutating(tmp_path, claim_id):
    claim = replace(_claim("c-0001@1", "high"), id=claim_id)
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})
    run.chmod(0o755)
    before = (run / "claims.jsonl").read_bytes()
    before_mode = run.stat().st_mode

    with pytest.raises(UsageError, match="malformed claim id"):
        cmd_resolve(_args(run, list_claims=True))

    assert (run / "claims.jsonl").read_bytes() == before
    assert run.stat().st_mode == before_mode


def test_resolve_next_quotes_a_spaced_run_directory(tmp_path, capsys):
    claim = _claim("c-0001@1", "high")
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})
    spaced = tmp_path / "run with space"
    run.rename(spaced)

    assert cmd_resolve(_args(spaced, next_claim=True)) == 0

    output = capsys.readouterr().out
    assert f"afriend resolve {shlex.quote(str(spaced))} --claim {shlex.quote(claim.id)}" in output


def test_resolve_discovery_rejects_an_invalid_claim_severity_without_mutating(tmp_path):
    claim = _claim("c-0001@1", "urgent")
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})
    before = (run / "claims.jsonl").read_bytes()

    with pytest.raises(UsageError, match="malformed claim severity"):
        cmd_resolve(_args(run, list_claims=True))

    assert (run / "claims.jsonl").read_bytes() == before


def test_resolve_discovery_neutralizes_terminal_and_bidi_controls_in_display_fields(
    tmp_path, capsys
):
    hostile = "safe\x1b]8;;https://evil.test\x07LINK\x1b]8;;\x07\u202eflip\x00end"
    claim = replace(
        _claim("c-0001@1", "high"),
        claim=hostile,
        location=hostile,
        evidence=hostile,
    )
    run, _ledger = _run(tmp_path, [claim], states={claim.id: "settled-upheld"})

    assert cmd_resolve(_args(run, list_claims=True)) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "\x00" not in output
