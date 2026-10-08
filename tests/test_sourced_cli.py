import io
from rich.console import Console
from cli import AligoCLI


def test_domain_statuses_and_sources_are_visible():
    cli = AligoCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    results = [{"agent_name": name, "result": {"status": "success", "data": {"status": status, "items": [], "missing_fields": ["check_in"], "source": {"provider": "authorized", "url": "https://example.com"}, "fetched_at": "2026-10-08T10:00:00+08:00"}}} for name, status in [("train_search", "unavailable"), ("hotel_search", "needs_input"), ("travel_guide", "error")]]
    cli._display_results({"results": results})
    text = output.getvalue()
    assert "unavailable" in text and "needs_input" in text and "error" in text
    assert "authorized" in text and "2026-10-08" in text
    assert "无票" not in text and "无房" not in text


def test_cli_guards_legacy_itinerary_before_any_display():
    cli = AligoCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_results({"results": [{"agent_name": "itinerary_planning", "status": "success", "data": {"summary": "车票299元", "itinerary": {"title": "今日开放", "notes": ["车票299元"], "estimated_budget": "299元", "daily_plans": [{"day": 1, "activities": [{"location_ref": "suggestion:city_walk", "location": "故宫", "description": "今日开放"}]}]}}}]})
    text = output.getvalue()
    assert "299" not in text and "今日开放" not in text
    assert "市内漫步" in text and "未报价" in text


def test_cli_rejects_price_inventory_prose_in_location():
    injected = "故宫门票两百块，现有库存充足"
    cli = AligoCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_results({"results": [{"agent_name": "itinerary_planning", "status": "success", "data": {"itinerary": {"daily_plans": [{"city": injected, "activities": [{"location": injected}]}]}}}]})
    assert injected not in output.getvalue()
