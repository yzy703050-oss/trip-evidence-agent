"""One-call saved-evidence checks: real supplier cache and confirmed hard conflict."""

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import RunState  # noqa: E402
from agents.main_agent import MainAgent  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_batches import check_batch_submission  # noqa: E402
from agents.workflow_guard import check_workflow  # noqa: E402
from agents.workflow_runner import WorkflowRunner  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from evals.run_live_policy_measurement import save  # noqa: E402
from evals.run_shanghai_selection_replay import OneCallCapture, public_data  # noqa: E402
from travel_data.tools import ToolExecutor  # noqa: E402
from utils.workflow_reply import build_reply_facts  # noqa: E402

OUTPUT = ROOT / "docs/evals/2026-10-13-prompt-optimization/boundaries"


def context(workflow, query):
    runner = SimpleNamespace(
        w=workflow,
        context={"original_query": query, "current_time": "2026-10-13T10:00:00+08:00"},
        run_state=RunState("prompt-boundary"),
        executor=ToolExecutor({"hotel_search": SimpleNamespace(hotel_places=True)}),
    )
    return WorkflowRunner._batch_context(runner)


async def main():
    model = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 45},
        generate_kwargs=get_model_generate_kwargs(),
    )
    reports = []
    try:
        for name in ("actual_supplier_cache", "hard_conflict"):
            path = ROOT / "docs/evals/2026-10-13-shanghai-selection-replay/result.json"
            if name == "hard_conflict":
                path = (
                    ROOT
                    / "docs/evals/2026-10-13-prompt-optimization/revision-2/fixed_dates/result.json"
                )
            workflow = public_data(json.loads(path.read_text(encoding="utf-8"))["workflow"])
            workflow.pop("candidate_cache", None)
            if name == "actual_supplier_cache":
                workflow["validation"] = None
                for task in workflow["tasks"]:
                    task.update(draft_plan=None, issues=[], draft_stage="query", status="draft")
                query = "我明天要去上海，帮我安排一下行程"
            else:
                task = workflow["tasks"][0]
                task["conditions"].update(check_out="2026-10-17", nights=3)
                task["condition_strengths"].update(check_out="hard", nights="hard")
                task["draft_plan"]["schedule"]["check_out"] = "2026-10-17"
                workflow["validation"] = check_workflow(workflow)
                query = "必须14日去上海，14日入住、17日离店，16日从上海返回重庆，这些日期都不能改。"
            view = context(workflow, query)
            save(OUTPUT / name / "context.json", view)
            capture = OneCallCapture(model, OUTPUT / name)
            report = dict(
                case=name,
                supplier_calls=0,
                model_call_limit=1,
                scope="saved select_batch context only; hard_conflict is synthetic",
            )
            before = deepcopy(workflow)
            try:
                action = await MainAgent(capture).select_batch(view)
                save(OUTPUT / name / "action.json", action)
                report["action"] = action["action"]
                if name == "hard_conflict":
                    report["question"] = action.get("question")
                    report["blocking_codes"] = [i["code"] for i in view["issues"] if i["blocking"]]
                    report["passed"] = (
                        action["action"] == "ask_user"
                        and bool(action.get("question"))
                        and "departure_before_previous_checkout" in report["blocking_codes"]
                        and workflow == before
                    )
                else:
                    submitted = check_batch_submission(workflow, action)
                    check = submitted["validation"]
                    result = dict(
                        workflow=submitted["workflow"],
                        validated_plan=check,
                        gaps=check["issues"],
                        status="partial" if not check["valid"] else "completed",
                    )
                    result["final_answer"] = build_reply_facts(result)["fallback"]
                    save(OUTPUT / name / "result.json", result)
                    report["original_counts"] = [
                        len(r["items"]) for r in workflow["results_by_query"].values()
                    ]
                    report["visible_counts"] = [len(r["items"]) for r in view["task_results"]]
                    plans = check["reconstructed_tasks"]
                    report["blocking_codes"] = [i["code"] for i in check["issues"] if i["blocking"]]
                    report["passed"] = bool(
                        action["action"] == "commit_batch"
                        and action.get("allow_repair") is False
                        and plans[0]["train"] is None
                        and plans[0]["hotel"]
                        and plans[1]["train"]
                        and report["blocking_codes"] == ["train_selection_missing"]
                        and plans[0]["hotel"]["hotel_name"] in result["final_answer"]
                        and plans[1]["train"]["train_number"] in result["final_answer"]
                    )
            except Exception as exc:
                report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
            call = capture.responses[-1] if capture.responses else {}
            report.update(
                model_calls=len(capture.responses),
                usage=call.get("usage"),
                latency_ms=call.get("latency_ms"),
            )
            reports.append(report)
            save(OUTPUT / "summary.json", reports)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await model.client.close()
    return all(r["passed"] for r in reports)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
