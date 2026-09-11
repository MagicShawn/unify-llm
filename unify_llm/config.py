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


class AuthConfig(BaseModel):
    api_key: str = ""


class LimitsConfig(BaseModel):
    """Optional gateway rate limits for /v1/* only.

    Both fields: 0 (or omitted / None) disables the corresponding check.
    Env expansion is not required for these ints.
    """

    # Per client IP token bucket. 0 disables RPM limiting.
    requests_per_minute: int = 0
    # Global concurrent in-flight /v1/* requests. 0 disables the concurrency cap.
    max_concurrent: int = 0

    @field_validator("requests_per_minute", "max_concurrent", mode="before")
    @classmethod
    def _none_is_disabled(cls, v: Any) -> Any:
        if v is None:
            return 0
        return v

    @field_validator("requests_per_minute", "max_concurrent")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0 (0 disables)")
        return int(v)


class DefaultsConfig(BaseModel):
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.8
    fallback_model: str | None = None
    # Background provider probe interval. 0 disables background health tasks.
    health_interval_seconds: float = 60.0


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


class ModelPricing(BaseModel):
    """Per-model USD rates per 1M tokens. Used as a pricing.models override."""

    input: float = 0.0
    output: float = 0.0


class PricingConfig(BaseModel):
    """Optional token cost estimation.

    Defaults are 0 — never invent vendor prices. Set real rates in config.yaml:

        pricing:
          per_million_input: 0.27
          per_million_output: 1.10
          models:
            deepseek-flash:
              input: 0.27
              output: 1.10
    """

    # USD per 1M input/prompt tokens. 0 disables default-based estimates.
    per_million_input: float = 0.0
    # USD per 1M output/completion tokens.
    per_million_output: float = 0.0
    # Optional overrides keyed by model id or alias. Fully replaces defaults.
    models: dict[str, ModelPricing] = Field(default_factory=dict)

    def rate_for(self, *model_ids: str) -> tuple[float, float]:
        """Return (input, output) USD per 1M tokens for the first matching override."""
        for mid in model_ids:
            if not mid:
                continue
            ov = self.models.get(mid)
            if ov is not None:
                return float(ov.input or 0.0), float(ov.output or 0.0)
        return float(self.per_million_input or 0.0), float(self.per_million_output or 0.0)

    def has_any_rate(self) -> bool:
        if float(self.per_million_input or 0.0) > 0 or float(self.per_million_output or 0.0) > 0:
            return True
        return any(
            float(ov.input or 0.0) > 0 or float(ov.output or 0.0) > 0
            for ov in self.models.values()
        )


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    pricing: PricingConfig = Field(default_factory=PricingConfig)

    def gateway_api_key(self) -> str:
        """Effective gateway key: config auth.api_key, else UNIFY_GATEWAY_KEY."""
        return self.auth.api_key or os.environ.get("UNIFY_GATEWAY_KEY", "")

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


def set_provider_enabled(path: str | Path, provider_id: str, enabled: bool) -> None:
    """Toggle providers.<id>.enabled in the YAML file.

    Loads the file as-is (no env expansion) so secret key strings stay literal.
    Never logs file contents. Raises ConfigError if the file cannot be updated.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")
    providers = data.get("providers")
    if not isinstance(providers, dict) or provider_id not in providers:
        raise ConfigError(f"Provider '{provider_id}' not found in {path}")
    entry = providers[provider_id]
    if not isinstance(entry, dict):
        raise ConfigError(f"Provider '{provider_id}' must be a mapping")
    entry["enabled"] = bool(enabled)

    try:
        new_text = yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Cannot write {path}: {e}") from e


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
