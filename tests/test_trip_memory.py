import json

from support.native import make_application
from support.workflow import populated_workflow

from trip_assistant.storage.identity import resolve_user_directory


def test_partial_and_completed_are_known_across_sessions_and_summary_repairs(tmp_path):
    m = make_application("alice", "one", storage_path=str(tmp_path))
    w = populated_workflow()
    w["status"] = "partial"
    saved = m.trips.save(w, expected_revision=None)
    path = resolve_user_directory(tmp_path, "alice") / "trip.md"
    assert path.exists() and saved["id"] in path.read_text(encoding="utf-8")
    saved["status"] = "completed"
    saved = m.trips.save(saved, expected_revision=saved["revision"])
    path.write_text("过时摘要 revision=0", encoding="utf-8")
    newer = make_application("alice", "two", storage_path=str(tmp_path))
    assert newer.trips.list_summaries()[0]["status"] == "completed"
    text = path.read_text(encoding="utf-8")
    assert f"revision: {saved['revision']}" in text and "planned_itinerary" in text
    assert "过时摘要" not in text
    assert not make_application("bob", "two", storage_path=str(tmp_path)).trips.list_summaries()


def test_legacy_projection_is_idempotent_and_preserves_json(tmp_path):
    source = tmp_path / "alice" / "trips.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "user_id": "alice",
                "trip_history": [dict(origin="重庆", destination="上海", summary="旧记录")],
            }
        ),
        encoding="utf-8",
    )
    m = make_application("alice", "one", storage_path=str(tmp_path))
    before = (tmp_path / "alice" / "trips.json").read_bytes()
    m.trips.list_summaries()
    m.trips.list_summaries()
    assert (tmp_path / "alice" / "trips.json").read_bytes() == before
    text = (resolve_user_directory(tmp_path, "alice") / "trip.md").read_text(encoding="utf-8")
    assert text.count("旧记录") == 1


def test_projection_failure_does_not_fail_canonical_save(tmp_path, monkeypatch):
    from trip_assistant.storage.legacy_mirror import TripMemory

    m = make_application("alice", "one", storage_path=str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("test readonly summary")

    monkeypatch.setattr(TripMemory, "rebuild", fail)
    saved = m.trips.save(populated_workflow(), expected_revision=None)
    assert m.trips.load(saved["id"]) == saved


def test_known_plans_are_recent_first_and_query_can_find_older_completed_plan(tmp_path):
    from trip_assistant.trips.conditions import create_workflow

    m = make_application("alice", "one", storage_path=str(tmp_path))
    for name, city in [("workflow_z", "上海"), ("workflow_a", "杭州")]:
        w = create_workflow(
            dict(
                confirmed_conditions={},
                tasks=[dict(origin="重庆", destination=city, requires_hotel=False)],
            ),
            {},
            workflow_id=name,
        )
        w["status"] = "completed"
        m.trips.save(w, expected_revision=None)
    assert (
        __import__("trip_assistant.trips.discovery", fromlist=["discover"]).discover(
            m.trips.list_summaries(), {}, None
        )[0]["id"]
        == "workflow_a"
    )
    assert m.trips.list_summaries()[0]["id"] == "workflow_a"


def test_active_trips_do_not_displace_five_recent_completed_details(tmp_path):
    from copy import deepcopy
    from types import SimpleNamespace

    from trip_assistant.storage.legacy_mirror import TripMemory

    rows = []
    for index in range(12):
        w = deepcopy(populated_workflow())
        w["id"] = f"workflow_{index}"
        w["status"] = "partial" if index < 6 else "completed"
        rows.append(w)
    projection = TripMemory(tmp_path, "alice")
    projection.rebuild(SimpleNamespace(list_all=lambda: rows, list_history=lambda: []))
    text = (resolve_user_directory(tmp_path, "alice") / "trip.md").read_text(encoding="utf-8")
    for index in range(11):
        section = text.split("## workflow_" + str(index) + "\n")[1].split("\n## ")[0]
        assert "详细任务见" not in section
    assert "详细任务见数据库行程 workflow_11" in text
