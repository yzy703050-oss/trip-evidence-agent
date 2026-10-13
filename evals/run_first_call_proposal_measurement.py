"""Complete requests with the real model and raw API-shaped supplier fixtures."""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import RunState  # noqa: E402
from agents.execution_harness import ExecutionHarness  # noqa: E402
from agents.main_agent import MainAgent  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_contracts import create_workflow  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from context.memory_manager import MemoryManager  # noqa: E402
from evals.api_shaped_simulation import (  # noqa: E402
    ApiShapedHotelProvider,
    ApiShapedTrainProvider,
)
from evals.run_live_policy_measurement import Capture, save  # noqa: E402

CASES = {
    "normal": "我明天要去上海，帮我安排一下往返火车和住宿行程。",
    "fixed_dates": "从重庆出发，10月14日去上海旅游，14日入住、16日离店，16日返回重庆。",
    "multi": "从重庆出发，明天去上海住1晚，再去杭州住3晚，最后回重庆，安排往返火车和酒店。",
}
NOW = datetime(2026, 10, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
OUTPUT = ROOT / "docs/evals/2026-10-13-first-call-proposal"


class CappedCapture(Capture):
    async def __call__(self, messages, **kwargs):
        if len(self.responses) >= 3:
            raise RuntimeError("three-call scenario cap reached")
        return await super().__call__(messages, **kwargs)


async def measure():
    model = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 45},
        generate_kwargs=get_model_generate_kwargs(),
    )
    reports = []
    try:
        for name, query in CASES.items():
            output = OUTPUT / name
            capture = CappedCapture(model, output)
            memory = MemoryManager(
                "eval-first-proposal", name, str(ROOT / "data/evals/first-call-proposal" / name)
            )
            memory.long_term.set_user_preference("home_location", "重庆")
            train = ApiShapedTrainProvider(now_fn=lambda: NOW)
            hotel = ApiShapedHotelProvider()

            class Registry(dict):
                providers = {"train_search": train, "hotel_search": hotel}

            harness = ExecutionHarness(MainAgent(capture), Registry(), memory)
            context = {"original_query": query, "current_time": NOW.isoformat()}
            start = perf_counter()
            report = dict(
                case=name,
                query=query,
                supplier_mode="simulated raw API responses",
                real_supplier_calls=0,
                rag_calls=0,
                model_call_limit=3,
            )
            try:
                result = await harness.run_turn(context, RunState(memory.start_turn(query)))
                save(output / "result.json", result)
                report["status"] = result.get("status")
                first = json.loads(capture.responses[0]["text"])["workflow_proposal"]
                create_workflow(
                    first,
                    {**context, "effective_preferences": {"home_location": "重庆"}},
                    require_stay_proposals=True,
                )
                report["first_proposal_valid"] = True
                tasks = result.get("workflow", {}).get("tasks", [])
                stays = [t for t in tasks if t["requires_hotel"]]
                report["stay_nights"] = [t["conditions"].get("nights") for t in stays]
                report["stay_sources"] = [t["field_sources"].get("nights") for t in stays]
                if name == "normal":
                    requirements_ok = (
                        len(stays) == 1
                        and stays[0]["field_sources"].get("nights") == "proposal"
                        and stays[0]["condition_strengths"].get("nights") == "soft"
                    )
                elif name == "fixed_dates":
                    requirements_ok = (
                        stays[0]["conditions"].get("check_in") == "2026-10-14"
                        and stays[0]["conditions"].get("check_out") == "2026-10-16"
                        and tasks[-1]["conditions"].get("departure_date") == "2026-10-16"
                    )
                else:
                    requirements_ok = report["stay_nights"] == [1, 3]
                report["requirements_ok"] = requirements_ok
            except Exception as exc:
                report.update(
                    first_proposal_valid=False, error_type=type(exc).__name__, error=str(exc)
                )
            report["latency_ms"] = round((perf_counter() - start) * 1000, 3)
            calls = capture.responses
            report["stages"] = [c.get("stage") for c in calls]
            report["model_calls"] = len(calls)
            report["repair_calls"] = sum("repair" in (c.get("stage") or "") for c in calls)
            report["stage_usage"] = [
                dict(stage=c.get("stage"), usage=c.get("usage"), latency_ms=c.get("latency_ms"))
                for c in calls
            ]
            report["usage"] = {
                key: sum((c.get("usage") or {}).get(key, 0) for c in calls)
                for key in ("input_tokens", "output_tokens")
            }
            report["simulated_supplier_calls"] = len(train.calls) + len(hotel.calls)
            report["passed"] = bool(
                report.get("first_proposal_valid")
                and report.get("requirements_ok")
                and report.get("status") == "completed"
                and report["stages"] == ["main:plan", "main:select_batch"]
            )
            save(
                output / "supplier-fixtures.json",
                {"train": train.fixtures, "hotel": hotel.fixtures},
            )
            reports.append(report)
            save(OUTPUT / "summary.json", reports)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await model.client.close()
    return all(r["passed"] for r in reports)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(measure()) else 1)
