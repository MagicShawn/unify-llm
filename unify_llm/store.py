from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class StatsStore:
    """SQLite persistence for lifetime token/cost totals (survives restart)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def load_totals(self) -> dict[str, int | float]:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k='totals'").fetchone()
        if not row:
            return {
                "requests": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
            }
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return {
                "requests": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
            }
        return {
            "requests": int(data.get("requests") or 0),
            "errors": int(data.get("errors") or 0),
            "prompt_tokens": int(data.get("prompt_tokens") or 0),
            "completion_tokens": int(data.get("completion_tokens") or 0),
            "cost_usd": float(data.get("cost_usd") or 0.0),
        }

    def save_totals(self, totals: dict[str, Any]) -> None:
        payload = {
            "requests": int(totals.get("requests") or 0),
            "errors": int(totals.get("errors") or 0),
            "prompt_tokens": int(totals.get("prompt_tokens") or 0),
            "completion_tokens": int(totals.get("completion_tokens") or 0),
            "cost_usd": float(totals.get("cost_usd") or 0.0),
            "updated_at": time.time(),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(k,v) VALUES('totals',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(payload),),
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
