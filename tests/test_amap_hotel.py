"""Provider boundary: real response parsing, with only the external HTTP call replaced."""

import importlib
import importlib.util

import pytest
import requests


def modules():
    assert importlib.util.find_spec("travel_data.amap_hotel") is not None, (
        "AMap hotel provider is missing"
    )
    return importlib.import_module("travel_data.amap_hotel"), importlib.import_module(
        "travel_data.hotel_places"
    )


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def poi(index=1):
    return {
        "id": f"B{index}",
        "name": f"汉庭酒店{index}",
        "typecode": "100100",
        "address": f"示例路{index}号",
        "cityname": "上海市",
        "adname": "闵行区",
        "location": "121.400000,31.200000",
        "business": {"rating": "4.5", "tel": "021-12345678", "cost": "350"},
        "photos": [{"title": "外观", "url": "https://example.com/hotel.jpg"}],
    }


@pytest.mark.asyncio
async def test_provider_returns_multiple_sourced_places_without_inventing_room_rates():
    api, domain = modules()

    def get(url, *, params, timeout):
        assert url == "https://restapi.amap.com/v5/place/text"
        assert params["key"] == "test-secret"
        assert params["types"] == "100000"
        assert params["region"] == "上海" and params["city_limit"] == "true"
        assert params["keywords"] == "汉庭"
        assert params["page_size"] == 25 and params["page_num"] == 1
        assert params["show_fields"] == "business,photos"
        assert timeout > 0
        return Response(
            {
                "status": "1",
                "infocode": "10000",
                "info": "OK",
                "count": "2",
                "pois": [poi(1), poi(2)],
            }
        )

    result = await api.AmapHotelProvider("test-secret", http_get=get).search(
        domain.HotelPlaceQuery(city="上海", keywords="汉庭")
    )
    assert result.status == "ok"
    assert [item["id"] for item in result.items] == ["B1", "B2"]
    first = result.items[0]
    assert first["hotel_name"] == "汉庭酒店1" and first["address"] == "示例路1号"
    assert first["location"] == "121.400000,31.200000"
    assert first["rating"] == "4.5" and first["phone"] == "021-12345678"
    assert first["photos"][0]["url"] == "https://example.com/hotel.jpg"
    assert first["kind"] == "hotel_place"
    assert first["stay_total_cny"] is None and first["availability"] == "unknown"
    assert "cost" not in first and first["room_type"] is None
    assert result.source.provider == "amap" and result.source.fetched_at.utcoffset() is not None
    assert "test-secret" not in str(result.to_dict())


@pytest.mark.asyncio
async def test_missing_key_does_not_send_http_request():
    api, domain = modules()

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP must not run without credentials")

    result = await api.AmapHotelProvider(" ", http_get=forbidden).search(
        domain.HotelPlaceQuery(city="北京")
    )
    assert result.status == "unavailable" and result.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY test-secret"},
        {"status": "0", "infocode": "10003", "info": "quota exhausted"},
        {"status": "1", "pois": "invalid"},
        {},
        [],
    ],
)
async def test_provider_rejects_service_errors_and_malformed_responses_safely(payload):
    api, domain = modules()
    result = await api.AmapHotelProvider(
        "test-secret", http_get=lambda *a, **kw: Response(payload)
    ).search(domain.HotelPlaceQuery(city="上海"))
    assert result.status in {"unavailable", "error"} and result.items == []
    assert "test-secret" not in str(result.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [requests.Timeout("test-secret"), ValueError("test-secret")])
async def test_network_or_json_error_does_not_leak_the_key(error):
    api, domain = modules()

    def get(*args, **kwargs):
        raise error

    result = await api.AmapHotelProvider("test-secret", http_get=get).search(
        domain.HotelPlaceQuery(city="上海")
    )
    assert result.status == "error" and result.items == []
    assert "test-secret" not in str(result.to_dict())


@pytest.mark.asyncio
async def test_empty_success_is_not_an_error_or_a_no_vacancy_claim():
    api, domain = modules()
    result = await api.AmapHotelProvider(
        "test-secret",
        http_get=lambda *a, **kw: Response(
            {"status": "1", "infocode": "10000", "count": "0", "pois": []}
        ),
    ).search(domain.HotelPlaceQuery(city="上海"))
    assert result.status == "ok" and result.items == []
    assert "无房" not in (result.message or "")


@pytest.mark.asyncio
async def test_optional_fields_can_be_missing_and_bad_locations_are_not_candidates():
    api, domain = modules()
    basic = {key: value for key, value in poi().items() if key not in {"business", "photos"}}
    bad = {**poi(2), "location": "999,31"}
    result = await api.AmapHotelProvider(
        "test-secret", http_get=lambda *a, **kw: Response({"status": "1", "pois": [basic, bad]})
    ).search(domain.HotelPlaceQuery(city="上海"))
    assert result.status == "partial" and len(result.items) == 1
    assert result.items[0]["rating"] is None and result.items[0]["photos"] == []


def test_settings_reads_amap_key_without_exposing_it_in_repr(monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "test-secret")
    from config import Settings

    settings = Settings(_env_file=None)
    assert getattr(settings, "amap_api_key", None) == "test-secret"
    assert "test-secret" not in repr(settings)
