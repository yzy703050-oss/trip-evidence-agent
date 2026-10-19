"""Decision roles derived from durable outcomes, not from assistant prose."""


def decision_role(results):
    role = "verifier"
    for result in results:
        if result.get("name") == "request_replan" and result.get("status") == "ok":
            role = "main"
        elif (
            result.get("name") == "write_trip"
            and result.get("status") in {"completed", "partial"}
            and result.get("trip_plan_id")
        ):
            role = "verifier"
    return role
