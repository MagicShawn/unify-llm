from __future__ import annotations

"""Smoke checks that do not require real API keys.

Verifies config load, model routing, OpenAPI surface, and monitor bookkeeping
against a local dummy upstream.
"""

import asyncio
import sys
import threading
import time
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
    from unify_llm.config import AppConfig, DefaultsConfig, ProviderConfig
    from unify_llm.registry import Registry
    from unify_llm.monitor import Monitor

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

    # --- HTTP surface ---
    app = create_app(config=cfg)
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True

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

        r = client.get("/dashboard")
        assert r.status_code == 200 and b"Unify LLM" in r.content

    print("SMOKE OK")


def main() -> None:
    server, port = start_dummy()
    try:
        asyncio.run(run_checks(port))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
