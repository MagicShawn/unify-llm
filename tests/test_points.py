"""Points/credits quota + /api/me/models catalog tests.

Run from repo root:
    python -m pytest tests/test_points.py -q
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import compute_points_cost, create_app, user_model_catalog
from unify_llm.config import AppConfig, AuthConfig, LimitsConfig, ModelLimit, ProviderConfig
from unify_llm.users import POINTS_UNLIMITED, UserStore


class _DummyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        body = (
            b'{"id":"chatcmpl-dummy","object":"chat.completion",'
            b'"choices":[{"index":0,"message":{"role":"assistant","content":"pong"},'
            b'"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1500,"completion_tokens":2500,"total_tokens":4000}}'
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        body = b'{"object":"list","data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def dummy_upstream():
    server = HTTPServer(("127.0.0.1", 0), _DummyHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()


def _cfg(
    *,
    api_key: str = "master-key",
    points_per_1k_prompt: float = 0.0,
    points_per_1k_completion: float = 0.0,
    models: list[str] | None = None,
    aliases: dict[str, str] | None = None,
    model_limits: dict[str, ModelLimit] | None = None,
) -> AppConfig:
    return AppConfig(
        auth=AuthConfig(api_key=api_key),
        limits=LimitsConfig(
            points_per_1k_prompt=points_per_1k_prompt,
            points_per_1k_completion=points_per_1k_completion,
        ),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="up",
                enabled=True,
                models=models or ["m-x"],
            ),
            "off": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="up",
                enabled=False,
                models=["hidden-model"],
            ),
        },
        aliases=aliases or {"fast": "m-x"},
        model_limits=model_limits
        or {
            "m-x": ModelLimit(max_output_tokens=8192, max_context_tokens=32768),
        },
    )


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50031))


def _live_cfg(
    base_url: str,
    *,
    points_per_1k_prompt: float = 0.0,
    points_per_1k_completion: float = 0.0,
) -> AppConfig:
    return AppConfig(
        auth=AuthConfig(api_key="master-key"),
        limits=LimitsConfig(
            points_per_1k_prompt=points_per_1k_prompt,
            points_per_1k_completion=points_per_1k_completion,
        ),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url=base_url,
                api_key="up",
                enabled=True,
                models=["m-x"],
            )
        },
        aliases={"fast": "m-x"},
        model_limits={
            "m-x": ModelLimit(max_output_tokens=8192, max_context_tokens=32768),
        },
    )


# ── formula ────────────────────────────────────────────────────────────────


def test_compute_points_cost_free_when_rates_zero():
    assert compute_points_cost(10_000, 10_000, 0, 0) == 0
    assert compute_points_cost(0, 0, 1, 1) == 0


def test_compute_points_cost_floors():
    # 1500 prompt @ 1/1k → floor(1.5)=1; 2500 completion @ 2/1k → floor(5)=5
    assert compute_points_cost(1500, 2500, 1, 2) == 6
    # exact multiples
    assert compute_points_cost(2000, 1000, 1, 1) == 3
    # fractional floors to 0 then min-1 because tokens > 0 and rates > 0
    assert compute_points_cost(100, 100, 1, 1) == 1
    # prompt rate only
    assert compute_points_cost(3000, 0, 1, 0) == 3
    # completion rate only
    assert compute_points_cost(0, 3000, 0, 1) == 3


def test_compute_points_cost_min_one_when_any_tokens_and_rates():
    assert compute_points_cost(1, 0, 0.001, 0) == 1
    assert compute_points_cost(0, 1, 0, 0.001) == 1
    assert compute_points_cost(0, 0, 1, 1) == 0


# ── store ──────────────────────────────────────────────────────────────────


def test_store_points_default_set_add_deduct(tmp_path: Path):
    store = UserStore(tmp_path / "pts.db")
    try:
        user = store.create_user("pts", email="pts@example.com")
        assert user["points_balance"] == 0
        assert user["points_spent"] == 0

        u2 = store.set_points_balance(user["id"], 100)
        assert u2 is not None and u2["points_balance"] == 100

        u3 = store.add_points(user["id"], 50)
        assert u3 is not None and u3["points_balance"] == 150

        u4 = store.deduct_points(user["id"], 40)
        assert u4 is not None
        assert u4["points_balance"] == 110
        assert u4["points_spent"] == 40

        with pytest.raises(ValueError):
            store.set_points_balance(user["id"], -2)

        u5 = store.set_points_balance(user["id"], POINTS_UNLIMITED)
        assert u5 is not None and u5["points_balance"] == -1

        # Unlimited: deduct tracks spent only.
        u6 = store.deduct_points(user["id"], 25)
        assert u6 is not None
        assert u6["points_balance"] == -1
        assert u6["points_spent"] == 65

        # add_points on unlimited stays unlimited.
        u7 = store.add_points(user["id"], 10)
        assert u7 is not None and u7["points_balance"] == -1
    finally:
        store.close()


# ── models catalog ─────────────────────────────────────────────────────────


def test_user_model_catalog_includes_enabled_and_aliases():
    cfg = _cfg(
        models=["m-x", "m-y"],
        aliases={"fast": "m-x", "pro": "m-y"},
    )
    items = user_model_catalog(cfg)
    by_id = {m["id"]: m for m in items}
    assert "m-x" in by_id and "m-y" in by_id
    assert "fast" in by_id and "pro" in by_id
    assert "hidden-model" not in by_id
    assert by_id["m-x"]["provider"] == "p1"
    assert by_id["m-x"]["type"] == "openai"
    assert "fast" in by_id["m-x"]["aliases"]
    assert by_id["fast"]["alias_of"] == "m-x"
    assert by_id["m-x"]["limits"]["max_output_tokens"] == 8192


def test_me_models_endpoint(admin_client: TestClient):
    r = admin_client.get("/api/me/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    ids = {m["id"] for m in body["models"]}
    assert "m-x" in ids
    assert "fast" in ids
    assert "hidden-model" not in ids
    m = next(x for x in body["models"] if x["id"] == "m-x")
    assert m["provider"] == "p1"
    assert m["type"] == "openai"
    assert "fast" in m["aliases"]
    assert m["limits"]["max_output_tokens"] == 8192


def test_me_models_requires_session(users_db: Path):
    app = create_app(config=_cfg(), users_db=users_db)
    with _client(app) as client:
        assert client.get("/api/me/models").status_code == 401


# ── fixtures for HTTP points ───────────────────────────────────────────────


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


@pytest.fixture()
def admin_client(users_db: Path, dummy_upstream: str):
    app = create_app(
        config=_live_cfg(dummy_upstream, points_per_1k_prompt=0, points_per_1k_completion=1),
        users_db=users_db,
    )
    with _client(app) as client:
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
        client.app.state.proxy  # noqa: B018 — ensure state exists
        yield client


def _make_user_with_key(client: TestClient, *, points: int | None = None) -> tuple[int, str]:
    r = client.post(
        "/api/admin/users",
        json={
            "name": "worker",
            "email": "worker@example.com",
            "password": "worker-pass-1",
            "status": "active",
        },
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["user"]["id"]
    if points is not None:
        r_set = client.patch(
            f"/api/admin/users/{uid}",
            json={"points_balance": points},
            headers={"Authorization": "Bearer master-key"},
        )
        assert r_set.status_code == 200, r_set.text
    r2 = client.post(
        "/api/admin/keys",
        json={"user_id": uid},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r2.status_code == 201, r2.text
    return uid, r2.json()["key"]["raw_key"]


def _chat(client: TestClient, raw_key: str):
    return client.post(
        "/v1/chat/completions",
        json={"model": "m-x", "messages": [{"role": "user", "content": "ping"}]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )


# ── HTTP: block / deduct / unlimited ───────────────────────────────────────


def test_blocked_when_balance_zero_with_rates_on(admin_client: TestClient):
    uid, raw = _make_user_with_key(admin_client, points=0)
    r = _chat(admin_client, raw)
    assert r.status_code == 402, r.text
    err = r.json()["error"]
    assert "points" in err["message"].lower()
    assert err["type"] == "PaymentRequiredError"
    # /v1/models still works (catalog, not an upstream model call)
    assert (
        admin_client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 200
    )
    # No spend recorded
    admin_client.post("/api/auth/logout")
    admin_client.post(
        "/api/auth/login",
        json={"email": "worker@example.com", "password": "worker-pass-1"},
    )
    usage = admin_client.get("/api/me/usage").json()
    assert usage["points_balance"] == 0
    assert usage["points_spent"] == 0


def test_deduct_after_success(admin_client: TestClient):
    # Dummy returns prompt=1500, completion=2500; rate completion=1/1k → floor(2.5)=2
    # prompt rate 0 → total floor(1.5*0)+floor(2.5*1)=2
    uid, raw = _make_user_with_key(admin_client, points=100)
    r = _chat(admin_client, raw)
    assert r.status_code == 200, r.text
    r2 = admin_client.get(
        f"/api/admin/users", headers={"Authorization": "Bearer master-key"}
    )
    user = next(u for u in r2.json()["users"] if u["id"] == uid)
    assert user["points_balance"] == 98
    assert user["points_spent"] == 2


def test_unlimited_never_blocks_and_tracks_spend(admin_client: TestClient):
    uid, raw = _make_user_with_key(admin_client, points=POINTS_UNLIMITED)
    r = _chat(admin_client, raw)
    assert r.status_code == 200, r.text
    r2 = admin_client.get(
        "/api/admin/users", headers={"Authorization": "Bearer master-key"}
    )
    user = next(u for u in r2.json()["users"] if u["id"] == uid)
    assert user["points_balance"] == -1
    assert user["points_spent"] == 2


def test_free_when_rates_zero_even_with_zero_balance(users_db: Path, dummy_upstream: str):
    app = create_app(
        config=_live_cfg(dummy_upstream, points_per_1k_prompt=0, points_per_1k_completion=0),
        users_db=users_db,
    )
    with _client(app) as client:
        client.post(
            "/api/admin/users",
            json={
                "name": "root",
                "email": "root@example.com",
                "password": "admin-pass-1",
                "role": "admin",
            },
            headers={"Authorization": "Bearer master-key"},
        )
        uid, raw = _make_user_with_key(client, points=0)
        r = _chat(client, raw)
        assert r.status_code == 200, r.text
        listed = client.get(
            "/api/admin/users", headers={"Authorization": "Bearer master-key"}
        ).json()
        user = next(u for u in listed["users"] if u["id"] == uid)
        assert user["points_balance"] == 0
        assert user["points_spent"] == 0


def test_master_key_not_charged(admin_client: TestClient):
    r = admin_client.post(
        "/v1/chat/completions",
        json={"model": "m-x", "messages": [{"role": "user", "content": "ping"}]},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code == 200, r.text
    # admin user balance unchanged (master key path has no user attribution)
    listed = admin_client.get(
        "/api/admin/users", headers={"Authorization": "Bearer master-key"}
    ).json()
    root = next(u for u in listed["users"] if u["name"] == "root")
    assert root["points_spent"] == 0


# ── admin + usage ──────────────────────────────────────────────────────────


def test_admin_patch_points_balance_and_add(admin_client: TestClient):
    r = admin_client.post(
        "/api/admin/users",
        json={"name": "p", "email": "p@example.com"},
        headers={"Authorization": "Bearer master-key"},
    )
    uid = r.json()["user"]["id"]
    r2 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"points_balance": 50},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r2.status_code == 200
    assert r2.json()["user"]["points_balance"] == 50

    r3 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"add_points": 25},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r3.json()["user"]["points_balance"] == 75

    r4 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"points_balance": POINTS_UNLIMITED},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r4.json()["user"]["points_balance"] == -1

    r5 = admin_client.patch(
        f"/api/admin/users/{uid}",
        json={"points_balance": -5},
        headers={"Authorization": "Bearer master-key"},
    )
    assert r5.status_code == 400


def test_me_usage_includes_points(admin_client: TestClient):
    admin_client.post("/api/auth/logout")
    admin_client.post(
        "/api/auth/login",
        json={"email": "root@example.com", "password": "admin-pass-1"},
    )
    r = admin_client.get("/api/me/usage")
    assert r.status_code == 200
    body = r.json()
    assert "points_balance" in body
    assert "points_spent" in body
    assert body["points_charging_enabled"] is True
    assert body["points_per_1k_completion"] == 1
