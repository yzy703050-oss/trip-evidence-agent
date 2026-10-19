"""Exercise real policy extraction and real model handoff with fixture evidence only."""

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from evals.manual.planning_reliability import (
    CASES,
    FixturePolicy,
    FixtureTrain,
    ObservedModel,
    assess,
    save,
    seed_content,
)
from evals.simulated_travel import SimulatedHotelProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.providers.public_query import PublicQueryProvider
from trip_assistant.settings import Settings, generation_options
from trip_assistant.tools.policy import build_policy_handler


async def run(case, root):
    settings = Settings()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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
            settings.llm_model, max_tokens=settings.llm_max_tokens, thinking=None
        ),
    )
    model = ObservedModel(sdk, root / "model-trace.json", secrets)
    app = create_application(
        settings,
        model=model,
        providers={"train": FixtureTrain(), "hotel": SimulatedHotelProvider()},
        storage_root=root / "storage",
        user_id="edge-review",
        session_id=case,
    )
    app.preferences.set_user_preference("home_location", "重庆")
    seed = None
    record = {
        "case": case,
        "thinking": "disabled",
        "fixed_current_time": now.isoformat(),
        "activity_date": (now + timedelta(days=2)).date().isoformat(),
        "request_timeout": settings.request_timeout,
        "max_output_tokens": settings.llm_max_tokens,
        "model": settings.llm_model,
        "mode": "real_model_simulated_suppliers_and_retrieval_evidence",
    }
    if case == "policy_extraction":

        class PolicyEvidence:
            async def search(self, query, limit):
                return [
                    {
                        "id": "fixture-policy-source",
                        "content": "本次模拟制度：所有员工火车只能乘二等座；住宿没有金额上限，"
                        "无需额外审批；本规则适用于所有目的地，无其他强制规则。",
                        "metadata": {"title": "模拟差旅制度"},
                    }
                ]

        app.policy_agent.retriever = PolicyEvidence()
        query = CASES["business_policy_ok"]
    elif case == "handoff_weather":
        seed = await seed_content(app, now)
        tomorrow = (now + timedelta(days=2)).date().isoformat()
        app.queries.public = PublicQueryProvider(
            weather_fetch=lambda city: {
                "weather": [
                    {
                        "date": tomorrow,
                        "mintempC": "15",
                        "maxtempC": "20",
                        "hourly": [{"weatherDesc": [{"value": "小雨"}]}],
                    }
                ],
                "current_condition": [{"temp_C": "18", "weatherDesc": [{"value": "小雨"}]}],
            }
        )
        query = (
            "保留这次上海行程的交通和酒店，另查一下上海后天天气，再结合实际天气"
            "判断是否应该调整下午的散步，必要时只调整软活动并保存。"
        )
    else:
        seed = await seed_content(app, now)
        app.registry["query_policy"] = replace(
            app.registry["query_policy"], handler=build_policy_handler(FixturePolicy(), app.trips)
        )
        query = (
            "这次上海旅游的交通、酒店和活动全部保持原样。另外独立查询公司的火车席别政策，"
            "单独告诉我政策，不要把政策附加到这次旅游行程，也不要修改这次行程。"
        )
    refresh = app.refresh_memory_context

    def fixed_context(context):
        context["current_time"] = now.isoformat()
        if seed:
            context["review_trip_plan_id"] = seed["id"]
        refresh(context)

    app.refresh_memory_context = fixed_context
    record["query"] = query
    started = perf_counter()
    try:
        record["result"] = await app.run(query)
    finally:
        record["trips"] = [app.trips.load(t["id"]) for t in app.trips.list_summaries()]
        record["events"] = app.sessions.read_events()
        record["runs"] = app.sessions.read_runs()
        record["seed"] = seed
        record["elapsed_seconds"] = round(perf_counter() - started, 3)
        if case == "policy_extraction":
            record["assessment"] = assess({**record, "case": "business_policy_ok"})
        else:
            calls = [
                c for e in record["events"] if e["type"] == "tool_call" for c in e["tool_calls"]
            ]
            failures = []
            if case != "handoff_weather" and not any(c["name"] == "request_replan" for c in calls):
                failures.append("missing_handoff")
            if (
                case == "handoff_weather"
                and not any(
                    e["type"] == "execution_job"
                    and e["content"].get("name") == "trip_query"
                    and any(v.get("domain") == "weather" for v in e["content"].get("data", []))
                    for e in record["events"]
                )
                and not any(
                    c["name"] == "query"
                    and any(q["domain"] == "weather" for q in c["arguments"]["query_plan"])
                    for c in calls
                )
            ):
                failures.append("weather_not_queried")
            if case == "handoff_policy" and not any(c["name"] == "query_policy" for c in calls):
                failures.append("policy_not_queried")
            work = record["trips"][0]
            for old, new in zip(seed["tasks"], work["tasks"], strict=False):
                if old["draft_plan"] != new["draft_plan"]:
                    failures.append("unrelated_travel_changed")
            if case == "handoff_policy" and work != seed:
                failures.append("unchanged_trip_mutated")
            if record["result"]["status"] != "completed":
                failures.append("not_completed")
            record["assessment"] = {"failures": failures, "semantic_review": "pending"}
        save(root / "result.json", record, secrets)
        await sdk.client.close()
    print(case, record["assessment"], record["result"]["final_answer"], flush=True)


async def main():
    root = (
        Path("docs/evals")
        / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d") + "-constraint-first")
    ) / datetime.now().strftime("edges-%H%M%S")
    for case in sys.argv[1:] or ["policy_extraction", "handoff_weather"]:
        await run(case, root / case)
    print("REPORT", root)


if __name__ == "__main__":
    asyncio.run(main())
