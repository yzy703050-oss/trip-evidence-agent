from dataclasses import replace

from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.settings import Settings


def proposal():
    return {"request_kind": "planning", "tripplan": {
        "operation": "propose", "default_round_trip": False,
        "tasks": [{"origin": "重庆", "destination": "上海",
                   "conditions": {"departure_date": "2026-10-23"},
                   "items": [{"domain": "train"}]}],
    }}


async def test_batch_previews_once_and_apply_revalidates_inside_write_lock(tmp_path, monkeypatch):
    provider = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    original = runtime.trip_service.preview
    phases = []

    def preview(plan, turn):
        phases.append(bool(turn.get("operation_id")))
        if turn.get("operation_id"):
            assert runtime.scheduler.write_lock(turn["operation_id"]).locked()
        return original(plan, turn)

    monkeypatch.setattr(runtime.trip_service, "preview", preview)
    result = await execute_batch(
        [{"id": "p", "name": "write_trip", "arguments": proposal()}],
        registry=runtime.registry, turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings), sessions=runtime.sessions,
    )
    assert result[0]["status"] == "partial"
    assert len(provider.calls) == 1
    assert phases == [False, True]


async def test_custom_preflight_hook_still_accounts_for_automatic_queries(tmp_path):
    provider = SimulatedTrainProvider()
    settings = Settings(_env_file=None, external_queries_per_batch=1)
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider}, settings=settings)
    checked = []

    def custom(args, turn):
        checked.append(args)
        return ["custom-extra-query"]

    runtime.registry["write_trip"] = replace(runtime.registry["write_trip"], expand_queries=custom)
    result = await execute_batch(
        [{"id": "p", "name": "write_trip", "arguments": proposal()}],
        registry=runtime.registry, turn={"turn_id": "t"},
        budget=TurnBudget(settings), sessions=runtime.sessions,
    )
    assert checked
    assert result[0]["status"] == "rejected"
    assert result[0]["reason"] == "external_query_limit_exceeded"
    assert runtime.trip_service.store.list_all() == []
    assert provider.calls == []
