"""Public harness wrapper. Business decisions belong to MainAgent."""
from agents.execution_harness import ExecutionHarness, business_schedule

normalize_schedule = business_schedule


class OrchestrationAgent(ExecutionHarness):
    def __init__(self, name='OrchestrationAgent', main_agent=None, agent_registry=None, memory_manager=None, emit=None, **kwargs):
        self.name = name
        super().__init__(main_agent, agent_registry or {}, memory_manager, emit)
