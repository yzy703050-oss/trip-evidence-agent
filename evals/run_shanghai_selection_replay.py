"""Replay one selection on the user's saved supplier facts, without querying suppliers."""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import RunState  # noqa: E402
from agents.main_agent import MainAgent  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_batches import check_batch_submission  # noqa: E402
from agents.workflow_runner import WorkflowRunner  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from evals.run_live_policy_measurement import Capture, save  # noqa: E402
from travel_data.tools import ToolExecutor  # noqa: E402
from utils.workflow_reply import build_reply_facts  # noqa: E402


def read_snapshot(database, workflow_id):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT content_json FROM trip_plans WHERE id=?", (workflow_id,)
        ).fetchone()
    if not row:
        raise ValueError("saved workflow not found")
    workflow = json.loads(row[0])
    # Restore query-complete selection inputs, not the model's previous rejected choices.
    workflow["validation"] = None
    for key in ("candidate_cache", "audit", "user_id", "checkpoint"):
        workflow.pop(key, None)
    for task in workflow["tasks"]:
        task.update(draft_plan=None, issues=[], draft_stage="query", status="draft")
    return workflow


def public_data(value):
    if isinstance(value, dict):
        return {k: public_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public_data(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
    return value


def batch_context(workflow):
    runner = SimpleNamespace(
        w=workflow,
        context={
            "original_query": "我明天要去上海，帮我安排一下行程",
            "current_time": "2026-10-13T18:58:33+08:00",
        },
        run_state=RunState("saved-shanghai-replay"),
        executor=ToolExecutor({"hotel_search": SimpleNamespace(hotel_places=True)}),
    )
    return WorkflowRunner._batch_context(runner)


class OneCallCapture(Capture):
    async def __call__(self, messages, **kwargs):
        if self.responses:
            raise RuntimeError("one-call replay limit reached")
        return await super().__call__(messages, **kwargs)


async def replay(database, workflow_id, output):
    workflow = public_data(read_snapshot(database, workflow_id))
    context = batch_context(workflow)
    save(output / "context.json", context)
    report = dict(
        scope="saved actual supplier facts; reconstructed select_batch context only",
        supplier_calls=0,
        rag_calls=0,
        model_call_limit=1,
        original_counts=[len(r["items"]) for r in workflow["results_by_query"].values()],
        visible_counts=[len(r["items"]) for r in context["task_results"]],
    )
    model = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 45},
        generate_kwargs=get_model_generate_kwargs(),
    )
    capture = OneCallCapture(model, output)
    try:
        action = await MainAgent(capture).select_batch(context)
        save(output / "action.json", action)
        submitted = check_batch_submission(workflow, action)
        checked = submitted["validation"]
        plans = checked["reconstructed_tasks"]
        result = dict(
            workflow=submitted["workflow"],
            validated_plan=checked,
            gaps=checked["issues"],
            status="partial" if not checked["valid"] else "completed",
        )
        result["final_answer"] = build_reply_facts(result)["fallback"]
        save(output / "result.json", result)
        report.update(
            action=action["action"],
            allow_repair=action.get("allow_repair"),
            validation_valid=checked["valid"],
            blocking_codes=[i["code"] for i in checked["issues"] if i["blocking"]],
            hotel_name=(plans[0]["hotel"] or {}).get("hotel_name"),
            return_train=(plans[1]["train"] or {}).get("train_number"),
        )
        report["passed"] = bool(
            action["action"] == "commit_batch"
            and action.get("allow_repair") is False
            and plans[0]["train"] is None
            and plans[0]["hotel"]
            and plans[1]["train"]
            and report["blocking_codes"] == ["train_selection_missing"]
        )
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        response = capture.responses[-1] if capture.responses else {}
        report.update(
            model_calls=len(capture.responses),
            usage=response.get("usage"),
            latency_ms=response.get("latency_ms"),
            ttft_ms=response.get("ttft_ms"),
        )
        save(output / "summary.json", report)
        await model.client.close()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/evals/2026-10-13-shanghai-selection-replay"
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(replay(args.database, args.workflow_id, args.output)) else 1)
