from datetime import datetime

from support.workflow_queries import SOURCE, train_result
from trip_assistant.providers.contracts import AgentDataResult, Source


class OfflineProvider:
    def __init__(self, domain):
        self.domain = domain
        self.hotel_places = domain == "hotel"
        self.calls = []
        self.status = "ok"
        self.unknown_time = False

    async def search(self, query):
        p = query.to_dict()
        self.calls.append(p)
        if self.domain == "train":
            items = train_result(
                {"origin": p["origin"], "destination": p["destination"], "conditions": p}
            )["items"]
            if self.unknown_time:
                for item in items:
                    item["arrival_at"] = None
        else:
            items = [
                dict(
                    id="place:" + p["city"],
                    kind="hotel_place",
                    hotel_name=p["city"] + "酒店",
                    address="地址",
                    city=p["city"],
                    district="",
                    location="116.1,39.1",
                    source=SOURCE,
                )
            ]
        src = Source("offline", datetime.fromisoformat(SOURCE["fetched_at"]), SOURCE["url"])
        return AgentDataResult(
            self.status, p, items if self.status == "ok" else [], [], src, src.fetched_at, None
        )
