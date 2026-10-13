import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_consolidated_travel_run import BatchMain
from test_information_tools import Provider
from workflow_runtime import OfflineProvider

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.tools import ToolExecutor


@pytest.mark.asyncio
async def test_rag_hotel_cap_conflict_requeries_hotel_without_replaying_trains(tmp_path):
    memory = MemoryManager("alice", "policy", str(tmp_path))

    class Retrieval:
        calls = 0

        async def search(self, query, limit):
            self.calls += 1
            return [
                {
                    "id": "SQ-POL-2026-09#hotel",
                    "content": "普通员工上海住宿500元/晚；品牌偏好不豁免上限。超标需事先审批。",
                    "metadata": {
                        "document_id": "SQ-POL-2026-09",
                        "location": "第三条",
                        "version": "2026-09",
                    },
                }
            ]

    retrieval = Retrieval()

    async def extract(messages):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "status": "ok",
                    "constraints": [
                        {
                            "id": "sh-hotel",
                            "rule": "无超标审批，上海住宿不得超过500元每晚",
                            "hard": True,
                            "field": "hotel_max_nightly_cny",
                            "operator": "<=",
                            "value": 500,
                            "unit": "CNY/night",
                            "applicability": {"city": "上海"},
                            "evidence_ids": ["SQ-POL-2026-09#hotel"],
                        }
                    ],
                    "applicability": {},
                    "references": [],
                    "effective_date": "2026-09-01",
                    "missing_fields": [],
                    "conflicts": [],
                }
            )
        )

    class Main(BatchMain):
        model = MeteredModel(extract, memory.session_store.append_run)

        async def initialize(self, context):
            assert context["policy_context"]["constraints"][0]["value"] == 500
            value = await super().initialize(context)
            value["workflow_proposal"]["tasks"][0]["purpose"] = "business"
            return value

        async def select_batch(self, context):
            if context.get("action_feedback"):
                self.calls += 1
                codes = {i["code"] for i in context["action_feedback"]["issues"]}
                assert "hotel_selection_missing" in codes
                task = context["workflow"]["tasks"][0]
                return {
                    "action": "schedule_queries",
                    "query_plan": [
                        {
                            "task_id": task["id"],
                            "task_revision": task["revision"],
                            "domain": "hotel",
                            "parameters": {
                                "city": "上海",
                                "check_in": "2026-10-11",
                                "check_out": "2026-10-14",
                                "guests": 1,
                                "refresh": True,
                            },
                        }
                    ],
                }
            return await super().select_batch(context)

    class Hotel(Provider):
        async def search(self, query):
            result = await super().search(query)
            price = "2100" if len(self.queries) == 1 else "900"
            return replace(result, items=[{**i, "stay_total_cny": price} for i in result.items])

    main = Main()
    train, hotel = OfflineProvider("train"), Hotel()
    harness = ExecutionHarness(main, {"rag_knowledge": retrieval}, memory)
    harness.runner.executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    result = await harness.run_turn(
        {
            "original_query": "重庆上海出差，无超标审批，优先全季",
            "current_time": "2026-10-10T10:00:00+08:00",
        },
        RunState("business"),
    )
    assert result["status"] == "completed", result
    assert len(train.calls) == 2 and len(hotel.queries) == 2 and retrieval.calls == 1
    assert result["workflow"]["results_by_query"][
        result["workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]["query_id"]
    ]["history"]
    policy_calls = [r for r in memory.session_store.read_runs() if r["type"] == "model_call"]
    assert len(policy_calls) == 1 and policy_calls[0]["stage"] == "agent:travel_policy"
    assert any(e["type"] == "policy_evidence" for e in memory.session_store.read_events())
