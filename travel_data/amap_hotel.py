"""AMap Web Service hotel POIs with no interpretation as bookable room prices."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import requests

from .contracts import AgentDataResult, Source
from .hotel_places import HotelPlace, HotelPlaceQuery, _photos, _text

API_URL = "https://restapi.amap.com/v5/place/text"
DOCUMENTATION_URL = "https://developer.amap.com/api/webservice/guide/api-advanced/newpoisearch"
BEIJING = timezone(timedelta(hours=8))
LIMITATION = "酒店地点候选；房型、入住日期对应房价及空房未查询。"


class AmapHotelProvider:
    hotel_places = True

    def __init__(self, api_key: str, *, http_get=None, timeout: float = 10.0):
        self._api_key = api_key.strip()
        self._http_get = http_get or requests.get
        self._timeout = timeout

    async def search(self, query: HotelPlaceQuery) -> AgentDataResult:
        def result(status, message, *, items=None, source=None):
            return AgentDataResult(
                status,
                query.to_dict(),
                items or [],
                [],
                source,
                source.fetched_at if source else None,
                message,
            )

        if not self._api_key:
            return result("unavailable", "未配置高德 AMAP_API_KEY。")
        try:
            response = await asyncio.to_thread(
                self._http_get,
                API_URL,
                params={
                    "key": self._api_key,
                    "keywords": query.keywords,
                    "types": "100000",
                    "region": query.city,
                    "city_limit": "true",
                    "page_size": 25,
                    "page_num": 1,
                    "show_fields": "business,photos",
                    "output": "json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return result("error", "高德酒店查询返回格式无效。")
            if str(data.get("status")) != "1" or str(data.get("infocode", "10000")) != "10000":
                code = str(data.get("infocode", ""))
                detail = f"（错误码 {code}）" if re.fullmatch(r"\d{5}", code) else ""
                return result(
                    "unavailable", f"高德酒店查询暂不可用{detail}，请检查 Key、权限与配额。"
                )
            pois = data.get("pois")
            if not isinstance(pois, list):
                return result("error", "高德酒店查询返回格式无效。")
            source = Source("amap", datetime.now(BEIJING), DOCUMENTATION_URL)
            items, skipped, seen = [], False, set()
            for poi in pois:
                try:
                    if not isinstance(poi, dict) or not str(poi.get("typecode", "")).startswith(
                        "10"
                    ):
                        raise ValueError("not an accommodation POI")
                    business = poi.get("business")
                    business = business if isinstance(business, dict) else {}
                    place = HotelPlace(
                        id=poi["id"],
                        hotel_name=poi["name"],
                        address=_text(poi.get("address")) or "",
                        city=_text(poi.get("cityname")) or "",
                        district=_text(poi.get("adname")) or "",
                        location=poi["location"],
                        source=source,
                        phone=_text(business.get("tel")),
                        rating=_text(business.get("rating")),
                        photos=_photos(poi.get("photos")),
                    )
                    if place.id not in seen:
                        items.append(place.to_dict())
                        seen.add(place.id)
                except (KeyError, TypeError, ValueError):
                    skipped = True
            message = LIMITATION + (" 部分地点字段无效，已排除。" if skipped else "")
            return result("partial" if skipped else "ok", message, items=items, source=source)
        except (requests.RequestException, ValueError, TypeError):
            return result("error", "高德酒店查询失败或超时，请稍后重试。")
