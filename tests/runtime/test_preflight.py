import pytest


@pytest.mark.asyncio
async def test_ninth_call_and_internal_thirteenth_query_have_no_side_effects(tmp_path):
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore
    from trip_assistant.tools.registry import build_registry

    count = 0

    async def handler(args, turn):
        nonlocal count
        count += 1
        return {"status": "ok"}

    registry = build_registry(
        {name: handler for name in ["read", "query", "write_memory", "write_trip", "query_policy"]}
    )
    sessions = SessionStore(tmp_path, "alice", "s")
    b = TurnBudget(Settings(_env_file=None))
    calls = [
        {
            "id": str(i),
            "name": "read",
            "arguments": {"request_kind": "lookup", "resource": "trip_summaries"},
        }
        for i in range(9)
    ]
    results = await execute_batch(
        calls, registry=registry, turn={"turn_id": "t"}, budget=b, sessions=sessions
    )
    assert len(results) == 9 and all(r["status"] == "rejected" for r in results) and count == 0
    from dataclasses import replace

    registry["write_memory"] = replace(
        registry["write_memory"], expand_queries=lambda args, turn: [str(i) for i in range(13)]
    )
    results = await execute_batch(
        [
            {
                "id": "w",
                "name": "write_memory",
                "arguments": {"request_kind": "lookup", "updates": [{"op": "add", "text": "x"}]},
            }
        ],
        registry=registry,
        turn={"turn_id": "t2"},
        budget=b,
        sessions=sessions,
    )
    assert results[0]["status"] == "rejected" and count == 0


@pytest.mark.asyncio
async def test_memory_write_precedes_reads_and_external_queries(tmp_path):
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore
    from trip_assistant.tools.registry import build_registry

    seen = []

    def handler(name):
        async def run(args, turn):
            seen.append(name)
            return {"status": "ok"}

        return run

    registry = build_registry({n: handler(n) for n in ["read", "query", "write_memory"]})
    calls = [
        {
            "id": "r",
            "name": "read",
            "arguments": {"request_kind": "planning", "resource": "trip_summaries"},
        },
        {
            "id": "w",
            "name": "write_memory",
            "arguments": {"request_kind": "planning", "updates": [{"op": "add", "text": "偏好"}]},
        },
    ]
    results = await execute_batch(
        calls,
        registry=registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=SessionStore(tmp_path, "a", "s"),
    )
    assert seen == ["write_memory", "read"]
    assert [r["call_id"] for r in results] == ["r", "w"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "partial"])
async def test_failed_memory_write_blocks_dependent_planning_but_all_results_are_paired(
    tmp_path, failure
):
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore
    from trip_assistant.tools.registry import build_registry

    seen = []

    async def write(args, turn):
        return {"status": failure, "reason": "偏好未成功保存"}

    async def effect(args, turn):
        seen.append(args)
        return {"status": "ok"}

    registry = build_registry({"write_memory": write, "write_trip": effect, "query": effect})
    calls = [
        {
            "id": "memory",
            "name": "write_memory",
            "arguments": {
                "request_kind": "planning",
                "updates": [{"op": "add", "text": "出差优先高铁"}],
            },
        },
        {
            "id": "plan",
            "name": "write_trip",
            "arguments": {
                "request_kind": "planning",
                "tripplan": {"tasks": [{"origin": "上海", "destination": "北京"}]},
            },
        },
    ]
    sessions = SessionStore(tmp_path, "a", "s")
    results = await execute_batch(
        calls,
        registry=registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=sessions,
    )
    assert not seen and results[1]["status"] == "rejected"
    assert results[1]["reason"] == "preference_write_failed" and not sessions.pending_tool_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_schema", [True, False])
async def test_rejected_trip_proposals_still_use_three_planning_rounds(tmp_path, invalid_schema):
    from dataclasses import replace

    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore
    from trip_assistant.tools.registry import build_registry

    def expand(args, turn):
        raise ValueError("route is not continuous")

    registry = build_registry({})
    registry["write_trip"] = replace(registry["write_trip"], expand_queries=expand)
    budget = TurnBudget(Settings(_env_file=None, planning_rounds=3))
    sessions = SessionStore(tmp_path, "a", "s")
    args = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "tasks": [{"origin": "北京", "destination": "上海", "requires_hotel": False}],
        },
    }
    if invalid_schema:
        args["tripplan"]["unknown_field"] = True
    for i in range(4):
        result = await execute_batch(
            [{"id": str(i), "name": "write_trip", "arguments": args}],
            registry=registry,
            turn={"turn_id": "t"},
            budget=budget,
            sessions=sessions,
        )
        assert result[0]["status"] == "rejected"
        if i < 3 and not invalid_schema:
            assert result[0]["reason"] == "route is not continuous"
    assert budget.rounds == 3
    assert result[0]["reason"] == "planning_round_limit_exceeded"
    assert not sessions.pending_tool_calls()
