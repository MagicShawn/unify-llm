from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ..convert import (
    anthropic_messages_to_openai_chat,
    anthropic_response_to_openai_chat,
    openai_response_to_anthropic_message,
    openai_sse_to_anthropic_sse,
)
from .base import BaseAdapter


class OpenAICompatAdapter(BaseAdapter):
    """Upstream speaks OpenAI-compatible Chat Completions."""

    @property
    def headers_auth(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.provider.api_key:
            h["Authorization"] = f"Bearer {self.provider.api_key}"
        return h

    def _url_chat(self) -> str:
        base = self.provider.base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    async def chat_completions(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        body = dict(payload)
        body["model"] = payload.get("model")
        body["stream"] = stream
        if stream:
            # Ask OpenAI-compatible upstreams for a final usage chunk (OpenAI + DeepSeek).
            so = body.get("stream_options")
            if not isinstance(so, dict):
                so = {}
            so.setdefault("include_usage", True)
            body["stream_options"] = so
        resp = await self._request_with_retries(
            "POST",
            self._url_chat(),
            headers=self.headers_auth,
            json_body=body,
            stream=stream,
        )
        if stream:
            return resp.status_code, None, resp.aiter_bytes()
        data = json.loads(resp.text)
        return resp.status_code, data, None

    async def messages(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
    ) -> tuple[int, dict[str, Any] | None, AsyncIterator[bytes] | None]:
        """Anthropic client → OpenAI-compatible upstream (cross-protocol)."""
        chat = anthropic_messages_to_openai_chat(
            payload,
        )
        # If convert used a default, overlay adapter model limit when client omitted max_tokens
        if self.model_max_output_tokens and "max_tokens" not in payload:
            chat["max_tokens"] = int(self.model_max_output_tokens)
        model = str(chat.get("model") or payload.get("model") or "")
        status, data, byte_iter = await self.chat_completions(chat, stream=stream)
        if status >= 400:
            return status, data, None
        if stream:
            assert byte_iter is not None
            return status, None, openai_sse_to_anthropic_sse(byte_iter, model=model)
        assert data is not None
        return status, openai_response_to_anthropic_message(data, model=model), None
