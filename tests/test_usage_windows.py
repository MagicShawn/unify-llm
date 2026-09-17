"""Time-window usage aggregation tests (lifetime / 24h / 7d).

Run from repo root:
    python -m pytest tests/test_usage_windows.py -q
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, ProviderConfig
from unify_llm.users import SESSION_COOKIE, UserStore
from unify_llm.usage_windows import (
    WINDOW_SECONDS,
    aggregate_points_log,
    aggregate_windows,
    estimate_points,
    empty_window,
)


NOW = 1_700_000_000.0


def _item(
    *,
    finished_at: float,
    user_id: str = "",
    status: str = "ok",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
) -> dict:
    return {
        "finished_at": finished_at,
        "user_id": user_id,
        "status": status,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": cost,
    }


def _synthetic_items() -> list[dict]:
    return [
        # 1h ago — in 24h and 7d
        _item(
            finished_at=NOW - 3600,
            user_id="1",
            prompt_tokens=100,
            completion_tokens=50,
            cost=0.01,
        ),
        # 2 days ago — 7d only
        _item(
            finished_at=NOW - 2 * 86400,
            user_id="1",
            status="error",
            prompt_tokens=10,
            completion_tokens=0,
            cost=0.0,
        ),
        # 3 days ago — 7d only, other user
        _item(
            finished_at=NOW - 3 * 86400,
            user_id="2",
            prompt_tokens=5,
            completion_tokens=5,
            cost=0.001,
        ),
        # 10 days ago — lifetime only
        _item(
            finished_at=NOW - 10 * 86400,
            user_id="1",
            prompt_tokens=1000,
            completion_tokens=500,
            cost=0.25,
        ),
    ]


# ── unit: aggregation ───────────────────────────────────────────────────────


def test_empty_window_defaults():
    w = empty_window()
    assert w["requests"] == 0
    assert w["total_tokens"] == 0
    assert w["cost_usd"] == 0.0
    assert w["points"] == 0.0


def test_window_seconds_constants():
    assert WINDOW_SECONDS["last_24h"] == 86400
    assert WINDOW_SECONDS["last_7d"] == 7 * 86400


def test_aggregate_windows_buckets_by_age():
    out = aggregate_windows(_synthetic_items(), now=NOW)
    windows = out["windows"]

    assert windows["lifetime"]["requests"] == 4
    assert windows["lifetime"]["errors"] == 1
    assert windows["lifetime"]["prompt_tokens"] == 100 + 10 + 5 + 1000
    assert windows["lifetime"]["completion_tokens"] == 50 + 0 + 5 + 500
    assert windows["lifetime"]["total_tokens"] == (
        150 + 10 + 10 + 1500
    )
    assert windows["lifetime"]["cost_usd"] == pytest.approx(0.01 + 0.0 + 0.001 + 0.25)

    assert windows["last_24h"]["requests"] == 1
    assert windows["last_24h"]["errors"] == 0
    assert windows["last_24h"]["prompt_tokens"] == 100
    assert windows["last_24h"]["completion_tokens"] == 50
    assert windows["last_24h"]["cost_usd"] == pytest.approx(0.01)

    assert windows["last_7d"]["requests"] == 3
    assert windows["last_7d"]["errors"] == 1
    assert windows["last_7d"]["prompt_tokens"] == 100 + 10 + 5
    assert windows["last_7d"]["completion_tokens"] == 50 + 0 + 5
    assert windows["last_7d"]["cost_usd"] == pytest.approx(0.011)


def test_aggregate_windows_filters_user():
    out = aggregate_windows(_synthetic_items(), now=NOW, user_id="1")
    windows = out["windows"]
    assert windows["lifetime"]["requests"] == 3
    assert windows["last_24h"]["requests"] == 1
    assert windows["last_7d"]["requests"] == 2
    assert windows["last_7d"]["errors"] == 1


def test_aggregate_windows_lifetime_override():
    totals = {
        "requests": 999,
        "errors": 7,
        "prompt_tokens": 111,
        "completion_tokens": 222,
        "total_tokens": 333,
        "cost_usd": 1.5,
    }
    out = aggregate_windows(_synthetic_items(), now=NOW, lifetime_totals=totals)
    life = out["windows"]["lifetime"]
    assert life["requests"] == 999
    assert life["errors"] == 7
    assert life["total_tokens"] == 333
    assert life["cost_usd"] == pytest.approx(1.5)
    # windows still from history
    assert out["windows"]["last_24h"]["requests"] == 1


def test_estimate_points_from_rates():
    w = empty_window()
    w["prompt_tokens"] = 2000
    w["completion_tokens"] = 1000
    pts = estimate_points(w, points_per_1k_prompt=2.0, points_per_1k_completion=1.0)
    assert pts == pytest.approx(2.0 * 2 + 1.0 * 1)
    assert estimate_points(w, points_per_1k_prompt=0.0, points_per_1k_completion=0.0) == 0.0


def test_aggregate_windows_rate_estimated_points():
    out = aggregate_windows(
        _synthetic_items(),
        now=NOW,
        points_per_1k_prompt=1.0,
        points_per_1k_completion=2.0,
    )
    assert out["points_source"] == "rate_estimate"
    w24 = out["windows"]["last_24h"]
    # 100/1000 * 1 + 50/1000 * 2 = 0.1 + 0.1
    assert w24["points"] == pytest.approx(0.2)


def test_aggregate_points_log_windows():
    rows = [
        {"delta": -10, "created_at": NOW - 60, "kind": "deduct"},
        {"delta": -4, "created_at": NOW - 2 * 86400, "kind": "deduct"},
        {"delta": -3, "created_at": NOW - 20 * 86400, "kind": "deduct"},
        {"delta": 50, "created_at": NOW - 3 * 86400, "kind": "grant"},
    ]
    by = aggregate_points_log(rows, now=NOW)
    assert by["last_24h"]["points"] == pytest.approx(10.0)
    # Grants are excluded — windows report Points spent.
    assert by["last_7d"]["points"] == pytest.approx(10 + 4)
    assert by["lifetime"]["points"] == pytest.approx(10 + 4 + 3)


def test_aggregate_windows_uses_points_log_when_provided():
    rows = [{"delta": -6, "created_at": NOW - 100, "kind": "deduct"}]
    out = aggregate_windows(
        _synthetic_items(),
        now=NOW,
        user_id="1",
        points_per_1k_prompt=99.0,
        points_per_1k_completion=99.0,
        points_log_rows=rows,
    )
    assert out["points_source"] == "log"
    assert out["windows"]["last_24h"]["points"] == pytest.approx(6.0)
    # rate estimate must not override log points
    assert out["windows"]["lifetime"]["points"] == pytest.approx(6.0)


# ── HTTP fixtures ───────────────────────────────────────────────────────────


def _cfg(api_key: str = "master-key") -> AppConfig:
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


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "usage_windows_users.db"


@pytest.fixture()
def app(users_db: Path):
    return create_app(config=_cfg(), users_db=users_db)


@pytest.fixture()
def client(app):
    with TestClient(app, client=("127.0.0.1", 50001)) as c:
        yield c


@pytest.fixture()
def admin_client(client: TestClient) -> TestClient:
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


def _seed_monitor_history(app, items: list[dict]) -> None:
    """Inject synthetic completed requests into the in-process monitor."""
    from unify_llm.monitor import CompletedRequest

    monitor = app.state.proxy.monitor
    for i, it in enumerate(items):
        status = it.get("status") or "ok"
        pt = int(it.get("prompt_tokens") or 0)
        ct = int(it.get("completion_tokens") or 0)
        cost = float(it.get("estimated_cost_usd") or 0.0)
        rec = CompletedRequest(
            id=f"seed{i}",
            provider_id="p1",
            model="m-x",
            requested_model="m-x",
            protocol="openai",
            path="/v1/chat/completions",
            status=status,
            http_status=200 if status == "ok" else 500,
            latency_ms=12,
            error=None if status == "ok" else "boom",
            finished_at=float(it.get("finished_at") or time.time()),
            client="test",
            prompt_tokens=pt,
            completion_tokens=ct,
            estimated_cost_usd=cost,
            user_id=str(it.get("user_id") or ""),
        )
        monitor._global_recent.append(rec)
        # Keep lifetime counters consistent with history (as begin/end would).
        monitor._global_total += 1
        monitor._prompt_tokens += pt
        monitor._completion_tokens += ct
        monitor._cost_usd += cost
        if status != "ok":
            monitor._global_errors += 1


# ── HTTP: /api/me/usage windows ────────────────────────────────────────────


def test_me_usage_returns_window_fields(admin_client: TestClient, app):
    uid = admin_client.get("/api/auth/me").json()["user"]["id"]
    now = time.time()
    _seed_monitor_history(
        app,
        [
            _item(
                finished_at=now - 60,
                user_id=str(uid),
                prompt_tokens=20,
                completion_tokens=10,
                cost=0.02,
            ),
            _item(
                finished_at=now - 3 * 86400,
                user_id=str(uid),
                status="error",
                prompt_tokens=5,
                completion_tokens=0,
            ),
            _item(
                finished_at=now - 2,
                user_id="999",
                prompt_tokens=999,
                completion_tokens=999,
            ),
        ],
    )
    r = admin_client.get("/api/me/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "windows" in body
    assert "usage_windows" in body
    win = body["windows"]
    for key in ("lifetime", "last_24h", "last_7d"):
        assert key in win, key
        assert set(win[key]) >= {
            "requests",
            "errors",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "points",
        }
    assert win["last_24h"]["requests"] == 1
    assert win["last_24h"]["prompt_tokens"] == 20
    assert win["last_24h"]["completion_tokens"] == 10
    assert win["last_7d"]["requests"] == 2
    assert win["last_7d"]["errors"] == 1
    assert win["lifetime"]["requests"] == 2
    # Other user's traffic must not leak into this user's windows.
    assert win["lifetime"]["prompt_tokens"] == 25
    # Backward-compatible top-level fields still present.
    assert "recent_requests" in body
    assert "points_balance" in body


def test_me_usage_windows_when_no_traffic(admin_client: TestClient):
    r = admin_client.get("/api/me/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["recent_requests"] == 0
    win = body["windows"]
    assert win["lifetime"]["requests"] == 0
    assert win["last_24h"]["requests"] == 0
    assert win["last_7d"]["requests"] == 0


# ── HTTP: gateway windows ───────────────────────────────────────────────────


def test_status_includes_gateway_usage_windows(admin_client: TestClient, app):
    now = time.time()
    _seed_monitor_history(
        app,
        [
            _item(finished_at=now - 30, prompt_tokens=3, completion_tokens=4, cost=0.001),
            _item(finished_at=now - 5 * 86400, prompt_tokens=1, completion_tokens=1),
            _item(finished_at=now - 30 * 86400, prompt_tokens=100, completion_tokens=100),
        ],
    )
    r = admin_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "usage_windows" in body
    uw = body["usage_windows"]
    win = uw["windows"]
    assert win["last_24h"]["requests"] >= 1
    assert win["last_7d"]["requests"] >= 2
    assert win["lifetime"]["requests"] >= 3
    # Lifetime may come from monitor counters (override) which are still valid.
    assert "requests" in win["lifetime"]
    assert uw["history_cap"] is not None


def test_api_stats_windows(admin_client: TestClient, app):
    now = time.time()
    _seed_monitor_history(
        app,
        [
            _item(finished_at=now - 10, prompt_tokens=8, completion_tokens=2),
        ],
    )
    r = admin_client.get("/api/stats/windows")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "windows" in body
    assert "last_24h" in body["windows"]


def test_portal_html_mentions_windows(app):
    with TestClient(app, client=("127.0.0.1", 50002)) as client:
        r = client.get("/portal")
        assert r.status_code == 200
        html = r.text
        assert "usageWindowsBody" in html
        assert "24h" in html
        assert "7d" in html or "7d" in html
