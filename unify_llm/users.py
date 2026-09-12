from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_USERS_DB = Path("data") / "unify_users.db"

# Raw key shape: sk-unify-<32 hex>. Prefix shown in UI is the first 8 hex chars.
KEY_SCHEME = "sk-unify-"
KEY_HEX_CHARS = 32
KEY_PREFIX_CHARS = 8


def _now() -> float:
    return time.time()


def hash_api_key(raw: str) -> str:
    """SHA-256 hex digest of the raw API key. Never store the raw key."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix).

    raw_key format: sk-unify-<32 hex>. Returned once at creation only.
    key_prefix is the first 8 hex characters (stable display id, not a secret).
    """
    hex_part = secrets.token_hex(KEY_HEX_CHARS // 2)
    raw = f"{KEY_SCHEME}{hex_part}"
    return raw, hash_api_key(raw), hex_part[:KEY_PREFIX_CHARS]


def _row_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "email": row["email"],
        "note": row["note"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": float(row["created_at"]),
    }


def _row_key(row: sqlite3.Row, *, include_user: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "key_prefix": row["key_prefix"],
        "name": row["name"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": float(row["created_at"]),
        "last_used_at": float(row["last_used_at"]) if row["last_used_at"] is not None else None,
        "revoked_at": float(row["revoked_at"]) if row["revoked_at"] is not None else None,
    }
    if include_user:
        item["user_name"] = row["user_name"] if "user_name" in row.keys() else ""
    return item


class UserStore:
    """SQLite store for LAN users and their API keys.

    Raw keys are never persisted. create_api_key returns the raw key once.
    Default path: data/unify_users.db (override with UNIFY_USERS_DB).
    """

    def __init__(self, path: Path | str | None = None):
        if path is None:
            env = os.environ.get("UNIFY_USERS_DB") or ""
            path = Path(env) if env else DEFAULT_USERS_DB
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
                CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
                """
            )
            self._conn.commit()

    # ── users ──────────────────────────────────────────────────────────────

    def create_user(
        self,
        name: str,
        email: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        email_v = (email or "").strip() or None
        note_v = (note or "").strip()
        now = _now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users(name, email, note, enabled, created_at) VALUES(?,?,?,?,?)",
                    (name, email_v, note_v, 1, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise ValueError(f"email already exists: {email_v}") from e
            uid = int(cur.lastrowid)
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        assert row is not None
        return _row_user(row)

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [_row_user(r) for r in rows]

    def set_user_enabled(self, user_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET enabled=? WHERE id=?",
                (1 if enabled else 0, int(user_id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def delete_user(self, user_id: int) -> bool:
        """Delete a user and cascade-delete their keys."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE id=?", (int(user_id),))
            self._conn.commit()
            return cur.rowcount > 0

    # ── api keys ───────────────────────────────────────────────────────────

    def create_api_key(self, user_id: int, name: str = "") -> dict[str, Any]:
        """Issue a new API key. Returns dict including raw_key exactly once."""
        user = self.get_user(user_id)
        if user is None:
            raise ValueError(f"unknown user_id: {user_id}")
        raw, key_hash, prefix = generate_api_key()
        now = _now()
        name_v = (name or "").strip()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO api_keys(user_id, key_hash, key_prefix, name, enabled, created_at)
                VALUES(?,?,?,?,1,?)
                """,
                (int(user_id), key_hash, prefix, name_v, now),
            )
            self._conn.commit()
            kid = int(cur.lastrowid)
            row = self._conn.execute("SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone()
        assert row is not None
        meta = _row_key(row)
        meta["raw_key"] = raw
        return meta

    def list_keys_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """List keys for a user. Never includes raw keys or hashes."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_keys WHERE user_id=? ORDER BY created_at ASC, id ASC",
                (int(user_id),),
            ).fetchall()
        return [_row_key(r) for r in rows]

    def list_keys(self, *, include_revoked: bool = True) -> list[dict[str, Any]]:
        """All keys with owning user name. Prefix only — never raw keys."""
        sql = """
            SELECT k.*, u.name AS user_name
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
        """
        if not include_revoked:
            sql += " WHERE k.revoked_at IS NULL"
        sql += " ORDER BY k.created_at ASC, k.id ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [_row_key(r, include_user=True) for r in rows]

    def revoke_key(self, key_id: int) -> dict[str, Any] | None:
        """Revoke a key (sets revoked_at + disables). Idempotent."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE api_keys
                SET revoked_at=COALESCE(revoked_at, ?), enabled=0
                WHERE id=?
                """,
                (now, int(key_id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM api_keys WHERE id=?", (int(key_id),)).fetchone()
        return _row_key(row) if row else None

    def set_key_enabled(self, key_id: int, enabled: bool) -> dict[str, Any] | None:
        """Enable/disable a key without revoking. Refuses to enable revoked keys."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM api_keys WHERE id=?", (int(key_id),)).fetchone()
            if row is None:
                return None
            if enabled and row["revoked_at"] is not None:
                raise ValueError("cannot enable a revoked key")
            self._conn.execute(
                "UPDATE api_keys SET enabled=? WHERE id=?",
                (1 if enabled else 0, int(key_id)),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM api_keys WHERE id=?", (int(key_id),)).fetchone()
        return _row_key(row) if row else None

    def touch_last_used(self, key_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?",
                (_now(), int(key_id)),
            )
            self._conn.commit()

    def authenticate_key(self, raw: str) -> dict[str, Any] | None:
        """Validate a raw API key. Returns user+key meta or None.

        Rejects revoked keys, disabled keys, and keys whose owner is disabled.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        digest = hash_api_key(raw)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT k.*, u.name AS user_name, u.email AS user_email,
                       u.enabled AS user_enabled, u.note AS user_note
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.key_hash=?
                """,
                (digest,),
            ).fetchone()
        if row is None:
            return None
        if not row["enabled"] or row["revoked_at"] is not None:
            return None
        if not row["user_enabled"]:
            return None
        return {
            "user_id": int(row["user_id"]),
            "username": row["user_name"],
            "user": {
                "id": int(row["user_id"]),
                "name": row["user_name"],
                "email": row["user_email"],
                "note": row["user_note"] or "",
                "enabled": True,
            },
            "key": {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "key_prefix": row["key_prefix"],
                "name": row["name"] or "",
                "enabled": True,
                "created_at": float(row["created_at"]),
                "last_used_at": (
                    float(row["last_used_at"]) if row["last_used_at"] is not None else None
                ),
                "revoked_at": None,
            },
            "key_id": int(row["id"]),
        }

    def has_any_active_key(self) -> bool:
        """True if at least one non-revoked, enabled key exists for an enabled user."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.enabled=1 AND k.revoked_at IS NULL AND u.enabled=1
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
