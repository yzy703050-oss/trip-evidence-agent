"""Real configured model, isolated storage, production runtime, fixture suppliers.

No product monkeypatches. Stress histories are labelled fixtures; summaries and
all evaluated answers use the real API. Run from the repository root.
"""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from agents.context_projection import project_session_result
from agents.contracts import RunLimits, RunState
from agents.execution_harness import ExecutionHarness
from agents.main_agent import MainAgent
from agents.memory_tools import read_memory
from agents.model_io import collect_model_turn
from config import RUN_LIMITS, SYSTEM_CONFIG
from context.context_budget import BudgetedModel, count_input_tokens
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel, current_model_context
from evals.api_shaped_simulation import (
    ApiShapedHotelProvider,
    ApiShapedTrainProvider,
    FixtureResponse,
    hotel_payload,
)

OUT = Path(__file__).resolve().parent / datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
    "run-%H%M%S"
)
NOW = datetime.now(ZoneInfo("Asia/Shanghai"))


def save(name, value):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class RecordingModel:
    def __init__(self, raw):
        self.raw, self.rows = raw, []
        self.model_name = raw.model_name
        self.turn_start = 0

    async def __call__(self, messages, **kwargs):
        if len(self.rows) >= 65 or len(self.rows) - self.turn_start >= 8:
            raise RuntimeError("evaluation model call cap reached")
        row = {
            **current_model_context(),
            "request": deepcopy(messages),
            "estimated_input": count_input_tokens(messages, kwargs.get("tools")).tokens,
        }
        self.rows.append(row)
        start = perf_counter()
        try:
            response = await self.raw(messages, **kwargs)
            turn = await collect_model_turn(response)
            row.update(
                status="success",
                text=turn.text,
                tool_calls=turn.tool_calls,
                usage=response.usage,
                ttft_ms=response.ttft_ms,
            )
            return response
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            row["latency_ms"] = round((perf_counter() - start) * 1000, 2)
            save("model-calls.json", self.rows)


class ManyHotels(ApiShapedHotelProvider):
    def _get(self, url, *, params, timeout):
        safe = {k: v for k, v in params.items() if k != "key"}
        payload = hotel_payload(safe)
        template = payload["pois"][0]
        payload["pois"] = [
            {
                **deepcopy(template),
                "id": f"B0MEM{i:06d}",
                "name": f"全季酒店（{safe['region']}模拟门店{i + 1:02d}）",
                "address": f"模拟路{i + 100}号",
            }
            for i in range(12)
        ]
        payload["count"] = "12"
        self.fixtures.append({"request": {"params": safe}, "response": deepcopy(payload)})
        return FixtureResponse(payload)


class Session:
    def __init__(self, model, user, session, budget=None, many=False, storage_root=None):
        self.model = model
        self.memory = MemoryManager(user, session, str(storage_root or OUT / "memory"))
        metered = MeteredModel(model, self.memory.session_store.append_run)
        self.budget = budget or SYSTEM_CONFIG["memory_context_budget_tokens"]
        wrapped = BudgetedModel(
            metered, budget_tokens=self.budget, record=self.memory.session_store.append_run
        )
        self.memory.llm_model = wrapped
        self.train = ApiShapedTrainProvider(now_fn=lambda: NOW)
        self.hotel = ManyHotels() if many else ApiShapedHotelProvider()
        registry = type("Registry", (dict,), {})()
        registry.providers = {"train_search": self.train, "hotel_search": self.hotel}
        self.harness = ExecutionHarness(
            MainAgent(wrapped, memory_manager=self.memory), registry, self.memory
        )
        self.harness.runner.limits = __import__("dataclasses").replace(
            self.harness.runner.limits, turn_timeout=120
        )

    async def turn(self, name, query):
        print("START", name, flush=True)
        self.model.turn_start = len(self.model.rows)
        before = self.memory.session_store.read_state()
        start = perf_counter()
        turn_id = self.memory.start_turn(query)
        snapshot = self.memory.get_compacted_context()
        run = RunState(turn_id, limits=RunLimits(**RUN_LIMITS))
        context = {
            "original_query": query,
            "current_time": NOW.isoformat(),
            "session_summary": snapshot["summary"],
            "recent_messages": [
                m for m in snapshot["recent_messages"] if m.get("turn_id") != turn_id
            ],
        }
        result = await self.harness.run_turn(context, run)
        self.memory.record_message(
            "assistant",
            json.dumps(project_session_result(result), ensure_ascii=False),
            turn_id,
            final=True,
            metadata={"format": "main_result"},
        )
        after = self.memory.session_store.read_state()
        calls = self.model.rows[self.model.turn_start :]
        row = {
            "name": name,
            "query": query,
            "session": self.memory.session_id,
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "answer": result.get("final_answer"),
            "model_calls": len(calls),
            "input_tokens": sum((c.get("usage") or {}).get("input_tokens", 0) for c in calls),
            "output_tokens": sum((c.get("usage") or {}).get("output_tokens", 0) for c in calls),
            "tool_requests": deepcopy(run.tool_requests),
            "external_requests": run.external_request_count,
            "summary_before": before.get("summary", ""),
            "summary_after": after.get("summary", ""),
            "covered_before": before.get("covered_through_seq", 0),
            "covered_after": after.get("covered_through_seq", 0),
            "latency_ms": round((perf_counter() - start) * 1000, 2),
        }
        save(
            f"turns/{name}.json",
            {
                "metrics": row,
                "result": result,
                "state": after,
                "memory": read_memory(self.memory, {}),
            },
        )
        (OUT / "turns" / f"{name}.txt").write_text(
            query + "\n\n" + (row["answer"] or ""), encoding="utf-8"
        )
        ROWS.append(row)
        save("turn-summary.json", ROWS)
        print(
            "DONE",
            name,
            row["status"],
            "models",
            len(calls),
            "tools",
            len(run.tool_requests),
            "external",
            run.external_request_count,
            flush=True,
        )
        return result


ROWS = []
CHECKS = []


def check(name, passed, evidence):
    CHECKS.append({"name": name, "passed": bool(passed), "evidence": evidence})
    save("checks.json", CHECKS)


def selected(result, field):
    return [
        t.get(field) for t in (result.get("validated_plan") or {}).get("reconstructed_tasks", [])
    ]
