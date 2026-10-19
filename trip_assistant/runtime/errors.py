"""Stable execution failures; message is display-only, never a control protocol."""

from copy import deepcopy


class ToolFailure(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = deepcopy(details or {})


def error_payload(error):
    from pydantic import ValidationError

    from trip_assistant.runtime.limits import LimitError

    if isinstance(error, ToolFailure):
        return {"code": error.code, "details": deepcopy(error.details)}
    if isinstance(error, LimitError):
        return {"code": error.code.upper(), "details": {}}
    if isinstance(error, ValidationError):
        fields = [
            {
                "field": ".".join(map(str, row["loc"])),
                "type": row["type"],
                "message": row["msg"][:200],
            }
            for row in error.errors(include_url=False, include_input=False)[:5]
        ]
        return {"code": "INVALID_ARGUMENTS", "details": {"fields": fields}}
    return {
        "code": "INVALID_ARGUMENTS" if isinstance(error, ValueError) else "TOOL_ERROR",
        "details": {"exception_type": type(error).__name__},
    }


def error_message(error):
    fields = error_payload(error)["details"].get("fields")
    if fields:
        return "; ".join(row["field"] + ": " + row["message"] for row in fields)
    return str(error)[:1200]
