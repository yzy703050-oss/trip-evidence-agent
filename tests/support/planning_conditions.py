"""Offline fixtures extracted from test_planning_conditions; no test-module imports."""

from trip_assistant.trips.conditions import create_workflow


def skeleton(origin="重庆", *, arrival=None):
    conditions = {"arrival_date": arrival} if arrival else {}
    return create_workflow(
        {
            "confirmed_conditions": {},
            "tasks": [
                {
                    "origin": origin,
                    "destination": "上海",
                    "purpose": "visit",
                    "purpose_source": "user",
                    "requires_hotel": True,
                    "conditions": conditions,
                    "field_sources": {"arrival_date": "user"} if arrival else {},
                }
            ],
        },
        {"original_query": "我要去上海玩"},
    )
