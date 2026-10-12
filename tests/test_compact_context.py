import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.context_projection import (
    project_domain_result,
    project_model_context,
    project_delegation_context,
)
from agents.main_agent import MainAgent
from agents.contracts import RunState
from agents.workflow_runner import WorkflowRunner
from travel_data.candidates import CandidateStore
from workflow_support import populated_workflow


def test_train_view_keeps_selection_facts_without_source_payload_or_duplicate_clock():
    raw = {
        "id": "q1",
        "domain": "train",
        "result_revision": 2,
        "status": "ok",
        "source": {
            "provider": "simulation:train",
            "url": "https://PRIVATE_URL",
            "fetched_at": "now",
        },
        "items": [
            {
                "id": "c1",
                "train_number": "G1",
                "price_cny": "80",
                "seat_class": "二等座",
                "departure_at": "2026-10-20T08:00:00+08:00",
                "departure_time": "08:00",
                "arrival_at": None,
                "arrival_time": "12:00",
                "availability": "available",
                "url": "https://PRIVATE_URL",
                "source": {"provider": "simulation:train", "url": "https://PRIVATE_URL"},
                "time_evidence": {"raw": "PRIVATE_EVIDENCE"},
                "remaining": 10,
            }
        ],
    }
    original = deepcopy(raw)
    view = project_domain_result(raw)
    assert "PRIVATE_" not in json.dumps(view)
    assert view["source"]["provider"] == "simulation:train"
    assert view["items"][0]["id"] == "c1" and view["items"][0]["price_cny"] == "80"
    assert view["items"][0]["arrival_at"] is None and view["items"][0]["arrival_time"] == "12:00"
    assert "departure_time" not in view["items"][0]
    assert "source" not in view["items"][0]  # Shared source is already at query level.
    assert raw == original


def test_hotel_place_does_not_expose_coordinates_or_imply_room_offer():
    row = {
        "domain": "hotel",
        "status": "partial",
        "missing_fields": ["price"],
        "items": [
            {
                "id": "p1",
                "kind": "hotel_place",
                "hotel_name": "酒店",
                "address": "车站附近",
                "location": "116.1,39.1",
                "rating": "4.8",
                "room_type": None,
                "stay_total_cny": None,
                "availability": "unknown",
                "url": "https://PRIVATE_URL",
            }
        ],
    }
    view = project_domain_result(row)
    item = view["items"][0]
    assert item["kind"] == "hotel_place" and item["availability"] == "unknown"
    assert view["missing_fields"] == ["price"]
    assert "location" not in item and "url" not in item and "rating" not in item
    assert project_domain_result(row, requirements={"rating"})["items"][0]["rating"] == "4.8"


@pytest.mark.asyncio
async def test_actual_main_request_has_no_other_drafts_or_candidate_urls():
    inputs = []

    async def model(messages, **kwargs):
        inputs.append(messages)
        return SimpleNamespace(text='{"action":"finish","status":"partial","final_answer":"ok"}')

    w = populated_workflow()
    w["tasks"][1]["draft_plan"]["unverified_requirements"] = ["PRIVATE_OTHER_DRAFT"]
    row = next(iter(w["results_by_query"].values()))
    context = {"workflow": w, "current_task": w["tasks"][0], "relevant_results": [row]}
    await MainAgent(model).step(context)
    sent = json.dumps(inputs)
    assert "PRIVATE_OTHER_DRAFT" not in sent and "https://example.com" not in sent
    assert row["items"][0]["id"] in sent
    assert "PRIVATE_OTHER_DRAFT" in json.dumps(w)


def test_workflow_child_gets_only_current_task_and_necessary_boundaries():
    w = populated_workflow()
    context = {
        "original_query": "安排旅程",
        "workflow": w,
        "current_task": w["tasks"][0],
        "workflow_overview": w["tasks"],
        "recent_messages": [{"content": "PRIVATE_HISTORY"}],
        "session_summary": "PRIVATE_HISTORY",
        "previous_boundary": {"check_out": "2026-10-22"},
        "effective_conditions": {"passengers": 1},
    }
    view = project_delegation_context(context)
    assert "workflow" not in view and "workflow_overview" not in view
    assert "PRIVATE_HISTORY" not in json.dumps(view)
    assert view["current_task"]["id"] == w["tasks"][0]["id"]
    assert view["previous_boundary"]["check_out"] == "2026-10-22"


def test_runner_exposes_current_candidates_once_and_small_validation():
    w = populated_workflow()
    runner = WorkflowRunner(None, None, None)
    runner.w = w
    runner.context = {"original_query": "安排旅程"}
    runner.run_state = RunState("t", workflow_tool_budget=10, candidates=CandidateStore())
    runner.latest = {
        "summary": "已查",
        "query_results": list(w["results_by_query"].values()),
        "domain_results": {"train": next(iter(w["results_by_query"].values()))},
    }
    runner.main_calls = 0
    runner.counts = {}
    view = runner._model_context()
    for task in view["workflow"]["tasks"]:
        assert "draft_plan" not in task
    assert "items" not in json.dumps(view["latest_child_result"])
    assert view["relevant_results"][0]["items"][0]["id"]


@pytest.mark.asyncio
async def test_child_invocation_cannot_receive_previous_tasks_domain_data(tmp_path):
    from workflow_runtime import Runtime, InfoModel

    runtime = Runtime(tmp_path)
    original = runtime.info_model.__call__

    class Inspect(InfoModel):
        async def __call__(self, messages, **kwargs):
            if len(messages) == 2:
                context = json.loads(messages[1]["content"])
                assert not context["domain_results"]
                assert not context["travel_conditions"]
            return await original(messages, **kwargs)

    runtime.info.model.model = Inspect()
    assert (await runtime.turn())["status"] == "completed"


def test_current_candidate_view_does_not_repeat_date_options_or_conditions():
    w = populated_workflow()
    context = {
        "workflow": w,
        "current_task": w["tasks"][0],
        "effective_conditions": {"passengers": 1},
        "preflight": {
            "effective_conditions": {"passengers": 1},
            "field_sources": {"passengers": "default"},
            "readiness": "ready",
            "missing_fields": [],
            "query_requests": [],
        },
        "relevant_results": [next(iter(w["results_by_query"].values()))],
        "latest_child_result": {
            "date_options": [{"candidate_id": "duplicate"}],
            "completed_conditions": {"passengers": 1},
        },
    }
    view = project_model_context(context)
    assert "date_options" not in view["latest_child_result"]
    assert "effective_conditions" not in view["preflight"]
    assert "query_ids" not in view["current_task"]
