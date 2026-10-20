from copy import deepcopy

from support.native import NativeRuntimeFixture
from support.workflow import populated_workflow

from trip_assistant.memory.planning import review_payload


def test_equal_policy_has_one_full_source_and_original_records_remain_intact(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    work = populated_workflow(quoted=True)
    policy = {
        "status": "ok",
        "constraints": [{"field": "seat_class", "value": "二等座"}],
        "references": [{"id": "p"}],
        "summary": "仅二等座",
    }
    work["policy_context"] = deepcopy(policy)
    work["constraint_snapshot"] = {
        "revision": 1,
        "tasks": {},
        "policy": {"status": "ok", "constraints": deepcopy(policy["constraints"])},
    }
    saved = runtime.trip_service.store.save(work, expected_revision=None)
    runtime.sessions.record_call({"id": "p", "name": "query_policy", "arguments": {}}, "t")
    runtime.sessions.record_result(
        "p", {"name": "query_policy", "status": "ok", "data": policy}, "t"
    )
    before = runtime.sessions.read_events()
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "去上海"}, saved["id"]
    )
    assert payload["tripplan"]["policy_context"] == policy
    assert payload["tripplan"]["constraint_snapshot"]["policy"]["ref"] == "tripplan.policy_context"
    assert payload["observations"][0]["data"] == {"policy_in_tripplan": True}
    assert len(payload["tripplan"]["tasks"]) == len(saved["tasks"])
    assert runtime.trip_service.store.load(saved["id"]) == saved
    assert runtime.sessions.read_events() == before


def test_different_policy_evidence_is_not_deduplicated(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    work = populated_workflow()
    work["policy_context"] = {"status": "ok", "constraints": [{"value": 100}]}
    work["constraint_snapshot"] = {
        "revision": 1,
        "tasks": {},
        "policy": {"constraints": [{"value": 200}]},
    }
    saved = runtime.trip_service.store.save(work, expected_revision=None)
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "继续"}, saved["id"]
    )
    assert payload["tripplan"]["constraint_snapshot"]["policy"]["constraints"] == [{"value": 200}]


async def test_execution_limits_and_supplier_capabilities_are_not_repeated_system_messages(
    tmp_path,
):
    runtime = NativeRuntimeFixture(tmp_path)
    runtime.model.script = ["你好"]
    await runtime.run("你好")
    assert not any("执行额度（数据）" in str(m["content"]) for m in runtime.model.messages[0][0])
