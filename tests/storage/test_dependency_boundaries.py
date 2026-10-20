import hashlib
import subprocess
import sys

import pytest


def test_identity_validation_does_not_import_session_repository():
    result = subprocess.run(
        [sys.executable, "-B", "-c", (
            "import sys; from trip_assistant.storage.identity import user_storage_key; "
            "user_storage_key('用户甲'); "
            "assert 'trip_assistant.storage.sessions' not in sys.modules"
        )], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_storage_identity_old_import_preserves_unicode_hash_and_rejections():
    from trip_assistant.storage.identity import storage_component, user_storage_key
    from trip_assistant.storage.sessions import storage_component as previous

    assert previous is storage_component
    assert user_storage_key("用户甲") == "user_" + hashlib.sha256("用户甲".encode()).hexdigest()
    for bad in ("..", "a/b", "a\\b", "a:", "a "):
        with pytest.raises(ValueError):
            previous(bad)


def test_lock_and_decision_role_keep_existing_import_paths():
    from trip_assistant.runtime.review import decision_role as previous_role
    from trip_assistant.runtime.state import decision_role
    from trip_assistant.storage.locking import _file_lock
    from trip_assistant.storage.trips import _file_lock as previous_lock

    assert previous_role is decision_role
    assert previous_lock is _file_lock
    assert decision_role([{"name": "request_replan", "status": "ok"}]) == "main"
    saved = {"name": "write_trip", "status": "partial", "trip_plan_id": "t"}
    assert decision_role([saved]) == "verifier"
