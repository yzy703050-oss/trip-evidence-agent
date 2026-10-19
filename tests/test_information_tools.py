"""Offline regression scenarios grouped by responsibility."""

import pytest
from support.weather import weather_info

from trip_assistant.providers.contracts import AgentDataResult
from trip_assistant.providers.validation import guard_domain_result

# information_tools


@pytest.mark.asyncio
async def test_weather_date_outside_response_is_incomplete():
    from trip_assistant.providers.public_query import PublicQueryProvider

    result = await PublicQueryProvider(
        weather_fetch=lambda city: {
            "current_condition": [{"temp_C": "20"}],
            "weather": [{"date": "2026-10-10", "mintempC": "18", "maxtempC": "25"}],
        }
    ).weather("北京", "2026-11-10")
    assert result.status == "partial"
    assert result.items == []


# information_agent_loop

# final_fix_connections


class final_fix_connections_Provider:
    def __init__(self, status="ok", fail=False):
        self.queries = []
        self.status = status
        self.fail = fail

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise TimeoutError("secret")
        return AgentDataResult(self.status, query.to_dict(), [], [], None, None, None)


# main_review_regressions


def test_weather_invalid_temperature_is_incomplete():
    data = weather_info()["domain_results"]["weather"]
    data["items"][0]["min_temp_c"] = float("nan")
    assert guard_domain_result("weather", data)["status"] == "partial"


def test_itinerary_cannot_silently_move_confirmed_dates():
    from trip_assistant.providers.plan_guard import guard_final_itinerary

    plan = {
        "planning_complete": True,
        "itinerary": {"daily_plans": [{"day": 1, "date": "2026-11-10"}]},
    }
    guarded = guard_final_itinerary(
        plan, {}, {"destination": "Beijing", "start_date": "2026-10-10", "end_date": "2026-10-12"}
    )
    assert guarded["planning_complete"] is False
    assert not guarded["itinerary"]["daily_plans"]
