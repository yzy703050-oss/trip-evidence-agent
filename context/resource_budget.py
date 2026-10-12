"""Successful model-request budgets; offline calibration leaves circuit guards intact."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_calibrating = ContextVar("resource_calibration", default=False)
_budget = ContextVar("successful_model_budget", default=None)


def is_calibrating():
    return _calibrating.get()


@contextmanager
def calibration_mode():
    token = _calibrating.set(True)
    try:
        yield
    finally:
        _calibrating.reset(token)


class ModelCallLimitExceeded(Exception):
    pass


@dataclass
class SuccessBudget:
    main_per_turn: int | None = None
    main_per_task: int | None = None
    total: int = 0
    by_task: dict = field(default_factory=dict)

    def before(self, context):
        if not context["stage"].startswith("main:") or is_calibrating():
            return
        if self.main_per_turn is not None and self.total >= self.main_per_turn:
            raise ModelCallLimitExceeded("main_model_calls_per_turn")
        tid = context.get("task_id")
        if (
            tid
            and self.main_per_task is not None
            and self.by_task.get(tid, 0) >= self.main_per_task
        ):
            raise ModelCallLimitExceeded("main_model_calls_per_task")

    def success(self, context):
        if not context["stage"].startswith("main:"):
            return
        self.total += 1
        tid = context.get("task_id")
        if tid:
            self.by_task[tid] = self.by_task.get(tid, 0) + 1


@contextmanager
def success_budget_scope(**limits):
    budget = SuccessBudget(**limits)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


def current_success_budget():
    return _budget.get()


def successful_information_result(result):
    return result.get("status") in {"ok", "partial"} and not result.get("execution", {}).get(
        "stop_reason"
    )
