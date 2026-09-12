from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from . import __version__
from .adapters import create_adapter
from .config import AppConfig, ProviderConfig, load_config, set_provider_enabled
from .errors import ConfigError, ModelNotFoundError, ProxyError, UpstreamError
from .convert import DEFAULT_MAX_TOKENS, apply_max_tokens_policy, stream_error_frame
from .monitor import Monitor, StreamUsageSniffer, extract_usage
from .rate_limit import RateLimiter
from .registry import Registry, ResolvedRoute
from .store import StatsStore
from .users import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    UserStore,
)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_STATS_DB = Path("data") / "unify_stats.db"
DEFAULT_USERS_DB = Path("data") / "unify_users.db"

_LOCALHOST_IPS = frozenset({"127.0.0.1", "::1", "localhost", ""})

PROBE_TIMEOUT_SECONDS = 10.0


async def probe_provider(
    http: httpx.AsyncClient,
    p: ProviderConfig,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Lightweight upstream reachability probe. Never logs or returns keys.

    openai → GET {base}/models; anthropic → GET base with x-api-key.
    Returns {ok, status_code, latency_ms} (+ error class name on failure).
    """
    if p.type == "anthropic":
        url = p.base_url.rstrip("/")
        headers = {
            "x-api-key": p.api_key or "",
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{p.base_url.rstrip('/')}/models"
        headers = {}
        if p.api_key:
            headers["Authorization"] = f"Bearer {p.api_key}"

    started = time.time()
    try:
        resp = await http.get(url, headers=headers, timeout=timeout)
        latency_ms = int((time.time() - started) * 1000)
        return {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": latency_ms,
            "error": e.__class__.__name__,
        }


class AppState:
    def __init__(
        self,
        config: AppConfig,
        config_path: str | Path | None = None,
        stats_db: Path | None = None,
        users_db: Path | None = None,
    ):
        self.config = config
        self.config_path: Path | None = Path(config_path) if config_path is not None else None
        self.registry = Registry(config)
        db_path = Path(stats_db) if stats_db is not None else None
        if db_path is None:
            env_db = os.environ.get("UNIFY_STATS_DB") or ""
            db_path = Path(env_db) if env_db else DEFAULT_STATS_DB
        try:
            self.store = StatsStore(db_path)
        except Exception:  # noqa: BLE001
            self.store = None
        users_path = Path(users_db) if users_db is not None else None
        if users_path is None:
            env_users = os.environ.get("UNIFY_USERS_DB") or ""
            users_path = Path(env_users) if env_users else DEFAULT_USERS_DB
        try:
            self.users = UserStore(users_path)
        except Exception:  # noqa: BLE001
            self.users = None  # type: ignore[assignment]
        self.monitor = Monitor(pricing=config.pricing, store=self.store)
        self.limiter = RateLimiter(
            requests_per_minute=config.limits.requests_per_minute,
            max_concurrent=config.limits.max_concurrent,
            max_queue=config.limits.max_queue,
            queue_timeout_seconds=config.limits.queue_timeout_seconds,
        )
        self._register_all(config)
        self.http: httpx.AsyncClient | None = None
        self._health_tasks: list[asyncio.Task[None]] = []

    def _register_all(self, config: AppConfig) -> None:
        for pid, p in config.providers.items():
            self.monitor.register_provider(
                pid,
                type=p.type,
                base_url=p.base_url,
                enabled=p.enabled,
                models=list(p.models),
            )

    def apply_config(self, config: AppConfig) -> dict[str, int]:
        """Replace live config/registry and refresh monitor provider metadata."""
        self.config = config
        self.registry = Registry(config)
        self.monitor.set_pricing(config.pricing)
        self.limiter.update_config(
            config.limits.requests_per_minute,
            config.limits.max_concurrent,
            config.limits.max_queue,
            config.limits.queue_timeout_seconds,
        )
        self._register_all(config)
        enabled = sum(1 for p in config.providers.values() if p.enabled)
        return {"providers": len(config.providers), "enabled": enabled}

    def provider_summary(self, provider_id: str) -> dict[str, Any] | None:
        p = self.config.providers.get(provider_id)
        if p is None:
            return None
        item: dict[str, Any] = {
            "id": provider_id,
            "type": p.type,
            "base_url": p.base_url,
            "enabled": p.enabled,
            "models": list(p.models),
            "api_key": ("***" if p.api_key else ""),
        }
        totals = self.monitor.provider_totals(provider_id)
        if totals is not None:
            item["totals"] = {
                k: v for k, v in totals.items() if k != "last_health"
            }
            item["last_health"] = totals.get("last_health")
        return item

    def health_interval(self) -> float:
        try:
            return float(self.config.defaults.health_interval_seconds)
        except (TypeError, ValueError):
            return 60.0

    def _cancel_health_tasks(self) -> list[asyncio.Task[None]]:
        tasks = list(self._health_tasks)
        self._health_tasks = []
        for t in tasks:
            t.cancel()
        return tasks

    def _spawn_health_tasks(self) -> None:
        """Start one background probe loop per enabled provider. Safe to re-call."""
        interval = self.health_interval()
        if interval <= 0 or self.http is None:
            return
        for pid, p in self.config.providers.items():
            if not p.enabled:
                continue
            task = asyncio.create_task(
                self._health_loop(pid, interval),
                name=f"unify-health-{pid}",
            )
            self._health_tasks.append(task)

    async def restart_health_tasks(self) -> None:
        """Cancel existing health loops and respawn for the current enabled set.

        Does not await cancelled tasks (avoids deadlock if called from a request
        handler while a probe is in-flight). Shutdown still gathers them.
        """
        self._cancel_health_tasks()
        self._spawn_health_tasks()

    async def _health_loop(self, provider_id: str, interval: float) -> None:
        while True:
            p = self.config.providers.get(provider_id)
            http = self.http
            if p is not None and p.enabled and http is not None:
                result = await probe_provider(http, p)
                self.monitor.set_health(provider_id, result)
            await asyncio.sleep(interval)

    async def startup(self) -> None:
        # read=600: idle gap between SSE chunks (long "thinking" pauses).
        # Do not use the small defaults.timeout_seconds here — that was unused
        # and a 120s idle cut would abort long streams.
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=600.0,
                write=60.0,
                pool=10.0,
            ),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self._spawn_health_tasks()

    async def shutdown(self) -> None:
        tasks = self._cancel_health_tasks()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except (asyncio.TimeoutError, TimeoutError):
                pass
        if self.http is not None:
            await self.http.aclose()
            self.http = None


def _client_ip(request: Request) -> str:
    """Best-effort client IP for LAN / reverse-proxy setups."""
    # Prefer first hop from X-Forwarded-For when behind nginx/caddy on the LAN.
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    xri = request.headers.get("x-real-ip") or ""
    if xri.strip():
        return xri.strip()
    if request.client:
        return request.client.host
    return ""


# Headers safe to log (never full secrets).
_SAFE_HEADER_KEYS = {
    "user-agent",
    "content-type",
    "content-length",
    "accept",
    "accept-language",
    "origin",
    "referer",
    "x-request-id",
    "x-correlation-id",
    "anthropic-version",
    "anthropic-beta",
    "openai-organization",
    "openai-project",
}
_SECRET_HEADERS = {"authorization", "x-api-key", "proxy-authorization", "cookie"}


def _mask_secret(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "***"
    return v[:3] + "…" + "***"


def _sanitize_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _SECRET_HEADERS:
            scheme = ""
            raw = v or ""
            if lk == "authorization" and raw.lower().startswith("bearer "):
                scheme = "Bearer"
                raw = raw[7:].strip()
            out[k] = f"{scheme} { _mask_secret(raw) }".strip() if scheme else _mask_secret(raw)
            continue
        if lk in _SAFE_HEADER_KEYS:
            out[k] = (v or "")[:200]
    return out


def _guess_client_app(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    table = [
        ("powershell", "PowerShell"),
        ("opencode", "OpenCode"),
        ("claude-code", "Claude Code"),
        ("anthropic", "Anthropic SDK"),
        ("openai", "OpenAI SDK"),
        ("curl", "curl"),
        ("python-httpx", "httpx"),
        ("python-requests", "requests"),
        ("insomnia", "Insomnia"),
        ("postman", "Postman"),
        ("vscode", "VS Code"),
        ("cursor", "Cursor"),
        ("go-http-client", "Go"),
        ("node", "Node"),
        ("java", "Java"),
        ("mozilla", "Browser/SDK"),
    ]
    for key, name in table:
        if key in ua:
            return name
    if not ua:
        return "unknown"
    return ua.split("/")[0][:32]


def _error_payload(exc: ProxyError) -> dict[str, Any]:
    return {"error": {"message": exc.message, "type": exc.__class__.__name__}}


def _error_response(exc: ProxyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_error_payload(exc))


def _extract_client_key(request: Request) -> str:
    provided = request.headers.get("x-api-key", "")
    if provided:
        return provided
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def _auth_error(message: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": message
                or "Invalid or missing API key. "
                "Send Authorization: Bearer <key> or x-api-key: <key>.",
                "type": "AuthenticationError",
            }
        },
    )


def _request_user_meta(request: Request) -> tuple[str, str]:
    """Best-effort (user_id, username) attached by auth middleware."""
    user_id = getattr(request.state, "user_id", "") or ""
    username = getattr(request.state, "username", "") or ""
    return str(user_id), str(username)


def _session_auth_error(message: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": message or "Not signed in. Log in at /portal.",
                "type": "AuthenticationError",
            }
        },
    )


def _resolve_session(state: AppState, request: Request) -> dict[str, Any] | None:
    """Look up the session cookie and attach user meta onto request.state."""
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    if not cookie or state.users is None:
        return None
    try:
        session = state.users.get_session(cookie)
    except Exception:  # noqa: BLE001
        return None
    if session is None:
        return None
    request.state.session = session
    request.state.user_id = session["user_id"]
    request.state.username = session["user"]["name"]
    request.state.role = session["user"].get("role") or "user"
    return session


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,  # LAN HTTP gateway
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email") or "",
        "role": user.get("role") or "user",
        "status": user.get("status") or "active",
        "display_name": user.get("display_name") or "",
        "badge": user.get("badge") or "",
    }


def _display_label(user: dict[str, Any] | None) -> str:
    """Human label for a user: display_name when set, else name."""
    if not user:
        return ""
    return (user.get("display_name") or "").strip() or (user.get("name") or "")


def _enrich_history_items(
    items: list[dict[str, Any]], users_store: UserStore | None
) -> list[dict[str, Any]]:
    """Attach display_name / badge to history rows that carry a user_id."""
    if users_store is None or not items:
        return items
    cache: dict[str, dict[str, Any] | None] = {}
    for rec in items:
        uid = str(rec.get("user_id") or "")
        if not uid:
            continue
        if uid not in cache:
            try:
                cache[uid] = users_store.get_user(int(uid))
            except (TypeError, ValueError):
                cache[uid] = None
        u = cache[uid]
        if u is None:
            continue
        rec["display_name"] = u.get("display_name") or ""
        rec["badge"] = u.get("badge") or ""
        rec["display_label"] = _display_label(u)
    return items


def create_app(
    config_path: str | Path | None = None,
    config: AppConfig | None = None,
    *,
    users_db: Path | str | None = None,
) -> FastAPI:
    if config is None:
        config = load_config(config_path or "config.yaml")
    state = AppState(config, config_path=config_path, users_db=Path(users_db) if users_db else None)
    gateway_key = config.gateway_api_key()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(
        title="Unify LLM",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.proxy = state

    if config.server.dashboard:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Auth: master gateway key, per-user API keys, and session cookies.
    # - /api/auth/*: open (login/register/logout/me-by-session).
    # - /api/me/*: requires an active session cookie (any role).
    # - /v1/*: master key OR a valid user key. Open when neither is configured.
    # - /api/admin/*: master key OR admin session OR localhost when no master key.
    # - other /api/*: master key, or admin session, when one is set.
    @app.middleware("http")
    async def gateway_auth(request: Request, call_next):
        path = request.url.path
        protected = path.startswith(("/v1/", "/api/"))
        if not protected:
            return await call_next(request)

        # Portal auth endpoints must stay reachable without a gateway key.
        if path.startswith("/api/auth/"):
            _resolve_session(state, request)
            return await call_next(request)

        # Self-service endpoints: any active session.
        if path.startswith("/api/me/") or path == "/api/me":
            if _resolve_session(state, request) is None:
                return _session_auth_error()
            return await call_next(request)

        client_key = _extract_client_key(request)
        is_v1 = path.startswith("/v1/")
        is_user_admin = path.startswith(("/api/admin/users", "/api/admin/keys"))

        # Master gateway key always grants full access.
        if gateway_key and client_key == gateway_key:
            return await call_next(request)

        # Admin session cookie grants dashboard + admin API access.
        # Not applied to /v1/* — model calls still need a master or user API key.
        session = _resolve_session(state, request)
        if (
            session is not None
            and not is_v1
            and session["user"].get("role") == "admin"
        ):
            return await call_next(request)

        # User/key admin without a master key → localhost only (v1 policy).
        if is_user_admin and not gateway_key:
            if _client_ip(request) not in _LOCALHOST_IPS:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "message": "Admin user API is localhost-only when no gateway key is set.",
                            "type": "ForbiddenError",
                        }
                    },
                )
            return await call_next(request)

        users = state.users
        has_user_keys = bool(users is not None and users.has_any_active_key())

        # User API keys authenticate /v1/* only.
        if is_v1 and client_key:
            if users is not None:
                auth = users.authenticate_key(client_key)
                if auth is not None:
                    request.state.user_id = auth["user_id"]
                    request.state.username = auth["username"]
                    request.state.api_key_id = auth["key_id"]
                    try:
                        users.touch_last_used(auth["key_id"])
                    except Exception:  # noqa: BLE001
                        pass
                    return await call_next(request)
            # Provided but unknown/revoked/disabled key.
            return _auth_error()

        # No credentials.
        if not client_key:
            if is_v1:
                # Open when no master key and no user keys (backward compatible).
                if not gateway_key and not has_user_keys:
                    return await call_next(request)
                return _auth_error()
            # Other /api/*: localhost always works for first-run / local admin.
            # Once portal accounts exist, non-localhost needs admin session or master key.
            if not gateway_key:
                if _client_ip(request) in _LOCALHOST_IPS:
                    return await call_next(request)
                if users is not None and users.count_users() > 0:
                    return _auth_error()
                return await call_next(request)
            return _auth_error()

        # Wrong key on non-/v1 protected path.
        return _auth_error()

    # Rate limit /v1/* only (token bucket per client IP + optional concurrency cap).
    # Always registered so admin reload can turn limits on without restart.
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/v1/"):
            return await call_next(request)
        if not state.limiter.enabled:
            return await call_next(request)

        client = _client_ip(request)

        async def _reject(reason: str, retry_after: int, message: str) -> Response:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": message,
                        "type": "RateLimitError",
                        "reason": reason,
                        "retry_after": retry_after,
                        "active": state.limiter.status().get("active"),
                        "queued": state.limiter.status().get("queued"),
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        # RPM first (cheap reject).
        rpm_decision = state.limiter.check_rpm_only(client)
        if not rpm_decision.allowed and rpm_decision.reason == "requests_per_minute":
            return await _reject(
                "requests_per_minute",
                rpm_decision.retry_after,
                f"Rate limit exceeded: {state.limiter.requests_per_minute} requests per minute. "
                f"Retry after {rpm_decision.retry_after}s.",
            )

        # Concurrency + optional queue.
        if not state.limiter.try_reserve_concurrency():
            if state.limiter.max_queue <= 0:
                return await _reject(
                    "max_concurrent",
                    1,
                    f"Too many concurrent requests (max {state.limiter.max_concurrent}). "
                    "Queue disabled; retry shortly.",
                )
            if not state.limiter.enter_queue():
                return await _reject(
                    "queue_full",
                    1,
                    f"Queue full (max {state.limiter.max_queue} waiting, "
                    f"active {state.limiter.status().get('active')}). Retry shortly.",
                )
            timeout = max(1.0, float(state.limiter.queue_timeout_seconds or 30.0))
            deadline = time.monotonic() + timeout
            reserved = False
            try:
                while time.monotonic() < deadline:
                    if state.limiter.try_reserve_concurrency():
                        reserved = True
                        break
                    await asyncio.sleep(0.05)
            finally:
                state.limiter.leave_queue()
            if not reserved:
                return await _reject(
                    "queue_timeout",
                    1,
                    f"Timed out waiting in queue ({int(timeout)}s) at concurrency "
                    f"{state.limiter.max_concurrent}.",
                )

        released = False

        def _release_once() -> None:
            nonlocal released
            if not released:
                released = True
                state.limiter.release()

        try:
            response = await call_next(request)
        except Exception:
            _release_once()
            raise

        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            # Fully buffered response: slot is free once call_next returns.
            _release_once()
            return response

        async def _wrapped(iterator=body_iterator):
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                _release_once()

        response.body_iterator = _wrapped()
        return response

    # ---------- health / status / dashboard ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "unify_llm", "version": __version__}

    @app.get("/api/info")
    async def api_info() -> dict[str, Any]:
        return {
            "service": "unify_llm",
            "version": __version__,
            "lan_ready": state.config.server.host,
            "auth_required": bool(state.config.gateway_api_key()),
        }

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        body = state.monitor.status()
        body["limits"] = state.limiter.status()
        # Enrich traffic rows with display_name / badge for the Account column.
        for p in body.get("providers") or []:
            if isinstance(p, dict) and isinstance(p.get("recent"), list):
                _enrich_history_items(p["recent"], state.users)
        pricing = state.config.pricing
        body["pricing"] = {
            "configured": pricing.has_any_rate(),
            "per_million_input": pricing.per_million_input,
            "per_million_output": pricing.per_million_output,
            "models": list(pricing.models.keys()),
        }
        return body

    @app.get("/api/history")
    async def api_history(limit: int = 50) -> dict[str, Any]:
        body = state.monitor.history(limit=min(max(limit, 1), 200))
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            _enrich_history_items(body["items"], state.users)
        return body

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        providers = {}
        for pid, p in state.config.providers.items():
            providers[pid] = {
                "type": p.type,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "models": p.models,
                "api_key": ("***" if p.api_key else ""),
            }
        return {
            "server": state.config.server.model_dump(),
            "defaults": state.config.defaults.model_dump(),
            "limits": state.config.limits.model_dump(),
            "providers": providers,
            "aliases": state.config.aliases,
            "pricing": state.config.pricing.model_dump(),
            "auth_required": bool(state.config.gateway_api_key()),
        }

    # ---------- provider admin (LAN ops) ----------

    @app.post("/api/admin/reload")
    async def admin_reload() -> Response:
        """Reload config from the startup path and rebuild routing."""
        path = state.config_path
        if path is None:
            return JSONResponse(
                {
                    "error": {
                        "message": "config_path not set; cannot reload",
                        "type": "ConfigError",
                    }
                },
                status_code=400,
            )
        try:
            new_config = load_config(path)
        except ConfigError as e:
            return JSONResponse(
                {"error": {"message": e.message, "type": e.__class__.__name__}},
                status_code=400,
            )
        counts = state.apply_config(new_config)
        await state.restart_health_tasks()
        state.monitor.log(
            "info",
            f"config reloaded ({counts.get('providers', 0)} providers, "
            f"{counts.get('enabled', 0)} enabled)",
        )
        return JSONResponse({"ok": True, "config_path": str(path), **counts})

    @app.post("/api/admin/clear-logs")
    async def clear_logs() -> dict[str, Any]:
        state.monitor.clear_logs()
        state.monitor.log("info", "logs cleared")
        return {"ok": True, "cleared": "logs"}

    @app.post("/api/admin/clear-stats")
    async def clear_stats() -> dict[str, Any]:
        state.monitor.clear_stats()
        state.monitor.log("info", "stats cleared (tokens/cost/history)")
        return {"ok": True, "cleared": "stats"}

    # ---------- multi-user API keys (LAN ops) ----------

    def _users_ready() -> UserStore | None:
        return state.users

    @app.get("/api/admin/users")
    async def admin_list_users() -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        items = []
        for u in users_store.list_users():
            entry = dict(u)
            entry["keys"] = users_store.list_keys_for_user(u["id"])
            items.append(entry)
        return JSONResponse({"ok": True, "users": items, "total": len(items)})

    @app.post("/api/admin/users")
    async def admin_create_user(request: Request) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        name = body.get("name")
        if not name or not str(name).strip():
            return JSONResponse(
                {"error": {"message": "Field 'name' is required"}},
                status_code=400,
            )
        role = str(body.get("role") or "user")
        status = str(body.get("status") or "active")
        password = body.get("password")
        try:
            user = users_store.create_user(
                name=str(name),
                email=body.get("email"),
                note=str(body.get("note") or ""),
                password=str(password) if password else None,
                role=role,
                status=status,
            )
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        state.monitor.log(
            "info",
            f"user created id={user['id']} name={user['name']} role={user['role']} status={user['status']}",
        )
        return JSONResponse({"ok": True, "user": user}, status_code=201)

    @app.patch("/api/admin/users/{user_id}")
    async def admin_patch_user(user_id: int, request: Request) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        allowed = {"enabled", "role", "status", "approve", "password", "display_name", "badge"}
        if not any(k in body for k in allowed):
            return JSONResponse(
                {"error": {"message": f"Supported fields: {sorted(allowed)}"}},
                status_code=400,
            )
        user: dict[str, Any] | None = None
        try:
            if body.get("approve"):
                user = users_store.approve_user(user_id)
            elif "status" in body:
                user = users_store.set_status(user_id, str(body["status"]))
            elif "enabled" in body:
                user = users_store.set_user_enabled(user_id, bool(body["enabled"]))
            if "role" in body:
                user = users_store.set_role(user_id, str(body["role"]))
            if "password" in body and body["password"]:
                user = users_store.set_password(user_id, str(body["password"]))
            if "display_name" in body or "badge" in body:
                user = users_store.set_profile(
                    user_id,
                    display_name=body.get("display_name"),
                    badge=body.get("badge"),
                )
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        if user is None:
            return JSONResponse(
                {"error": {"message": f"Unknown user id: {user_id}"}},
                status_code=404,
            )
        # Invalidate sessions when the account is no longer active.
        if user.get("status") != "active":
            try:
                users_store.delete_sessions_for_user(user_id)
            except Exception:  # noqa: BLE001
                pass
        state.monitor.log(
            "info",
            f"user {user_id} role={user.get('role')} status={user.get('status')}",
        )
        return JSONResponse({"ok": True, "user": user})

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: int) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        if not users_store.delete_user(user_id):
            return JSONResponse(
                {"error": {"message": f"Unknown user id: {user_id}"}},
                status_code=404,
            )
        state.monitor.log("info", f"user deleted id={user_id}")
        return JSONResponse({"ok": True, "deleted": user_id})

    @app.post("/api/admin/users/{user_id}/password")
    async def admin_reset_password(user_id: int, request: Request) -> Response:
        """Admin password reset for another user. Does not log the password."""
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        password = body.get("password")
        if not password:
            return JSONResponse(
                {"error": {"message": "Field 'password' is required"}},
                status_code=400,
            )
        try:
            user = users_store.set_password(user_id, str(password))
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        if user is None:
            return JSONResponse(
                {"error": {"message": f"Unknown user id: {user_id}"}},
                status_code=404,
            )
        # Force re-login after an admin reset (other devices should re-auth).
        try:
            users_store.delete_sessions_for_user(user_id)
        except Exception:  # noqa: BLE001
            pass
        state.monitor.log("info", f"password reset by admin for user id={user_id}")
        return JSONResponse({"ok": True, "user": _public_user(user)})

    @app.get("/api/admin/keys")
    async def admin_list_keys() -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        keys = users_store.list_keys()
        return JSONResponse({"ok": True, "keys": keys, "total": len(keys)})

    @app.post("/api/admin/keys")
    async def admin_create_key(request: Request) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict) or body.get("user_id") is None:
            return JSONResponse(
                {"error": {"message": "Field 'user_id' is required"}},
                status_code=400,
            )
        try:
            user_id = int(body["user_id"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": {"message": "Field 'user_id' must be an integer"}},
                status_code=400,
            )
        try:
            meta = users_store.create_api_key(user_id, name=str(body.get("name") or ""))
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        # raw_key is present only in this response — never logged, never re-listed.
        state.monitor.log(
            "info",
            f"api key issued user_id={user_id} prefix={meta.get('key_prefix', '')}",
        )
        return JSONResponse({"ok": True, "key": meta}, status_code=201)

    @app.post("/api/admin/keys/{key_id}/revoke")
    async def admin_revoke_key(key_id: int) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        meta = users_store.revoke_key(key_id)
        if meta is None:
            return JSONResponse(
                {"error": {"message": f"Unknown key id: {key_id}"}},
                status_code=404,
            )
        state.monitor.log("info", f"api key revoked id={key_id}")
        return JSONResponse({"ok": True, "key": meta})

    # ---------- session auth + user portal ----------

    @app.post("/api/auth/register")
    async def auth_register(request: Request) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        name = str(body.get("name") or "").strip()
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not name or not email or not password:
            return JSONResponse(
                {"error": {"message": "Fields 'name', 'email', and 'password' are required"}},
                status_code=400,
            )
        try:
            user = users_store.register_user(name, email, password)
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        state.monitor.log("info", f"user registered id={user['id']} email={user['email']}")
        return JSONResponse(
            {
                "ok": True,
                "user": _public_user(user),
                "message": "Account created. Wait for an admin to approve before signing in.",
            },
            status_code=201,
        )

    @app.post("/api/auth/login")
    async def auth_login(request: Request) -> Response:
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        login = str(body.get("email") or body.get("login") or "").strip()
        password = str(body.get("password") or "")
        if not login or not password:
            return JSONResponse(
                {"error": {"message": "Fields 'email' and 'password' are required"}},
                status_code=400,
            )
        user = users_store.authenticate_password(login, password)
        if user is None:
            # Distinguish pending vs bad credentials without leaking password state.
            by_email = users_store.get_user_by_email(login)
            if by_email is not None and by_email.get("status") == "pending":
                return JSONResponse(
                    {
                        "error": {
                            "message": "Account pending approval. An admin must activate it first.",
                            "type": "ForbiddenError",
                        }
                    },
                    status_code=403,
                )
            return JSONResponse(
                {
                    "error": {
                        "message": "Invalid email or password.",
                        "type": "AuthenticationError",
                    }
                },
                status_code=401,
            )
        token = users_store.create_session(user["id"])
        resp = JSONResponse({"ok": True, "user": _public_user(user)})
        _set_session_cookie(resp, token)
        state.monitor.log("info", f"user login id={user['id']} name={user['name']}")
        return resp

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> Response:
        users_store = _users_ready()
        cookie = request.cookies.get(SESSION_COOKIE) or ""
        if users_store is not None and cookie:
            try:
                users_store.delete_session(cookie)
            except Exception:  # noqa: BLE001
                pass
        resp = JSONResponse({"ok": True})
        _clear_session_cookie(resp)
        return resp

    @app.get("/api/auth/me")
    async def auth_me(request: Request) -> Response:
        session = _resolve_session(state, request)
        if session is None:
            return _session_auth_error()
        return JSONResponse({"ok": True, "user": _public_user(session["user"])})

    def _require_me(request: Request) -> dict[str, Any] | None:
        return getattr(request.state, "session", None) or None

    @app.get("/api/me/keys")
    async def me_list_keys(request: Request) -> Response:
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        keys = users_store.list_keys_for_user(session["user_id"])
        return JSONResponse({"ok": True, "keys": keys, "total": len(keys)})

    @app.post("/api/me/keys")
    async def me_create_key(request: Request) -> Response:
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = ""
        if isinstance(body, dict):
            name = str(body.get("name") or "")
        try:
            meta = users_store.create_api_key(session["user_id"], name=name)
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        state.monitor.log(
            "info",
            f"api key issued user_id={session['user_id']} prefix={meta.get('key_prefix', '')}",
        )
        return JSONResponse({"ok": True, "key": meta}, status_code=201)

    @app.post("/api/me/keys/{key_id}/revoke")
    async def me_revoke_key(key_id: int, request: Request) -> Response:
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        keys = users_store.list_keys_for_user(session["user_id"])
        if not any(int(k["id"]) == int(key_id) for k in keys):
            return JSONResponse(
                {"error": {"message": f"Unknown key id: {key_id}"}},
                status_code=404,
            )
        meta = users_store.revoke_key(key_id)
        if meta is None:
            return JSONResponse(
                {"error": {"message": f"Unknown key id: {key_id}"}},
                status_code=404,
            )
        state.monitor.log("info", f"api key revoked id={key_id} by user={session['user_id']}")
        return JSONResponse({"ok": True, "key": meta})

    @app.post("/api/me/password")
    async def me_change_password(request: Request) -> Response:
        """Self-service password change. Keeps the current session active."""
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        old_password = str(body.get("old_password") or "")
        new_password = str(body.get("new_password") or "")
        if not old_password or not new_password:
            return JSONResponse(
                {"error": {"message": "Fields 'old_password' and 'new_password' are required"}},
                status_code=400,
            )
        try:
            user = users_store.change_password(
                session["user_id"], old_password, new_password
            )
        except ValueError as e:
            msg = str(e)
            status = 400 if "unknown user" not in msg else 404
            # Map wrong old password to 401-style client error without leaking state.
            if "old_password" in msg:
                return JSONResponse(
                    {"error": {"message": msg, "type": "AuthenticationError"}},
                    status_code=400,
                )
            return JSONResponse({"error": {"message": msg}}, status_code=status)
        # Session intentionally kept (documented): user stays signed in after change.
        state.monitor.log("info", f"password changed by user id={session['user_id']}")
        return JSONResponse({"ok": True, "user": _public_user(user), "session_kept": True})

    @app.patch("/api/me")
    async def me_patch_profile(request: Request) -> Response:
        """Self-service profile: display_name only. Badge is admin-set."""
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        users_store = _users_ready()
        if users_store is None:
            return JSONResponse(
                {"error": {"message": "User store unavailable", "type": "ConfigError"}},
                status_code=503,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "JSON object required"}}, status_code=400)
        if "badge" in body:
            return JSONResponse(
                {"error": {"message": "badge is admin-set; ask an admin to change it"}},
                status_code=403,
            )
        if "display_name" not in body:
            return JSONResponse(
                {"error": {"message": "Supported field: display_name"}},
                status_code=400,
            )
        try:
            user = users_store.set_profile(
                session["user_id"], display_name=str(body.get("display_name") or "")
            )
        except ValueError as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        if user is None:
            return JSONResponse(
                {"error": {"message": f"Unknown user id: {session['user_id']}"}},
                status_code=404,
            )
        return JSONResponse({"ok": True, "user": _public_user(user)})

    @app.get("/api/me/usage")
    async def me_usage(request: Request) -> Response:
        """Cheap personal usage: filter recent monitor history by user_id."""
        session = _require_me(request)
        if session is None:
            return _session_auth_error()
        uid = str(session["user_id"])
        hist = state.monitor.history(limit=200)
        items = [r for r in hist.get("items") or [] if str(r.get("user_id") or "") == uid]
        prompt = sum(int(r.get("prompt_tokens") or 0) for r in items)
        completion = sum(int(r.get("completion_tokens") or 0) for r in items)
        errors = sum(1 for r in items if r.get("status") != "ok")
        cost = sum(float(r.get("estimated_cost_usd") or 0.0) for r in items)
        return JSONResponse(
            {
                "ok": True,
                "user_id": session["user_id"],
                "recent_requests": len(items),
                "errors": errors,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "estimated_cost_usd": round(cost, 12),
                "items": items[:20],
            }
        )

    @app.get("/api/providers")
    async def list_providers() -> dict[str, Any]:
        items = []
        enabled = 0
        for pid in state.config.providers:
            summary = state.provider_summary(pid)
            if summary is None:
                continue
            items.append(summary)
            if summary["enabled"]:
                enabled += 1
        return {
            "ok": True,
            "providers": items,
            "enabled": enabled,
            "total": len(items),
        }

    @app.patch("/api/providers/{provider_id}")
    async def patch_provider(provider_id: str, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)

        p = state.config.providers.get(provider_id)
        if p is None:
            return JSONResponse(
                {"error": {"message": f"Unknown provider: {provider_id}"}},
                status_code=404,
            )
        if "enabled" not in body:
            return JSONResponse(
                {"error": {"message": "Only field 'enabled' is supported"}},
                status_code=400,
            )

        enabled = bool(body["enabled"])
        written = False
        warning: str | None = None

        if state.config_path is None:
            warning = "config_path not set; in-memory only"
        else:
            try:
                set_provider_enabled(state.config_path, provider_id, enabled)
                written = True
            except Exception as e:  # noqa: BLE001 — do not log secrets
                warning = f"YAML write failed: {e.__class__.__name__}"

        # Always update live registry/monitor even if the file write failed.
        p.enabled = enabled
        state.registry = Registry(state.config)
        state.monitor.register_provider(
            provider_id,
            type=p.type,
            base_url=p.base_url,
            enabled=p.enabled,
            models=list(p.models),
        )
        await state.restart_health_tasks()

        return JSONResponse(
            {
                "ok": True,
                "id": provider_id,
                "enabled": enabled,
                "written": written,
                "warning": warning,
            }
        )

    @app.post("/api/providers/{provider_id}/test")
    async def test_provider(provider_id: str) -> Response:
        """Lightweight upstream reachability probe. Never logs keys."""
        p = state.config.providers.get(provider_id)
        if p is None:
            return JSONResponse(
                {"error": {"message": f"Unknown provider: {provider_id}"}},
                status_code=404,
            )
        if state.http is None:
            return JSONResponse(
                {"error": {"message": "HTTP client not ready"}},
                status_code=503,
            )

        result = await probe_provider(state.http, p)
        state.monitor.set_health(provider_id, result)
        payload: dict[str, Any] = {
            "provider_id": provider_id,
            "ok": result["ok"],
            "status_code": result["status_code"],
            "latency_ms": result["latency_ms"],
        }
        if "error" in result:
            payload["error"] = result["error"]
        return JSONResponse(payload)

    @app.get("/")
    async def home(request: Request) -> Response:
        """Unified entry: admin → dashboard, everyone else → portal."""
        session = _resolve_session(state, request)
        if session is not None and session["user"].get("role") == "admin":
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/portal", status_code=302)

    @app.get("/dashboard")
    async def dashboard(request: Request) -> Response:
        """Admin-only control plane. Non-admins are sent to /portal."""
        session = _resolve_session(state, request)
        if session is None or session["user"].get("role") != "admin":
            # Prefer portal login; 302 keeps the URL clean for bookmarks.
            return RedirectResponse("/portal?next=/dashboard", status_code=302)
        html_path = STATIC_DIR / "dashboard.html"
        return Response(html_path.read_text(encoding="utf-8"), media_type="text/html")

    @app.get("/portal")
    async def portal() -> Response:
        html_path = STATIC_DIR / "portal.html"
        if not html_path.exists():
            return JSONResponse(
                {"error": {"message": "Portal UI not installed", "type": "ConfigError"}},
                status_code=404,
            )
        return Response(html_path.read_text(encoding="utf-8"), media_type="text/html")

    # ---------- shared proxy executor ----------

    async def _proxy_call(
        *,
        request: Request,
        protocol: str,
        path: str,
        model: str,
        payload: dict[str, Any],
        call: Callable[
            [ResolvedRoute, dict[str, Any], bool],
            Coroutine[Any, Any, tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]],
        ],
    ) -> Response:
        """Resolve model, call upstream with monitor + optional fallback, return HTTP response."""
        assert state.http is not None

        try:
            route = state.registry.resolve(str(model))
        except ModelNotFoundError as e:
            return _error_response(e)

        stream = bool(payload.get("stream"))
        body = dict(payload)
        body["model"] = route.model

        attempts: list[ResolvedRoute] = [route]
        fb = state.config.defaults.fallback_model
        if fb and fb != route.requested_model and fb != route.model:
            try:
                alt = state.registry.resolve(fb)
                if alt.provider_id != route.provider_id or alt.model != route.model:
                    attempts.append(alt)
            except ModelNotFoundError:
                pass

        last_err: ProxyError | None = None
        user_agent = request.headers.get("user-agent", "")
        client_app = _guess_client_app(user_agent)
        hdrs = _sanitize_headers(request)
        auth_user_id, auth_username = _request_user_meta(request)
        for idx, rt in enumerate(attempts):
            rid = state.monitor.begin(
                provider_id=rt.provider_id,
                model=rt.model,
                requested_model=rt.requested_model,
                protocol=protocol,
                path=path,
                client=_client_ip(request),
                user_agent=user_agent,
                app=client_app,
                headers=hdrs,
                user_id=auth_user_id,
                username=auth_username,
            )
            started = time.time()
            model_limit = state.config.resolve_max_output_tokens(rt.model, rt.requested_model)
            adapter = create_adapter(
                rt.provider_id,
                rt.provider,
                state.config.defaults,
                state.http,
                model_max_output_tokens=model_limit,
            )
            attempt_body = dict(body)
            attempt_body["model"] = rt.model
            attempt_body, effective_max = apply_max_tokens_policy(
                attempt_body,
                model_limit=model_limit,
                raise_to_model_limit=state.config.defaults.raise_max_tokens_to_model_limit,
            )
            if protocol == "anthropic" and "max_tokens" not in attempt_body:
                attempt_body["max_tokens"] = int(effective_max or DEFAULT_MAX_TOKENS)

            try:
                status, json_body, byte_iter = await call(adapter, attempt_body, stream)
            except UpstreamError as e:
                state.monitor.end(
                    rid,
                    provider_id=rt.provider_id,
                    http_status=e.status_code,
                    error=e.message,
                    started_at=started,
                )
                last_err = e
                continue
            except Exception as e:  # noqa: BLE001
                state.monitor.end(
                    rid,
                    provider_id=rt.provider_id,
                    http_status=500,
                    error=str(e),
                    started_at=started,
                )
                last_err = ProxyError(str(e), status_code=500)
                continue

            if status >= 400 and not stream:
                state.monitor.end(
                    rid,
                    provider_id=rt.provider_id,
                    http_status=status,
                    error=f"HTTP {status}",
                    started_at=started,
                )
                last_err = UpstreamError(
                    f"HTTP {status} from {rt.provider_id}",
                    status_code=status,
                    detail=json_body,
                )
                # try fallback only on transport/upstream hard failures, not 4xx from model API
                if status < 500 and status not in (408, 429):
                    return JSONResponse(json_body or {}, status_code=status)
                continue

            if not stream:
                pt, ct = extract_usage(json_body)
                state.monitor.end(
                    rid,
                    provider_id=rt.provider_id,
                    http_status=status,
                    error=None,
                    started_at=started,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                )
                return JSONResponse(
                    json_body,
                    status_code=status,
                    headers={"X-Proxy-Effective-Max-Tokens": str(effective_max or "")},
                )

            if status >= 400:
                # Stream request failed before any body: surface JSON, do not
                # wrap an error page as SSE (clients see a truncated "reply").
                detail = None
                if byte_iter is not None:
                    try:
                        chunks = []
                        async for c in byte_iter:
                            chunks.append(c)
                            if sum(len(x) for x in chunks) > 64_000:
                                break
                        detail = b"".join(chunks)
                    except Exception:  # noqa: BLE001
                        detail = None
                state.monitor.end(
                    rid,
                    provider_id=rt.provider_id,
                    http_status=status,
                    error=f"HTTP {status}",
                    started_at=started,
                )
                last_err = UpstreamError(
                    f"HTTP {status} from {rt.provider_id}",
                    status_code=status,
                    detail=(detail.decode("utf-8", errors="replace")[:2000] if detail else None),
                )
                if status < 500 and status not in (408, 429):
                    if detail:
                        return Response(content=detail, status_code=status, media_type="application/json")
                    return _error_response(last_err)
                continue

            sniffer = StreamUsageSniffer(protocol)
            client_proto = protocol

            async def event_gen(
                byte_iter=byte_iter,
                rid=rid,
                rt=rt,
                started=started,
                status=status,
                sniffer=sniffer,
                client_proto=client_proto,
            ) -> AsyncIterator[bytes]:
                try:
                    assert byte_iter is not None
                    async for chunk in byte_iter:
                        sniffer.feed(chunk)
                        yield chunk
                except Exception as e:  # noqa: BLE001
                    pt, ct = sniffer.usage()
                    state.monitor.end(
                        rid,
                        provider_id=rt.provider_id,
                        http_status=502,
                        error=f"stream: {type(e).__name__}: {e}",
                        started_at=started,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                    )
                    # Do not die silently — client would see a truncated reply.
                    yield stream_error_frame(
                        client_proto,
                        f"Upstream stream aborted: {type(e).__name__}: {e}",
                    )
                    return
                else:
                    pt, ct = sniffer.usage()
                    state.monitor.end(
                        rid,
                        provider_id=rt.provider_id,
                        http_status=status,
                        error=None,
                        started_at=started,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                    )

            return StreamingResponse(
                event_gen(),
                status_code=status,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Proxy-Request-Id": rid,
                    "X-Proxy-Effective-Max-Tokens": str(effective_max or ""),
                },
            )

        if last_err is not None:
            return _error_response(last_err)
        return _error_response(ProxyError("No upstream available", status_code=503))

    # ---------- OpenAI-compatible ----------

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        return {"object": "list", "data": state.registry.list_models()}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        model = payload.get("model")
        if not model:
            return JSONResponse({"error": {"message": "Field 'model' is required"}}, status_code=400)

        async def call(adapter, body, stream):
            return await adapter.chat_completions(body, stream=stream)

        return await _proxy_call(
            request=request,
            protocol="openai",
            path="/v1/chat/completions",
            model=str(model),
            payload=payload,
            call=call,
        )

    # ---------- Anthropic ----------

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)
        model = payload.get("model")
        if not model:
            return JSONResponse({"error": {"message": "Field 'model' is required"}}, status_code=400)
        if "max_tokens" not in payload and "max_completion_tokens" not in payload:
            payload = dict(payload)
            from .convert import DEFAULT_MAX_TOKENS

            # Prefer per-model limit from config.model_limits; else global fallback.
            resolved = None
            try:
                r = state.registry.resolve(str(model))
                resolved = state.config.resolve_max_output_tokens(r.model, r.requested_model)
            except ModelNotFoundError:
                resolved = state.config.resolve_max_output_tokens(str(model))
            payload["max_tokens"] = int(resolved or DEFAULT_MAX_TOKENS)

        async def call(adapter, body, stream):
            return await adapter.messages(body, stream=stream)

        return await _proxy_call(
            request=request,
            protocol="anthropic",
            path="/v1/messages",
            model=str(model),
            payload=payload,
            call=call,
        )

    @app.exception_handler(ProxyError)
    async def _proxy_error_handler(request: Request, exc: ProxyError) -> JSONResponse:
        return _error_response(exc)

    return app
