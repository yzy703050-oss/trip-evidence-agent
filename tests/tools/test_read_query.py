from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_query_cache_reuse_explicit_refresh_and_read_page(tmp_path):
    from trip_assistant.providers.contracts import AgentDataResult, Source
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.query import QueryService
    from trip_assistant.tools.read import build_read_handler

    class Provider:
        calls = 0

        async def search(self, q):
            self.calls += 1
            src = Source("offline", datetime(2026, 10, 15, tzinfo=UTC), "https://example.com")
            return AgentDataResult(
                "ok",
                q.to_dict(),
                [
                    {
                        "id": "x",
                        "hotel_name": "上海酒店",
                        "kind": "hotel_place",
                        "city": "上海",
                        "location": "121.1,31.2",
                        "address": "路",
                        "source": src.to_dict(),
                    }
                ],
                [],
                src,
                src.fetched_at,
                None,
            )

        hotel_places = True

    settings = Settings(_env_file=None)
    provider = Provider()
    memory = LongTermMemory("a", str(tmp_path))
    evidence = EvidenceStore(memory.storage)
    service = QueryService(
        {"hotel": provider}, evidence, Scheduler(settings), now=lambda: "2026-10-15T00:10:00+00:00"
    )
    turn = {"turn_id": "t", "budget": TurnBudget(settings)}
    args = {
        "request_kind": "lookup",
        "query_plan": [
            {"domain": "hotel", "parameters": {"city": "上海"}, "ranking": [], "constraints": {}}
        ],
    }
    first = await service.handler(args, turn)
    again = await service.handler(args, turn)
    assert provider.calls == 1 and again["data"][0]["cache_hit"]
    args["query_plan"][0]["refresh"] = True
    fresh = await service.handler(args, turn)
    assert provider.calls == 2 and fresh["data"][0]["result_revision"] == 2
    read = build_read_handler(memory.workflow_store, evidence)
    result = await read(
        {
            "resource": "candidates",
            "query_id": first["data"][0]["query_id"],
            "result_revision": 1,
            "offset": 0,
        },
        turn,
    )
    assert result["data"]["historical"] and provider.calls == 2


@pytest.mark.asyncio
async def test_simple_write_only_changes_preferences(tmp_path):
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.memory_write import build_memory_writer

    m = LongTermMemory("a", str(tmp_path))
    write = build_memory_writer(m)
    result = await write({"updates": [{"op": "add", "text": "以后出差尽量高铁"}]}, {})
    assert result["status"] == "ok" and len(m.data["travel_memories"]) == 1
    assert m.workflow_store.list_summaries() == []


@pytest.mark.asyncio
async def test_normalized_transient_failures_reach_single_retry_owner(tmp_path):
    from support.native import FakeClock

    from trip_assistant.providers.contracts import AgentDataResult
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.sqlite import SQLiteStorage
    from trip_assistant.tools.query import QueryService

    class Provider:
        hotel_places = True
        calls = 0

        async def search(self, q):
            self.calls += 1
            return AgentDataResult(
                "error", q.to_dict(), [], [], None, None, "temporary", error_type="transient"
            )

    settings = Settings(_env_file=None)
    provider = Provider()
    clock = FakeClock()
    scheduler = Scheduler(settings, clock=clock)
    service = QueryService(
        {"hotel": provider}, EvidenceStore(SQLiteStorage(tmp_path, "a")), scheduler
    )
    service.retry_clock = clock
    result = await service.handler(
        {"query_plan": [{"domain": "hotel", "parameters": {"city": "上海"}}]},
        {"budget": TurnBudget(settings, clock=clock)},
    )
    assert provider.calls == 3 and result["status"] == "partial"
    assert scheduler.breakers.entries["Provider:hotel"]["failures"]


@pytest.mark.asyncio
async def test_candidate_paging_keeps_original_filters_and_order(tmp_path):
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.read import build_read_handler

    m = LongTermMemory("a", str(tmp_path))
    evidence = EvidenceStore(m.storage)
    value = evidence.save(
        {
            "status": "ok",
            "domain": "train",
            "query": {},
            "fetched_at": "2026-10-15T00:00:00+00:00",
            "items": [
                {"id": str(i), "price_cny": str(i), "train_number": "G" + str(i)} for i in range(12)
            ],
        },
        "test",
    )
    key = value["query_id"] + ":1"
    turn = {
        "candidate_views": {
            key: {
                "constraints": {"train_max_total_cny": 9},
                "ranking": [{"field": "price_cny", "direction": "desc"}],
                "view_offset": 0,
                "has_more": True,
            }
        }
    }
    result = await build_read_handler(m.workflow_store, evidence)(
        {
            "resource": "candidates",
            "query_id": value["query_id"],
            "result_revision": 1,
            "offset": 5,
        },
        turn,
    )
    assert [i["id"] for i in result["data"]["items"]] == ["4", "3", "2", "1", "0"]
    assert result["data"]["candidate_total"] == 10


@pytest.mark.asyncio
async def test_paging_cannot_skip_ahead_or_change_original_conditions(tmp_path):
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.read import build_read_handler

    m = LongTermMemory("a", str(tmp_path))
    e = EvidenceStore(m.storage)
    value = e.save(
        {
            "status": "ok",
            "domain": "train",
            "query": {},
            "items": [{"id": str(i)} for i in range(20)],
        },
        "test",
    )
    turn = {
        "candidate_views": {
            value["query_id"] + ":1": {
                "constraints": {},
                "ranking": [],
                "view_offset": 0,
                "has_more": True,
            }
        }
    }
    read = build_read_handler(m.workflow_store, e)
    with pytest.raises(ValueError):
        await read(
            {
                "resource": "candidates",
                "query_id": value["query_id"],
                "result_revision": 1,
                "offset": 10,
            },
            turn,
        )


@pytest.mark.asyncio
async def test_legacy_history_is_discovered_and_read_by_exact_id(tmp_path):
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.read import build_read_handler

    memory = LongTermMemory("a", str(tmp_path))
    trips = memory.workflow_store
    trips.save_history(
        {
            "trip_id": "legacy-trip",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-10-15",
            "end_date": "2026-10-17",
            "purpose": "business",
            "summary": "以前出差",
            "itinerary": {"notes": "原记录"},
        }
    )
    read = build_read_handler(trips, EvidenceStore(memory.storage))
    result = await read(
        {"resource": "trip_summaries", "filters": {"city": "上海", "date": "2026-10-15"}}, {}
    )
    assert len(result["data"]["summaries"]) == 1
    summary = result["data"]["summaries"][0]
    assert summary["id"].startswith("history_") and summary["read_only"]
    detail = await read({"resource": "trip", "trip_plan_id": summary["id"]}, {})
    assert detail["data"]["historical"] and detail["data"]["itinerary"]["notes"] == "原记录"
    with pytest.raises(ValueError, match="executable"):
        trips.save(
            {"id": summary["id"], "user_id": "a", "revision": 1, "tasks": []},
            expected_revision=None,
        )


@pytest.mark.asyncio
async def test_unexpected_provider_exception_does_not_enter_main_context(tmp_path):
    from support.native import NativeRuntimeFixture

    class Provider:
        async def search(self, query):
            raise RuntimeError("secret-token")

    runtime = NativeRuntimeFixture(tmp_path, {"guide": Provider()})
    from trip_assistant.runtime.limits import TurnBudget

    value = await runtime.queries.fetch(
        {"domain": "guide", "parameters": {"destination": "北京"}},
        {"budget": TurnBudget(runtime.settings)},
    )
    assert value["status"] == "error" and "secret-token" not in str(value)


@pytest.mark.asyncio
async def test_cross_session_read_is_explicit_isolated_and_hides_child_audit(tmp_path):
    from support.native import make_application, record_user

    from trip_assistant.tools.read import build_read_handler

    old = make_application("a", "old", str(tmp_path))
    tid = record_user(old.sessions, "之前会议在苏州")
    old.sessions.append(
        {
            "scope": "session",
            "type": "message",
            "role": "assistant",
            "turn_id": tid,
            "content": "已记下会议地点。",
            "final": True,
        }
    )
    old.sessions.append(
        {"scope": "policy", "type": "agent_stage", "content": {"raw": "PRIVATE_CHILD"}}
    )
    old.sessions.update_state({"summary": "会议地点苏州"})
    other = make_application("b", "foreign", str(tmp_path))
    record_user(other.sessions, "PRIVATE_FOREIGN")
    current = make_application("a", "current", str(tmp_path))
    read = build_read_handler(current.trips, current.evidence)
    listed = await read({"resource": "session_summaries", "filters": {"query": "会议"}}, {})
    assert [r["session_id"] for r in listed["data"]["summaries"]] == ["old"]
    before = old.sessions.read_events()
    detail = await read({"resource": "session", "session_id": "old"}, {})
    assert detail["data"]["summary"] == "会议地点苏州"
    assert "之前会议在苏州" in str(detail) and "PRIVATE_CHILD" not in str(detail)
    assert "PRIVATE_FOREIGN" not in str(detail) and old.sessions.read_events() == before
    with pytest.raises(ValueError, match="session_not_found"):
        await read({"resource": "session", "session_id": "foreign"}, {})
    assert [r["session_id"] for r in current.sessions.list_sessions(tmp_path, "a")] == ["old"]
