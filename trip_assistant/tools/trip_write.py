"""Complex writes delegate to the single TripPlan service."""

from trip_assistant.presentation.trip_reply import bind_trip_reply, validate_trip_reply


def build_trip_writer(service):
    async def write(args, turn):
        plan = args["tripplan"]
        reply = args.get("reply")
        if reply is not None:
            reply = validate_trip_reply(
                reply,
                plan,
                service.store.load(plan["trip_plan_id"]) if plan.get("trip_plan_id") else None,
            )
        # Legacy goal_assessment is accepted at the API boundary, never a commit token.
        result = await service.apply(plan, turn)
        return bind_trip_reply(result, reply) if reply is not None else result

    return write
