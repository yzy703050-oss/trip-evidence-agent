"""One durable execution ledger; model calls and system jobs share transitions."""

import json
from copy import deepcopy

from trip_assistant.storage.sqlite import encode


class ActionStore:
    def __init__(self, sessions):
        self.sessions = sessions
        self.owner = sessions.session_id

    def _get(self, conn, identity):
        row = conn.execute(
            "SELECT definition_json,state,result_ref FROM execution_actions "
            "WHERE session_id=? AND operation_id=?",
            (self.owner, identity),
        ).fetchone()
        return {**json.loads(row[0]), "state": row[1], "result_ref": row[2]} if row else None

    def get(self, operation_id):
        with self.sessions.storage.connection() as conn:
            return self._get(conn, operation_id)

    def list(self, turn_id, *, origin=None, states=None):
        with self.sessions.storage.connection() as conn:
            rows = conn.execute(
                "SELECT operation_id FROM execution_actions WHERE session_id=? AND turn_id=? "
                "ORDER BY sequence",
                (self.owner, turn_id),
            ).fetchall()
            values = [self._get(conn, row[0]) for row in rows]
        return [
            a
            for a in values
            if (origin is None or a["origin"] == origin)
            and (states is None or a["state"] in states)
        ]

    def _event(self, conn, action, state):
        self.sessions.append_in(
            conn,
            {
                "event_id": f"action:{action['operation_id']}:{state}",
                "turn_id": action["turn_id"],
                "type": "action_state",
                "scope": "session",
                "content": {"operation_id": action["operation_id"], "state": state},
            },
        )

    def accept_many(self, turn_id, actions):
        with self.sessions.storage.transaction() as conn:
            for raw in actions:
                value = deepcopy(raw)
                if (
                    not value.get("operation_id")
                    or not value.get("kind")
                    or value.get("origin") not in {"model", "system"}
                    or not isinstance(value.get("payload"), dict)
                ):
                    raise ValueError("invalid execution action")
                value.update(turn_id=turn_id)
                value.setdefault("action_id", value["operation_id"])
                value.setdefault("batch_id", turn_id)
                parent = value["payload"].get("parent_call_id")
                value.setdefault("parent_action_id", turn_id + ":" + parent if parent else None)
                existing = self._get(conn, value["operation_id"])
                if existing:
                    definition = {
                        k: v for k, v in existing.items() if k not in {"state", "result_ref"}
                    }
                    if definition != value:
                        raise ValueError("action identity reused with different parameters")
                    continue
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM execution_actions WHERE session_id=?",
                    (self.owner,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO execution_actions VALUES (?,?,?,?,?,?,?)",
                    (
                        self.owner,
                        value["operation_id"],
                        turn_id,
                        sequence,
                        encode(value),
                        "queued",
                        None,
                    ),
                )
                self._event(conn, value, "queued")
        return [self.get(a["operation_id"]) for a in actions]

    def claim(self, operation_id):
        with self.sessions.storage.transaction() as conn:
            action = self._get(conn, operation_id)
            if not action or action["state"] != "queued":
                return False
            conn.execute(
                "UPDATE execution_actions SET state='running' "
                "WHERE session_id=? AND operation_id=?",
                (self.owner, operation_id),
            )
            self._event(conn, action, "running")
            return True

    def finish(self, operation_id, state, result_ref):
        if state not in {"completed", "unknown", "rejected"}:
            raise ValueError("invalid final action state")
        with self.sessions.storage.transaction() as conn:
            action = self._get(conn, operation_id)
            if not action:
                raise ValueError("unknown execution action")
            if action["state"] in {"completed", "unknown", "rejected"}:
                if (action["state"], action["result_ref"]) != (state, result_ref):
                    raise ValueError("execution action is already final")
                return
            if not conn.execute(
                "SELECT 1 FROM events WHERE session_id=? AND event_id=? AND turn_id=?",
                (self.owner, result_ref, action["turn_id"]),
            ).fetchone():
                raise ValueError("action result is outside this turn and session")
            conn.execute(
                "UPDATE execution_actions SET state=?,result_ref=? "
                "WHERE session_id=? AND operation_id=?",
                (state, result_ref, self.owner, operation_id),
            )
            self._event(conn, action, state)

    def result(self, action):
        if not action.get("result_ref"):
            return None
        with self.sessions.storage.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE session_id=? AND event_id=?",
                (self.owner, action["result_ref"]),
            ).fetchone()
        return self.sessions._decode(row[0])["content"] if row else None
