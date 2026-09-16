"""Extended validation battery: HTTP surface, dashboard HTML, static checks.

Run from repo root:
    python scripts/validate_full.py
"""

from __future__ import annotations

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, str, str]] = []


def record(check: str, ok: bool, notes: str = "") -> None:
    RESULTS.append((check, "PASS" if ok else "FAIL", notes))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {check}" + (f" — {notes}" if notes else ""))


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


# Tags that are void / self-closing and need no pair
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Tags that may legally omit closing tags in HTML5 in some contexts;
# we still expect them closed in this self-contained dashboard.
OPTIONAL_CLOSE = {"p", "li", "td", "th", "tr", "option", "thead", "tbody", "tfoot"}


def check_balanced_tags(html: str) -> tuple[bool, str]:
    """Rough tag-balance check ignoring void tags, comments, script, and style."""
    # Strip comments
    stripped = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    # Strip script and style content (but keep their open/close tags)
    stripped = re.sub(r"(<script\b[^>]*>).*?(</script>)", r"\1\2", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"(<style\b[^>]*>).*?(</style>)", r"\1\2", stripped, flags=re.DOTALL | re.IGNORECASE)

    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>", re.DOTALL)
    stack: list[str] = []
    mismatches: list[str] = []

    for m in tag_re.finditer(stripped):
        closing, name, rest = m.group(1), m.group(2).lower(), m.group(3)
        if name in VOID_TAGS:
            continue
        # self-closing form <div />
        if rest.rstrip().endswith("/"):
            continue
        if closing:
            if not stack:
                mismatches.append(f"extra </{name}>")
            elif stack[-1] != name:
                mismatches.append(f"expected </{stack[-1]}> got </{name}>")
                # try to recover
                if name in stack:
                    while stack and stack[-1] != name:
                        stack.pop()
                    if stack:
                        stack.pop()
            else:
                stack.pop()
        else:
            stack.append(name)

    if stack:
        mismatches.append(f"unclosed: {stack}")
    if mismatches:
        return False, "; ".join(mismatches[:8])
    return True, "all tags balanced"


def main() -> None:
    from fastapi.testclient import TestClient

    from unify_llm.app import create_app
    from unify_llm.config import AppConfig, ProviderConfig

    server, port = start_dummy()
    try:
        cfg = AppConfig(
            providers={
                "dummy": ProviderConfig(
                    type="openai",
                    base_url=f"http://127.0.0.1:{port}/v1",
                    api_key="test",
                    models=["dummy-chat"],
                ),
            },
            aliases={"chat": "dummy-chat"},
        )
        app = create_app(config=cfg)

        with TestClient(app) as client:
            # --- required endpoints ---
            r = client.get("/healthz")
            record(
                "GET /healthz",
                r.status_code == 200 and r.json().get("ok") is True,
                f"status={r.status_code} body={r.json() if r.status_code==200 else r.text[:80]}",
            )

            r = client.get("/api/info")
            info_ok = (
                r.status_code == 200
                and r.json().get("service") == "unify_llm"
                and "lan_ready" in r.json()
                and r.json().get("auth_required") is False
            )
            record(
                "GET /api/info",
                info_ok,
                f"status={r.status_code} body={r.json() if r.status_code==200 else r.text[:80]}",
            )

            r = client.get("/api/status")
            status_ok = r.status_code == 200 and "totals" in r.json()
            record(
                "GET /api/status",
                status_ok,
                f"status={r.status_code} totals={r.json().get('totals') if status_ok else r.text[:80]}",
            )

            r = client.get("/api/providers")
            prov_ok = (
                r.status_code == 200
                and r.json().get("ok") is True
                and r.json().get("total") == 1
                and r.json().get("enabled") == 1
            )
            record(
                "GET /api/providers",
                prov_ok,
                f"status={r.status_code} body={r.json() if r.status_code==200 else r.text[:80]}",
            )

            r = client.get("/dashboard", follow_redirects=False)
            dash_ok = r.status_code == 302 and r.headers.get("location", "").startswith("/portal")
            record(
                "GET /dashboard redirects unauthenticated user",
                dash_ok,
                f"status={r.status_code} location={r.headers.get('location')}",
            )

            # --- chat completions against dummy ---
            r = client.post(
                "/v1/chat/completions",
                json={"model": "chat", "messages": [{"role": "user", "content": "ping"}]},
            )
            chat_ok = (
                r.status_code == 200
                and r.json().get("choices", [{}])[0].get("message", {}).get("content") == "pong"
            )
            record(
                "POST /v1/chat/completions",
                chat_ok,
                f"status={r.status_code} content={r.json().get('choices',[{}])[0].get('message',{}).get('content') if r.status_code==200 else r.text[:80]}",
            )

            # --- dashboard HTML content ---
            html = (ROOT / "unify_llm" / "static" / "dashboard.html").read_text(
                encoding="utf-8"
            )

            for needle in ("Unify LLM", "latencyChart", "providerGrid", "toastHost"):
                present = needle in html
                record(f"dashboard.html contains '{needle}'", present, "found" if present else "MISSING")

            # also check brand title specifically
            record(
                "dashboard.html <title> brand",
                "Unify LLM" in html and "<title>" in html,
                "title/brand present",
            )

            # --- static HTML: no external CDN ---
            cdn_patterns = [
                r"https?://(?:cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|fonts\.googleapis\.com|fonts\.gstatic\.com|ajax\.googleapis\.com|code\.jquery\.com|stackpath\.bootstrapcdn\.com|use\.fontawesome\.com|cdn\.bootstrapcdn\.com)",
                r"integrity\s*=\s*[\"']sha",
                r"<link[^>]+href\s*=\s*[\"']https?://",
                r"<script[^>]+src\s*=\s*[\"']https?://",
            ]
            cdn_hits: list[str] = []
            for pat in cdn_patterns:
                for m in re.finditer(pat, html, flags=re.IGNORECASE):
                    cdn_hits.append(m.group(0)[:120])
            record(
                "dashboard.html no external CDN URLs",
                len(cdn_hits) == 0,
                "clean" if not cdn_hits else f"hits={cdn_hits[:5]}",
            )

            # --- static HTML: balanced tags ---
            balanced, bal_notes = check_balanced_tags(html)
            record("dashboard.html balanced tags", balanced, bal_notes)

            # --- extra: /v1/models ---
            r = client.get("/v1/models")
            ids = {m["id"] for m in r.json().get("data", [])} if r.status_code == 200 else set()
            models_ok = r.status_code == 200 and "dummy-chat" in ids and "chat" in ids
            record("GET /v1/models", models_ok, f"ids={sorted(ids)}")

            # --- extra: Anthropic cross-protocol ---
            r = client.post(
                "/v1/messages",
                json={
                    "model": "dummy-chat",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            anth_ok = (
                r.status_code == 200
                and r.json().get("type") == "message"
                and r.json().get("content", [{}])[0].get("text") == "pong"
            )
            record("POST /v1/messages (cross-protocol)", anth_ok, f"status={r.status_code}")

    finally:
        server.shutdown()

    # --- summary ---
    failed = [c for c, s, _ in RESULTS if s == "FAIL"]
    print()
    print("=" * 72)
    print(f"TOTAL: {len(RESULTS)}  PASS: {len(RESULTS)-len(failed)}  FAIL: {len(failed)}")
    if failed:
        print("FAILED CHECKS:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
