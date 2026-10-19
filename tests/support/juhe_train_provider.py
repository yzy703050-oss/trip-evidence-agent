"""Offline fixtures extracted from test_juhe_train_provider; no test-module imports."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone

from trip_assistant.providers.contracts import TrainQuery
from trip_assistant.providers.juhe_train import JuheTrainProvider

NOW = datetime(2026, 10, 9, 9, tzinfo=timezone(timedelta(hours=8)))

SECRET = "offline-test-key"

ROW = dict(
    train_no="G25",
    departure_station="北京南",
    arrival_station="苏州北",
    departure_time="18:04",
    arrival_time="22:32",
    enable_booking="Y",
    prices=[
        dict(seat_name="二等座", seat_type_code="O", price=627, num="1"),
        dict(seat_name="商务座", seat_type_code="9", price=2194, num="无"),
    ],
)


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def search(payload=None, *, row=None, day=None, failure=None, key=SECRET, now_fn=None):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if failure:
            raise failure
        return Response(
            payload
            if payload is not None
            else dict(error_code=0, result=[row or copy.deepcopy(ROW)])
        )

    provider = JuheTrainProvider(key, http_post=post, now_fn=now_fn or (lambda: NOW))
    result = asyncio.run(provider.search(TrainQuery("北京南", "苏州北", day or NOW.date())))
    return result, calls
