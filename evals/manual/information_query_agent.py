"""Manual live query through the current CLI; excluded from offline pytest."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trip_assistant.cli import TripEvidenceCLI


async def run(query):
    app = TripEvidenceCLI()
    await app.initialize_system()
    await app.process_query(query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="One live information request")
    asyncio.run(run(parser.parse_args().query))
