"""
registry.py - Tiny case index.

LangGraph's checkpointer stores state *per thread* but offers no cheap "list my
open cases" query. The registry is a small side table (same SQLite file) mapping
thread_id -> case metadata so the dashboard can enumerate cases, then ask the
graph for each one's live state. In production this would be your ticketing DB.
"""

import sqlite3
import threading
from datetime import datetime, timezone


class CaseRegistry:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS cases (
                       thread_id   TEXT PRIMARY KEY,
                       customer_id TEXT NOT NULL,
                       order_id    TEXT NOT NULL,
                       issue       TEXT NOT NULL,
                       created_at  TEXT NOT NULL
                   )"""
            )
            self._conn.commit()

    def add(self, thread_id: str, customer_id: str, order_id: str, issue: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?, ?)",
                (thread_id, customer_id, order_id, issue,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cases ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
