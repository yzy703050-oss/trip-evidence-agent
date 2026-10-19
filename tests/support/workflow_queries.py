"""Offline fixtures extracted from test_workflow_queries; no test-module imports."""

from trip_assistant.trips.evidence import record_query

SOURCE = {
    "provider": "offline",
    "fetched_at": "2026-10-10T10:00:00+08:00",
    "url": "https://example.com",
}


def train_result(task, count=8):
    p = {
        "origin": task["origin"],
        "destination": task["destination"],
        "departure_date": task["conditions"]["departure_date"],
        "passengers": 1,
    }
    return {
        "status": "ok",
        "query": p,
        "source": SOURCE,
        "fetched_at": SOURCE["fetched_at"],
        "missing_fields": [],
        "items": [
            {
                "id": f"{p['departure_date']}:G{i}",
                "train_number": f"G{i}",
                "origin_station": p["origin"],
                "destination_station": p["destination"],
                "departure_time": "08:00",
                "arrival_time": "12:00",
                "seat_class": "二等座",
                "price_cny": "100",
                "availability": "available",
                "remaining": 8,
                "source": SOURCE,
                "url": None,
                "departure_at": p["departure_date"] + "T08:00:00+08:00",
                "arrival_at": p["departure_date"] + "T12:00:00+08:00",
                "time_evidence": {"departure": "provider_datetime", "arrival": "provider_datetime"},
            }
            for i in range(count)
        ],
    }


def put_train(w, index=0):
    t = w["tasks"][index]
    data = train_result(t)
    return record_query(w, t["id"], data["query"], {}, data, {}, domain="train")
