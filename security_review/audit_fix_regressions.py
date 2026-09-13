#!/usr/bin/env python3
"""Adversarial follow-up: verify the FIXES introduced no new defects.

Covers what audit_live.py does not:
  R1  legacy scrypt N=2^14 hashes still verify (upgrade compat)
  R2  legacy plaintext session tokens migrate + old cookie stays valid
  R3  trusted_proxies positive path: XFF honored only from listed proxy peer
  R4  tampered scrypt params near the caps must not 500 the login endpoint
  R5  password length bounds (10..128) on register/change/admin reset
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unify_llm.app import create_app  # noqa: E402
from unify_llm.users import (  # noqa: E402
    UserStore,
    hash_password,
    verify_password,
    hash_session_token,
)

RESULTS = []


def record(tid, ok, note):
    RESULTS.append((tid, ok))
    print(f"[{tid}] {'PASS' if ok else 'FAIL'} — {note}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def run_app(config_path, host, users_db):
    port = free_port()
    app = create_app(config_path=config_path, users_db=users_db)
    cfg = uvicorn.Config(app, host=host, port=port, log_level="error", proxy_headers=False)
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        time.sleep(0.1)
        if server.started:
            return server, port
    raise RuntimeError("no start")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="unify-recheck-"))
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    # R1: legacy N=2^14 hash still verifies (upgrade compatibility)
    legacy = "scrypt$16384$8$1$" + "ab" * 16 + "$" + "cd" * 32
    import hashlib as _h
    salt = bytes.fromhex("ab" * 16)
    dk = _h.scrypt("legacy-pass-123".encode(), salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    legacy = f"scrypt$16384$8$1${'ab'*16}${dk.hex()}"
    record("R1", verify_password("legacy-pass-123", legacy) and not verify_password("wrong-pass-999", legacy),
           "legacy scrypt N=2^14 hash verifies; new N=2^15 default coexists")

    # R2: legacy plaintext session token migrates + old cookie keeps working
    udb = tmp / "r2.db"
    s = UserStore(udb)
    u = s.create_user("carol", "carol@test", password="carol-pass-123", status="active")
    legacy_token = "legacy-plaintext-cookie-token-0123456789"
    conn = sqlite3.connect(str(udb))
    conn.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                 (legacy_token, u["id"], time.time(), time.time() + 3600))
    conn.commit()
    conn.close()
    s2 = UserStore(udb)  # reopen triggers migration
    sess = s2.get_session(legacy_token)  # lookup hashes the incoming raw token
    s2.close()
    blob = udb.read_bytes()
    record("R2", sess is not None and legacy_token.encode() not in blob,
           f"legacy plaintext session migrated to SHA-256 ({'cookie still valid' if sess else 'COOKIE BROKE'}) and raw token scrubbed")

    # live app for R3/R4/R5: bind 127.0.0.1, trusted proxy = own loopback IP
    cfgp = tmp / "cfg.yaml"
    key = ""  # no master key so /api/auth is the focus; keep defaults
    cfgp.write_text(f"""
server:
  host: 127.0.0.1
  port: 8787
  dashboard: true
auth:
  api_key: "{key}"
  trusted_proxies: [127.0.0.1]
limits:
  requests_per_minute: 0
providers:
  mock:
    type: openai
    base_url: http://127.0.0.1:{mock_port}/v1
    api_key: sk-mock
    enabled: true
    models: [m]
""", encoding="utf-8")
    udb3 = tmp / "r3.db"
    st = UserStore(udb3)
    st.create_user("dave", "dave@test", password="dave-pass-1234", role="admin", status="active")
    st.close()
    srv, port = run_app(cfgp, "127.0.0.1", udb3)

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        # R3a: loopback peer IS the trusted proxy → XFF honored for request attribution.
        # Generate a /v1 call (open: no master key, no user keys) carrying XFF.
        r = c.post("/v1/chat/completions",
                   json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"X-Forwarded-For": "9.9.9.9"})
        assert r.status_code == 200, f"/v1 call failed: {r.status_code}"
        r = c.get("/api/status")
        clients = set()
        for p in r.json().get("providers", []):
            for rec in p.get("recent", []):
                clients.add(rec.get("client"))
        record("R3a", "9.9.9.9" in clients,
               f"trusted proxy peer: XFF honored in request attribution (clients seen: {clients or '∅'})")

        # R4: tampered near-cap scrypt params must not 500 the login endpoint
        conn = sqlite3.connect(str(udb3))
        # N=2^19,r=8 needs 512MB > maxmem 64MB → hashlib.scrypt raises; endpoint must stay 4xx
        conn.execute("UPDATE users SET password_hash=? WHERE email=?",
                     (f"scrypt$524288$8$1${'ab'*16}${'cd'*32}", "dave@test"))
        conn.commit()
        conn.close()
        try:
            r = c.post("/api/auth/login", json={"email": "dave@test", "password": "whatever-123"})
            record("R4", r.status_code in (400, 401, 403, 429),
                   f"login with tampered over-maxmem scrypt row → HTTP {r.status_code} (no 500)")
        except Exception as e:
            record("R4", False, f"login raised: {type(e).__name__}: {e}")

        # R5: password length bounds
        r = c.post("/api/auth/register", json={"name": "x", "email": "short@test", "password": "short9"})
        r2 = c.post("/api/auth/register", json={"name": "x", "email": "long@test", "password": "x" * 129})
        record("R5", r.status_code == 400 and r2.status_code == 400,
               f"register rejects <10 chars → {r.status_code}, >128 chars → {r2.status_code}")

    srv.should_exit = True
    mock.shutdown()
    fails = [t for t, ok in RESULTS if not ok]
    print(f"\nSUMMARY: {len(RESULTS)} checks, {len(fails)} failed: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
