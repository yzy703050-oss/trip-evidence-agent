import json
from travel_data.plan_guard import guard_itinerary


def test_guard_removes_model_facts_and_unknown_ids():
    plan = {"itinerary": {"title": "299元车票", "notes": ["今日开放"], "estimated_budget": "299元", "daily_plans": [{"day": 1, "activities": [{"time": "09:00", "location": "故宫", "description": "今日开放299元", "notes": "已预约"}]}]}, "selected_train_id": "fake"}
    guarded = guard_itinerary(plan, [])
    assert "299" not in json.dumps(guarded, ensure_ascii=False)
    assert "今日开放" not in json.dumps(guarded, ensure_ascii=False)
    assert guarded["selected_train_id"] is None
    assert guarded["budget"]["complete"] is False
    assert guarded["itinerary"]["daily_plans"][0]["activities"] == []


def test_guard_uses_real_offer_and_confirmed_passengers():
    source = {"provider": "authorized", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com"}
    offer = {"id": "t1", "train_number": "G1", "origin_station": "上海", "destination_station": "北京", "departure_time": "09:00", "arrival_time": "14:00", "seat_class": "二等座", "price_cny": "123.45", "availability": "available", "remaining": None, "source": source, "url": None}
    results = [{"agent_name": "train_search", "result": {"data": {"status": "ok", "query": {"passengers": 2}, "items": [offer]}}}, {"agent_name": "hotel_search", "result": {"data": {"status": "unavailable", "items": []}}}]
    guarded = guard_itinerary({"selected_train_id": "t1", "passengers": 9}, results)
    assert guarded["selected_train"]["price_cny"] == "123.45"
    assert guarded["budget"]["known_subtotal_cny"] == "246.90"
    assert "hotel" in guarded["budget"]["missing_categories"]


def test_guide_facts_and_hotel_are_rebuilt_with_item_sources():
    source = {"provider": "official", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com/official"}
    hotel = {"id": "h1", "hotel_name": "全季", "room_type": "标准房", "check_in": "2026-10-08", "check_out": "2026-10-10", "guests": 2, "stay_total_cny": "500.00", "fees_included": True, "availability": "available", "cancellation": "不可取消", "source": source, "url": None}
    facts = [{"kind": "opening", "content": "09:00-17:00", "verification": "verified", "source": source}, {"kind": "booking", "content": "待核实", "verification": "needs_check", "source": None}]
    rows = [{"agent_name": "hotel_search", "result": {"data": {"status": "ok", "items": [hotel]}}}, {"agent_name": "travel_guide", "result": {"data": {"status": "partial", "items": facts}}}]
    plan = guard_itinerary({"selected_hotel_id": "h1", "weather": "晴", "booking": "无需预约"}, rows)
    assert plan["selected_hotel"] == hotel
    assert plan["guide_facts"] == facts
    assert plan["budget"]["known_subtotal_cny"] == "500.00"
    assert "weather" not in plan and "booking" not in plan


def test_invalid_source_and_malformed_model_fields_are_discarded():
    rows = [{"agent_name": "train_search", "result": {"data": {"status": "ok", "items": [{"id": "bad", "price_cny": "299", "source": {"provider": "bad", "fetched_at": "2026-10-08T10:00:00"}}]}}}]
    plan = guard_itinerary({"selected_train_id": [], "itinerary": {"daily_plans": None}}, rows)
    assert plan["selected_train"] is None
    assert plan["itinerary"]["daily_plans"] == []


import pytest

@pytest.mark.asyncio
async def test_planner_applies_guard_to_actual_model_response(monkeypatch):
    import importlib.util
    from pathlib import Path
    from agentscope.message import Msg
    spec = importlib.util.spec_from_file_location("tested_planner", Path(".claude/skills/plan-trip/script/agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async def fake_extract(response):
        return json.dumps({"itinerary": {"notes": ["今日开放"], "estimated_budget": "299元", "daily_plans": [{"city": "故宫门票两百块，现有库存充足", "activities": [{"location": "故宫门票两百块，现有库存充足"}]}]}})
    async def fake_model(messages):
        return object()
    monkeypatch.setattr(module, "extract_json_from_async_response", fake_extract)
    result = await module.ItineraryPlanningAgent(model=fake_model).reply(Msg("test", content=json.dumps({"context": {}, "previous_results": []}), role="user"))
    assert "299" not in result.content and "今日开放" not in result.content
    assert "故宫门票两百块，现有库存充足" not in result.content
    assert json.loads(result.content)["budget"]["complete"] is False


@pytest.mark.parametrize("field", ["city", "location"])
def test_guard_rejects_factual_prose_in_suggestion_fields(field):
    injected = "故宫门票两百块，现有库存充足"
    day = {"day": 1, "city": injected if field == "city" else "北京", "activities": [{"location": injected if field == "location" else "故宫"}]}
    plan = guard_itinerary({"itinerary": {"daily_plans": [day]}}, [])
    assert injected not in json.dumps(plan, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "parse", "exception"])
async def test_planner_failure_survives_orchestration_cli_and_edd(failure, monkeypatch, tmp_path):
    import importlib.util
    import io
    from pathlib import Path
    from rich.console import Console
    from cli import AligoCLI
    from agents.orchestration_agent import OrchestrationAgent
    from context.session_store import SessionStore
    from evals.v0_memory.runner import evaluate_case, sourced_output_valid
    spec = importlib.util.spec_from_file_location("failure_planner", Path(".claude/skills/plan-trip/script/agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async def fake_model(messages):
        if failure == "timeout":
            raise TimeoutError("secret arbitrary error prose")
        if failure == "exception":
            raise RuntimeError("secret arbitrary error prose")
        return object()
    async def fake_extract(response):
        return "unparsable arbitrary prose"
    monkeypatch.setattr(module, "extract_json_from_async_response", fake_extract)
    planner = module.ItineraryPlanningAgent(model=fake_model)
    orchestrator = OrchestrationAgent(agent_registry={"itinerary_planning": planner})
    executed = await orchestrator._execute_agent("itinerary_planning", {}, "", "", [])
    assert executed["status"] == "error"
    assert executed["data"]["error_code"] == {"timeout": "timeout", "parse": "invalid_response", "exception": "model_error"}[failure]
    assert "arbitrary" not in json.dumps(executed)
    cli = AligoCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_results({"results": [{"agent_name": "itinerary_planning", **executed}]})
    assert "规划失败" in output.getvalue()
    assert "0天" not in output.getvalue()
    stage = {"type": "stage_complete", "turn_id": "t1", "agent_name": "itinerary_planning", "status": executed["status"], "content": executed}
    assert sourced_output_valid([stage]) is True  # Safe provenance, but unsuccessful execution.
    store = SessionStore(tmp_path, "alice", "failure")
    store.append(stage)
    store.append_run({"type": "agent_plan", "turn_id": "t1", "agents": [{"agent_name": "itinerary_planning"}]})
    store.append_run({"type": "query_run", "turn_id": "t1"})
    checks = evaluate_case({"id": "failure", "expected_agents": ["itinerary_planning"]}, store)
    assert checks["checks"]["execution_success"] is False
    assert checks["passed"] is False


def test_places_only_use_provider_references_or_fixed_generic_suggestions():
    source = {"provider": "maps", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com"}
    rows = [{"agent_name": "travel_guide", "result": {"data": {"status": "ok", "query": {"destination": "北京"}, "items": [{"kind": "place", "content": "故宫", "verification": "verified", "source": source}]}}}]
    plan = guard_itinerary({"itinerary": {"daily_plans": [{"city_ref": "guide_destination", "city": "恶意散文", "activities": [{"location_ref": "guide:0", "location": "恶意散文"}, {"location_ref": "suggestion:city_walk"}]}]}}, rows)
    day = plan["itinerary"]["daily_plans"][0]
    assert day["city"] == "北京"
    assert day["activities"][0]["location"] == "故宫"
    assert day["activities"][1]["location"] == "市内漫步（地点待核实）"
    assert guard_itinerary(plan, rows) == plan
