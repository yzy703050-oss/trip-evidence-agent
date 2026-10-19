"""Filter summaries first; selection and exact loading remain separate."""


def discover(summaries, filters, session_id):
    allowed = {"city", "date", "start_date", "end_date", "purpose", "session_id"}
    if set(filters) - allowed:
        raise ValueError("unsupported discovery filters")
    matches = []
    for row in summaries:
        tasks = row.get("tasks", [])
        if filters.get("session_id") and row.get("session_id") != filters["session_id"]:
            continue
        if filters.get("city") and not any(
            filters["city"] in (t.get("origin"), t.get("destination")) for t in tasks
        ):
            continue
        if filters.get("purpose") and not any(
            t.get("purpose") == filters["purpose"] for t in tasks
        ):
            continue
        dates = [
            v
            for t in tasks
            for k, v in t.get("conditions", {}).items()
            if k in {"departure_date", "arrival_date", "check_in", "check_out"}
            and isinstance(v, str)
        ]
        if filters.get("date") and filters["date"] not in dates:
            continue
        if filters.get("start_date") and dates and max(dates) < filters["start_date"]:
            continue
        if filters.get("end_date") and dates and min(dates) > filters["end_date"]:
            continue
        matches.append(row)
    return sorted(
        matches,
        key=lambda r: (r.get("session_id") == session_id, r.get("updated_at", "")),
        reverse=True,
    )
