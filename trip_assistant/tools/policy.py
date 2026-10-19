"""Expose policy interpretation as a native capability, with applicability reuse."""

from copy import deepcopy

from trip_assistant.agents.policy import resolve_policy_for_workflow


def build_policy_handler(agent, trips):
    async def query(args, turn):
        context = {
            **{
                k: v
                for k, v in turn.items()
                if k in {"original_query", "current_time", "employee_id", "project_id", "purpose"}
            },
            **args.get("context", {}),
        }
        saved = (
            trips.load(args["trip_plan_id"])
            if trips is not None and args.get("trip_plan_id")
            else None
        )
        policy = deepcopy(saved.get("policy_context")) if saved else None
        if policy:
            policy = resolve_policy_for_workflow(policy, saved, context)
            if policy.get("status") != "ok":
                policy = None
        if policy is None:
            policy = await agent.query(context)
        shared = turn.setdefault("policy_context", {})
        shared.clear()
        shared.update(policy)
        return {"status": "ok" if policy.get("status") == "ok" else "partial", "data": policy}

    return query
