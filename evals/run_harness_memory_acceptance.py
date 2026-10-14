"""Live configured model, real sequential turns, simulated production suppliers."""

import asyncio
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.openai_model import OpenAIChatModel  # noqa: E402
from config import LLM_CONFIG, get_model_generate_kwargs  # noqa: E402
from evals import harness_eval_support as support  # noqa: E402
from evals.api_shaped_simulation import FixtureResponse, hotel_payload  # noqa: E402
from utils.runtime_logging import configure_runtime_logging  # noqa: E402


async def supplemental_checks(output):
    """Controlled cache replay and labelled 20k boundary, separate from dialogue."""
    import json

    from agents.contracts import RunState
    from agents.main_agent import MainAgent
    from context.context_budget import BudgetedModel, count_input_tokens
    from context.memory_manager import MemoryManager
    from context.telemetry import MeteredModel, model_scope, model_stage
    from travel_data.batch_execution import execute_query_batch
    from travel_data.candidates import CandidateStore
    from travel_data.tools import ToolExecutor

    support.OUT = Path(output)
    support.CHECKS = json.loads((support.OUT / "checks.json").read_text(encoding="utf-8"))
    prior = json.loads((support.OUT / "metrics.json").read_text(encoding="utf-8"))
    configure_runtime_logging()
    run = RunState("controlled-cache")
    run.candidates = CandidateStore()
    provider = OpaqueHotels()
    executor = ToolExecutor({"hotel_search": provider})
    audits = []
    for phase in ["cold", "warm"]:
        start = len(run.tool_requests)
        external_before = run.external_request_count
        for index, offset in enumerate([0, 5, 10, 5, 0]):
            await execute_query_batch(
                {
                    "requests": [
                        {
                            "request_id": f"{phase}-{index}",
                            "name": "hotel_search",
                            "domain": "hotel",
                            "parameters": {
                                "city": "上海",
                                "keywords": "酒店",
                                "candidate_offset": offset,
                            },
                        }
                    ]
                },
                run,
                executor,
            )
        requests = run.tool_requests[start:]
        audits.append(
            {
                "phase": phase,
                "mode": "controlled executor replay, not model decisions",
                "requests": requests,
                "cache_hits": sum(r.get("cache_hit") is True for r in requests),
                "external_requests": run.external_request_count - external_before,
            }
        )
    support.save("controlled-cache-audit.json", audits)
    support.check(
        "controlled_cache_cold_80_warm_100",
        audits[0]["cache_hits"] == 4
        and audits[0]["external_requests"] == 1
        and audits[1]["cache_hits"] == 5
        and audits[1]["external_requests"] == 0,
        audits,
    )
    raw = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 60},
        generate_kwargs=get_model_generate_kwargs(),
    )
    model = support.RecordingModel(raw)
    model.rows = json.loads((support.OUT / "model-calls.json").read_text(encoding="utf-8"))
    model.turn_start = len(model.rows)
    memory = MemoryManager(
        "boundary-eval",
        "20k-" + support.NOW.strftime("%H%M%S"),
        str(support.OUT / "boundary-memory"),
    )
    # Replay six actually generated earlier turns; there are no new API calls
    # for replay, and artificial fixed padding is explicitly a boundary fixture.
    rows = json.loads((support.OUT / "turn-summary.json").read_text(encoding="utf-8"))
    selected = [r for r in rows if r["session"] == "continuous"][:6]
    for row in selected:
        turn = memory.start_turn(row["query"])
        memory.record_message("assistant", row["answer"], turn, final=True)
    query = "直接回忆最新去程、返程、人数、晚数及最晚到达时刻；房价是否已知。不要查询或修改。"
    memory.start_turn(query)
    wrapped = BudgetedModel(
        MeteredModel(model, memory.session_store.append_run), budget_tokens=20000
    )
    memory.llm_model = wrapped
    main_agent = MainAgent(wrapped, memory_manager=memory)
    context = {"original_query": query, "reference_padding": ""}
    prompt = (
        "以下JSON是背景数据，reference_padding仅占位；"
        "保留会话最新条件，按本轮请求回答。\n__MAIN_CONTEXT__"
    )

    def build(view):
        return main_agent._messages(prompt, view)

    current = count_input_tokens(build(main_agent._refresh_history(context))).tokens
    context["reference_padding"] = "中" * max(0, 16500 - current)
    before = count_input_tokens(build(main_agent._refresh_history(context))).tokens
    try:
        with model_stage("main:boundary_recall"), model_scope(turn_id=memory.current_turn_id):
            response = await main_agent._request(build, context=context, stage="main")
        after = count_input_tokens(build(main_agent._refresh_history(context))).tokens
        state = memory.session_store.read_state()
        support.save(
            "20k-boundary.json",
            {
                "mode": "replayed genuine history plus artificial fixed padding; boundary only",
                "budget": 20000,
                "trigger": 16000,
                "before_tokens": before,
                "after_tokens": after,
                "covered_through_seq": state.get("covered_through_seq"),
                "answer": response.text,
                "api_input_tokens": model.rows[-1]["usage"]["input_tokens"],
                "counting_method": "utf8_conservative estimate; not exact model tokenizer",
            },
        )
        support.check(
            "20k_boundary_real_summary_and_recall",
            before >= 16000
            and after <= 16000
            and state.get("covered_through_seq", 0) > 0
            and all(x in response.text for x in ["2026-10-21", "2026-10-25", "3", "4", "19:00"]),
            response.text,
        )
    finally:
        await raw.client.close()
        prior.update(
            real_api_calls=len(model.rows),
            input_tokens=sum((r.get("usage") or {}).get("input_tokens", 0) for r in model.rows),
            output_tokens=sum((r.get("usage") or {}).get("output_tokens", 0) for r in model.rows),
            summary_calls=sum(r.get("stage") == "main:summary" for r in model.rows),
            checks_passed=sum(c["passed"] for c in support.CHECKS),
            checks_total=len(support.CHECKS),
            controlled_cache_cold_hit_rate=audits[0]["cache_hits"] / 5,
            controlled_cache_warm_hit_rate=audits[1]["cache_hits"] / 5,
        )
        support.save("metrics.json", prior)


class OpaqueHotels(support.ManyHotels):
    def _get(self, url, *, params, timeout):
        safe = {k: v for k, v in params.items() if k != "key"}
        payload = hotel_payload(safe)
        template = payload["pois"][0]
        payload["pois"] = [
            {
                **deepcopy(template),
                "id": f"B0OPAQUE{(i * 47 + 19) % 101:04d}",
                "name": f"全季酒店（{safe['region']}模拟门店{i + 1:02d}）",
                "address": f"模拟路{i + 100}号",
            }
            for i in range(12)
        ]
        payload["count"] = "12"
        self.fixtures.append({"request": {"params": safe}, "response": deepcopy(payload)})
        return FixtureResponse(payload)


def snapshot(session):
    entries = session.memory.workflow_store.list_summaries()
    if not entries:
        return None
    return session.memory.workflow_store.load(entries[0]["id"])


def selection(workflow, domain):
    return [
        (t.get("draft_plan") or {}).get(domain + "_selection") for t in workflow.get("tasks", [])
    ]


def hotel_names(workflow):
    names = []
    for ref in selection(workflow or {}, "hotel"):
        if ref:
            row = workflow["results_by_query"][ref["query_id"]]
            names.extend(i["hotel_name"] for i in row["items"] if i["id"] == ref["candidate_id"])
    return names


def make_session(model, name, budget=20000):
    session = support.Session(model, "harness-fix-eval-" + name, name, budget=budget)
    session.hotel = OpaqueHotels()
    session.harness.runner.executor.providers["hotel_search"] = session.hotel
    session.memory.long_term.set_user_preference("home_location", "重庆")
    return session


async def main():
    support.OUT = (
        ROOT / "docs/evals/2026-10-14-harness-memory-fix" / support.NOW.strftime("run-%H%M%S")
    )
    support.OUT.mkdir(parents=True, exist_ok=True)
    log_path = configure_runtime_logging()
    support.save(
        "manifest.json",
        {
            "real_model": True,
            "supplier_mode": "simulated production adapters",
            "sequential_turns": True,
            "synthetic_history_turns": 0,
            "normal_budget": 20000,
            "continuous_memory_budget_override": 9000,
            "temperature": LLM_CONFIG["temperature"],
            "generation": get_model_generate_kwargs(),
            "runtime_log": str(log_path),
            "notes": "Gold expectations are used only for scoring.",
        },
    )
    raw = OpenAIChatModel(
        LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 60},
        generate_kwargs=get_model_generate_kwargs(),
    )
    model = support.RecordingModel(raw)
    try:
        normal = make_session(model, "normal")
        await normal.turn(
            "01_plan",
            "10月21日从重庆去上海旅游，住两晚，10月23日返回重庆。安排往返火车和酒店，二等座，酒店只需地点推荐，无需房价。",
        )
        first = snapshot(normal)
        support.check("normal_plan_saved", bool(first) and first["status"] == "completed", first)
        if first:
            original_trains = deepcopy(selection(first, "train"))
            original_conditions = [deepcopy(t["conditions"]) for t in first["tasks"]]
            await normal.turn(
                "02_replace",
                "把上次上海行程去程酒店换一家，火车和日期保持不变，使用已有候选，不要重新查询供应商。",
            )
            changed = snapshot(normal)
            support.check(
                "ordinary_replacement_actual",
                selection(first, "hotel") != selection(changed, "hotel"),
                hotel_names(changed),
            )
            await normal.turn(
                "03_target_12",
                "我明确要求实际替换：把上次上海行程去程酒店换成名称带模拟门店12的那家。火车、日期保持不变，只用完整保存缓存，不要调用供应商。",
            )
            target = snapshot(normal)
            support.check(
                "actual_target_12",
                any("模拟门店12" in n for n in hotel_names(target)),
                hotel_names(target),
            )
            support.check(
                "unmodified_train_and_dates",
                selection(target, "train") == original_trains
                and [t["conditions"] for t in target["tasks"]] == original_conditions,
                target["tasks"],
            )
            support.check(
                "modifications_zero_supplier",
                all(r["external_requests"] == 0 for r in support.ROWS[-2:]),
                support.ROWS[-2:],
            )
            before_read = deepcopy(target)
            await normal.turn(
                "04_read", "查看上次上海行程去程酒店第6条开始的5个候选，先只展示，不修改、不查询。"
            )
            support.check(
                "read_only_no_mutation",
                snapshot(normal) == before_read and support.ROWS[-1]["external_requests"] == 0,
                support.ROWS[-1],
            )

        memory = make_session(model, "continuous", budget=9000)
        for i, query in enumerate(
            [
                "请只用文字复述并记住，不创建行程，不查询，不调用工具：本次临时旅行从成都去南京，去程日期2026-10-21、返程2026-10-24，2人、住3晚。这些只属于本次对话，不保存为长期偏好。",
                "请只用文字复述并记住，不创建行程，不查询，不调用工具：本次旅行硬条件是19:00前到达南京，火车二等座；酒店仅推荐地点，房价和空房尚未查询，不允许编造。",
                "请只用文字确认更正，不创建行程，不查询，不调用工具：本次旅行改为3人，返程2026-10-25、住4晚，其他条件不变。",
            ]
        ):
            await memory.turn(f"10_fact_{i}", query)
        recall = (
            "请回忆这趟临时旅行最新出发地、目的地、去程、返程、人数、晚数、"
            "最晚到达时刻、房价是否已知。直接回答，不查询。"
        )
        await memory.turn("11_before_recall", recall)
        await memory.turn(
            "12_before_plan",
            "根据刚才本次临时旅行的最新条件，安排去程火车和南京酒店地点，不安排返程火车，严格保持日期、人数与到达条件。",
        )
        pre = snapshot(memory)
        support.check(
            "before_compression_plan_saved", bool(pre) and pre["status"] == "completed", pre
        )
        for i in range(18):
            # Every acknowledgement is a genuine model result. The background
            # is user content used for a labelled continuous load, not inserted history.
            query = (
                f"旅行准备讨论第{i + 1}项：请给出两句建议即可，暂不查询、不要更改已确认旅行条件。"
                + (
                    "我考虑带轻便行李、雨具、充电线，保管证件，预留到站时间；"
                    "沿途阅读只作消遣，酒店价格和空房仍未查询。"
                )
                * 10
            )
            await memory.turn(f"13_discussion_{i:02d}", query)
            if memory.memory.session_store.read_state().get("covered_through_seq", 0):
                break
        state = memory.memory.session_store.read_state()
        support.check(
            "automatic_compaction_on_real_turns", state.get("covered_through_seq", 0) > 0, state
        )
        await memory.turn("14_after_recall", recall)
        answer = support.ROWS[-1]["answer"] or ""
        support.check(
            "latest_corrected_facts_after_compression",
            all(
                x in answer for x in ["成都", "南京", "2026-10-21", "2026-10-25", "3", "4", "19:00"]
            )
            and any(x in answer for x in ["未知", "未查询", "尚未"]),
            answer,
        )
        if pre:
            await memory.turn(
                "15_after_modify",
                "把之前这趟南京行程的酒店换成名称带模拟门店12的那家。去程火车、所有日期和3人条件保持不变，只用已保存候选，不要调用供应商。",
            )
            post = snapshot(memory)
            support.check(
                "after_compression_actual_target",
                any("模拟门店12" in n for n in hotel_names(post)),
                hotel_names(post),
            )
            support.check(
                "after_compression_scope_preserved",
                selection(post, "train") == selection(pre, "train")
                and [t["conditions"] for t in post["tasks"]]
                == [t["conditions"] for t in pre["tasks"]],
                post["tasks"],
            )
        reopened = make_session(model, "continuous", budget=9000)
        await reopened.turn("16_restart_recall", recall)
        answer = support.ROWS[-1]["answer"] or ""
        support.check(
            "restart_latest_facts",
            all(x in answer for x in ["成都", "南京", "2026-10-25", "19:00"]),
            answer,
        )
        support.save(
            "supplier-fixtures.json",
            {"normal": normal.hotel.fixtures, "continuous": memory.hotel.fixtures},
        )
    finally:
        rows = model.rows
        tools = [t for turn in support.ROWS for t in turn["tool_requests"]]
        support.save(
            "metrics.json",
            {
                "real_api_calls": len(rows),
                "turns": len(support.ROWS),
                "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in rows),
                "output_tokens": sum((r.get("usage") or {}).get("output_tokens", 0) for r in rows),
                "summary_calls": sum(r.get("stage") == "main:summary" for r in rows),
                "tool_requests": len(tools),
                "cache_hits": sum(t.get("cache_hit") is True for t in tools),
                "external_requests": sum(t["external_requests"] for t in support.ROWS),
                "checks_passed": sum(c["passed"] for c in support.CHECKS),
                "checks_total": len(support.CHECKS),
            },
        )
        await raw.client.close()
    print("REPORT", support.OUT, flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--supplement":
        asyncio.run(supplemental_checks(sys.argv[2]))
    else:
        asyncio.run(main())
