"""One native-runtime evaluation entry; offline by default, live model only when explicit."""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from evals.latency import summarize_latency
from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.settings import Settings, generation_options

CASES = ("query_price", "default_date", "multi_route_unspecified_stay")


def tool(name, args):
    identity = name + str(perf_counter())
    content = [{"type": "tool_use", "id": identity, "name": name, "input": args}]
    return SimpleNamespace(content=content)


class ReplayModel:
    """Scripted model decisions for deterministic evaluation; never a production fallback."""

    def __init__(self, case, day):
        self.case = case
        self.day = day
        self.phase = 0

    async def __call__(self, messages, **kwargs):
        self.phase += 1
        if self.phase == 1:
            if self.case == "query_price":
                return tool(
                    "query",
                    {
                        "request_kind": "lookup",
                        "query_plan": [
                            {
                                "domain": "train",
                                "parameters": {
                                    "origin": "北京",
                                    "destination": "上海",
                                    "departure_date": self.day.isoformat(),
                                },
                            }
                        ],
                    },
                )
            cities = ["北京", "上海", *(["杭州"] if self.case.startswith("multi") else []), "北京"]
            tasks = []
            for i, (origin, destination) in enumerate(zip(cities, cities[1:], strict=False)):
                day = self.day + timedelta(days=i * 2)
                stay = i < len(cities) - 2
                conditions = {"departure_date": day.isoformat()}
                if stay:
                    conditions.update(
                        check_in=day.isoformat(),
                        check_out=(day + timedelta(days=2)).isoformat(),
                        nights=2,
                    )
                tasks.append(
                    {
                        "origin": origin,
                        "destination": destination,
                        "requires_hotel": stay,
                        "conditions": conditions,
                        "items": [
                            {"domain": "train", "parameters": {}},
                            *([{"domain": "hotel", "parameters": {}}] if stay else []),
                        ],
                    }
                )
            return tool("write_trip", {"request_kind": "planning", "tripplan": {"tasks": tasks}})
        value = json.loads(messages[-1]["content"])
        if self.case == "query_price":
            row = value["query_results"][0]["data"][0]["items"][0]
            return f"候选{row['train_number']}，单人票价{row['price_cny']}元；模拟数据，未预订。"
        value = value.get("tripplan", value)
        if self.phase == 2:
            tasks = []
            for task in value["tasks"]:
                refs = {
                    v["domain"]: {
                        "query_id": v["query_id"],
                        "result_revision": v["result_revision"],
                        "candidate_id": v["items"][0]["id"],
                    }
                    for v in value["candidates"]
                    if v["query_id"].startswith(task["id"] + ":") and v["items"]
                }
                tasks.append({"task_id": task["id"], "selection_refs": refs})
            return tool(
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "回放模型确认实际选择满足测试请求。",
                    },
                    "tripplan": {
                        "operation": "select",
                        "trip_plan_id": value["trip_plan_id"],
                        "expected_revision": value["revision"],
                        "tasks": tasks,
                    },
                },
            )
        parts = ["模拟行程已保存。"]
        for plan in value["validation"]["reconstructed_tasks"]:
            train = plan.get("train") or {}
            hotel = plan.get("hotel") or {}
            if train:
                parts.append(
                    " ".join(
                        str(train[k])
                        for k in ("train_number", "seat_class", "departure_at", "arrival_at")
                    )
                    + "，"
                    + str(train["price_cny"])
                    + "元。"
                )
            if hotel:
                parts.append(
                    hotel["hotel_name"]
                    + "，入住"
                    + plan["schedule"]["check_in"]
                    + "，离店"
                    + plan["schedule"]["check_out"]
                    + "。"
                )
        return "\n".join(parts) + "\n酒店仅为地点参考，房价、空房和退改规则未知；未预订。"


async def run_case(case, args):
    settings = Settings(single_pass_planning=False)
    day = date.today() + timedelta(days=3)
    model = ReplayModel(case, day)
    if args.live_model:
        model = OpenAIChatModel(
            settings.llm_model,
            api_key=settings.llm_api_key or settings.deepseek_api_key,
            client_kwargs={"base_url": settings.llm_base_url, "timeout": settings.request_timeout},
            generate_kwargs=generation_options(
                settings.llm_model,
                max_tokens=settings.llm_max_tokens,
                thinking=None if args.thinking == "disabled" else args.thinking,
            ),
        )
    app = create_application(
        settings,
        model=model,
        providers={"train": SimulatedTrainProvider(), "hotel": SimulatedHotelProvider()},
        storage_root=args.output / "memory",
        user_id="native-eval",
        session_id=case + "-" + str(perf_counter()),
    )
    query = (
        f"查询北京到上海{day.isoformat()}的火车票"
        if case == "query_price"
        else f"从北京{day.isoformat()}出发去上海"
        + ("、杭州" if case.startswith("multi") else "")
        + "游玩，每站住两晚后返北京，只安排火车和酒店。"
    )
    started = perf_counter()
    result = await app.run(query)
    records = app.sessions.read_runs()
    calls = [r for r in records if r["type"] == "model_call"]
    attempts = [r for r in records if r["type"] == "external_attempt"]
    trip = app.trips.load(result["trip"]["trip_plan_id"]) if result.get("trip") else None
    passed = result["status"] == "completed" and (
        bool(trip and trip.get("validation", {}).get("valid"))
        if case != "query_price"
        else bool(result["tool_results"])
    )
    return {
        "case": case,
        "status": result["status"],
        "data_mode": "simulation",
        "assessment": {"passed": passed},
        "result": result,
        "workflow": trip,
        "latency": summarize_latency(
            calls, [], total_ms=(perf_counter() - started) * 1000, external_requests=len(attempts)
        ),
    }


async def evaluate(args):
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in args.cases:
        print("START " + case, flush=True)
        try:
            rows.append(await run_case(case, args))
        except Exception as error:
            rows.append(
                {
                    "case": case,
                    "status": "error",
                    "assessment": {"passed": False},
                    "error_type": type(error).__name__,
                }
            )
    (args.output / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return 0 if all(r["assessment"]["passed"] for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--thinking", choices=["disabled", "low", "medium", "high"], default="disabled"
    )
    parser.add_argument(
        "--live-model", action="store_true", help="明确使用配置中的真实模型；供应商仍为显式模拟数据"
    )
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))


if __name__ == "__main__":
    main()
