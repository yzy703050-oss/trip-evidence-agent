import io
from rich.console import Console
from cli import TripEvidenceCLI
from utils.response_renderer import finalize_business_result


def test_domain_statuses_and_sources_are_visible():
    cli = TripEvidenceCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    results = [{"agent_name": name, "result": {"status": "success", "data": {"status": status, "items": [], "missing_fields": ["check_in"], "source": {"provider": "authorized", "url": "https://example.com"}, "fetched_at": "2026-10-08T10:00:00+08:00"}}} for name, status in [("train_search", "unavailable"), ("hotel_search", "needs_input"), ("travel_guide", "error")]]
    cli._display_results(finalize_business_result({"domain_results": {dict(train_search="train", hotel_search="hotel", travel_guide="guide")[r["agent_name"]]: r["result"]["data"] for r in results}}))
    text = output.getvalue()
    assert "服务暂时不可用" in text and "需要补充条件" in text and "攻略查询失败" in text
    assert "入住日期" in text and "check_in" not in text
    assert "authorized" in text and "2026-10-08" in text
    assert "无票" not in text and "无房" not in text


def test_cli_guards_legacy_itinerary_before_any_display():
    cli = TripEvidenceCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_results(finalize_business_result({"itinerary": {"summary": "车票299元", "itinerary": {"title": "今日开放", "notes": ["车票299元"], "estimated_budget": "299元", "daily_plans": [{"day": 1, "activities": [{"location_ref": "suggestion:city_walk", "location": "故宫", "description": "今日开放"}]}]}}}))
    text = output.getvalue()
    assert "299" not in text and "今日开放" not in text
    assert "市内漫步" in text and "未报价" in text


def test_cli_rejects_price_inventory_prose_in_location():
    injected = "故宫门票两百块，现有库存充足"
    cli = TripEvidenceCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_results(finalize_business_result({"itinerary": {"itinerary": {"daily_plans": [{"city": injected, "activities": [{"location": injected}]}]}}}))
    assert injected not in output.getvalue()
