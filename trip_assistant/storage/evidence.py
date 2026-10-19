"""Versioned query evidence: historical facts never masquerade as fresh data."""

import hashlib
import json
from datetime import datetime

from trip_assistant.storage.sqlite import encode


class EvidenceStore:
    def __init__(self, storage):
        self.storage = storage

    def save(self, result, request_key):
        query_id = "query_" + hashlib.sha256(request_key.encode()).hexdigest()[:24]
        with self.storage.transaction() as conn:
            revision = conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM query_evidence WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            value = {
                **result,
                "query_id": query_id,
                "result_revision": revision,
                "request_key": request_key,
            }
            conn.execute(
                "INSERT INTO query_evidence VALUES (?,?,?,?,?)",
                (query_id, revision, request_key, result.get("fetched_at"), encode(value)),
            )
        return value

    def get(self, query_id, revision=None):
        with self.storage.connection() as conn:
            row = conn.execute(
                (
                    "SELECT payload_json FROM query_evidence WHERE query_id=? AND (? "
                    "IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1"
                ),
                (query_id, revision, revision),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def find_valid(self, request_key, *, now, max_age=3600):
        with self.storage.connection() as conn:
            row = conn.execute(
                (
                    "SELECT payload_json FROM query_evidence WHERE request_key=? ORDE"
                    "R BY revision DESC LIMIT 1"
                ),
                (request_key,),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if value.get("status", "ok") not in {"ok", "partial"}:
            return None
        try:
            age = (
                datetime.fromisoformat(now) - datetime.fromisoformat(value["fetched_at"])
            ).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        return value if 0 <= age <= max_age else None
