"""Authorized provider boundaries; production defaults never invent data."""
from typing import Protocol

from travel_data.contracts import AgentDataResult, TrainQuery, HotelQuery, GuideQuery


class TrainProvider(Protocol):
    async def search(self, query: TrainQuery) -> AgentDataResult: ...


class HotelProvider(Protocol):
    async def search(self, query: HotelQuery) -> AgentDataResult: ...


class GuideProvider(Protocol):
    async def search(self, query: GuideQuery) -> AgentDataResult: ...


class UnavailableProvider:
    async def search(self, query: TrainQuery | HotelQuery | GuideQuery) -> AgentDataResult:
        return AgentDataResult('unavailable', query.to_dict(), [], [], None, None,
                               '未配置获授权的数据接口，无法查询报价、库存或攻略事实。')
