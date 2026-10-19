import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trip_assistant.trips.conditions import create_workflow

CAPTURED_INVALID_PROPOSALS = json.loads(
    (Path(__file__).parent / "fixtures/invalid_workflow_proposals.json").read_text(encoding="utf-8")
)


def proposal_from_prompt(prompt):
    marker = "结构契约JSON："
    assert marker in prompt
    contract = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
    return contract, contract["example"]


@pytest.mark.parametrize(
    "captured",
    CAPTURED_INVALID_PROPOSALS,
    ids=lambda item: f"{item['case']}-{item['stage']}-{item['call_index']}",
)
def test_captured_invalid_proposals_are_still_rejected(captured):
    from trip_assistant.llm.structured import robust_json_parse

    value = robust_json_parse(captured["text"])
    with pytest.raises(ValueError):
        create_workflow(
            value.get("workflow_proposal", value),
            {"original_query": "酒店必须取得含税报价"},
            require_stay_proposals=True,
        )


def test_policy_top_level_string_scope_is_rejected_even_without_constraints():
    from trip_assistant.agents.policy import validate_policy_context

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
    from trip_assistant.agents.policy import TravelPolicyAgent

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
async def test_new_policy_output_rejects_string_budget_at_model_boundary():
    from trip_assistant.agents.policy import TravelPolicyAgent

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
