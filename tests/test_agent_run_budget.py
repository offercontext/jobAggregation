from __future__ import annotations

import math

import pytest

from offerpilot.agent_runtime.budget import (
    JOURNAL_DEFAULT_BUSY_TIMEOUT_MS,
    JOURNAL_DISPOSITION_BUDGET_SECONDS,
    JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS,
    JOURNAL_OPERATION_HARD_CAP_SECONDS,
    JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS,
    JOURNAL_SQLITE_PROGRESS_STEPS,
    ActiveWorkBudget,
    JournalBudgetExhausted,
    JournalDeadlineExceeded,
    MonotonicSample,
)


class ScriptedClock:
    def __init__(self, values: list[float | BaseException]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


def test_budget_constants_are_the_versioned_contract() -> None:
    assert JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS == 0.150
    assert JOURNAL_OPERATION_HARD_CAP_SECONDS == 0.050
    assert JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS == 0.005
    assert JOURNAL_DISPOSITION_BUDGET_SECONDS == 0.050
    assert JOURNAL_SQLITE_PROGRESS_STEPS == 100
    assert JOURNAL_DEFAULT_BUSY_TIMEOUT_MS == 50


def test_negative_monotonic_origin_is_valid_when_values_increase() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([-10.0, -9.5, -9.0]))

    assert [budget.safe_monotonic_read().valid for _ in range(3)] == [True, True, True]
    assert budget.used_seconds == 0.0
    assert budget.clock_invalid_latched is False


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_invalid_sample_has_no_budget_or_diagnostic_side_effect(value: object) -> None:
    budget = ActiveWorkBudget(0.150, lambda: value)  # type: ignore[return-value]

    sample = budget.safe_monotonic_read()

    assert sample.valid is False
    assert budget.used_seconds == 0.0
    assert budget.clock_invalid_latched is False


def test_decreasing_sample_is_invalid_without_replacing_last_valid_value() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([10.0, 9.0, 10.5]))

    assert budget.safe_monotonic_read() == MonotonicSample(10.0, True)
    assert budget.safe_monotonic_read() == MonotonicSample(9.0, False)
    assert budget.safe_monotonic_read() == MonotonicSample(10.5, True)
    assert budget.used_seconds == 0.0
    assert budget.clock_invalid_latched is False


@pytest.mark.parametrize(
    "failure",
    [Exception("clock failure"), KeyboardInterrupt(), SystemExit(), BaseException("base")],
)
def test_clock_failures_are_total_and_have_no_budget_side_effects(
    failure: BaseException,
) -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([failure]))

    sample = budget.safe_monotonic_read()

    assert sample == MonotonicSample(0.0, False)
    assert budget.used_seconds == 0.0
    assert budget.clock_invalid_latched is False


def test_safe_adapter_keeps_sample_no_throw_and_require_value_seals_reason() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([KeyboardInterrupt(), SystemExit()]))
    adapter = budget.safe_clock_adapter()

    assert adapter.sample().valid is False
    with pytest.raises(JournalDeadlineExceeded) as error:
        adapter.require_value()

    assert error.value.reason == "clock_invalid"
    assert str(error.value) == "journal deadline exhausted"


def test_invalid_final_sample_after_valid_entry_saturates_and_latches() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([1.0, float("nan")]))
    entry = budget.safe_monotonic_read()

    assert entry.valid is True
    assert budget.finish_operation(entry) is True
    assert budget.used_seconds == budget.total_seconds
    assert budget.clock_invalid_latched is True


def test_begin_operation_requires_valid_entry_and_uses_remaining_allowance() -> None:
    budget = ActiveWorkBudget(0.150, lambda: 1.0)

    with pytest.raises(JournalDeadlineExceeded) as error:
        budget.begin_operation(MonotonicSample(0.0, False), 0.050)
    assert error.value.reason == "clock_invalid"

    budget.used_seconds = 0.120
    lease = budget.begin_operation(MonotonicSample(10.0, True), 0.050)

    assert lease.entry_started_at == 10.0
    assert lease.hard_deadline == pytest.approx(10.030)
    assert lease.work_deadline == pytest.approx(10.025)
    assert lease.budget is budget


def test_begin_operation_rejects_allowance_equal_to_cleanup_reserve() -> None:
    budget = ActiveWorkBudget(0.150, lambda: 1.0)
    budget.used_seconds = 0.145001

    with pytest.raises(JournalBudgetExhausted):
        budget.begin_operation(MonotonicSample(0.0, True), 0.050)


def test_operation_checkpoint_uses_budget_reader_and_work_deadline() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([0.0, 0.0449, 0.0451]))
    entry = budget.safe_monotonic_read()
    lease = budget.begin_operation(entry, 0.050)

    lease.checkpoint()
    with pytest.raises(JournalBudgetExhausted):
        lease.checkpoint()


def test_operation_checkpoint_latches_invalid_clock_and_raises_sealed_error() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([0.0, math.nan]))
    entry = budget.safe_monotonic_read()
    lease = budget.begin_operation(entry, 0.050)

    with pytest.raises(JournalDeadlineExceeded) as error:
        lease.checkpoint()

    assert error.value.reason == "clock_invalid"
    assert budget.used_seconds == budget.total_seconds
    assert budget.clock_invalid_latched is True


def test_finish_operation_charges_nonnegative_elapsed_and_caps_total() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([10.0, 10.020, 10.500]))
    entry = budget.safe_monotonic_read()

    assert budget.finish_operation(entry) is False
    assert budget.used_seconds == pytest.approx(0.020)

    next_entry = budget.safe_monotonic_read()
    assert budget.finish_operation(next_entry) is True
    assert budget.used_seconds == budget.total_seconds


def test_latch_clock_invalid_saturates_budget_without_reading_clock() -> None:
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0

    budget = ActiveWorkBudget(0.150, clock)
    budget.used_seconds = 0.050

    budget.latch_clock_invalid()

    assert budget.used_seconds == budget.total_seconds
    assert budget.clock_invalid_latched is True
    assert calls == 0


def test_safe_clock_adapter_returns_fresh_adapter_for_operation_lease() -> None:
    budget = ActiveWorkBudget(0.150, lambda: 1.0)
    lease = budget.begin_operation(MonotonicSample(1.0, True), 0.050)

    assert lease.safe_clock is not lease.safe_clock
    assert lease.safe_clock._reader == budget.safe_monotonic_read


def test_invalid_final_sample_does_not_charge_previous_usage_before_latching() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([0.0, 0.010, float("inf")]))
    first_entry = budget.safe_monotonic_read()
    assert budget.finish_operation(first_entry) is False
    assert budget.used_seconds == pytest.approx(0.010)

    second_entry = budget.safe_monotonic_read()
    assert second_entry.valid is False
    assert budget.finish_operation(MonotonicSample(0.010, True)) is True
    assert budget.used_seconds == budget.total_seconds
    assert budget.clock_invalid_latched is True
