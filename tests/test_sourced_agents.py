import json
from datetime import UTC, datetime

import pytest

from trip_assistant.providers.contracts import AgentDataResult, Source

CASES = [
    (
        "train_search",
        {"origin": "上海", "destination": "北京", "departure_date": "2026-10-20"},
        "departure_date",
    ),
    (
        "hotel_search",
        {"city": "北京", "check_in": "2026-10-20", "check_out": "2026-10-22", "guests": 2},
        "check_in",
    ),
    ("travel_guide", {"destination": "北京", "visit_dates": ["2026-10-20"]}, "destination"),
]


class RecordingProvider:
    def __init__(self, fail=False, empty=False):
        self.queries = []
        self.fail = fail
        self.empty = empty
        self.source = Source("test-only", datetime(2026, 10, 8, tzinfo=UTC), "https://example.org")

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise TimeoutError("test timeout")
        return AgentDataResult(
            "ok", query.to_dict(), [], [], self.source, self.source.fetched_at, None
        )


def service(tmp_path, name, provider=None):
    from support.native import FakeClock

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.sqlite import SQLiteStorage
    from trip_assistant.tools.query import QueryService

    domain = {"train_search": "train", "hotel_search": "hotel", "travel_guide": "guide"}[name]
    clock = FakeClock()
    settings = Settings(_env_file=None)
    queries = QueryService(
        {domain: provider} if provider else {},
        EvidenceStore(SQLiteStorage(tmp_path, "a")),
        Scheduler(settings, clock=clock),
    )
    queries.retry_clock = clock
    return domain, queries, {"budget": TurnBudget(settings, clock=clock)}


@pytest.mark.parametrize("name,fields,required", CASES)
@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.asyncio
async def test_queries_and_sources_pass_through(tmp_path, name, fields, required, previous):
    provider = RecordingProvider()
    domain, queries, turn = service(tmp_path, name, provider)
    if previous:
        turn["unrelated_history"] = "无关旧查询"
    result = await queries.fetch({"domain": domain, "parameters": fields}, turn)
    assert result["source"]["provider"] == provider.source.provider and result["items"] == []
    raw = queries.evidence.get(result["query_id"], result["result_revision"])
    assert raw["source"] == provider.source.to_dict()
    assert provider.queries[0].to_dict() == {
        **fields,
        **({"passengers": 1} if domain == "train" else {}),
    }


@pytest.mark.parametrize("name,fields,required", CASES)
def test_missing_does_not_query(tmp_path, name, fields, required):
    provider = RecordingProvider()
    domain, queries, turn = service(tmp_path, name, provider)
    with pytest.raises(ValueError):
        queries.prepare(
            {"domain": domain, "parameters": {k: v for k, v in fields.items() if k != required}}
        )
    assert not provider.queries


@pytest.mark.parametrize("name,fields,required", CASES)
@pytest.mark.asyncio
async def test_unavailable_timeout_and_empty(tmp_path, name, fields, required):
    domain, queries, turn = service(tmp_path, name)
    unavailable = await queries.fetch({"domain": domain, "parameters": fields}, turn)
    assert unavailable["status"] == "unavailable" and not unavailable["items"]
    failed = RecordingProvider(fail=True)
    queries.providers[domain] = failed
    result = await queries.fetch({"domain": domain, "parameters": fields, "refresh": True}, turn)
    assert result["status"] == "error" and len(failed.queries) == 3
    queries.providers[domain] = RecordingProvider(empty=True)
    result = await queries.fetch({"domain": domain, "parameters": fields, "refresh": True}, turn)
    assert result["status"] == "ok" and result["items"] == []


def test_invalid_date_and_guest_count_do_not_query(tmp_path):
    provider = RecordingProvider()
    domain, queries, turn = service(tmp_path, "hotel_search", provider)
    for overrides in [{"check_out": "2026-10-19"}, {"guests": 0}, {"check_in": "tomorrow"}]:
        with pytest.raises(ValueError):
            queries.prepare({"domain": domain, "parameters": {**CASES[1][1], **overrides}})
    assert not provider.queries


@pytest.mark.asyncio
async def test_guide_without_dates_and_profile_not_used(tmp_path):
    provider = RecordingProvider()
    domain, queries, turn = service(tmp_path, "travel_guide", provider)
    result = await queries.fetch({"domain": domain, "parameters": {"destination": "北京"}}, turn)
    assert result["query"]["visit_dates"] == []
    with pytest.raises(ValueError):
        queries.prepare({"domain": "train", "parameters": {}})


@pytest.mark.asyncio
async def test_provider_failure_does_not_expose_exception_details(tmp_path):
    class FailingProvider:
        async def search(self, query):
            raise RuntimeError("secret-token")

    domain, queries, turn = service(tmp_path, "travel_guide", FailingProvider())
    result = await queries.fetch({"domain": domain, "parameters": {"destination": "北京"}}, turn)
    assert result["status"] == "error" and "secret-token" not in json.dumps(result)
