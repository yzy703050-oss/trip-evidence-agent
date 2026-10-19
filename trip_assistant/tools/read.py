"""Bounded reads of summaries, plans and evidence absent from injected memory."""

from copy import deepcopy

from trip_assistant.providers.candidates import candidate_view
from trip_assistant.providers.views import build_tool_result_view, remember_view
from trip_assistant.trips.discovery import discover


def build_read_handler(trips, evidence):
    async def read(args, turn):
        resource = args["resource"]
        offset = args.get("offset", 0)
        if resource in {"session_summaries", "session"}:
            import json

            from trip_assistant.memory.context import project_events
            from trip_assistant.storage.sessions import SessionStore, storage_component

            if resource == "session_summaries":
                filters = args.get("filters", {})
                if set(filters) - {"query"}:
                    raise ValueError("unsupported session filters")
                rows = SessionStore.list_sessions(trips.storage.root, trips.user_id, limit=None)
                text = filters.get("query", "")
                rows = [r for r in rows if text in (r["title"] + r["last_user_message"])]
                data = {"summaries": rows[offset : offset + 5], "has_more": offset + 5 < len(rows)}
            else:
                identity = storage_component(args.get("session_id"))
                with trips.storage.connection() as conn:
                    row = conn.execute(
                        "SELECT state_json FROM sessions WHERE id=?", (identity,)
                    ).fetchone()
                    if row is None:
                        raise ValueError("session_not_found")
                    state = json.loads(row[0])
                    cut = state.get("cleared_through_seq", 0)
                    rows = list(
                        conn.execute(
                            "SELECT payload_json FROM events WHERE session_id=? AND seq>? "
                            "AND scope='session' AND type='message' "
                            "AND role IN ('user','assistant') "
                            "ORDER BY seq DESC LIMIT 6 OFFSET ?",
                            (identity, cut, offset),
                        )
                    )
                events = [json.loads(r[0]) for r in reversed(rows[:5])]
                data = {
                    "session_id": identity,
                    "summary": state.get("summary") or state.get("session_summary") or "",
                    "entries": project_events(events),
                    "has_more": len(rows) > 5,
                    "historical": True,
                }
        elif resource == "trip_summaries":
            rows = discover(
                trips.discovery_summaries(), args.get("filters", {}), turn.get("session_id")
            )
            data = {
                "summaries": rows[offset : offset + 5],
                "has_more": offset + 5 < len(rows),
                "needs_clarification": len(rows) > 1,
            }
        elif resource == "trip":
            value = trips.load(args.get("trip_plan_id")) or trips.read_history(
                args.get("trip_plan_id")
            )
            if value is None:
                raise ValueError("trip_not_found")
            data = {
                k: deepcopy(v)
                for k, v in value.items()
                if k
                not in {
                    "results_by_query",
                    "execution_plan",
                    "committed",
                    "working",
                    "query_observation",
                }
            }
        elif resource == "candidates":
            if args.get("filters"):
                raise ValueError("candidate page cannot change query filters")
            trip = trips.load(args["trip_plan_id"]) if args.get("trip_plan_id") else None
            binding = (trip or {}).get("results_by_query", {}).get(args.get("query_id"))
            value = (
                deepcopy(binding)
                if binding
                else evidence.get(args.get("query_id"), args.get("result_revision"))
            )
            if value is None:
                raise ValueError("evidence_not_found")
            if binding:
                value["query_id"] = binding["id"]
                value["query"] = binding["parameters"]
                if (
                    args.get("result_revision") is not None
                    and binding["result_revision"] != args["result_revision"]
                ):
                    raise ValueError("candidate version changed")
            key = f"{value['query_id']}:{value['result_revision']}"
            previous = turn.get("candidate_views", {}).get(key)
            if offset and (
                not previous
                or not previous.get("has_more")
                or offset != previous.get("view_offset", 0) + 5
            ):
                raise ValueError("candidate page must read the next cached window")
            constraints = (previous or binding or {}).get("constraints", {}) or {}
            ranking = (previous or binding or {}).get("ranking", []) or []
            data = candidate_view(value, constraints=constraints, ranking=ranking, offset=offset)
            data = build_tool_result_view(
                value.get("domain"),
                data,
                limits={"candidate_limit": 5, "already_paged": True},
                requirements=[r["field"] for r in ranking],
            )
            data.update(historical=True, constraints=constraints, ranking=ranking)
            remember_view(turn, data)
        else:
            raise ValueError("unsupported read resource; injected memory is already in context")
        return {"status": "ok", "data": data}

    return read
