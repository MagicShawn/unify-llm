from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .adapters import create_adapter
from .config import AppConfig, load_config, set_provider_enabled
from .errors import ConfigError, ModelNotFoundError, ProxyError, UpstreamError
from .monitor import Monitor, StreamUsageSniffer, extract_usage
from .registry import Registry, ResolvedRoute

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    def __init__(self, config: AppConfig, config_path: str | Path | None = None):
        self.config = config
        self.config_path: Path | None = Path(config_path) if config_path is not None else None
        self.registry = Registry(config)
        self.monitor = Monitor()
        self._register_all(config)
        self.http: httpx.AsyncClient | None = None

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
            item["totals"] = totals
        return item

    async def startup(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )

    async def shutdown(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return ""


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


def create_app(config_path: str | Path | None = None, config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config(config_path or "config.yaml")
    state = AppState(config, config_path=config_path)
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

    if gateway_key:

        @app.middleware("http")
        async def gateway_auth(request: Request, call_next):
            path = request.url.path
            protected = path.startswith(("/v1/", "/api/"))
            if protected and _extract_client_key(request) != gateway_key:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "message": "Invalid or missing gateway API key. "
                            "Send Authorization: Bearer <key> or x-api-key: <key>.",
                            "type": "AuthenticationError",
                        }
                    },
                )
            return await call_next(request)

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
        return state.monitor.status()

    @app.get("/api/history")
    async def api_history(limit: int = 50) -> dict[str, Any]:
        return state.monitor.history(limit=min(max(limit, 1), 200))

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
            "providers": providers,
            "aliases": state.config.aliases,
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
        return JSONResponse({"ok": True, "config_path": str(path), **counts})

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
            resp = await state.http.get(url, headers=headers, timeout=10.0)
            latency_ms = int((time.time() - started) * 1000)
            return JSONResponse(
                {
                    "provider_id": provider_id,
                    "ok": resp.status_code < 400,
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                }
            )
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.time() - started) * 1000)
            return JSONResponse(
                {
                    "provider_id": provider_id,
                    "ok": False,
                    "status_code": 0,
                    "latency_ms": latency_ms,
                    "error": e.__class__.__name__,
                }
            )

    @app.get("/dashboard")
    async def dashboard() -> Response:
        html_path = STATIC_DIR / "dashboard.html"
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
        for idx, rt in enumerate(attempts):
            rid = state.monitor.begin(
                provider_id=rt.provider_id,
                model=rt.model,
                requested_model=rt.requested_model,
                protocol=protocol,
                path=path,
                client=_client_ip(request),
            )
            started = time.time()
            adapter = create_adapter(rt.provider_id, rt.provider, state.config.defaults, state.http)
            attempt_body = dict(body)
            attempt_body["model"] = rt.model

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
                return JSONResponse(json_body, status_code=status)

            sniffer = StreamUsageSniffer(protocol)

            async def event_gen(
                byte_iter=byte_iter,
                rid=rid,
                rt=rt,
                started=started,
                status=status,
                sniffer=sniffer,
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
                        error=f"stream: {e}",
                        started_at=started,
                        prompt_tokens=pt,
                        completion_tokens=ct,
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
                headers={"Cache-Control": "no-cache", "X-Proxy-Request-Id": rid},
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
        if "max_tokens" not in payload:
            payload = dict(payload)
            payload["max_tokens"] = 4096

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
