"""Program-owned execution phases, independent from durable TripPlan business state."""

import json
from copy import deepcopy
from hashlib import sha256

from trip_assistant.runtime.errors import ToolFailure

TRANSITIONS = {
    "deciding": {"executing", "reply_ready", "stopping", "paused"},
    "executing": {"validating", "stopping", "paused"},
    "validating": {"deciding", "repair_required", "reply_ready", "stopping", "paused"},
    "repair_required": {"deciding", "executing", "reply_ready", "stopping", "paused"},
    "stopping": {"reply_ready", "paused"},
    "reply_ready": {"completed", "stopped"},
    "completed": set(),
    "stopped": set(),
    "paused": set(),
}
LIMIT_CODES = {
    "PLANNING_ROUND_LIMIT_EXCEEDED": "planning_round_limit_exceeded",
    "TURN_DEADLINE_EXCEEDED": "turn_deadline_exceeded",
    "AUTOMATIC_REFRESH_LIMIT": "automatic_refresh_limit",
}


def decision_role(results):
    role = "verifier"
    for result in results:
        if result.get("name") == "request_replan" and result.get("status") == "ok":
            role = "main"
        elif (
            result.get("name") == "write_trip"
            and result.get("status") in {"completed", "partial"}
            and result.get("trip_plan_id")
        ):
            role = "verifier"
    return role


def goal_partial_reply(result, calls):
    validation = result.get("validation") or {}
    assessment = validation.get("goal_assessment") or {}
    blocking = {i["code"] for i in validation.get("issues", []) if i.get("blocking")}
    return (
        result.get("status") == "partial"
        and result.get("stop_reason") == "goal_not_satisfied"
        and result.get("stop_planning")
        and result.get("trip_plan_id")
        and result.get("revision") is not None
        and assessment.get("status") in {"partial", "unmet", "unknown"}
        and "goal_not_satisfied" in blocking
        and blocking <= {"goal_not_satisfied", "policy_unverified", "policy_constraint_unverified"}
        and any(
            c["id"] == result["call_id"]
            and (submitted := c.get("arguments", {}).get("goal_assessment") or {}).get("status")
            == assessment["status"]
            and (submitted.get("reason") or "").strip() == assessment.get("reason")
            for c in calls
        )
    )


class ExecutionState:
    @staticmethod
    def ready_reply(calls, results):
        """A reply belongs to the actual batch outcome, never a prose success claim."""
        accepted = {
            r["call_id"]
            for r in results
            if r.get("name") == "write_trip"
            and r.get("operation") in {"select", "amend"}
            and (
                r.get("status") == "completed"
                or goal_partial_reply(r, calls)
                or (
                    r.get("status") == "partial"
                    and r.get("stop_planning")
                    and r.get("trip_plan_id")
                    and r.get("revision") is not None
                    and all(
                        i["code"]
                        in {
                            "policy_unverified",
                            "policy_constraint_unverified",
                            "hotel_price_unknown",
                            "total_budget_unknown",
                        }
                        for i in (r.get("validation") or {}).get("issues", [])
                        if i.get("blocking")
                    )
                    and (
                        (
                            r.get("stop_reason") == "policy_unverified"
                            and bool(
                                policy_codes := {
                                    i["code"]
                                    for i in (r.get("validation") or {}).get("issues", [])
                                    if i.get("blocking")
                                }
                            )
                            and policy_codes
                            <= {"policy_unverified", "policy_constraint_unverified"}
                        )
                        or any(
                            c["id"] == r["call_id"]
                            and c.get("arguments", {}).get("tripplan", {}).get("allow_repair")
                            is False
                            for c in calls
                        )
                    )
                )
            )
        }
        if not all(
            r.get("status") in {"ok", "completed"} or r.get("call_id") in accepted for r in results
        ):
            return None
        if {c["id"] for c in calls} - {r.get("call_id") for r in results}:
            return None
        by_call = {r.get("call_id"): r for r in results}
        if any(by_call[c["id"]].get("rendered_answer") for c in calls if c["id"] in accepted):
            answers = [
                by_call[c["id"]].get("rendered_answer")
                or c.get("arguments", {}).get("final_answer")
                for c in calls
                if c["id"] in accepted
            ]
            return "\n\n".join(dict.fromkeys(answer for answer in answers if answer)) or None
        return next(
            (
                by_call[c["id"]].get("rendered_answer")
                or c.get("arguments", {}).get("final_answer")
                for c in calls
                if c["id"] in accepted
                and (
                    by_call[c["id"]].get("rendered_answer")
                    or c.get("arguments", {}).get("final_answer")
                )
            ),
            None,
        )

    def __init__(self, snapshot=None):
        snapshot = snapshot or {}
        self.phase = snapshot.get("phase", "deciding")
        if self.phase not in TRANSITIONS:
            raise ToolFailure("INVALID_EXECUTION_STATE", "unknown execution phase")
        self.blocked = deepcopy(snapshot.get("blocked_evidence", []))
        self.stop_reason = snapshot.get("stop_reason")
        self.errors = deepcopy(snapshot.get("errors", []))
        self.review_fingerprint = snapshot.get("review_fingerprint")
        self.unchanged_reviews = snapshot.get("unchanged_reviews", 0)

    def observe_review(self, review):
        """Detect unchanged business/evidence, ignoring receipts and prose summaries."""
        states = []
        for plan in review.get("tripplans") or [review.get("tripplan", {})]:
            tasks = []
            for task in plan.get("tasks", []):
                current = {
                    k: task.get(k)
                    for k in (
                        "id",
                        "origin",
                        "destination",
                        "purpose",
                        "requires_hotel",
                        "conditions",
                        "field_sources",
                        "condition_strengths",
                        "content",
                        "status",
                    )
                }
                current["draft_plan"] = {
                    k: v for k, v in (task.get("draft_plan") or {}).items() if k != "task_revision"
                }
                tasks.append(current)
            states.append(
                {
                    "trip_plan_id": plan.get("trip_plan_id"),
                    "tasks": tasks,
                    "conditions": plan.get("confirmed_conditions"),
                    "policy": plan.get("policy_context"),
                    "candidates": plan.get("candidates"),
                    "pending_queries": plan.get("pending_queries"),
                    "issues": [
                        {k: issue.get(k) for k in ("code", "task_ids", "blocking", "evidence")}
                        for issue in (plan.get("validation") or {}).get("issues", [])
                    ],
                }
            )
        fingerprint = sha256(
            json.dumps(states, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.unchanged_reviews = (
            self.unchanged_reviews + 1 if fingerprint == self.review_fingerprint else 0
        )
        self.review_fingerprint = fingerprint
        if self.unchanged_reviews >= 2:
            self.stop("no_progress")

    def transition(self, phase):
        if phase == self.phase:
            return
        if phase not in TRANSITIONS[self.phase]:
            raise ToolFailure(
                "INVALID_STATE_TRANSITION",
                "execution phase does not permit this operation",
                {"from": self.phase, "to": phase},
            )
        self.phase = phase

    @property
    def summary_only(self):
        return self.phase in {"stopping", "reply_ready", "completed", "stopped", "paused"}

    def stop(self, reason):
        self.stop_reason = self.stop_reason or reason
        if self.phase not in {"stopping", "reply_ready"}:
            self.transition("stopping")

    def learn(self, results):
        for result in results:
            for details in result.get("automatic_refresh", result).get("invalidated_evidence", []):
                if details not in self.blocked:
                    self.blocked.append(deepcopy(details))
            error = result.get("error") or {}
            if error.get("code") == "EVIDENCE_EXPIRED":
                details = error.get("details", {})
                if details not in self.blocked:
                    self.blocked.append(deepcopy(details))

    def observe(self, results):
        self.learn(results)
        self.errors = [
            {k: deepcopy(r[k]) for k in ("call_id", "name", "status", "error", "reason") if k in r}
            for r in results
            if r.get("status") in {"error", "rejected", "unknown"}
        ]
        for result in results:
            code = (result.get("error") or {}).get("code")
            if code == "EVIDENCE_REFRESH_REQUIRED":
                self.stop("no_progress")
                return
            if code in LIMIT_CODES:
                self.stop(LIMIT_CODES[code])
                return
            if result.get("stop_planning"):
                self.stop(result.get("stop_reason") or "tool_requested_stop")
                return
        self.transition("repair_required" if self.errors else "deciding")

    def guard(self, calls):
        if self.summary_only:
            raise ToolFailure("EXECUTION_STOPPED", "execution no longer permits tools")
        for call in calls:
            if call["name"] != "write_trip":
                continue
            plan = call.get("arguments", {}).get("tripplan", {})
            for task in plan.get("tasks", []):
                for domain, ref in task.get("selection_refs", {}).items():
                    if any(
                        row.get("trip_plan_id") == plan.get("trip_plan_id")
                        and row.get("query_id") == ref.get("query_id")
                        and row.get("result_revision") == ref.get("result_revision")
                        for row in self.blocked
                    ):
                        raise ToolFailure(
                            "EVIDENCE_REFRESH_REQUIRED",
                            "refresh expired evidence before selecting",
                            {
                                "call_id": call["id"],
                                "trip_plan_id": plan.get("trip_plan_id"),
                                "task_id": task.get("task_id"),
                                "domain": domain,
                                **ref,
                            },
                        )

    def checkpoint(self):
        return {
            "phase": self.phase,
            "blocked_evidence": deepcopy(self.blocked),
            "stop_reason": self.stop_reason,
            "errors": deepcopy(self.errors),
            "review_fingerprint": self.review_fingerprint,
            "unchanged_reviews": self.unchanged_reviews,
        }
