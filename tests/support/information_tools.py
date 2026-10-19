"""Offline fixtures extracted from test_information_tools; no test-module imports."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from trip_assistant.providers.contracts import AgentDataResult, HotelOffer, Source


class Provider:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        await asyncio.sleep(0)
        source = Source("test", datetime(2026, 10, 9, tzinfo=UTC), "https://example.com")
        items = [
            HotelOffer(
                str(i),
                f"酒店{i}",
                "大床房",
                query.check_in,
                query.check_out,
                query.guests,
                Decimal(str(100 + i)),
                True,
                "available",
                None,
                source,
                None,
            ).to_dict()
            for i in range(7)
        ]
        return AgentDataResult("ok", query.to_dict(), items, [], source, source.fetched_at, None)
