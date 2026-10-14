"""Bounded real-model evaluation; supplier HTTP uses API-shaped test fixtures only."""

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import RunState  # noqa: E402
from agents.execution_harness import ExecutionHarness  # noqa: E402
from agents.main_agent import MainAgent  # noqa: E402
from agents.model_contracts import validate_model_money  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_contracts import create_workflow  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from context.memory_manager import MemoryManager  # noqa: E402
from context.telemetry import model_stage  # noqa: E402
from evals.api_shaped_simulation import ApiShapedHotelProvider, ApiShapedTrainProvider  # noqa: E402
from evals.run_live_policy_measurement import Capture, save  # noqa: E402
from utils.json_parser import robust_json_parse  # noqa: E402

OUTPUT = ROOT / "docs/evals/2026-10-13-prompt-optimization"
NOW = datetime(2026, 10, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
QUERY = "我明天要去上海，帮我安排一下往返火车和住宿行程。"
CASES = {
    "baseline_normal": QUERY,
    "normal": QUERY,
    "fixed_dates": "从重庆出发，10月14日去上海旅游，14日入住、16日离店，16日返回重庆。",
    "multi": "从重庆出发，明天去上海住1晚，再去杭州住3晚，最后回重庆，安排往返火车和酒店。",
    "outbound_failure": QUERY,
    "only_hotel": "去程酒店换一家，火车和返程保持不变。",
}


class CappedCapture(Capture):
    async def __call__(self, messages, **kwargs):
        if len(self.responses) >= 4:
            raise RuntimeError("four-call scenario cap reached; no automatic evaluation retry")
        return await super().__call__(messages, **kwargs)


class BaselineMain(MainAgent):
    """Frozen pre-change prompts with the identical production runtime and parsers."""

    def __init__(self, model):
        super().__init__(model)
        self.prompts = json.loads((OUTPUT / "baseline-prompts.json").read_text(encoding="utf-8"))

    async def initialize(self, context):
        with model_stage("main:plan"):
            turn = await self._request(
                lambda view: self._messages(self.prompts["initialize"], view),
                context=context,
                stage="main",
            )
        return self._initial_decision(turn.text)

    async def select_batch(self, context):
        key = (
            "selection_commit"
            if context.get("allowed_actions") == ["commit_batch"]
            else "selection_extended"
        )
        with model_stage("main:select_batch"):
            turn = await self._request(
                lambda view: self._messages(self.prompts[key], view),
                context=context,
                stage="main",
            )
        return validate_model_money(robust_json_parse(turn.text))


class FailingOutbound(ApiShapedTrainProvider):
    async def search(self, query):
        result = await super().search(query)
        if query.origin == "重庆" and query.destination == "上海":
            result = replace(result, status="error", items=[], message="模拟供应商去程查询失败")
        return result


def output_checks(result):
    """Inspect rendered prose against selected facts, not just valid model JSON."""
    text = result.get("final_answer", "")
    plans = (result.get("validated_plan") or {}).get("reconstructed_tasks", [])
    checks = {
        "nonempty_reply": bool(text.strip()),
        "no_unrendered_placeholders": "{{" not in text,
        "no_false_booking": not any(s in text for s in ("已预订", "已经订好", "已出票")),
        "simulation_disclosed": "模拟" in text,
        "selected_facts_in_reply": bool(plans),
        "dates_consistent": bool(plans),
        "unknown_hotel_quotes_disclosed": True,
    }
    prior_boundary = None
    for p in plans:
        train, hotel, schedule = p.get("train"), p.get("hotel"), p.get("schedule") or {}
        if train:
            checks["selected_facts_in_reply"] &= train["train_number"] in text
            if train.get("departure_at") and prior_boundary:
                checks["dates_consistent"] &= train["departure_at"][:10] >= prior_boundary[:10]
        if hotel:
            checks["selected_facts_in_reply"] &= hotel["hotel_name"] in text
            if hotel.get("kind") == "hotel_place":
                checks["unknown_hotel_quotes_disclosed"] &= "房价" in text and "空房" in text
        check_in, check_out = schedule.get("check_in"), schedule.get("check_out")
        if check_in and check_out:
            checks["dates_consistent"] &= check_in < check_out
            if schedule.get("arrival_at"):
                checks["dates_consistent"] &= check_in >= schedule["arrival_at"][:10]
        prior_boundary = check_out or schedule.get("arrival_at") or prior_boundary
    return checks


async def measure(output, cases):
    model = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 45},
        generate_kwargs=get_model_generate_kwargs(),
    )
    reports = []
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        for name in cases:
            directory = output / name
            capture = CappedCapture(model, directory)
            memory = MemoryManager(
                "prompt-eval", name, str(ROOT / "data/evals/prompt-optimization" / run_id / name)
            )
            memory.long_term.set_user_preference("home_location", "重庆")
            provider_type = (
                FailingOutbound if name == "outbound_failure" else ApiShapedTrainProvider
            )
            train, hotel = provider_type(now_fn=lambda: NOW), ApiShapedHotelProvider()

            class Registry(dict):
                providers = {"train_search": train, "hotel_search": hotel}

            main_type = BaselineMain if name == "baseline_normal" else MainAgent
            harness = ExecutionHarness(main_type(capture), Registry(), memory)
            report = dict(
                case=name,
                query=CASES[name],
                real_supplier_calls=0,
                supplier_mode="simulated raw API responses through production parsers",
                model_call_limit=4,
                rag_calls=0,
            )
            start = perf_counter()
            try:
                previous = None
                if name == "only_hotel":
                    previous = await harness.run_turn(
                        {"original_query": QUERY, "current_time": NOW.isoformat()},
                        RunState(memory.start_turn(QUERY)),
                    )
                    save(directory / "previous-result.json", previous)
                    report["setup_model_calls"] = len(capture.responses)
                    report["setup_train_calls"] = len(train.calls)
                context = {"original_query": CASES[name], "current_time": NOW.isoformat()}
                result = await harness.run_turn(context, RunState(memory.start_turn(CASES[name])))
                save(directory / "result.json", result)
                (directory / "final-answer.txt").write_text(
                    result.get("final_answer", ""), encoding="utf-8"
                )
                checks = output_checks(result)
                report.update(status=result.get("status"), output_checks=checks)
                tasks = result.get("workflow", {}).get("tasks", [])
                stays = [t for t in tasks if t["requires_hotel"]]
                if not previous:
                    initial = robust_json_parse(capture.responses[0]["text"])["workflow_proposal"]
                    create_workflow(
                        initial,
                        {**context, "effective_preferences": {"home_location": "重庆"}},
                        require_stay_proposals=True,
                    )
                    report["first_proposal_valid"] = True
                requirements_ok = bool(tasks)
                if name in {"normal", "baseline_normal"}:
                    requirements_ok &= (
                        len(stays) == 1 and stays[0]["field_sources"].get("nights") == "proposal"
                    )
                    requirements_ok &= stays[0]["condition_strengths"].get("nights") == "soft"
                elif name == "fixed_dates":
                    requirements_ok &= stays[0]["conditions"].get("check_in") == "2026-10-14"
                    requirements_ok &= stays[0]["conditions"].get("check_out") == "2026-10-16"
                    requirements_ok &= tasks[-1]["conditions"].get("departure_date") == "2026-10-16"
                elif name == "multi":
                    requirements_ok &= [t["conditions"].get("nights") for t in stays] == [1, 3]
                elif name == "outbound_failure":
                    plans = result["validated_plan"]["reconstructed_tasks"]
                    requirements_ok &= plans[0]["train"] is None and bool(
                        plans[0]["hotel"] and plans[-1]["train"]
                    )
                    requirements_ok &= [i["code"] for i in result["gaps"] if i["blocking"]] == [
                        "train_selection_missing"
                    ]
                    requirements_ok &= (
                        "待核实" in result["final_answer"] or "暂定" in result["final_answer"]
                    )
                elif name == "only_hotel":
                    old = previous["workflow"]["tasks"]
                    requirements_ok &= (
                        len(old) == len(tasks) and len(train.calls) == report["setup_train_calls"]
                    )
                    requirements_ok &= all(
                        a["draft_plan"]["train_selection"] == b["draft_plan"]["train_selection"]
                        for a, b in zip(old, tasks, strict=False)
                    )
                    requirements_ok &= (
                        old[0]["draft_plan"]["hotel_selection"]
                        != tasks[0]["draft_plan"]["hotel_selection"]
                    )
                    requirements_ok &= old[-1]["draft_plan"] == tasks[-1]["draft_plan"]
                report["requirements_ok"] = bool(requirements_ok)
                expected_status = "partial" if name == "outbound_failure" else "completed"
                report["passed"] = bool(
                    requirements_ok and all(checks.values()) and result["status"] == expected_status
                )
            except Exception as exc:
                report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
            report["latency_ms"] = round((perf_counter() - start) * 1000, 3)
            calls = capture.responses
            report.update(model_calls=len(calls), stages=[c.get("stage") for c in calls])
            report["stage_usage"] = [
                {k: c.get(k) for k in ("stage", "usage", "latency_ms", "ttft_ms")} for c in calls
            ]
            report["usage"] = {
                k: sum((c.get("usage") or {}).get(k, 0) for c in calls)
                for k in ("input_tokens", "output_tokens")
            }
            report["simulated_supplier_calls"] = {
                "train": len(train.calls),
                "hotel": len(hotel.calls),
            }
            save(
                directory / "supplier-fixtures.json",
                {"train": train.fixtures, "hotel": hotel.fixtures},
            )
            reports.append(report)
            save(output / "summary.json", reports)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await model.client.close()
    return all(r["passed"] for r in reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--cases", nargs="+", choices=list(CASES), default=list(CASES))
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(measure(args.output, args.cases)) else 1)
