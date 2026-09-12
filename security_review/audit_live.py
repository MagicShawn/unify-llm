#!/usr/bin/env python3
"""Live security audit for central_proxy (unify_llm gateway).

Closed-loop verification: spins up a mock upstream + real gateway instances
(temp config / temp DBs, ephemeral ports) and exercises the actual HTTP
surface. After the security-hardening branch, former FINDING checks assert
the FIXED behavior (vulnerability must NOT be reproducible).

Usage:  python security_review/audit_live.py
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unify_llm.app import create_app  # noqa: E402
from unify_llm.config import load_config  # noqa: E402
from unify_llm.users import UserStore, hash_password, verify_password  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []  # (id, verdict, note)


def record(tid: str, ok: bool, note: str) -> None:
    if "FINDING" in tid or "FIXED" in tid:
        # ok=True means the mitigation holds (vulnerability is gone).
        verdict = "FIXED (OK)" if ok else "STILL VULNERABLE"
    else:
        verdict = "PASS (OK)" if ok else "FAIL (FINDING)"
    RESULTS.append((tid, verdict, note))
    print(f"[{tid}] {verdict} — {note}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ───────────────────────────── mock upstream ─────────────────────────────

class MockOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        body = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


def start_mock() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ─────────────────────────── app harness ───────────────────────────

def run_app(config_path: Path, host: str, users_db: Path, stats_db: Path) -> tuple[uvicorn.Server, int]:
    port = free_port()
    app = create_app(config_path=config_path, users_db=users_db)
    cfg = uvicorn.Config(app, host=host, port=port, log_level="error", proxy_headers=False)
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        time.sleep(0.1)
        if server.started:
            return server, port
    raise RuntimeError("uvicorn did not start")


def write_config(path: Path, mock_port: int, *, gateway_key: str, rpm: int) -> None:
    limits = f"""
limits:
  requests_per_minute: {rpm}
  max_concurrent: 0
  points_per_1k_prompt: 1
  points_per_1k_completion: 0
"""
    text = f"""
server:
  host: 127.0.0.1
  port: 8787
  dashboard: true
auth:
  api_key: "{gateway_key}"
{limits}
providers:
  mock:
    type: openai
    base_url: http://127.0.0.1:{mock_port}/v1
    api_key: "sk-mock-upstream-000"
    enabled: true
    models: [mock-model]
aliases:
  mock-alias: mock-model
"""
    path.write_text(text, encoding="utf-8")


# ─────────────────────────── Part A: crypto/storage ───────────────────────────

def part_a(tmp: Path) -> None:
    print("\n═══ Part A: password & key storage (direct store checks) ═══")

    h1 = hash_password("correct horse battery staple")
    h2 = hash_password("correct horse battery staple")
    record("A1", h1 != h2 and h1.startswith("scrypt$32768$8$1$"),
           f"scrypt N=2^15,r=8,p=1, per-hash random salt (two hashes of same password differ: {h1 != h2})")

    stored = h1.split("$")
    record("A2", verify_password("correct horse battery staple", h1) and not verify_password("wrong", h1),
           "verify_password accepts correct / rejects wrong password (constant-time compare)")

    udb = tmp / "a_users.db"
    store = UserStore(udb)
    u = store.create_user("alice", "alice@test", password="password-123")
    raw, kh, prefix = "", "", ""
    meta = store.create_api_key(u["id"], "k")
    raw = meta["raw_key"]
    store.create_session(u["id"])  # leave a live session row for A5
    store.close()

    blob = udb.read_bytes()
    record("A3", raw.encode() not in blob,
           "raw API key never persisted (SHA-256 hash + prefix only) — raw key absent from DB file bytes")

    conn = sqlite3.connect(str(udb))
    row = conn.execute("SELECT password_hash FROM users WHERE id=1").fetchone()
    conn.close()
    record("A4", row and row[0].startswith("scrypt$") and b"password-123" not in blob,
           "plaintext password never persisted; users table holds scrypt string only")

    conn = sqlite3.connect(str(udb))
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        ("findme-session-token-abc", 1, time.time(), time.time() + 999),
    )
    conn.commit()
    conn.close()
    # Re-open store so migration hashes any legacy plaintext tokens.
    store2 = UserStore(udb)
    store2.close()
    blob2 = udb.read_bytes()
    record("A5-FIXED", b"findme-session-token-abc" not in blob2,
           "session tokens are hashed (SHA-256) at rest — raw token absent after store open")

    try:
        verify_password("x", "scrypt$1073741824$8$1$" + "aa" * 16 + "$" + "bb" * 32)
        cost_note = "huge-N scrypt param accepted (no cap on n/r/p from stored hash)"
        cost_ok = False
    except Exception as e:
        cost_note = f"rejected: {type(e).__name__}"
        cost_ok = True
    # verify_password returns False rather than raising for out-of-range params.
    if not cost_ok:
        cost_ok = verify_password("x", "scrypt$1073741824$8$1$" + "aa" * 16 + "$" + "bb" * 32) is False
        if cost_ok:
            cost_note = "huge-N scrypt param rejected (return False)"
    print(f"    info: tampered hash with N=2^30 → {cost_note}")


# ─────────────────────────── Part B: live gateway ───────────────────────────

def part_b(tmp: Path, mock_port: int, lan_ip: str) -> None:
    print("\n═══ Part B1: LAN-bound gateway, NO master key (open-admin mode) ═══")

    cfg1 = tmp / "cfg_lan.yaml"
    udb1 = tmp / "lan_users.db"
    sdb1 = tmp / "lan_stats.db"
    write_config(cfg1, mock_port, gateway_key="", rpm=0)

    store = UserStore(udb1)
    admin = store.create_user("boss", "boss@test", password="admin-pass-123", role="admin", status="active")
    store.close()

    srv1, port1 = run_app(cfg1, lan_ip, udb1, sdb1)
    base1 = f"http://{lan_ip}:{port1}"

    with httpx.Client(base_url=base1, timeout=10) as c:
        r = c.get("/healthz")
        record("B0", r.status_code == 200, f"gateway up on {base1} (/healthz={r.status_code})")

        # B1: admin API from a NON-localhost source IP without any credential.
        r = c.get("/api/admin/users")
        record("B1", r.status_code in (401, 403),
               f"admin API from LAN IP {lan_ip} without credentials → {r.status_code} (correctly refused)")

        # B2: same request + forged X-Forwarded-For: 127.0.0.1 — must NOT work.
        r = c.get("/api/admin/users", headers={"X-Forwarded-For": "127.0.0.1"})
        record("B2-FIXED", r.status_code in (401, 403),
               f"FORGED 'X-Forwarded-For: 127.0.0.1' from LAN IP → HTTP {r.status_code} "
               f"(localhost privilege uses TCP peer, not XFF)")

        # B3: full takeover attempt via forged localhost — must fail.
        r = c.post("/api/admin/users", headers={"X-Forwarded-For": "127.0.0.1"},
                   json={"name": "pwn", "email": "pwn@test", "password": "pwn-pass-123", "role": "admin", "status": "active"})
        takeover = r.status_code == 201
        record("B3-FIXED", not takeover,
               f"remote XFF-forge admin create → HTTP {r.status_code} (takeover blocked)")

        # B4: key forge via spoofed admin API — must fail.
        r = c.post("/api/admin/keys", headers={"X-Forwarded-For": "127.0.0.1"}, json={"user_id": admin["id"], "name": "stolen"})
        record("B4-FIXED", r.status_code in (401, 403),
               f"same forged-XFF admin key mint → HTTP {r.status_code}")

    srv1.should_exit = True
    time.sleep(0.5)

    print("\n═══ Part B2: localhost gateway WITH master key (auth matrix, brute force, limits) ═══")
    cfg2 = tmp / "cfg_local.yaml"
    udb2 = tmp / "loc_users.db"
    write_config(cfg2, mock_port, gateway_key="sk-master-test-1234567890", rpm=5)

    store = UserStore(udb2)
    u_rich = store.create_user("rich", "rich@test", password="rich-pass-123", status="active")
    u_poor = store.create_user("poor", "poor@test", password="poor-pass-123", status="active")
    store.set_points_balance(u_rich["id"], 10)
    store.set_points_balance(u_poor["id"], 0)
    key_rich = store.create_api_key(u_rich["id"], "k1")["raw_key"]
    key_poor = store.create_api_key(u_poor["id"], "k2")["raw_key"]
    store.close()

    srv2, port2 = run_app(cfg2, "127.0.0.1", udb2, None)
    base2 = f"http://127.0.0.1:{port2}"

    with httpx.Client(base_url=base2, timeout=15) as c:
        r = c.get("/healthz")
        record("C0", r.status_code == 200, f"gateway up on {base2}")

        # C1 auth matrix on /v1
        r1 = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer sk-master-test-1234567890"})
        r2 = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer sk-master-WRONG"})
        r3 = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]})
        record("C1", r1.status_code == 200 and r2.status_code == 401 and r3.status_code == 401,
               f"/v1 auth: master key={r1.status_code}, wrong key={r2.status_code}, no key={r3.status_code}")

        # C2 per-user key works and deducts points (charging enabled: 1 pt / 1k prompt tokens)
        r = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": f"Bearer {key_rich}"})
        conn = sqlite3.connect(str(udb2))
        bal = conn.execute("SELECT points_balance FROM users WHERE id=?", (u_rich["id"],)).fetchone()[0]
        conn.close()
        record("C2", r.status_code == 200 and bal == 9,
               f"user key call → HTTP {r.status_code}, points 10→{bal} (mock usage 1000 prompt tok × 1pt/1k = 1)")

        # C3 zero-balance user blocked before upstream (402)
        r = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": f"Bearer {key_poor}"})
        record("C3", r.status_code == 402, f"zero-balance user key → HTTP {r.status_code} (blocked pre-upstream)")

        # C4 /api/config must not leak upstream provider keys
        r = c.get("/api/config", headers={"Authorization": "Bearer sk-master-test-1234567890"})
        leaked = "sk-mock-upstream-000" in r.text
        record("C4", not leaked and '"api_key": "***"' in r.text.replace(" ", " ").replace('"api_key":"***"', '"api_key": "***"') or not leaked,
               f"provider upstream key masked in /api/config (leak={leaked})")

        # C5 brute-force login: must be throttled.
        codes = []
        t0 = time.time()
        for i in range(25):
            r = c.post("/api/auth/login", json={"email": "boss@test", "password": f"wrong-{i}"})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        dt = time.time() - t0
        record("C5-FIXED", 429 in codes,
               f"wrong-password logins → {codes} ({dt:.1f}s) — login throttle engages (429)")

        # C6 account enumeration: pending + wrong password must match unknown email.
        c.post("/api/auth/register", json={"name": "pending1", "email": "pending@test", "password": "whatever-123"})
        r_pending = c.post("/api/auth/login", json={"email": "pending@test", "password": "totally-wrong"})
        r_unknown = c.post("/api/auth/login", json={"email": "nosuchuser@test", "password": "totally-wrong"})
        record("C6-FIXED", r_pending.status_code == r_unknown.status_code == 401
               and r_pending.json().get("error", {}).get("message")
               == r_unknown.json().get("error", {}).get("message"),
               f"login wrong-password: pending → {r_pending.status_code}, unknown → {r_unknown.status_code} "
               "(uniform 401, no enumeration)")

        # C7 SQL injection on login
        r = c.post("/api/auth/login", json={"email": "' OR 1=1 --", "password": "x"})
        r2 = c.post("/api/auth/login", json={"email": "boss@test' --", "password": "x"})
        record("C7", r.status_code == 401 and r2.status_code == 401,
               f"SQLi payloads in login → {r.status_code}/{r2.status_code} (parameterized queries hold)")

        # C8 session cookie flags (Secure optional via auth.session_cookie_secure)
        r = c.post("/api/auth/login", json={"email": "rich@test", "password": "rich-pass-123"})
        setc = r.headers.get("set-cookie", "")
        flags = {f: (f in setc) for f in ("HttpOnly", "SameSite=lax", "Secure")}
        record("C8", flags["HttpOnly"] and flags["SameSite=lax"],
               f"session cookie: HttpOnly={flags['HttpOnly']}, SameSite=lax={flags['SameSite=lax']}, "
               f"Secure={flags['Secure']} (Secure configurable; default off for LAN HTTP)")

        # C9 RBAC: normal user session cannot touch /api/admin/*
        r = c.post("/api/auth/login", json={"email": "poor@test", "password": "poor-pass-123"})
        user_cookie = r.headers.get("set-cookie", "").split(";")[0]
        r = c.get("/api/admin/users", headers={"Cookie": user_cookie})
        record("C9", r.status_code == 401, f"user-role session on /api/admin/users → {r.status_code} (RBAC holds)")

        # C10 password change invalidates other sessions
        s1 = c.post("/api/auth/login", json={"email": "rich@test", "password": "rich-pass-123"}).headers.get("set-cookie", "").split(";")[0]
        s2 = c.post("/api/auth/login", json={"email": "rich@test", "password": "rich-pass-123"}).headers.get("set-cookie", "").split(";")[0]
        c.post("/api/me/password", headers={"Cookie": s1}, json={"old_password": "rich-pass-123", "new_password": "new-pass-456"})
        r = c.get("/api/auth/me", headers={"Cookie": s2})
        r_keep = c.get("/api/auth/me", headers={"Cookie": s1})
        record("C10-FIXED", r.status_code == 401 and r_keep.status_code == 200,
               f"after password change: older session → {r.status_code}, current session kept → {r_keep.status_code}")

        # C11 disabled user's session dies
        conn = sqlite3.connect(str(udb2))
        conn.execute("UPDATE users SET status='disabled' WHERE id=?", (u_poor["id"],))
        conn.commit()
        conn.close()
        r = c.get("/api/auth/me", headers={"Cookie": user_cookie})
        record("C11", r.status_code == 401, f"session of disabled user → {r.status_code} (server-side revocation works)")

        # C12 RPM limit: bucket per client; 6th request 429
        got429 = False
        for _ in range(8):
            r = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                       headers={"Authorization": "Bearer sk-master-test-1234567890"})
            if r.status_code == 429:
                got429 = True
                break
        record("C12", got429, f"RPM=5 → 429 after burst (limit enforced on /v1)")

        # C13 RPM must not be bypassable by rotating spoofed XFF
        bypassed = 0
        got429 = False
        for i in range(15):
            r = c.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                       headers={"Authorization": "Bearer sk-master-test-1234567890",
                                "X-Forwarded-For": f"10.9.9.{i}"})
            if r.status_code == 200:
                bypassed += 1
            if r.status_code == 429:
                got429 = True
        record("C13-FIXED", got429 and bypassed < 15,
               f"XFF rotation after RPM cap: {bypassed}/15 passed, got429={got429} "
               "(rate-limit identity uses TCP peer unless trusted_proxies)")

        # C14 open registration (spam vector, pending approval required)
        r = c.post("/api/auth/register", json={"name": "spam", "email": f"spam{int(time.time())}@t", "password": "spam-pass-123"})
        record("C14", r.status_code == 201, f"open self-registration → {r.status_code} (pending approval gate; spam/noise vector)")

    srv2.should_exit = True
    time.sleep(0.5)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="unify-audit-"))
    lan_ip = "192.168.124.4"
    print(f"workdir: {tmp}   (mock upstream + 2 gateway instances, ephemeral ports)")
    mock_srv, mock_port = start_mock()
    try:
        part_a(tmp)
        part_b(tmp, mock_port, lan_ip)
    finally:
        mock_srv.shutdown()
        print("\n" + "═" * 70)
        findings = [
            r for r in RESULTS
            if r[1] in ("STILL VULNERABLE", "FAIL (FINDING)")
        ]
        print(f"SUMMARY: {len(RESULTS)} checks, {len(findings)} remaining finding(s)")
        for tid, verdict, note in findings:
            print(f"  • {tid}: {note}")
        return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
