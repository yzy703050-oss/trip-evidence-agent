"""Case-sensitive identities and paths shared by storage and legacy fixtures."""

import hashlib
from pathlib import Path


def storage_component(value: str) -> str:
    """Reject path traversal while allowing existing Unicode user IDs."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("user_id and session_id must be non-empty path components")
    if any(character in value for character in "/\\:\0<>|?*") or value.endswith((".", " ")):
        raise ValueError("user_id and session_id must be safe path components")
    return value


def user_storage_key(user_id: str) -> str:
    storage_component(user_id)
    return "user_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def resolve_user_directory(root: str | Path, user_id: str) -> Path:
    """Resolve a path only; migration never copies or repairs old files."""
    return Path(root) / user_storage_key(user_id)
