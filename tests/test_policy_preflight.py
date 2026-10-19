import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_direct_policy_extraction_once_and_no_generic_answer():
    from trip_assistant.agents.policy import TravelPolicyAgent

    calls = []

    class Retrieval:
        async def search(self, text, limit):
            return [
                {"id": "doc-1", "content": "上海住宿限额500元/晚", "metadata": {"city": "上海"}}
            ]

    async def model(messages):
        calls.append(messages)
        return SimpleNamespace(
            text=json.dumps(
                {
                    "status": "ok",
                    "constraints": [
                        {
                            "id": "hotel-limit",
                            "rule": "上海500元每晚",
                            "applicability": {"city": "上海"},
                            "hard": True,
                            "field": "hotel_max_nightly_cny",
                            "operator": "<=",
                            "value": 500,
                            "unit": "CNY/night",
                            "evidence_ids": ["doc-1"],
                        }
                    ],
                    "applicability": {"city": "上海"},
                    "references": [{"id": "doc-1"}],
                    "effective_date": None,
                    "missing_fields": [],
                    "conflicts": [],
                },
                ensure_ascii=False,
            )
        )

    result = await TravelPolicyAgent(model, Retrieval()).query(
        {"original_query": "去上海出差", "city": "上海"}
    )
    assert len(calls) == 1
    assert result["constraints"][0]["value"] == 500
    assert result["references"][0]["id"] == "doc-1"
    assert "content" not in result["references"][0]


def test_policy_rejects_fake_source_unit_and_scope():
    from trip_assistant.agents.policy import validate_policy_context

    evidence = {"references": [{"id": "real"}]}
    base = {
        "status": "ok",
        "constraints": [
            {
                "id": "x",
                "rule": "限额",
                "hard": True,
                "field": "hotel_max_nightly_cny",
                "operator": "<=",
                "value": 500,
                "unit": "CNY/night",
                "evidence_ids": ["fake"],
                "applicability": {},
            }
        ],
        "applicability": {},
        "references": [],
        "effective_date": None,
        "missing_fields": [],
        "conflicts": [],
    }
    with pytest.raises(ValueError):
        validate_policy_context(base, {}, evidence)
    base["constraints"][0]["evidence_ids"] = ["real"]
    base["constraints"][0]["unit"] = "USD"
    with pytest.raises(ValueError):
        validate_policy_context(base, {}, evidence)
    base["constraints"][0]["unit"] = "CNY/night"
    base["constraints"][0]["applicability"] = {"employee": "another"}
    result = validate_policy_context(base, {"employee": "alice"}, evidence)
    assert result["missing_fields"] and result["status"] != "ok"


@pytest.mark.asyncio
async def test_no_policy_evidence_has_no_model_and_complex_rule_is_gap():
    from trip_assistant.agents.policy import TravelPolicyAgent, validate_policy_context

    class Empty:
        async def search(self, text, limit):
            return []

    async def never(messages):
        pytest.fail("no evidence must not infer a policy")

    result = await TravelPolicyAgent(never, Empty()).query({"original_query": "出差"})
    assert result["status"] == "no_knowledge" and result["missing_fields"]
    result = validate_policy_context(
        {
            "status": "ok",
            "constraints": [
                {
                    "id": "x",
                    "rule": "需主管审批",
                    "hard": True,
                    "evidence_ids": ["real"],
                    "applicability": {},
                }
            ],
            "applicability": {},
            "references": [],
            "effective_date": None,
            "missing_fields": [],
            "conflicts": [],
        },
        {},
        {"references": [{"id": "real"}]},
    )
    assert result["missing_fields"] == ["policy_rule:x"]


def test_policy_scope_and_effective_date_resolve_from_scheduled_trip_without_model():
    from support.workflow import populated_workflow

    from trip_assistant.agents.policy import resolve_policy_for_workflow

    w = populated_workflow()
    policy = {
        "status": "partial",
        "constraints": [{"id": "x", "hard": True, "applicability": {"city": "杭州"}}],
        "applicability": {},
        "references": [{"id": "real"}],
        "effective_date": "2026-10-01",
        "missing_fields": ["policy_applicability:city", "policy_effective_date"],
        "conflicts": [],
    }
    result = resolve_policy_for_workflow(policy, w, {"current_time": "2026-09-01T10:00:00+08:00"})
    assert result["status"] == "ok" and result["missing_fields"] == []
    policy["missing_fields"].append("approval_id")
    assert resolve_policy_for_workflow(policy, w, {})["missing_fields"] == ["approval_id"]
