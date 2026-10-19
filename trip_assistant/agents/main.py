"""Native model boundary; no text-JSON business routing."""

from trip_assistant.llm.messages import collect_model_turn
from trip_assistant.runtime.telemetry import model_stage


class MainAgent:
    def __init__(self, model):
        self.model = model

    async def respond(self, messages, tools, *, stage="agent:main"):
        options = {"tools": tools} if tools else {"tool_choice": "none"}
        with model_stage(stage):
            return await collect_model_turn(await self.model(messages, **options))
