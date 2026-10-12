"""Offline calibration through production MainAgent/child/Runner and isolated fixture data.

No network or user-memory writes. JSON report is the evidence; configuration is
updated separately after all scenario assertions succeed.
"""

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from agents.contracts import RunState, RunLimits
from agents.execution_harness import ExecutionHarness
from agents.main_agent import MainAgent
from context.context_budget import BudgetedModel
from context.memory_manager import MemoryManager
from context.resource_budget import calibration_mode
from context.telemetry import MeteredModel, current_model_context
from travel_data.tools import ToolExecutor
from workflow_runtime import WorkflowMain, OfflineProvider, InfoModel
from workflow_support import proposal
from test_information_agent_loop import info_class
from test_workflow_repair import action as repair_action
import config


class FixtureModel:
    model_name = "offline-scripted-responses"

    def __init__(self, p):
        self.controller = WorkflowMain(p)
        self.resume = None
        self.update = None
        self.nonworkflow = False
        self.finalizations = 0

    async def __call__(self, messages, **kwargs):
        stage = current_model_context()["stage"]
        context = json.loads(
            messages[-1]["content"]
            if stage == "main:workflow_match"
            else messages[0]["content"].rsplit("上下文：", 1)[1]
        )
        if stage == "main:plan":
            if self.nonworkflow:
                value = dict(
                    response_mode="answer",
                    finalization_mode="synthesize",
                    agent_schedule=[
                        dict(
                            agent_name="information_query", requested_domains=["train"], priority=2
                        )
                    ],
                )
            elif self.resume:
                value = dict(
                    response_mode="workflow",
                    finalization_mode="synthesize",
                    agent_schedule=[],
                    resume_workflow_id=self.resume,
                )
                if self.update is not None:
                    value["workflow_update"] = self.update
            else:
                value = await self.controller.initialize(context)
        elif stage == "main:workflow_match":
            value = dict(action="reuse", workflow_id=context["candidates"][0]["id"], refresh=False)
        elif stage == "main:step":
            w, task = context["workflow"], context["current_task"]
            if w.get("validation") and not w["validation"]["valid"]:
                value = repair_action(w)
            elif task["status"] == "pending":
                requests = [
                    r
                    for r in context["preflight"]["query_requests"]
                    if r["domain"] in task.get("update_scope", ["train", "hotel"])
                ]
                value = dict(
                    action="dispatch",
                    task_id=task["id"],
                    task_revision=task["revision"],
                    goal="查询当前段",
                    query_requests=requests,
                )
            else:
                value = await self.controller.step(context)
                if value["action"] == "draft_task":
                    for d in {"train", "hotel"} - set(task.get("update_scope", ["train", "hotel"])):
                        value["draft_plan"][d + "_selection"] = deepcopy(
                            task["draft_plan"][d + "_selection"]
                        )
        elif stage == "main:reply":
            value = {
                "template": "。".join(
                    "{{" + key + "}}" for key in context["reply_facts"]["required"]
                )
                + "。"
            }
        elif stage == "main:finalize":
            self.finalizations += 1
            value = (
                dict(
                    action="needs_requery",
                    reason="查看后续候选进行比较",
                    domains=["train"],
                    constraints={"candidate_offset": 5},
                )
                if self.finalizations == 1
                else dict(action="answer", final_answer="已比较缓存中的后续候选。")
            )
        else:
            raise AssertionError("Uncovered model stage: " + stage)
        return SimpleNamespace(text=json.dumps(value, ensure_ascii=False))


class FixtureInfoModel(InfoModel):
    async def __call__(self, messages, **kwargs):
        context = json.loads(messages[1]["content"])
        if context.get("type") == "task_request":
            return await super().__call__(messages, **kwargs)
        if len(messages) == 2:
            return SimpleNamespace(
                content=[
                    dict(
                        type="tool_use",
                        id="train1",
                        name="train_search",
                        input=dict(
                            origin="北京",
                            destination="上海",
                            departure_date="2026-10-11",
                            passengers=1,
                        ),
                    )
                ]
            )
        return SimpleNamespace(text='{"summary":"离线样本资料已整理"}')


class SampleRuntime:
    def __init__(self, root, p, *, session="s"):
        self.memory = MemoryManager("calibration", session, str(root))
        self.raw = FixtureModel(p)
        self.rows = []

        def record(row):
            self.rows.append(deepcopy(row))
            self.memory.session_store.append_run(row)

        budget = config.SYSTEM_CONFIG["memory_context_budget_tokens"]
        model = BudgetedModel(MeteredModel(self.raw, record), budget_tokens=budget, record=record)
        self.main = MainAgent(model, memory_manager=self.memory)
        self.memory.llm_model = model
        self.train, self.hotel = OfflineProvider("train"), OfflineProvider("hotel")
        child = BudgetedModel(
            MeteredModel(FixtureInfoModel(), record), budget_tokens=budget, record=record
        )
        self.info = info_class()(
            model=child,
            tool_executor=ToolExecutor({"train_search": self.train, "hotel_search": self.hotel}),
            memory_manager=self.memory,
        )
        self.harness = ExecutionHarness(self.main, {"information_query": self.info}, self.memory)

    async def turn(self, query="安排交通和酒店"):
        tid = self.memory.start_turn(query)
        run = RunState(tid, limits=RunLimits(**config.RUN_LIMITS))
        result = await self.harness.run_turn(
            {"original_query": query, "current_time": "2026-10-10T10:00:00+08:00"}, run
        )
        # The response is persisted as a completed user turn just as in CLI.
        self.memory.record_message("assistant", result["final_answer"], tid, final=True)
        return result, tid

    def metrics(self, tid):
        rows = [r for r in self.memory.session_store.read_runs() if r.get("turn_id") == tid]
        estimates = [r for r in rows if r["type"] == "context_budget"]
        success = [r for r in rows if r["type"] == "model_call" and r["status"] == "success"]
        main = [r for r in success if r["stage"].startswith("main:")]
        by_task = Counter(r["task_id"] for r in main if r.get("task_id"))
        by_child = Counter(
            r["child_invocation_id"] for r in success if r.get("child_invocation_id")
        )
        delegation = [
            r for r in rows if r["type"] == "information_delegation" and r["status"] == "success"
        ]
        per_task = Counter(r["task_id"] for r in delegation if r.get("task_id"))
        return {
            "context_tokens": max([r["tokens"] for r in estimates] or [0]),
            "output_tokens": max([r.get("response_tokens", 0) for r in estimates] or [0]),
            "main_model_calls_per_turn": len(main),
            "main_model_calls_per_task": max(by_task.values(), default=0),
            "info_model_calls": max(by_child.values(), default=0),
            "main_decisions_per_turn": sum(r["type"] == "workflow_decision" for r in rows),
            "info_executions_per_task": max(per_task.values(), default=0),
            "info_executions_per_run": sum(not r.get("task_id") for r in delegation),
        }


async def calibrate():
    samples = []
    with tempfile.TemporaryDirectory(prefix="ctsample") as temp, calibration_mode():
        base = Path(temp)

        async def take(name, rt, query="安排交通和酒店", *, expected_tasks=None):
            result, tid = await rt.turn(query)
            expected = "ok" if rt.raw.nonworkflow else "completed"
            assert result["status"] == expected, (
                name,
                result.get("stop_reason"),
                result.get("final_answer"),
            )
            if expected_tasks is not None:
                assert len(result["workflow"]["tasks"]) == expected_tasks
            samples.append(
                {
                    "scenario": name,
                    "status": result["status"],
                    "metrics": rt.metrics(tid),
                    "reply_style": result.get("reply_style"),
                    "provider_calls": len(rt.train.calls) + len(rt.hotel.calls),
                }
            )
            return result

        one = proposal(["北京", "上海", "北京"])
        one["tasks"] = one["tasks"][:1]
        await take(
            "single_destination_default_return", SampleRuntime(base / "one", one), expected_tasks=2
        )
        trip = SampleRuntime(base / "trip", proposal())
        first = await take("three_segments", trip, expected_tasks=3)
        await take(
            "five_segments",
            SampleRuntime(
                base / "five", proposal(["重庆", "上海", "苏州", "杭州", "北京", "重庆"])
            ),
            expected_tasks=5,
        )
        # Existing-trip matching uses summary candidates and its actual main-model request.
        await take(
            "same_trip_summary_matching", SampleRuntime(base / "trip", proposal(), session="match")
        )
        restore = SampleRuntime(base / "trip", proposal(), session="resume")
        restore.raw.resume = first["workflow_id"]
        restore.raw.update = {"confirmed_conditions": {"passengers": 3}}
        await take(
            "resume_and_party_update", restore, "恢复上次旅程，实际是三个人", expected_tasks=3
        )
        repair = SampleRuntime(base / "repair", proposal())
        repair.raw.controller.p["tasks"][1]["conditions"]["departure_date"] = "2026-10-12"
        repair.raw.controller.p["tasks"][1]["field_sources"]["departure_date"] = "proposal"
        await take("cross_segment_middle_task_repair", repair, expected_tasks=3)
        non = SampleRuntime(base / "non", proposal())
        non.raw.nonworkflow = True
        await take("nonworkflow_candidate_page_cache_feedback", non, "只查北京去上海车票")
        assert len(non.train.calls) == 1, "feedback must reuse cached provider result"
    maxima = {}
    for sample in samples:
        for key, value in sample["metrics"].items():
            if key not in maxima or value > maxima[key]["max"]:
                maxima[key] = {
                    "max": value,
                    "scenario": sample["scenario"],
                    "configured": value * 2,
                }
    assert all(row["max"] > 0 for row in maxima.values()), maxima
    return {
        "schema_version": 1,
        "source": "production harness with offline scripted SDK responses and test providers",
        "token_method": "utf8_conservative (estimated, not provider usage)",
        "real_supplier_requests": 0,
        "provider_usage": None,
        "provider_hard_limits": "not verified by offline fixtures",
        "independent_guards": {
            "resilience": deepcopy(config.RESILIENCE_CONFIG),
            "info_tool_calls": config.RUN_LIMITS["info_tool_calls"],
            "tool_timeout": config.RUN_LIMITS["tool_timeout"],
            "candidate_limit": config.RUN_LIMITS["candidate_limit"],
            "tools_per_task": config.WORKFLOW_LIMITS["tools_per_task"],
            "turn_timeout": config.WORKFLOW_LIMITS["turn_timeout"],
        },
        "samples": samples,
        "maxima": maxima,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/evals/2026-10-12-resource-calibration.json")
    args = parser.parse_args()
    report = asyncio.run(calibrate())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["maxima"], ensure_ascii=False, indent=2))
