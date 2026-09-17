"""Usage series + last_1h window tests.

Run from repo root:
    python -m pytest tests/test_usage_series.py -q
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, ProviderConfig
from unify_llm.usage_windows import (
    SERIES_BINS,
    WINDOW_SECONDS,
    aggregate_series,
    aggregate_windows,
    series_meta,
)
from unify_llm.users import SESSION_COOKIE

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


# ── unit: window constants + series bins ───────────────────────────────────


def test_window_seconds_include_last_1h():
    assert WINDOW_SECONDS["last_1h"] == 3600
    assert WINDOW_SECONDS["last_24h"] == 86400
    assert WINDOW_SECONDS["last_7d"] == 7 * 86400


def test_series_bins_layout():
    assert SERIES_BINS["last_1h"] == (300.0, 12)
    assert SERIES_BINS["last_24h"] == (3600.0, 24)
    assert SERIES_BINS["last_7d"] == (86400.0, 7)
    meta = series_meta()
    assert meta["last_1h"]["buckets"] == 12
    assert meta["last_1h"]["bucket_seconds"] == 300
    assert meta["last_24h"]["buckets"] == 24
    assert meta["last_7d"]["buckets"] == 7


# ── unit: bucket edges ─────────────────────────────────────────────────────


def test_series_1h_bucket_edges():
    """5-min right-aligned bins for last hour."""
    items = [
        # Just inside lower edge → bucket 0
        _item(finished_at=NOW - 3600 + 0.001, prompt_tokens=1),
        # Lower edge itself (age == 3600) → still in window, bucket 0
        _item(finished_at=NOW - 3600.0, prompt_tokens=2),
        # Just before last bucket (age 301) → bucket 10
        _item(finished_at=NOW - 301, prompt_tokens=4),
        # Start of last bucket (age 300) → bucket 11
        _item(finished_at=NOW - 300, prompt_tokens=8),
        # Age 0 (now) → last bucket
        _item(finished_at=NOW, prompt_tokens=16),
        # Outside 1h → ignored for this series
        _item(finished_at=NOW - 3601, prompt_tokens=32),
    ]
    series = aggregate_series(items, now=NOW)
    s1 = series["last_1h"]
    assert len(s1) == 12
    # Bucket t values: start = NOW-3600, step 300
    assert s1[0]["t"] == pytest.approx(NOW - 3600)
    assert s1[11]["t"] == pytest.approx(NOW - 300)

    # Bucket 0: two items (edge + just-inside), prompt 1+2
    assert s1[0]["requests"] == 2
    assert s1[0]["prompt_tokens"] == 3
    # Bucket 10: age 301
    assert s1[10]["requests"] == 1
    assert s1[10]["prompt_tokens"] == 4
    # Bucket 11: age 300 + age 0
    assert s1[11]["requests"] == 2
    assert s1[11]["prompt_tokens"] == 24
    # Empty middle buckets still present with zeros
    for i in range(1, 10):
        assert s1[i]["requests"] == 0
        assert s1[i]["prompt_tokens"] == 0


def test_series_24h_hourly_bucket_edges():
    items = [
        _item(finished_at=NOW - 86400 + 0.001, prompt_tokens=10),  # bucket 0
        _item(finished_at=NOW - 86400.0, prompt_tokens=20),  # bucket 0 edge
        _item(finished_at=NOW - 3600.0, prompt_tokens=40),  # last hourly bucket start
        _item(finished_at=NOW - 1.0, prompt_tokens=80),  # last bucket
        _item(finished_at=NOW - 86401.0, prompt_tokens=160),  # outside
    ]
    series = aggregate_series(items, now=NOW)
    s24 = series["last_24h"]
    assert len(s24) == 24
    assert s24[0]["t"] == pytest.approx(NOW - 86400)
    assert s24[23]["t"] == pytest.approx(NOW - 3600)
    assert s24[0]["requests"] == 2
    assert s24[0]["prompt_tokens"] == 30
    assert s24[23]["requests"] == 2
    assert s24[23]["prompt_tokens"] == 120
    # Outside item not counted
    total_req = sum(b["requests"] for b in s24)
    assert total_req == 4


def test_series_7d_daily_bucket_edges():
    items = [
        _item(finished_at=NOW - 7 * 86400 + 0.001, prompt_tokens=1),  # day 0
        _item(finished_at=NOW - 86400.0, prompt_tokens=2),  # last day start
        _item(finished_at=NOW - 10.0, prompt_tokens=4),  # last day
        _item(finished_at=NOW - 7 * 86400 - 1.0, prompt_tokens=8),  # outside
    ]
    series = aggregate_series(items, now=NOW)
    s7 = series["last_7d"]
    assert len(s7) == 7
    assert s7[0]["t"] == pytest.approx(NOW - 7 * 86400)
    assert s7[6]["t"] == pytest.approx(NOW - 86400)
    assert s7[0]["requests"] == 1
    assert s7[6]["requests"] == 2
    assert sum(b["requests"] for b in s7) == 3


def test_series_user_filter():
    items = [
        _item(finished_at=NOW - 10, user_id="1", prompt_tokens=5),
        _item(finished_at=NOW - 20, user_id="2", prompt_tokens=999),
    ]
    series = aggregate_series(items, now=NOW, user_id="1")
    total_req = sum(b["requests"] for b in series["last_1h"])
    total_pt = sum(b["prompt_tokens"] for b in series["last_1h"])
    assert total_req == 1
    assert total_pt == 5


def test_aggregate_windows_includes_last_1h_and_series():
    items = [
        _item(finished_at=NOW - 120, user_id="1", prompt_tokens=20, completion_tokens=10),
        _item(finished_at=NOW - 2 * 86400, user_id="1", status="error"),
        _item(finished_at=NOW - 5, user_id="2", prompt_tokens=999),
    ]
    out = aggregate_windows(items, now=NOW, user_id="1")
    win = out["windows"]
    assert "last_1h" in win
    assert win["last_1h"]["requests"] == 1
    assert win["last_1h"]["prompt_tokens"] == 20
    assert win["last_24h"]["requests"] == 1
    assert win["last_7d"]["requests"] == 2
    assert win["lifetime"]["requests"] == 2  # only user 1
    # series present + privacy
    series = out["series"]
    assert set(series) >= {"last_1h", "last_24h", "last_7d"}
    assert len(series["last_1h"]) == 12
    s1_req = sum(b["requests"] for b in series["last_1h"])
    s1_pt = sum(b["prompt_tokens"] for b in series["last_1h"])
    assert s1_req == 1
    assert s1_pt == 20
    # Same request appears in overlapping windows; other user's tokens never do.
    for win_key, buckets in series.items():
        pt = sum(b["prompt_tokens"] for b in buckets)
        assert pt in (0, 20), win_key  # user 1's item only
        assert all(b["prompt_tokens"] != 999 for b in buckets), win_key
        assert all(b["requests"] <= 1 for b in buckets), win_key
    assert out["series_meta"]["last_1h"]["buckets"] == 12


def test_series_points_from_log_bucketed():
    rows = [
        {"delta": -3.0, "created_at": NOW - 60.0},
        {"delta": -2.0, "created_at": NOW - 400.0},
        {"delta": -99.0, "created_at": NOW - 10 * 86400.0},
        {"delta": 50.0, "created_at": NOW - 30.0},  # grant ignored
    ]
    series = aggregate_series([], now=NOW, points_log_rows=rows)
    s1 = series["last_1h"]
    pts = sum(b["points"] for b in s1)
    assert pts == pytest.approx(5.0)
    # bucket for NOW-60 is last bucket; NOW-400 is earlier 5-min bin
    assert s1[11]["points"] == pytest.approx(3.0)
    # 400s ago: age 400 → (400//300)=1 → index 11-1=10
    assert s1[10]["points"] == pytest.approx(2.0)


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
    return tmp_path / "usage_series_users.db"


@pytest.fixture()
def app(users_db: Path):
    return create_app(config=_cfg(), users_db=users_db)


@pytest.fixture()
def client(app):
    with TestClient(app, client=("127.0.0.1", 50011)) as c:
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
        monitor._global_total += 1
        monitor._prompt_tokens += pt
        monitor._completion_tokens += ct
        monitor._cost_usd += cost
        if status != "ok":
            monitor._global_errors += 1


# ── HTTP: /api/me/usage series (per-user) ──────────────────────────────────


def test_me_usage_includes_last_1h_and_series(admin_client: TestClient, app):
    uid = admin_client.get("/api/auth/me").json()["user"]["id"]
    now = time.time()
    _seed_monitor_history(
        app,
        [
            _item(
                finished_at=now - 90,
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
            ),
            # Other user — must not leak into windows or series
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
    win = body["windows"]
    for key in ("lifetime", "last_1h", "last_24h", "last_7d"):
        assert key in win, key
    assert win["last_1h"]["requests"] == 1
    assert win["last_1h"]["prompt_tokens"] == 20
    assert win["last_24h"]["requests"] == 1
    assert win["last_7d"]["requests"] == 2
    # Privacy: other user's traffic excluded
    assert win["lifetime"]["prompt_tokens"] == 25

    series = body["series"]
    assert series, "series required on /api/me/usage"
    assert "last_1h" in series
    assert "last_24h" in series
    assert "last_7d" in series
    assert len(series["last_1h"]) == 12
    assert len(series["last_24h"]) == 24
    assert len(series["last_7d"]) == 7
    # Bucket shape
    b0 = series["last_1h"][0]
    for field in (
        "t",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "points",
    ):
        assert field in b0, field

    # Series totals for this user only
    s1_req = sum(b["requests"] for b in series["last_1h"])
    s1_pt = sum(b["prompt_tokens"] for b in series["last_1h"])
    s1_ct = sum(b["completion_tokens"] for b in series["last_1h"])
    assert s1_req == 1
    assert s1_pt == 20
    assert s1_ct == 10
    # Across all series windows, other user's 999 tokens never appear
    for win_key, buckets in series.items():
        assert sum(b["prompt_tokens"] for b in buckets) <= 25, win_key
        assert all(b["prompt_tokens"] != 999 for b in buckets), win_key

    meta = body["series_meta"]
    assert meta["last_1h"]["bucket_seconds"] == 300
    assert meta["last_1h"]["buckets"] == 12
    # usage_windows mirror also carries series
    assert body["usage_windows"]["series"]["last_1h"]


def test_me_usage_series_when_no_traffic(admin_client: TestClient):
    r = admin_client.get("/api/me/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["windows"]["last_1h"]["requests"] == 0
    series = body["series"]
    assert len(series["last_1h"]) == 12
    assert all(b["requests"] == 0 for b in series["last_1h"])


def test_status_windows_include_last_1h_and_series(admin_client: TestClient, app):
    now = time.time()
    _seed_monitor_history(
        app,
        [
            _item(finished_at=now - 30, prompt_tokens=3, completion_tokens=4),
            _item(finished_at=now - 5 * 86400, prompt_tokens=1, completion_tokens=1),
        ],
    )
    r = admin_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    uw = body["usage_windows"]
    assert "last_1h" in uw["windows"]
    assert uw["windows"]["last_1h"]["requests"] >= 1
    assert uw["series"]["last_1h"]
    r2 = admin_client.get("/api/stats/windows")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["windows"]["last_1h"]["requests"] >= 1
    assert len(body2["series"]["last_24h"]) == 24


def test_portal_html_mentions_1h_and_chart(app):
    with TestClient(app, client=("127.0.0.1", 50012)) as client:
        r = client.get("/portal")
        assert r.status_code == 200
        html = r.text
        assert "usageChart" in html
        assert "last_1h" in html
        assert "winSumReq" in html
        assert 'data-win="last_1h"' in html
        assert "uReq1" not in html
        assert "1h" in html
        # Self-contained: no CDN chart libs
        assert "cdn." not in html.lower()
        assert "chart.js" not in html.lower()
        assert "d3" not in html.lower()
