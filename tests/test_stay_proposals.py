"""Offline regression scenarios grouped by responsibility."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.workflow import proposal as stay_origin_contract_proposal

from trip_assistant.runtime.telemetry import current_model_context
from trip_assistant.trips.conditions import create_workflow

# stay_origin_contract


def test_runtime_requires_model_stay_proposal_without_filling_nights():
    value = stay_origin_contract_proposal()
    for row in value["tasks"]:
        row["conditions"] = {}
    original = deepcopy(value)
    with pytest.raises(ValueError, match="stay proposal"):
        create_workflow(value, {}, require_stay_proposals=True)
    assert value == original


def test_runtime_preserves_different_model_stays_and_return_scope():
    value = stay_origin_contract_proposal()
    for row, nights in zip(value["tasks"], (3, 2, None), strict=False):
        row["conditions"] = {"nights": nights} if nights else {}
        row["field_sources"] = {"nights": "proposal"} if nights else {}
    result = create_workflow(value, {}, require_stay_proposals=True)
    assert [t["conditions"].get("nights") for t in result["tasks"]] == [3, 2, None]
    assert result["tasks"][0]["field_sources"]["nights"] == "proposal"


def test_program_default_is_not_an_acceptable_stay_proposal():
    value = stay_origin_contract_proposal()
    value["tasks"][0]["conditions"]["nights"] = 2
    value["tasks"][0]["field_sources"]["nights"] = "default"
    with pytest.raises(ValueError, match="stay proposal"):
        create_workflow(value, {}, require_stay_proposals=True)


def test_false_user_quote_label_does_not_create_a_requirement():
    value = stay_origin_contract_proposal()
    value["tasks"][0]["conditions"]["hotel_quote_required"] = True
    value["tasks"][0]["field_sources"]["hotel_quote_required"] = "user"
    with pytest.raises(ValueError, match="hotel quote requirement"):
        create_workflow(value, {"original_query": "从上海到北京，只安排火车和酒店推荐"})
    assert create_workflow(value, {"original_query": "从上海到北京，请核实酒店报价"})["tasks"][0][
        "conditions"
    ]["hotel_quote_required"]


@pytest.mark.parametrize(
    "query", ["查火车价格和推荐酒店", "推荐酒店，价格不重要", "酒店不用报价，只推荐地点"]
)
def test_train_price_or_declined_hotel_quote_is_not_a_hotel_requirement(query):
    value = stay_origin_contract_proposal()
    value["tasks"][0]["conditions"]["hotel_quote_required"] = True
    value["tasks"][0]["field_sources"]["hotel_quote_required"] = "user"
    with pytest.raises(ValueError, match="hotel quote requirement"):
        create_workflow(value, {"original_query": query})


# first_call_stay_proposal


def first_call_stay_proposal_proposal(case):
    route = [("重庆", "上海", "2026-10-14", 1)]
    if case == "multi":
        route += [("上海", "杭州", "2026-10-15", 3)]
    tasks = [
        dict(
            origin=a,
            destination=b,
            purpose="visit",
            requires_hotel=True,
            conditions={"departure_date": day, "nights": nights},
            field_sources={"departure_date": "user", "nights": "proposal"},
            condition_strengths={"departure_date": "hard", "nights": "soft"},
        )
        for a, b, day, nights in route
    ]
    if case == "dates":
        tasks[0]["conditions"] = {
            "departure_date": "2026-10-14",
            "check_in": "2026-10-14",
            "check_out": "2026-10-16",
        }
        tasks[0]["field_sources"] = {k: "user" for k in tasks[0]["conditions"]}
        tasks[0]["condition_strengths"] = {k: "hard" for k in tasks[0]["conditions"]}
    return_day = "2026-10-18" if case == "multi" else "2026-10-16"
    tasks.append(
        dict(
            origin=route[-1][1],
            destination="重庆",
            purpose="return",
            requires_hotel=False,
            conditions={"departure_date": return_day},
            field_sources={"departure_date": "user"},
        )
    )
    return dict(confirmed_conditions={}, tasks=tasks)


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.stages = []

    async def __call__(self, messages, **kwargs):
        self.stages.append(current_model_context()["stage"])
        return SimpleNamespace(text=json.dumps(next(self.values), ensure_ascii=False))
