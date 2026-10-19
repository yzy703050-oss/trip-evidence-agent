"""Transactional user preferences and compatible chat/travel history."""

import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from trip_assistant.storage.sessions import SessionStore, storage_component
from trip_assistant.storage.sqlite import SQLiteStorage, encode, metadata, set_metadata
from trip_assistant.storage.trips import TripStore


class LongTermMemory:
    def __init__(self, user_id: str, storage_path: str = "data/memory"):
        self.user_id = storage_component(user_id)
        self.storage_path = storage_path
        self.storage = SQLiteStorage(storage_path, user_id)
        self.storage.ensure_ready()
        self.user_dir = self.storage.directory
        self.db_path = self.storage.path
        self.workflow_store = TripStore(storage_path, user_id)

    @property
    def data(self):
        with self.storage.connection() as conn:
            return self._profile(conn)

    def _profile(self, conn):
        row = conn.execute("SELECT payload_json FROM profiles WHERE id=1").fetchone()
        value = json.loads(row[0]) if row else {"user_id": self.user_id, "statistics": {}}
        if value.get("user_id") != self.user_id:
            raise ValueError("profile owner mismatch")
        value["preferences"] = [
            dict(
                type=row[0],
                value=json.loads(row[1]),
                source=row[2],
                **({"deleted": True} if row[3] else {}),
            )
            for row in conn.execute(
                "SELECT type,value_json,source,deleted FROM preferences ORDER BY rowid"
            )
        ]
        value.setdefault("statistics", {})
        value["_revision"] = int(metadata(conn, "memory_revision", 0))
        return value

    def _write_profile(self, conn, profile):
        profile = {
            key: value
            for key, value in profile.items()
            if key not in {"preferences", "chat_history", "trip_history", "_revision"}
        }
        profile["updated_at"] = datetime.now(UTC).isoformat()
        set_metadata(conn, "memory_revision", int(metadata(conn, "memory_revision", 0)) + 1)
        conn.execute(
            (
                "INSERT INTO profiles VALUES (1,?) ON CONFLICT(id) DO UPDATE SET "
                "payload_json=excluded.payload_json"
            ),
            (encode(profile),),
        )

    def _preference(self, conn, pref_type, value, source, *, deleted=False):
        previous = conn.execute(
            "SELECT source FROM preferences WHERE type=?", (pref_type,)
        ).fetchone()
        if previous and previous[0] == "user" and source != "user":
            return False
        conn.execute(
            (
                "INSERT INTO preferences VALUES (?,?,?,?) ON CONFLICT(type) DO UP"
                "DATE SET value_json=excluded.value_json,source=excluded.source,d"
                "eleted=excluded.deleted"
            ),
            (pref_type, encode(value), source, int(deleted)),
        )
        return True

    def save_preference(self, pref_type, value, *, source="agent"):
        with self.storage.transaction() as conn:
            return self._preference(conn, pref_type, value, source)

    def set_user_preference(self, pref_type, value):
        self.save_preference(pref_type, value, source="user")

    def apply_memory_updates(
        self, updates, analysis=None, expected_revision=None, operation_id=None
    ):
        """Incremental natural-language memories and bootstrap marker commit together."""
        applied, rejected = [], []
        with self.storage.transaction() as conn:
            if operation_id:
                prior = conn.execute(
                    "SELECT result_json FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if prior:
                    return json.loads(prior[0])
            profile = self._profile(conn)
            if expected_revision is not None and profile["_revision"] != expected_revision:
                from trip_assistant.storage.sqlite import StateConflictError

                raise StateConflictError("memory revision changed")
            memories = profile.setdefault("travel_memories", [])
            for update in updates:
                if not isinstance(update, dict):
                    rejected.append(deepcopy(update))
                    continue
                op, identity = update.get("op"), update.get("id")
                source = update.get("source", "explicit")
                existing = next((m for m in memories if m["id"] == identity), None)
                text = update.get("text")
                valid = (
                    op in {"add", "replace", "retract"}
                    and source in {"explicit", "history"}
                    and (op == "retract" or isinstance(text, str) and bool(text.strip()))
                    and (op == "add" or existing is not None)
                )
                protected = (
                    existing
                    and source == "history"
                    and (existing["source"] == "explicit" or existing["status"] == "retracted")
                )
                protected = (
                    protected
                    or source == "history"
                    and any(
                        m.get("status") == "retracted" and m.get("text") == text for m in memories
                    )
                )
                if not valid or protected:
                    rejected.append(deepcopy(update))
                    continue
                if op == "add":
                    identity = "memory_" + uuid.uuid4().hex
                    existing = {"id": identity, "source": source}
                    memories.append(existing)
                if op == "retract":
                    existing["status"] = "retracted"
                else:
                    existing.update(text=text.strip(), status="active", source=source)
                    if source == "history":
                        existing["evidence_trip_ids"] = deepcopy(
                            update.get("evidence_trip_ids", [])
                        )
                applied.append({"op": op, "id": identity})
            if analysis is not None:
                previous = profile.get("preference_analysis", {})
                profile["preference_analysis"] = {
                    **previous,
                    **deepcopy(analysis),
                    "covered_trip_ids": sorted(
                        set(previous.get("covered_trip_ids", []))
                        | set(analysis.get("covered_trip_ids", []))
                    ),
                    "memory_ids": [
                        m["id"]
                        for m in memories
                        if m.get("source") == "history" and m.get("status") == "active"
                    ],
                }
            if "preference_analysis" in profile:
                profile["preference_analysis"]["summary"] = "；".join(
                    m["text"]
                    for m in memories
                    if m.get("source") == "history" and m.get("status") == "active"
                )
            self._write_profile(conn, profile)
            result = {"applied": applied, "rejected": rejected}
            if operation_id:
                conn.execute(
                    "INSERT INTO operation_receipts VALUES (?,?,?)",
                    (operation_id, "memory", json.dumps(result, ensure_ascii=False)),
                )
        return result

    def save_analysis(self, result, *, expected_revision=None, operation_id=None):
        profile = self.data
        previous = profile.get("preference_analysis", {})
        owned = [
            m
            for m in profile.get("travel_memories", [])
            if m["id"] in previous.get("memory_ids", [])
            and m.get("source") == "history"
            and m.get("status") == "active"
        ]
        updates = []
        for index, item in enumerate(result["memories"]):
            update = {**item, "op": "replace" if index < len(owned) else "add", "source": "history"}
            if index < len(owned):
                update["id"] = owned[index]["id"]
            updates.append(update)
        if result["memories"]:
            updates.extend(
                {"op": "retract", "id": m["id"], "source": "history"}
                for m in owned[len(result["memories"]) :]
            )
        analysis = {
            "initial_summary_done": True,
            "covered_trip_ids": result["covered_trip_ids"],
            "trigger": result.get("trigger", "incremental"),
        }
        return self.apply_memory_updates(
            updates,
            analysis=analysis,
            expected_revision=profile["_revision"]
            if expected_revision is None
            else expected_revision,
            operation_id=operation_id,
        )

    def delete_user_preference(self, pref_type):
        with self.storage.transaction() as conn:
            self._preference(conn, pref_type, None, "user", deleted=True)

    def get_preference(self, pref_type=None):
        with self.storage.connection() as conn:
            if pref_type is not None:
                row = conn.execute(
                    "SELECT value_json FROM preferences WHERE type=? AND deleted=0", (pref_type,)
                ).fetchone()
                return json.loads(row[0]) if row else None
            return {
                row[0]: json.loads(row[1])
                for row in conn.execute("SELECT type,value_json FROM preferences WHERE deleted=0")
            }

    def _append_preference(self, pref_type, item):
        with self.storage.transaction() as conn:
            row = conn.execute(
                "SELECT value_json,source,deleted FROM preferences WHERE type=?", (pref_type,)
            ).fetchone()
            if row and row[1] == "user":
                return False
            value = json.loads(row[0]) if row and not row[2] else None
            values = list(value) if isinstance(value, list) else ([value] if value else [])
            if item not in values:
                values.append(item)
            return self._preference(conn, pref_type, values, "agent")

    def add_hotel_brand(self, brand):
        return self._append_preference("hotel_brands", brand)

    def add_airline(self, airline):
        return self._append_preference("airlines", airline)

    def add_chat_message(self, role, content, session_id=None):
        store = SessionStore(self.storage_path, self.user_id, session_id or "legacy")
        with self.storage.connection() as conn:
            row = conn.execute(
                (
                    "SELECT turn_id FROM events WHERE session_id=? AND scope='session"
                    "' AND type='message' AND role='user' ORDER BY seq DESC LIMIT 1"
                ),
                (store.session_id,),
            ).fetchone()
        turn = row[0] if role == "assistant" and row else uuid.uuid4().hex
        store.append(
            {
                "turn_id": turn,
                "scope": "session",
                "type": "message",
                "role": role,
                "content": content,
                "final": role == "assistant",
            }
        )

    def get_chat_history(self, limit=None, session_id=None):
        clauses = ["scope='session'", "type='message'", "role IN ('user','assistant')"]
        params = []
        if session_id:
            clauses.append("session_id=?")
            params.append(storage_component(session_id))
        sql = (
            "SELECT session_id,payload_json FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp DESC,session_id DESC,seq DESC"
        )
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.storage.connection() as conn:
            rows = list(conn.execute(sql, params))
        messages = []
        for session, payload in reversed(rows):
            row = json.loads(payload)
            if row.get("user_id") != self.user_id or row.get("session_id") != session:
                raise ValueError("session record identity mismatch")
            messages.append(
                {
                    "role": row["role"],
                    "content": row.get("content", ""),
                    "timestamp": row.get("timestamp", ""),
                    "session_id": session,
                }
            )
        return messages

    def save_trip_history(self, trip_info):
        self.workflow_store.save_history(trip_info)

    def get_trip_history(self, limit=10):
        return self.workflow_store.list_history(limit)

    def _history_destinations(self):
        return self._travel_statistics()[1]

    def _travel_statistics(self):
        counts = {}
        with self.storage.connection() as conn:
            conn.execute("BEGIN")
            cutoff = metadata(conn, "history_cleared_before", "")
            destinations = []
            for row in conn.execute(
                "SELECT summary_json FROM trip_plans WHERE status='completed' AND updated_at>?",
                (cutoff,),
            ):
                tasks = json.loads(row[0]).get("tasks", [])
                destinations.append(tasks[-1].get("destination") if tasks else None)
            destinations += [
                row[0]
                for row in conn.execute(
                    (
                        "SELECT json_extract(h.payload_json,'$.destination') FROM travel_"
                        "history h LEFT JOIN trip_plans p ON p.id=json_extract(h.payload_"
                        "json,'$.workflow_id') WHERE p.id IS NULL AND h.updated_at>?"
                    ),
                    (cutoff,),
                )
            ]
        for destination in destinations:
            if destination:
                counts[destination] = counts.get(destination, 0) + 1
        return len(destinations), counts

    def get_frequent_destinations(self, top_n=5):
        return sorted(self._history_destinations().items(), key=lambda item: item[1], reverse=True)[
            :top_n
        ]

    def increment_query_count(self):
        with self.storage.transaction() as conn:
            profile = self._profile(conn)
            profile["statistics"]["total_queries"] = (
                profile["statistics"].get("total_queries", 0) + 1
            )
            self._write_profile(conn, profile)

    def get_statistics(self):
        with self.storage.connection() as conn:
            statistics = self._profile(conn)["statistics"].copy()
            statistics["total_messages"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE scope='session' AND type='mess"
                "age' AND role IN ('user','assistant')"
            ).fetchone()[0]
        statistics["total_trips"], statistics["frequent_destinations"] = self._travel_statistics()
        return statistics

    def clear_history(self):
        with self.storage.transaction() as conn:
            set_metadata(conn, "history_cleared_before", datetime.now(UTC).isoformat())
            conn.execute("DELETE FROM sessions")
            profile = self._profile(conn)
            profile["legacy_chat_cleared"] = True
            profile["statistics"].update(total_trips=0, total_messages=0, frequent_destinations={})
            self._write_profile(conn, profile)

    def delete_all(self):
        with self.storage.transaction() as conn:
            for table in (
                "sessions",
                "preferences",
                "profiles",
                "trip_plans",
                "travel_history",
                "migration_sources",
            ):
                conn.execute("DELETE FROM " + table)
            conn.execute(
                "DELETE FROM metadata WHERE key NOT IN ('owner','schema_version',"
                "'migration_complete','state_clock')"
            )
            set_metadata(conn, "migration_complete", "1")
