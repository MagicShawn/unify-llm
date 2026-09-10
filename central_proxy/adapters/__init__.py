from __future__ import annotations

from typing import Any

from ..config import DefaultsConfig, ProviderConfig
from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .openai_compat import OpenAICompatAdapter

__all__ = [
    "AnthropicAdapter",
    "BaseAdapter",
    "OpenAICompatAdapter",
    "create_adapter",
]


def create_adapter(
    provider_id: str,
    provider: ProviderConfig,
    defaults: DefaultsConfig,
    client,
) -> BaseAdapter:
    if provider.type == "anthropic":
        return AnthropicAdapter(provider_id, provider, defaults, client)
    return OpenAICompatAdapter(provider_id, provider, defaults, client)
