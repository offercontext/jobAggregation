from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS = 0.150
JOURNAL_OPERATION_HARD_CAP_SECONDS = 0.050
JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS = 0.005
JOURNAL_DISPOSITION_BUDGET_SECONDS = 0.050
JOURNAL_SQLITE_PROGRESS_STEPS = 100
JOURNAL_DEFAULT_BUSY_TIMEOUT_MS = 50


class JournalBudgetExhausted(RuntimeError):
    """Raised when a Journal operation has no usable active-work allowance."""


class JournalDeadlineExceeded(RuntimeError):
    """Raised when an operation cannot obtain a valid clock or met its deadline."""

    def __init__(self, reason: Literal["deadline", "clock_invalid"] = "deadline") -> None:
        if reason not in {"deadline", "clock_invalid"}:
            raise ValueError("invalid journal deadline reason")
        super().__init__("journal deadline exhausted")
        self.reason: Literal["deadline", "clock_invalid"] = reason


@dataclass(frozen=True)
class MonotonicSample:
    value: float
    valid: bool


@dataclass(frozen=True, repr=False)
class SafeClockAdapter:
    """Closed adapter for deadline consumers and callbacks that cannot raise."""

    _reader: Callable[[], MonotonicSample]

    def sample(self) -> MonotonicSample:
        try:
            sample = self._reader()
        except BaseException:
            return MonotonicSample(0.0, False)
        if not isinstance(sample, MonotonicSample):
            return MonotonicSample(0.0, False)
        if sample.valid is not True:
            return MonotonicSample(0.0, False)
        if type(sample.value) not in {int, float} or not math.isfinite(sample.value):
            return MonotonicSample(0.0, False)
        return MonotonicSample(float(sample.value), True)

    def require_value(self) -> float:
        sample = self.sample()
        if sample.valid is not True:
            raise JournalDeadlineExceeded("clock_invalid")
        return sample.value


@dataclass(repr=False)
class ActiveWorkBudget:
    total_seconds: float
    clock: Callable[[], float]
    used_seconds: float = 0.0
    clock_invalid_latched: bool = False
    _last_valid: float | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def safe_monotonic_read(self) -> MonotonicSample:
        try:
            raw = self.clock()
            if type(raw) not in {int, float} or not math.isfinite(raw):
                return MonotonicSample(0.0, False)
            value = float(raw)
        except BaseException:
            return MonotonicSample(0.0, False)

        try:
            with self._lock:
                if self._last_valid is not None and value < self._last_valid:
                    return MonotonicSample(value, False)
                self._last_valid = value
        except BaseException:
            return MonotonicSample(0.0, False)
        return MonotonicSample(value, True)

    def begin_operation(
        self,
        entry: MonotonicSample,
        hard_cap_seconds: float,
    ) -> OperationLease:
        if entry.valid is not True:
            raise JournalDeadlineExceeded("clock_invalid")
        with self._lock:
            allowance = min(hard_cap_seconds, self.total_seconds - self.used_seconds)
        if allowance <= JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS:
            raise JournalBudgetExhausted
        hard_deadline = entry.value + allowance
        return OperationLease(
            budget=self,
            entry_started_at=entry.value,
            work_deadline=hard_deadline - JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS,
            hard_deadline=hard_deadline,
        )

    def finish_operation(self, entry: MonotonicSample) -> bool:
        if entry.valid is not True:
            self.latch_clock_invalid()
            return True
        final = self.safe_monotonic_read()
        if final.valid is not True:
            self.latch_clock_invalid()
            return True
        try:
            elapsed = max(0.0, final.value - entry.value)
            with self._lock:
                self.used_seconds = min(self.total_seconds, self.used_seconds + elapsed)
                return self.used_seconds >= self.total_seconds
        except BaseException:
            self.latch_clock_invalid()
            return True

    def latch_clock_invalid(self) -> None:
        try:
            with self._lock:
                self.used_seconds = self.total_seconds
                self.clock_invalid_latched = True
        except BaseException:
            # The standard lock and dataclass fields are safe, but this transition is
            # deliberately fail-closed if a caller mutates internals in a test.
            self.used_seconds = self.total_seconds
            self.clock_invalid_latched = True

    def safe_clock_adapter(self) -> SafeClockAdapter:
        return SafeClockAdapter(self.safe_monotonic_read)


@dataclass(frozen=True, repr=False)
class OperationLease:
    budget: ActiveWorkBudget
    entry_started_at: float
    work_deadline: float
    hard_deadline: float

    @property
    def safe_clock(self) -> SafeClockAdapter:
        return self.budget.safe_clock_adapter()

    def checkpoint(self) -> None:
        sample = self.budget.safe_monotonic_read()
        if sample.valid is not True:
            self.budget.latch_clock_invalid()
            raise JournalDeadlineExceeded("clock_invalid")
        if sample.value >= self.work_deadline:
            raise JournalBudgetExhausted
