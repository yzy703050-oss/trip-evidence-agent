"""Trip review views over the existing per-user session and operation memory."""

import json
from copy import deepcopy
from datetime import datetime

from trip_assistant.memory.context import AUDIT_FIELDS
from trip_assistant.storage.sessions import storage_component
from trip_assistant.storage.trips import TripStore
from trip_assistant.trips.contracts import QueryItem
from trip_assistant.trips.state import planning_stage


def query_intent(item):
    normalized = QueryItem.model_validate(item).model_dump(exclude_none=True)
    normalized.pop("refresh", None)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


def retryable_query_intents(sessions, turn_id):
    events = sessions.read_events(turn_id=turn_id, scope="session")
    results = {e["tool_call_id"]: e["content"] for e in events if e["type"] == "tool_result"}
    return {
        query_intent(item)
        for event in events
        if event["type"] == "tool_call"
        for call in event["tool_calls"]
        if call["name"] == "query"
        and results.get(call["id"], {}).get("status") in {"partial", "error"}
        for item in call["arguments"].get("query_plan", [])
    }


def _tripplan(arguments):
    plan = arguments.get("tripplan")
    return plan if isinstance(plan, dict) else {}


def _summary(arguments, text=None):
    summary = arguments.get("round_summary")
    return (summary if isinstance(summary, str) else text if isinstance(text, str) else "")[:300]


def previous_round(sessions, turn_id):
    events = sessions.read_events(turn_id=turn_id, scope="session")
    call = next((e for e in reversed(events) if e["type"] == "tool_call"), None)
    if not call:
        return {"summary": "", "results": []}, {}
    calls = call["tool_calls"]
    results = {e["tool_call_id"]: e["content"] for e in events if e["type"] == "tool_result"}
    summary = "；".join(_summary(c["arguments"]) for c in calls).strip("；")
    summary = summary or call.get("content") or ""
    view = {
        "summary": summary[:300],
        "results": [
            {
                "name": c["name"],
                "call_id": c["id"],
                **(
                    {"requested_trip_plan_id": _tripplan(c["arguments"])["trip_plan_id"]}
                    if c["name"] == "write_trip"
                    and isinstance(_tripplan(c["arguments"]).get("trip_plan_id"), str)
                    else {}
                ),
                **{
                    k: v
                    for k, v in results.get(c["id"], {}).items()
                    if k
                    in {
                        "status",
                        "reason",
                        "stop_reason",
                        "trip_plan_id",
                        "revision",
                        "error",
                        "selection_saved",
                    }
                },
            }
            for c in calls
        ],
    }
    attempted = next(
        (
            _tripplan(c["arguments"])
            for c in reversed(calls)
            if c["name"] == "write_trip" and _tripplan(c["arguments"])
        ),
        {},
    )
    return view, attempted


def adjustment_history(sessions, trip_id, current_revision):
    # Native calls, paired outcomes and atomic receipts are the only source of history.
    with sessions.storage.connection() as conn:
        rows = conn.execute(
            "SELECT c.call_id,c.session_id,c.payload_json,e.timestamp,"
            "json_extract(e.payload_json,'$.content') AS text,"
            "json_extract(r.payload_json,'$.content.status') AS status,"
            "json_extract(r.payload_json,'$.content.reason') AS result_reason,"
            "json_extract(r.payload_json,'$.content.selection_saved') AS selection_saved,"
            "COALESCE(json_extract(o.result_json,'$.last_operation'),"
            "json_extract(r.payload_json,'$.content.operation')) AS applied_operation,"
            "COALESCE(json_extract(o.result_json,'$.revision'),"
            "json_extract(r.payload_json,'$.content.revision')) AS revision,"
            "json_extract(o.result_json,'$.status') AS saved_status "
            "FROM tool_calls c JOIN events e ON e.session_id=c.session_id AND e.seq=c.call_seq "
            "LEFT JOIN events r ON r.session_id=c.session_id AND r.seq=c.result_seq "
            "LEFT JOIN operation_receipts o ON o.operation_id=c.turn_id||':'||c.call_id "
            "WHERE c.scope='session' AND json_extract(c.payload_json,'$.name')='write_trip' AND ("
            "json_extract(c.payload_json,'$.arguments.tripplan.trip_plan_id')=? OR "
            "json_extract(r.payload_json,'$.content.trip_plan_id')=? OR o.trip_id=?) "
            "ORDER BY e.timestamp,c.session_id,c.call_seq,c.call_id",
            (trip_id, trip_id, trip_id),
        ).fetchall()
    history = []
    for row in rows:
        args = json.loads(row["payload_json"])["arguments"]
        plan = _tripplan(args)
        effect = (
            "not_applied"
            if row["selection_saved"] == 0
            else "committed"
            if row["saved_status"] == "completed" or row["status"] == "completed"
            else "saved_draft"
            if row["saved_status"] or row["revision"] is not None
            else "unknown"
            if row["status"] in {None, "unknown", "error"}
            else "not_applied"
        )
        history.append(
            {
                "call_id": row["call_id"],
                "timestamp": row["timestamp"],
                "session_id": row["session_id"],
                "operation": row["applied_operation"]
                or plan.get("operation")
                or ("unspecified" if plan.get("trip_plan_id") else "propose"),
                "summary": _summary(args, row["text"]),
                "reason": plan.get("reason"),
                "result_reason": row["result_reason"],
                "status": row["status"],
                "effect": effect,
                "revision": row["revision"],
                "historical_revision": row["revision"] is not None
                and row["revision"] < current_revision,
                "changes": {
                    k: v
                    for k, v in plan.items()
                    if k
                    not in {
                        "operation",
                        "trip_plan_id",
                        "expected_revision",
                        "reason",
                    }
                },
            }
        )
    return history


def _review_view(view):
    value = deepcopy(view)
    for key in ("cache_hit", "excluded_candidate_count", "returned_candidate_total"):
        value.pop(key, None)
    if value.get("sources"):
        for key in ("source", "source_id", "fetched_at"):
            value.pop(key, None)
        if len(value["sources"]) == 1:
            source_id = next(iter(value["sources"]))
            for item in value.get("items", []):
                if item.get("source_id") == source_id:
                    item.pop("source_id")
    if value.get("domain") == "train":
        core = ("id", "train_number", "departure_at", "arrival_at", "seat_class", "price_cny")
        rows = []
        for item in value.get("items", []):
            # Keep the actual identity beside the facts used in selection and prose.
            row = {key: item[key] for key in core if key in item}
            row.update({key: val for key, val in item.items() if key not in row})
            for short, full in (("departure_time", "departure_at"), ("arrival_time", "arrival_at")):
                try:
                    stamp = datetime.fromisoformat(row.get(full))
                except (TypeError, ValueError):
                    continue
                row[full.removesuffix("_at") + "_period"] = (
                    "凌晨"
                    if stamp.hour < 6
                    else "上午"
                    if stamp.hour < 12
                    else "下午"
                    if stamp.hour < 18
                    else "晚上"
                )
                if row.get(short) == stamp.strftime("%H:%M"):
                    row.pop(short)
            rows.append(row)
        value["items"] = rows
    if value.get("domain") == "hotel" and not value.get("unverified_constraints"):
        # Location kind remains explicit; routine missing-price prose is not a user-facing task.
        value.pop("message", None)
        value["items"] = [
            {k: v for k, v in item.items() if v is not None} for item in value.get("items", [])
        ]
    return value


def reply_facts(saved):
    """Storage receipts prove preservation, never live travel availability."""
    unverified = {}
    for binding in saved.get("results_by_query", {}).values():
        if binding.get("needs_revalidation"):
            key = (binding.get("task_id"), binding["domain"])
            unverified[key] = {"task_id": key[0], "domain": key[1], "reason": "needs_revalidation"}
    for pending in saved.get("pending_queries", []):
        key = (pending.get("task_id"), pending["item"]["domain"])
        unverified[key] = {"task_id": key[0], "domain": key[1], "reason": pending["reason"]}
    return {
        "old_plan_preserved": bool(saved.get("committed")),
        "booking_performed": False,
        "reservation_performed": False,
        # None is deliberately unknown: an absence of failures is not fresh evidence.
        "current_data_verified": False if unverified else None,
        "unverified_components": list(unverified.values()),
    }


def planning_query_results(sessions, turn_id, saved_trip):
    """Project successful lookups; bindings describe reuse, not review eligibility."""
    events = sessions.read_events(turn_id=turn_id, scope="session")
    results = {e["tool_call_id"]: e["content"] for e in events if e["type"] == "tool_result"}
    projected = []
    bindings = list(saved_trip.get("results_by_query", {}).values()) if saved_trip else []
    for event in events:
        if event["type"] != "tool_call":
            continue
        for call in event["tool_calls"]:
            if call["name"] not in {"query", "write_memory"}:
                continue
            result = results.get(call["id"], {})
            # A rejected reviewer attempt must not regain orchestration permissions.
            if (result.get("error") or {}).get("code") == "UNKNOWN_TOOL":
                continue
            if call["name"] == "write_memory":
                continue
            row = {"call_id": call["id"], "status": result.get("status", "unknown"), "data": []}
            if result.get("reason"):
                row["reason"] = result["reason"]

            for view in result.get("data", []):
                matched = [
                    b["id"]
                    for b in bindings
                    if not b.get("needs_revalidation")
                    and (
                        b.get("evidence_ref")
                        == {
                            "query_id": view.get("query_id"),
                            "result_revision": view.get("result_revision"),
                        }
                        or (
                            b["id"] == view.get("query_id")
                            and b["result_revision"] == view.get("result_revision")
                        )
                    )
                    and b.get("task_revision")
                    == next(
                        (t["revision"] for t in saved_trip["tasks"] if t["id"] == b.get("task_id")),
                        None,
                    )
                ]
                projected_view = _review_view(view)
                if matched:
                    current = {
                        json.dumps(item, sort_keys=True, ensure_ascii=False): item
                        for candidate_view in saved_trip.get("last_candidate_views", [])
                        if candidate_view.get("query_id") in matched
                        for item in _review_view(candidate_view).get("items", [])
                    }
                    original_items = projected_view.get("items", [])
                    unique = [
                        item
                        for item in original_items
                        if json.dumps(item, sort_keys=True, ensure_ascii=False) not in current
                    ]
                    projected_view.pop("items", None)
                    if unique:
                        projected_view["items"] = unique
                    projected_view["candidate_order"] = [item["id"] for item in original_items]
                    projected_view["candidates_in_tripplan"] = True
                row["data"].append({**projected_view, "task_bindings": matched})

            projected.append(row)
    return projected


def planning_memory_results(sessions, turn_id):
    events = sessions.read_events(turn_id=turn_id, scope="session")
    results = {e["tool_call_id"]: e["content"] for e in events if e["type"] == "tool_result"}
    return [
        {"updates": deepcopy(call["arguments"].get("updates", [])), **deepcopy(results[call["id"]])}
        for event in events
        if event["type"] == "tool_call"
        for call in event["tool_calls"]
        if call["name"] == "write_memory" and call["id"] in results
    ]


def _current_pages(sessions, turn_id, saved):
    events = sessions.read_events(turn_id=turn_id, scope="session")
    cutoff = max(
        (
            e["seq"]
            for e in events
            if (
                e["type"] == "tool_result"
                or e["type"] == "execution_job"
                and e.get("state") == "completed"
            )
            and e["content"].get("name") in {"write_trip", "query", "trip_query"}
            and e["content"].get("trip_plan_id") == saved["id"]
            and e["content"].get("revision") == saved["revision"]
        ),
        default=0,
    )
    pages = {}
    for event in events:
        if event["type"] != "tool_result" or event["seq"] <= cutoff:
            continue
        result = event["content"]
        if result.get("name") != "read" or result.get("status") != "ok":
            continue
        view = result.get("data", {})
        binding = saved.get("results_by_query", {}).get(view.get("query_id"))
        if (
            binding
            and not binding.get("needs_revalidation")
            and binding["result_revision"] == view.get("result_revision")
            and binding.get("task_revision")
            == next(
                (
                    task["revision"]
                    for task in saved["tasks"]
                    if task["id"] == binding.get("task_id")
                ),
                None,
            )
        ):
            pages[view["query_id"]] = _review_view(view)
    return pages


def execution_summaries(sessions, turn_id):
    rows = []
    for event in sessions.read_events(turn_id=turn_id, event_type="execution_job"):
        if event.get("state") not in {"completed", "unknown"}:
            continue
        result = event["content"]
        summary = {
            k: v
            for k, v in result.items()
            if k
            in {
                "call_id",
                "parent_call_id",
                "name",
                "status",
                "reason",
                "error",
                "trip_plan_id",
                "revision",
            }
        }
        failures = [
            {
                k: v
                for k, v in view.items()
                if k in {"domain", "status", "reason", "message", "error"}
            }
            for view in result.get("data", [])
            if view.get("status") != "ok"
        ]
        if failures:
            summary["query_outcomes"] = failures
        rows.append(summary)
    return rows


def review_payload(sessions, turn, trip_plan_id=None):
    if isinstance(trip_plan_id, list):
        payloads = [review_payload(sessions, turn, identity) for identity in trip_plan_id]
        payloads = [p for p in payloads if p]
        payload = review_payload(sessions, turn)
        payload["tripplan"] = {}
        payload["tripplans"] = [p["tripplan"] for p in payloads]
        payload["reply_facts"] = [
            {"trip_plan_id": p["tripplan"]["trip_plan_id"], **p["reply_facts"]} for p in payloads
        ]
        payload["adjustment_history"] = [
            {"trip_plan_id": p["tripplan"]["trip_plan_id"], **change}
            for p in payloads
            for change in p["adjustment_history"]
        ]
        combined = {"results_by_query": {}, "tasks": [], "last_candidate_views": []}
        store = TripStore(sessions.storage.root, sessions.user_id)
        for identity in trip_plan_id:
            saved = store.load(identity)
            if saved:
                combined["results_by_query"].update(saved.get("results_by_query", {}))
                combined["tasks"].extend(saved["tasks"])
                combined["last_candidate_views"].extend(saved.get("last_candidate_views", []))
        payload["query_results"] = planning_query_results(sessions, turn["turn_id"], combined)
        return payload
    summary, attempted = previous_round(sessions, turn["turn_id"])
    history = []
    query_results = []
    if trip_plan_id:
        try:
            storage_component(trip_plan_id)
        except ValueError:
            return None
        saved = TripStore(sessions.storage.root, sessions.user_id).load(trip_plan_id)
        if saved is None:
            return None
        snapshot = {
            k: deepcopy(v)
            for k, v in saved.items()
            if k
            not in AUDIT_FIELDS
            | {
                "last_candidate_views",
                "query_cache",
                "raw_payload",
                "execution_plan",
                "effective_preferences",
                "rail_preferences",
                "user_id",
                "session_id",
                "query_observation",
            }
        }
        snapshot["trip_plan_id"] = saved["id"]
        snapshot["stage"] = planning_stage(saved)
        for key in (
            "id",
            "original_query",
            "created_at",
            "updated_at",
            "checkpoint",
            "last_operation",
        ):
            snapshot.pop(key, None)
        snapshot["committed_revision"] = (saved.get("committed") or {}).get("revision")
        policy = snapshot.get("policy_context")
        constraint_policy = snapshot.get("constraint_snapshot", {}).get("policy")
        if (
            policy
            and constraint_policy
            and all(
                key in policy and policy[key] == value for key, value in constraint_policy.items()
            )
        ):
            snapshot["constraint_snapshot"]["policy"] = {
                "ref": "tripplan.policy_context",
                "fields": list(constraint_policy),
            }
        snapshot["candidates"] = (
            []
            if saved["status"] == "completed"
            else [_review_view(view) for view in saved.get("last_candidate_views", [])]
        )
        snapshot["candidate_queries"] = {
            key: row["result_revision"]
            for key, row in saved.get("results_by_query", {}).items()
            if not row.get("needs_revalidation")
            and row.get("task_revision")
            == next((t["revision"] for t in saved["tasks"] if t["id"] == row.get("task_id")), None)
        }
        pages = _current_pages(sessions, turn["turn_id"], saved)
        previous_pages = [
            view
            for view in snapshot["candidates"]
            if view.get("query_id") in pages
            and view.get("view_offset", 0) != pages[view["query_id"]].get("view_offset", 0)
            and snapshot["candidate_queries"].get(view.get("query_id"))
            == view.get("result_revision")
        ]
        if previous_pages:
            snapshot["previous_candidate_pages"] = previous_pages
        snapshot["candidates"] = [
            view for view in snapshot["candidates"] if view.get("query_id") not in pages
        ]
        snapshot["candidates"].extend(pages.values())
        if snapshot.get("validation"):
            for key in ("signature", "validated_revisions"):
                snapshot["validation"].pop(key, None)
            selected = [
                {k: v for k, v in row.items() if k in {"task_id", "train", "hotel", "schedule"}}
                for row in snapshot["validation"].get("reconstructed_tasks", [])
                if row.get("train") or row.get("hotel")
            ]
            snapshot["validation"]["reconstructed_tasks"] = selected
            snapshot["validation"]["issues"] = [
                i
                for i in snapshot["validation"].get("issues", [])
                if i.get("blocking") or i.get("code") != "hotel_price_unknown"
            ]
        for task in snapshot["tasks"]:
            if "items" not in task:
                task["items"] = [
                    {
                        "domain": row["domain"],
                        "parameters": deepcopy(row.get("parameters", {})),
                        "constraints": deepcopy(row.get("constraints", {})),
                        "ranking": deepcopy(row.get("ranking", [])),
                    }
                    for ref in task.get("query_ids", [])
                    if (row := saved.get("results_by_query", {}).get(ref))
                ]
            task.pop("query_ids", None)
            for item in task.get("items", []):
                # Only omit inherited values when parameters AND provenance agree.
                for key, value in list(item.get("parameters", {}).items()):
                    inherited = task.get("conditions", {}).get(key, task.get(key))
                    if key == "city":
                        inherited = task.get("destination")
                    if (
                        value is not None
                        and value == inherited
                        and all(
                            key not in item.get(meta, {})
                            or item[meta][key] == task.get(meta, {}).get(key)
                            for meta in ("field_sources", "condition_strengths")
                        )
                    ):
                        item["parameters"].pop(key)
            # Query history is durable audit; failed/current bindings remain visible.
            for container in task.get("query_containers", {}).values():
                container.pop("history_refs", None)
            for key in ("summary", "issues", "draft_plan", "depends_on"):
                if not task.get(key):
                    task.pop(key, None)
        history = adjustment_history(sessions, trip_plan_id, saved["revision"])
        for change in history:
            if change["operation"] == "propose":
                change["changes"] = {"initial_plan_in_current_snapshot": True}
        query_results = planning_query_results(sessions, turn["turn_id"], saved)

    else:
        snapshot = deepcopy(attempted)
    observations = [
        {"call_id": e["tool_call_id"], **deepcopy(e["content"])}
        for e in sessions.read_events(turn_id=turn["turn_id"], scope="session")
        if e["type"] == "tool_result" and e["content"].get("name") in {"read", "query_policy"}
    ]
    for observation in observations:
        data = observation.get("data")
        if (
            observation.get("name") == "query_policy"
            and data
            and data == snapshot.get("policy_context")
        ):
            observation["data"] = {"policy_in_tripplan": True}
            continue
        if observation.get("name") != "read" or not isinstance(data, dict):
            continue
        if data.get("query_id") in snapshot.get("candidate_queries", {}):
            observation["data"] = {
                key: data[key]
                for key in ("query_id", "result_revision", "view_offset")
                if key in data
            } | {"candidates_in_tripplan": True}
        elif trip_plan_id and data.get("id") == trip_plan_id:
            observation["data"] = {"trip_plan_id": trip_plan_id, "in_current_snapshot": True}
    if not trip_plan_id:
        query_results = planning_query_results(sessions, turn["turn_id"], None)
    return {
        "reply_facts": reply_facts(saved) if trip_plan_id else {},
        "execution_results": execution_summaries(sessions, turn["turn_id"]),
        "observations": observations,
        "original_query": turn["original_query"],
        "tripplan": snapshot,
        "previous_round_summary": summary,
        "adjustment_history": history,
        "query_results": query_results,
        "memory_results": planning_memory_results(sessions, turn["turn_id"]),
    }
