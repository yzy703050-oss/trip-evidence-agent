from copy import deepcopy
from datetime import UTC

import pytest
from support.native import write_then_query
from support.workflow import populated_workflow

from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.runtime.scheduler import Scheduler
from trip_assistant.settings import Settings
from trip_assistant.storage.trips import TripStore, WorkflowConflictError


@pytest.fixture
def service(tmp_path):
    from trip_assistant.trips.service import TripService

    store = TripStore(str(tmp_path), "a")
    return TripService(store, None, Scheduler(Settings()))


def committed(service):
    from trip_assistant.trips.validation import check_workflow, finalize_workflow

    w = populated_workflow(quoted=True)
    w["user_id"] = "a"
    w["tasks"][0]["condition_strengths"] = {"check_out": "soft"}
    return service.store.commit(
        finalize_workflow(w, check_workflow(w)), expected_revision=None, operation_id="seed"
    )


def turn():
    return {
        "original_query": "只换上海酒店",
        "session_id": "s",
        "operation_id": "op",
        "shown_candidates": {},
        "policy_context": {},
        "budget": TurnBudget(Settings()),
    }


@pytest.mark.asyncio
async def test_equivalent_amendment_records_receipt_without_new_trip_revision(service):
    original = committed(service)
    plan = service.normalize_input(
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {"task_id": original["tasks"][0]["id"], "origin": original["tasks"][0]["origin"]}
            ],
        }
    )
    result = await service.apply(plan, turn())
    assert result["revision"] == original["revision"]
    assert service.store.load(original["id"]) == original
    assert service.store.lookup_operation("op")["revision"] == original["revision"]


@pytest.mark.parametrize("decision", ["task", "global", "origin", "route", "confirm", "resume"])
def test_native_decision_does_not_require_authorization_keywords(service, decision):
    original = committed(service)
    if decision == "resume":
        original["stop_reason"] = "paused"
        original = service.store.save_working(
            original, expected_revision=original["revision"], operation_id="paused-seed"
        )
    args = {
        "operation": "confirm" if decision == "confirm" else "amend",
        "trip_plan_id": original["id"],
        "expected_revision": original["revision"],
    }
    if decision == "task":
        args["tasks"] = [
            {"task_id": original["tasks"][0]["id"], "conditions": {"departure_date": "2026-10-20"}}
        ]
    elif decision == "global":
        args["confirmed_conditions"] = {"start_date": "2026-10-20"}
    elif decision == "origin":
        args["tasks"] = [{"task_id": original["tasks"][0]["id"], "origin": "重庆"}]
    elif decision == "route":
        args["route_tasks"] = [
            {
                "origin": "上海",
                "destination": "苏州",
                "conditions": {"departure_date": "2026-10-20"},
            }
        ]
    ctx = {**turn(), "original_query": "按我刚才的意思办"}
    prepared = service.preview(args, ctx)
    assert prepared["arguments"]["operation"] == args["operation"]
    assert prepared["working"].get("stop_reason") != "paused"
    assert service.store.load(original["id"])["revision"] == original["revision"]


@pytest.mark.asyncio
async def test_failed_amendment_keeps_original_and_working_progress(service):
    original = committed(service)
    t = original["tasks"][0]
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {
                    "task_id": t["id"],
                    "components": ["hotel"],
                    "conditions": {"check_out": "2026-10-18"},
                }
            ],
        },
        turn(),
    )
    saved = service.store.load(original["id"])
    assert saved["committed"] == original["committed"] and saved["working"]
    assert result["status"] != "completed" and result["validation"]["issues"]


@pytest.mark.asyncio
async def test_hard_date_change_keeps_original_until_new_plan_is_valid(service):
    original = committed(service)
    t = original["tasks"][0]
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {
                    "task_id": t["id"],
                    "components": ["train"],
                    "conditions": {"departure_date": "2026-10-20"},
                    "field_sources": {"departure_date": "user"},
                }
            ],
        },
        turn(),
    )
    saved = service.store.load(original["id"])
    assert result["status"] != "completed"
    assert saved["committed"] == original["committed"]
    assert saved["tasks"][0]["conditions"]["departure_date"] == "2026-10-20"


@pytest.mark.asyncio
async def test_stale_trip_revision_has_no_mutation(service):
    original = committed(service)
    with pytest.raises(WorkflowConflictError):
        await write_then_query(
            service,
            {
                "operation": "pause",
                "trip_plan_id": original["id"],
                "expected_revision": 0,
                "reason": "等待用户",
            },
            turn(),
        )
    assert service.store.load(original["id"])["revision"] == original["revision"]


@pytest.mark.asyncio
async def test_pause_is_idempotent_and_explained(service):
    original = committed(service)
    args = {
        "operation": "pause",
        "trip_plan_id": original["id"],
        "expected_revision": original["revision"],
        "reason": "等待用户确认预算",
    }
    first = await write_then_query(service, args, turn())
    second = await write_then_query(service, args, turn())
    assert first == second and first["reason"] == "等待用户确认预算"
    assert service.store.load(original["id"])["stop_reason"] == "paused"


@pytest.mark.asyncio
async def test_unshown_selection_rejected_before_writing(service):
    original = committed(service)
    t = original["tasks"][0]
    with pytest.raises(ValueError, match="shown"):
        await write_then_query(
            service,
            {
                "operation": "select",
                "trip_plan_id": original["id"],
                "expected_revision": original["revision"],
                "tasks": [
                    {
                        "task_id": t["id"],
                        "selection_refs": {"train": t["draft_plan"]["train_selection"]},
                    }
                ],
            },
            turn(),
        )
    assert service.store.load(original["id"])["revision"] == original["revision"]


def test_business_proposal_without_policy_forms_unverified_working_draft(service):
    from trip_assistant.trips.validation import check_workflow

    prepared = service.preview(
        {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [{"origin": "上海", "destination": "北京", "purpose": "business"}],
        },
        turn(),
    )
    checked = check_workflow(prepared["working"])
    assert not checked["valid"]
    assert any(i["code"] == "policy_unverified" for i in checked["issues"])
    assert service.store.list_summaries() == []


def test_input_validation_and_preview_are_side_effect_free(service):
    ctx = turn()
    ctx["original_query"] = "单程去北京"
    prepared = service.preview(
        {"operation": "propose", "tasks": [{"origin": "上海", "destination": "北京"}]}, ctx
    )
    assert prepared["query_keys"] == [] and service.store.list_summaries() == []
    assert len(prepared["working"]["tasks"]) == 1


@pytest.mark.asyncio
async def test_soft_date_can_change_but_other_components_are_preserved(service):
    original = populated_workflow(quoted=True)
    original["user_id"] = "a"
    original["tasks"][0]["condition_strengths"] = {"check_out": "soft"}
    original = service.store.save(original, expected_revision=None)
    t = original["tasks"][0]
    previous = deepcopy(t["draft_plan"]["train_selection"])
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {
                    "task_id": t["id"],
                    "components": ["hotel"],
                    "conditions": {"check_out": "2026-10-18"},
                }
            ],
        },
        turn(),
    )
    saved = service.store.load(original["id"])
    assert saved["tasks"][0]["draft_plan"]["train_selection"] == previous
    assert saved["tasks"][1]["draft_plan"] == original["tasks"][1]["draft_plan"]
    assert result["validation"]["valid"] is False


@pytest.mark.asyncio
async def test_commit_snapshot_records_actual_new_revision(service):
    original = committed(service)
    ctx = turn()
    ctx["original_query"] = "确认采用这个行程"
    result = await write_then_query(
        service,
        {
            "operation": "confirm",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
        },
        ctx,
    )
    saved = service.store.load(original["id"])
    assert result["status"] == "completed" and saved["committed"]["revision"] == saved["revision"]


@pytest.mark.asyncio
async def test_proposal_queries_top5_then_main_selects_and_commits(tmp_path):
    from datetime import datetime

    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.tools.query import QueryService
    from trip_assistant.trips.evidence import as_result
    from trip_assistant.trips.service import TripService

    provider_calls = []
    sample = populated_workflow(quoted=True)
    sample_row = next(r for r in sample["results_by_query"].values() if r["domain"] == "train")
    now = datetime.now(UTC).isoformat()

    class Provider:
        async def search(self, q):
            provider_calls.append(q)
            row = deepcopy(sample_row)
            row["parameters"] = q.to_dict()
            row["fetched_at"] = now
            row["source"]["fetched_at"] = now
            row["items"] = [
                {**row["items"][0], "id": f"candidate{i}", "source": row["source"]}
                for i in range(9)
            ]
            return as_result(row)

    settings = Settings()
    scheduler = Scheduler(settings)
    store = TripStore(str(tmp_path), "a")
    queries = QueryService({"train": Provider()}, EvidenceStore(store.storage), scheduler)
    service = TripService(store, queries, scheduler)
    ctx = turn()
    ctx["original_query"] = "单程从上海到北京"
    result = await write_then_query(
        service,
        {
            "tasks": [
                {
                    "origin": "上海",
                    "destination": "北京",
                    "conditions": {"departure_date": "2026-10-11"},
                    "items": [{"domain": "train", "parameters": {"departure_date": "2026-10-11"}}],
                }
            ]
        },
        ctx,
    )
    assert len(provider_calls) == 1 and len(result["candidates"][0]["items"]) == 5
    ctx["operation_id"] = "selection"
    view = result["candidates"][0]
    final = await write_then_query(
        service,
        {
            "operation": "select",
            "trip_plan_id": result["trip_plan_id"],
            "expected_revision": result["revision"],
            "tasks": [
                {
                    "task_id": result["tasks"][0]["id"],
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
        ctx,
    )
    assert final["status"] == "completed" and len(provider_calls) == 1


def test_unmet_item_dependency_is_saved_as_gap_without_query(service):
    ctx = turn()
    ctx["original_query"] = "单程去北京再去杭州"
    result = service.preview(
        {
            "tasks": [
                {"origin": "上海", "destination": "北京"},
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "items": [{"domain": "train", "parameters": {}, "depends_on": ["previous"]}],
                },
            ]
        },
        ctx,
    )
    assert not result["query_keys"] and result["working"]["pending_queries"]
    assert result["working"]["pending_queries"][0]["reason"] == "waiting_dependency"


@pytest.mark.asyncio
async def test_missing_query_parameters_keep_trip_progress_and_explanation(service):
    ctx = turn()
    ctx["original_query"] = "单程去北京"
    result = await write_then_query(
        service,
        {
            "tasks": [
                {
                    "origin": "上海",
                    "destination": "北京",
                    "items": [{"domain": "train", "parameters": {}}],
                }
            ]
        },
        ctx,
    )
    assert result["status"] == "partial" and result["pending_queries"]
    assert service.store.load(result["trip_plan_id"])["working"]


@pytest.mark.asyncio
async def test_hard_contradiction_reports_reason_without_question_or_provider_queries(service):
    ctx = turn()
    ctx["original_query"] = "单程去北京，必须20日出发、19日抵达"
    result = await write_then_query(
        service,
        {
            "tasks": [
                {
                    "origin": "上海",
                    "destination": "北京",
                    "conditions": {"departure_date": "2026-10-20", "arrival_date": "2026-10-19"},
                    "field_sources": {"departure_date": "user", "arrival_date": "user"},
                    "condition_strengths": {"departure_date": "hard", "arrival_date": "hard"},
                    "items": [{"domain": "train", "parameters": {}}],
                }
            ]
        },
        ctx,
    )
    assert result["status"] == "partial" and "晚于" in result["reason"]
    assert result["stop_planning"] and "？" not in result["reason"]
    assert any(i["code"] == "hard_date_conflict" for i in result["validation"]["issues"])


@pytest.mark.asyncio
async def test_unpriced_hotel_capability_gap_stops_even_without_model_stop_flag(service):
    work = populated_workflow(quoted=False)
    work["user_id"] = "a"
    work["confirmed_conditions"]["constraints"] = {"hotel_max_nightly_cny": 500}
    saved = service.store.save(work, expected_revision=None)
    result = await write_then_query(
        service,
        {
            "operation": "select",
            "trip_plan_id": saved["id"],
            "expected_revision": saved["revision"],
        },
        turn(),
    )
    assert result["stop_reason"] == "provider_capability_missing" and result["stop_planning"]
    assert service.store.load(saved["id"])["working"]


@pytest.mark.asyncio
async def test_old_cached_selection_is_not_accepted_as_fresh(service):
    saved = committed(service)
    task = saved["tasks"][0]
    ref = task["draft_plan"]["train_selection"]
    ctx = turn()
    ctx["shown_candidates"][ref["candidate_id"]] = {
        "query_id": ref["query_id"],
        "result_revision": ref["result_revision"],
    }
    with pytest.raises(ValueError, match="expired"):
        await write_then_query(
            service,
            {
                "operation": "select",
                "trip_plan_id": saved["id"],
                "expected_revision": saved["revision"],
                "tasks": [{"task_id": task["id"], "selection_refs": {"train": ref}}],
            },
            ctx,
        )


@pytest.mark.asyncio
async def test_native_amend_resumes_stopped_trip_without_keyword_gate(service):
    original = committed(service)
    stopped = await write_then_query(
        service,
        {
            "operation": "pause",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "reason": "等用户核实",
        },
        turn(),
    )
    assert stopped["stop_planning"]
    ctx = turn()
    ctx["operation_id"] = "resume"
    ctx["original_query"] = "接着办吧"
    resumed = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": stopped["revision"],
        },
        ctx,
    )
    assert resumed["status"] == "completed" and not resumed["stop_planning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["就这个", "可以"])
async def test_native_confirmation_does_not_require_confirmation_keyword(service, query):
    original = committed(service)
    ctx = turn()
    ctx["original_query"] = query
    await write_then_query(
        service,
        {
            "operation": "confirm",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
        },
        ctx,
    )
    assert service.store.load(original["id"])["user_confirmed"]


@pytest.mark.asyncio
async def test_interrupted_query_preserves_new_working_trip(service):
    import asyncio
    from types import SimpleNamespace

    async def interrupted(item, ctx):
        raise asyncio.CancelledError()

    service.queries = SimpleNamespace(expand_queries=lambda *args: ["key"], fetch=interrupted)
    ctx = turn()
    ctx["original_query"] = "单程去北京"
    args = {
        "tasks": [
            {
                "origin": "上海",
                "destination": "北京",
                "conditions": {"departure_date": "2026-10-20"},
                "items": [{"domain": "train", "parameters": {}}],
            }
        ]
    }
    with pytest.raises(asyncio.CancelledError):
        await write_then_query(service, args, ctx)
    saved = service.store.list_all()
    assert len(saved) == 1 and saved[0]["working"]
    assert saved[0]["stop_reason"] == "interrupted_query"
    assert saved[0]["pending_queries"][0]["item"]["domain"] == "train"
    assert service.store.lookup_operation(ctx["operation_id"]) is not None
    assert service.store.lookup_operation(ctx["operation_id"] + "-query") is None


@pytest.mark.parametrize("levels", [{}, {"hotel_max_nightly_cny": "hard"}])
def test_task_cannot_override_nested_global_hard_budget(service, levels):
    with pytest.raises(ValueError, match="hard global"):
        service.preview(
            {
                "confirmed_conditions": {"constraints": {"hotel_max_nightly_cny": 500}},
                "tasks": [
                    {
                        "origin": "上海",
                        "destination": "北京",
                        "conditions": {"constraints": {"hotel_max_nightly_cny": 800}},
                        "condition_strengths": levels,
                    }
                ],
            },
            turn(),
        )


@pytest.mark.asyncio
async def test_failed_refresh_cannot_commit_old_quote_as_current(service):
    from types import SimpleNamespace

    original = committed(service)
    task = original["tasks"][0]

    async def failed(item, turn):
        return {"domain": "hotel", "status": "error", "items": [], "reason": "供应商超时"}

    service.queries = SimpleNamespace(expand_queries=lambda *args: ["key"], fetch=failed)
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {
                    "task_id": task["id"],
                    "components": ["hotel"],
                    "items": [{"domain": "hotel", "parameters": {}, "refresh": True}],
                }
            ],
        },
        turn(),
    )
    saved = service.store.load(original["id"])
    assert result["status"] != "completed" and saved["committed"] == original["committed"]
    assert any(
        q["domain"] == "hotel" and q["needs_revalidation"]
        for q in saved["results_by_query"].values()
    )
    assert saved["tasks"][0]["query_containers"]["hotel"]["status"] == "failed"
    assert saved["results_by_query"][task["draft_plan"]["hotel_selection"]["query_id"]]["items"]


@pytest.mark.asyncio
async def test_route_insertion_keeps_unchanged_leg_ids_and_evidence(service):
    original = committed(service)
    first, middle, last = original["tasks"]
    ctx = turn()
    ctx["original_query"] = "调整路线，在北京和杭州之间新增苏州一站，其余安排保留"
    route = [
        {"task_id": first["id"], "origin": first["origin"], "destination": first["destination"]},
        {"origin": "北京", "destination": "苏州", "conditions": {"departure_date": "2026-10-13"}},
        {"origin": "苏州", "destination": "杭州", "conditions": {"departure_date": "2026-10-14"}},
        {"task_id": last["id"], "origin": last["origin"], "destination": last["destination"]},
    ]
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "route_tasks": route,
        },
        ctx,
    )
    work = service.store.load(original["id"])
    assert work["id"] == original["id"] and work["committed"] == original["committed"]
    assert work["tasks"][0] == first
    assert work["tasks"][-1]["id"] == last["id"]
    assert work["tasks"][1]["id"] not in {t["id"] for t in original["tasks"]}
    assert work["tasks"][2]["depends_on"] == [work["tasks"][1]["id"]]
    assert (
        work["results_by_query"][first["draft_plan"]["train_selection"]["query_id"]]["items"]
        == original["results_by_query"][first["draft_plan"]["train_selection"]["query_id"]]["items"]
    )
    assert result["status"] == "partial"


def test_native_route_replacement_still_rejects_broken_route_before_writing(service):
    original = committed(service)
    args = {
        "operation": "amend",
        "trip_plan_id": original["id"],
        "expected_revision": original["revision"],
        "route_tasks": [{"origin": "上海", "destination": "杭州"}],
    }
    prepared = service.preview(args, turn())
    assert prepared["working"]["tasks"][0]["destination"] == "杭州"
    ctx = turn()
    ctx["original_query"] = "调整路线到杭州和苏州"
    args["route_tasks"].append({"origin": "苏州", "destination": "北京"})
    with pytest.raises(ValueError):
        service.preview(args, ctx)
    assert service.store.load(original["id"])["revision"] == original["revision"]


@pytest.mark.asyncio
async def test_origin_only_change_preserves_hotel_and_other_tasks(service):
    original = committed(service)
    task = original["tasks"][0]
    ctx = turn()
    ctx["original_query"] = "把出发地改为重庆"
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [{"task_id": task["id"], "origin": "重庆", "components": ["train"]}],
        },
        ctx,
    )
    saved = service.store.load(original["id"])
    assert (
        saved["tasks"][0]["draft_plan"]["hotel_selection"] == task["draft_plan"]["hotel_selection"]
    )
    assert saved["tasks"][1:] == original["tasks"][1:]
    assert result["status"] == "partial"


def test_strength_only_change_invalidates_task_validation(service):
    original = committed(service)
    task = original["tasks"][0]
    prepared = service.preview(
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [{"task_id": task["id"], "condition_strengths": {"check_out": "hard"}}],
        },
        turn(),
    )
    changed = prepared["working"]["tasks"][0]
    assert changed["revision"] == task["revision"] + 1
    assert changed["draft_plan"]["hotel_selection"] is None


def test_business_amendment_requires_recheck_for_changed_policy_scope(service):
    original = committed(service)
    original["tasks"][0]["purpose"] = "business"
    original["policy_context"] = {
        "status": "ok",
        "summary": "北京规则",
        "applicability": {"city": "北京"},
        "constraints": [],
        "references": [{"id": "p"}],
        "missing_fields": [],
        "conflicts": [],
        "effective_date": None,
    }
    saved = service.store.save(original, expected_revision=original["revision"])
    ctx = turn()
    ctx["original_query"] = "把目的地改为苏州"
    prepared = service.preview(
        {
            "operation": "amend",
            "trip_plan_id": saved["id"],
            "expected_revision": saved["revision"],
            "tasks": [{"task_id": saved["tasks"][0]["id"], "destination": "苏州"}],
        },
        ctx,
    )
    assert "policy_applicability:city" in prepared["working"]["policy_context"]["missing_fields"]
    assert service.store.load(saved["id"])["revision"] == saved["revision"]


@pytest.mark.asyncio
async def test_changed_checkout_reports_dependent_recheck_without_erasing_quotes(service):
    original = committed(service)
    task = original["tasks"][0]
    next_task = original["tasks"][1]
    result = await write_then_query(
        service,
        {
            "operation": "amend",
            "trip_plan_id": original["id"],
            "expected_revision": original["revision"],
            "tasks": [
                {
                    "task_id": task["id"],
                    "components": ["hotel"],
                    "conditions": {"check_out": "2026-10-18"},
                }
            ],
        },
        turn(),
    )
    assert any(
        i["code"] == "departure_before_previous_checkout" for i in result["validation"]["issues"]
    )
    assert next_task["id"] in result["dependency_impacts"]
    saved = service.store.load(original["id"])
    assert saved["tasks"][1]["needs_dependency_check"]
    assert (
        saved["tasks"][1]["draft_plan"]["train_selection"]
        == next_task["draft_plan"]["train_selection"]
    )
    assert saved["committed"] == original["committed"]


def test_repair_cannot_target_unrelated_task(service):
    original = committed(service)
    original["validation"]["issues"] = [
        {
            "code": "departure_before_previous_checkout",
            "task_ids": [t["id"] for t in original["tasks"][:2]],
            "blocking": True,
            "message": "衔接冲突",
        }
    ]
    original["validation"]["valid"] = False
    saved = service.store.save(original, expected_revision=original["revision"])
    args = {
        "operation": "amend",
        "trip_plan_id": saved["id"],
        "expected_revision": saved["revision"],
        "repair_issue_codes": ["departure_before_previous_checkout"],
        "tasks": [{"task_id": saved["tasks"][-1]["id"], "components": ["train"]}],
    }
    with pytest.raises(ValueError, match="unrelated repair"):
        service.preview(args, turn())
    args["tasks"][0]["task_id"] = saved["tasks"][1]["id"]
    assert service.preview(args, turn())["working"]["id"] == saved["id"]
