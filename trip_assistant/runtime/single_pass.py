"""One-decision context and factual receipts, using the existing plan lifecycle."""

from copy import deepcopy

from trip_assistant.memory.planning import review_payload
from trip_assistant.presentation.facts import trip_results, valid_save_receipt


def planning_context(sessions, turn, service):
    state = sessions.read_state()
    identities = list(turn.get("review_trip_plan_ids") or [])
    if turn.get("review_trip_plan_id"):
        identities.append(turn["review_trip_plan_id"])
    events = sessions.read_events(scope="session")
    previous = next((e for e in reversed(events) if e.get("final") and e.get("trip_plan_refs")), {})
    if not identities:
        identities = [r["id"] for r in previous.get("trip_plan_refs", [])]
    plans = []
    for identity in dict.fromkeys(identities):
        payload = review_payload(sessions, turn, identity)
        if payload:
            if service:
                payload["tripplan"]["original_query"] = (service.store.load(identity) or {}).get(
                    "original_query", ""
                )
            plans.append(payload["tripplan"])
    # Policy is already understood evidence; new policy retrieval would require a new decision.
    policy = turn.get("policy_context") or None
    if not policy and len(plans) == 1:
        policy = plans[0].get("policy_context")
        if policy:
            turn["policy_context"] = deepcopy(policy)
    return {
        "original_query": turn["original_query"],
        "previous_summary": state.get("summary", ""),
        "recent_trips": service.store.list_summaries()[:5] if service else [],
        "current_plans": plans,
        "policy_context": policy,
    }


def decision_reply(decision, results):
    """Show model prose only for successful writes; label working plans as estimates."""
    calls = decision.get("tool_calls", [])
    latest = {}
    for row in trip_results(results):
        if row.get("trip_plan_id"):
            latest[row["trip_plan_id"]] = row
    writes = [c for c in calls if c["name"] == "write_trip"]
    by_call = {r.get("call_id"): r for r in results}
    saved = [
        c
        for c in writes
        if (r := by_call.get(c["id"], {})).get("trip_plan_id")
        and type(r.get("revision")) is int
        and r.get("status") in {"partial", "completed", "needs_input"}
    ]
    if writes and len(saved) != len(writes):
        return (
            "本次部分行程草案已保存，其余修改未保存。"
            if saved
            else "本次行程修改未保存，原有安排保留。"
        ) + _failures(results)
    if writes:
        prose = "\n\n".join(c["arguments"].get("final_answer", "") for c in saved).strip()
        verified = bool(latest) and all(valid_save_receipt(r) for r in latest.values())
        receipt = (
            "本次安排已通过数据与证据校验并保存。"
            if verified
            else "估算行程草案已保存；尚未完成车次选择或全部证据核实。"
        )
        independent = [
            _query_receipt(view)
            for result in results
            if result.get("name") == "query"
            for view in result.get("data", [])[
                : result.get("independent_count", len(result.get("data", [])))
            ]
        ]
        return (
            receipt
            + ("\n\n" + prose if prose else "")
            + ("\n\n查询事实（尚未选定）：\n" + "\n".join(independent) if independent else "")
            + _failures(results)
        )
    if calls:
        # Results did not exist when the model spoke. Present facts, not a fabricated summary.
        facts = []
        for result in results:
            if result.get("name") == "query":
                for view in result.get("data", []):
                    facts.append(_query_receipt(view))
            elif result.get("name") == "read" and result.get("status") == "ok":
                facts.append(_read_receipt(result.get("data", {})))
            elif result.get("name") == "write_memory" and result.get("status") == "ok":
                facts.append("长期偏好已保存。")
        return "\n".join(facts) + _failures(results) or "本次工具未返回可用结果。"
    return decision.get("text", "").strip() or "本次未获得完整模型结果，未重新生成，原有进度保留。"


def _query_receipt(view):
    label = {"train": "火车", "hotel": "酒店", "weather": "天气", "guide": "攻略"}.get(
        view.get("domain"), "资料"
    )
    rows = view.get("items", [])
    status = {"ok": "已返回", "partial": "部分返回"}.get(view.get("status"), "尚未核实")
    lines = [f"{label}查询{status}，本次展示{len(rows)}条结果。"]
    for row in rows:
        values = [
            str(row[k])
            for k in (
                "train_number",
                "origin_station",
                "destination_station",
                "departure_at",
                "arrival_at",
                "seat_class",
                "hotel_name",
                "address",
                "title",
                "snippet",
            )
            if row.get(k) is not None
        ]
        if row.get("price_cny") is not None:
            values.append(str(row["price_cny"]) + "元")
        if values:
            lines.append("，".join(values))
    if view.get("message"):
        lines.append(view["message"])
    return "\n".join(lines)


def _read_receipt(data):
    lines = []
    if data.get("summary"):
        lines.append(data["summary"])
    for summary in data.get("summaries", []):
        lines.append(str(summary.get("summary") or summary.get("title") or "已保存行程"))
    for task in data.get("tasks", []):
        conditions = task.get("conditions", {})
        lines.append(" → ".join(str(task.get(k) or "待定") for k in ("origin", "destination")))
        for key in ("departure_date", "check_in", "check_out"):
            if conditions.get(key):
                label = {"departure_date": "出发", "check_in": "入住", "check_out": "退房"}[key]
                lines.append(label + "：" + conditions[key])
        for activity in task.get("content", []):
            lines.append(
                " ".join(
                    str(activity.get(k) or "")
                    for k in (
                        "date",
                        "time",
                        "purpose",
                        "description",
                    )
                ).strip()
            )
    for entry in data.get("entries", []):
        if isinstance(entry.get("content"), str):
            lines.append(entry["content"])
    return "\n".join(lines) or "当前没有可展示的相关记录。"


def _failures(results):
    lines = []
    latest = {}
    for result in trip_results(results):
        if result.get("trip_plan_id"):
            latest[result["trip_plan_id"]] = result
    if any(
        i.get("code") in {"policy_unverified", "policy_constraint_unverified"}
        for result in latest.values()
        for i in (result.get("validation") or {}).get("issues", [])
    ):
        lines.append("企业政策尚未核实，不能确认合规。")
    for result in results:
        if result.get("status") in {"error", "rejected", "unknown", "unavailable"}:
            lines.append("有操作未完成：" + str(result.get("reason") or "未取得有效结果。"))
        for view in result.get("data", []) if isinstance(result.get("data"), list) else []:
            if view.get("status") not in {"ok", "partial"}:
                lines.append("有查询未核实：" + str(view.get("message") or view.get("status")))
    return "\n\n" + "\n".join(lines) if lines else ""
