"""Core unit tests for unify_llm: convert, registry, config, gateway auth.

Run from repo root:
    python -m pytest tests -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import (
    AppConfig,
    AuthConfig,
    ProviderConfig,
    expand_env,
    load_config,
)
from unify_llm.convert import (
    anthropic_messages_to_openai_chat,
    anthropic_response_to_openai_chat,
    openai_chat_to_anthropic_messages,
    openai_response_to_anthropic_message,
)
from unify_llm.errors import ModelNotFoundError
from unify_llm.monitor import StreamUsageSniffer, extract_usage
from unify_llm.registry import Registry


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def test_openai_chat_to_anthropic_messages_system_and_roles():
    payload = {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Again"},
            {"role": "tool", "content": "tool result"},
        ],
        "max_tokens": 128,
        "temperature": 0.2,
        "stop": ["END"],
    }
    out = openai_chat_to_anthropic_messages(payload)
    assert out["model"] == "m1"
    assert out["max_tokens"] == 128
    assert out["system"] == "You are helpful."
    assert out["temperature"] == 0.2
    assert out["stop_sequences"] == ["END"]
    roles = [m["role"] for m in out["messages"]]
    assert roles[0] == "user"
    assert "assistant" in roles
    # consecutive same-role turns are merged
    assert roles.count("user") >= 1
    # tool result collapsed into user
    assert any("tool result" in m["content"] for m in out["messages"])


def test_openai_chat_to_anthropic_messages_merges_and_defaults():
    out = openai_chat_to_anthropic_messages(
        {
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"},
            ]
        }
    )
    assert out["max_tokens"] == 4096
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == "a\n\nb"
    empty = openai_chat_to_anthropic_messages({"messages": []})
    assert empty["messages"] == [{"role": "user", "content": "(empty)"}]


def test_anthropic_messages_to_openai_chat():
    payload = {
        "model": "m2",
        "system": "sys",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stop_sequences": ["STOP"],
    }
    out = anthropic_messages_to_openai_chat(payload)
    assert out["model"] == "m2"
    assert out["max_tokens"] == 64
    assert out["stop"] == ["STOP"]
    assert out["messages"][0] == {"role": "system", "content": "sys"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_openai_response_to_anthropic_message():
    data = {
        "id": "chatcmpl-1",
        "model": "upstream-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    out = openai_response_to_anthropic_message(data)
    assert out["type"] == "message"
    assert out["id"] == "msg_chatcmpl-1"
    assert out["content"][0]["text"] == "Hello world"
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}
    # model override
    out2 = openai_response_to_anthropic_message(data, model="alias-m")
    assert out2["model"] == "alias-m"


def test_openai_response_to_anthropic_message_length_mapping():
    data = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
        "usage": {},
    }
    out = openai_response_to_anthropic_message(data)
    assert out["stop_reason"] == "max_tokens"
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_anthropic_response_to_openai_chat():
    data = {
        "id": "msg_abc",
        "model": "claude-x",
        "content": [{"type": "text", "text": "Hi there"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    out = anthropic_response_to_openai_chat(data)
    assert out["object"] == "chat.completion"
    assert out["id"] == "chatcmpl-msg_abc"
    choice = out["choices"][0]
    assert choice["message"]["content"] == "Hi there"
    assert choice["finish_reason"] == "stop"
    assert out["usage"]["prompt_tokens"] == 3
    assert out["usage"]["completion_tokens"] == 4
    assert out["usage"]["total_tokens"] == 7


def test_extract_usage_openai_and_anthropic():
    assert extract_usage(
        {"usage": {"prompt_tokens": 11, "completion_tokens": 22}}
    ) == (11, 22)
    assert extract_usage(
        {"usage": {"input_tokens": 7, "output_tokens": 8}}
    ) == (7, 8)
    assert extract_usage({}) == (0, 0)
    assert extract_usage(None) == (0, 0)
    assert extract_usage({"usage": "n/a"}) == (0, 0)


def test_stream_usage_sniffer_openai_sse():
    sniffer = StreamUsageSniffer("openai")
    lines = [
        'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n',
        'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":" world"}}]}\n\n',
        'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":34}}\n\n',
        "data: [DONE]\n\n",
    ]
    for line in lines:
        sniffer.feed(line.encode("utf-8"))
    assert sniffer.usage() == (12, 34)


def test_stream_usage_sniffer_anthropic_sse():
    sniffer = StreamUsageSniffer("anthropic")
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
        },
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]
    for ev in events:
        sniffer.feed(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
    assert sniffer.usage() == (5, 9)


def test_stream_usage_sniffer_split_chunks():
    """Feed complete SSE events across separate chunk boundaries."""
    sniffer = StreamUsageSniffer("openai")
    part1 = 'data: {"id":"c1","choices":[{"delta":{"content":"a"}}]}\n\n'
    part2 = (
        'data: {"id":"c1","choices":[],'
        '"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
    )
    sniffer.feed(part1.encode("utf-8"))
    sniffer.feed(part2.encode("utf-8"))
    assert sniffer.usage() == (1, 2)


def test_stream_usage_sniffer_partial_json_rebuffer():
    """JSON payload split mid-token must be re-buffered, not dropped."""
    sniffer = StreamUsageSniffer("openai")
    full = 'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":9}}\n\n'
    mid = len(full) // 2
    sniffer.feed(full[:mid].encode("utf-8"))
    assert sniffer.usage() == (0, 0)
    sniffer.feed(full[mid:].encode("utf-8"))
    assert sniffer.usage() == (7, 9)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def _registry_config() -> AppConfig:
    return AppConfig(
        providers={
            "enabled_p": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="k",
                enabled=True,
                models=["real-model", "other-model"],
            ),
            "disabled_p": ProviderConfig(
                type="anthropic",
                base_url="http://127.0.0.1:9",
                api_key="k",
                enabled=False,
                models=["disabled-model"],
            ),
        },
        aliases={"fast": "real-model", "chain": "fast"},
    )


def test_registry_alias_resolve():
    reg = Registry(_registry_config())
    route = reg.resolve("fast")
    assert route.provider_id == "enabled_p"
    assert route.model == "real-model"
    assert route.requested_model == "fast"
    assert route.is_alias is True
    assert route.upstream_type == "openai"


def test_registry_alias_chain():
    reg = Registry(_registry_config())
    route = reg.resolve("chain")
    assert route.model == "real-model"
    assert route.is_alias is True


def test_registry_direct_model():
    reg = Registry(_registry_config())
    route = reg.resolve("other-model")
    assert route.provider_id == "enabled_p"
    assert route.is_alias is False
    assert route.model == "other-model"


def test_registry_missing_model():
    reg = Registry(_registry_config())
    with pytest.raises(ModelNotFoundError) as ei:
        reg.resolve("no-such-model")
    assert "no-such-model" in str(ei.value)


def test_registry_disabled_provider_not_routable():
    reg = Registry(_registry_config())
    assert "disabled_p" not in reg.provider_ids()
    with pytest.raises(ModelNotFoundError):
        reg.resolve("disabled-model")
    # alias pointing only at a disabled model also fails
    cfg = _registry_config()
    cfg.aliases["bad"] = "disabled-model"
    reg2 = Registry(cfg)
    with pytest.raises(ModelNotFoundError):
        reg2.resolve("bad")


def test_registry_list_models_includes_aliases():
    reg = Registry(_registry_config())
    ids = {m["id"] for m in reg.list_models()}
    assert "real-model" in ids
    assert "fast" in ids
    assert "disabled-model" not in ids


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_expand_env_string_list_dict(monkeypatch):
    monkeypatch.setenv("TEST_UNIFY_KEY", "secret-value")
    assert expand_env("prefix-${TEST_UNIFY_KEY}-suffix") == "prefix-secret-value-suffix"
    assert expand_env("${TEST_UNIFY_KEY}") == "secret-value"
    assert expand_env(["${TEST_UNIFY_KEY}", "x"]) == ["secret-value", "x"]
    assert expand_env({"a": "${TEST_UNIFY_KEY}", "b": 1}) == {"a": "secret-value", "b": 1}
    # missing env → empty string
    assert expand_env("${TEST_UNIFY_MISSING}") == ""
    # non-string passthrough
    assert expand_env(42) == 42
    assert expand_env(None) is None


def test_load_config_from_temp_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNIT_TEST_UPSTREAM_KEY", "unit-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 9999, "dashboard": False},
                "auth": {"api_key": "gw-key"},
                "defaults": {"max_retries": 1},
                "providers": {
                    "local": {
                        "type": "openai",
                        "base_url": "http://127.0.0.1:18000/v1/",
                        "api_key": "${UNIT_TEST_UPSTREAM_KEY}",
                        "enabled": True,
                        "models": ["m-a", "m-b"],
                    },
                    "off": {
                        "type": "openai",
                        "base_url": "http://127.0.0.1:18001",
                        "enabled": False,
                        "models": ["m-off"],
                    },
                },
                "aliases": {"a": "m-a"},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.server.port == 9999
    assert cfg.server.dashboard is False
    assert cfg.auth.api_key == "gw-key"
    assert cfg.gateway_api_key() == "gw-key"
    assert cfg.providers["local"].api_key == "unit-key"
    # trailing slash stripped
    assert cfg.providers["local"].base_url == "http://127.0.0.1:18000/v1"
    assert cfg.providers["off"].enabled is False
    assert cfg.aliases["a"] == "m-a"
    catalog_ids = {m["id"] for m in cfg.model_catalog()}
    assert "m-a" in catalog_ids
    assert "m-off" not in catalog_ids
    assert "a" in catalog_ids


def test_load_config_requires_providers(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("server:\n  port: 1\n", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        load_config(path)
    assert "No providers" in str(ei.value) or "providers" in str(ei.value).lower()


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(Exception):
        load_config(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# auth / create_app
# ---------------------------------------------------------------------------


def _auth_config(api_key: str = "test-gateway-key") -> AppConfig:
    return AppConfig(
        auth=AuthConfig(api_key=api_key),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="up",
                enabled=True,
                models=["m-x"],
            )
        },
        aliases={"alias-x": "m-x"},
    )


def test_create_app_requires_gateway_key_on_v1_models():
    app = create_app(config=_auth_config("secret-gw"))
    with TestClient(app) as client:
        # 401 without token
        r = client.get("/v1/models")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["type"] == "AuthenticationError"

        # 200 with Bearer
        r2 = client.get("/v1/models", headers={"Authorization": "Bearer secret-gw"})
        assert r2.status_code == 200
        data = r2.json()
        assert data["object"] == "list"
        ids = {m["id"] for m in data["data"]}
        assert "m-x" in ids
        assert "alias-x" in ids

        # also accepted via x-api-key
        r3 = client.get("/v1/models", headers={"x-api-key": "secret-gw"})
        assert r3.status_code == 200

        # wrong key still 401
        r4 = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        assert r4.status_code == 401

        # health stays open
        r5 = client.get("/healthz")
        assert r5.status_code == 200


def test_create_app_no_gateway_key_open_v1():
    cfg = _auth_config("")
    app = create_app(config=cfg)
    with TestClient(app) as client:
        r = client.get("/v1/models")
        assert r.status_code == 200
        assert r.json()["object"] == "list"
