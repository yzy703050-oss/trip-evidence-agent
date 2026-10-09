"""Score recorded V0 runs against a small, reviewable case set.

This is a deterministic baseline. It does not claim that keyword checks prove
semantic answer quality; a model judge may be added separately for that.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from context.session_store import SessionStore
from travel_data.plan_guard import guard_itinerary


def _answer_text(stage_events: list[dict[str, Any]]) -> str:
    """Use answer fields the CLI presents, excluding hidden retrieval snippets."""
    answers = []
    for event in stage_events:
        result = event.get("content") or {}
        data = result.get("data", {}) if isinstance(result, dict) else {}
        if not isinstance(data, dict):
            continue
        for key in ("answer", "summary", "output", "message"):
            value = data.get(key)
            if isinstance(value, str):
                answers.append(value)
    return "\n".join(answers)


def _rag_documents(stage_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documents = []
    for event in stage_events:
        if event.get("agent_name") != "rag_knowledge":
            continue
        result = event.get("content") or {}
        data = result.get("data", {}) if isinstance(result, dict) else {}
        if isinstance(data, dict):
            documents.extend(
                item for item in data.get("retrieved_documents", []) if isinstance(item, dict)
            )
    return documents


def _sum_known(records: list[dict[str, Any]], key: str) -> int | float | None:
    values = [record.get(key) for record in records]
    return sum(values) if values and all(value is not None for value in values) else None


def _contains_facts(text: str, facts: list[str]) -> bool:
    normalized = re.sub(r"\s+", "", text).casefold()
    return all(re.sub(r"\s+", "", fact).casefold() in normalized for fact in facts)


def _budget_component_bounds(text: str) -> tuple[int, int] | None:
    """Sum stated component minima and maxima after the overall estimate's colon."""
    _, separator, components = text.partition("：")
    if not separator:
        _, separator, components = text.partition(":")
    if not separator:
        return None
    minima = []
    maxima = []
    for component in re.split(r"[，,；;]", components):
        amount = re.search(r"(?<!\d)(\d+)\s*(?:[-–—~至到]\s*(\d+))?\s*元", component)
        if amount:
            minima.append(int(amount.group(1)))
            maxima.append(int(amount.group(2) or amount.group(1)))
    return (sum(minima), sum(maxima)) if len(maxima) >= 2 else None


def sourced_output_valid(stages: list[dict[str, Any]]) -> bool:
    """Reject plan fields or factual output that cannot be reproduced from sources."""
    rows = [{"agent_name": stage.get("agent_name"), "result": stage.get("content", {})} for stage in stages]
    for stage in stages:
        name = stage.get("agent_name")
        if name in {"train_search", "hotel_search", "travel_guide"}:
            data = stage.get("content", {}).get("data", {})
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    return False
                kind = "train" if name == "train_search" else "hotel"
                grounded = guard_itinerary({f"selected_{kind}_id": item.get("id")}, rows)
                if name == "travel_guide":
                    if item not in grounded["guide_facts"]:
                        return False
                elif grounded.get(f"selected_{kind}") != item:
                    return False
        if name == "itinerary_planning":
            data = stage.get("content", {}).get("data", {})
            if data != guard_itinerary(data, rows):
                return False
    return True


def _stage_success(event: dict[str, Any]) -> bool:
    data = event.get("content", {}).get("data", {})
    return (event.get("status") == "success" and "error" not in data
            and (event.get("agent_name") not in {"train_search", "hotel_search", "travel_guide"}
                 or data.get("status") == "ok"))


def evaluate_case(
    case: dict[str, Any],
    store: SessionStore,
    *,
    judge: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate the last completed run, or the case's explicit turn_id."""
    events = store.read_events()
    runs = store.read_runs()
    query_runs = [record for record in runs if record.get("type") == "query_run"]
    turn_id = case.get("turn_id") or (query_runs[-1].get("turn_id") if query_runs else None)
    if not turn_id:
        raise ValueError("No recorded turn exists for this evaluation case")
    turn_events = [event for event in events if event.get("turn_id") == turn_id]
    plan_records = [
        record
        for record in runs
        if record.get("type") == "agent_plan" and record.get("turn_id") == turn_id
    ]
    planned = (
        [item.get("agent_name") for item in plan_records[-1].get("agents", [])]
        if plan_records
        else []
    )
    stages = [event for event in turn_events if event.get("type") == "stage_complete"]
    actual = [event.get("agent_name") for event in stages]
    expected = case.get("expected_agents", [])
    checks: dict[str, bool | None] = {
        "planned_route": Counter(planned) == Counter(expected),
        "executed_route": Counter(actual) == Counter(expected),
        "plan_execution_match": bool(plan_records) and Counter(actual) == Counter(planned),
        "execution_success": all(_stage_success(event) for event in stages),
    }
    if any(event.get("agent_name") in {"itinerary_planning", "train_search", "hotel_search", "travel_guide"} for event in stages):
        checks["sourced_realtime_output"] = sourced_output_valid(stages)
    answer = _answer_text(stages)
    if "memory_facts" in case:
        checks["memory_recall"] = _contains_facts(answer, case["memory_facts"])
    if "answer_facts" in case:
        checks["answer_facts"] = _contains_facts(answer, case["answer_facts"])
    docs = _rag_documents(stages)
    if "rag_gold_sources" in case:
        sources = {item.get("metadata", {}).get("parent_doc") for item in docs[:3]}
        checks["rag_recall_at_3"] = all(source in sources for source in case["rag_gold_sources"])
    if "answer_facts" in case and "rag_gold_sources" in case:
        evidence = "\n".join(str(item.get("content", "")) for item in docs[:3])
        checks["rag_grounding"] = _contains_facts(evidence, case["answer_facts"])
    itinerary = next(
        (
            event.get("content", {}).get("data", {}).get("itinerary", {})
            for event in stages
            if event.get("agent_name") == "itinerary_planning"
        ),
        {},
    )
    if "itinerary_days" in case:
        checks["itinerary_days"] = len(itinerary.get("daily_plans", [])) == case["itinerary_days"]
    if "itinerary_hotel_brand" in case:
        checks["itinerary_hotel_brand"] = _contains_facts(
            json.dumps(itinerary, ensure_ascii=False), [case["itinerary_hotel_brand"]]
        )
    budget_lower = None
    budget_upper = None
    if "budget_limit_yuan" in case:
        rows = [{"agent_name": stage.get("agent_name"), "result": stage.get("content", {})} for stage in stages]
        plan = next((stage.get("content", {}).get("data", {}) for stage in stages if stage.get("agent_name") == "itinerary_planning"), {})
        budget = guard_itinerary(plan, rows)["budget"]
        if budget["complete"]:
            budget_lower = budget_upper = float(budget["known_subtotal_cny"])
        checks["budget_feasible_within_limit"] = budget_lower is not None and budget_lower <= case["budget_limit_yuan"]
        checks["budget_components_within_limit"] = budget_upper is not None and budget_upper <= case["budget_limit_yuan"]
    judge_reason = None
    if judge is not None:
        question = next(
            (
                event.get("content", "")
                for event in turn_events
                if event.get("scope") == "session"
                and event.get("type") == "message"
                and event.get("role") == "user"
            ),
            "",
        )
        verdict = judge(
            {"question": question, "answer": answer, "retrieved_documents": docs, "case": case}
        )
        checks["model_judge"] = verdict.get("passed") is True
        judge_reason = verdict.get("reason")
    latest_run = next(
        (record for record in reversed(query_runs) if record.get("turn_id") == turn_id), {}
    )
    end_seq = latest_run.get("run_seq", 0)
    prior_seq = max(
        (record["run_seq"] for record in query_runs if record["run_seq"] < end_seq), default=0
    )
    turn_runs = [record for record in runs if prior_seq < record.get("run_seq", 0) <= end_seq]
    model_calls = [record for record in turn_runs if record.get("type") == "model_call"]
    retries = [
        record
        for record in turn_runs
        if record.get("type") == "retry" and record.get("turn_id") == turn_id
    ]
    metrics = {
        "model_calls": len(model_calls),
        "latency_ms": latest_run.get("latency_ms"),
        "input_tokens": _sum_known(model_calls, "input_tokens"),
        "output_tokens": _sum_known(model_calls, "output_tokens"),
        "estimated_cost_usd": _sum_known(model_calls, "estimated_cost_usd"),
        "observed_retries": len(retries),
        "budget_component_lower_yuan": budget_lower,
        "budget_component_upper_yuan": budget_upper,
        "failed_agents": [
            event.get("agent_name") for event in stages if not _stage_success(event)
        ],
    }
    return {
        "case_id": case["id"],
        "user_id": store.user_id,
        "session_id": store.session_id,
        "turn_id": turn_id,
        "checks": checks,
        "passed": all(value is True for value in checks.values()),
        "metrics": metrics,
        "judge_reason": judge_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate an existing V0 session log")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, default=Path("data/memory"))
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = [cases]
    store = SessionStore(args.storage_root, args.user_id, args.session_id)
    results = [evaluate_case(case, store) for case in cases]
    print(
        json.dumps(
            {"passed": all(item["passed"] for item in results), "cases": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
