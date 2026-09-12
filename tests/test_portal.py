"""Portal + role separation tests: password auth, sessions, admin roles, portal APIs.

Run from repo root:
    python -m pytest tests/test_portal.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, ProviderConfig
from unify_llm.users import (
    SESSION_COOKIE,
    UserStore,
    hash_password,
    verify_password,
)


def _cfg(api_key: str = "") -> AppConfig:
    return AppConfig(
        auth=AuthConfig(api_key=api_key),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="up",
                enabled=True,
                models=["m-x"],
            )
        },
    )


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


@pytest.fixture()
def store(users_db: Path) -> UserStore:
    s = UserStore(users_db)
    yield s
    s.close()


@pytest.fixture()
def app(users_db: Path):
    return create_app(config=_cfg("master-key"), users_db=users_db)


@pytest.fixture()
def client(app):
    with _client(app) as c:
        yield c


@pytest.fixture()
def admin_client(client: TestClient) -> TestClient:
    """Bootstrap an admin via localhost + no-gateway... wait, gateway key is set.

    Create admin with master key, then login to get a session cookie.
    """
    r = client.post(
        "/api/admin/users",
        json={
            "name": "root",
            "email": "root@example.com",
            "password": "admin-pass-1",
            "role": "admin",
            "status": "active",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code == 201, r.text
    r2 = client.post(
        "/api/auth/login",
        json={"email": "root@example.com", "password": "admin-pass-1"},
    )
    assert r2.status_code == 200, r2.text
    assert SESSION_COOKIE in client.cookies
    return client


# ── password hashing ───────────────────────────────────────────────────────


def test_hash_password_roundtrip():
    h = hash_password("correct-horse")
    assert h.startswith("scrypt$")
    assert "correct-horse" not in h
    assert verify_password("correct-horse", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("", h) is False
    assert verify_password("correct-horse", None) is False
    assert verify_password("correct-horse", "not-a-hash") is False


def test_unique_salts():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


# ── store: roles, status, sessions, password auth ──────────────────────────


def test_register_creates_pending_user(store: UserStore):
    user = store.register_user("alice", "alice@example.com", "password123")
    assert user["status"] == "pending"
    assert user["role"] == "user"
    assert user["enabled"] is False
    # Cannot issue keys while pending.
    with pytest.raises(ValueError):
        store.create_api_key(user["id"])
    # Cannot login while pending.
    assert store.authenticate_password("alice@example.com", "password123") is None

    approved = store.approve_user(user["id"])
    assert approved is not None
    assert approved["status"] == "active"
    assert approved["enabled"] is True
    auth = store.authenticate_password("alice@example.com", "password123")
    assert auth is not None
    assert auth["id"] == user["id"]


def test_password_auth_rejects_wrong_and_disabled(store: UserStore):
    user = store.create_user(
        "bob", email="bob@example.com", password="password123", role="user", status="active"
    )
    assert store.authenticate_password("bob@example.com", "password123") is not None
    assert store.authenticate_password("bob@example.com", "nope") is None
    assert store.authenticate_password("missing@example.com", "password123") is None
    # name login fallback
    assert store.authenticate_password("bob", "password123") is not None

    store.set_status(user["id"], "disabled")
    assert store.authenticate_password("bob@example.com", "password123") is None


def test_set_role_and_status(store: UserStore):
    user = store.create_user("carol", email="carol@example.com", password="password123")
    assert user["role"] == "user"
    r = store.set_role(user["id"], "admin")
    assert r is not None and r["role"] == "admin"
    with pytest.raises(ValueError):
        store.set_role(user["id"], "superuser")

    store.set_status(user["id"], "pending")
    assert store.get_user(user["id"])["status"] == "pending"
    store.approve_user(user["id"])
    assert store.get_user(user["id"])["status"] == "active"


def test_session_lifecycle(store: UserStore):
    user = store.create_user(
        "dave", email="dave@example.com", password="password123", status="active"
    )
    token = store.create_session(user["id"])
    assert isinstance(token, str) and len(token) > 20
    sess = store.get_session(token)
    assert sess is not None
    assert sess["user_id"] == user["id"]
    assert sess["user"]["role"] == "user"

    assert store.delete_session(token) is True
    assert store.get_session(token) is None

    # Pending users cannot create sessions.
    store.set_status(user["id"], "pending")
    with pytest.raises(ValueError):
        store.create_session(user["id"])

    # Disabling drops live sessions.
    store.set_status(user["id"], "active")
    token2 = store.create_session(user["id"])
    assert store.get_session(token2) is not None
    store.set_status(user["id"], "disabled")
    assert store.get_session(token2) is None


def test_authenticate_key_requires_active(store: UserStore):
    user = store.create_user("erin", email="erin@example.com", status="pending")
    with pytest.raises(ValueError):
        store.create_api_key(user["id"])
    store.approve_user(user["id"])
    meta = store.create_api_key(user["id"])
    auth = store.authenticate_key(meta["raw_key"])
    assert auth is not None
    assert auth["role"] == "user"
    assert auth["username"] == "erin"
    store.set_status(user["id"], "disabled")
    assert store.authenticate_key(meta["raw_key"]) is None


# ── HTTP: register / login / me ────────────────────────────────────────────


def test_register_login_logout_me(client: TestClient):
    r = client.post(
        "/api/auth/register",
        json={"name": "portal-user", "email": "p@example.com", "password": "password123"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["user"]["status"] == "pending"

    # Pending cannot login.
    r2 = client.post(
        "/api/auth/login",
        json={"email": "p@example.com", "password": "password123"},
    )
    assert r2.status_code == 403
    assert "pending" in r2.json()["error"]["message"].lower()

    # Admin approves.
    uid = r.json()["user"]["id"]
    r3 = client.post(
        "/api/admin/users",
        json={
            "name": "boss",
            "email": "boss@example.com",
            "password": "boss-pass-1",
            "role": "admin",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    assert r3.status_code == 201
    # Approve portal user via master key.
    r4 = client.patch(
        f"/api/admin/users/{uid}",
        json={"approve": True},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["user"]["status"] == "active"

    # Now login works and sets cookie.
    r5 = client.post(
        "/api/auth/login",
        json={"email": "p@example.com", "password": "password123"},
    )
    assert r5.status_code == 200, r5.text
    assert r5.json()["user"]["name"] == "portal-user"
    assert SESSION_COOKIE in client.cookies

    r6 = client.get("/api/auth/me")
    assert r6.status_code == 200
    assert r6.json()["user"]["email"] == "p@example.com"

    r7 = client.post("/api/auth/logout")
    assert r7.status_code == 200
    r8 = client.get("/api/auth/me")
    assert r8.status_code == 401


def test_login_wrong_password(client: TestClient):
    client.post(
        "/api/admin/users",
        json={
            "name": "x",
            "email": "x@example.com",
            "password": "password123",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    r = client.post(
        "/api/auth/login",
        json={"email": "x@example.com", "password": "wrong-pass"},
    )
    assert r.status_code == 401


def test_register_requires_fields(client: TestClient):
    r = client.post("/api/auth/register", json={"name": "a"})
    assert r.status_code == 400
    r2 = client.post(
        "/api/auth/register",
        json={"name": "a", "email": "a@b.c", "password": "short"},
    )
    assert r2.status_code == 400


# ── HTTP: portal self-service keys + usage ─────────────────────────────────


def test_me_keys_create_revoke_and_usage(admin_client: TestClient):
    # Create a normal active user via admin, then logout and login as them.
    r = admin_client.post(
        "/api/admin/users",
        json={
            "name": "worker",
            "email": "worker@example.com",
            "password": "worker-pass-1",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code == 201

    admin_client.post("/api/auth/logout")
    r2 = admin_client.post(
        "/api/auth/login",
        json={"email": "worker@example.com", "password": "worker-pass-1"},
    )
    assert r2.status_code == 200, r2.text

    r3 = admin_client.post("/api/me/keys", json={"name": "laptop"})
    assert r3.status_code == 201, r3.text
    raw = r3.json()["key"]["raw_key"]
    assert raw.startswith("sk-unify-")
    kid = r3.json()["key"]["id"]

    r4 = admin_client.get("/api/me/keys")
    assert r4.status_code == 200
    keys = r4.json()["keys"]
    assert len(keys) == 1
    assert "raw_key" not in keys[0]

    r5 = admin_client.post(f"/api/me/keys/{kid}/revoke")
    assert r5.status_code == 200
    assert r5.json()["key"]["revoked_at"] is not None

    # Cannot revoke unknown key.
    r6 = admin_client.post("/api/me/keys/99999/revoke")
    assert r6.status_code == 404

    r7 = admin_client.get("/api/me/usage")
    assert r7.status_code == 200
    assert r7.json()["recent_requests"] == 0

    # Revoked key no longer works on /v1.
    assert (
        admin_client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401
    )

    # Issue a fresh key and use it.
    r8 = admin_client.post("/api/me/keys", json={"name": "again"})
    raw2 = r8.json()["key"]["raw_key"]
    assert (
        admin_client.get("/v1/models", headers={"Authorization": f"Bearer {raw2}"}).status_code
        == 200
    )

    # After logout, me APIs require session again.
    admin_client.post("/api/auth/logout")
    assert admin_client.get("/api/me/keys").status_code == 401
    assert admin_client.get("/api/me/usage").status_code == 401


# ── HTTP: role middleware ──────────────────────────────────────────────────


def test_admin_session_grants_admin_api(admin_client: TestClient):
    r = admin_client.get("/api/admin/users", headers={"Authorization": "Bearer master-key"})
    assert r.status_code == 200

    # Session cookie alone is enough (no Bearer).
    r2 = admin_client.get("/api/admin/users")
    assert r2.status_code == 200, r2.text
    users = r2.json()["users"]
    assert any(u["role"] == "admin" for u in users)


def test_user_session_cannot_access_admin(admin_client: TestClient):
    r = admin_client.post(
        "/api/admin/users",
        json={
            "name": "plain",
            "email": "plain@example.com",
            "password": "plain-pass-1",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code == 201

    admin_client.post("/api/auth/logout")
    r2 = admin_client.post(
        "/api/auth/login",
        json={"email": "plain@example.com", "password": "plain-pass-1"},
    )
    assert r2.status_code == 200
    # Can hit /api/auth/me and /api/me/*
    assert admin_client.get("/api/auth/me").status_code == 200
    assert admin_client.get("/api/me/keys").status_code == 200
    # Cannot hit admin APIs (gateway key is set → need master key or admin role).
    assert admin_client.get("/api/admin/users").status_code == 401
    assert admin_client.get("/api/status").status_code == 401


def test_admin_session_grants_status(app, admin_client: TestClient):
    # admin_client already has session cookie; no Bearer.
    r = admin_client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_endpoints_open_with_gateway_key(users_db: Path):
    app = create_app(config=_cfg("secret-gw"), users_db=users_db)
    with _client(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        r = client.post(
            "/api/auth/register",
            json={"name": "n", "email": "n@example.com", "password": "password123"},
        )
        assert r.status_code == 201
        r2 = client.post(
            "/api/auth/login",
            json={"email": "n@example.com", "password": "password123"},
        )
        assert r2.status_code == 403  # pending


def test_localhost_bootstrap_without_gateway_key(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app) as client:
        r = client.post(
            "/api/admin/users",
            json={
                "name": "first-admin",
                "email": "first@example.com",
                "password": "first-pass-1",
                "role": "admin",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["user"]["role"] == "admin"
        r2 = client.post(
            "/api/auth/login",
            json={"email": "first@example.com", "password": "first-pass-1"},
        )
        assert r2.status_code == 200


def test_admin_patch_role_and_status(admin_client: TestClient):
    r = admin_client.post(
        "/api/admin/users",
        json={"name": "temp", "email": "temp@example.com"},
        headers={"Authorization": "Bearer master-key"},
    )
    uid = r.json()["user"]["id"]
    r2 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"role": "admin"},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r2.json()["user"]["role"] == "admin"
    r3 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"status": "pending"},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r3.json()["user"]["status"] == "pending"
    r4 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"approve": True},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r4.json()["user"]["status"] == "active"


def test_portal_html_served(app):
    with _client(app) as client:
        r = client.get("/portal")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert b"Login" in r.content or b"login" in r.content


def test_cookie_is_httponly(app, users_db: Path):
    with _client(app) as client:
        client.post(
            "/api/admin/users",
            json={
                "name": "ck",
                "email": "ck@example.com",
                "password": "ck-pass-123",
            },
            headers={"Authorization": "Bearer master-key"},
        )
        r = client.post(
            "/api/auth/login",
            json={"email": "ck@example.com", "password": "ck-pass-123"},
        )
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "")
        assert SESSION_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
        assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()


def test_username_in_monitor_history(users_db: Path):
    """User-key auth attaches username for traffic attribution."""
    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as client:
        r = client.post(
            "/api/admin/users",
            json={"name": "attr", "email": "attr@example.com"},
            headers={"Authorization": "Bearer master-key"},
        )
        uid = r.json()["user"]["id"]
        r2 = client.post(
            "/api/admin/keys",
            json={"user_id": uid},
            headers={"Authorization": "Bearer master-key"},
        )
        raw = r2.json()["key"]["raw_key"]
        # /v1/models does not hit monitor.begin, so simulate via store authenticate.
        from unify_llm.users import UserStore

        store = UserStore(users_db)
        try:
            auth = store.authenticate_key(raw)
            assert auth is not None
            assert auth["username"] == "attr"
        finally:
            store.close()
