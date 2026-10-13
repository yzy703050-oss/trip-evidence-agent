"""Real configured model and local vector RAG; label supplier fixtures explicitly."""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import RunState  # noqa: E402
from agents.execution_harness import ExecutionHarness  # noqa: E402
from agents.lazy_agent_registry import LazyAgentRegistry  # noqa: E402
from agents.main_agent import MainAgent  # noqa: E402
from agents.model_io import collect_model_turn  # noqa: E402
from agents.openai_model import OpenAIChatModel  # noqa: E402
from agents.workflow_guard import check_workflow  # noqa: E402
from agents.workflow_runner import WorkflowLimits  # noqa: E402
from config import (  # noqa: E402
    LLM_CONFIG,
    SYSTEM_CONFIG,
    WORKFLOW_LIMITS,
    get_model_generate_kwargs,
    get_settings,
)
from context.context_budget import BudgetedModel  # noqa: E402
from context.memory_manager import MemoryManager  # noqa: E402
from context.telemetry import MeteredModel, current_model_context  # noqa: E402
from evals.api_shaped_simulation import ApiShapedHotelProvider, ApiShapedTrainProvider  # noqa: E402
from travel_data.amap_hotel import AmapHotelProvider  # noqa: E402
from travel_data.contracts import AgentDataResult, HotelOffer, Source  # noqa: E402
from travel_data.juhe_train import JuheTrainProvider  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


class Capture:
    def __init__(self, model, directory):
        self.model, self.directory = model, directory
        self.responses = []

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def __call__(self, messages, **kwargs):
        if len(self.responses) >= 8:
            raise RuntimeError("evaluation model-call cap reached")
        row = {
            **current_model_context(),
            "request": messages,
            "started_at": datetime.now(TZ).isoformat(),
        }
        self.responses.append(row)
        start = perf_counter()
        try:
            response = await self.model(messages, **kwargs)
            turn = await collect_model_turn(response)
            row.update(
                text=turn.text,
                tool_calls=turn.tool_calls,
                usage=response.usage,
                ttft_ms=response.ttft_ms,
                status="success",
            )
            return response
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            row["latency_ms"] = round((perf_counter() - start) * 1000, 3)
            save(self.directory / "model-responses.json", self.responses)


class TimedRAG:
    def __init__(self, agent, directory):
        self.agent, self.directory, self.calls = agent, directory, []

    async def search(self, query, limit=5):
        start = perf_counter()
        rows = self.agent.search_knowledge(query, top_k=limit)
        row = {
            "query": query,
            "latency_ms": round((perf_counter() - start) * 1000, 3),
            "results": rows,
            "error": self.agent._last_search_error,
        }
        self.calls.append(row)
        save(self.directory / "rag-searches.json", self.calls)
        if row["error"]:
            raise RuntimeError("local vector retrieval failed: " + row["error"])
        return rows


class ConflictHotel:
    """First batch violates the cap; explicit refresh yields compliant quotes."""

    hotel_places = False

    def __init__(self, *, conflict=True):
        self.calls, self.fixtures = [], []
        self.conflict = conflict

    async def search(self, query):
        self.calls.append(query.to_dict())
        await asyncio.sleep(0)
        source = Source("simulation:quote-contract-fixture", datetime.now(TZ), None)
        nights = (query.check_out - query.check_in).days
        prices = [700, 760] if self.conflict and len(self.calls) == 1 else [380, 420]
        items = [
            HotelOffer(
                f"fixture-hotel-{len(self.calls)}-{i}",
                f"全季（上海模拟报价候选{i + 1}）",
                "模拟标准间",
                query.check_in,
                query.check_out,
                query.guests,
                Decimal(price * nights),
                True,
                "available",
                None,
                source,
                None,
            ).to_dict()
            for i, price in enumerate(prices)
        ]
        result = AgentDataResult(
            "ok",
            query.to_dict(),
            items,
            [],
            source,
            source.fetched_at,
            "独立酒店报价契约测试夹具；不来自高德，不是真实酒店价格或库存。",
        )
        self.fixtures.append({"request": query.to_dict(), "parsed": result.to_dict()})
        return result


def build_rag(directory):
    start = perf_counter()
    # Faiss on Windows requires an ASCII path; use a fresh temporary DB, not production.
    db_path = tempfile.mkdtemp(prefix="v0-policy-live-")
    os.environ["V0_RAG_DB_DIR"] = db_path
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch

    torch.set_num_threads(2)
    script = ROOT / ".claude/skills/ask-question/script/agent.py"
    spec = importlib.util.spec_from_file_location("live_policy_rag", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.RAGKnowledgeAgent(
        knowledge_base_path=str(directory / "rag"), collection_name="live_policy_eval", top_k=5
    )
    documents = [
        {
            "content": (
                "【虚构测试制度】星桥信息境内出差制度 SQ-TRAVEL-2026-V2，版本2，2026-10-01生效。"
                "适用普通员工上海出差。第一条：高铁动车只能二等座，单程火车费用每人不超过700元。"
                "第二条：上海住宿含税及必付费用每间每晚不超过500元。品牌是软偏好，不能覆盖上限。"
                "必须检索当前制度并遵守硬约束。"
            ),
            "metadata": {
                "document_id": "SQ-TRAVEL-2026-V2",
                "title": "星桥信息境内出差制度",
                "version": "2",
                "effective_date": "2026-10-01",
                "location": "第一、二条",
                "status": "active",
            },
        },
        {
            "content": (
                "【虚构测试旧制度】星桥信息旧差旅标准 SQ-TRAVEL-2025-V1 曾允许上海住宿900元/晚。"
                "本制度已于2026-10-01废止，被SQ-TRAVEL-2026-V2替代。当前出差不能采用旧900元标准，"
                "必须采用版本2的上海500元/晚限额及高铁二等座要求。"
            ),
            "metadata": {
                "document_id": "SQ-TRAVEL-2025-V1",
                "title": "已废止住宿标准",
                "version": "1",
                "location": "废止说明",
                "status": "superseded",
            },
        },
        {
            "content": (
                "【虚构测试例外】星桥信息SQ-EXCEPTION-2026仅说明超标审批流程："
                "项目出差如需上海住宿上限800元/晚，必须在预订前取得有效审批编号，"
                "并核对员工、城市、日期、项目。此文档不是已批准凭证。"
                "没有审批编号时采用版本2基础限额500元/晚；不能因为用户偏好全季就视为批准。"
            ),
            "metadata": {
                "document_id": "SQ-EXCEPTION-2026",
                "title": "超标审批条件",
                "version": "1",
                "location": "例外适用条件",
                "status": "active",
            },
        },
    ]
    for i, document in enumerate(documents, 1):
        (directory / f"policy-{i}.txt").write_text(document["content"] + "\n", encoding="utf-8")
    added = agent.add_documents(documents)
    if added["status"] != "success":
        raise RuntimeError("RAG document ingestion failed: " + str(added))
    metadata = {
        "database_path": db_path,
        "documents": documents,
        "ingestion": added,
        "initialization_and_ingestion_ms": round((perf_counter() - start) * 1000, 3),
        "embedding_tokens": None,
        "embedding_note": "local sentence-transformers; no billed embedding API",
    }
    save(directory / "rag-setup.json", metadata)
    return agent, metadata


async def run_case(case, query, directory, rag, providers, *, live_suppliers):
    directory.mkdir(parents=True, exist_ok=True)
    memory = MemoryManager(case, "live", storage_path=str(directory / "memory"))
    memory.long_term.set_user_preference("home_location", "重庆")
    raw = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 65},
        generate_kwargs=get_model_generate_kwargs(),
    )
    capture = Capture(raw, directory)
    model = MeteredModel(capture, memory.session_store.append_run)
    model = BudgetedModel(
        model,
        budget_tokens=SYSTEM_CONFIG["memory_context_budget_tokens"],
        record=memory.session_store.append_run,
    )
    timed = TimedRAG(rag, directory)
    registry = LazyAgentRegistry(
        model, {"rag_knowledge": timed}, memory_manager=memory, providers=providers
    )
    harness = ExecutionHarness(MainAgent(model, memory_manager=memory), registry, memory)
    harness.runner.limits = replace(
        WorkflowLimits(**WORKFLOW_LIMITS),
        main_model_calls_per_turn=6,
        main_decisions_per_turn=4,
        turn_timeout=240,
    )
    turn = memory.start_turn(query)
    run = RunState(turn)
    context = {
        "original_query": query,
        "current_time": datetime.now(TZ).isoformat(),
        "employee_level": "普通员工",
        "company": "星桥信息",
        "purpose": "tourism" if case.startswith("tourism_") else "business",
    }
    started = perf_counter()
    try:
        result = await asyncio.wait_for(harness.run_turn(context, run), timeout=260)
    except Exception as exc:
        result = {"status": "error", "error_type": type(exc).__name__, "final_answer": str(exc)}
    elapsed = round((perf_counter() - started) * 1000, 3)
    save(directory / "result.json", result)
    if not live_suppliers:
        save(
            directory / "supplier-fixtures.json",
            {
                name: {
                    "mode": (
                        "raw_api_response"
                        if isinstance(provider, ApiShapedTrainProvider | ApiShapedHotelProvider)
                        else "quote_contract_fixture"
                    ),
                    "network_used": False,
                    "fixtures": provider.fixtures,
                }
                for name, provider in providers.items()
            },
        )
    rows = [r for r in memory.session_store.read_runs() if r.get("type") == "model_call"]
    save(directory / "model-usage.json", rows)
    workflow = result.get("workflow") or {}
    validation = check_workflow(workflow) if workflow else None
    save(directory / "validation.json", validation)
    policy = workflow.get("policy_context") or {}
    fields = {c.get("field"): c.get("value") for c in policy.get("constraints", [])}
    counts = Counter(r["name"] for r in run.tool_requests if not r.get("cache_hit"))
    checks = {
        "no_model_error": bool(rows) and all(r["status"] == "success" for r in rows),
        "usage_available": bool(rows)
        and all(
            r.get("input_tokens") is not None and r.get("output_tokens") is not None for r in rows
        ),
        "no_proposal_repair": not any(r["stage"] == "main:repair_proposal" for r in rows),
        "finite_stop": result.get("status") in {"completed", "partial"}
        and result.get("stop_reason") not in {"main_decision_limit", "no_progress"},
    }
    if case.startswith("tourism_"):
        checks.update(
            honest_supplier_result=bool(
                validation
                and (
                    result.get("status") == "completed"
                    and validation["valid"]
                    or result.get("status") == "partial"
                    and not validation["valid"]
                    and any(i["code"] == "train_selection_missing" for i in validation["issues"])
                )
            ),
            no_policy_call=not timed.calls,
        )
        if not live_suppliers:
            checks.update(
                completed=result.get("status") == "completed",
                global_valid=bool(validation and validation["valid"]),
                two_main_calls=len(rows) == 2,
            )
    else:
        checks.update(
            actual_vector_retrieval=bool(timed.calls and timed.calls[0]["results"]),
            current_hotel_cap=Decimal(str(fields.get("hotel_max_nightly_cny", 0))) == 500,
            source_citations=bool(policy.get("references")),
        )
        if live_suppliers or case == "business_api_simulated":
            checks.update(
                no_false_completion=result.get("status") != "completed",
                price_evidence_gap=bool(
                    validation
                    and any(
                        "hotel" in i["code"] or "policy" in i["code"] for i in validation["issues"]
                    )
                ),
            )
        else:
            checks.update(
                completed=result.get("status") == "completed",
                global_valid=bool(validation and validation["valid"]),
                trains_not_replayed=len(providers["train_search"].calls) == 2,
            )
            if case == "business_conflict":
                checks["hotel_requeried"] = len(providers["hotel_search"].calls) == 2
                checks["four_model_calls"] = len(rows) == 4
            else:
                checks.update(
                    hotel_once=len(providers["hotel_search"].calls) == 1,
                    three_model_calls=len(rows) == 3,
                )
    report = {
        "case": case,
        "query": query,
        "live_model": True,
        "live_rag": not case.startswith("tourism_"),
        "supplier_mode": "real" if live_suppliers else "simulation",
        "supplier_request_count": run.external_request_count,
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "latency_ms": elapsed,
        "model_calls": len(rows),
        "stage_counts": dict(Counter(r["stage"] for r in rows)),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in rows),
        "token_usage_complete": checks["usage_available"],
        "tools": dict(counts),
        "rag_search_count": len(timed.calls),
        "rag_latency_ms": sum(r["latency_ms"] for r in timed.calls),
        "checks": checks,
        "expected_behavior_success": all(checks.values()),
        "optimization_success": all(
            value
            for key, value in checks.items()
            if key
            in {
                "no_proposal_repair",
                "trains_not_replayed",
                "hotel_once",
                "hotel_requeried",
                "two_main_calls",
                "three_model_calls",
                "four_model_calls",
            }
        ),
        "task_success": result.get("status") == "completed"
        and bool(validation and validation["valid"]),
        "final_answer": result.get("final_answer"),
        "model_usage": rows,
    }
    report["total_tokens"] = report["input_tokens"] + report["output_tokens"]
    save(directory / "report.json", report)
    await raw.client.close()
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "case",
                    "status",
                    "task_success",
                    "expected_behavior_success",
                    "model_calls",
                    "total_tokens",
                    "latency_ms",
                    "checks",
                )
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


async def main(output, cases):
    for case in cases:
        if (output / case / "report.json").exists():
            raise FileExistsError("Preserve previous measurement: " + str(output / case))
    output.mkdir(parents=True, exist_ok=True)
    rag, setup = build_rag(output)
    settings = get_settings()
    depart = (datetime.now(TZ).date() + timedelta(days=2)).isoformat()
    back = (datetime.now(TZ).date() + timedelta(days=4)).isoformat()
    route = (
        f"请规划{depart}重庆到上海，{back}上海返回重庆的行程，1人，"
        "去程当天入住、返程当天离店，火车二等座，单程700元以内。"
    )
    business = (
        route + "这是星桥信息普通员工出差，优先全季，请检索公司当前差旅制度。"
        "无超标审批编号，不能采用已废止900元标准或未批准800元例外。"
        "酒店必须取得含税报价并符合当前上限。查到超标或没有可选报价时，"
        "请定向重新查询酒店，保留有效火车；若接口没有报价能力则如实说明缺口，不反复查询。"
    )
    reports = []
    for case, query, live in [
        (
            "tourism_live",
            route + "这是旅游，不查询企业政策，酒店只需提供地点，价格及空房未知请说明。",
            True,
        ),
        ("business_live", business, True),
        ("business_conflict", business, False),
        (
            "tourism_simulated",
            route + "这是旅游，不查询企业政策，酒店只需提供地点，价格及空房未知请说明。",
            False,
        ),
        ("business_simulated", business, False),
        ("business_api_simulated", business, False),
    ]:
        if case not in cases:
            continue
        providers = (
            {
                "train_search": JuheTrainProvider(settings.juhe_train_api_key),
                "hotel_search": AmapHotelProvider(settings.amap_api_key),
            }
            if live
            else {
                "train_search": ApiShapedTrainProvider(),
                "hotel_search": ApiShapedHotelProvider()
                if case.startswith("tourism_") or case == "business_api_simulated"
                else ConflictHotel(conflict=case == "business_conflict"),
            }
        )
        print("RUN " + case, flush=True)
        reports.append(
            await run_case(case, query, output / case, rag, providers, live_suppliers=live)
        )
        save(
            output / "summary.json",
            {
                "model": LLM_CONFIG["model_name"],
                "generation": get_model_generate_kwargs(),
                "created_at": datetime.now(TZ).isoformat(),
                "rag_setup": setup,
                "runs": reports,
                "total_tokens": sum(r["total_tokens"] for r in reports),
            },
        )
    rag.milvus_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/evals/2026-10-13-live-policy-measurement")
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=[
            "tourism_live",
            "business_live",
            "business_conflict",
            "tourism_simulated",
            "business_simulated",
            "business_api_simulated",
        ],
        default=["tourism_simulated", "business_simulated", "business_conflict"],
    )
    args = parser.parse_args()
    asyncio.run(main(ROOT / args.output, args.cases))
