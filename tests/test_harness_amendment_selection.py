"""Actual saved candidate identities and authorized names govern amendments."""

from copy import deepcopy

import pytest
from workflow_support import populated_workflow

from agents.contracts import RunState
from agents.workflow_amendments import apply_amendment_patch
from agents.workflow_runner import WorkflowRunner


def fixture():
    w = populated_workflow()
    task = w["tasks"][0]
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == task["id"] and r["domain"] == "hotel"
    )
    row["items"] = [
        dict(
            row["items"][0],
            id=f"opaque-{(i * 47 + 19) % 101}",
            hotel_name=f"全季酒店（上海模拟门店{i + 1:02d}）",
        )
        for i in range(12)
    ]
    task["draft_plan"]["hotel_selection"]["candidate_id"] = row["items"][0]["id"]
    update = {
        "update_type": "replace",
        "target": {"workflow_id": w["id"], "task_ids": [task["id"]], "components": ["hotel"]},
        "condition_updates": {},
    }
    return w, task, row, update


def patch(w, task, row, item):
    return {
        "workflow_id": w["id"],
        "expected_workflow_revision": w["revision"],
        "task_id": task["id"],
        "expected_task_revision": task["revision"],
        "components": ["hotel"],
        "condition_changes": {},
        "selection_refs": {
            "hotel": {
                "query_id": row["id"],
                "result_revision": row["result_revision"],
                "candidate_id": item["id"],
            }
        },
    }


def test_non_name_condition_is_not_parsed_as_hotel_target():
    from agents.amendment_selection import resolve_literal_selection_requirements

    w, task, row, update = fixture()
    assert resolve_literal_selection_requirements("酒店换一家，人数改为3人", [row]) == {}


class Runner(WorkflowRunner):
    commits = 0

    async def _amend_request(self, workflow, proposed, context, run):
        result = apply_amendment_patch(workflow, proposed, context)
        self.commits += 1
        return result


@pytest.mark.asyncio
async def test_initial_patch_cannot_bypass_loaded_target_selection():
    w, task, row, update = fixture()

    class Main:
        async def initialize(self, context):
            return {
                "response_mode": "workflow",
                "resume_workflow_id": w["id"],
                "amendment_patch": patch(w, task, row, row["items"][10]),
            }

        async def amend_saved_trip(self, context):
            target = next(v for v in context["candidate_views"] if v["domain"] == "hotel")
            assert "门店12" in target["items"][0]["hotel_name"]
            return patch(w, task, row, target["items"][0])

    class LocalRunner(Runner):
        def _load_target(self, identity, context):
            return w

    result = await LocalRunner(Main())._request_flow(
        {"original_query": "换成名称带模拟门店12的那家酒店"}, RunState("initial-patch")
    )
    assert (
        result["candidate_workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]["candidate_id"]
        == row["items"][11]["id"]
    )


@pytest.mark.asyncio
async def test_plain_replace_with_valid_alternatives_finishes_selection():
    w, task, row, update = fixture()
    seen = []

    class Main:
        async def amend_saved_trip(self, context):
            seen.append(context)
            if "ask_user" in context.get("allowed_actions", ["ask_user"]):
                return {"action": "ask_user", "question": "请选择一家"}
            hotels = next(v for v in context["candidate_views"] if v["domain"] == "hotel")
            return patch(w, task, row, hotels["items"][0])

    runner = Runner(Main())
    result = await runner._decide_amendment(
        w, update, {"original_query": "换一家酒店，火车日期不变"}, RunState("simple")
    )
    assert runner.commits == 1
    assert len(seen) == 1
    assert (
        result["candidate_workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]["candidate_id"]
        != row["items"][0]["id"]
    )


@pytest.mark.asyncio
async def test_named_target_uses_real_ref_not_number():
    w, task, row, update = fixture()
    contexts = []

    class Main:
        async def amend_saved_trip(self, context):
            contexts.append(deepcopy(context))
            hotels = next(v for v in context["candidate_views"] if v["domain"] == "hotel")
            target = next((i for i in hotels["items"] if "门店12" in i["hotel_name"]), None)
            assert target is not None, "unique literal target must be retrieved before selection"
            return patch(w, task, row, target)

    runner = Runner(Main())
    before = deepcopy(w)
    result = await runner._decide_amendment(
        w,
        update,
        {"original_query": "酒店换成名称带模拟门店12的那家，火车日期保持不变"},
        RunState("target"),
    )
    selected = result["candidate_workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]
    assert selected["candidate_id"] == row["items"][11]["id"]
    assert w == before
    assert len(contexts) == 1


@pytest.mark.asyncio
async def test_existing_but_unseen_id_is_not_committed():
    w, task, row, update = fixture()

    class Main:
        async def amend_saved_trip(self, context):
            return patch(w, task, row, row["items"][10])

    runner = Runner(Main())
    result = await runner._decide_amendment(
        w, update, {"original_query": "换一家酒店，火车不变"}, RunState("unseen")
    )
    assert runner.commits == 0
    assert result["status"] in {"partial", "needs_input", "error"}


@pytest.mark.asyncio
async def test_amendment_can_read_cached_later_page_and_keep_first_refs():
    w, task, row, update = fixture()
    calls = []

    class Main:
        async def amend_saved_trip(self, context):
            hotels = next(v for v in context["candidate_views"] if v["domain"] == "hotel")
            calls.append(deepcopy(hotels))
            if len(calls) <= 2:
                return {
                    "action": "read_cached_page",
                    "query_id": row["id"],
                    "result_revision": row["result_revision"],
                    "task_id": task["id"],
                    "task_revision": task["revision"],
                    "domain": "hotel",
                    "offset": hotels["view_offset"] + len(hotels["items"]),
                }
            return patch(w, task, row, calls[0]["items"][0])

    runner = Runner(Main())
    run = RunState("pages")
    result = await runner._decide_amendment(
        w, update, {"original_query": "换一家酒店，比较完整缓存后再决定"}, run
    )
    assert [c["view_offset"] for c in calls] == [0, 5, 10]
    assert [len(c["items"]) for c in calls] == [5, 5, 1]  # original choice excluded
    assert run.external_request_count == 0
    assert (
        result["candidate_workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]["candidate_id"]
        == row["items"][1]["id"]
    )


@pytest.mark.asyncio
async def test_wrong_named_target_is_rejected_and_original_plan_preserved():
    w, task, row, update = fixture()
    original = deepcopy(w)

    class Main:
        async def amend_saved_trip(self, context):
            wrong = patch(w, task, row, row["items"][10])
            wrong["reply_template"] = "已换成模拟门店12"
            return wrong

    runner = Runner(Main())
    result = await runner._decide_amendment(
        w, update, {"original_query": "请换成名称带模拟门店12的那家"}, RunState("wrong")
    )
    assert runner.commits == 0
    assert result["status"] in {"partial", "needs_input", "error"}
    assert w == original


@pytest.mark.asyncio
async def test_missing_or_ambiguous_named_target_does_not_choose_another():
    w, task, row, update = fixture()

    class Main:
        async def amend_saved_trip(self, context):
            raise AssertionError("must report missing target without selecting another hotel")

    result = await Runner(Main())._decide_amendment(
        w,
        update,
        {"original_query": "请换成名称带不存在门店99的酒店，不要重新查询"},
        RunState("missing"),
    )
    assert result["status"] in {"partial", "needs_input"}


@pytest.mark.asyncio
async def test_nested_model_page_command_retrieves_only_authorized_next_page():
    w, task, row, update = fixture()
    contexts = []

    class Main:
        async def amend_saved_trip(self, context):
            contexts.append(context)
            if len(contexts) == 1:
                return {
                    "action": "read_cached_page",
                    "cached_page_options": context["cached_page_options"][0],
                }
            return patch(w, task, row, context["candidate_views"][1]["items"][0])

    runner = Runner(Main())
    result = await runner._decide_amendment(
        w, update, {"original_query": "比较后页再换一家酒店，火车不变"}, RunState("nested")
    )
    assert runner.commits == 1
    assert (
        result["candidate_workflow"]["tasks"][0]["draft_plan"]["hotel_selection"]["candidate_id"]
        != row["items"][0]["id"]
    )
    assert contexts[1]["candidate_views"][1]["view_offset"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["revision", "repeat", "ambiguous"])
async def test_invalid_pages_or_ambiguous_target_never_commit(kind):
    w, task, row, update = fixture()
    before = deepcopy(w)
    commands = []

    class Main:
        async def amend_saved_trip(self, context):
            assert kind != "ambiguous"
            if not commands:
                commands.append(deepcopy(context["cached_page_options"][0]))
            command = deepcopy(commands[0])
            if kind == "revision":
                command["result_revision"] += 1
            return {"action": "read_cached_page", **command}

    runner = Runner(Main())
    query = "换成名称带模拟门店的那家" if kind == "ambiguous" else "比较后页再换一家酒店，火车不变"
    result = await runner._decide_amendment(w, update, {"original_query": query}, RunState(kind))
    assert result["status"] in {"partial", "needs_input"}
    assert runner.commits == 0 and w == before
