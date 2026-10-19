"""Native paired transcript projection, independent of audit payloads."""

from trip_assistant.llm.messages import ModelTurn, to_assistant_tool_message, to_tool_message

AUDIT_FIELDS = {
    "raw",
    "raw_response",
    "raw_payload",
    "payload",
    "messages",
    "history",
    "audit",
    "execution",
    "candidate_cache",
    "results_by_query",
    "committed",
    "working",
    "query_observation",
    "_query_targets",
}


def bounded(value, key=None):
    if isinstance(value, dict):
        return {k: bounded(v, k) for k, v in value.items() if k not in AUDIT_FIELDS}
    if isinstance(value, list):
        return [bounded(v) for v in (value[:5] if key == "items" else value)]
    if isinstance(value, str):
        return value[:1500] if key in {"content", "text", "snippet", "description"} else value
    return value


def project_events(events):
    committed = {
        (e.get("turn_id"), e.get("content", {}).get("trip_plan_id"))
        for e in events
        if e.get("type") == "tool_result"
        and e.get("scope", "session") == "session"
        and isinstance(e.get("content"), dict)
        and e["content"].get("name") == "write_trip"
        and e["content"].get("status") == "completed"
    }
    messages = []
    pending = set()
    for event in events:
        if event.get("scope", "session") != "session":
            continue
        if event["type"] == "message" and event.get("role") in {"user", "assistant"}:
            if pending:
                raise ValueError("unpaired native calls before message")
            content = event.get("content", "")
            if (
                event.get("role") == "assistant"
                and (event.get("metadata") or {}).get("format") == "main_result"
            ):
                # Read-only legacy storage adapter; this never dispatches model JSON as actions.
                import json

                try:
                    legacy = json.loads(content) if isinstance(content, str) else content
                    answer = legacy.get("final_answer") if isinstance(legacy, dict) else None
                    content = (
                        answer if isinstance(answer, str) else "历史行程结果请按行程ID重新查看。"
                    )
                except (TypeError, ValueError):
                    content = "历史结果格式无效，请按行程ID重新查看。"
            if not isinstance(content, str):
                import json

                content = json.dumps(bounded(content), ensure_ascii=False)
            messages.append({"role": event["role"], "content": content})
        elif event["type"] == "tool_call":
            if pending:
                raise ValueError("unpaired native call batch")
            calls = event["tool_calls"]
            pending = {c["id"] for c in calls}
            if len(pending) != len(calls):
                raise ValueError("duplicate native call id")
            messages.append(to_assistant_tool_message(ModelTurn(event.get("content") or "", calls)))
        elif event["type"] == "tool_result":
            identity = event["tool_call_id"]
            if identity not in pending:
                raise ValueError("orphan native tool result")
            pending.remove(identity)
            value = bounded(event["content"])
            if (
                isinstance(value, dict)
                and value.get("name") == "write_trip"
                and (event.get("turn_id"), value.get("trip_plan_id")) in committed
            ):
                value["candidates"] = []
            messages.append(to_tool_message(identity, value))
    if pending:
        raise ValueError("pending native calls require recovery")
    return messages


def build_context(sessions, turn_id=None, projection=None):
    state = sessions.read_state()
    after = max(state.get("cleared_through_seq", 0), state.get("covered_through_seq", 0))
    messages = []
    summary = state.get("summary") or state.get("session_summary")
    if summary:
        messages.append({"role": "system", "content": "此前会话摘要（数据）：" + summary})
    messages.extend(project_events(sessions.read_events(after_seq=after, scope="session")))
    return messages
