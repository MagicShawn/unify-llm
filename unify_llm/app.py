from __future__ import annotations

import asyncio
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
from .config import AppConfig, ProviderConfig, load_config, set_provider_enabled
from .errors import ConfigError, ModelNotFoundError, ProxyError, UpstreamError
from .monitor import Monitor, StreamUsageSniffer, extract_usage
from .rate_limit import RateLimiter
from .registry import Registry, ResolvedRoute

STATIC_DIR = Path(__file__).parent / "static"

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
    def __init__(self, config: AppConfig, config_path: str | Path | None = None):
        self.config = config
        self.config_path: Path | None = Path(config_path) if config_path is not None else None
        self.registry = Registry(config)
        self.monitor = Monitor(pricing=config.pricing)
        self.limiter = RateLimiter(
            requests_per_minute=config.limits.requests_per_minute,
            max_concurrent=config.limits.max_concurrent,
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

    # Rate limit /v1/* only (token bucket per client IP + optional concurrency cap).
    # Always registered so admin reload can turn limits on without restart.
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/v1/"):
            return await call_next(request)
        if not state.limiter.enabled:
            return await call_next(request)

        decision = state.limiter.check(_client_ip(request))
        if not decision.allowed:
            if decision.reason == "max_concurrent":
                message = (
                    f"Too many concurrent requests (max {state.limiter.max_concurrent}). "
                    "Retry shortly."
                )
            else:
                rpm = state.limiter.requests_per_minute
                message = (
                    f"Rate limit exceeded: {rpm} requests per minute. "
                    f"Retry after {decision.retry_after}s."
                )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": message,
                        "type": "RateLimitError",
                        "retry_after": decision.retry_after,
                    }
                },
                headers={"Retry-After": str(decision.retry_after)},
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
        if "max_tokens" not in payload and "max_completion_tokens" not in payload:
            payload = dict(payload)
            # Anthropic API requires max_tokens. Use a high default so long
            # generations are not truncated when the client omits the field.
            from .convert import DEFAULT_MAX_TOKENS

            payload["max_tokens"] = DEFAULT_MAX_TOKENS

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
