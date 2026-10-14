"""The model receives usable contracts only for its allowed decision scope."""

from copy import deepcopy

import pytest

from travel_data.tools import ToolExecutor


def context(actions):
    return {
        "allowed_actions": actions,
        "workflow": {"tasks": [dict(id="outbound", requires_hotel=True, conditions={})]},
        "task_results": [
            {"task_id": "outbound", "domain": "train", "items": [{"id": "train1"}]},
            {"task_id": "outbound", "domain": "hotel", "items": [{"id": "place1"}]},
        ],
        "query_tools": [s["function"] for s in ToolExecutor({}).schemas()],
    }


def project(value):
    from agents.context_projection import project_batch_decision_context

    return project_batch_decision_context(value)


def test_commit_only_keeps_candidates_and_capabilities_without_query_parameters():
    value = context(["commit_batch"])
    before = deepcopy(value)
    result = project(value)
    assert result.get("query_tools", []) == []
    assert {c["name"] for c in result["query_capabilities"]} == {"train_search", "hotel_search"}
    assert all("parameters" not in c for c in result["query_capabilities"])
    assert result["task_results"] == before["task_results"]
    assert result["allowed_actions"] == ["commit_batch"]
    assert value == before


def test_open_query_scope_preserves_actual_parameters_and_cached_pagination():
    value = context(["commit_batch", "schedule_queries", "repair_tasks", "ask_user"])
    result = project(value)
    tools = {c["name"]: c for c in result["query_tools"]}
    assert set(tools) == {"train_search", "hotel_search"}
    for name in tools:
        actual = next(c for c in value["query_tools"] if c["name"] == name)
        assert tools[name] == actual
        assert tools[name]["parameters"]["properties"]["candidate_offset"]["type"] == "integer"
        assert "refresh" in tools[name]["parameters"]["properties"]
    assert result["allowed_actions"] == value["allowed_actions"]


@pytest.mark.parametrize("case", ["arrival_date", "arrival_before", "weather"])
def test_specialized_tools_remain_available_when_related(case):
    value = context(["commit_batch", "schedule_queries"])
    if case == "weather":
        value["task_results"].append({"domain": "weather", "items": []})
        expected = "weather_query"
    else:
        value["workflow"]["tasks"][0]["conditions"][case] = "2026-10-14"
        expected = "train_search_by_arrival"
    assert expected in {c["name"] for c in project(value)["query_tools"]}


def test_repair_only_keeps_actions_without_unavailable_query_schemas():
    result = project(context(["commit_batch", "repair_tasks"]))
    assert result["allowed_actions"] == ["commit_batch", "repair_tasks"]
    assert result.get("query_tools", []) == []
