from __future__ import annotations

import hashlib
import hmac
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

# Password hashing: scrypt (stdlib). Format: scrypt$n$r$p$salt_hex$hash_hex
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# Session cookie / table
SESSION_COOKIE = "unify_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
SESSION_TOKEN_BYTES = 32

ROLES = ("admin", "user")
STATUSES = ("pending", "active", "disabled")


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


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password with scrypt. Returns scrypt$n$r$p$salt_hex$hash_hex.

    Never store plaintext. Salt is random per call unless provided.
    """
    if not password:
        raise ValueError("password is required")
    salt_b = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_b,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time password check against a scrypt hash string."""
    if not password or not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$", 5)
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected) or _SCRYPT_DKLEN,
    )
    return hmac.compare_digest(dk, expected)


def _normalize_role(role: str | None, default: str = "user") -> str:
    r = (role or default or "user").strip().lower()
    if r not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    return r


def _normalize_status(status: str | None, default: str = "active") -> str:
    s = (status or default or "active").strip().lower()
    if s not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    return s


def _status_to_enabled(status: str) -> int:
    return 1 if status == "active" else 0


def _row_user(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    # Prefer explicit status/role columns; fall back for pre-migration rows.
    if "status" in keys and row["status"]:
        status = str(row["status"])
    else:
        status = "active" if bool(row["enabled"]) else "disabled"
    role = str(row["role"]) if "role" in keys and row["role"] else "user"
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "email": row["email"],
        "note": row["note"] or "",
        "enabled": bool(row["enabled"]),
        "role": role,
        "status": status,
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
    """SQLite store for LAN users, passwords, sessions, and API keys.

    Raw keys are never persisted. create_api_key returns the raw key once.
    Passwords are stored as scrypt hashes only.
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
        self._migrate_schema()

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

    def _migrate_schema(self) -> None:
        """Add password_hash / role / status columns and sessions table (idempotent)."""
        with self._lock:
            cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "password_hash" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            if "role" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
                )
            if "status" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN status TEXT")
                # Map existing enabled → status.
                self._conn.execute(
                    "UPDATE users SET status = CASE WHEN enabled=1 THEN 'active' "
                    "ELSE 'disabled' END WHERE status IS NULL OR status=''"
                )
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                """
            )
            self._conn.commit()

    # ── users ──────────────────────────────────────────────────────────────

    def create_user(
        self,
        name: str,
        email: str | None = None,
        note: str = "",
        *,
        password: str | None = None,
        role: str = "user",
        status: str = "active",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        email_v = (email or "").strip() or None
        note_v = (note or "").strip()
        role_v = _normalize_role(role)
        status_v = _normalize_status(status)
        pw_hash = hash_password(password) if password else None
        now = _now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO users(name, email, note, enabled, created_at,
                                      password_hash, role, status)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        name,
                        email_v,
                        note_v,
                        _status_to_enabled(status_v),
                        now,
                        pw_hash,
                        role_v,
                        status_v,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise ValueError(f"email already exists: {email_v}") from e
            uid = int(cur.lastrowid)
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        assert row is not None
        return _row_user(row)

    def register_user(self, name: str, email: str, password: str) -> dict[str, Any]:
        """Self-service signup: creates a pending user with a password."""
        email_v = (email or "").strip()
        if not email_v:
            raise ValueError("email is required")
        if not password or len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        return self.create_user(
            name=name,
            email=email_v,
            note="self-registered",
            password=password,
            role="user",
            status="pending",
        )

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        email_v = (email or "").strip()
        if not email_v:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email=?", (email_v,)
            ).fetchone()
        return _row_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [_row_user(r) for r in rows]

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def set_user_enabled(self, user_id: int, enabled: bool) -> dict[str, Any] | None:
        """Enable/disable; keeps status in sync (active ↔ disabled)."""
        status = "active" if enabled else "disabled"
        return self.set_status(user_id, status)

    def set_status(self, user_id: int, status: str) -> dict[str, Any] | None:
        status_v = _normalize_status(status)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET status=?, enabled=? WHERE id=?",
                (status_v, _status_to_enabled(status_v), int(user_id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def approve_user(self, user_id: int) -> dict[str, Any] | None:
        """Approve a pending user → active."""
        return self.set_status(user_id, "active")

    def set_role(self, user_id: int, role: str) -> dict[str, Any] | None:
        role_v = _normalize_role(role)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET role=? WHERE id=?",
                (role_v, int(user_id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def set_password(self, user_id: int, password: str) -> dict[str, Any] | None:
        if not password or len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        pw_hash = hash_password(password)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (pw_hash, int(user_id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
        return _row_user(row) if row else None

    def authenticate_password(self, login: str, password: str) -> dict[str, Any] | None:
        """Validate email + password. Returns user meta or None.

        Rejects users that are not status=active (pending / disabled cannot login).
        """
        login_v = (login or "").strip()
        if not login_v or not password:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email=?", (login_v,)
            ).fetchone()
            if row is None:
                # Allow name login as fallback (useful for bootstrap admins).
                row = self._conn.execute(
                    "SELECT * FROM users WHERE name=?", (login_v,)
                ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        user = _row_user(row)
        if user["status"] != "active":
            return None
        return user

    def delete_user(self, user_id: int) -> bool:
        """Delete a user and cascade-delete their keys + sessions."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE id=?", (int(user_id),))
            self._conn.commit()
            return cur.rowcount > 0

    # ── sessions ───────────────────────────────────────────────────────────

    def create_session(self, user_id: int, *, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        """Issue a session token for an active user. Returns the raw token."""
        user = self.get_user(user_id)
        if user is None:
            raise ValueError(f"unknown user_id: {user_id}")
        if user["status"] != "active":
            raise ValueError("only active users can create sessions")
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        now = _now()
        expires = now + max(60, int(ttl_seconds))
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
                (token, int(user_id), now, expires),
            )
            self._conn.commit()
        return token

    def get_session(self, token: str) -> dict[str, Any] | None:
        """Resolve a session token → {token, user_id, user, expires_at} or None."""
        tok = (token or "").strip()
        if not tok:
            return None
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE token=?", (tok,)
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) < now:
                self._conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
                self._conn.commit()
                return None
            uid = int(row["user_id"])
            urow = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if urow is None:
            return None
        user = _row_user(urow)
        if user["status"] != "active":
            # Stale session after disable/pending — drop it.
            self.delete_session(tok)
            return None
        return {
            "token": tok,
            "user_id": uid,
            "user": user,
            "expires_at": float(row["expires_at"]),
            "created_at": float(row["created_at"]),
        }

    def delete_session(self, token: str) -> bool:
        tok = (token or "").strip()
        if not tok:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_sessions_for_user(self, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE user_id=?", (int(user_id),)
            )
            self._conn.commit()
            return cur.rowcount

    def purge_expired_sessions(self) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._conn.commit()
            return cur.rowcount

    # ── api keys ───────────────────────────────────────────────────────────

    def create_api_key(self, user_id: int, name: str = "") -> dict[str, Any]:
        """Issue a new API key. Returns dict including raw_key exactly once."""
        user = self.get_user(user_id)
        if user is None:
            raise ValueError(f"unknown user_id: {user_id}")
        if user["status"] != "active":
            raise ValueError("cannot issue keys for pending/disabled users")
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

        Rejects revoked keys, disabled keys, and keys whose owner is not active.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        digest = hash_api_key(raw)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT k.*, u.name AS user_name, u.email AS user_email,
                       u.enabled AS user_enabled, u.note AS user_note,
                       u.role AS user_role, u.status AS user_status
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
        status = row["user_status"] or ("active" if row["user_enabled"] else "disabled")
        if status != "active" or not row["user_enabled"]:
            return None
        return {
            "user_id": int(row["user_id"]),
            "username": row["user_name"],
            "role": (row["user_role"] or "user"),
            "user": {
                "id": int(row["user_id"]),
                "name": row["user_name"],
                "email": row["user_email"],
                "note": row["user_note"] or "",
                "enabled": True,
                "role": (row["user_role"] or "user"),
                "status": "active",
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
        """True if at least one non-revoked, enabled key exists for an active user."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.enabled=1 AND k.revoked_at IS NULL AND u.enabled=1
                  AND (u.status IS NULL OR u.status = 'active')
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
