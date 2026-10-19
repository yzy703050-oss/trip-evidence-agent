import pytest


@pytest.mark.asyncio
async def test_amap_transport_fault_is_classified_but_not_echoed():
    import requests

    from trip_assistant.providers.amap_hotel import AmapHotelProvider
    from trip_assistant.providers.hotel_places import HotelPlaceQuery

    def fail(*args, **kwargs):
        raise requests.ConnectionError("key=secret-value")

    value = await AmapHotelProvider("secret-value", http_get=fail).search(
        HotelPlaceQuery("上海", "酒店")
    )
    assert value.status == "error" and value.error_type == "transient"
    assert "secret-value" not in str(value.to_dict())


@pytest.mark.asyncio
async def test_public_transport_fault_is_classified():
    from trip_assistant.providers.public_query import PublicQueryProvider

    def fail(*args):
        raise TimeoutError("temporary")

    value = await PublicQueryProvider(weather_fetch=fail).weather("上海", None)
    assert value.error_type == "transient"
