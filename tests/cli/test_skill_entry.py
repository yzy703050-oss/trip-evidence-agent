import importlib.util
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized", [False, True])
async def test_plan_script_uses_current_cli_once(initialized):
    path = Path(".claude/skills/plan-trip/script/plan_trip_execution.py")
    spec = importlib.util.spec_from_file_location("plan_trip_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class CLI:
        app = object() if initialized else None
        last_result = None
        initializations = 0
        queries = []

        async def initialize_system(self):
            self.initializations += 1
            self.app = object()

        async def process_query(self, query):
            assert self.app is not None
            self.queries.append(query)
            self.last_result = {"final_answer": "reply", "status": "completed"}

    cli = CLI()
    result = await module.plan_trip("plan", cli)
    assert cli.initializations == int(not initialized)
    assert cli.queries == ["plan"]
    assert result is cli.last_result
