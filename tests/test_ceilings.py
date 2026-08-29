"""Tests for run ceilings (spec §7.4).

The one that matters is the derivation. §7.4 exists because version 2 of the
design hard-coded `--max-calls = 60`, which is exactly `4 x 3 x 5` -- so the
shipped defaults tripped their own ceiling the moment a fourth friend was
present, converting a normal run into a truncated one.
"""

from adversarial_friends import ceilings


def test_max_calls_is_derived_from_the_roster():
    assert ceilings.derive_max_calls(4, max_rounds=3, max_loop_iterations=5) == 90


def test_the_default_configuration_does_not_trip_its_own_ceiling():
    """The regression §7.4 was written for. A four-friend roster over three
    rounds and five iterations needs 60 calls; a ceiling of exactly 60 leaves
    no headroom for a single re-invocation."""
    for friends in range(1, 9):
        needed = friends * ceilings.DEFAULT_MAX_ROUNDS * ceilings.DEFAULT_MAX_LOOP_ITERATIONS
        assert ceilings.derive_max_calls(friends) > needed


def test_crossexam_budgets_one_iteration():
    """Budgeting a crossexam for five loop iterations would make its ceiling
    unreachable, which is its own kind of useless."""
    assert ceilings.derive_max_calls(3, max_rounds=3, max_loop_iterations=1) == 14


def test_the_call_ceiling_is_checked_before_spending_it():
    """Noticing the ceiling after dispatching spends the very budget the
    ceiling exists to protect -- real money on a metered CLI."""
    budget = ceilings.Budget(max_calls=4)
    budget.spend(3)
    assert budget.would_exceed_calls(2) is True
    assert budget.would_exceed_calls(1) is False


def test_the_first_ceiling_hit_is_the_one_reported():
    budget = ceilings.Budget(max_calls=1)
    budget.exhaust("calls")
    budget.exhaust("wall clock")
    assert budget.exhausted_by == "calls"


def test_wall_clock_is_measured_against_an_injected_clock():
    budget = ceilings.Budget(max_calls=99, max_wall_clock_s=100, started=1000.0)
    assert budget.out_of_time(1099.0) is False
    assert budget.out_of_time(1100.0) is True


def test_an_unreachable_ceiling_is_warned_about():
    warning = ceilings.warn_if_unreachable(friends=4, max_rounds=3, max_calls=5)
    assert warning is not None
    assert "cannot accommodate" in warning


def test_a_sufficient_ceiling_produces_no_warning():
    assert ceilings.warn_if_unreachable(friends=2, max_rounds=3, max_calls=99) is None


def test_the_unreachable_warning_counts_loop_iterations():
    """c-0007. It assumed one iteration, so a loop configured with an
    operator-set --max-calls started silently and hit budget-exhausted
    mid-run -- the exact outcome the warning exists to pre-empt.

    `derive_max_calls` already multiplied by iterations, so the default and
    the warning disagreed about what the same run costs.
    """
    from adversarial_friends.ceilings import warn_if_unreachable

    # 4 friends x 3 rounds x 5 iterations = 60 calls, not 12.
    assert warn_if_unreachable(4, 3, 12, iterations=1) is None
    warning = warn_if_unreachable(4, 3, 12, iterations=5)
    assert warning is not None
    assert "60 calls minimum" in warning
    assert "5 iterations" in warning


def test_a_single_iteration_warning_does_not_mention_iterations():
    """Every non-loop run passes 1. Saying "x 1 iterations" would be noise
    in the common case."""
    from adversarial_friends.ceilings import warn_if_unreachable

    warning = warn_if_unreachable(4, 3, 5, iterations=1)
    assert warning is not None
    assert "iterations" not in warning
