"""Case-sensitive identities and paths shared by storage and legacy fixtures."""

import hashlib
from pathlib import Path


def user_storage_key(user_id: str) -> str:
    from trip_assistant.storage.sessions import storage_component

    storage_component(user_id)
    return "user_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def resolve_user_directory(root: str | Path, user_id: str) -> Path:
    """Resolve a path only; migration never copies or repairs old files."""
    return Path(root) / user_storage_key(user_id)
