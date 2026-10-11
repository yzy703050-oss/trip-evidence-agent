"""Case-safe local identities and conservative, non-destructive legacy imports."""

import hashlib
import json
import threading
from pathlib import Path

_guard = threading.RLock()


def user_storage_key(user_id: str) -> str:
    from context.session_store import storage_component

    storage_component(user_id)
    return "user_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def resolve_user_directory(root: str | Path, user_id: str) -> Path:
    from context.session_store import atomic_json_write
    from context.workflow_store import _file_lock

    directory = Path(root) / user_storage_key(user_id)
    with _guard, _file_lock(directory / "identity.lock"):
        identity = directory / "identity.json"
        if identity.exists():
            value = json.loads(identity.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("user_id") != user_id or value.get("version") != 1:
                raise ValueError("user storage identity mismatch")
        else:
            value = {"user_id": user_id, "version": 1}
            atomic_json_write(identity, value)
        manifest = value
        imported = manifest.get("imported_legacy_sessions", [])
        # Copy only records whose own metadata proves ownership. Never alter
        # the legacy directory, infer ownership from its spelling, or overwrite.
        legacy = Path(root) / user_id
        for name in ("profile.json", "trips.json"):
            source, target = legacy / name, directory / name
            if source.exists() and not target.exists():
                record = _owned_json(source, user_id)
                if record is not None:
                    atomic_json_write(target, record)
        for source in (legacy / "workflows").glob("*.json"):
            target = directory / "workflows" / source.name
            if not target.exists():
                record = _owned_json(source, user_id)
                if record is not None:
                    atomic_json_write(target, record)
        for source in (legacy / "sessions").glob("*/events.jsonl"):
            if source.parent.name in imported:
                continue
            try:
                rows = [
                    json.loads(line)
                    for line in source.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (ValueError, OSError):
                continue
            if not rows or any(
                not isinstance(row, dict)
                or row.get("user_id") != user_id
                or row.get("session_id") != source.parent.name
                for row in rows
            ):
                continue
            target = directory / "sessions" / source.parent.name
            # Prepare a complete snapshot before exposing events to the list.
            documents = {}
            for name in ("state.json", "runs.jsonl"):
                path = source.parent / name
                if not path.exists():
                    continue
                try:
                    values = (
                        [
                            json.loads(line)
                            for line in path.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]
                        if name.endswith("jsonl")
                        else [json.loads(path.read_text(encoding="utf-8"))]
                    )
                except (ValueError, OSError):
                    break
                if any(
                    not isinstance(row, dict)
                    or row.get("user_id") != user_id
                    or row.get("session_id") != source.parent.name
                    for row in values
                ):
                    break
                documents[name] = path.read_bytes()
            else:
                if not (target / "events.jsonl").exists():
                    target.mkdir(parents=True, exist_ok=True)
                    for name, content in documents.items():
                        (target / name).write_bytes(content)
                    temporary = target / ".import-events.tmp"
                    temporary.write_bytes(source.read_bytes())
                    temporary.replace(target / "events.jsonl")
                imported = [*imported, source.parent.name]
                manifest["imported_legacy_sessions"] = imported
                atomic_json_write(identity, manifest)
    return directory


def _owned_json(path, user_id):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("user_id") == user_id else None
    except (ValueError, OSError):
        return None
