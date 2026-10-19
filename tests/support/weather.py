"""Offline fixtures extracted from test_main_harness; no test-module imports."""


def weather_info():
    source = {
        "provider": "weather",
        "fetched_at": "2026-10-09T09:00:00+08:00",
        "url": "https://example.com/weather",
    }
    return {
        "status": "ok",
        "summary": "北京明天多云，18～25℃",
        "missing_fields": [],
        "travel_conditions": {},
        "domain_results": {
            "weather": {
                "status": "ok",
                "query": {"city": "北京", "date": "2026-10-10"},
                "items": [
                    {
                        "date": "2026-10-10",
                        "description": "多云",
                        "min_temp_c": 18,
                        "max_temp_c": 25,
                        "source": source,
                    }
                ],
                "source": source,
                "missing_fields": [],
                "fetched_at": source["fetched_at"],
                "message": None,
            }
        },
    }
