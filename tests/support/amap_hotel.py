"""Offline fixtures extracted from test_amap_hotel; no test-module imports."""


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
