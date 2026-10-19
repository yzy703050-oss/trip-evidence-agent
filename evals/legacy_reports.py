INTENT_TYPES = {"ask", "plan", "update", "control"}
"""Read old recorded reports; no model calls or orchestration."""


def assess(case, result, run, previous=None):
    w = result.get("workflow")
    checks = {"no_execution_error": result.get("status") != "error"}
    if case == "query_price":
        checks.update(
            no_workflow=w is None,
            train_result=bool(result.get("domain_results", {}).get("train", {}).get("items")),
        )
    elif case in {"explain", "pause", "ambiguous", "unsupported_purchase", "memory_read"}:
        checks["no_external_requests"] = run.external_request_count == 0
        if case == "pause":
            checks["paused"] = result.get("status") == "paused"
        if case == "ambiguous":
            checks["scope_checkpoint"] = ((w or {}).get("checkpoint") or {}).get(
                "kind"
            ) == "feedback_scope"
        if case in {"unsupported_purchase", "memory_read"}:
            checks["no_new_workflow"] = w is None
            checks["answer_returned"] = (
                bool(result.get("final_answer")) and result.get("status") == "ok"
            )
        if case == "memory_read":
            brands = run.effective_preferences.get("hotel_brands")
            brands = [brands] if isinstance(brands, str) else brands
            checks["stored_preference_answer"] = bool(brands) and all(
                brand in result.get("final_answer", "") for brand in brands
            )
    else:
        checks["workflow_created"] = isinstance(w, dict)
        if w:
            from trip_assistant.trips.validation import check_workflow

            checked = check_workflow(w)
            checks["valid_candidate_references"] = not any(
                i["code"]
                in {"invalid_candidate_reference", "stale_draft", "preserved_component_changed"}
                for i in checked["issues"]
            )
            checks["one_or_three_tasks"] = len(w["tasks"]) == (
                3 if case.startswith("multi_route") else 1
            )
            checks["simulation_label"] = (
                case == "unknown_origin" or result.get("data_mode") == "simulation"
            )
            checks["model_stay_proposals"] = all(
                not t["requires_hotel"]
                or t["conditions"].get("nights")
                or (t["conditions"].get("check_in") and t["conditions"].get("check_out"))
                for t in w["tasks"]
            )
            checks["no_invented_quote_requirement"] = not w["confirmed_conditions"].get(
                "hotel_quote_required"
            ) and not any(t["conditions"].get("hotel_quote_required") for t in w["tasks"])
            if case == "unknown_origin":
                checks["origin_checkpoint"] = (
                    result["status"] == "needs_input"
                    and (w.get("checkpoint") or {}).get("kind") == "required_conditions"
                )
                checks["no_external_queries"] = (
                    run.external_request_count == 0 and w["tasks"][0]["origin"] is None
                )
            else:
                checks["usable_draft"] = result["status"] == "completed" and all(
                    t.get("draft_plan") for t in w["tasks"]
                )
                checks["no_personal_question"] = w.get("checkpoint") is None
                if case.startswith("multi_route"):
                    checks["completed"] = result["status"] == "completed"
                    checks["return_no_hotel"] = (
                        w["tasks"][-1]["purpose"] == "return"
                        and not w["tasks"][-1]["requires_hotel"]
                    )
            if case == "arrival_overnight":
                plan = result.get("validated_plan", {}).get("reconstructed_tasks", [{}])[0]
                train = plan.get("train") or {}
                checks["previous_day_departure"] = bool(
                    train.get("arrival_at")
                    and train["departure_at"][:10] < train["arrival_at"][:10]
                )
            if case in {"hotel_replace", "preference_replace"} and previous:
                checks["train_preserved"] = (
                    w["tasks"][0]["draft_plan"]["train_selection"]
                    == previous["workflow"]["tasks"][0]["draft_plan"]["train_selection"]
                )
                checks["no_train_request"] = all(
                    t["name"] == "hotel_search" for t in run.tool_requests
                )
            if case == "supplement_origin" and previous:
                checks["same_workflow"] = w["id"] == previous["workflow_id"]
                checks["default_not_drifted"] = (
                    w["tasks"][0]["conditions"]["departure_date"]
                    == previous["workflow"]["tasks"][0]["conditions"]["departure_date"]
                )
            if case in {"preference_plan", "preference_replace"}:
                brands = run.effective_preferences.get("hotel_brands")
                # Existing preference values allow a scalar or a list. Also
                # verify the refreshed preference reached the actual query.
                checks["preference_updated"] = brands in ("全季", ["全季"]) and any(
                    row.get("domain") == "hotel"
                    and row.get("parameters", {}).get("keywords") == "全季"
                    for row in w.get("results_by_query", {}).values()
                )
            if case == "resume" and previous:
                checks["same_workflow"] = w["id"] == previous["workflow_id"]
                checks["reuse_without_query"] = run.external_request_count == 0
            if case == "change_stay":
                before = (
                    previous["workflow"]["tasks"][0]["conditions"].get("nights")
                    if previous
                    else None
                )
                checks["stay_changed"] = (
                    before is not None
                    and w["tasks"][0]["conditions"].get("nights") == before + 1
                    and w.get("last_update", {}).get("update_type") == "change"
                )
    return dict(passed=all(checks.values()), checks=checks)


def assess_intents(case, responses):
    """Judge the model's actual initial JSON, before compatibility inference."""
    from trip_assistant.llm.structured import robust_json_parse

    initial = [
        row for row in responses if row.get("stage") in {"main:plan", "main:repair_decision"}
    ]
    try:
        value = robust_json_parse(initial[-1]["text"]) if initial else {}
        intents = value.get("intents")
        valid = (
            isinstance(intents, list)
            and bool(intents)
            and all(
                isinstance(row, dict)
                and isinstance(row.get("type"), str)
                and row["type"] in INTENT_TYPES
                for row in intents
            )
        )
    except (ValueError, TypeError, AttributeError):
        value, intents, valid = {}, [], False
    actual = {row["type"] for row in intents} if valid else set()
    expected = {
        "query_price": {"ask"},
        "explain": {"ask"},
        "memory_read": {"ask"},
        "hotel_replace": {"update"},
        "preference_replace": {"update"},
        "change_stay": {"update"},
        "supplement_origin": {"update"},
        "ambiguous": {"update"},
        "pause": {"control"},
        "resume": {"control"},
        "unsupported_purchase": {"control"},
        "preference_plan": {"update", "plan"},
    }.get(case, {"plan"})
    checks = {"four_type_contract": valid, "expected_purposes": actual == expected}
    if case in {"preference_plan", "preference_replace"}:
        checks["preference_action_retained"] = any(
            row.get("agent_name") == "preference" for row in value.get("agent_schedule", [])
        )
    if case in {"explain", "unsupported_purchase"}:
        checks["direct_without_dispatch"] = (
            value.get("response_mode") == "direct"
            and not value.get("agent_schedule")
            and not value.get("travel_update")
        )
    if case == "memory_read":
        names = {row.get("agent_name") for row in value.get("agent_schedule", [])}
        checks["memory_source"] = names == {"memory_query"} or (
            not names and value.get("response_mode") == "direct" and bool(value.get("final_answer"))
        )
    return dict(passed=all(checks.values()), checks=checks)


def case_query(case, query, previous):
    if case == "change_stay" and previous:
        nights = previous["workflow"]["tasks"][0]["conditions"].get("nights")
        if nights is not None:
            return f"把刚才上海旅行的停留时间改为{nights + 1}晚，其他要求保留。"
    return query
