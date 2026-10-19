from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_policy_summary_and_structured_constraints_share_verified_source():
    from trip_assistant.agents.policy import TravelPolicyAgent

    async def search(query, limit=5):
        return [{"id": "e", "content": "住宿上限500元", "metadata": {"document_id": "policy"}}]

    async def model(messages):
        return SimpleNamespace(
            text='{"summary":"住宿每晚不超过500元。","status":"ok","constraints":[{"id":"h","rule":"每晚上限500","hard":true,"field":"hotel_max_nightly_cny","operator":"<=","value":500,"unit":"CNY/night","applicability":{},"evidence_ids":["e"]}],"applicability":{},"references":[],"effective_date":null,"missing_fields":[],"conflicts":[]}'
        )

    result = await TravelPolicyAgent(model, SimpleNamespace(search=search)).query(
        {"original_query": "安排出差"}
    )
    assert (
        result["summary"]
        and result["constraints"][0]["value"] == 500
        and result["references"][0]["id"] == "e"
    )


@pytest.mark.asyncio
async def test_policy_tool_reuses_valid_saved_policy_and_updates_shared_turn_context(tmp_path):
    from trip_assistant.tools.policy import build_policy_handler

    calls = []

    class Agent:
        async def query(self, ctx):
            calls.append(ctx)
            return {
                "status": "ok",
                "summary": "小结",
                "constraints": [],
                "applicability": {},
                "references": [],
                "missing_fields": [],
                "conflicts": [],
                "effective_date": None,
            }

    handler = build_policy_handler(Agent(), None)
    turn = {"policy_context": {}, "original_query": "出差"}
    result = await handler({"context": {}}, turn)
    assert result["data"]["summary"] == "小结" and turn["policy_context"]["summary"] == "小结"


@pytest.mark.asyncio
@pytest.mark.parametrize("employee,expected_calls", [("alice", 0), ("bob", 1)])
async def test_saved_policy_is_rechecked_when_identity_changes(employee, expected_calls):
    from trip_assistant.tools.policy import build_policy_handler

    policy = {
        "status": "ok",
        "summary": "saved",
        "constraints": [],
        "applicability": {"employee_id": "alice"},
        "references": [],
        "missing_fields": [],
        "conflicts": [],
        "effective_date": "2026-01-01",
    }
    saved = {
        "tasks": [{"destination": "上海", "conditions": {"departure_date": "2026-10-20"}}],
        "policy_context": policy,
    }
    calls = []

    class Agent:
        async def query(self, context):
            calls.append(context)
            return {**policy, "summary": "refreshed"}

    class Trips:
        def load(self, identity):
            return saved

    result = await build_policy_handler(Agent(), Trips())(
        {"trip_plan_id": "t", "context": {"employee_id": employee}}, {"policy_context": {}}
    )
    assert len(calls) == expected_calls
    assert result["data"]["summary"] == ("refreshed" if expected_calls else "saved")


@pytest.mark.asyncio
async def test_saved_policy_is_rechecked_for_dates_before_effective_date():
    from trip_assistant.tools.policy import build_policy_handler

    policy = {
        "status": "ok",
        "summary": "saved",
        "constraints": [],
        "applicability": {},
        "references": [],
        "missing_fields": [],
        "conflicts": [],
        "effective_date": "2026-11-01",
    }

    class Trips:
        def load(self, identity):
            return {
                "tasks": [{"conditions": {"departure_date": "2026-10-20"}}],
                "policy_context": policy,
            }

    class Agent:
        async def query(self, context):
            return {**policy, "status": "partial", "summary": "needs date check"}

    result = await build_policy_handler(Agent(), Trips())(
        {"trip_plan_id": "t", "context": {}}, {"policy_context": {}}
    )
    assert result["status"] == "partial" and result["data"]["summary"] == "needs date check"


@pytest.mark.asyncio
async def test_lazy_policy_adapter_uses_existing_sync_retrieval_without_answer_model():
    from trip_assistant.providers.rag import LazyPolicyRetriever

    adapter = LazyPolicyRetriever(None)

    class Backend:
        _last_search_error = None

        def search_knowledge(self, query, top_k):
            return [{"id": "p", "content": query, "metadata": {}}][:top_k]

    adapter.backend = Backend()
    assert (await adapter.search("住宿政策", limit=5))[0]["id"] == "p"
