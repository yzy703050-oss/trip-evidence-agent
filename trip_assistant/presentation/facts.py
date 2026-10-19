"""Structured execution facts; bound replies read the actual write receipt."""

import json


def valid_save_receipt(result):
    return (
        result.get("name") == "write_trip"
        and result.get("status") == "completed"
        and bool(result.get("trip_plan_id"))
        and type(result.get("revision")) is int
        and result["revision"] > 0
        and (result.get("validation") or {}).get("valid") is True
    )


def trip_results(results):
    return [
        r if r.get("name") == "write_trip" else {"name": r["name"], **r["trip"]}
        for r in results
        if r.get("name") == "write_trip"
        or r.get("name") in {"query", "trip_query"}
        and r.get("trip")
    ]


def turn_status(results, stop_reason):
    trips = {r["trip_plan_id"]: r for r in trip_results(results) if r.get("trip_plan_id")}
    failures = {"error", "rejected", "partial", "needs_input", "unknown", "unavailable"}
    independent = {}
    other_failures = []
    policy_failed = False
    query_batch_failed = False
    for result in results:
        if result.get("name") == "query":
            data = result.get("data", [])
            if result.get("trip"):
                data = data[: result.get("independent_count", 0)]
            elif result.get("trip_query"):
                continue
            elif not data:
                query_batch_failed = result.get("status") in failures
            if data:
                query_batch_failed = False
            for view in data:
                key = json.dumps(
                    [view.get("domain"), view.get("query", view.get("parameters"))],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                independent[key] = view.get("status")
        elif result.get("name") == "query_policy":
            policy_failed = result.get("status") in failures
        elif result.get("name") not in {"write_trip", "trip_query"} or not trips:
            other_failures.append(result.get("status") in failures)
    unresolved = (
        any(r.get("status") in failures for r in trips.values())
        or any(s in failures for s in independent.values())
        or any(other_failures)
        or policy_failed
        or query_batch_failed
    )
    return "partial" if stop_reason or unresolved else "completed"


def execution_facts(results):
    writes = trip_results(results)
    latest = writes[-1] if writes else {}
    return {
        "saved": valid_save_receipt(latest),
        "draft_saved": bool(latest.get("trip_plan_id"))
        and type(latest.get("revision")) is int
        and latest.get("status") in {"partial", "completed", "needs_input"},
        "trip_plan_id": latest.get("trip_plan_id"),
        "revision": latest.get("revision"),
        "status": latest.get("status"),
        "validation_issues": (latest.get("validation") or {}).get("issues", []),
        "errors": [
            {k: r[k] for k in ("name", "status", "error", "reason") if k in r}
            for r in results
            if r.get("status") in {"error", "rejected", "unknown"}
        ],
    }


def finalize(answer, tool_results, *, context=None):
    issues = [
        "invalid_save_receipt"
        for r in tool_results
        if r.get("name") == "write_trip"
        and r.get("status") == "completed"
        and not valid_save_receipt(r)
    ]
    return {
        "answer": answer,
        "method": "main_agent",
        "issues": sorted(set(issues)),
        "execution_facts": execution_facts(tool_results),
    }
