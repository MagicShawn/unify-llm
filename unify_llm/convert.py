"""Conversion between OpenAI chat.completions and Anthropic Messages.

Supports text, tools/tool_use/tool_result, and stream (SSE) both directions.
Same-protocol paths do not use these helpers for body rewrite.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

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


def apply_max_tokens_policy(
    payload: dict[str, Any],
    *,
    model_limit: int | None,
    raise_to_model_limit: bool,
) -> tuple[dict[str, Any], int | None]:
    body = dict(payload)
    client_raw = body.get("max_tokens")
    if client_raw is None:
        client_raw = body.get("max_completion_tokens")
    client_n: int | None = None
    if client_raw is not None:
        try:
            client_n = max(int(client_raw), 1)
        except (TypeError, ValueError):
            client_n = None

    effective = client_n
    if effective is None and model_limit:
        effective = int(model_limit)
        body["max_tokens"] = effective
    elif (
        effective is not None
        and raise_to_model_limit
        and model_limit
        and effective < int(model_limit)
    ):
        effective = int(model_limit)
        body["max_tokens"] = effective
    return body, effective


def stream_error_frame(protocol: str, message: str) -> bytes:
    payload = {
        "type": "error",
        "error": {"type": "upstream_error", "message": message},
    }
    if protocol == "anthropic":
        return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    body = {"error": {"message": message, "type": "upstream_error"}}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


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
                elif "text" in block and block.get("type") not in ("tool_use", "tool_result"):
                    parts.append(str(block.get("text") or ""))
        return "".join(parts)
    if isinstance(content, dict):
        if block_text := content.get("text"):
            return str(block_text)
        return ""
    return str(content)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _parse_json_arg(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return raw


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def openai_tools_to_anthropic(tools: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            item: dict[str, Any] = {
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            out.append(item)
        elif t.get("name"):
            out.append(
                {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "input_schema": t.get("input_schema")
                    or t.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
    return out


def anthropic_tools_to_openai(tools: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name") or "",
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema")
                    or t.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def openai_tool_choice_to_anthropic(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "none":
        return None
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        name = fn.get("name") or choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if choice is None:
        return None
    if choice == "auto" or (isinstance(choice, dict) and choice.get("type") == "auto"):
        return "auto"
    if choice == "any" or (isinstance(choice, dict) and choice.get("type") == "any"):
        return "required"
    if isinstance(choice, dict) and choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


# ---------------------------------------------------------------------------
# request: OpenAI → Anthropic
# ---------------------------------------------------------------------------


def openai_chat_to_anthropic_messages(
    payload: dict[str, Any],
    *,
    model_max_output_tokens: int | None = None,
) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    # OpenAI tool_call_id → Anthropic tool_use_id pairing via sequential map
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in payload.get("messages") or []:
        role = m.get("role")
        content = m.get("content")

        if role in ("system", "developer"):
            flush_tool_results()
            system_parts.append(_as_text(content))
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block
            result_text = content if isinstance(content, str) else _as_text(content)
            if isinstance(content, list):
                # already block-ish
                result_text = _as_text(content)
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or m.get("id") or "",
                    "content": result_text,
                }
            )
            continue

        flush_tool_results()

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _as_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": _parse_json_arg(fn.get("arguments")),
                    }
                )
            # legacy function_call
            fc = m.get("function_call")
            if isinstance(fc, dict) and fc.get("name"):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": m.get("tool_call_id") or "call_legacy",
                        "name": fc.get("name"),
                        "input": _parse_json_arg(fc.get("arguments")),
                    }
                )
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            messages.append({"role": "assistant", "content": blocks})
            continue

        # user: may contain tool_result blocks already (rare on OpenAI path)
        if isinstance(content, list):
            anth_blocks: list[dict[str, Any]] = []
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("text", "image_url"):
                    if b.get("type") == "text":
                        anth_blocks.append({"type": "text", "text": b.get("text") or ""})
                    else:
                        # drop images in v1 text path
                        continue
                else:
                    anth_blocks.append({"type": "text", "text": _as_text(b)})
            messages.append(
                {"role": "user", "content": anth_blocks or [{"type": "text", "text": ""}]}
            )
        else:
            messages.append({"role": "user", "content": _as_text(content)})

    flush_tool_results()

    # Merge consecutive same-role text-only messages; keep structured blocks intact
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if (
            merged
            and merged[-1]["role"] == msg["role"]
            and isinstance(merged[-1]["content"], str)
            and isinstance(msg["content"], str)
        ):
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{msg['content']}"
        else:
            merged.append(dict(msg))

    if not merged:
        merged = [{"role": "user", "content": "(empty)"}]
    if merged[0]["role"] != "user":
        # tool_result turns are user; if first is assistant, insert placeholder
        if not (
            isinstance(merged[0].get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in merged[0]["content"]
            )
        ):
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
    tools = openai_tools_to_anthropic(payload.get("tools"))
    if tools:
        out["tools"] = tools
    tc = openai_tool_choice_to_anthropic(payload.get("tool_choice"))
    if tc:
        out["tool_choice"] = tc
    return out


# ---------------------------------------------------------------------------
# request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


def anthropic_messages_to_openai_chat(payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _as_text(system)})

    for m in payload.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content")

        if isinstance(content, str):
            messages.append({"role": role if role in ("user", "assistant") else "user", "content": content})
            continue

        if not isinstance(content, list):
            messages.append(
                {"role": role if role in ("user", "assistant") else "user", "content": _as_text(content)}
            )
            continue

        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for b in content:
            if not isinstance(b, dict):
                text_parts.append(_as_text(b))
                continue
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text") or "")
            elif btype == "tool_use":
                tool_uses.append(
                    {
                        "id": b.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": b.get("name") or "",
                            "arguments": _json_dumps(b.get("input") or {}),
                        },
                    }
                )
            elif btype == "tool_result":
                rc = b.get("content")
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id") or "",
                        "content": rc if isinstance(rc, str) else _as_text(rc),
                    }
                )
            # ignore images etc.

        if role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
            if tool_uses:
                msg["tool_calls"] = tool_uses
            if msg.get("content") == "" and not tool_uses:
                msg["content"] = ""
            messages.append(msg)
            continue

        # user: emit tool results first, then text
        for tr in tool_results:
            messages.append(tr)
        if text_parts or not tool_results:
            messages.append({"role": "user", "content": "".join(text_parts)})

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
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
    tools = anthropic_tools_to_openai(payload.get("tools"))
    if tools:
        out["tools"] = tools
    tc = anthropic_tool_choice_to_openai(payload.get("tool_choice"))
    if tc is not None:
        out["tool_choice"] = tc
    return out


# ---------------------------------------------------------------------------
# stop reasons
# ---------------------------------------------------------------------------


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
    text = _as_text(message.get("content"))
    if not text:
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
    """Non-stream OpenAI chat.completions → Anthropic message (incl. tool_use)."""
    if data.get("type") == "message" and "content" in data:
        return data
    if data.get("type") == "error":
        return data

    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    usage = data.get("usage") or {}

    blocks: list[dict[str, Any]] = []
    text = _openai_message_text(message)
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "input": _parse_json_arg(fn.get("arguments")),
            }
        )
    if not blocks:
        blocks = [{"type": "text", "text": ""}]

    return {
        "id": f"msg_{data.get('id') or 'proxy'}",
        "type": "message",
        "role": "assistant",
        "model": model or data.get("model") or "",
        "content": blocks,
        "stop_reason": _finish_reason_to_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def anthropic_response_to_openai_chat(
    data: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Non-stream Anthropic message → OpenAI chat.completion (incl. tool_calls)."""
    if "choices" in data:
        return data

    blocks = data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                text_parts.append(_as_text(b))
                continue
            if b.get("type") == "text":
                text_parts.append(b.get("text") or "")
            elif b.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": b.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": b.get("name") or "",
                            "arguments": _json_dumps(b.get("input") or {}),
                        },
                    }
                )

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None

    usage = data.get("usage") or {}
    return {
        "id": f"chatcmpl-{data.get('id') or 'proxy'}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or data.get("model") or "",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _stop_reason_to_finish_reason(data.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0,
            "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
        },
    }


def _anthropic_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


# ---------------------------------------------------------------------------
# stream: OpenAI SSE → Anthropic SSE (text + tool_use)
# ---------------------------------------------------------------------------


async def openai_sse_to_anthropic_sse(
    source: AsyncIterator[bytes],
    *,
    model: str,
) -> AsyncIterator[bytes]:
    msg_id = f"msg_proxy_{model}"
    output_tokens = 0
    input_tokens = 0
    saw_usage = False
    buffer = b""

    # block index management
    next_index = 1
    text_index: int | None = 0
    tool_index_by_slot: dict[int, int] = {}
    open_text = True
    open_tools: set[int] = set()
    finish_reason = None

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

    async for chunk in source:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            # SSE comments and metadata are not JSON payloads.
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip().decode("utf-8", errors="replace")
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                # This line is complete; requeuing it would block all later data.
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

            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            text = delta.get("content")
            if not text:
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
                        "index": text_index if text_index is not None else 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )

            # OpenAI tool_calls deltas
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                slot = int(tc.get("index") or 0)
                if slot not in tool_index_by_slot:
                    # close text block first if open and we already emitted text
                    if open_text and text_index is not None:
                        yield _anthropic_sse(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": text_index},
                        )
                        open_text = False
                    tool_index_by_slot[slot] = next_index
                    next_index += 1
                    fn0 = tc.get("function") or {}
                    yield _anthropic_sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": tool_index_by_slot[slot],
                            "content_block": {
                                "type": "tool_use",
                                "id": tc.get("id") or f"toolu_{slot}",
                                "name": fn0.get("name") or "",
                                "input": {},
                            },
                        },
                    )
                    open_tools.add(slot)
                idx = tool_index_by_slot[slot]
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if args:
                    yield _anthropic_sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        },
                    )

            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr

    # close open blocks
    if open_text and text_index is not None:
        yield _anthropic_sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": text_index},
        )
    for slot in list(open_tools):
        yield _anthropic_sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": tool_index_by_slot[slot]},
        )
    open_tools.clear()

    yield _anthropic_sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _finish_reason_to_stop_reason(finish_reason),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": max(output_tokens, 1 if not saw_usage else 0),
            },
        },
    )
    yield _anthropic_sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# stream: Anthropic SSE → OpenAI SSE (text + tool_calls)
# ---------------------------------------------------------------------------


async def anthropic_sse_to_openai_sse(
    source: AsyncIterator[bytes],
    *,
    model: str,
    include_usage: bool = False,
) -> AsyncIterator[bytes]:
    chunk_id = f"chatcmpl-{model}"
    created = int(time.time())
    buffer = b""
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    usage_emitted = False

    def _chunk(
        delta: dict[str, Any],
        finish: str | None = None,
        usage: dict[str, int] | None = None,
        usage_only: bool = False,
    ) -> bytes:
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
        if usage is not None and usage_only:
            body["choices"] = []
        if usage is not None:
            body["usage"] = usage
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode()

    def _usage() -> dict[str, int]:
        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    yield _chunk({"role": "assistant", "content": ""})

    # track anthropic content block index → openai tool_calls index
    block_tool_slot: dict[int, int] = {}
    next_tool_slot = 0

    async for chunk in source:
        buffer += chunk
        # Keep incomplete UTF-8 sequences as bytes until their event is complete.
        # Normalizing after appending also handles CRLF split across chunks.
        buffer = buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            raw_bytes, buffer = buffer.split(b"\n\n", 1)
            raw_event = raw_bytes.decode("utf-8", errors="replace")
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
            usage = (
                (obj.get("message") or {}).get("usage")
                if etype == "message_start"
                else obj.get("usage")
            )
            if isinstance(usage, dict):
                input_tokens = max(input_tokens, int(usage.get("input_tokens") or 0))
                output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
                saw_usage = True
            if etype == "content_block_start":
                cb = obj.get("content_block") or {}
                bidx = int(obj.get("index") or 0)
                if cb.get("type") == "tool_use":
                    slot = next_tool_slot
                    next_tool_slot += 1
                    block_tool_slot[bidx] = slot
                    yield _chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": slot,
                                    "id": cb.get("id") or f"call_{slot}",
                                    "type": "function",
                                    "function": {
                                        "name": cb.get("name") or "",
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                    )
            elif etype == "content_block_delta":
                delta = obj.get("delta") or {}
                bidx = int(obj.get("index") or 0)
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield _chunk({"content": delta["text"]})
                elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                    slot = block_tool_slot.get(bidx, 0)
                    yield _chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": slot,
                                    "function": {"arguments": delta["partial_json"]},
                                }
                            ]
                        }
                    )
            elif etype == "message_delta":
                stop = (obj.get("delta") or {}).get("stop_reason")
                if stop:
                    # Keep default streams compatible with clients that index
                    # choices[0], while still exposing usage to gateway billing.
                    final_usage = _usage() if saw_usage and not include_usage else None
                    yield _chunk({}, _stop_reason_to_finish_reason(stop), usage=final_usage)
                    usage_emitted = final_usage is not None
            elif etype == "message_stop":
                if saw_usage:
                    if include_usage:
                        yield _chunk({}, usage=_usage(), usage_only=True)
                    elif not usage_emitted:
                        yield _chunk({}, usage=_usage())
                yield b"data: [DONE]\n\n"
            elif etype == "error":
                err = obj.get("error") or {}
                yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n".encode()
                yield b"data: [DONE]\n\n"
