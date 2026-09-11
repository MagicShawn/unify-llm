from __future__ import annotations

"""Smoke checks that do not require real API keys.

Verifies config load, model routing, OpenAPI surface, and monitor bookkeeping
against a local dummy upstream. Also checks optional gateway auth and rate limits.
"""

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DummyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        body = b'{"id":"chatcmpl-dummy","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"pong"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
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


def start_dummy() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), DummyHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


async def run_checks(port: int) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from unify_llm.app import create_app
    from unify_llm.config import AppConfig, AuthConfig, LimitsConfig, PricingConfig, ProviderConfig
    from unify_llm.registry import Registry
    from unify_llm.monitor import Monitor, estimate_cost_usd
    from unify_llm.rate_limit import RateLimiter

    # --- unit-ish ---
    cfg = AppConfig(
        providers={
            "dummy": ProviderConfig(
                type="openai",
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="test",
                models=["dummy-chat"],
            ),
            "off": ProviderConfig(
                type="openai",
                base_url="https://example.com/v1",
                api_key="x",
                enabled=False,
                models=["nope"],
            ),
        },
        aliases={"chat": "dummy-chat"},
    )
    reg = Registry(cfg)
    r = reg.resolve("chat")
    assert r.model == "dummy-chat" and r.provider_id == "dummy"
    try:
        reg.resolve("missing-model")
        raise AssertionError("expected ModelNotFoundError")
    except Exception as e:
        assert "not found" in str(e).lower() or "Model" in str(e)

    mon = Monitor()
    mon.register_provider("dummy", type="openai", base_url="x", enabled=True, models=["dummy-chat"])
    rid = mon.begin(
        provider_id="dummy",
        model="dummy-chat",
        requested_model="chat",
        protocol="openai",
        path="/v1/chat/completions",
    )
    st = mon.status()
    assert st["totals"]["active"] == 1
    mon.end(rid, provider_id="dummy", http_status=200)
    st = mon.status()
    assert st["totals"]["active"] == 0
    assert st["totals"]["requests"] == 1
    assert st["totals"].get("cost_usd", 0.0) == 0.0

    # pricing estimate helper: default rates stay 0
    assert estimate_cost_usd(PricingConfig(), model="dummy-chat", prompt_tokens=10, completion_tokens=10) == 0.0
    priced = PricingConfig(per_million_input=1.0, per_million_output=2.0)
    assert estimate_cost_usd(priced, model="dummy-chat", prompt_tokens=1_000_000, completion_tokens=0) == 1.0

    # --- HTTP surface (no gateway auth) ---
    app = create_app(config=cfg)
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True

        r = client.get("/api/info")
        assert r.status_code == 200
        info = r.json()
        assert info["service"] == "unify_llm"
        assert info["auth_required"] is False
        assert "lan_ready" in info

        r = client.get("/v1/models")
        assert r.status_code == 200
        ids = {m["id"] for m in r.json()["data"]}
        assert "dummy-chat" in ids and "chat" in ids

        r = client.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["choices"][0]["message"]["content"] == "pong"

        # Cross-protocol: Anthropic client → OpenAI upstream
        r = client.post(
            "/v1/messages",
            json={
                "model": "dummy-chat",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert r.status_code == 200, r.text
        adata = r.json()
        assert adata.get("type") == "message", adata
        assert adata["content"][0]["type"] == "text"
        assert adata["content"][0]["text"] == "pong"
        assert adata.get("role") == "assistant"

        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["requests"] >= 1
        assert "cost_usd" in body["totals"]
        # default pricing is zero — no invented vendor rates
        assert body["totals"]["cost_usd"] == 0.0

        r = client.get("/dashboard")
        assert r.status_code == 200 and b"Unify LLM" in r.content
        assert b"kpiCost" in r.content

    # --- gateway auth ---
    secret = "smoke-gateway-key"
    auth_cfg = AppConfig(
        providers=cfg.providers,
        aliases=cfg.aliases,
        auth=AuthConfig(api_key=secret),
    )
    auth_app = create_app(config=auth_cfg)
    with TestClient(auth_app) as client:
        # healthz and dashboard stay open
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True

        r = client.get("/dashboard")
        assert r.status_code == 200

        # 401 without token
        for path in ("/v1/models", "/api/status", "/api/info", "/api/history", "/api/config"):
            r = client.get(path)
            assert r.status_code == 401, (path, r.status_code, r.text)
            assert "error" in r.json()

        # 401 with wrong token
        r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = client.get("/v1/models", headers={"x-api-key": "wrong"})
        assert r.status_code == 401

        # 200 with Bearer token
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200, r.text

        # 200 with x-api-key
        r = client.get("/v1/models", headers={"x-api-key": secret})
        assert r.status_code == 200, r.text

        r = client.get("/api/info", headers={"x-api-key": secret})
        assert r.status_code == 200
        info = r.json()
        assert info["auth_required"] is True
        assert secret not in r.text

        r = client.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "ping"}]},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert r.status_code == 200, r.text

    # --- env UNIFY_GATEWAY_KEY ---
    env_secret = "env-gateway-key"
    os.environ["UNIFY_GATEWAY_KEY"] = env_secret
    try:
        env_app = create_app(config=cfg)
        with TestClient(env_app) as client:
            r = client.get("/healthz")
            assert r.status_code == 200

            r = client.get("/v1/models")
            assert r.status_code == 401

            r = client.get("/v1/models", headers={"x-api-key": env_secret})
            assert r.status_code == 200

            r = client.get("/api/info", headers={"Authorization": f"Bearer {env_secret}"})
            assert r.status_code == 200
            assert r.json()["auth_required"] is True
    finally:
        os.environ.pop("UNIFY_GATEWAY_KEY", None)

    # --- provider admin: list / patch / test / reload (with on-disk config) ---
    import tempfile

    import yaml

    dummy_base = f"http://127.0.0.1:{port}/v1"
    secret_key = "smoke-secret-key-do-not-leak"
    raw_cfg = {
        "server": {"host": "127.0.0.1", "port": 8787},
        "providers": {
            "dummy": {
                "type": "openai",
                "base_url": dummy_base,
                "api_key": secret_key,
                "enabled": True,
                "models": ["dummy-chat"],
            },
            "off": {
                "type": "openai",
                "base_url": "https://example.com/v1",
                "api_key": "x",
                "enabled": False,
                "models": ["nope"],
            },
        },
        "aliases": {"chat": "dummy-chat"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(raw_cfg, sort_keys=False), encoding="utf-8")
        admin_app = create_app(config_path=cfg_path)
        with TestClient(admin_app) as client:
            # GET /api/providers — redacted keys + monitor totals
            r = client.get("/api/providers")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["total"] == 2 and body["enabled"] == 1
            by_id = {p["id"]: p for p in body["providers"]}
            assert by_id["dummy"]["api_key"] == "***"
            assert by_id["off"]["api_key"] == "***"
            assert secret_key not in r.text
            assert "totals" in by_id["dummy"]

            # POST /api/providers/{id}/test — openai GET {base}/models
            r = client.post("/api/providers/dummy/test")
            assert r.status_code == 200, r.text
            t = r.json()
            assert t["ok"] is True and t["status_code"] == 200
            assert t["latency_ms"] >= 0
            assert secret_key not in r.text

            # last_health stored on the provider snapshot
            r = client.get("/api/providers")
            by_id = {p["id"]: p for p in r.json()["providers"]}
            lh = by_id["dummy"]["last_health"]
            assert lh is not None and lh["ok"] is True
            assert lh["status_code"] == 200
            assert "checked_at" in lh
            assert secret_key not in r.text

            r = client.get("/api/status")
            snap = next(p for p in r.json()["providers"] if p["id"] == "dummy")
            assert snap["last_health"]["ok"] is True

            # POST test on missing provider
            r = client.post("/api/providers/nope/test")
            assert r.status_code == 404

            # PATCH disable — YAML write-back + in-memory
            r = client.patch("/api/providers/dummy", json={"enabled": False})
            assert r.status_code == 200, r.text
            p = r.json()
            assert p["ok"] is True and p["enabled"] is False and p["written"] is True

            disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            assert disk["providers"]["dummy"]["enabled"] is False
            assert disk["providers"]["dummy"]["api_key"] == secret_key

            r = client.get("/api/providers")
            assert r.json()["enabled"] == 0

            r = client.get("/v1/models")
            ids = {m["id"] for m in r.json()["data"]}
            assert "dummy-chat" not in ids

            # PATCH re-enable
            r = client.patch("/api/providers/dummy", json={"enabled": True})
            assert r.status_code == 200 and r.json()["written"] is True

            # POST /api/admin/reload — picks up on-disk changes
            disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            disk["providers"]["off"]["enabled"] = True
            cfg_path.write_text(yaml.safe_dump(disk, sort_keys=False), encoding="utf-8")
            r = client.post("/api/admin/reload")
            assert r.status_code == 200, r.text
            rel = r.json()
            assert rel["ok"] is True
            assert rel["providers"] == 2 and rel["enabled"] == 2

            r = client.get("/api/providers")
            assert r.json()["enabled"] == 2

            # reload with no config_path
            bare = create_app(config=cfg)
            with TestClient(bare) as c2:
                r = c2.post("/api/admin/reload")
                assert r.status_code == 400
                r = c2.patch("/api/providers/dummy", json={"enabled": False})
                assert r.status_code == 200
                assert r.json()["written"] is False
                assert r.json()["warning"]

    # --- optional rate limits ---
    rl = RateLimiter(requests_per_minute=2, max_concurrent=1)
    assert rl.enabled is True
    assert rl.check("smoke").allowed is True
    rl.release()

    limits_cfg = AppConfig(
        providers=cfg.providers,
        aliases=cfg.aliases,
        limits=LimitsConfig(requests_per_minute=2, max_concurrent=1),
    )
    limits_app = create_app(config=limits_cfg)
    with TestClient(limits_app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        lim = r.json()["limits"]
        assert lim["enabled"] is True
        assert lim["requests_per_minute"] == 2
        assert lim["max_concurrent"] == 1
        assert lim["active"] == 0

        assert client.get("/v1/models").status_code == 200
        assert client.get("/v1/models").status_code == 200
        r = client.get("/v1/models")
        assert r.status_code == 429, r.text
        assert r.json()["error"]["type"] == "RateLimitError"
        assert "Retry-After" in r.headers
        # /api and /healthz are not rate limited
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 200

    # concurrency cap → 429
    conc_cfg = AppConfig(
        providers=cfg.providers,
        aliases=cfg.aliases,
        limits=LimitsConfig(requests_per_minute=0, max_concurrent=1),
    )
    conc_app = create_app(config=conc_cfg)
    with TestClient(conc_app) as client:
        assert conc_app.state.proxy.limiter.check("held").allowed is True
        r = client.get("/v1/models")
        assert r.status_code == 429, r.text
        assert "concurrent" in r.json()["error"]["message"].lower()
        conc_app.state.proxy.limiter.release()
        assert client.get("/v1/models").status_code == 200

    # disabled limits stay open
    open_app = create_app(config=cfg)
    with TestClient(open_app) as client:
        lim = client.get("/api/status").json()["limits"]
        assert lim["enabled"] is False
        for _ in range(5):
            assert client.get("/v1/models").status_code == 200

    print("SMOKE OK")


def main() -> None:
    server, port = start_dummy()
    try:
        asyncio.run(run_checks(port))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
