"""Opt-in live model evaluation; no train/hotel requests or user-memory writes."""

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def cases():
    trip = dict(
        id="workflow_example_one",
        revision=4,
        status="completed",
        original_query="10月18日重庆去上海，安排火车酒店",
        created_at="2026-10-10T09:00:00+08:00",
        updated_at="2026-10-10T09:30:00+08:00",
        confirmed_conditions={},
        field_sources={},
        tasks=[
            dict(
                id="task_example",
                origin="重庆",
                destination="上海",
                requires_hotel=True,
                conditions=dict(
                    departure_date="2026-10-18", check_in="2026-10-18", check_out="2026-10-20"
                ),
                field_sources=dict(departure_date="user", check_in="user", check_out="user"),
            )
        ],
    )
    context = dict(
        original_query=trip["original_query"],
        current_time="2026-10-11T10:00:00+08:00",
        proposal=deepcopy(trip),
        candidates=[trip],
    )
    values = []

    def add(name, query, expected, *, date=None, extra=None, refresh=None):
        c = deepcopy(context)
        c["original_query"] = query
        if date:
            c["proposal"]["tasks"][0]["conditions"]["departure_date"] = date
        if extra:
            extra(c)
        want = {"action": expected}
        if expected == "reuse":
            want["workflow_id"] = "workflow_example_one"
        if refresh is not None:
            want["refresh"] = refresh
        values.append((name, c, want))

    add("same_trip", context["original_query"], "reuse")
    add("different_date", "10月25日重庆去上海，安排火车酒店", "new", date="2026-10-25")
    add("relative_date_changed", "明天重庆去上海，安排火车酒店", "new", date="2026-10-12")
    add("explicit_new_copy", "10月18日重庆去上海，另外新建一份对比方案", "new")
    add(
        "missing_date",
        "重庆去上海，安排火车酒店",
        "clarify",
        extra=lambda c: c["proposal"]["tasks"][0].update(conditions={}, field_sources={}),
    )

    def duplicate(c):
        other = deepcopy(c["candidates"][0])
        other["id"] = "workflow_example_two"
        c["candidates"].append(other)

    add("duplicate_candidates", context["original_query"], "clarify", extra=duplicate)
    add("latest_data", "10月18日重庆去上海，重新查询最新火车酒店", "reuse", refresh=True)

    def past(c):
        c["candidates"][0]["original_query"] = "10月1日重庆去上海，安排火车酒店"
        c["candidates"][0]["created_at"] = "2026-09-29T09:00:00+08:00"
        c["candidates"][0]["updated_at"] = "2026-09-29T09:30:00+08:00"
        c["candidates"][0]["tasks"][0]["conditions"].update(
            departure_date="2026-10-01", check_in="2026-10-01", check_out="2026-10-03"
        )

    add("past_trip", context["original_query"], "new", extra=past)

    def different_route(c):
        c["proposal"]["tasks"][0]["destination"] = "北京"

    add("different_destination", "10月18日重庆去北京，安排火车酒店", "new", extra=different_route)

    def numbered(c):
        duplicate(c)
        c["pending_workflow_match"] = {
            "question": "第一份或第二份，要哪一份？",
            "candidates": c["candidates"],
        }

    add("numbered_selection", "第一份", "reuse", extra=numbered)
    return values


async def evaluate(args):
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    from agentscope.model import OpenAIChatModel

    from agents.main_agent import MainAgent
    from config import LLM_CONFIG, get_model_generate_kwargs

    if not LLM_CONFIG["api_key"]:
        raise SystemExit("No model API key configured; live evaluation was not run.")
    model = OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        stream=True,
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 60},
        generate_kwargs=get_model_generate_kwargs(),
    )
    main = MainAgent(model)
    semaphore = asyncio.Semaphore(2)

    async def run(name, context, expected):
        async with semaphore:
            try:
                result = await asyncio.wait_for(main.match_workflow(context), timeout=75)
                passed = all(result.get(k) == v for k, v in expected.items())
                return dict(case=name, expected=expected, actual=result, passed=passed)
            except Exception as exc:
                return dict(case=name, expected=expected, error=type(exc).__name__, passed=False)

    results = await asyncio.gather(*(run(*case) for case in cases()))
    report = dict(
        model=LLM_CONFIG["model_name"],
        passed=sum(r["passed"] for r in results),
        total=len(results),
        results=results,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/evals/workflow-matching.json"))
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))
