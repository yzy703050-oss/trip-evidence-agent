"""A single persisted budget for one active user turn."""

from time import monotonic


class LimitError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class TurnBudget:
    def __init__(self, limits, snapshot=None, clock=monotonic):
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.saved = dict(snapshot or {})
        self.rounds = self.saved.get("planning_rounds_used", 0)
        self.active_seconds = self.saved.get("active_seconds", 0)
        self.planning = self.saved.get("planning_started", self.rounds > 0)
        self.query_keys = set(self.saved.get("external_query_keys", []))

    def remaining_seconds(self):
        return max(
            0, self.limits.turn_timeout - self.active_seconds - (self.clock() - self.started)
        )

    def require_time(self):
        if self.remaining_seconds() <= 0:
            raise LimitError("turn_deadline_exceeded")

    def reserve_batch(self, calls, query_keys, starts_planning):
        self.require_time()
        planning = self.planning or starts_planning
        if planning and self.rounds >= self.limits.planning_rounds:
            raise LimitError("planning_round_limit_exceeded")
        if planning:
            self.rounds += 1
        self.planning = planning
        if len(calls) > self.limits.tools_per_batch:
            raise LimitError("tool_limit_exceeded")
        self.query_keys = set()
        self.check_external_queries(query_keys)

    def check_external_queries(self, query_keys):
        combined = self.query_keys | set(query_keys)
        if len(combined) > self.limits.external_queries_per_batch:
            raise LimitError("external_query_limit_exceeded")
        self.query_keys = combined

    def checkpoint(self):
        return {
            "planning_rounds_used": self.rounds,
            "planning_started": self.planning,
            "external_query_keys": sorted(self.query_keys),
            "active_seconds": self.active_seconds + self.clock() - self.started,
        }
