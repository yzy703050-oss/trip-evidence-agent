"""One model call per saved context; no supplier or RAG calls and no retries."""

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.main_agent import MainAgent  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_batches import check_batch_submission  # noqa: E402
from agents.workflow_repair import apply_repair  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from evals.run_live_policy_measurement import Capture, save  # noqa: E402

CASES = {
    "place_without_quote": "2026-10-13-api-shaped-capability-fixed/business_api_simulated",
    "quotes_over_policy": "2026-10-13-api-shaped-simulation/business_conflict",
    "normal_candidates": "2026-10-13-api-shaped-simulation/tourism_simulated",
}


def snapshot(source):
    directory = ROOT / "docs/evals" / source
    calls = json.loads((directory / "model-responses.json").read_text(encoding="utf-8"))
    call = next(c for c in calls if c["stage"] == "main:select_batch")
    view = json.loads(call["request"][0]["content"].split("上下文：", 1)[1])
    final = json.loads((directory / "result.json").read_text(encoding="utf-8"))["workflow"]
    workflow = deepcopy(final)
    workflow.update({k: deepcopy(v) for k, v in view["workflow"].items() if k != "tasks"})
    originals = {t["id"]: t for t in workflow["tasks"]}
    workflow["tasks"] = [
        {**originals[t["id"]], **deepcopy(t), "draft_plan": None} for t in view["workflow"]["tasks"]
    ]
    # Restore revisions that preceded the historical refresh; preserve full cache evidence.
    for visible in view["task_results"]:
        row = workflow["results_by_query"][visible["id"]]
        if row["result_revision"] != visible["result_revision"]:
            historical = next(
                h
                for h in row.get("history", [])
                if h["result_revision"] == visible["result_revision"]
            )
            workflow["results_by_query"][visible["id"]] = deepcopy(historical)
    workflow["validation"] = None
    return view, workflow


async def soft_snapshot(output):
    sys.path.insert(0, str(ROOT / "tests"))
    from dataclasses import replace

    from workflow_runtime import OfflineProvider, WorkflowMain

    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor

    proposal = {
        "confirmed_conditions": {"end_date": "2026-10-22"},
        "tasks": [
            dict(
                origin=a,
                destination=b,
                requires_hotel=False,
                conditions={"departure_date": day},
                field_sources={"departure_date": "user"},
                condition_strengths={"departure_date": "soft"},
            )
            for a, b, day in [("重庆", "上海", "2026-10-17"), ("上海", "重庆", "2026-10-19")]
        ],
    }

    class Main(WorkflowMain):
        async def select_batch(self, context):
            self.snapshot = deepcopy(context), deepcopy(harness.runner.w)
            action = await super().select_batch(context)
            action["allow_repair"] = False
            return action

    class Train(OfflineProvider):
        async def search(self, query):
            result = await super().search(query)
            return (
                replace(result, items=[]) if str(query.departure_date) == "2026-10-17" else result
            )

    main = Main(proposal)
    harness = ExecutionHarness(main, {}, MemoryManager("eval", "soft", str(output / "memory")))
    harness.runner.executor = ToolExecutor({"train_search": Train("train")})
    await harness.run_turn(
        {
            "original_query": "最好17日重庆去上海、19日返回，去返程日期均可调整。"
            "若17日无车，请去返程一起调整，保持两天间隔；必须22日前返回。仅查火车。",
            "current_time": "2026-10-13T10:00:00+08:00",
        },
        RunState("soft"),
    )
    return main.snapshot


async def main(output, cases):
    raw = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 45},
        generate_kwargs=get_model_generate_kwargs(),
    )
    reports = []
    try:
        for case in cases:
            source = CASES.get(case, "synthetic soft-date context with offline train fixtures")
            view, workflow = (
                await soft_snapshot(output / case) if case == "soft_dates" else snapshot(source)
            )
            save(output / case / "context.json", view)
            capture = Capture(raw, output / case)
            report = {
                "case": case,
                "source": source,
                "scope": "saved-context select_batch only",
                "supplier_calls": 0,
                "rag_calls": 0,
                "model_call_limit": 1,
            }
            try:
                action = await MainAgent(capture).select_batch(view)
                save(output / case / "action.json", action)
                report.update(action=action["action"], allow_repair=action.get("allow_repair"))
                if case == "soft_dates":
                    if action["action"] != "repair_tasks":
                        raise ValueError("expected repair_tasks; received " + action["action"])
                    repaired = apply_repair(workflow, action)
                    dates = [t["conditions"]["departure_date"] for t in repaired["tasks"]]
                    from datetime import date

                    report["new_dates"] = dates
                    report["passed"] = (
                        action["action"] == "repair_tasks"
                        and dates[0] != "2026-10-17"
                        and (date.fromisoformat(dates[1]) - date.fromisoformat(dates[0])).days == 2
                        and dates[1] <= "2026-10-22"
                    )
                else:
                    submitted = check_batch_submission(workflow, action)
                    check = submitted["validation"]
                    report["validation_valid"] = check["valid"]
                    report["issues"] = [i["code"] for i in check["issues"]]
                    expected_partial = case != "normal_candidates"
                    report["passed"] = (
                        action["action"] == "commit_batch"
                        and check["valid"] != expected_partial
                        and (not expected_partial or action.get("allow_repair") is False)
                    )
            except Exception as exc:
                report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
            response = capture.responses[-1] if capture.responses else {}
            report.update(
                model_calls=len(capture.responses),
                usage=response.get("usage"),
                latency_ms=response.get("latency_ms"),
                ttft_ms=response.get("ttft_ms"),
            )
            reports.append(report)
            save(output / "summary.json", reports)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await raw.client.close()
    return all(r["passed"] for r in reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/evals/2026-10-13-tool-evidence-boundaries"
    )
    parser.add_argument("--cases", nargs="+", choices=[*CASES, "soft_dates"], default=list(CASES))
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(main(args.output, args.cases)) else 1)
