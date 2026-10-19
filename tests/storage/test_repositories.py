import pytest


def document(user="alice"):
    return {
        "id": "trip-1",
        "user_id": user,
        "revision": 0,
        "status": "running",
        "tasks": [],
        "validation": {"valid": True},
    }


def test_legacy_store_location_cas_and_working_copy(tmp_path):
    from trip_assistant.storage.trips import TripStore, WorkflowConflictError

    store = TripStore(tmp_path, "alice")
    initial = store.save(document(), expected_revision=None)
    work = {**initial, "summary": "new draft"}
    saved = store.save_working(work, expected_revision=initial["revision"], operation_id="stage-1")
    assert saved["working"]["summary"] == "new draft"
    with pytest.raises(WorkflowConflictError):
        store.save_working(work, expected_revision=initial["revision"], operation_id="stage-2")
    assert store.storage.path.name == "agent.sqlite3"
    with pytest.raises(ValueError):
        store.save(document("bob"), expected_revision=saved["revision"])


def test_operation_receipt_is_atomic_and_idempotent(tmp_path, monkeypatch):
    from trip_assistant.storage.trips import TripStore

    store = TripStore(tmp_path, "alice")
    initial = store.save(document(), expected_revision=None)

    def fail(*args):
        raise OSError("receipt write failed")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_record_operation", fail)
        with pytest.raises(OSError):
            store.save_working(initial, expected_revision=1, operation_id="op")
    assert store.load("trip-1")["revision"] == 1
    assert store.lookup_operation("op") is None
    first = store.save_working(initial, expected_revision=1, operation_id="op")
    again = store.save_working(initial, expected_revision=1, operation_id="op")
    assert first == again
    assert store.load("trip-1")["revision"] == 2
