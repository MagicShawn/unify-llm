from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .errors import ConfigError

EnvPattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")

        return EnvPattern.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    dashboard: bool = True


class DefaultsConfig(BaseModel):
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.8
    fallback_model: str | None = None


class ProviderConfig(BaseModel):
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key: str = ""
    enabled: bool = True
    timeout_seconds: float | None = None
    max_retries: int | None = None
    models: list[str] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url) and (bool(self.api_key) or self.type == "openai")


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)

    def model_catalog(self) -> list[dict[str, Any]]:
        """OpenAI-style model list, including aliases."""
        items: dict[str, dict[str, Any]] = {}
        for pid, p in self.providers.items():
            if not p.enabled:
                continue
            for m in p.models:
                if m not in items:
                    items[m] = {
                        "id": m,
                        "object": "model",
                        "created": 0,
                        "owned_by": pid,
                    }
        for alias, target in self.aliases.items():
            if alias not in items:
                items[alias] = {
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": f"alias:{target}",
                }
        return list(items.values())


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config not found: {path}. Copy config.example.yaml to config.yaml and edit."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    data = expand_env(raw)
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")

    try:
        cfg = AppConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e

    if not cfg.providers:
        raise ConfigError("No providers configured")

    # Warn-level issues become hard errors only when a provider is enabled with empty base_url
    for name, p in cfg.providers.items():
        if p.enabled and not p.base_url:
            raise ConfigError(f"Provider '{name}' is enabled but base_url is empty")

    return cfg
