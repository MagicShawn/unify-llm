"""Short-lived live-port check on 8798: start uvicorn in-process, hit endpoints, shut down."""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8798


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


def main() -> None:
    import httpx
    import uvicorn

    from unify_llm.app import create_app
    from unify_llm.config import AppConfig, ProviderConfig

    dummy = HTTPServer(("127.0.0.1", 0), DummyHandler)
    dummy_port = dummy.server_address[1]
    threading.Thread(target=dummy.serve_forever, daemon=True).start()

    cfg = AppConfig(
        providers={
            "dummy": ProviderConfig(
                type="openai",
                base_url=f"http://127.0.0.1:{dummy_port}/v1",
                api_key="test",
                models=["dummy-chat"],
            ),
        },
        aliases={"chat": "dummy-chat"},
    )
    app = create_app(config=cfg)

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{PORT}"
    # wait for server
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.1)

    if not ready:
        print("FAIL: uvicorn did not become ready on port", PORT)
        sys.exit(1)

    results = []
    with httpx.Client(base_url=base, timeout=10.0) as client:
        checks = [
            ("GET /healthz", lambda: client.get("/healthz")),
            ("GET /api/info", lambda: client.get("/api/info")),
            ("GET /api/status", lambda: client.get("/api/status")),
            ("GET /api/providers", lambda: client.get("/api/providers")),
            ("GET /dashboard", lambda: client.get("/dashboard")),
            (
                "POST /v1/chat/completions",
                lambda: client.post(
                    "/v1/chat/completions",
                    json={"model": "chat", "messages": [{"role": "user", "content": "ping"}]},
                ),
            ),
        ]
        for name, fn in checks:
            try:
                r = fn()
                ok = r.status_code == 200
                extra = ""
                if name.endswith("completions") and ok:
                    ok = r.json()["choices"][0]["message"]["content"] == "pong"
                    extra = " content=pong"
                if name.endswith("dashboard") and ok:
                    ok = b"Unify LLM" in r.content and b"latencyChart" in r.content
                    extra = " html ok"
                print(f"[{'PASS' if ok else 'FAIL'}] {name} — {r.status_code}{extra}")
                results.append(ok)
            except Exception as e:
                print(f"[FAIL] {name} — {e}")
                results.append(False)

    server.should_exit = True
    thread.join(timeout=5)
    dummy.shutdown()

    if not all(results):
        sys.exit(1)
    print("LIVE PORT 8798 OK")


if __name__ == "__main__":
    main()
