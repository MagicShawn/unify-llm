"""Cross-protocol streaming regressions; all upstream data stays in memory."""

import asyncio
import json

import pytest

from unify_llm.convert import anthropic_sse_to_openai_sse, openai_sse_to_anthropic_sse
from unify_llm.monitor import StreamUsageSniffer


def _event(data):
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


def _convert(converter, chunks, **kwargs):
    async def source():
        for chunk in chunks:
            yield chunk

    async def collect():
        return b"".join([
            chunk async for chunk in converter(source(), model="test-model", **kwargs)
        ])

    return asyncio.run(collect())


def _objects(output):
    return [
        json.loads(line[5:])
        for line in output.decode().splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("delta_type,field", [("text_delta", "text"), ("input_json_delta", "partial_json")])
def test_anthropic_stream_preserves_utf8_at_every_byte_boundary(newline, delta_type, field):
    text = '\u4f60\u597d\U0001f642'
    raw = _event({"type": "content_block_delta", "index": 0, "delta": {"type": delta_type, field: text}})
    raw = raw.replace(b"\n", newline)
    output = _convert(anthropic_sse_to_openai_sse, [raw[i:i + 1] for i in range(len(raw))])
    deltas = [obj["choices"][0]["delta"] for obj in _objects(output)]
    if delta_type == "text_delta":
        assert "".join(delta.get("content", "") for delta in deltas) == text
    else:
        assert "".join(call["function"]["arguments"] for delta in deltas for call in delta.get("tool_calls", [])) == text


@pytest.mark.parametrize("prefix", [b": heartbeat\n\n", b"event: completion\n", b"id: 12\nretry: 1000\n", b"data: invalid-json\n\n"])
def test_openai_stream_skips_non_json_sse_fields_without_losing_later_text(prefix):
    text = _event({"choices": [{"delta": {"content": "hello"}}]})
    output = _convert(openai_sse_to_anthropic_sse, [prefix, text, b"data: [DONE]\n\n"])
    assert "".join(obj.get("delta", {}).get("text", "") for obj in _objects(output)) == "hello"


@pytest.mark.parametrize("direction", ["openai_to_anthropic", "anthropic_to_openai"])
def test_converted_stream_exposes_upstream_usage_to_billing(direction):
    if direction == "anthropic_to_openai":
        chunks = [
            _event({"type": "message_start", "message": {"usage": {"input_tokens": 12000, "output_tokens": 0}}}),
            _event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4000}}),
            _event({"type": "message_stop"}),
        ]
        output = _convert(anthropic_sse_to_openai_sse, chunks, include_usage=True)
        sniffer = StreamUsageSniffer("openai")
        usage_chunks = [obj for obj in _objects(output) if obj.get("usage")]
        assert usage_chunks[-1]["choices"] == []
        assert usage_chunks[-1]["usage"]["total_tokens"] == 16000
        assert output.endswith(b"data: [DONE]\n\n")
    else:
        chunks = [
            _event({"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}),
            _event({"choices": [], "usage": {"prompt_tokens": 12000, "completion_tokens": 4000}}),
            b"data: [DONE]\n\n",
        ]
        output = _convert(openai_sse_to_anthropic_sse, chunks)
        sniffer = StreamUsageSniffer("anthropic")
    sniffer.feed(output)
    assert sniffer.usage() == (12000, 4000)


@pytest.mark.parametrize("kwargs", ({}, {"include_usage": False}))
def test_default_openai_stream_keeps_choices_nonempty_while_exposing_billing_usage(kwargs):
    chunks = [
        _event({"type": "message_start", "message": {
            "usage": {"input_tokens": 12, "output_tokens": 0}
        }}),
        _event({"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}}),
        _event({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4}}),
        _event({"type": "message_stop"}),
    ]
    output = _convert(anthropic_sse_to_openai_sse, chunks, **kwargs)
    objects = _objects(output)

    assert all(obj["choices"] for obj in objects)
    assert all(obj["choices"][0].get("delta") is not None for obj in objects)
    sniffer = StreamUsageSniffer("openai")
    sniffer.feed(output)
    assert sniffer.usage() == (12, 4)
