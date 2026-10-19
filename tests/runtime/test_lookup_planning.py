import json
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planning", "lookup"])
async def test_history_observation_routes_planning_back_to_main_and_lookup_to_summary(
    tmp_path, kind
):
    from trip_assistant.agents.prompts import MAIN_PROMPT, SUMMARY_PROMPT

    runtime = NativeRuntimeFixture(tmp_path)

    def inspect(messages):
        assert messages[0]["content"] == (MAIN_PROMPT if kind == "planning" else SUMMARY_PROMPT)
        return "未找到可定位的已有行程。"

    runtime.model.script = [
        batch(("history", "read", {"request_kind": kind, "resource": "trip_summaries"})),
        inspect,
    ]
    result = await runtime.run(
        "找到之前的上海行程，再按新要求调整" if kind == "planning" else "查已有行程"
    )
    assert len(runtime.model.messages) == 2 and result["stop_reason"] is None


@pytest.mark.asyncio
async def test_rejected_mixed_batch_keeps_planning_role(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.agents.prompts import MAIN_PROMPT
    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    scripted = ScriptedModel([])
    runtime = create_application(Settings(_env_file=None), model=scripted, storage_root=tmp_path)
    runtime.preferences.set_user_preference("home_location", "重庆")

    def inspect(messages):
        assert messages[0]["content"] == MAIN_PROMPT
        return "当前批次未执行，已保留原要求。"

    scripted.script = [
        batch(
            (
                "lookup",
                "query",
                {
                    "request_kind": "lookup",
                    "query_plan": [{"domain": "hotel", "parameters": {"city": "上海"}}],
                },
            ),
            (
                "planning",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": {
                        "operation": "propose",
                        "default_round_trip": False,
                        "tasks": [
                            {
                                "origin": "重庆",
                                "destination": "上海",
                                "conditions": {"departure_date": "2026-10-21"},
                                "items": [{"domain": "train"}],
                            }
                        ],
                    },
                },
            ),
        ),
        inspect,
    ]
    result = await runtime.run("查已有行程并调整上海行程")
    assert result["stop_reason"] is None
    assert all(r["status"] == "rejected" for r in result["tool_results"])


def batch(*calls):
    return SimpleNamespace(
        content=[
            {"type": "tool_use", "id": identity, "name": name, "input": arguments}
            for identity, name, arguments in calls
        ]
    )


@pytest.mark.asyncio
async def test_same_batch_lookup_precedes_plan_reuses_hotel_and_both_reach_review(tmp_path):
    hotel = SimulatedHotelProvider()
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": hotel, "train": train})
    proposal = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "requires_hotel": True,
                    "conditions": {"departure_date": "2026-10-17", "nights": 1},
                    "items": [
                        {"domain": "train"},
                        {"domain": "hotel", "parameters": {"keywords": "如家"}},
                    ],
                }
            ],
        },
    }
    lookup = {
        "request_kind": "planning",
        "query_plan": [{"domain": "hotel", "parameters": {"city": "上海", "keywords": "如家"}}],
    }

    def inspect(messages):
        from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        assert payload["query_results"][0]["call_id"] == "lookup"
        assert payload["query_results"][0]["data"][0]["candidates_in_tripplan"]
        assert payload["query_results"][0]["data"][0]["task_bindings"]
        assert payload["tripplan"]["candidates"]
        assert "不提供房价" not in str(payload["tripplan"]["candidates"])
        assert "hotel_price_unknown" not in str(payload["tripplan"].get("validation"))
        return "已为你整理上海的如家酒店和去程候选。"

    runtime.model.script = [
        batch(("plan", "write_trip", proposal), ("lookup", "query", lookup)),
        inspect,
    ]
    result = await runtime.run("查上海如家酒店，安排明天去上海的旅程")
    assert result["stop_reason"] is None and len(hotel.calls) == 1 and len(train.calls) == 1
    assert result["tool_results"][0]["name"] == "write_trip"
    assert len(runtime.model.messages) == 2 and not runtime.sessions.pending_tool_calls()
    rows = runtime.sessions.read_events(event_type="tool_result")
    assert rows[0]["content"]["name"] == "write_trip"


def test_quote_request_identity_still_depends_on_stay_dates(tmp_path):
    class Quotes:
        async def search(self, query):
            raise AssertionError("identity check must not query")

    runtime = NativeRuntimeFixture(tmp_path, {"hotel": Quotes()})
    first = {
        "domain": "hotel",
        "parameters": {
            "city": "上海",
            "guests": 1,
            "check_in": "2026-10-17",
            "check_out": "2026-10-18",
        },
    }
    second = {**first, "parameters": {**first["parameters"], "check_out": "2026-10-19"}}
    assert runtime.queries.request_key(first) != runtime.queries.request_key(second)


@pytest.mark.asyncio
async def test_policy_gap_still_selects_useful_candidate_and_finishes_same_call(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    proposal = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "purpose": "business",
                    "conditions": {"departure_date": "2026-10-17"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
    }

    def choose(messages):
        payload = json.loads(messages[-1]["content"])
        trip = payload["tripplan"]
        view = trip["candidates"][0]
        return batch(
            (
                "select",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "final_answer": (
                        "可以先乘模拟车次SIM001去上海。差旅政策资料缺失，这份安排尚未核实合规。"
                    ),
                    "tripplan": {
                        "operation": "select",
                        "trip_plan_id": trip["trip_plan_id"],
                        "expected_revision": trip["revision"],
                        "allow_repair": False,
                        "tasks": [
                            {
                                "task_id": trip["tasks"][0]["id"],
                                "selection_refs": {
                                    "train": {
                                        "query_id": view["query_id"],
                                        "result_revision": view["result_revision"],
                                        "candidate_id": view["items"][0]["id"],
                                    }
                                },
                            }
                        ],
                    },
                },
            )
        )

    runtime.model.script = [batch(("p", "write_trip", proposal)), choose]
    result = await runtime.run(
        "明天去上海出差，只要单程火车",
        context={
            "policy_context": {
                "status": "no_knowledge",
                "constraints": [],
                "missing_fields": ["policy"],
            }
        },
    )
    assert result["trip"]["operation"] == "select" and result["status"] == "partial"
    assert result["trip"]["validation"]["reconstructed_tasks"][0]["train"]
    assert len(runtime.model.messages) == 2 and result["finalization_method"] == "main_agent"
    assert "SIM001" in result["final_answer"] and "？" not in result["final_answer"]


@pytest.mark.asyncio
async def test_review_adjusts_content_same_selection_and_restores_scoped_history(tmp_path):
    from trip_assistant.memory.planning import review_payload
    from trip_assistant.storage.sessions import SessionStore

    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    proposal = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "conditions": {"departure_date": "2026-10-17"},
                    "items": [{"domain": "train"}],
                    "content": [
                        {
                            "date": "2026-10-17",
                            "time": "下午",
                            "purpose": "游览",
                            "description": "散步",
                            "source": "proposal",
                            "strength": "soft",
                        }
                    ],
                }
            ],
        },
    }

    def choose(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        view = trip["candidates"][0]
        content = trip["tasks"][0]["content"]
        content[0]["description"] = "沿江散步"
        return batch(
            (
                "s",
                "write_trip",
                {
                    "request_kind": "planning",
                    "round_summary": "结合实际抵达时间调整散步安排",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "final_answer": "乘模拟车次SIM001去上海，下午沿江散步。",
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": trip["trip_plan_id"],
                        "expected_revision": trip["revision"],
                        "tasks": [
                            {
                                "task_id": trip["tasks"][0]["id"],
                                "content": content,
                                "selection_refs": {
                                    "train": {
                                        "query_id": view["query_id"],
                                        "result_revision": view["result_revision"],
                                        "candidate_id": view["items"][0]["id"],
                                    }
                                },
                            }
                        ],
                    },
                },
            )
        )

    runtime.model.script = [batch(("p", "write_trip", proposal)), choose]
    result = await runtime.run("明天从重庆去上海旅游，单程")
    assert result["status"] == "completed" and len(runtime.model.messages) == 2
    saved_id = result["trip"]["trip_plan_id"]
    sessions = SessionStore(tmp_path, "a", "restored")
    payload = review_payload(sessions, {"turn_id": "new", "original_query": "查看安排"}, saved_id)
    assert payload["tripplan"]["tasks"][0]["content"][0]["description"] == "沿江散步"
    assert payload["adjustment_history"][-1]["changes"]["tasks"][0]["content"]
    other = SessionStore(tmp_path, "b", "restored")
    assert review_payload(other, {"turn_id": "new", "original_query": "查看"}, saved_id) is None
