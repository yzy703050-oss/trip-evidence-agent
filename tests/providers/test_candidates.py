from copy import deepcopy


def test_hard_filter_before_top5_and_no_hidden_default():
    from trip_assistant.providers.candidates import candidate_view

    rows = [
        {
            "id": str(i),
            "price_cny": str(i),
            "train_number": "K1",
            "arrival_at": "2026-10-20T12:00:00+08:00",
        }
        for i in range(12)
    ]
    rows.append(
        {
            "id": "fast",
            "price_cny": "100",
            "train_number": "G1",
            "arrival_at": "2026-10-20T11:00:00+08:00",
        }
    )
    result = {"status": "ok", "query": {"departure_date": "2026-10-20"}, "items": rows}
    before = deepcopy(result)
    view = candidate_view(
        result,
        constraints={"train_max_total_cny": 10},
        ranking=[{"field": "price_cny", "direction": "desc"}],
    )
    assert [r["id"] for r in view["items"]] == ["10", "9", "8", "7", "6"]
    assert view["candidate_total"] == 11 and result == before
    assert candidate_view(result, constraints={}, ranking=[])["items"][0]["id"] == "0"


def test_unknown_hard_price_is_labelled_not_proven():
    from trip_assistant.providers.candidates import candidate_view

    v = candidate_view(
        {
            "status": "ok",
            "query": {"search_kind": "hotel_place"},
            "items": [{"id": "h", "hotel_name": "酒店", "kind": "hotel_place"}],
        },
        constraints={"hotel_max_nightly_cny": 500},
        ranking=[],
    )
    assert v["status"] == "partial" and "hotel_max_nightly_cny" in v["unverified_constraints"]
    assert v["items"][0]["unverified_constraints"]


def test_arrival_deadline_filters_full_cache_and_pages_stable():
    from trip_assistant.providers.candidates import candidate_view

    rows = [
        {"id": "late" + str(i), "arrival_at": "2026-10-20T20:00:00+08:00"} for i in range(6)
    ] + [{"id": "early", "arrival_at": "2026-10-20T08:00:00+08:00"}]
    v = candidate_view(
        {"items": rows, "query": {}, "status": "ok"},
        constraints={"arrival_before": "2026-10-20T09:00:00+08:00"},
        ranking=[],
    )
    assert [r["id"] for r in v["items"]] == ["early"] and v["excluded_candidate_count"] == 6
