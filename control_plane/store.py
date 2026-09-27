"""SQLite-backed store.

Both processes in the demo (the MCP server the AI agent talks to, and the
`ctl` CLI the human uses) share this database, so proposals created by the
agent show up immediately for human review.

Entities are stored as JSON blobs; this is a reference implementation, not a
production schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

DEFAULT_DB_PATH = os.environ.get(
    "CTL_DB_PATH", os.path.join(os.path.dirname(__file__), "..", ".state", "ctl.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rollouts  (id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv        (key TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | None = None):
        self.db_path = os.path.abspath(db_path or DEFAULT_DB_PATH)
        self._db: sqlite3.Connection | None = None
        self._inode: int | None = None

    @property
    def _conn(self) -> sqlite3.Connection:
        """Connection to the DB file *currently* at db_path.

        The MCP server is long-lived; if someone deletes .state/ under it
        (`rm -rf .state`), a cached connection would keep writing to the
        unlinked file (or fail read-only) and its proposals would never reach
        `ctl`. Reconnect whenever the path no longer names the file we opened.
        """
        try:
            inode = os.stat(self.db_path).st_ino
        except FileNotFoundError:
            inode = None
        if self._db is None or inode != self._inode:
            if self._db is not None:
                self._db.close()
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._db = sqlite3.connect(self.db_path)
            self._db.executescript(_SCHEMA)
            self._db.commit()
            self._inode = os.stat(self.db_path).st_ino
        return self._db

    def reset(self) -> None:
        """Empty every table in place. Unlike deleting the file, this is seen
        immediately by every process sharing the DB."""
        conn = self._conn
        with conn:
            for table in ("proposals", "rollouts", "kv", "audit"):
                conn.execute(f"DELETE FROM {table}")

    # -- generic helpers ----------------------------------------------------

    def _put(self, table: str, id_: str, obj: dict[str, Any]) -> None:
        self._conn.execute(
            f"INSERT INTO {table}(id, json) VALUES(?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET json=excluded.json",
            (id_, json.dumps(obj)),
        )
        self._conn.commit()

    def _get(self, table: str, id_: str) -> dict[str, Any] | None:
        row = self._conn.execute(f"SELECT json FROM {table} WHERE id=?", (id_,)).fetchone()
        return json.loads(row[0]) if row else None

    def _all(self, table: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(f"SELECT json FROM {table}").fetchall()
        return [json.loads(r[0]) for r in rows]

    # -- proposals / rollouts ------------------------------------------------

    def put_proposal(self, p: dict[str, Any]) -> None:
        self._put("proposals", p["proposal_id"], p)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._get("proposals", proposal_id)

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self._all("proposals")
        if status:
            items = [p for p in items if p["status"] == status]
        return sorted(items, key=lambda p: p["created_at"], reverse=True)

    def put_rollout(self, r: dict[str, Any]) -> None:
        self._put("rollouts", r["rollout_id"], r)

    def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        return self._get("rollouts", rollout_id)

    # -- kv (fleet + scenario state) ------------------------------------------

    def put_kv(self, key: str, obj: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, json) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET json=excluded.json",
            (key, json.dumps(obj)),
        )
        self._conn.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # -- audit -----------------------------------------------------------------

    def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO audit(ts, actor, action, detail) VALUES(?, ?, ?, ?)",
            (time.time(), actor, action, json.dumps(detail)),
        )
        self._conn.commit()

    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, ts, actor, action, detail FROM audit ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"seq": r[0], "ts": r[1], "actor": r[2], "action": r[3], "detail": json.loads(r[4])}
            for r in rows
        ]
