import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.main_agent import MainAgent
from agents.workflow_contracts import CONDITION_KEYS, PURPOSES, SOURCES, create_workflow


def proposal_from_prompt(prompt):
    marker = "结构契约JSON："
    assert marker in prompt
    contract = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
    return contract, contract["example"]


def test_initial_arrival_query_inherits_prepared_tool_name():
    from agents.model_contracts import workflow_proposal_contract
    from agents.workflow_batches import bind_query_plan

    proposal = workflow_proposal_contract()["example"]
    task = proposal["tasks"][0]
    task["conditions"]["arrival_date"] = task["conditions"]["departure_date"]
    w = create_workflow(proposal, {}, require_stay_proposals=True)
    query = {"task_index": 0, "domain": "train", "parameters": {}}
    bound = bind_query_plan([query], w, {"current_time": "2026-10-10T09:00:00+08:00"})
    request = bound["requests"][0]
    assert request["name"] == "train_search_by_arrival"
    assert request["parameters"]["arrival_date"] == task["conditions"]["arrival_date"]


@pytest.mark.asyncio
async def test_initialize_contract_example_is_valid_without_schema_repair():
    from agents.model_contracts import workflow_proposal_contract

    calls = []

    async def model(messages, **kwargs):
        prompt = messages[0]["content"]
        calls.append(prompt)
        contract = workflow_proposal_contract()
        proposal = contract["example"]
        schema = contract["schema"]
        conditions = schema["$defs"]["conditions"]["properties"]
        assert set(conditions) == CONDITION_KEYS
        assert conditions["hotel_keywords"]["type"] == ["string", "null"]
        task = schema["properties"]["tasks"]["items"]["properties"]
        assert set(task["purpose"]["enum"]) == PURPOSES
        assert set(task["purpose_source"]["enum"]) == SOURCES
        assert "结构契约JSON：" not in prompt
        assert "可选query_plan每项task_index绑定任务" not in prompt
        return SimpleNamespace(
            text=json.dumps(
                {
                    "response_mode": "workflow",
                    "intents": [{"type": "plan"}],
                    "agent_schedule": [],
                    "workflow_proposal": proposal,
                }
            )
        )

    result = await MainAgent(model).initialize({"original_query": "重庆上海旅游往返，单程700元"})
    w = create_workflow(result["workflow_proposal"], {}, require_stay_proposals=True)
    assert len(calls) == 1
    assert w["confirmed_conditions"]["constraints"]["train_max_total_cny"] == 700
    assert "total_budget_cny" not in w["confirmed_conditions"]["constraints"]
    assert [t["purpose"] for t in w["tasks"]] == ["visit", "return"]


@pytest.mark.asyncio
async def test_repair_uses_same_contract_without_mutating_original_bad_proposal():
    from copy import deepcopy

    calls = []

    async def model(messages, **kwargs):
        contract, proposal = proposal_from_prompt(messages[0]["content"])
        calls.append(contract)
        return SimpleNamespace(text=json.dumps(proposal))

    bad = {"confirmed_conditions": {"seat_class": "二等座"}, "tasks": []}
    before = deepcopy(bad)
    repaired = await MainAgent(model).repair_proposal(
        {"original_query": "重庆上海往返，每单程700元"}, bad, "unsupported workflow conditions"
    )
    create_workflow(repaired, {}, require_stay_proposals=True)
    assert bad == before
    assert len(calls) == 1
    assert repaired["confirmed_conditions"]["constraints"] == {
        "seat_class": "二等座",
        "train_max_total_cny": 700,
    }


@pytest.mark.parametrize("case", ["tourism_live", "business_live", "business_conflict"])
def test_captured_invalid_proposals_are_still_rejected(case):
    path = Path(__file__).parent / "fixtures/invalid_workflow_proposals.json"
    calls = json.loads(path.read_text(encoding="utf-8"))["cases"][case]
    assert [call["stage"] for call in calls] == ["main:plan", "main:repair_proposal"]
    for call in calls:
        with pytest.raises(ValueError, match=call["expected_error"]):
            create_workflow(
                call["proposal"],
                {"original_query": "酒店必须取得含税报价"},
                require_stay_proposals=True,
            )


def test_policy_top_level_string_scope_is_rejected_even_without_constraints():
    from agents.policy_preflight import validate_policy_context

    value = {
        "status": "ok",
        "constraints": [],
        "applicability": "上海普通员工",
        "references": [],
        "effective_date": None,
        "missing_fields": [],
        "conflicts": [],
    }
    with pytest.raises(ValueError, match="applicability must be an object"):
        validate_policy_context(value, {}, {"references": []})


@pytest.mark.asyncio
async def test_policy_contract_has_object_scope_string_seat_and_no_quote_gaps():
    from agents.policy_preflight import TravelPolicyAgent

    calls = []

    class Retrieval:
        async def search(self, query, limit):
            return [{"id": "current", "content": "二等座、上海500元每晚；无审批不适用800元例外"}]

    async def model(messages):
        prompt = messages[0]["content"]
        marker = "政策契约JSON："
        assert marker in prompt
        contract = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
        value = contract["example"]
        calls.append(value)
        assert isinstance(value["applicability"], dict)
        assert all(isinstance(c["applicability"], dict) for c in value["constraints"])
        seat = next(c for c in value["constraints"] if c["field"] == "seat_class")
        assert seat["value"] == "二等座" and seat["unit"] is None
        assert "报价缺口不属于政策missing_fields" in prompt
        assert "软偏好不得输出为hard约束" in prompt
        assert value["missing_fields"] == []
        return SimpleNamespace(text=json.dumps(value))

    result = await TravelPolicyAgent(model, Retrieval()).query(
        {"original_query": "上海出差", "city": "上海"}
    )
    assert len(calls) == 1 and result["status"] == "ok"
    assert result["references"][0]["id"] == "current"


@pytest.mark.asyncio
async def test_runner_preserves_policy_validation_error_in_audit(tmp_path):
    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager

    class Retrieval:
        async def search(self, query, limit):
            return [{"id": "current", "content": "二等座"}]

    async def model(messages):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "status": "ok",
                    "constraints": [],
                    "applicability": "上海",
                    "references": [],
                    "effective_date": None,
                    "missing_fields": [],
                    "conflicts": [],
                }
            )
        )

    class Main:
        async def initialize(self, context):
            assert context["policy_context"]["status"] == "error"
            return {
                "response_mode": "direct",
                "agent_schedule": [],
                "final_answer": "政策未核实，先确认。",
            }

    main = Main()
    main.model = model
    memory = MemoryManager("eval", "s", str(tmp_path))
    result = await ExecutionHarness(main, {"rag_knowledge": Retrieval()}, memory).run_turn(
        {"original_query": "上海出差"}, RunState("t")
    )
    assert result["status"] != "completed"
    events = [
        e for e in memory.session_store.read_events() if e["type"] == "policy_validation_error"
    ]
    assert len(events) == 1
    assert events[0]["content"]["error_type"] == "ValueError"
    assert "applicability must be an object" in events[0]["content"]["reason"]


@pytest.mark.asyncio
async def test_explicit_refresh_uses_actual_tool_contract_without_train_replay(tmp_path):
    from dataclasses import replace

    from test_consolidated_travel_run import BatchMain
    from test_information_tools import Provider
    from workflow_runtime import OfflineProvider

    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor

    class Hotel(Provider):
        async def search(self, query):
            result = await super().search(query)
            price = "2100" if len(self.queries) == 1 else "900"
            return replace(result, items=[{**i, "stay_total_cny": price} for i in result.items])

    train, hotel = OfflineProvider("train"), Hotel()
    executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    calls = []

    async def model(messages, **kwargs):
        assert "tools" not in kwargs
        prompt = messages[0]["content"]
        view = json.loads(prompt.split("上下文：", 1)[1])
        calls.append(view)
        if len(calls) == 1:
            contracts = {c["name"]: c for c in view["query_tools"]}
            schema = contracts["hotel_search"]["parameters"]
            actual = next(
                s["function"] for s in executor.schemas()
                if s["function"]["name"] == "hotel_search"
            )
            assert schema == actual["parameters"]
            assert "quote_required" not in schema["properties"]
            assert "refresh" in schema["properties"]
            assert "查询酒店报价" in contracts["hotel_search"]["description"]
            hotel_row = next(r for r in view["task_results"] if r["domain"] == "hotel")
            assert hotel_row["returned_candidate_total"] == 7
            assert hotel_row["excluded_candidate_count"] == 7 and hotel_row["items"] == []
            # Inject an explicitly requested action to test runtime/schema compatibility,
            # not model obedience to a prompt or an automatic over-budget retry.
            example = {"action": "schedule_queries", "query_plan": [{
                "domain": "hotel", "parameters": {"refresh": True}}]}
            task = view["workflow"]["tasks"][0]
            example["query_plan"][0].update(task_id=task["id"], task_revision=task["revision"])
            example["query_plan"][0]["parameters"].update(
                city=task["destination"], check_in="2026-10-11", check_out="2026-10-14", guests=1
            )
            example["query_plan"][0]["parameters"]["constraints"] = {"hotel_max_nightly_cny": 500}
            return SimpleNamespace(text=json.dumps(example))
        assert view['query_scope'] == 'cached_pages_only'
        assert any(c["name"] == "hotel_search" for c in view["query_tools"])
        action = await BatchMain().select_batch(view)
        return SimpleNamespace(text=json.dumps(action))

    class Main(MainAgent):
        async def initialize(self, context):
            value = await BatchMain().initialize(context)
            value["workflow_proposal"]["confirmed_conditions"] = {
                "constraints": {"hotel_max_nightly_cny": "500"}
            }
            return value

    memory = MemoryManager("a", "refresh-contract", str(tmp_path))
    harness = ExecutionHarness(Main(model), {}, memory)
    harness.runner.executor = executor
    result = await harness.run_turn(
        {"original_query": "酒店超标就只重查酒店", "current_time": "2026-10-10T10:00:00+08:00"},
        RunState("refresh-contract"),
    )
    assert result["status"] == "completed", result
    assert len(calls) == 2 and len(train.calls) == 2 and len(hotel.queries) == 2
    task = result["workflow"]["tasks"][0]
    ref = task["draft_plan"]["hotel_selection"]
    row = result["workflow"]["results_by_query"][ref["query_id"]]
    assert ref["result_revision"] == 2 and row["history"]


@pytest.mark.asyncio
async def test_explicit_no_repair_preserves_partial_drafts_with_two_main_calls(tmp_path):
    from test_consolidated_travel_run import BatchMain
    from workflow_runtime import OfflineProvider

    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor

    class Main(BatchMain):
        async def initialize(self, context):
            value = await super().initialize(context)
            value["workflow_proposal"]["confirmed_conditions"] = {"hotel_quote_required": True}
            return value

        async def select_batch(self, context):
            if self.calls >= 2:
                pytest.fail("unrepairable price gap must not call selection model again")
            tool = next(c for c in context["query_tools"] if c["name"] == "hotel_search")
            assert "不提供房型、房价、空房" in tool["description"]
            action = await super().select_batch(context)
            action["allow_repair"] = False
            for row in action["draft_tasks"]:
                row["draft_plan"]["unverified_requirements"] = ["酒店接口无报价能力"]
            return action

    main = Main()
    memory = MemoryManager("a", "partial-contract", str(tmp_path))
    harness = ExecutionHarness(main, {}, memory)
    harness.runner.executor = ToolExecutor(
        {"train_search": OfflineProvider("train"), "hotel_search": OfflineProvider("hotel")}
    )
    result = await harness.run_turn(
        {"original_query": "酒店必须报价", "current_time": "2026-10-10T10:00:00+08:00"},
        RunState("partial-contract"),
    )
    assert main.calls == 2
    assert result["status"] == "partial" and result["stop_reason"] == "model_partial"
    w = result["workflow"]
    assert w["status"] == "running" and not w["validation"]["valid"]
    assert all(t["draft_plan"]["train_selection"] for t in w["tasks"])
    assert memory.workflow_store.load(w["id"]) == w


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_time", [False, True])
async def test_place_only_capability_stops_without_model_stop_flag(tmp_path, unknown_time):
    from test_consolidated_travel_run import BatchMain
    from workflow_runtime import OfflineProvider

    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor

    class Main(BatchMain):
        async def initialize(self, context):
            value = await super().initialize(context)
            value["workflow_proposal"]["confirmed_conditions"] = {"hotel_quote_required": True}
            return value

        async def select_batch(self, context):
            if self.calls >= 2 and not unknown_time:
                pytest.fail("known quote capability gap cannot be repaired by another model call")
            action = await super().select_batch(context)
            if unknown_time and self.calls == 3:
                action["allow_repair"] = False
            return action

    main, train, hotel = Main(), OfflineProvider("train"), OfflineProvider("hotel")
    train.unknown_time = unknown_time
    memory = MemoryManager("a", "capability-stop", str(tmp_path))
    harness = ExecutionHarness(main, {}, memory)
    harness.runner.executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    result = await harness.run_turn(
        {"original_query": "酒店必须报价", "current_time": "2026-10-10T10:00:00+08:00"},
        RunState("capability-stop"),
    )
    assert result["status"] == "partial"
    expected_stop = "model_partial" if unknown_time else "provider_capability_missing"
    assert result["stop_reason"] == expected_stop
    assert main.calls == (3 if unknown_time else 2)
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    w = result["workflow"]
    assert w["status"] == "running" and not w["validation"]["valid"]
    if not unknown_time:
        assert all(t["draft_plan"]["train_selection"] for t in w["tasks"])
    assert memory.workflow_store.load(w["id"]) == w


@pytest.mark.asyncio
async def test_captured_bad_hotel_parameters_remain_rejected_before_provider():
    from test_information_tools import Provider

    from agents.contracts import RunState
    from travel_data.tools import ToolExecutor

    provider = Provider()
    result = await ToolExecutor({"hotel_search": provider}).execute(
        "hotel_search",
        {
            "city": "上海",
            "check_in": "2026-10-15",
            "check_out": "2026-10-17",
            "guests": 1,
            "hotel_keywords": "全季",
            "quote_required": True,
            "max_nightly_cny": 500,
        },
        RunState("bad-contract"),
        call_id="bad-hotel",
    )
    assert result["status"] == "error" and provider.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount,allowed",
    [
        (500, True),
        (500.0, True),
        ("500.00", True),
        (501, False),
        ("NaN", False),
        ("Infinity", False),
    ],
)
async def test_tool_compares_budget_semantics_without_relaxing_amount(amount, allowed):
    from test_information_tools import Provider

    from agents.contracts import RunState
    from travel_data.tools import ToolExecutor

    provider = Provider()
    run = RunState("amount-contract")
    run.travel_conditions = {"constraints": {"hotel_max_nightly_cny": "500"}}
    result = await ToolExecutor({"hotel_search": provider}).execute(
        "hotel_search",
        {
            "city": "上海",
            "check_in": "2026-10-15",
            "check_out": "2026-10-17",
            "guests": 1,
            "constraints": {"hotel_max_nightly_cny": amount},
        },
        run,
        call_id="amount-query",
    )
    assert bool(provider.queries) == allowed
    assert (result["status"] == "ok") == allowed
    assert run.travel_conditions["constraints"]["hotel_max_nightly_cny"] == "500"


@pytest.mark.parametrize("amount", ["500", True, float("nan"), float("inf")])
def test_new_main_output_requires_numeric_finite_money(amount):
    from agents.model_contracts import workflow_proposal_contract

    proposal = workflow_proposal_contract()["example"]
    proposal["confirmed_conditions"]["constraints"]["train_max_total_cny"] = amount
    value = {
        "response_mode": "workflow",
        "intents": [{"type": "plan"}],
        "agent_schedule": [],
        "workflow_proposal": proposal,
    }
    with pytest.raises(ValueError, match="model amount must be a finite nonnegative JSON number"):
        MainAgent._initial_decision(json.dumps(value))


@pytest.mark.asyncio
async def test_new_batch_output_rejects_string_budget():
    async def model(messages, **kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "action": "schedule_queries",
                    "query_plan": [
                        {
                            "domain": "hotel",
                            "parameters": {"constraints": {"hotel_max_nightly_cny": "500"}},
                        }
                    ],
                }
            )
        )

    with pytest.raises(ValueError, match="model amount must be a finite nonnegative JSON number"):
        await MainAgent(model).select_batch({"workflow": {"tasks": []}})


@pytest.mark.asyncio
async def test_new_policy_output_rejects_string_budget_at_model_boundary():
    from agents.policy_preflight import TravelPolicyAgent

    class Retrieval:
        async def search(self, query, limit):
            return [{"id": "policy", "content": "上海每晚500元"}]

    async def model(messages):
        contract = json.loads(messages[0]["content"].split("政策契约JSON：", 1)[1].splitlines()[0])
        value = contract["example"]
        value["constraints"][1]["value"] = "500"
        return SimpleNamespace(text=json.dumps(value))

    with pytest.raises(ValueError, match="model amount must be a finite nonnegative JSON number"):
        await TravelPolicyAgent(model, Retrieval()).query({"city": "上海"})


@pytest.mark.asyncio
async def test_main_commit_only_contract_omits_query_actions_and_examples():
    async def model(messages, **kwargs):
        prompt = messages[0]["content"].split("上下文：", 1)[0]
        assert "只允许commit_batch" in prompt
        assert "批量查询契约JSON" not in prompt and "schedule_queries" not in prompt
        return SimpleNamespace(text='{"action":"commit_batch","draft_tasks":[]}')

    await MainAgent(model).select_batch(
        {"workflow": {"tasks": []}, "allowed_actions": ["commit_batch"]}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action_name", ["schedule_queries", "repair_tasks", "ask_user"])
async def test_runner_rejects_unnecessary_refresh_without_supplier_call(tmp_path, action_name):
    from test_consolidated_travel_run import BatchMain
    from workflow_runtime import OfflineProvider

    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor

    class Main(BatchMain):
        async def select_batch(self, context):
            assert context["allowed_actions"] == ["commit_batch", "schedule_queries"]
            assert context['query_scope'] == 'cached_pages_only'
            if self.calls == 1:
                self.calls += 1
                task = context["workflow"]["tasks"][0]
                return {
                    "action": action_name,
                    "query_plan": [
                        {
                            "task_id": task["id"],
                            "task_revision": task["revision"],
                            "domain": "hotel",
                            "parameters": {"city": "上海", "refresh": True},
                        }
                    ],
                }
            assert context["action_feedback"]["code"] == (
                'invalid_query_plan' if action_name == 'schedule_queries' else 'cached_pages_only'
            )
            return await super().select_batch(context)

    train, hotel = OfflineProvider("train"), OfflineProvider("hotel")
    memory = MemoryManager("a", "action-contract", str(tmp_path))
    harness = ExecutionHarness(Main(), {}, memory)
    harness.runner.executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    result = await harness.run_turn(
        {"original_query": "规划往返", "current_time": "2026-10-10T10:00:00+08:00"},
        RunState("action-contract"),
    )
    assert result["status"] == "completed"
    assert len(train.calls) == 2 and len(hotel.calls) == 1
