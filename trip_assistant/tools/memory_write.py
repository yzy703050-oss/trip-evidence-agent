"""The simple writer has only a preference dependency, never a trip store."""


def build_memory_writer(preferences, reanalyze=None):
    async def write(arguments, turn):
        if arguments.get("operation") == "reanalyze":
            if reanalyze is None:
                raise ValueError("preference analysis unavailable")
            return {"status": "ok", "data": await reanalyze(turn)}
        result = preferences.apply_memory_updates(
            [{**u, "source": "explicit"} for u in arguments["updates"]],
            operation_id=turn.get("operation_id"),
        )
        return {"status": "ok" if not result["rejected"] else "partial", "data": result}

    return write
