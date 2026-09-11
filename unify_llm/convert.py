"""Best-effort conversion between OpenAI chat.completions and Anthropic messages.

v1: text-focused. Tools / images may be dropped on cross-protocol paths.
Same-protocol paths are preferred and do not use these helpers for body rewrite
(only model name / stream flags).
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

# Fallback when neither the client nor model_limits provides a value.
# max_tokens is the completion budget, not the context window.
DEFAULT_MAX_TOKENS = 128_000


def _resolve_max_tokens(
    payload: dict[str, Any],
    *,
    model_default: int | None = None,
) -> int:
    raw = payload.get("max_tokens")
    if raw is None:
        raw = payload.get("max_completion_tokens")
    if raw is not None:
        try:
            return max(int(raw), 1)
        except (TypeError, ValueError):
            pass
    if model_default is not None and int(model_default) > 0:
        return int(model_default)
    return DEFAULT_MAX_TOKENS


def openai_chat_to_anthropic_messages(
    payload: dict[str, Any],
    *,
    model_max_output_tokens: int | None = None,
) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    for m in payload.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system" or role == "developer":
            system_parts.append(_as_text(content))
            continue
        if role == "tool":
            # collapse tool results into a user turn
            text = _as_text(content)
            messages.append({"role": "user", "content": text})
            continue
        if role == "assistant":
            messages.append({"role": "assistant", "content": _as_text(content)})
            continue
        # user / default
        messages.append({"role": "user", "content": _as_text(content)})

    # Anthropic requires alternating roles starting with user; merge consecutive same roles
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{msg['content']}"
        else:
            merged.append(dict(msg))
    if not merged:
        merged = [{"role": "user", "content": "(empty)"}]
    if merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(continue)"})

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": merged,
        "max_tokens": _resolve_max_tokens(payload, model_default=model_max_output_tokens),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    if payload.get("stop"):
        stop = payload["stop"]
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return out


def anthropic_messages_to_openai_chat(payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _as_text(system)})

    for m in payload.get("messages") or []:
        role = m.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": _as_text(m.get("content"))})

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
    # Pass through client max_tokens unchanged when present; never invent a low cap.
    if payload.get("max_tokens") is not None:
        out["max_tokens"] = payload["max_tokens"]
    elif payload.get("max_completion_tokens") is not None:
        out["max_tokens"] = payload["max_completion_tokens"]
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    if payload.get("stop_sequences"):
        out["stop"] = payload["stop_sequences"]
    return out


def _finish_reason_to_stop_reason(reason: str | None) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "end_turn",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    if not reason:
        return "end_turn"
    return mapping.get(reason, "end_turn")


def _stop_reason_to_finish_reason(reason: str | None) -> str:
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "stop_sequence": "stop",
    }
    if not reason:
        return "stop"
    return mapping.get(reason, "stop")


def _openai_message_text(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    text = _as_text(content)
    if not text:
        # DeepSeek / reasoning models may put the answer elsewhere
        for key in ("reasoning_content", "reasoning", "thinking"):
            extra = message.get(key)
            if extra:
                text = _as_text(extra)
                break
    return text


def openai_response_to_anthropic_message(
    data: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Non-stream OpenAI chat.completions → Anthropic message."""
    if "type" in data and data.get("type") in ("message", "error") and "content" in data:
        # already anthropic-shaped
        return data

    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    text = _openai_message_text(message)
    usage = data.get("usage") or {}
    input_tokens = usage.get("prompt_tokens") or 0
    output_tokens = usage.get("completion_tokens") or 0

    return {
        "id": f"msg_{data.get('id') or 'proxy'}",
        "type": "message",
        "role": "assistant",
        "model": model or data.get("model") or "",
        "content": [{"type": "text", "text": text}],
        "stop_reason": _finish_reason_to_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def anthropic_response_to_openai_chat(
    data: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Non-stream Anthropic message → OpenAI chat.completion."""
    if "choices" in data:
        return data

    blocks = data.get("content") or []
    parts: list[str] = []
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
    usage = data.get("usage") or {}
    return {
        "id": f"chatcmpl-{data.get('id') or 'proxy'}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or data.get("model") or "",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(parts)},
                "finish_reason": _stop_reason_to_finish_reason(data.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0,
            "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
        },
    }


def _sse_events(raw: bytes) -> list[str]:
    """Split SSE payload into data field values (no 'data: ' prefix)."""
    text = raw.decode("utf-8", errors="replace")
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            out.append(line[5:].strip())
    return out


def _anthropic_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def openai_sse_to_anthropic_sse(
    source: AsyncIterator[bytes],
    *,
    model: str,
) -> AsyncIterator[bytes]:
    """Convert OpenAI chat.completion chunks to Anthropic Messages SSE."""
    msg_id = f"msg_proxy_{model}"
    started = False
    finished = False
    output_tokens = 0
    input_tokens = 0
    saw_usage = False
    buffer = b""

    yield _anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _anthropic_sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    started = True

    finish_reason = None
    async for chunk in source:
        buffer += chunk
        # process complete lines
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            # re-wrap single line as SSE data for parser
            if line.startswith(b"data:"):
                payload = line[5:].strip().decode("utf-8", errors="replace")
            else:
                payload = line.decode("utf-8", errors="replace").strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("id") and str(obj["id"]).startswith(("chatcmpl", "req")):
                msg_id = f"msg_{obj['id']}"
            usage = obj.get("usage")
            if isinstance(usage, dict):
                if "prompt_tokens" in usage or "completion_tokens" in usage:
                    input_tokens = max(input_tokens, int(usage.get("prompt_tokens") or 0))
                    output_tokens = max(output_tokens, int(usage.get("completion_tokens") or 0))
                    saw_usage = True
                elif "input_tokens" in usage or "output_tokens" in usage:
                    input_tokens = max(input_tokens, int(usage.get("input_tokens") or 0))
                    output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
                    saw_usage = True
            choices = obj.get("choices") or []
            if not choices:
                # usage-only final chunk
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if not text:
                # reasoning models
                for key in ("reasoning_content", "reasoning", "thinking"):
                    if delta.get(key):
                        text = delta.get(key)
                        break
            if text:
                if not saw_usage:
                    output_tokens += 1
                yield _anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr

    if started:
        yield _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _finish_reason_to_stop_reason(finish_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": max(output_tokens, 1 if not saw_usage else 0)},
            },
        )
        yield _anthropic_sse("message_stop", {"type": "message_stop"})
        finished = True
    _ = finished


async def anthropic_sse_to_openai_sse(
    source: AsyncIterator[bytes],
    *,
    model: str,
) -> AsyncIterator[bytes]:
    """Convert Anthropic Messages SSE to OpenAI chat.completion chunks."""
    chunk_id = f"chatcmpl-{model}"
    created = int(time.time())
    buffer = b""

    def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        body = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        }
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode()

    yield _chunk({"role": "assistant", "content": ""})

    async for chunk in source:
        buffer += chunk
        text_buf = buffer.decode("utf-8", errors="replace")
        # parse whole events separated by blank lines
        while "\n\n" in text_buf:
            raw_event, text_buf = text_buf.split("\n\n", 1)
            data_lines = []
            for line in raw_event.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            etype = obj.get("type")
            if etype == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield _chunk({"content": delta["text"]})
            elif etype == "message_delta":
                stop = (obj.get("delta") or {}).get("stop_reason")
                if stop:
                    yield _chunk({}, _stop_reason_to_finish_reason(stop))
            elif etype == "message_stop":
                yield b"data: [DONE]\n\n"
        buffer = text_buf.encode("utf-8")


def _as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif "text" in block:
                    parts.append(str(block.get("text") or ""))
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content)
    return str(content)
