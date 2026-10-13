"""Explicit memory commands: ordinary programs, never a model fallback."""

import re
from sqlite3 import Error as SQLiteError


def parse_memory_command(text):
    text = text.strip()
    if text in {"查看我的历史行程", "查询历史行程", "我的历史行程", "查看旅行历史"}:
        return {"updates": [], "history": True, "remaining_query": ""}
    if text in {"查看我的偏好", "查询偏好", "我的偏好", "读取偏好", "查看出行偏好"}:
        return {"updates": [], "read": True, "remaining_query": ""}
    match = re.fullmatch(r"(删除|撤回)偏好\s+(memory_\w+)", text)
    if match:
        return {"updates": [{"op": "retract", "id": match[2]}], "remaining_query": ""}
    match = re.fullmatch(r"(?:更新|修改)偏好\s+(memory_\w+)\s*[:：]\s*(.+)", text)
    if match:
        return {
            "updates": [{"op": "replace", "id": match[1], "text": match[2]}],
            "remaining_query": "",
        }
    if text in {"修改我的偏好", "修改偏好", "删除我的偏好"}:
        return {"updates": [], "missing_fields": ["memory_id", "text"], "remaining_query": ""}
    if re.match(r"^(以后|今后|从今往后|记住[：:，,]?|我的长期偏好[：:])", text):
        pieces = re.split(
            r"[，,；;。]\s*(?=(?:然后|并且|顺便|并)?(?:帮我|请)?(?:规划|安排|查询|查|搜|看看))",
            text,
            maxsplit=1,
        )
        return {
            "updates": [{"op": "add", "text": pieces[0]}],
            "remaining_query": pieces[1] if len(pieces) == 2 else "",
        }
    return None


def read_memory(memory, request):
    store = getattr(memory, "long_term", memory)
    if store is None:
        return {"memories": [], "legacy_preferences": {}, "analysis": {}}
    profile = store.data
    result = {
        "memories": [m for m in profile.get("travel_memories", []) if m["status"] == "active"],
        "legacy_preferences": store.get_preference(),
        "analysis": profile.get("preference_analysis", {}),
    }
    if request.get("history"):
        result["history"] = store.get_trip_history(limit=request.get("limit", 5))
    return result


def apply_preference_updates(memory, updates):
    store = getattr(memory, "long_term", memory)
    try:
        if store is None:
            raise OSError("memory unavailable")
        result = store.apply_memory_updates(updates)
        return {
            "status": "ok" if not result["rejected"] else "partial",
            **result,
            "effective_memories": read_memory(memory, {})["memories"],
        }
    except (OSError, ValueError, SQLiteError):
        return {"status": "error", "applied": [], "rejected": updates, "effective_memories": []}
