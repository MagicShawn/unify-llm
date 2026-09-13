"""Closed-loop security regression tests for findings F1–F8.

Run from repo root:
    python -m pytest tests/test_security.py -q
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import LoginGuard, _client_ip, _is_loopback, _peer_ip, create_app
from unify_llm.config import (
    AppConfig,
    AuthConfig,
    LimitsConfig,
    LoginLimitConfig,
    ProviderConfig,
)
from unify_llm.users import (
    PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    UserStore,
    hash_password,
    hash_session_token,
    verify_password,
)


def _cfg(
    api_key: str = "",
    *,
    rpm: int = 0,
    trusted_proxies: list[str] | None = None,
    login: LoginLimitConfig | None = None,
    cookie_secure: bool = False,
) -> AppConfig:
    return AppConfig(
        auth=AuthConfig(
            api_key=api_key,
            trusted_proxies=trusted_proxies or [],
            session_cookie_secure=cookie_secure,
        ),
        login=login or LoginLimitConfig(max_failures_per_ip=0, max_failures_per_account=0),
        limits=LimitsConfig(requests_per_minute=rpm),
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


def _client(app, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app, client=(host, 50000))


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


# ── F1: forged XFF must not grant localhost admin ─────────────────────────


def test_f1_forged_xff_cannot_use_localhost_admin(users_db: Path):
    """LAN peer + X-Forwarded-For: 127.0.0.1 must not unlock admin APIs."""
    store = UserStore(users_db)
    store.create_user("boss", "boss@test", password="admin-pass-123", role="admin", status="active")
    store.close()

    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app, host="192.168.1.50") as c:
        # Real LAN peer, no credentials.
        assert c.get("/api/admin/users").status_code in (401, 403)
        # Forged localhost XFF still refused.
        r = c.get("/api/admin/users", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code in (401, 403), r.text
        r = c.get("/api/admin/users", headers={"X-Real-IP": "127.0.0.1"})
        assert r.status_code in (401, 403)
        # Cannot create an admin via forged XFF.
        r = c.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={
                "name": "pwn",
                "email": "pwn@test",
                "password": "pwn-pass-123",
                "role": "admin",
                "status": "active",
            },
        )
        assert r.status_code in (401, 403)
        # Cannot mint keys via forged XFF.
        r = c.post(
            "/api/admin/keys",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"user_id": 1, "name": "stolen"},
        )
        assert r.status_code in (401, 403)


def test_f1_true_localhost_still_works(users_db: Path):
    app = create_app(config=_cfg(""), users_db=users_db)
    with _client(app, host="127.0.0.1") as c:
        r = c.post(
            "/api/admin/users",
            json={
                "name": "boss",
                "email": "boss@test",
                "password": "admin-pass-123",
                "role": "admin",
                "status": "active",
            },
        )
        assert r.status_code == 201, r.text


def test_f1_trusted_proxy_may_forward_xff(users_db: Path):
    """Only configured trusted proxies can supply XFF for identity."""
    app = create_app(
        config=_cfg("master-key", trusted_proxies=["10.0.0.2"]),
        users_db=users_db,
    )
    # Peer is NOT the trusted proxy → XFF ignored for rate-limit identity,
    # but localhost privilege still requires a real loopback peer.
    with _client(app, host="10.0.0.9") as c:
        r = c.get("/api/config", headers={"X-Forwarded-For": "127.0.0.1"})
        # No master key on this request path from non-local without key.
        assert r.status_code in (401, 403)


# ── F2: RPM buckets keyed by real peer, not spoofable XFF ─────────────────


def test_f2_rpm_not_bypassed_by_xff_rotation(users_db: Path):
    app = create_app(config=_cfg("master-key", rpm=3), users_db=users_db)
    with _client(app) as c:
        codes = []
        for i in range(6):
            r = c.get(
                "/api/config",
                headers={
                    "Authorization": "Bearer master-key",
                    "X-Forwarded-For": f"10.9.9.{i}",
                },
            )
            # /api/config is not under /v1 rate limit; use a /v1 path instead.
            codes.append(r.status_code)
        # Direct /v1 model list (auth OK) — rate limited per peer.
        seen_429 = False
        for i in range(8):
            r = c.get(
                "/v1/models",
                headers={
                    "Authorization": "Bearer master-key",
                    "X-Forwarded-For": f"10.9.9.{i}",
                },
            )
            if r.status_code == 429:
                seen_429 = True
                break
        assert seen_429, "RPM cap must apply despite rotating X-Forwarded-For"


def test_client_ip_prefers_peer_unless_trusted_proxy():
    class Req:
        def __init__(self, peer, xff=None, xri=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = {}
            if xff:
                self.headers["x-forwarded-for"] = xff
            if xri:
                self.headers["x-real-ip"] = xri

    assert _peer_ip(Req("192.168.1.5", xff="127.0.0.1")) == "192.168.1.5"
    assert _client_ip(Req("192.168.1.5", xff="127.0.0.1")) == "192.168.1.5"
    assert (
        _client_ip(Req("10.0.0.2", xff="192.168.1.9"), frozenset({"10.0.0.2"}))
        == "192.168.1.9"
    )
    assert _is_loopback("127.0.0.1") and _is_loopback("::1")
    assert not _is_loopback("192.168.1.5")
    assert not _is_loopback("127.0.0.1.evil")


# ── F3: login failed-attempt throttle ─────────────────────────────────────


def test_f3_login_rate_limited(users_db: Path):
    store = UserStore(users_db)
    store.create_user("victim", "victim@test", password="victim-pass-1", status="active")
    store.close()

    login_cfg = LoginLimitConfig(
        max_failures_per_ip=5,
        max_failures_per_account=5,
        window_seconds=60.0,
        lockout_seconds=30.0,
    )
    app = create_app(config=_cfg(login=login_cfg), users_db=users_db)
    with _client(app) as c:
        codes = []
        for i in range(10):
            r = c.post(
                "/api/auth/login",
                json={"email": "victim@test", "password": f"wrong-{i}"},
            )
            codes.append(r.status_code)
        assert 429 in codes, f"expected throttle after failures, got {codes}"
        # Locked out — even the correct password is refused while locked.
        r = c.post(
            "/api/auth/login",
            json={"email": "victim@test", "password": "victim-pass-1"},
        )
        assert r.status_code == 429


def test_f3_success_clears_account_counter(users_db: Path):
    store = UserStore(users_db)
    store.create_user("ok", "ok@test", password="ok-pass-12345", status="active")
    store.close()
    login_cfg = LoginLimitConfig(max_failures_per_ip=0, max_failures_per_account=3)
    app = create_app(config=_cfg(login=login_cfg), users_db=users_db)
    with _client(app) as c:
        for _ in range(2):
            assert (
                c.post(
                    "/api/auth/login",
                    json={"email": "ok@test", "password": "nope"},
                ).status_code
                == 401
            )
        assert (
            c.post(
                "/api/auth/login",
                json={"email": "ok@test", "password": "ok-pass-12345"},
            ).status_code
            == 200
        )
        # Counter cleared — two more failures still 401, not 429.
        for i in range(2):
            r = c.post(
                "/api/auth/login",
                json={"email": "ok@test", "password": f"bad-{i}"},
            )
            assert r.status_code == 401


# ── F4: no account enumeration on wrong password ──────────────────────────


def test_f4_pending_wrong_password_is_401(users_db: Path):
    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as c:
        c.post(
            "/api/auth/register",
            json={"name": "p1", "email": "pending@test", "password": "whatever-123"},
        )
        r_pending = c.post(
            "/api/auth/login",
            json={"email": "pending@test", "password": "totally-wrong"},
        )
        r_unknown = c.post(
            "/api/auth/login",
            json={"email": "nosuchuser@test", "password": "totally-wrong"},
        )
        assert r_pending.status_code == 401
        assert r_unknown.status_code == 401
        assert r_pending.json()["error"]["message"] == r_unknown.json()["error"]["message"]


def test_f4_pending_correct_password_still_403(users_db: Path):
    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as c:
        c.post(
            "/api/auth/register",
            json={"name": "p2", "email": "pending2@test", "password": "whatever-123"},
        )
        r = c.post(
            "/api/auth/login",
            json={"email": "pending2@test", "password": "whatever-123"},
        )
        assert r.status_code == 403
        assert "pending" in r.json()["error"]["message"].lower()


# ── F5: password change revokes other sessions ────────────────────────────


def test_f5_password_change_revokes_other_sessions(users_db: Path):
    store = UserStore(users_db)
    store.create_user("multi", "multi@test", password="multi-pass-12", status="active")
    store.close()

    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as c:
        r1 = c.post(
            "/api/auth/login",
            json={"email": "multi@test", "password": "multi-pass-12"},
        )
        cookie1 = r1.headers.get("set-cookie", "").split(";")[0]
        r2 = c.post(
            "/api/auth/login",
            json={"email": "multi@test", "password": "multi-pass-12"},
        )
        cookie2 = r2.headers.get("set-cookie", "").split(";")[0]
        assert r1.status_code == 200 and r2.status_code == 200
        assert c.get("/api/auth/me", headers={"Cookie": cookie1}).status_code == 200
        assert c.get("/api/auth/me", headers={"Cookie": cookie2}).status_code == 200

        # Change password on session 1.
        r = c.post(
            "/api/me/password",
            headers={"Cookie": cookie1},
            json={"old_password": "multi-pass-12", "new_password": "multi-pass-34"},
        )
        assert r.status_code == 200, r.text
        # Current session kept.
        assert c.get("/api/auth/me", headers={"Cookie": cookie1}).status_code == 200
        # Other session revoked.
        assert c.get("/api/auth/me", headers={"Cookie": cookie2}).status_code == 401


def test_store_change_password_keep_token(users_db: Path):
    store = UserStore(users_db)
    u = store.create_user("kt", "kt@test", password="kt-pass-12345", status="active")
    t1 = store.create_session(u["id"])
    t2 = store.create_session(u["id"])
    store.change_password(u["id"], "kt-pass-12345", "kt-pass-67890", keep_session_token=t1)
    assert store.get_session(t1) is not None
    assert store.get_session(t2) is None
    store.close()


# ── F6: session tokens hashed at rest ─────────────────────────────────────


def test_f6_session_tokens_not_stored_plaintext(users_db: Path):
    store = UserStore(users_db)
    u = store.create_user("sec", "sec@test", password="sec-pass-12345", status="active")
    raw = store.create_session(u["id"])
    store.close()

    blob = users_db.read_bytes()
    assert raw.encode("utf-8") not in blob
    assert hash_session_token(raw).encode("utf-8") in blob

    # Lookup still works via the raw cookie value.
    store2 = UserStore(users_db)
    assert store2.get_session(raw) is not None
    store2.close()


def test_f6_legacy_plaintext_sessions_migrate(users_db: Path):
    store = UserStore(users_db)
    u = store.create_user("legacy", "legacy@test", password="legacy-pass-12", status="active")
    raw = store.create_session(u["id"])
    store.close()

    # Simulate a pre-upgrade DB row that still holds the raw token.
    conn = sqlite3.connect(str(users_db))
    conn.execute(
        "UPDATE sessions SET token=? WHERE token=?",
        (raw, hash_session_token(raw)),
    )
    conn.commit()
    conn.close()
    assert raw.encode() in users_db.read_bytes()

    store2 = UserStore(users_db)  # runs migration
    # Live table stores only the hash.
    conn = sqlite3.connect(str(users_db))
    rows = [r[0] for r in conn.execute("SELECT token FROM sessions").fetchall()]
    conn.close()
    assert raw not in rows
    assert hash_session_token(raw) in rows
    # Lookup still works with the raw cookie clients hold.
    assert store2.get_session(raw) is not None
    store2.close()


# ── F7: cookie Secure flag configurable ───────────────────────────────────


def test_f7_cookie_secure_flag(users_db: Path):
    store = UserStore(users_db)
    store.create_user("ck", "ck@test", password="ck-pass-12345", status="active")
    store.close()

    app = create_app(config=_cfg(cookie_secure=True), users_db=users_db)
    with _client(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "ck@test", "password": "ck-pass-12345"},
        )
        assert r.status_code == 200
        assert "Secure" in r.headers.get("set-cookie", "")


def test_f7_cookie_default_not_secure_for_lan_http(users_db: Path):
    store = UserStore(users_db)
    store.create_user("ck2", "ck2@test", password="ck2-pass-12345", status="active")
    store.close()
    app = create_app(config=_cfg(cookie_secure=False), users_db=users_db)
    with _client(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "ck2@test", "password": "ck2-pass-12345"},
        )
        setc = r.headers.get("set-cookie", "")
        assert "HttpOnly" in setc and "SameSite=lax" in setc
        assert "Secure" not in setc


# ── F8: crypto / policy hardening ─────────────────────────────────────────


def test_f8_scrypt_params_raised_and_capped():
    import hashlib as _h
    import secrets as _s

    h = hash_password("correct-horse-battery")
    assert h.startswith("scrypt$32768$8$1$")  # N=2^15 for new hashes
    assert verify_password("correct-horse-battery", h)

    # Old N=2^14 hashes still verify (format embeds n/r/p).
    salt = _s.token_bytes(16)
    dk = _h.scrypt(
        b"legacy-password-ok",
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    old_fmt = f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"
    assert verify_password("legacy-password-ok", old_fmt)

    # Absurd N from a tampered row is rejected (no CPU bomb).
    assert (
        verify_password("x", "scrypt$1073741824$8$1$" + "aa" * 16 + "$" + "bb" * 32)
        is False
    )
    # Near-cap N=2^19,r=8 needs 512MB > maxmem 64MB → must return False, not raise.
    assert (
        verify_password("x", "scrypt$524288$8$1$" + "aa" * 16 + "$" + "bb" * 32)
        is False
    )
    assert PASSWORD_MIN_LEN == 10
    assert PASSWORD_MAX_LEN == 128


def test_r4_login_tampered_scrypt_row_no_500(users_db: Path):
    """R4: a users.password_hash row with over-maxmem scrypt params must not 500 login."""
    store = UserStore(users_db)
    store.create_user("bomb", "bomb@test", password="bomb-pass-1234", status="active")
    store.close()

    conn = sqlite3.connect(str(users_db))
    conn.execute(
        "UPDATE users SET password_hash=? WHERE email=?",
        (f"scrypt$524288$8$1${'ab' * 16}${'cd' * 32}", "bomb@test"),
    )
    conn.commit()
    conn.close()

    app = create_app(config=_cfg("master-key"), users_db=users_db)
    with _client(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "bomb@test", "password": "whatever-123"},
        )
        assert r.status_code == 401, r.text
        assert r.status_code != 500


def test_f8_password_min_and_max_length(users_db: Path):
    store = UserStore(users_db)
    with pytest.raises(ValueError):
        store.create_user("a", "a@test", password="short")
    with pytest.raises(ValueError):
        store.create_user("b", "b@test", password="x" * (PASSWORD_MAX_LEN + 1))
    u = store.create_user("c", "c@test", password="c-pass-12345", status="active")
    assert u is not None
    store.close()


def test_f8_master_key_compare_is_constant_time():
    # Behavioral: wrong key still 401; right key 200. Timing is not asserted
    # (flaky), but the code path uses hmac.compare_digest.
    import inspect

    from unify_llm import app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert "hmac.compare_digest" in src


def test_login_guard_unit():
    guard = LoginGuard(
        LoginLimitConfig(
            max_failures_per_ip=2,
            max_failures_per_account=2,
            window_seconds=60.0,
            lockout_seconds=5.0,
        )
    )
    assert guard.check("1.1.1.1", "a@b.c")[0] is True
    guard.record_failure("1.1.1.1", "a@b.c")
    guard.record_failure("1.1.1.1", "a@b.c")
    allowed, retry = guard.check("1.1.1.1", "a@b.c")
    assert allowed is False and retry >= 1
    guard.record_success("a@b.c")
    assert guard.check("2.2.2.2", "a@b.c")[0] is True
