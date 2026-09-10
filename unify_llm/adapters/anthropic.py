from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ..convert import (
    anthropic_sse_to_openai_sse,
    openai_chat_to_anthropic_messages,
    anthropic_response_to_openai_chat,
)
from .base import BaseAdapter


class AnthropicAdapter(BaseAdapter):
    """Upstream speaks Anthropic Messages API."""

    @property
    def headers_auth(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.provider.api_key or "",
            "anthropic-version": "2023-06-01",
        }

    def _url_messages(self) -> str:
        base = self.provider.base_url
        if base.endswith("/messages"):
            return base
        return f"{base}/v1/messages"

    async def messages(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        body = dict(payload)
        body["model"] = payload.get("model")
        body["stream"] = stream
        resp = await self._request_with_retries(
            "POST",
            self._url_messages(),
            headers=self.headers_auth,
            json_body=body,
            stream=stream,
        )
        if stream:
            return resp.status_code, None, resp.aiter_bytes()
        data = json.loads(resp.text)
        return resp.status_code, data, None

    async def chat_completions(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        """OpenAI client → Anthropic upstream (cross-protocol)."""
        msg = openai_chat_to_anthropic_messages(payload)
        model = str(msg.get("model") or payload.get("model") or "")
        status, data, byte_iter = await self.messages(msg, stream=stream)
        if status >= 400:
            return status, data, None
        if stream:
            assert byte_iter is not None
            return status, None, anthropic_sse_to_openai_sse(byte_iter, model=model)
        assert data is not None
        return status, anthropic_response_to_openai_chat(data, model=model), None
