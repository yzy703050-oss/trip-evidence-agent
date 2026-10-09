"""Standalone entry uses the same main-agent runtime as the CLI."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from cli import TripEvidenceCLI


async def plan_trip(user_query: str, app=None):
    app = app or TripEvidenceCLI()
    if app.orchestrator is None:
        await app.initialize_system()
    await app.process_query(user_query)
    return app.last_result


if __name__ == '__main__':
    print(json.dumps(asyncio.run(plan_trip(input('Query: '))), ensure_ascii=False, indent=2))
