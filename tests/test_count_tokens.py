"""Regression: OpenCode Anthropic client calls /v1/messages/count_tokens."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import ANTHROPIC_MESSAGES_PATHS, create_app
from unify_llm.config import AppConfig, AuthConfig, LimitsConfig, ProviderConfig
from unify_llm.users import UserStore


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


BASE_PATHS = ANTHROPIC_MESSAGES_PATHS


def _app(users_db: Path, *, rpm: int = 0):
    cfg = AppConfig(
        auth=AuthConfig(api_key="master-key"),
        limits=LimitsConfig(requests_per_minute=rpm),
        providers={
            "p1": ProviderConfig(
                type="openai",
                base_url="http://127.0.0.1:9",
                api_key="up",
                enabled=True,
                models=["glm-5.3-flash"],
            )
        },
    )
    return create_app(config=cfg, users_db=users_db)


@pytest.mark.parametrize("base_path", BASE_PATHS)
def test_count_tokens_not_404(users_db: Path, base_path: str):
    app = _app(users_db)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.post(
            base_path + "/count_tokens",
            json={
                "model": "glm-5.3-flash",
                "messages": [{"role": "user", "content": "hello world"}],
            },
            headers={"x-api-key": "master-key", "anthropic-version": "2023-06-01"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["input_tokens"] >= 1
        assert isinstance(body["input_tokens"], int)


def test_count_tokens_requires_auth_when_keys_exist(users_db: Path):
    store = UserStore(users_db)
    store.create_user("u", "u@test", password="pass-123456", status="active")
    store.create_api_key(store.get_user_by_email("u@test")["id"], "k")
    store.close()
    app = _app(users_db)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.post(
            "/v1/messages/count_tokens",
            json={"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401


@pytest.mark.parametrize("base_path", BASE_PATHS)
@pytest.mark.parametrize("suffix", ("", "/count_tokens"))
@pytest.mark.parametrize("headers", ({}, {"x-api-key": "invalid-key"}))
def test_message_aliases_require_valid_key(users_db, base_path, suffix, headers):
    with TestClient(_app(users_db), client=("192.0.2.1", 50000)) as client:
        response = client.post(base_path + suffix, json={}, headers=headers)
        assert response.status_code == 401, response.text


@pytest.mark.parametrize("base_path", BASE_PATHS)
def test_count_tokens_aliases_enforce_rate_limit(users_db, base_path):
    with TestClient(_app(users_db, rpm=1)) as client:
        kwargs = {"json": {"messages": []}, "headers": {"x-api-key": "master-key"}}
        assert client.post(base_path + "/count_tokens", **kwargs).status_code == 200
        response = client.post(base_path + "/count_tokens", **kwargs)
        assert response.status_code == 429, response.text
        assert int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize("base_path", BASE_PATHS)
def test_count_tokens_aliases_accept_user_key(users_db, base_path):
    store = UserStore(users_db)
    user = store.create_user("u", "u@test", status="active")
    key = store.create_api_key(user["id"], "k")["raw_key"]
    store.close()
    with TestClient(_app(users_db)) as client:
        response = client.post(
            base_path + "/count_tokens",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"x-api-key": key},
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("messages", (42, "invalid", {}, [42]))
def test_count_tokens_rejects_malformed_messages(users_db, messages):
    with TestClient(_app(users_db), raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/messages/count_tokens",
            json={"messages": messages},
            headers={"x-api-key": "master-key"},
        )
        assert response.status_code == 400, response.text


@pytest.mark.parametrize("extra", (
    {"tools": [{"name": "search", "description": "x" * 4000,
                "input_schema": {"type": "object"}}]},
    {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "tool_1", "name": "search",
         "input": {"query": "x" * 4000}}]}]},
))
def test_count_tokens_includes_tool_definitions_and_inputs(users_db, extra):
    with TestClient(_app(users_db)) as client:
        response = client.post(
            "/v1/messages/count_tokens",
            json={"messages": [], **extra},
            headers={"x-api-key": "master-key"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["input_tokens"] >= 1000


def test_missing_route_does_not_log_query_secrets(users_db):
    app = _app(users_db)
    with TestClient(app) as client:
        response = client.post(
            "/v1/missing?api_key=synthetic-secret",
            headers={"x-api-key": "master-key"},
        )
        assert response.status_code == 404
        logs = str(app.state.proxy.monitor.logs.tail(20))
        assert "404 POST /v1/missing" in logs
        assert "synthetic-secret" not in logs
