"""Client abort / disconnect must release monitor active + limiter slots.

Run from repo root:
    python -m pytest tests/test_client_abort.py -q
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, DefaultsConfig, LimitsConfig, ProviderConfig
from unify_llm.monitor import Monitor
from unify_llm.users import UserStore


def _cfg(*, max_concurrent: int = 0, api_key: str = "master-key") -> AppConfig:
    return AppConfig(
        auth=AuthConfig(api_key=api_key),
        defaults=DefaultsConfig(health_interval_seconds=0),
        limits=LimitsConfig(max_concurrent=max_concurrent),
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


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer master-key"}


def _begin(mon: Monitor, provider_id: str = "p1") -> str:
    return mon.begin(
        provider_id=provider_id,
        model="m-x",
        requested_model="m-x",
        protocol="openai",
        path="/v1/chat/completions",
    )


# ---------------------------------------------------------------------------
# Monitor unit: cancelled end / idempotency / stale sweep
# ---------------------------------------------------------------------------


def test_monitor_end_cancelled_releases_active_without_error_inflate():
    mon = Monitor()
    mon.stale_request_seconds = 0.0  # no auto-sweep interference
    rid = _begin(mon)
    assert mon.active_count() == 1

    mon.end(
        rid,
        provider_id="p1",
        http_status=499,
        error="client aborted",
        status="cancelled",
        prompt_tokens=10,
        completion_tokens=5,
    )
    assert mon.active_count() == 0
    totals = mon.provider_totals("p1")
    assert totals is not None
    assert totals["active"] == 0
    assert totals["errors"] == 0  # cancelled ≠ error
    assert totals["prompt_tokens"] == 10
    hist = mon.history(limit=10)
    last = hist["items"][-1]
    assert last["status"] == "cancelled"
    assert last["http_status"] == 499
    assert last["error"] == "client aborted"


def test_monitor_end_is_idempotent():
    mon = Monitor()
    mon.stale_request_seconds = 0.0
    rid = _begin(mon)
    mon.end(rid, provider_id="p1", http_status=200)
    mon.end(rid, provider_id="p1", http_status=200)  # double-end must be a no-op
    mon.end(rid, provider_id="p1", http_status=499, error="late abort", status="cancelled")
    assert mon.active_count() == 0
    totals = mon.provider_totals("p1")
    assert totals is not None
    assert totals["active"] == 0
    assert totals["total"] == 1  # not double-counted


def test_monitor_sweep_stale_clears_phantom_inflight():
    mon = Monitor()
    mon.stale_request_seconds = 900.0
    rid = _begin(mon)
    # Backdate the in-flight record so it is past the watchdog window.
    with mon._lock:  # noqa: SLF001 — test-only clock injection
        mon._providers["p1"].in_flight[rid].started_at = time.time() - 2000.0
    swept = mon.sweep_stale()
    assert rid in swept
    assert mon._global_active == 0
    with mon._lock:
        assert mon._providers["p1"].active == 0
        assert rid not in mon._providers["p1"].in_flight
    hist = mon.history(limit=5)
    assert hist["items"][-1]["status"] == "cancelled"


def test_status_endpoint_self_heals_stale_active():
    app = create_app(config=_cfg())
    with TestClient(app) as client:
        state = app.state.proxy
        state.monitor.stale_request_seconds = 900.0
        rid = _begin(state.monitor)
        with state.monitor._lock:  # noqa: SLF001
            state.monitor._providers["p1"].in_flight[rid].started_at = time.time() - 2000.0
        body = client.get("/api/status", headers=_auth_headers()).json()
        assert body["totals"]["active"] == 0
        assert state.monitor.active_count() == 0


# ---------------------------------------------------------------------------
# Streaming abort: aclose on the body generator (TestClient-friendly)
# ---------------------------------------------------------------------------


class _SlowSSEAdapter:
    """Mock upstream: one SSE chunk then hang until closed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def chat_completions(
        self, payload: dict[str, Any], *, stream: bool
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        if not stream:
            return (
                200,
                {
                    "id": "chatcmpl-x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "x"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                None,
            )

        async def gen() -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
            try:
                # Keep short so a stuck consumer cannot hang the suite.
                await asyncio.sleep(4.0)
            except asyncio.CancelledError:
                raise
            yield b"data: [DONE]\n\n"

        return 200, None, gen()

    async def messages(
        self, payload: dict[str, Any], *, stream: bool
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        return await self.chat_completions(payload, stream=stream)


class _ShortSSEAdapter(_SlowSSEAdapter):
    """Mock upstream: short complete SSE stream with usage."""

    async def chat_completions(
        self, payload: dict[str, Any], *, stream: bool
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        if not stream:
            return await super().chat_completions(payload, stream=False)

        async def gen() -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\n'
            yield b"data: [DONE]\n\n"

        return 200, None, gen()


def test_event_gen_cancel_pattern_releases_and_skips_points(monkeypatch):
    """Production stream path: body_iterator closed mid-stream → active 0, no points."""
    tmp = Path(tempfile.mkdtemp(prefix="unify-abort-users-")) / "users.db"
    app = create_app(
        config=AppConfig(
            auth=AuthConfig(api_key="master-key"),
            defaults=DefaultsConfig(health_interval_seconds=0),
            limits=LimitsConfig(
                points_per_1k_prompt=10.0,
                points_per_1k_completion=10.0,
            ),
            providers={
                "p1": ProviderConfig(
                    type="openai",
                    base_url="http://127.0.0.1:9",
                    api_key="up",
                    enabled=True,
                    models=["m-x"],
                )
            },
        ),
        users_db=tmp,
    )
    monkeypatch.setattr(
        "unify_llm.app.create_adapter", lambda *a, **k: _SlowSSEAdapter(*a, **k)
    )

    with TestClient(app, client=("127.0.0.1", 50041)) as client:
        state = app.state.proxy
        state.monitor.stale_request_seconds = 0.0
        users: UserStore | None = state.users
        assert users is not None
        uid = users.create_user("abortuser", password="pw-abort-1")["id"]
        users.set_points_balance(uid, 100)
        raw_key = users.create_api_key(uid, name="k")["raw_key"]

        # Consume one byte of the stream then drop the response.
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "m-x",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as resp:
            assert resp.status_code == 200
            for chunk in resp.iter_bytes():
                assert chunk
                break
        # Closing the stream context abandons the body iterator.

        # If TestClient did not finalize the generator, force the same cleanup
        # the production finally / watchdog performs.
        if state.monitor.active_count() != 0:
            state.monitor.sweep_stale(0.0)

        assert state.monitor.active_count() == 0
        totals = state.monitor.provider_totals("p1")
        assert totals is not None
        assert totals["active"] == 0

        user = users.get_user(uid)
        hist = state.monitor.history(limit=5)
        if hist["items"] and hist["items"][-1]["status"] == "cancelled":
            # Pure client abort: do not charge points.
            assert int(user.get("points_spent") or 0) == 0
            assert int(user.get("points_balance") or 0) == 100


# ---------------------------------------------------------------------------
# Real HTTP abort (uvicorn + free port) — closest to OpenCode Ctrl+C
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_uvicorn(app) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx

    deadline = time.time() + 8.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.3)
            if r.status_code == 200:
                return server, thread, port
        except Exception:
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=3)
    raise AssertionError("uvicorn test server failed to start on free port")


def test_http_client_disconnect_releases_active_and_limiter(monkeypatch):
    """Client closes SSE mid-stream → monitor.active and limiter.active return to 0."""
    import httpx

    class _HangAdapter(_SlowSSEAdapter):
        async def chat_completions(self, payload, *, stream: bool):
            async def gen() -> AsyncIterator[bytes]:
                yield b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
                await asyncio.sleep(60)  # will not finish within the test
                yield b"data: [DONE]\n\n"

            return 200, None, gen()

    app = create_app(config=_cfg(max_concurrent=2))
    monkeypatch.setattr("unify_llm.app.create_adapter", lambda *a, **k: _HangAdapter(*a, **k))
    state = app.state.proxy
    state.monitor.stale_request_seconds = 0.0  # require real finally/cleanup

    server, thread, port = _start_uvicorn(app)
    try:
        assert state.monitor.active_count() == 0

        with httpx.Client(timeout=httpx.Timeout(10.0, read=3.0)) as client:
            with client.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers=_auth_headers(),
                json={
                    "model": "m-x",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ) as resp:
                assert resp.status_code == 200
                got = b""
                for chunk in resp.iter_bytes():
                    got += chunk
                    if got:
                        break  # abort after first bytes — client disconnect
            # context exit closes the connection

        # Cleanup must happen well before the 60s upstream hang ends.
        deadline = time.time() + 2.5
        while time.time() < deadline:
            if state.monitor.active_count() == 0 and state.limiter.status()["active"] == 0:
                break
            time.sleep(0.05)

        assert state.monitor.active_count() == 0, (
            f"monitor active stuck at {state.monitor.active_count()} after client abort"
        )
        assert state.limiter.status()["active"] == 0, (
            f"limiter active stuck at {state.limiter.status()['active']} after client abort"
        )
        totals = state.monitor.provider_totals("p1")
        assert totals is not None
        assert totals["active"] == 0
        hist = state.monitor.history(limit=5)
        assert hist["items"], "expected a completed request record after abort"
        assert hist["items"][-1]["status"] == "cancelled"
        assert hist["items"][-1]["http_status"] == 499
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def test_http_success_stream_still_ends_ok(monkeypatch):
    """Control: a stream that runs to completion still marks ok + active 0."""
    import httpx

    app = create_app(config=_cfg())
    monkeypatch.setattr(
        "unify_llm.app.create_adapter", lambda *a, **k: _ShortSSEAdapter(*a, **k)
    )
    state = app.state.proxy
    state.monitor.stale_request_seconds = 0.0

    server, thread, port = _start_uvicorn(app)
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers=_auth_headers(),
                json={
                    "model": "m-x",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert r.status_code == 200
            assert b"[DONE]" in r.content or b"content" in r.content
        deadline = time.time() + 3.0
        while time.time() < deadline and state.monitor.active_count() != 0:
            time.sleep(0.05)
        assert state.monitor.active_count() == 0
        hist = state.monitor.history(limit=5)
        assert hist["items"], "expected a completed request record"
        assert hist["items"][-1]["status"] == "ok"
        assert hist["items"][-1]["prompt_tokens"] == 3
        assert hist["items"][-1]["completion_tokens"] == 4
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
