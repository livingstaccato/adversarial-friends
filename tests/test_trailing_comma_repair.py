"""Trailing-comma repair: linear, and aware of string boundaries.

Both properties came out of a cross-examination of normalize.py.

The regex this replaced was `(?:,\\s*)+(?=[}\\]])`. It was documented as
"Verified linear with this pattern", and it is -- on input where the comma
run is actually followed by a closing bracket. On a run that is NOT (a
repetition-looping local model emitting commas forever, which is the exact
case the comment cites) every start position rescans the whole run, and the
cost goes quadratic: 16k commas took 7.5 seconds, against 0.3ms for the same
count with a bracket. That work happens in `normalize()`, which runs AFTER
the process was killed -- so untrusted output kept burning CPU past the
timeout meant to bound it.

Being a flat regex, it was also blind to string boundaries and rewrote
comma-space runs occurring INSIDE JSON string literals.
"""

import time

from adversarial_friends.normalize import drop_trailing_commas


def test_a_trailing_comma_run_is_dropped():
    assert drop_trailing_commas("[1,,,]") == "[1]"
    assert drop_trailing_commas('{"a":1, , }') == '{"a":1}'


def test_a_separating_comma_is_left_alone():
    assert drop_trailing_commas("[1, 2]") == "[1, 2]"
    assert drop_trailing_commas('{"a":1, "b":2}') == '{"a":1, "b":2}'


def test_a_comma_inside_a_string_is_never_touched():
    """The repair is structural, so it has no business editing content.

    `{"a": "x, }"}` carries a comma, a space and a brace INSIDE the string
    value. The old flat regex rewrote it to `{"a": "x}"}` -- still valid
    JSON, with a silently different value.
    """
    original = '{"a": "x, }"}'
    assert drop_trailing_commas(original) == original
    assert drop_trailing_commas('{"a": "trailing,, ]"}') == '{"a": "trailing,, ]"}'


def test_an_escaped_quote_does_not_end_the_string():
    text = '{"a": "he said \\", }", "b": 1}'
    assert drop_trailing_commas(text) == text


def test_an_unterminated_comma_run_stays_linear():
    """The case the old pattern was quadratic on. Doubling the input must
    roughly double the time, not quadruple it."""

    def elapsed(n: int) -> float:
        text = "," * n
        start = time.perf_counter()
        drop_trailing_commas(text)
        return time.perf_counter() - start

    elapsed(20_000)  # warm up, so the first call's import cost is not measured
    small = elapsed(50_000)
    large = elapsed(200_000)
    # 4x the input. Linear would be ~4x; the old regex was ~30x by this point.
    assert large < small * 12 + 0.05, (small, large)


def test_a_huge_unterminated_run_finishes_promptly():
    start = time.perf_counter()
    drop_trailing_commas("," * 200_000)
    assert time.perf_counter() - start < 1.0
