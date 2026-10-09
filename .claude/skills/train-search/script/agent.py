"""Sourced train queries; no LLM generated prices or facts."""
from agentscope.agent import AgentBase
from travel_data.agent_support import sourced_reply
from travel_data.providers import UnavailableProvider


class TrainSearchAgent(AgentBase):
    def __init__(self, name="TrainSearchAgent", model=None, provider=None):
        super().__init__()
        self.name = name
        self.model = model
        self.provider = provider if provider is not None else UnavailableProvider()

    async def reply(self, x=None):
        return await sourced_reply(self, x, "train")
