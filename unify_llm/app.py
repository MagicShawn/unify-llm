from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import create_adapter
from .config import AppConfig, load_config
from .errors import ModelNotFoundError, ProxyError, UpstreamError
from .monitor import Monitor, StreamUsageSniffer, extract_usage
from .registry import Registry, ResolvedRoute

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    def __init__(self, config: AppConfig):
        self.config = config
        self.registry = Registry(config)
        self.monitor = Monitor()
        for pid, p in config.providers.items():
            self.monitor.register_provider(
                pid,
                type=p.type,
                base_url=p.base_url,
                enabled=p.enabled,
                models=list(p.models),
            )
        self.http: httpx.AsyncClient | None = None

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


def create_app(config_path: str | Path | None = None, config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config(config_path or "config.yaml")
    state = AppState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Unify LLM", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    app.state.proxy = state

    # ---------- health / status / dashboard ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "unify_llm", "version": "0.1.0"}

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return state.monitor.status()

    @app.get("/api/history")
    async def api_history(limit: int = 50) -> dict[str, Any]:
        return state.monitor.history(limit=min(max(limit, 1), 200))

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        providers = {}
        for pid, p in config.providers.items():
            providers[pid] = {
                "type": p.type,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "models": p.models,
                "api_key": ("***" if p.api_key else ""),
            }
        return {
            "server": config.server.model_dump(),
            "defaults": config.defaults.model_dump(),
            "providers": providers,
            "aliases": config.aliases,
        }

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
        fb = config.defaults.fallback_model
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
            adapter = create_adapter(rt.provider_id, rt.provider, config.defaults, state.http)
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
