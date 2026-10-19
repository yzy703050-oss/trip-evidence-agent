"""Authorized provider boundaries; production defaults never invent data."""

from typing import Protocol

from trip_assistant.providers.contracts import AgentDataResult, GuideQuery, HotelQuery, TrainQuery


class TrainProvider(Protocol):
    async def search(self, query: TrainQuery) -> AgentDataResult: ...


class HotelProvider(Protocol):
    async def search(self, query: HotelQuery) -> AgentDataResult: ...


class GuideProvider(Protocol):
    async def search(self, query: GuideQuery) -> AgentDataResult: ...


class UnavailableProvider:
    async def search(self, query: TrainQuery | HotelQuery | GuideQuery) -> AgentDataResult:
        return AgentDataResult(
            "unavailable",
            query.to_dict(),
            [],
            [],
            None,
            None,
            "未配置获授权的数据接口，无法查询报价、库存或攻略事实。",
        )


def transient_exception(error):
    import httpx
    import requests

    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(
        error,
        ConnectionError
        | TimeoutError
        | requests.Timeout
        | requests.ConnectionError
        | httpx.TransportError,
    ) or status in {429, 500, 502, 503, 504}
