import asyncio
import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from travel_data.contracts import TrainQuery
from travel_data.juhe_train import JuheTrainProvider

NOW = datetime(2026, 10, 9, 9, tzinfo=timezone(timedelta(hours=8)))
SECRET = 'offline-test-key'
ROW = dict(train_no='G25', departure_station='北京南', arrival_station='苏州北',
           departure_time='18:04', arrival_time='22:32', enable_booking='Y', prices=[
               dict(seat_name='二等座', seat_type_code='O', price=627, num='1'),
               dict(seat_name='商务座', seat_type_code='9', price=2194, num='无')])

class Response:
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload


def search(payload=None, *, row=None, day=None, failure=None, key=SECRET, now_fn=None):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        if failure: raise failure
        return Response(payload if payload is not None else dict(error_code=0, result=[row or copy.deepcopy(ROW)]))
    provider = JuheTrainProvider(key, http_post=post, now_fn=now_fn or (lambda: NOW))
    result = asyncio.run(provider.search(TrainQuery('北京南', '苏州北', day or NOW.date())))
    return result, calls


def test_documented_response_and_post_form():
    result, calls = search()
    assert result.status == 'ok'
    assert len(calls) == 1
    url, args = calls[0]
    assert url == 'https://apis.juhe.cn/fapigw/train/query'
    assert args['data'] == dict(key=SECRET, search_type=1, enable_booking=2,
                               departure_station='北京南', arrival_station='苏州北', date='2026-10-09')
    assert args['timeout'] == 10.0
    assert args['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    offer, sold = result.items
    assert (offer['price_cny'], offer['remaining'], offer['availability']) == ('627', 1, 'available')
    assert (sold['remaining'], sold['availability']) == (0, 'sold_out')
    assert all(part in offer['id'] for part in ['2026-10-09', 'G25', '北京南', '苏州北', 'O'])
    assert offer['url'] is None
    assert result.source.provider == '聚合数据'
    assert result.source.url == 'https://www.juhe.cn/docs/api/id/817'
    assert SECRET not in json.dumps(result.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize('num,booking,remaining,availability', [
    ('有','Y',None,'available'), ('待确认','Y',None,'unknown'),
    (None,'Y',None,'unknown'), ('0','Y',0,'sold_out'),
    ('2','N',2,'unavailable'), ('2',None,2,'unknown'),
])
def test_inventory_is_not_invented(num, booking, remaining, availability):
    row = copy.deepcopy(ROW)
    row['enable_booking'] = booking
    row['prices'] = [dict(seat_name='二等座', seat_type_code='O', price='627.50', num=num)]
    result, _ = search(row=row)
    assert result.items[0]['remaining'] == remaining
    assert result.items[0]['availability'] == availability
    assert result.items[0]['price_cny'] == '627.50'


@pytest.mark.parametrize('bad', [None, {}, {'prices': []}, dict(ROW, prices='bad'),
                                dict(ROW, departure_time='bad')])
def test_malformed_rows_are_partial(bad):
    result, _ = search(dict(error_code=0, result=[copy.deepcopy(ROW), bad]))
    assert result.status == 'partial'
    assert len(result.items) == 2


@pytest.mark.parametrize('bad_price', [None, 'oops', '-1', 'NaN', True])
def test_invalid_price_skips_seat(bad_price):
    row = copy.deepcopy(ROW)
    row['prices'][0]['price'] = bad_price
    result, _ = search(row=row)
    assert result.status == 'partial'
    assert len(result.items) == 1


@pytest.mark.parametrize('payload', [[], {}, {'error_code': 0}, {'error_code': 0, 'result': {}},
                                    {'error_code': 'bad', 'result': []}])
def test_invalid_envelopes_are_errors(payload):
    result, _ = search(payload)
    assert result.status == 'error'
    assert result.items == []


def test_valid_empty_response_is_ok():
    result, _ = search(dict(error_code=0, result=[]))
    assert result.status == 'ok'
    assert result.items == []
    assert '车站' in result.message


@pytest.mark.parametrize('code,status', [(10001,'unavailable'), (10002,'unavailable'),
    (10003,'unavailable'), (10009,'unavailable'), (10011,'unavailable'),
    (10012,'unavailable'), (10013,'unavailable'), (10020,'unavailable'),
    (10021,'unavailable'), (281701,'error'), (10014,'error'), (999,'error')])
def test_api_failure_is_not_empty_success_or_secret_echo(code, status):
    result, calls = search(dict(error_code=code, reason=SECRET, result=[]))
    assert result.status == status
    assert len(calls) == 1
    assert SECRET not in json.dumps(result.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize('failure', [requests.Timeout(SECRET), requests.ConnectionError(SECRET), ValueError(SECRET)])
def test_network_and_json_failure_are_sanitized(failure):
    result, calls = search(failure=failure)
    assert result.status == 'error'
    assert len(calls) == 1
    assert SECRET not in json.dumps(result.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize('offset,requested', [(-1,False), (0,True), (14,True), (15,False)])
def test_beijing_fifteen_calendar_day_window(offset, requested):
    result, calls = search(day=NOW.date() + timedelta(days=offset))
    assert bool(calls) is requested
    assert result.status == ('ok' if requested else 'unavailable')


def test_window_uses_beijing_date_and_fetched_time_is_after_response():
    times = iter([datetime(2026,10,8,17,tzinfo=timezone.utc),
                  datetime(2026,10,8,17,0,2,tzinfo=timezone.utc)])
    result, calls = search(day=date(2026,10,9), now_fn=lambda: next(times))
    assert len(calls) == 1
    assert result.fetched_at == datetime(2026,10,9,1,0,2,tzinfo=NOW.tzinfo)
    assert result.fetched_at.utcoffset() == timedelta(hours=8)


def test_missing_key_does_not_request():
    result, calls = search(key='')
    assert result.status == 'unavailable'
    assert calls == []

@pytest.mark.parametrize('mode', ['http', 'json'])
def test_http_status_and_invalid_json_are_errors(mode):
    class BrokenResponse:
        status_code = 503 if mode == 'http' else 200
        def json(self): raise ValueError(SECRET)
    result = asyncio.run(JuheTrainProvider(SECRET, http_post=lambda *a, **k: BrokenResponse(),
        now_fn=lambda: NOW).search(TrainQuery('北京南', '苏州北', NOW.date())))
    assert result.status == 'error'
    assert result.items == []
    assert SECRET not in json.dumps(result.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize('field', ['seat_name', 'price'])
def test_missing_seat_field_is_partial(field):
    row = copy.deepcopy(ROW)
    del row['prices'][0][field]
    result, _ = search(row=row)
    assert result.status == 'partial'
    assert len(result.items) == 1


def test_provider_cannot_echo_secret_in_response_fields():
    row = copy.deepcopy(ROW)
    row['train_no'] = SECRET
    result, _ = search(row=row)
    assert result.status == 'partial'
    assert result.items == []
    assert SECRET not in json.dumps(result.to_dict(), ensure_ascii=False)
