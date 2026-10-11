"""Model views are separate from persisted business and tool audit objects."""
from copy import deepcopy
import json


def project_session_message(event: dict) -> dict | None:
    if (event.get("scope") != "session" or event.get("type") != "message"
            or event.get("role") not in {"user", "assistant"}):
        return None
    projected = {key: deepcopy(event[key]) for key in
                 ("seq", "turn_id", "scope", "type", "role", "content", "final") if key in event}
    if event.get("role") == "assistant" and event.get("metadata", {}).get("format") == "main_result":
        try:
            result = json.loads(event["content"]) if isinstance(event["content"], str) else event["content"]
            projected["content"] = result.get("final_answer", "")
        except (ValueError, TypeError, AttributeError):
            projected["content"] = "历史答复格式无法读取"
    return projected
