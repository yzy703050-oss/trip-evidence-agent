"""Real model, deterministic suppliers; outcome checks never steer model decisions."""

import argparse
import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from evals.simulated_travel import SimulatedHighSpeedProvider, SimulatedHotelProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.settings import Settings, generation_options
from trip_assistant.tools.policy import build_policy_handler
from trip_assistant.trips.query_execution import TripQueryExecutor

CASES = {
    "flexible_tourism": "后天想在上海从上午开始玩，帮我安排从重庆过去的旅游行程。",
    "business_policy_ok": "后天上午在上海开会，帮我从重庆安排交通和酒店。只安排我提到的会议。",
    "business_policy_missing": "后天上午在上海开会，帮我从重庆安排交通和酒店。只安排我提到的会议。",
    "hard_conflict": "只能后天从重庆出发去上海出差，不能提前出发，必须后天上午在上海开会，"
    "不许更改会议时间，安排交通和住宿。",
    "mixed_high_speed": "我想查一下上海如家的酒店，再安排一下后天从重庆去上海的旅游行程。",
    "mixed_early_return": "我想查一下上海如家的酒店，再安排一下后天从重庆去上海的旅游行程。",
    "content_amend": "把这次上海行程已建议的散步改成同日参观博物馆，"
    "其他安排保持原样，保存后告诉我。",
    "multi_city": "后天从重庆出发去上海玩一天，然后去杭州玩一天，最后返回重庆。"
    "每段交通都安排高铁，上海和杭州各住一晚，别漏掉上海到杭州这一段。",
    "return_extension": "把这次上海行程的返程顺延一天，需要的住宿也一起调整，"
    "其他安排保持原样并保存。",
}


class FixtureTrain(SimulatedHighSpeedProvider):
    def __init__(self, *, early_return=False, route_durations=None):
        super().__init__(duration_minutes=420, departure_hours=[7, 8, 9, 10, 11, 12, 14, 16])
        self.early_return = early_return
        self.route_durations = route_durations or {}

    async def search(self, query):
        result = await super().search(query)
        for item in result.items:
            departure = datetime.fromisoformat(item["departure_at"]) + timedelta(minutes=20)
            arrival = datetime.fromisoformat(item["arrival_at"]) + timedelta(minutes=20)
            if duration := self.route_durations.get((query.origin, query.destination)):
                arrival = departure + timedelta(minutes=duration)
            item.update(
                departure_at=departure.isoformat(),
                arrival_at=arrival.isoformat(),
                departure_time=departure.strftime("%H:%M"),
                arrival_time=arrival.strftime("%H:%M"),
            )
        if self.early_return and query.destination == "重庆":
            result.items[:] = result.items[:1]
        return result


class FixturePolicy:
    def __init__(self, missing=False):
        self.missing = missing

    async def query(self, context):
        return {
            "status": "no_knowledge" if self.missing else "ok",
            "summary": "模拟政策库无适用文件。"
            if self.missing
            else "模拟适用政策：允许二等座；住宿不设金额上限。",
            "constraints": []
            if self.missing
            else [
                {
                    "id": "fixture-seat-class",
                    "field": "seat_class",
                    "value": "二等座",
                    "hard": True,
                    "evidence_ids": ["fixture-policy"],
                    "rule": "火车二等座",
                    "applicability": {},
                }
            ],
            "references": []
            if self.missing
            else [{"id": "fixture-policy", "title": "模拟差旅政策"}],
            "missing_fields": ["policy"] if self.missing else [],
            "conflicts": [],
            "applicability": {},
        }


def assess(record):
    """Check this benchmark's explicit invariants; prose review remains separate."""
    failures = []
    result = record.get("result", {})
    trips = record.get("trips", [])
    case = record["case"]
    day = (
        date.fromisoformat(record["activity_date"])
        if record.get("activity_date")
        else (datetime.fromisoformat(record["fixed_current_time"]) + timedelta(days=1)).date()
    )
    if record.get("exception"):
        failures.append("execution_exception")
    if not result.get("final_answer"):
        failures.append("missing_answer")
    if len(trips) != 1:
        failures.append("expected_one_trip")
        return {"outcome": "failed", "failures": failures, "semantic_review": "pending"}
    work = trips[0]
    validation = work.get("validation") or {}
    blocking = {i["code"] for i in validation.get("issues", []) if i.get("blocking")}
    plans = validation.get("reconstructed_tasks", [])
    if case == "multi_city":
        tasks = work.get("tasks", [])
        actual = [(t.get("origin"), t.get("destination")) for t in tasks]
        if actual != [("重庆", "上海"), ("上海", "杭州"), ("杭州", "重庆")]:
            failures.append("missing_required_leg")
        for city in ("上海", "杭州"):
            if not any(t.get("destination") == city and t.get("content") for t in tasks):
                failures.append("missing_city_activity")
            if not any(t.get("destination") == city and t.get("requires_hotel") for t in tasks):
                failures.append("missing_city_stay")
    if case == "return_extension":
        before = record.get("seed", {}).get("tasks", [])
        tasks = work.get("tasks", [])
        original_date = (
            (before[-1].get("conditions") or {}).get("departure_date") if before else None
        )
        target_date = (
            (date.fromisoformat(original_date) + timedelta(days=1)).isoformat()
            if original_date
            else None
        )
        if (
            not target_date
            or not tasks
            or tasks[-1].get("conditions", {}).get("departure_date") != target_date
        ):
            failures.append("return_not_extended")
        if before and tasks and before[0].get("content") != tasks[0].get("content"):
            failures.append("unrelated_activity_changed")
    if case == "hard_conflict":
        tasks = work.get("tasks", [])
        if result.get("status") != "partial" or work.get("committed"):
            failures.append("hard_conflict_claimed_complete")
        if not tasks or tasks[0].get("conditions", {}).get("departure_date") != day.isoformat():
            failures.append("hard_departure_changed")
        if not any(
            a.get("date") == day.isoformat()
            and a.get("time") == "上午"
            and a.get("strength") == "hard"
            for t in tasks
            for a in t.get("content", [])
        ):
            failures.append("hard_meeting_changed")
    else:
        expected_partial = case == "business_policy_missing"
        if expected_partial:
            policy_blocking = blocking
            if (validation.get("goal_assessment") or {}).get("status") in {
                "partial",
                "unmet",
                "unknown",
            }:
                policy_blocking = blocking - {"goal_not_satisfied"}
            if (
                result.get("status") != "partial"
                or not policy_blocking
                or any(not code.startswith("policy_") for code in policy_blocking)
                or work.get("committed")
            ):
                failures.append("policy_draft_not_ready")
        elif result.get("status") != "completed" or not work.get("committed") or blocking:
            failures.append("not_committed_complete")
        if len(plans) < 2 or any(not p.get("train") for p in plans):
            failures.append("missing_train_selection")
        for previous, following in zip(plans, plans[1:], strict=False):
            checkout = previous.get("schedule", {}).get("check_out")
            departure = (following.get("train") or {}).get("departure_at")
            if previous.get("hotel") and checkout and departure and checkout < departure[:10]:
                failures.append("overnight_stay_uncovered")
            # Benchmark quality check only; never routes or blocks product planning.
            if departure and case in {"flexible_tourism", "mixed_high_speed", "mixed_early_return"}:
                leaving = datetime.fromisoformat(departure)
                if leaving.hour < 8 and any(
                    activity.get("date") == leaving.date().isoformat()
                    and activity.get("time") == "上午"
                    for activity in previous.get("content", [])
                ):
                    failures.append("early_return_activity_conflict")
        if case in {"flexible_tourism", "mixed_high_speed", "mixed_early_return"} and not any(
            p.get("content") for p in plans
        ):
            failures.append("missing_tourism_activity")
        for p in plans:
            departure = (p.get("train") or {}).get("departure_at")
            if departure and datetime.fromisoformat(departure) < datetime.fromisoformat(
                record["fixed_current_time"]
            ):
                failures.append("selected_departure_in_past")
        if case in {"flexible_tourism", "business_policy_ok", "business_policy_missing"}:
            first = plans[0] if plans else {}
            schedule = first.get("schedule", {})
            arrival = schedule.get("arrival_at")
            if not arrival or datetime.fromisoformat(arrival).date() >= day:
                failures.append("not_arrived_previous_day")
            check_in = schedule.get("check_in")
            if not first.get("hotel") or not check_in or (arrival and check_in > arrival[:10]):
                failures.append("arrival_night_uncovered")
            if case.startswith("business"):
                if not any(
                    a.get("date") == day.isoformat()
                    and a.get("time") == "上午"
                    and a.get("strength") == "hard"
                    for a in first.get("content", [])
                ):
                    failures.append("hard_meeting_changed")
                for p in plans[1:]:
                    departure = (p.get("train") or {}).get("departure_at")
                    if departure and datetime.fromisoformat(departure) < datetime.combine(
                        day, datetime.min.time(), ZoneInfo("Asia/Shanghai")
                    ) + timedelta(hours=12):
                        failures.append("return_before_morning_activity_ends")
        if case == "content_amend":
            seeded = record.get("seed", {})
            old = seeded.get("committed", {})
            current = work.get("committed") or {}
            if current.get("revision", 0) <= old.get("revision", 0):
                failures.append("amend_not_committed")
            old_tasks, tasks = old.get("tasks", []), current.get("tasks", [])
            if len(old_tasks) != len(tasks) or not tasks:
                failures.append("amend_tasks_changed")
            else:
                for before, after in zip(old_tasks, tasks, strict=True):
                    for key in ("id", "origin", "destination", "conditions", "draft_plan"):
                        if before.get(key) != after.get(key):
                            failures.append("amend_changed_" + key)
                previous = old_tasks[0].get("content", [])
                updated = tasks[0].get("content", [])
                if not updated or not previous or updated[0].get("date") != previous[0].get("date"):
                    failures.append("amend_activity_date_changed")
                elif "博物馆" not in updated[0].get("description", ""):
                    failures.append("amend_activity_not_updated")
    outcome = (
        "failed"
        if failures
        else (
            "expected_partial"
            if case in {"hard_conflict", "business_policy_missing"}
            else "complete"
        )
    )
    return {"outcome": outcome, "failures": failures, "semantic_review": "pending"}


def save(path, value, secrets=()):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    path.write_text(text, encoding="utf-8")


class ObservedModel:
    def __init__(self, sdk, path, secrets):
        self.sdk, self.path, self.secrets = sdk, path, secrets
        self.rows = []

    async def __call__(self, messages, **kwargs):
        from trip_assistant.runtime.telemetry import current_model_context

        row = {
            "messages": deepcopy(messages),
            "tools": deepcopy(kwargs),
            "context": current_model_context(),
        }
        started = perf_counter()
        try:
            response = await self.sdk(messages, **kwargs)
            row.update(content=response.content, usage=response.usage)
            row["diagnostics"] = getattr(response, "diagnostics", None)
            return response
        except Exception as error:
            row["error"] = {"type": type(error).__name__, "message": str(error)}
            row["error"]["finish_reason"] = getattr(error, "finish_reason", None)
            row["usage"] = getattr(error, "usage", None)
            row["diagnostics"] = getattr(error, "diagnostics", None)
            raise
        finally:
            row["elapsed_seconds"] = round(perf_counter() - started, 3)
            self.rows.append(row)
            save(self.path, self.rows, self.secrets)


async def seed_content(app, now):
    today = now.date() + timedelta(days=1)
    turn = {
        "operation_id": "fixture:propose",
        "original_query": "保存重庆上海往返及散步建议",
        "budget": TurnBudget(app.settings),
        "session_id": app.sessions.session_id,
    }
    first_day, back_day = today.isoformat(), (today + timedelta(days=2)).isoformat()
    created = await app.trip_service.apply(
        {
            "operation": "propose",
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "purpose": "visit",
                    "requires_hotel": True,
                    "conditions": {
                        "departure_date": first_day,
                        "check_in": first_day,
                        "check_out": back_day,
                        "nights": 2,
                    },
                    "content": [
                        {
                            "date": (today + timedelta(days=1)).isoformat(),
                            "time": "下午",
                            "purpose": "散步",
                            "description": "沿江散步",
                            "source": "proposal",
                            "strength": "soft",
                        }
                    ],
                    "items": [{"domain": "train"}, {"domain": "hotel"}],
                },
                {
                    "origin": "上海",
                    "destination": "重庆",
                    "purpose": "return",
                    "conditions": {"departure_date": back_day},
                    "items": [{"domain": "train"}],
                },
            ],
        },
        turn,
    )
    queried = await TripQueryExecutor(app.trip_service).run(
        created["trip_plan_id"], created["revision"], {**turn, "operation_id": "fixture:query"}
    )
    trip = queried["trip"]
    selected = []
    for task in trip["tasks"]:
        refs = {
            view["domain"]: {
                "query_id": view["query_id"],
                "result_revision": view["result_revision"],
                "candidate_id": view["items"][0]["id"],
            }
            for view in trip["candidates"]
            if view["task_id"] == task["id"]
        }
        selected.append({"task_id": task["id"], "selection_refs": refs})
    saved = await app.trip_service.apply(
        {
            "operation": "select",
            "trip_plan_id": trip["trip_plan_id"],
            "expected_revision": trip["revision"],
            "tasks": selected,
        },
        {**turn, "operation_id": "fixture:select"},
    )
    if saved["status"] != "completed":
        raise ValueError("seed fixture did not commit")
    return app.trips.load(saved["trip_plan_id"])


async def run_case(case, root, settings, now, *, thinking=None, isolate_public=False):
    thinking = "disabled"  # Legacy flags cannot enable thinking in this runner.
    root.mkdir(parents=True)
    secrets = (
        settings.llm_api_key,
        settings.deepseek_api_key,
        settings.amap_api_key,
        settings.juhe_train_api_key,
    )
    sdk = OpenAIChatModel(
        settings.llm_model,
        api_key=settings.llm_api_key or settings.deepseek_api_key,
        client_kwargs={"base_url": settings.llm_base_url, "timeout": settings.request_timeout},
        generate_kwargs=generation_options(
            settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            thinking=None,
        ),
    )
    model = ObservedModel(sdk, root / "model-trace.json", secrets)
    train = FixtureTrain(
        early_return=case == "mixed_early_return",
        route_durations={("上海", "杭州"): 80, ("杭州", "重庆"): 480}
        if case == "multi_city"
        else None,
    )
    hotel = SimulatedHotelProvider()
    app = create_application(
        settings,
        model=model,
        providers={"train": train, "hotel": hotel},
        storage_root=root / "storage",
        user_id="planning-reliability",
        session_id=case,
    )
    app.preferences.set_user_preference("home_location", "重庆")
    if isolate_public:
        from trip_assistant.providers.public_query import PublicQueryProvider

        def unavailable(_query):
            raise ValueError("本次评测没有公共查询fixture，不能核实外部网页或天气信息")

        app.queries.public = PublicQueryProvider(weather_fetch=unavailable, web_fetch=unavailable)
    app.registry["query_policy"] = replace(
        app.registry["query_policy"],
        handler=build_policy_handler(
            FixturePolicy(missing=case == "business_policy_missing"), app.trips
        ),
    )
    refresh = app.refresh_memory_context
    record = {
        "case": case,
        "query": CASES[case],
        "fixed_current_time": now.isoformat(),
        "activity_date": (now + timedelta(days=2)).date().isoformat(),
        "model": settings.llm_model,
        "planning_rounds": settings.planning_rounds,
        "single_pass_planning": settings.single_pass_planning,
        "rail_fixture": {
            "kind": "high_speed",
            "duration_minutes": train.duration_minutes,
            "real_timetable": False,
            "early_return_stress": train.early_return,
            "route_duration_overrides": {"→".join(k): v for k, v in train.route_durations.items()},
        },
        "max_output_tokens": settings.llm_max_tokens,
        "thinking": thinking,
        "temperature": generation_options(settings.llm_model)["temperature"],
        "request_timeout": settings.request_timeout,
        "public_query_mode": "isolated_unavailable_fixture" if isolate_public else "default",
        "mode": "real_model_simulated_suppliers_and_policy",
    }
    started = perf_counter()
    print("START", root.name, case, flush=True)
    try:
        if case in {"content_amend", "return_extension"}:
            record["seed"] = await seed_content(app, now)

        def fixed_context(context):
            context["current_time"] = now.isoformat()
            if record.get("seed"):
                context["review_trip_plan_id"] = record["seed"]["id"]
            refresh(context)

        app.refresh_memory_context = fixed_context
        record["result"] = await app.run(CASES[case])
    except Exception as error:
        record["exception"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        record["trips"] = [app.trips.load(t["id"]) for t in app.trips.list_summaries()]
        record["events"] = app.sessions.read_events()
        record["runs"] = app.sessions.read_runs()
        record["train_requests"] = train.calls
        record["hotel_requests"] = hotel.calls
        usage = [r.get("usage") for r in model.rows]
        record["metrics"] = {
            key: sum(u[key] for u in usage)
            if usage and all(u and u.get(key) is not None for u in usage)
            else None
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
            )
        }
        record["metrics"].update(
            model_calls=len(model.rows), elapsed_seconds=round(perf_counter() - started, 3)
        )
        record["assessment"] = assess(record)
        save(root / "result.json", record, secrets)
        await sdk.client.close()
    print("DONE", case, record["assessment"], record["metrics"], flush=True)
    return {
        key: record[key]
        for key in (
            "case",
            "assessment",
            "metrics",
            "model",
            "planning_rounds",
            "max_output_tokens",
        )
    } | {"path": str(root), "final_answer": record.get("result", {}).get("final_answer")}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--planning-rounds", type=int)
    parser.add_argument("--thinking", choices=["disabled", "low", "medium", "high"])
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--isolate-public", action="store_true")
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=CASES,
        default=[case for case in CASES if case not in {"hard_conflict", "mixed_early_return"}],
    )
    args = parser.parse_args()
    if args.concurrency < 1 or args.repeats < 1:
        parser.error("concurrency and repeats must be positive")
    settings = Settings()
    if args.planning_rounds is not None:
        settings = Settings(**{**settings.model_dump(), "planning_rounds": args.planning_rounds})
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    root = (Path("docs/evals") / (now.strftime("%Y-%m-%d") + "-planning-reliability")) / (
        args.label + "-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    root.mkdir(parents=True)
    summaries = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one_case(repeat, case):
        async with semaphore:
            summaries.append(
                await run_case(
                    case,
                    root / f"{repeat + 1:02d}-{case}",
                    settings,
                    now,
                    thinking=args.thinking,
                    isolate_public=args.isolate_public,
                )
            )
            save(root / "summary.json", summaries)

    await asyncio.gather(
        *(one_case(repeat, case) for repeat in range(args.repeats) for case in args.cases)
    )
    print("REPORT", root, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
