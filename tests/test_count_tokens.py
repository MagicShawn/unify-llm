"""Regression: OpenCode Anthropic client calls /v1/messages/count_tokens."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unify_llm.app import create_app
from unify_llm.config import AppConfig, AuthConfig, ProviderConfig
from unify_llm.users import UserStore


@pytest.fixture()
def users_db(tmp_path: Path) -> Path:
    return tmp_path / "unify_users.db"


def _app(users_db: Path):
    cfg = AppConfig(
        auth=AuthConfig(api_key="master-key"),
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


def test_count_tokens_not_404(users_db: Path):
    app = _app(users_db)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.post(
            "/v1/messages/count_tokens",
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
