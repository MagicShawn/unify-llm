"""Core unit tests for unify_llm: convert, registry, config, gateway auth.

Run from repo root:
    python -m pytest tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unify_llm.app import create_app, probe_provider
from unify_llm.config import (
    AppConfig,
    AuthConfig,
    DefaultsConfig,
    LimitsConfig,
    ModelPricing,
    PricingConfig,
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
from unify_llm.monitor import (
    Monitor,
    StreamUsageSniffer,
    estimate_cost_usd,
    extract_usage,
)
from unify_llm.rate_limit import RateLimiter
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


# ---------------------------------------------------------------------------
# background provider health
# ---------------------------------------------------------------------------


def test_defaults_health_interval_seconds():
    assert DefaultsConfig().health_interval_seconds == 60.0
    assert DefaultsConfig(health_interval_seconds=15).health_interval_seconds == 15.0
    assert DefaultsConfig(health_interval_seconds=0).health_interval_seconds == 0.0


def test_monitor_set_health_snapshot_and_totals():
    mon = Monitor()
    mon.register_provider(
        "p1",
        type="openai",
        base_url="http://127.0.0.1:9",
        enabled=True,
        models=["m"],
    )
    assert mon.provider_totals("p1")["last_health"] is None

    mon.set_health("p1", {"ok": True, "status_code": 200, "latency_ms": 12})
    totals = mon.provider_totals("p1")
    assert totals["last_health"]["ok"] is True
    assert totals["last_health"]["status_code"] == 200
    assert totals["last_health"]["latency_ms"] == 12
    assert totals["last_health"]["checked_at"] > 0

    snap = next(p for p in mon.status()["providers"] if p["id"] == "p1")
    assert snap["last_health"]["ok"] is True

    mon.set_health("p1", {"ok": False, "status_code": 0, "latency_ms": 5, "error": "ConnectError"})
    snap = next(p for p in mon.status()["providers"] if p["id"] == "p1")
    assert snap["last_health"]["ok"] is False
    assert snap["last_health"]["error"] == "ConnectError"

    # unknown provider is a no-op
    mon.set_health("nope", {"ok": True, "status_code": 200, "latency_ms": 1})


def test_probe_provider_openai_and_anthropic_url_shapes():
    """Probe builds openai /models vs anthropic base — verified via mock transport."""
    import asyncio

    class _FakeResp:
        status_code = 200

    class _FakeHttp:
        def __init__(self):
            self.calls = []

        async def get(self, url, headers=None, timeout=None):
            self.calls.append({"url": url, "headers": dict(headers or {})})
            return _FakeResp()

    async def _run():
        http = _FakeHttp()
        openai_p = ProviderConfig(
            type="openai",
            base_url="http://example.com/v1/",
            api_key="sk-openai-secret",
            models=["m"],
        )
        anthro_p = ProviderConfig(
            type="anthropic",
            base_url="http://example.com/anthropic/",
            api_key="sk-ant-secret",
            models=["c"],
        )
        r1 = await probe_provider(http, openai_p)  # type: ignore[arg-type]
        r2 = await probe_provider(http, anthro_p)  # type: ignore[arg-type]
        return r1, r2, http.calls

    r1, r2, calls = asyncio.run(_run())
    assert r1["ok"] is True and r1["status_code"] == 200
    assert r2["ok"] is True and r2["status_code"] == 200
    assert calls[0]["url"] == "http://example.com/v1/models"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-openai-secret"
    assert calls[1]["url"] == "http://example.com/anthropic"
    assert calls[1]["headers"]["x-api-key"] == "sk-ant-secret"
    assert calls[1]["headers"]["anthropic-version"] == "2023-06-01"


def test_api_providers_include_last_health_after_test():
    secret = "secret-key-do-not-leak"
    cfg = AppConfig(
        defaults=DefaultsConfig(health_interval_seconds=0),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key=secret,
                enabled=True,
                models=["m-x"],
            )
        },
    )
    app = create_app(config=cfg)
    with TestClient(app) as client:
        # background tasks disabled by interval=0
        assert app.state.proxy._health_tasks == []

        r = client.post("/api/providers/p1/test")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        # 0 on transport error; some environments intercept dead ports (e.g. 502)
        assert body["status_code"] >= 0
        assert secret not in r.text

        r2 = client.get("/api/providers")
        assert r2.status_code == 200
        p = r2.json()["providers"][0]
        assert p["last_health"]["ok"] is False
        assert "checked_at" in p["last_health"]
        assert secret not in r2.text

        r3 = client.get("/api/status")
        assert r3.status_code == 200
        snap = next(x for x in r3.json()["providers"] if x["id"] == "p1")
        assert snap["last_health"]["ok"] is False


def test_health_tasks_spawn_on_startup_and_cancel_on_shutdown():
    cfg = AppConfig(
        defaults=DefaultsConfig(health_interval_seconds=30),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="k",
                enabled=True,
                models=["m-x"],
            ),
            "off": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="k",
                enabled=False,
                models=["m-off"],
            ),
        },
    )
    app = create_app(config=cfg)
    with TestClient(app) as client:
        state = app.state.proxy
        assert len(state._health_tasks) == 1
        assert state._health_tasks[0].get_name() == "unify-health-p1"
        # client still usable while health tasks run
        assert client.get("/healthz").status_code == 200
    # lifespan shutdown cancels tasks
    assert app.state.proxy._health_tasks == []


def test_health_tasks_restart_on_admin_reload_and_patch(tmp_path: Path):
    import yaml as _yaml

    raw = {
        "defaults": {"health_interval_seconds": 30},
        "providers": {
            "dummy": {
                "type": "openai",
                "base_url": "http://127.0.0.1:9",
                "api_key": "k",
                "enabled": True,
                "models": ["m"],
            },
            "off": {
                "type": "openai",
                "base_url": "http://127.0.0.1:9",
                "api_key": "k",
                "enabled": False,
                "models": ["m-off"],
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    app = create_app(config_path=path)
    with TestClient(app) as client:
        state = app.state.proxy
        assert len(state._health_tasks) == 1

        # enable second provider → restart spawns both
        r = client.patch("/api/providers/off", json={"enabled": True})
        assert r.status_code == 200
        assert len(state._health_tasks) == 2

        # disable first → only "off" remains
        r = client.patch("/api/providers/dummy", json={"enabled": False})
        assert r.status_code == 200
        assert len(state._health_tasks) == 1

        # reload from disk (dummy re-enabled on disk)
        disk = _yaml.safe_load(path.read_text(encoding="utf-8"))
        disk["providers"]["dummy"]["enabled"] = True
        path.write_text(_yaml.safe_dump(disk, sort_keys=False), encoding="utf-8")
        r = client.post("/api/admin/reload")
        assert r.status_code == 200
        assert len(state._health_tasks) == 2


# ---------------------------------------------------------------------------
# pricing / cost estimation
# ---------------------------------------------------------------------------


def test_pricing_defaults_are_zero_not_invented():
    p = PricingConfig()
    assert p.per_million_input == 0.0
    assert p.per_million_output == 0.0
    assert p.models == {}
    assert p.has_any_rate() is False
    assert estimate_cost_usd(p, model="m", prompt_tokens=1000, completion_tokens=1000) == 0.0
    assert estimate_cost_usd(None, model="m", prompt_tokens=10, completion_tokens=10) == 0.0


def test_estimate_cost_global_rates():
    p = PricingConfig(per_million_input=1.0, per_million_output=2.0)
    # 1M input * $1 + 1M output * $2 = $3
    assert estimate_cost_usd(p, model="m", prompt_tokens=1_000_000, completion_tokens=1_000_000) == 3.0
    # 500k in * $0.5/M + 250k out * $2/M
    p2 = PricingConfig(per_million_input=0.5, per_million_output=2.0)
    assert estimate_cost_usd(p2, model="m", prompt_tokens=500_000, completion_tokens=250_000) == 0.75


def test_estimate_cost_model_override_replaces_default():
    p = PricingConfig(
        per_million_input=10.0,
        per_million_output=10.0,
        models={"cheap": ModelPricing(input=0.25, output=0.5)},
    )
    # override wins
    assert estimate_cost_usd(p, model="cheap", prompt_tokens=1_000_000, completion_tokens=0) == 0.25
    assert estimate_cost_usd(p, model="cheap", prompt_tokens=0, completion_tokens=1_000_000) == 0.5
    # non-overridden model uses defaults
    assert estimate_cost_usd(p, model="other", prompt_tokens=1_000_000, completion_tokens=0) == 10.0


def test_estimate_cost_requested_model_alias_fallback():
    p = PricingConfig(
        per_million_input=99.0,
        per_million_output=99.0,
        models={"fast": ModelPricing(input=1.0, output=1.0)},
    )
    # resolved model has no override; alias does
    cost = estimate_cost_usd(
        p,
        model="deepseek-flash",
        requested_model="fast",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    assert cost == 1.0


def test_estimate_cost_zero_tokens_or_zero_rates():
    p = PricingConfig(per_million_input=5.0, per_million_output=5.0)
    assert estimate_cost_usd(p, model="m", prompt_tokens=0, completion_tokens=0) == 0.0
    empty = PricingConfig()
    assert estimate_cost_usd(empty, model="m", prompt_tokens=1_000_000, completion_tokens=1_000_000) == 0.0


def test_monitor_accumulates_cost_global_provider_and_request():
    pricing = PricingConfig(per_million_input=1.0, per_million_output=2.0)
    mon = Monitor(pricing=pricing)
    mon.register_provider(
        "p1",
        type="openai",
        base_url="http://127.0.0.1:9",
        enabled=True,
        models=["m1"],
    )
    rid = mon.begin(
        provider_id="p1",
        model="m1",
        requested_model="m1",
        protocol="openai",
        path="/v1/chat/completions",
    )
    mon.end(
        rid,
        provider_id="p1",
        http_status=200,
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
    )
    # 1*1.0 + 0.5*2.0 = 2.0
    st = mon.status()
    assert st["totals"]["cost_usd"] == 2.0
    assert st["totals"]["prompt_tokens"] == 1_000_000
    assert st["totals"]["completion_tokens"] == 500_000
    prov = next(p for p in st["providers"] if p["id"] == "p1")
    assert prov["cost_usd"] == 2.0
    assert mon.provider_totals("p1")["cost_usd"] == 2.0
    assert len(st["models"]) == 1
    assert st["models"][0]["cost_usd"] == 2.0
    recent = mon.history(10)["items"]
    assert recent[-1]["estimated_cost_usd"] == 2.0


def test_monitor_cost_zero_without_pricing():
    mon = Monitor()
    mon.register_provider(
        "p1",
        type="openai",
        base_url="http://127.0.0.1:9",
        enabled=True,
        models=["m1"],
    )
    rid = mon.begin(
        provider_id="p1",
        model="m1",
        requested_model="m1",
        protocol="openai",
        path="/v1/chat/completions",
    )
    mon.end(rid, provider_id="p1", http_status=200, prompt_tokens=100, completion_tokens=50)
    st = mon.status()
    assert st["totals"]["cost_usd"] == 0.0
    assert st["models"][0]["cost_usd"] == 0.0
    assert mon.history(1)["items"][-1]["estimated_cost_usd"] == 0.0


def test_load_config_pricing_section(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "p": {
                        "type": "openai",
                        "base_url": "http://127.0.0.1:9",
                        "enabled": True,
                        "models": ["m-x"],
                    }
                },
                "pricing": {
                    "per_million_input": 0.25,
                    "per_million_output": 1.0,
                    "models": {
                        "m-x": {"input": 0.1, "output": 0.2},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.pricing.per_million_input == 0.25
    assert cfg.pricing.per_million_output == 1.0
    assert cfg.pricing.models["m-x"].input == 0.1
    assert cfg.pricing.models["m-x"].output == 0.2
    assert cfg.pricing.has_any_rate() is True


def test_api_status_includes_cost_usd():
    pricing = PricingConfig(per_million_input=2.0, per_million_output=0.0)
    cfg = AppConfig(
        defaults=DefaultsConfig(health_interval_seconds=0),
        pricing=pricing,
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="k",
                enabled=True,
                models=["m-x"],
            )
        },
    )
    app = create_app(config=cfg)
    with TestClient(app) as client:
        state = app.state.proxy
        rid = state.monitor.begin(
            provider_id="p1",
            model="m-x",
            requested_model="m-x",
            protocol="openai",
            path="/v1/chat/completions",
        )
        state.monitor.end(rid, provider_id="p1", http_status=200, prompt_tokens=500_000, completion_tokens=0)

        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["cost_usd"] == 1.0
        assert body["models"][0]["cost_usd"] == 1.0
        prov = next(p for p in body["providers"] if p["id"] == "p1")
        assert prov["cost_usd"] == 1.0

        r2 = client.get("/api/config")
        assert r2.status_code == 200
        assert r2.json()["pricing"]["per_million_input"] == 2.0


# ---------------------------------------------------------------------------
# rate limits (optional gateway limits)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _limits_app(rpm: int = 0, max_concurrent: int = 0) -> FastAPI:
    return create_app(
        config=AppConfig(
            defaults=DefaultsConfig(health_interval_seconds=0),
            limits=LimitsConfig(
                requests_per_minute=rpm,
                max_concurrent=max_concurrent,
            ),
            providers={
                "p1": ProviderConfig(
                    type="openai",
                    base_url="http://127.0.0.1:9",
                    api_key="k",
                    enabled=True,
                    models=["m-x"],
                )
            },
            aliases={"alias-x": "m-x"},
        )
    )


def test_limits_config_defaults_none_and_validation():
    assert LimitsConfig().requests_per_minute == 0
    assert LimitsConfig().max_concurrent == 0
    # None is treated as disabled
    assert LimitsConfig(requests_per_minute=None, max_concurrent=None).requests_per_minute == 0  # type: ignore[arg-type]
    assert LimitsConfig(requests_per_minute=30, max_concurrent=4).requests_per_minute == 30
    with pytest.raises(Exception):
        LimitsConfig(requests_per_minute=-1)
    with pytest.raises(Exception):
        LimitsConfig(max_concurrent=-5)


def test_rate_limiter_disabled_allows_unlimited():
    rl = RateLimiter(0, 0)
    assert rl.enabled is False
    for _ in range(50):
        d = rl.check("10.0.0.1")
        assert d.allowed is True
        rl.release()
    st = rl.status()
    assert st["enabled"] is False
    assert st["requests_per_minute"] == 0
    assert st["max_concurrent"] == 0


def test_rate_limiter_rpm_token_bucket_and_refill():
    clock = _FakeClock()
    rl = RateLimiter(requests_per_minute=5, max_concurrent=0, clock=clock)
    assert rl.enabled is True
    for i in range(5):
        d = rl.check("10.0.0.1")
        assert d.allowed is True, i
        assert d.remaining == 4 - i
        rl.release()

    denied = rl.check("10.0.0.1")
    assert denied.allowed is False
    assert denied.reason == "requests_per_minute"
    assert denied.retry_after >= 1
    assert denied.remaining == 0

    # different client still has its own bucket
    other = rl.check("10.0.0.2")
    assert other.allowed is True
    rl.release()

    # after 12s one token should have refilled at 5/min
    clock.t = 12.0
    again = rl.check("10.0.0.1")
    assert again.allowed is True
    rl.release()


def test_rate_limiter_max_concurrent():
    clock = _FakeClock()
    rl = RateLimiter(requests_per_minute=0, max_concurrent=2, clock=clock)
    assert rl.check("a").allowed is True
    assert rl.check("b").allowed is True
    blocked = rl.check("c")
    assert blocked.allowed is False
    assert blocked.reason == "max_concurrent"
    assert blocked.retry_after == 1
    assert blocked.active == 2
    rl.release()
    assert rl.check("c").allowed is True
    rl.release()
    rl.release()
    # extra release does not go negative
    rl.release()
    assert rl.status()["active"] == 0


def test_rate_limiter_status_and_update_config():
    clock = _FakeClock()
    rl = RateLimiter(requests_per_minute=10, max_concurrent=3, clock=clock)
    rl.check("10.0.0.1")
    st = rl.status()
    assert st["enabled"] is True
    assert st["requests_per_minute"] == 10
    assert st["max_concurrent"] == 3
    assert st["active"] == 1
    assert st["remaining"] == 9
    assert st["tracked_clients"] == 1
    assert "api_key" not in st

    rl.update_config(0, 0)
    assert rl.enabled is False
    assert rl.status()["requests_per_minute"] == 0
    # active count is preserved across config update
    assert rl.status()["active"] == 1
    rl.release()


def test_load_config_limits_section(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "p": {
                        "type": "openai",
                        "base_url": "http://127.0.0.1:9",
                        "enabled": True,
                        "models": ["m-x"],
                    }
                },
                "limits": {"requests_per_minute": 30, "max_concurrent": 2},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.limits.requests_per_minute == 30
    assert cfg.limits.max_concurrent == 2


def test_api_status_includes_limits_when_disabled():
    app = _limits_app(0, 0)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        lim = r.json()["limits"]
        assert lim["enabled"] is False
        assert lim["requests_per_minute"] == 0
        assert lim["max_concurrent"] == 0
        assert lim["active"] == 0
        assert lim["remaining"] is None

        r2 = client.get("/api/config")
        assert r2.json()["limits"] == {"requests_per_minute": 0, "max_concurrent": 0}


def test_v1_rate_limit_returns_429_with_retry_after():
    app = _limits_app(rpm=2, max_concurrent=0)
    with TestClient(app) as client:
        r1 = client.get("/v1/models")
        assert r1.status_code == 200
        r2 = client.get("/v1/models")
        assert r2.status_code == 200
        r3 = client.get("/v1/models")
        assert r3.status_code == 429, r3.text
        body = r3.json()
        assert body["error"]["type"] == "RateLimitError"
        assert "Rate limit exceeded" in body["error"]["message"]
        assert "Retry-After" in r3.headers
        assert int(r3.headers["Retry-After"]) >= 1

        # non-/v1 endpoints are not rate limited
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 200
        lim = client.get("/api/status").json()["limits"]
        assert lim["enabled"] is True
        assert lim["requests_per_minute"] == 2
        assert lim["remaining"] == 0


def test_v1_max_concurrent_returns_429():
    app = _limits_app(rpm=0, max_concurrent=1)
    with TestClient(app) as client:
        # occupy the single slot via the live limiter
        decision = app.state.proxy.limiter.check("manual")
        assert decision.allowed is True

        r = client.get("/v1/models")
        assert r.status_code == 429, r.text
        body = r.json()
        assert body["error"]["type"] == "RateLimitError"
        assert "concurrent" in body["error"]["message"].lower()
        assert "Retry-After" in r.headers

        # free the slot → request succeeds
        app.state.proxy.limiter.release()
        r2 = client.get("/v1/models")
        assert r2.status_code == 200


def test_rate_limit_releases_slot_after_request():
    app = _limits_app(rpm=0, max_concurrent=2)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/v1/models").status_code == 200
        # slots must not leak
        assert app.state.proxy.limiter.status()["active"] == 0


def test_admin_reload_updates_limits():
    import yaml as _yaml

    raw = {
        "defaults": {"health_interval_seconds": 0},
        "providers": {
            "dummy": {
                "type": "openai",
                "base_url": "http://127.0.0.1:9",
                "api_key": "k",
                "enabled": True,
                "models": ["m-x"],
            }
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        app = create_app(config_path=path)
        with TestClient(app) as client:
            assert client.get("/api/status").json()["limits"]["enabled"] is False

            disk = _yaml.safe_load(path.read_text(encoding="utf-8"))
            disk["limits"] = {"requests_per_minute": 1, "max_concurrent": 1}
            path.write_text(_yaml.safe_dump(disk, sort_keys=False), encoding="utf-8")
            r = client.post("/api/admin/reload")
            assert r.status_code == 200

            lim = client.get("/api/status").json()["limits"]
            assert lim["enabled"] is True
            assert lim["requests_per_minute"] == 1

            assert client.get("/v1/models").status_code == 200
            assert client.get("/v1/models").status_code == 429
