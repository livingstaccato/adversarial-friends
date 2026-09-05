"""Truncated output must not reach the orchestrator extraction path (c-0004).

spawn's docstring says a killed friend's truncated output "never enters the
repair path". That was true of normalize(), and only of normalize():
commands/critique.py hands `result.stdout` verbatim to §14.2 extraction,
gated on `payload is None` -- which is exactly what a timed-out or
overflowed SpawnResult carries.

So the one guarantee spawn states most plainly had a second reader that
ignored it. A prefix of a friend's answer can be perfectly valid JSON, and
extraction is the path *designed* to salvage meaning from raw text, which
makes it the worst place for a truncated buffer to arrive.
"""

import json

from afriend.commands.critique import extraction_candidates
from afriend.normalize import NormalizeResult
from afriend.spawn import SpawnResult

PREFIX = json.dumps({"findings": [{"severity": "high", "claim": "half a thought"}]})[:-2]


def _result(*, timed_out=False, truncated=False, reason="exit 1") -> SpawnResult:
    return SpawnResult(
        argv=["friend"],
        exit_code=1,
        stdout=PREFIX,
        stderr="",
        duration_s=1.0,
        timed_out=timed_out,
        result=NormalizeResult(None, ["unparseable"], False),
        failure_reason=reason,
        orphans_suspected=False,
        output_truncated=truncated,
    )


def test_a_timed_out_result_is_not_offered_for_extraction():
    assert extraction_candidates(_result(timed_out=True, reason="timeout")) is False


def test_an_overflowed_result_is_not_offered_for_extraction():
    assert extraction_candidates(_result(truncated=True, reason="output exceeded")) is False


def test_an_ordinary_unparseable_result_is_still_offered():
    """The case the extraction path exists for: a friend that ran to
    completion and produced something a human could still read."""
    assert extraction_candidates(_result()) is True


def test_empty_stdout_is_never_offered():
    result = _result()
    result.stdout = "   "
    assert extraction_candidates(result) is False
