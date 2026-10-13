from copy import deepcopy

from agents.context_projection import project_domain_result


def test_weather_duplicate_long_text_retains_city_temperature_and_full_data():
    from travel_data.result_views import build_tool_result_view

    data = {
        "status": "ok",
        "query": {"city": "上海", "date": "2026-10-13"},
        "items": [
            {"date": "2026-10-13", "temp_c": 22, "description": "晴" * 2000, "photos": "large"}
            for _ in range(7)
        ],
        "missing_fields": [],
        "fetched_at": "2026-10-13T12:00:00+08:00",
        "source": {"provider": "weather"},
    }
    before = deepcopy(data)
    view = build_tool_result_view("weather", data, limits={})
    assert len(view["items"]) == 1 and len(view["items"][0]["description"]) == 128
    assert view["items"][0]["truncated_fields"] == ["description"]
    assert view["query"]["city"] == "上海" and view["items"][0]["temp_c"] == 22
    assert data == before
    main = project_domain_result({**data, "domain": "weather"})
    assert main["items"][0]["temp_c"] == 22 and main["query"]["city"] == "上海"


def test_pagination_dedup_and_identity_fields_not_truncated():
    from travel_data.result_views import build_tool_result_view

    data = {
        "status": "ok",
        "query": {"city": "上海"},
        "items": [
            {
                "id": str(i),
                "hotel_name": "酒店" * 1000,
                "stay_total_cny": "500",
                "check_in": "2026-10-13",
                "check_out": "2026-10-14",
                "photos": "large",
            }
            for i in range(12)
        ],
        "unverified_constraints": ["inventory"],
        "source": {"provider": "hotel"},
    }
    data["items"].append(deepcopy(data["items"][0]))
    view = build_tool_result_view("hotel", data, limits={"candidate_limit": 5, "offset": 5})
    assert view["candidate_total"] == 12 and view["has_more"]
    assert [i["id"] for i in view["items"]] == ["5", "6", "7", "8", "9"]
    assert view["items"][0]["stay_total_cny"] == "500"
    assert view["unverified_constraints"] == ["inventory"]


def test_hotel_view_derives_nightly_cost_from_quote_dates_without_mutating_evidence():
    data = {
        "domain": "hotel",
        "status": "ok",
        "items": [
            {
                "id": "q",
                "hotel_name": "全季",
                "check_in": "2026-10-15",
                "check_out": "2026-10-17",
                "stay_total_cny": "760",
                "fees_included": True,
                "derived_nightly_cny": "1",
            }
        ],
    }
    before = deepcopy(data)
    view = project_domain_result(data)
    quote = view["items"][0]
    assert quote["derived_stay_nights"] == 2
    assert quote["derived_nightly_cny"] == "380"
    assert quote["stay_total_cny"] == "760" and data == before
    assert project_domain_result(view)["items"][0]["derived_nightly_cny"] == "380"


def test_hotel_view_does_not_invent_quote_or_cost_for_invalid_evidence():
    for amount, start, end in [
        (None, "2026-10-15", "2026-10-17"),
        ("NaN", "2026-10-15", "2026-10-17"),
        ("760", "bad", "2026-10-17"),
        ("760", "2026-10-17", "2026-10-15"),
    ]:
        view = project_domain_result(
            {
                "domain": "hotel",
                "items": [
                    {"id": "unknown", "stay_total_cny": amount, "check_in": start, "check_out": end}
                ],
            }
        )
        assert "derived_nightly_cny" not in view["items"][0]
        assert "derived_stay_nights" not in view["items"][0]
