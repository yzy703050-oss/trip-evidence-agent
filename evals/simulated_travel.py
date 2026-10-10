"""Explicitly injected evaluation data, never a production fallback."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from travel_data.contracts import AgentDataResult, Source, TrainOffer
from travel_data.hotel_places import HotelPlace

BEIJING = timezone(timedelta(hours=8))


class SimulatedTrainProvider:
    def __init__(self, *, duration_minutes=240, departure_hours=None, empty_dates=(), now_fn=None):
        self.duration_minutes = duration_minutes
        self.departure_hours = departure_hours or [8, 12, 18]
        self.empty_dates = set(empty_dates)
        self.now_fn = now_fn or (lambda: datetime.now(BEIJING))
        self.calls = []

    async def search(self, query):
        self.calls.append(query.departure_date)
        source = Source('simulation:train', self.now_fn(), None)
        items = []
        if query.departure_date not in self.empty_dates:
            for i, hour in enumerate(self.departure_hours):
                departure = datetime.combine(query.departure_date, time(hour), BEIJING)
                arrival = departure + timedelta(minutes=self.duration_minutes)
                offer = TrainOffer(f'sim-train|{query.origin}|{query.destination}|{query.departure_date}|{hour}',
                    f'SIM{i+1:03d}', query.origin, query.destination, departure.strftime('%H:%M'), arrival.strftime('%H:%M'),
                    '二等座', Decimal(200+i*30), 'available', 20, source, None,
                    departure_at=departure.isoformat(), arrival_at=arrival.isoformat(),
                    time_evidence={'departure': 'simulation', 'arrival': 'simulation_segment_duration'})
                items.append(offer.to_dict())
        return AgentDataResult('ok', query.to_dict(), items, [], source, source.fetched_at,
                               '模拟测试数据；车次、时间、票价和余票均非真实查询。')


class SimulatedHotelProvider:
    hotel_places = True

    def __init__(self, *, now_fn=None):
        self.now_fn = now_fn or (lambda: datetime.now(BEIJING))
        self.calls = []

    async def search(self, query):
        self.calls.append(query.to_dict())
        source = Source('simulation:hotel', self.now_fn(), None)
        brands = ['全季', '如家', '汉庭'] if query.keywords == '酒店' else [query.keywords]*3
        items = [HotelPlace(f'sim-hotel|{query.city}|{brand}|{i}', f'{brand}（{query.city}模拟候选{i+1}）',
                            '模拟地址', query.city, '模拟区域', '121.0,31.0', source).to_dict()
                 for i, brand in enumerate(brands)]
        return AgentDataResult('ok', query.to_dict(), items, [], source, source.fetched_at,
                               '模拟酒店地点；名称地址均为测试生成，不提供房价或空房。')
