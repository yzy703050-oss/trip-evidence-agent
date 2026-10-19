"""Replay the explicit one-decision experiment with real LLM and simulated suppliers."""

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from evals.manual.planning_reliability import (
    FixtureTrain,
    ObservedModel,
    save,
    seed_content,
)
from evals.simulated_travel import SimulatedHighSpeedProvider, SimulatedHotelProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.providers.public_query import PublicQueryProvider
from trip_assistant.settings import Settings, generation_options

CASES = {
    "morning_tourism": "后天上午想开始在上海玩，帮我安排从重庆过去的旅游行程。",
    "departure_tourism": "查上海如家酒店，再安排后天从重庆出发去上海的旅游行程。",
    "one_day": "后天从杭州到上海，只玩一天，当天坐火车往返，不住宿。帮我安排游玩。",
    "business_missing": "后天上午在上海开会，从重庆安排交通和住宿，只安排这场会议。",
    "hard_conflict": "只能后天从重庆出发去上海，不能提前，必须当天上午在上海开会，"
    "会议不能改，安排交通住宿。",
    "amend": "把这次上海行程的下午散步改成同日下午参观博物馆，其他安排保持原样。",
}


def assess(record):
    """Execution invariants only; semantic plausibility requires a separate human review."""
    failures = []
    result, metrics = record.get("result", {}), record.get("metrics", {})
    if metrics.get("logical_model_calls") != 1:
        failures.append("not_one_decision")
    if not result.get("final_answer") or record.get("exception"):
        failures.append("missing_result")
    if not (result.get("execution_facts") or {}).get("draft_saved"):
        failures.append("plan_not_saved")
    trips = record.get("trips", [])
    day = record.get("activity_date")
    if not trips:
        failures.append("no_plan")
    if record["case"] in {"morning_tourism", "departure_tourism", "one_day"}:
        if not any(t.get("content") for p in trips for t in p.get("tasks", [])):
            failures.append("no_tourism_activity")
    tasks = [t for p in trips for t in p.get("tasks", [])]
    if record["case"] in {"hard_conflict", "business_missing"}:
        if not any(
            a.get("date") == day and a.get("time") == "上午" and a.get("strength") == "hard"
            for t in tasks
            for a in t.get("content", [])
        ):
            failures.append("hard_meeting_changed")
    if record["case"] in {"hard_conflict", "departure_tourism", "one_day"}:
        if not tasks or tasks[0].get("conditions", {}).get("departure_date") != day:
            failures.append("explicit_departure_changed")
    if record["case"] == "one_day":
        if any(
            t.get("requires_hotel") or t.get("conditions", {}).get("departure_date") != day
            for t in tasks
        ):
            failures.append("one_day_limit_changed")
    return {"passed": not failures, "failures": failures, "semantic_review": "pending"}


async def run_case(case, root, settings, now):
    root.mkdir(parents=True)
    secrets = (settings.llm_api_key, settings.deepseek_api_key)
    sdk = OpenAIChatModel(
        settings.llm_model,
        api_key=settings.llm_api_key or settings.deepseek_api_key,
        client_kwargs={"base_url": settings.llm_base_url, "timeout": settings.request_timeout},
        generate_kwargs=generation_options(settings.llm_model, thinking=None),
    )
    observed = ObservedModel(sdk, root / "model-trace.json", secrets)
    train = (
        SimulatedHighSpeedProvider(duration_minutes=60, departure_hours=[8, 18])
        if case == "one_day"
        else FixtureTrain()
    )
    hotel = SimulatedHotelProvider()
    app = create_application(
        settings,
        model=observed,
        storage_root=root / "storage",
        providers={"train": train, "hotel": hotel},
        user_id="single-pass",
        session_id=case,
    )
    app.preferences.set_user_preference("home_location", "重庆")

    def unavailable(_query):
        raise ValueError("本次评测无公共查询数据，不请求外部网站")

    app.queries.public = PublicQueryProvider(weather_fetch=unavailable, web_fetch=unavailable)
    record = {
        "case": case,
        "query": CASES[case],
        "current_time": now.isoformat(),
        "activity_date": (now + timedelta(days=2)).date().isoformat(),
        "mode": "single_pass_real_model_simulated_suppliers",
        "thinking": "disabled",
    }
    refresh = app.refresh_memory_context
    started = perf_counter()
    try:
        if case == "amend":
            record["seed"] = await seed_content(app, now)

        def context(turn):
            turn["current_time"] = now.isoformat()
            if record.get("seed"):
                turn["review_trip_plan_id"] = record["seed"]["id"]
            refresh(turn)

        app.refresh_memory_context = context
        record["result"] = await app.run(CASES[case])
    except Exception as error:
        record["exception"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        record["trips"] = [app.trips.load(r["id"]) for r in app.trips.list_summaries()]
        record["runs"] = app.sessions.read_runs()
        record["events"] = app.sessions.read_events()
        calls = [r for r in record["runs"] if r["type"] == "model_call"]
        record["metrics"] = {
            key: sum(r[key] for r in calls)
            if calls and all(r.get(key) is not None for r in calls)
            else None
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        }
        record["metrics"].update(
            api_attempts=len(calls),
            logical_model_calls=len({r.get("logical_call_id") for r in calls}),
            elapsed_seconds=round(perf_counter() - started, 3),
        )
        record["assessment"] = assess(record)
        save(root / "result.json", record, secrets)
        await sdk.client.close()
    print(case, json.dumps(record["metrics"]), record["assessment"], flush=True)
    return {
        "case": case,
        "path": str(root),
        "metrics": record["metrics"],
        "assessment": record["assessment"],
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", choices=CASES, nargs="+", default=list(CASES))
    args = parser.parse_args()
    if not args.live_model:
        parser.error("Use --live-model explicitly; offline assertions live in test_single_pass.py")
    args.output.mkdir(parents=True, exist_ok=False)
    settings = Settings(single_pass_planning=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    rows = []
    for case in args.cases:
        rows.append(await run_case(case, args.output / case, settings, now))
        save(args.output / "summary.json", rows)


if __name__ == "__main__":
    asyncio.run(main())
