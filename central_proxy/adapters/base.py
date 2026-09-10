from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import DefaultsConfig, ProviderConfig
from ..errors import UpstreamError

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def build_timeout(provider: ProviderConfig, defaults: DefaultsConfig) -> httpx.Timeout:
    t = provider.timeout_seconds if provider.timeout_seconds is not None else defaults.timeout_seconds
    connect = defaults.connect_timeout_seconds
    return httpx.Timeout(t, connect=connect)


def max_retries_for(provider: ProviderConfig, defaults: DefaultsConfig) -> int:
    if provider.max_retries is not None:
        return provider.max_retries
    return defaults.max_retries


def should_retry(status_code: int | None, exc: Exception | None) -> bool:
    if exc is not None:
        return isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            ),
        )
    return status_code is not None and status_code in RETRYABLE_STATUS


class BaseAdapter:
    """Sends a chat/messages request to one upstream."""

    def __init__(
        self,
        provider_id: str,
        provider: ProviderConfig,
        defaults: DefaultsConfig,
        client: httpx.AsyncClient,
    ):
        self.provider_id = provider_id
        self.provider = provider
        self.defaults = defaults
        self.client = client

    @property
    def headers_auth(self) -> dict[str, str]:
        raise NotImplementedError

    async def chat_completions(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        """Call upstream with an OpenAI chat.completions-shaped payload.

        Returns (http_status, json_body, byte_iterator). Exactly one of json_body / byte_iterator is set.
        """
        raise NotImplementedError

    async def messages(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        """Call upstream with an Anthropic messages-shaped payload."""
        raise NotImplementedError

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        stream: bool,
    ) -> httpx.Response:
        retries = max_retries_for(self.provider, self.defaults)
        backoff = self.defaults.retry_backoff_seconds
        last_exc: Exception | None = None
        last_status: int | None = None

        for attempt in range(retries + 1):
            try:
                req = self.client.build_request(method, url, headers=headers, json=json_body)
                resp = await self.client.send(req, stream=stream)
                if resp.status_code >= 400 and not stream:
                    body = await resp.aread()
                    resp.aclose()
                    if attempt < retries and should_retry(resp.status_code, None):
                        last_status = resp.status_code
                        import asyncio

                        await asyncio.sleep(backoff * (attempt + 1))
                        continue
                    raise UpstreamError(
                        f"{self.provider_id} upstream HTTP {resp.status_code}",
                        status_code=resp.status_code,
                        detail=body.decode("utf-8", errors="replace")[:2000],
                    )
                if resp.status_code >= 400 and stream:
                    # For stream start failure, read error body if small
                    if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                        await resp.aread()
                        resp.aclose()
                        import asyncio

                        await asyncio.sleep(backoff * (attempt + 1))
                        last_status = resp.status_code
                        continue
                    # non-retryable or exhausted: return response so caller can surface
                    return resp
                return resp
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < retries and should_retry(None, e):
                    import asyncio

                    await asyncio.sleep(backoff * (attempt + 1))
                    continue
                raise UpstreamError(
                    f"{self.provider_id} upstream error: {e.__class__.__name__}: {e}",
                    status_code=502,
                ) from e

        raise UpstreamError(
            f"{self.provider_id} upstream failed after retries",
            status_code=502,
            detail={"last_status": last_status, "last_exc": str(last_exc)},
        )


async def aiter_error_as_sse(message: str) -> AsyncIterator[bytes]:
    payload = {"error": {"message": message, "type": "upstream_error"}}
    yield f"data: {json.dumps(payload)}\n\n".encode()
    yield b"data: [DONE]\n\n"
