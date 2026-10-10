from datetime import date

import pytest

from travel_data.contracts import TrainQuery
from travel_data.hotel_places import HotelPlaceQuery


@pytest.mark.asyncio
async def test_simulated_trains_are_valid_candidates_with_multiple_options():
    from evals.simulated_travel import SimulatedTrainProvider
    from travel_data.result_guard import guard_domain_result
    provider = SimulatedTrainProvider()
    result = await provider.search(TrainQuery('重庆', '上海', date(2026, 10, 17)))
    checked = guard_domain_result('train', result.to_dict())
    assert checked['status'] == 'ok'
    assert len(checked['items']) >= 3
    assert all(i['source']['provider'] == 'simulation:train' and i['arrival_at'] for i in checked['items'])
    assert '模拟' in result.message


@pytest.mark.asyncio
async def test_simulated_hotels_remain_places_without_prices_or_inventory():
    from evals.simulated_travel import SimulatedHotelProvider
    from travel_data.result_guard import guard_domain_result
    result = await SimulatedHotelProvider().search(HotelPlaceQuery('上海'))
    checked = guard_domain_result('hotel', result.to_dict())
    assert checked['status'] == 'ok'
    assert len(checked['items']) >= 3
    assert all(i['stay_total_cny'] is None and i['availability'] == 'unknown' for i in checked['items'])
    assert result.source.provider == 'simulation:hotel'
