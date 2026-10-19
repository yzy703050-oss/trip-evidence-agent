"""Raw supplier API fixtures through production parsers; never a live fallback."""

from copy import deepcopy
from dataclasses import replace

from trip_assistant.providers.amap_hotel import AmapHotelProvider
from trip_assistant.providers.juhe_train import JuheTrainProvider


class FixtureResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return deepcopy(self.payload)

    def raise_for_status(self):
        pass


def mark_simulated(result, provider):
    source = replace(result.source, provider=provider) if result.source else None
    return replace(
        result,
        source=source,
        items=[{**i, "source": source.to_dict()} for i in result.items] if source else result.items,
        message="模拟原始API响应，车次、地点、金额及库存均为测试数据。 " + (result.message or ""),
    )


def train_payload(data):
    stations = {"重庆": "重庆西", "上海": "上海虹桥"}
    origin = stations.get(data["departure_station"], data["departure_station"])
    destination = stations.get(data["arrival_station"], data["arrival_station"])

    def seat(name, code, price, num):
        return {"seat_name": name, "seat_type_code": code, "price": price, "num": num}

    def train(number, departure, arrival, duration, prices):
        return {
            "train_no": number,
            "departure_station": origin,
            "arrival_station": destination,
            "departure_time": departure,
            "arrival_time": arrival,
            "duration": duration,
            "enable_booking": "Y",
            "prices": prices,
        }

    return {
        "reason": "查询成功",
        "error_code": 0,
        "result": [
            train(
                "G9001",
                "07:20",
                "18:50",
                "11:30",
                [
                    seat("二等座", "O", "584.50", "有"),
                    seat("一等座", "M", "940.00", "3"),
                ],
            ),
            train(
                "G9003",
                "08:00",
                "19:40",
                "11:40",
                [
                    seat("二等座", "O", "650.00", "5"),
                    seat("商务座", "9", "1900.00", "无"),
                ],
            ),
            train(
                "D9005",
                "19:00",
                "07:30",
                "12:30",
                [
                    seat("二等座", "O", "605.00", "待确认"),
                ],
            ),
            train(
                "G9007",
                "08:30",
                "20:30",
                "12:00",
                [
                    seat("二等座", "O", "615.00", "12"),
                ],
            ),
            {"train_no": "G9999", "departure_time": "bad-time", "prices": []},
        ],
    }


def hotel_payload(params):
    city = params["region"]
    brands = ["全季", "汉庭", "如家"] if params["keywords"] == "酒店" else [params["keywords"]] * 3
    pois = [
        {
            "id": f"B0SIM{i:06d}",
            "name": f"{brand}酒店（{city}模拟门店{i + 1}）",
            "type": "住宿服务;宾馆酒店;经济型连锁酒店",
            "typecode": "100100",
            "address": f"模拟路{100 + i}号",
            "cityname": city if city.endswith("市") else city + "市",
            "adname": "闵行区" if city.startswith("上海") else "模拟区",
            "location": f"{121.40 + i * 0.01:.6f},{31.20 + i * 0.01:.6f}",
            "business": {"rating": "4.5", "tel": "021-00000000", "cost": "350"},
            "photos": [{"title": "模拟外观", "url": "https://example.com/fixture-hotel.jpg"}],
        }
        for i, brand in enumerate(brands)
    ]
    pois.extend([deepcopy(pois[0]), {**deepcopy(pois[1]), "id": "BAD-POI", "location": "invalid"}])
    return {"status": "1", "infocode": "10000", "info": "OK", "count": str(len(pois)), "pois": pois}


class ApiShapedTrainProvider(JuheTrainProvider):
    """Only HTTP is simulated; timetable parsing and inventory semantics stay real."""

    def __init__(self, *, now_fn=None):
        self.calls, self.fixtures = [], []
        super().__init__("offline-fixture-key", http_post=self._post, now_fn=now_fn)

    def _post(self, url, *, data, headers, timeout):
        safe_data = {k: v for k, v in data.items() if k != "key"}
        payload = train_payload(safe_data)
        self.fixtures.append(
            {
                "request": {"url": url, "data": safe_data, "headers": headers, "timeout": timeout},
                "response": deepcopy(payload),
            }
        )
        return FixtureResponse(payload)

    async def search(self, query):
        self.calls.append(query.to_dict())
        result = mark_simulated(await super().search(query), "simulation:juhe-api")
        for fixture in reversed(self.fixtures):
            request = fixture["request"]["data"]
            if (
                request["date"] == query.departure_date.isoformat()
                and request["departure_station"] == query.origin
                and request["arrival_station"] == query.destination
                and "parsed" not in fixture
            ):
                fixture["parsed"] = result.to_dict()
                break
        return result


class ApiShapedHotelProvider(AmapHotelProvider):
    """AMap POI fixtures deliberately contain no bookable room quote."""

    def __init__(self):
        self.calls, self.fixtures = [], []
        super().__init__("offline-fixture-key", http_get=self._get)

    def _get(self, url, *, params, timeout):
        safe_params = {k: v for k, v in params.items() if k != "key"}
        payload = hotel_payload(safe_params)
        self.fixtures.append(
            {
                "request": {"url": url, "params": safe_params, "timeout": timeout},
                "response": deepcopy(payload),
            }
        )
        return FixtureResponse(payload)

    async def search(self, query):
        self.calls.append(query.to_dict())
        result = mark_simulated(await super().search(query), "simulation:amap-api")
        for fixture in reversed(self.fixtures):
            params = fixture["request"]["params"]
            if (
                params["region"] == query.city
                and params["keywords"] == query.keywords
                and "parsed" not in fixture
            ):
                fixture["parsed"] = result.to_dict()
                break
        return result
