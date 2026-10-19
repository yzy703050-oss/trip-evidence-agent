"""Offline fixtures extracted from test_consolidated_travel_run; no test-module imports."""

from copy import deepcopy


class BatchMain:
    def __init__(self):
        self.calls = 0
        self.views = []

    async def initialize(self, context):
        self.calls += 1
        return {
            "response_mode": "workflow",
            "agent_schedule": [],
            "workflow_proposal": {
                "confirmed_conditions": {},
                "tasks": [
                    {
                        "origin": "重庆",
                        "destination": "上海",
                        "purpose": "visit",
                        "requires_hotel": True,
                        "conditions": {
                            "departure_date": "2026-10-11",
                            "check_in": "2026-10-11",
                            "check_out": "2026-10-14",
                        },
                    },
                    {
                        "origin": "上海",
                        "destination": "重庆",
                        "purpose": "return",
                        "requires_hotel": False,
                        "conditions": {"departure_date": "2026-10-14"},
                    },
                ],
            },
        }

    async def select_batch(self, context):
        self.calls += 1
        self.views.append(deepcopy(context))
        tasks = []
        for task in context["workflow"]["tasks"]:
            refs = {}
            for row in context["task_results"]:
                if row.get("task_id") == task["id"] and row.get("items"):
                    refs[row["domain"]] = {
                        "query_id": row.get("query_id") or row["id"],
                        "result_revision": row["result_revision"],
                        "candidate_id": row["items"][0]["id"],
                    }
            tasks.append(
                {
                    "task_id": task["id"],
                    "task_revision": task["revision"],
                    "draft_plan": {
                        "task_revision": task["revision"],
                        "train_selection": refs.get("train"),
                        "hotel_selection": refs.get("hotel"),
                        "schedule": {
                            k: v
                            for k, v in task["conditions"].items()
                            if k in {"check_in", "check_out"}
                        },
                        "unverified_requirements": [],
                    },
                }
            )
        return {
            "action": "commit_batch",
            "expected_workflow_revision": context["workflow"]["revision"],
            "draft_tasks": tasks,
            "reply_template": "这趟出行可以这样安排。",
        }
