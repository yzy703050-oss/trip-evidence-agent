import json
from copy import deepcopy

from trip_assistant.memory.context import project_events


def test_completed_trip_keeps_tool_pairs_without_repeating_old_candidate_pool():
    events = []
    for i, status in enumerate(["partial", "completed"]):
        events.extend(
            [
                {
                    "type": "tool_call",
                    "turn_id": "t",
                    "tool_calls": [{"id": str(i), "name": "write_trip", "arguments": {}}],
                },
                {
                    "type": "tool_result",
                    "turn_id": "t",
                    "tool_call_id": str(i),
                    "content": {
                        "name": "write_trip",
                        "trip_plan_id": "trip",
                        "status": status,
                        "candidates": [{"items": [{"id": "old-candidate"}]}] if i == 0 else [],
                        "validation": {"selected": "real-fact"} if i else None,
                    },
                },
            ]
        )
    before = deepcopy(events)
    messages = project_events(events)
    assert len(messages) == 4
    assert json.loads(messages[1]["content"])["candidates"] == []
    assert "real-fact" in messages[-1]["content"]
    assert events == before
