import pytest
from support.native import make_application


@pytest.mark.parametrize(
    "stamp,tomorrow,following",
    [
        ("2026-10-18T05:00:00+08:00", "2026-10-19", "2026-10-20"),
        ("2026-12-31T23:00:00+08:00", "2027-01-01", "2027-01-02"),
    ],
)
def test_calendar_context_supplies_date_anchors_without_assigning_trip_conditions(
    tmp_path, stamp, tomorrow, following
):
    app = make_application("a", "s", storage_path=tmp_path)
    context = {"current_time": stamp}
    app.refresh_memory_context(context)
    assert context["system_context"]["calendar"] == {
        "today": stamp[:10],
        "tomorrow": tomorrow,
        "day_after_tomorrow": following,
    }
    assert "confirmed_conditions" not in context
