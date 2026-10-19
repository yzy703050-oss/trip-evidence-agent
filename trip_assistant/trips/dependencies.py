"""Report invalidated components; never choose replacement business arrangements."""

TRAIN_FIELDS = {
    "departure_date",
    "arrival_date",
    "arrival_before",
    "passengers",
    "start_date",
    "end_date",
}
HOTEL_FIELDS = {
    "check_in",
    "check_out",
    "nights",
    "guests",
    "hotel_keywords",
    "hotel_quote_required",
}
TRAIN_CONSTRAINTS = {
    "seat_class",
    "train_kind",
    "train_max_total_cny",
    "departure_time_after",
    "departure_time_before",
}
HOTEL_CONSTRAINTS = {"hotel_max_total_cny", "hotel_max_nightly_cny"}


def _constraint_domains(fields):
    domains = set()
    for field in fields:
        if field in TRAIN_CONSTRAINTS:
            domains.add("train")
        elif field in HOTEL_CONSTRAINTS:
            domains.add("hotel")
        else:
            domains.update({"train", "hotel"})
    return domains


def affected_components(before, proposed):
    changed = {
        k
        for k, v in proposed.get("conditions", {}).items()
        if before.get("conditions", {}).get(k) != v
    }
    changed.update(
        k
        for k, v in proposed.get("condition_strengths", {}).items()
        if before.get("condition_strengths", {}).get(k) != v
    )
    domains = set()
    if changed & (TRAIN_FIELDS | TRAIN_CONSTRAINTS):
        domains.add("train")
    if changed & (HOTEL_FIELDS | HOTEL_CONSTRAINTS):
        domains.add("hotel")
    if "constraints" in changed:
        old = before.get("conditions", {}).get("constraints", {})
        new = proposed.get("conditions", {}).get("constraints", old)
        fields = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        if "constraints" in proposed.get("condition_strengths", {}):
            fields.update(old.keys() | new.keys())
        domains.update(_constraint_domains(fields))
    if "origin" in proposed and proposed["origin"] != before.get("origin"):
        domains.add("train")
    if "requires_hotel" in proposed and proposed["requires_hotel"] != before.get("requires_hotel"):
        domains.add("hotel")
    if "destination" in proposed and proposed["destination"] != before.get("destination"):
        domains.update({"train", "hotel"})
    return domains


def invalidate_components(work, task, domains):
    from trip_assistant.trips.state import invalidate_query_binding

    if not domains:
        return
    task["revision"] += 1
    task["status"] = "draft" if task.get("draft_plan") else "pending"
    draft = task.get("draft_plan")
    if draft:
        draft["task_revision"] = task["revision"]
        for domain in domains:
            draft[domain + "_selection"] = None
        if "hotel" in domains:
            for key in ("check_in", "check_out"):
                if task["conditions"].get(key):
                    draft.setdefault("schedule", {})[key] = task["conditions"][key]
    for key in task.get("query_ids", []):
        row = work["results_by_query"][key]
        if row["domain"] in domains:
            row["needs_revalidation"] = True
        else:
            row["task_revision"] = task["revision"]
    for domain, container in task.get("query_containers", {}).items():
        if domain in domains:
            task["query_containers"][domain] = invalidate_query_binding(
                container, "parameters_changed"
            )
