"""Juhe's authorized train query, with conservative inventory and safe diagnostics."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from .contracts import AgentDataResult, Source, TrainOffer, TrainQuery

BEIJING = timezone(timedelta(hours=8))
API_URL = 'https://apis.juhe.cn/fapigw/train/query'
DOCUMENTATION_URL = 'https://www.juhe.cn/docs/api/id/817'
UNAVAILABLE_CODES = {10001, 10002, 10003, 10004, 10005, 10007, 10008, 10009,
                     10011, 10012, 10013, 10020, 10021}


class JuheTrainProvider:
    def __init__(self, api_key: str, *, http_post=None, now_fn=None, timeout: float = 10.0):
        self._api_key = api_key
        self._http_post = http_post or requests.post
        self._now_fn = now_fn or (lambda: datetime.now(BEIJING))
        self._timeout = timeout

    async def search(self, query: TrainQuery) -> AgentDataResult:
        def result(status, message=None, *, items=None, source=None):
            return AgentDataResult(status, query.to_dict(), items or [], [], source,
                                   source.fetched_at if source else None, message)

        if not self._api_key or not self._api_key.strip():
            return result('unavailable', '未配置聚合数据火车查询凭据。')
        today = self._now_fn().astimezone(BEIJING).date()
        if not today <= query.departure_date < today + timedelta(days=15):
            return result('unavailable', '聚合数据仅支持北京时间今天起的 15 个日历日查询。')
        try:
            response = await asyncio.to_thread(
                self._http_post, API_URL,
                data=dict(key=self._api_key, search_type=1, enable_booking=2,
                          departure_station=query.origin, arrival_station=query.destination,
                          date=query.departure_date.isoformat()),
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=self._timeout,
            )
            source = Source('聚合数据', self._now_fn().astimezone(BEIJING), DOCUMENTATION_URL)
        except Exception:
            # Exception text may include credentials or request bodies. Never echo it.
            return result('error', '聚合数据火车查询请求失败或超时。')
        if response.status_code != 200:
            return result('error', '聚合数据火车查询 HTTP 响应失败。', source=source)
        try:
            payload = response.json()
        except Exception:
            return result('error', '聚合数据火车查询响应格式无效。', source=source)
        if not isinstance(payload, dict) or type(payload.get('error_code')) is not int:
            return result('error', '聚合数据火车查询响应格式无效。', source=source)
        code = payload['error_code']
        if code:
            status = 'unavailable' if code in UNAVAILABLE_CODES else 'error'
            return result(status, '聚合数据火车查询服务不可用。' if status == 'unavailable'
                          else '聚合数据火车查询返回错误。', source=source)
        rows = payload.get('result')
        if not isinstance(rows, list):
            return result('error', '聚合数据火车查询响应格式无效。', source=source)
        items = []
        skipped = False
        for row in rows:
            try:
                fields = [self._text(row[name]) for name in (
                    'train_no', 'departure_station', 'arrival_station',
                    'departure_time', 'arrival_time')]
                for value in fields[3:]:
                    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                        raise ValueError('invalid time')
                prices = row['prices']
                if not isinstance(prices, list) or not prices:
                    raise ValueError('invalid prices')
            except (KeyError, TypeError, ValueError):
                skipped = True
                continue
            for seat in prices:
                try:
                    name = self._text(seat['seat_name'])
                    code = seat.get('seat_type_code')
                    code = self._text(code) if code is not None else name
                    raw_price = seat['price']
                    if isinstance(raw_price, bool) or not isinstance(raw_price, (str, int, float, Decimal)):
                        raise ValueError('invalid price')
                    price = Decimal(str(raw_price))
                    if not price.is_finite() or price < 0:
                        raise ValueError('invalid price')
                    remaining, availability = self._inventory(seat.get('num'), row.get('enable_booking'))
                    offer_id = '|'.join([query.departure_date.isoformat(), *fields[:3], code, name])
                    departure = datetime.fromisoformat(f'{query.departure_date.isoformat()}T{fields[3]}:00+08:00')
                    arrival = None
                    evidence = {'departure': 'query_date_and_provider_clock'}
                    duration = row.get('duration')
                    if isinstance(duration, str) and re.fullmatch(r'\d{1,3}:[0-5]\d', duration):
                        hours, minutes = (int(part) for part in duration.split(':'))
                        candidate = departure + timedelta(hours=hours, minutes=minutes)
                        if candidate.strftime('%H:%M') == fields[4]:
                            arrival = candidate.isoformat()
                            evidence['arrival'] = 'provider_segment_duration'
                    items.append(TrainOffer(offer_id, *fields, name, price, availability,
                                            remaining, source, None,
                                            departure_at=departure.isoformat(), arrival_at=arrival,
                                            time_evidence=evidence).to_dict())
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    skipped = True
        message = '部分车次或席别字段不完整，已跳过。' if skipped else (
            '按所填站名查询无结果，请核对具体车站。' if not items else None)
        return result('partial' if skipped else 'ok', message, items=items, source=source)

    def _text(self, value):
        if not isinstance(value, str) or not value.strip() or self._api_key in value:
            raise ValueError('invalid text')
        return value

    @staticmethod
    def _inventory(num, booking):
        remaining = None
        if type(num) is int and num >= 0:
            remaining = num
        elif isinstance(num, str) and re.fullmatch(r'[0-9]+', num):
            remaining = int(num)
        elif num == '无':
            remaining = 0
        if remaining == 0:
            return 0, 'sold_out'
        positive = (remaining is not None and remaining > 0) or num == '有'
        if booking == 'Y' and positive:
            return remaining, 'available'
        if booking == 'N':
            return remaining, 'unavailable'
        return remaining, 'unknown'
