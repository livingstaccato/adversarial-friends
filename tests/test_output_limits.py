"""A friend's output is bounded in bytes, not only in seconds (c-0002).

Cross-examining normalize.py surfaced this about its neighbours: stdout,
stderr and HTTP response bodies were accumulated with no ceiling, so the
documented timeout bounded how LONG a friend could run but nothing about how
much memory it could make this process hold. A friend looping on output --
the repetition-looping local model this codebase already designs around --
fills RAM until something dies, and the runner dispatches friends
concurrently, so it is not one friend's memory at stake.

Truncated output is never repaired, matching the rule already applied to a
timed-out friend: a killed friend's partial output is a failed round, not a
parsing candidate. Silently keeping a prefix would be worse than failing,
because a prefix can still parse.
"""

import json
import sys

from adversarial_friends import spawn


def test_output_past_the_cap_fails_the_round_instead_of_growing(tmp_path):
    script = (
        "import sys\n"
        "chunk = 'x' * 65536\n"
        "for _ in range(200):\n"
        "    sys.stdout.write(chunk)\n"
        "    sys.stdout.flush()\n"
    )
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.output_truncated is True
    assert outcome.failure_reason is not None
    assert "exceeded" in outcome.failure_reason
    # The whole point: it stopped holding the output, rather than keeping
    # all 12MB the friend tried to send.
    assert len(outcome.stdout) < 2 * 256 * 1024


def test_truncated_output_is_never_parsed_even_when_its_prefix_would(tmp_path):
    """A prefix can be valid JSON. Repairing one would report a friend's
    partial answer as its whole answer."""
    answer = json.dumps({"no_findings": True})
    script = (
        f"import sys\nsys.stdout.write({json.dumps(answer)})\nsys.stdout.flush()\n"
        "chunk = 'x' * 65536\n"
        "for _ in range(200):\n"
        "    sys.stdout.write(chunk)\n"
        "    sys.stdout.flush()\n"
    )
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.output_truncated is True
    assert outcome.result.succeeded is False


def test_output_under_the_cap_is_unaffected(tmp_path):
    answer = json.dumps({"no_findings": True})
    script = f"import sys;sys.stdout.write({json.dumps(answer)})"
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.output_truncated is False
    assert outcome.result.succeeded is True
    assert outcome.failure_reason is None


def test_a_flood_on_stderr_is_bounded_too(tmp_path):
    """stderr is the easier one to overlook: nothing parses it, so an
    unbounded stderr grows silently until the process dies."""
    script = (
        "import sys\n"
        "chunk = 'e' * 65536\n"
        "for _ in range(200):\n"
        "    sys.stderr.write(chunk)\n"
        "    sys.stderr.flush()\n"
    )
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.output_truncated is True
    assert len(outcome.stderr) < 2 * 256 * 1024


def test_the_default_cap_is_generous_enough_for_a_real_critique(tmp_path):
    """The cap must never fire on legitimate output. A large real critique is
    tens of KB; the default is orders of magnitude above that."""
    assert spawn.MAX_OUTPUT_BYTES >= 16 * 1024 * 1024
