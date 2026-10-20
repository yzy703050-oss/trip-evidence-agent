"""External requests and evidence views for explicit native query calls."""

import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from trip_assistant.providers.arrival_search import search_by_arrival
from trip_assistant.providers.base import UnavailableProvider
from trip_assistant.providers.candidates import candidate_view
from trip_assistant.providers.contracts import ProviderQueryError
from trip_assistant.providers.hotel_places import make_hotel_place_query
from trip_assistant.providers.public_query import PublicQueryProvider
from trip_assistant.providers.queries import make_query
from trip_assistant.providers.validation import guard_domain_result
from trip_assistant.providers.views import build_tool_result_view, remember_view
from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.runtime.resilience import CircuitOpenError, call_with_retry


def query_key(domain, parameters):
    return domain + ":" + json.dumps(parameters, sort_keys=True, ensure_ascii=False, default=str)


class QueryService:
    def __init__(self, providers, evidence, scheduler, now=None):
        self.providers = providers
        self.evidence = evidence
        self.scheduler = scheduler
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.public = providers.get("public") or PublicQueryProvider()
        self.locks = {}
        self.refresh_generations = {}
        self.refresh_failures = {}

    def request_key(self, item):
        provider, parameters, _ = self.prepare(item)
        return self._request_key(item["domain"], provider, parameters)

    def _request_key(self, domain, provider, parameters):
        if domain == "hotel" and getattr(provider, "hotel_places", False):
            parameters = {k: parameters[k] for k in ("city", "keywords", "search_kind")}
        return query_key(
            domain,
            {
                "provider": type(provider).__module__ + "." + type(provider).__qualname__,
                "parameters": parameters,
            },
        )

    def prepare(self, item):
        domain = item["domain"]
        parameters = item["parameters"]
        provider = (
            self.providers.get(domain)
            or self.providers.get(domain + "_search")
            or UnavailableProvider()
        )
        if domain == "train" and parameters.get("arrival_date"):
            from trip_assistant.providers.arrival_search import arrival_parameters

            return provider, arrival_parameters(parameters), None
        if domain == "hotel" and getattr(provider, "hotel_places", False):
            query, missing = make_hotel_place_query(parameters)
        elif domain in {"train", "hotel", "guide"}:
            query, missing = make_query(domain, parameters)
        else:
            required = "city" if domain == "weather" else "query"
            if not isinstance(parameters.get(required), str) or not parameters[required].strip():
                raise ToolFailure(
                    "MISSING_QUERY_PARAMETERS",
                    "missing_query_parameter:" + required,
                    {"domain": domain, "fields": [required]},
                )
            return self.public, parameters, None
        if missing:
            raise ToolFailure(
                "MISSING_QUERY_PARAMETERS",
                "missing_query_parameters:" + ",".join(missing),
                {"domain": domain, "fields": missing},
            )
        return provider, query.to_dict(), query

    def expand_queries(self, args, turn):
        keys = []
        for item in args["query_plan"]:
            provider, p, query = self.prepare(item)
            if item["domain"] == "train" and p.get("arrival_date") and not p.get("departure_date"):
                target = date.fromisoformat(p["arrival_date"])
                keys.extend(
                    self.request_key(
                        {
                            "domain": "train",
                            "parameters": {
                                "origin": p["origin"],
                                "destination": p["destination"],
                                "departure_date": (target - timedelta(days=i)).isoformat(),
                                "passengers": p["passengers"],
                            },
                        }
                    )
                    for i in range(3)
                )
            else:
                keys.append(self._request_key(item["domain"], provider, p))
        return keys

    async def fetch(self, item, turn):
        prepared = self.prepare(item)
        key = self._request_key(item["domain"], *prepared[:2])
        observed = self.refresh_generations.get(key, 0)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if item.get("refresh") and self.refresh_generations.get(key, 0) != observed:
                # An overlapping refresh already finished. Re-project raw evidence for
                # this caller's constraints/ranking instead of sharing a bounded view.
                failure = self.refresh_failures.get(key)
                if failure is not None:
                    turn.setdefault("query_attempts", {})[key] = deepcopy(failure)
                    return deepcopy(failure)
                turn.setdefault("query_attempts", {}).pop(key, None)
                item = {**item, "refresh": False}
            elif item.get("refresh"):
                turn.setdefault("query_attempts", {}).pop(key, None)
            value = await self._fetch_safe(item, turn, prepared, key)
            if item.get("refresh"):
                self.refresh_failures[key] = deepcopy(turn.get("query_attempts", {}).get(key))
                self.refresh_generations[key] = observed + 1
            return value

    async def _fetch_safe(self, item, turn, prepared, key):
        attempts = turn.setdefault("query_attempts", {})
        if not item.get("refresh") and key in attempts:
            return deepcopy(attempts[key])
        try:
            return await self._fetch(item, turn, prepared, key)
        except ProviderQueryError as error:
            result = error.result.to_dict()
            result.update(domain=item["domain"], cache_hit=False)
        except (CircuitOpenError, ConnectionError, TimeoutError):
            result = {
                "status": "error",
                "domain": item["domain"],
                "query": item["parameters"],
                "items": [],
                "cache_hit": False,
                "reason": "供应商暂不可用、熔断或查询超时。",
            }

        except Exception as error:
            logging.getLogger(__name__).warning("Provider query failed: %s", type(error).__name__)
            result = {
                "status": "error",
                "domain": item["domain"],
                "items": [],
                "cache_hit": False,
                "reason": "查询失败，供应商未返回可核实结果。",
            }
        result.setdefault("query", prepared[1])
        attempts[key] = deepcopy(result)
        return result

    async def _fetch(self, item, turn, prepared, key):
        domain = item["domain"]
        provider, p, query = prepared
        attempts = turn.setdefault("query_attempts", {})
        value = (
            None
            if item.get("refresh")
            else self.evidence.find_valid(
                key, now=self.now(), max_age=self.scheduler.settings.evidence_max_age_seconds
            )
        )
        cache_hit = value is not None
        if value is None:

            async def checked(fetch, endpoint):
                async def request():
                    value = await fetch()
                    if value.status == "error":
                        raise ProviderQueryError(value)
                    return value

                return await call_with_retry(
                    request,
                    endpoint=endpoint,
                    budget=turn["budget"],
                    scheduler=self.scheduler,
                    clock=getattr(self, "retry_clock", None),
                )

            async def search(query):
                return await checked(
                    lambda: provider.search(query), type(provider).__name__ + ":" + domain
                )

            if domain == "train" and p.get("arrival_date"):
                result = await search_by_arrival(SimpleNamespace(search=search), p)
            elif domain == "weather":
                result = await checked(
                    lambda: self.public.weather(p["city"], p.get("date")), "public:weather"
                )
            elif domain == "web":
                result = await checked(lambda: self.public.web(p["query"]), "public:web")
            else:
                result = await search(query)
            raw = guard_domain_result(domain, result.to_dict())
            raw.update(domain=domain, query=p)
            if raw["status"] not in {"ok", "partial"}:
                raw["cache_hit"] = False
                attempts[key] = deepcopy(raw)
                return raw
            value = self.evidence.save(raw, key)
        constraints = {
            k: v
            for k, v in item.get("constraints", {}).items()
            if item.get("condition_strengths", {}).get(k, "hard") == "hard"
        }
        view = candidate_view(value, constraints=constraints, ranking=item.get("ranking", []))
        view = build_tool_result_view(
            domain,
            view,
            limits={"candidate_limit": 5, "already_paged": True},
            requirements=[r["field"] for r in item.get("ranking", [])],
        )
        view["data_quality"] = view.get("status", "unknown")
        if (
            view.get("status") == "partial"
            and view.get("items")
            and not view.get("unverified_constraints")
        ):
            view["status"] = "ok"
        view["cache_hit"] = cache_hit
        view["constraints"] = constraints
        view["ranking"] = item.get("ranking", [])
        remember_view(turn, view)
        from trip_assistant.runtime.logging import log_event
        from trip_assistant.runtime.telemetry import current_model_context

        event = {
            "type": "query_result",
            "domain": domain,
            "query_id": view["query_id"],
            "result_revision": view["result_revision"],
            "cache_hit": cache_hit,
            "status": view["status"],
            "candidate_total": view.get("candidate_total", 0),
            "returned_count": len(view["items"]),
            "turn_id": turn.get("turn_id"),
            **current_model_context(),
        }
        record = getattr(self.scheduler, "record", None)
        if record:
            record(event)
        log_event(
            logging.getLogger(__name__),
            "query_result",
            **{k: v for k, v in event.items() if k != "type"},
        )
        return view

    async def handler(self, args, turn):
        self.expand_queries(args, turn)
        values = await asyncio.gather(*(self.fetch(item, turn) for item in args["query_plan"]))
        return {
            "status": "ok" if all(v["status"] == "ok" for v in values) else "partial",
            "data": values,
        }
