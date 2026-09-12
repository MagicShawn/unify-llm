"""Multi-user API key tests: store, auth via /v1/*, admin endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, ProviderConfig
from unify_llm.users import UserStore, generate_api_key, hash_api_key


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
    # Default TestClient host is "testclient"; admin localhost check needs 127.0.0.1.
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


@pytest.fixture()
def store(users_db: Path) -> UserStore:
    s = UserStore(users_db)
    yield s
    s.close()


# ── store ──────────────────────────────────────────────────────────────────


def test_generate_key_format():
    raw, digest, prefix = generate_api_key()
    assert raw.startswith("sk-unify-")
    assert len(raw) == len("sk-unify-") + 32
    hex_part = raw[len("sk-unify-") :]
    assert len(hex_part) == 32
    int(hex_part, 16)  # valid hex
    assert prefix == hex_part[:8]
    assert digest == hash_api_key(raw)
    assert raw not in digest


def test_create_user_and_issue_key(store: UserStore):
    user = store.create_user("alice", email="alice@example.com", note="lan")
    assert user["id"] >= 1
    assert user["name"] == "alice"
    assert user["enabled"] is True

    meta = store.create_api_key(user["id"], name="laptop")
    assert "raw_key" in meta
    assert meta["raw_key"].startswith("sk-unify-")
    assert meta["key_prefix"]
    assert len(meta["key_prefix"]) == 8
    # never list the raw key
    listed = store.list_keys_for_user(user["id"])
    assert len(listed) == 1
    assert "raw_key" not in listed[0]
    assert "key_hash" not in listed[0]
    assert listed[0]["key_prefix"] == meta["key_prefix"]
    assert listed[0]["key_prefix"] != meta["raw_key"]


def test_duplicate_email_rejected(store: UserStore):
    store.create_user("a", email="dup@example.com")
    with pytest.raises(ValueError):
        store.create_user("b", email="dup@example.com")


def test_authenticate_valid_and_invalid(store: UserStore):
    user = store.create_user("bob")
    meta = store.create_api_key(user["id"])
    raw = meta["raw_key"]

    auth = store.authenticate_key(raw)
    assert auth is not None
    assert auth["user_id"] == user["id"]
    assert auth["username"] == "bob"
    assert auth["key_id"] == meta["id"]

    assert store.authenticate_key("sk-unify-" + "0" * 32) is None
    assert store.authenticate_key("") is None
    assert store.authenticate_key("not-a-key") is None


def test_disabled_user_and_revoked_key(store: UserStore):
    user = store.create_user("carol")
    meta = store.create_api_key(user["id"])
    raw = meta["raw_key"]
    assert store.authenticate_key(raw) is not None

    store.set_user_enabled(user["id"], False)
    assert store.authenticate_key(raw) is None
    assert store.has_any_active_key() is False

    store.set_user_enabled(user["id"], True)
    assert store.authenticate_key(raw) is not None

    store.revoke_key(meta["id"])
    assert store.authenticate_key(raw) is None
    again = store.revoke_key(meta["id"])
    assert again is not None
    assert again["revoked_at"] is not None


def test_delete_user_cascades_keys(store: UserStore):
    user = store.create_user("dave")
    store.create_api_key(user["id"])
    store.create_api_key(user["id"], name="phone")
    assert len(store.list_keys_for_user(user["id"])) == 2
    assert store.delete_user(user["id"]) is True
    assert store.list_keys_for_user(user["id"]) == []
    assert store.get_user(user["id"]) is None


def test_touch_last_used(store: UserStore):
    user = store.create_user("erin")
    meta = store.create_api_key(user["id"])
    assert meta["last_used_at"] is None
    store.touch_last_used(meta["id"])
    keys = store.list_keys_for_user(user["id"])
    assert keys[0]["last_used_at"] is not None


# ── HTTP auth integration ──────────────────────────────────────────────────


def test_user_key_authenticates_v1_models(users_db: Path):
    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as client:
        r = client.post(
            "/api/admin/users",
            json={"name": "alice", "email": "alice@example.com"},
            headers={"Authorization": "Bearer master-key"},
        )
        assert r.status_code == 201, r.text
        uid = r.json()["user"]["id"]

        r2 = client.post(
            "/api/admin/keys",
            json={"user_id": uid, "name": "desk"},
            headers={"Authorization": "Bearer master-key"},
        )
        assert r2.status_code == 201, r2.text
        key_body = r2.json()["key"]
        raw = key_body["raw_key"]
        assert raw.startswith("sk-unify-")

        r3 = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
        assert r3.status_code == 200
        assert r3.json()["object"] == "list"

        r4 = client.get("/v1/models", headers={"x-api-key": raw})
        assert r4.status_code == 200

        r5 = client.get("/v1/models", headers={"Authorization": "Bearer master-key"})
        assert r5.status_code == 200

        r6 = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert r6.status_code == 401

        r7 = client.get(
            "/api/admin/users",
            headers={"Authorization": "Bearer master-key"},
        )
        assert r7.status_code == 200
        listed = r7.json()["users"][0]["keys"][0]
        assert "raw_key" not in listed
        assert "key_hash" not in listed
        assert listed["key_prefix"] == key_body["key_prefix"]
        assert raw not in r7.text


def test_disabled_user_gets_401(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app) as client:
        r = client.post("/api/admin/users", json={"name": "bob"})
        assert r.status_code == 201
        uid = r.json()["user"]["id"]
        raw = client.post("/api/admin/keys", json={"user_id": uid}).json()["key"]["raw_key"]

        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 200

        r_off = client.patch(f"/api/admin/users/{uid}", json={"enabled": False})
        assert r_off.status_code == 200
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_revoked_key_gets_401(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app) as client:
        uid = client.post("/api/admin/users", json={"name": "carol"}).json()["user"]["id"]
        key = client.post("/api/admin/keys", json={"user_id": uid}).json()["key"]
        raw = key["raw_key"]
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 200

        r_rev = client.post(f"/api/admin/keys/{key['id']}/revoke")
        assert r_rev.status_code == 200
        assert r_rev.json()["key"]["revoked_at"] is not None
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_v1_requires_key_once_user_keys_exist(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app) as client:
        assert client.get("/v1/models").status_code == 200

        uid = client.post("/api/admin/users", json={"name": "dina"}).json()["user"]["id"]
        raw = client.post("/api/admin/keys", json={"user_id": uid}).json()["key"]["raw_key"]

        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_admin_requires_master_key_when_set(users_db: Path):
    app = create_app(config=_cfg("secret-gw"), users_db=users_db)
    with _client(app) as client:
        assert client.get("/api/admin/users").status_code == 401
        assert client.post("/api/admin/users", json={"name": "x"}).status_code == 401

        uid = client.post(
            "/api/admin/users",
            json={"name": "eve"},
            headers={"Authorization": "Bearer secret-gw"},
        ).json()["user"]["id"]
        raw = client.post(
            "/api/admin/keys",
            json={"user_id": uid},
            headers={"Authorization": "Bearer secret-gw"},
        ).json()["key"]["raw_key"]
        r = client.get("/api/admin/users", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 401


def test_no_keys_stays_open(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app) as client:
        assert client.get("/v1/models").status_code == 200
        assert client.get("/api/status").status_code == 200


def test_user_key_on_v1_does_not_open_api_status(users_db: Path):
    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as client:
        uid = client.post(
            "/api/admin/users",
            json={"name": "frank"},
            headers={"Authorization": "Bearer master-key"},
        ).json()["user"]["id"]
        raw = client.post(
            "/api/admin/keys",
            json={"user_id": uid},
            headers={"Authorization": "Bearer master-key"},
        ).json()["key"]["raw_key"]
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 200
        assert client.get("/api/status", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
        assert (
            client.get("/api/status", headers={"Authorization": "Bearer master-key"}).status_code
            == 200
        )
