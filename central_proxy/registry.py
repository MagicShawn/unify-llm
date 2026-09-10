from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import ModelNotFoundError

if TYPE_CHECKING:
    from .config import AppConfig, ProviderConfig


@dataclass(frozen=True)
class ResolvedRoute:
    provider_id: str
    provider: "ProviderConfig"
    model: str  # real upstream model id (alias resolved)
    requested_model: str  # what the client sent
    is_alias: bool
    upstream_type: str  # openai | anthropic


class Registry:
    """Maps model names / aliases to providers."""

    def __init__(self, config: "AppConfig"):
        self.config = config
        self._by_model: dict[str, str] = {}  # model -> provider_id
        for pid, p in config.providers.items():
            if not p.enabled:
                continue
            for m in p.models:
                if m in self._by_model:
                    # first wins; keep a note for status UI
                    continue
                self._by_model[m] = pid

    def resolve(self, model: str) -> ResolvedRoute:
        raw = model
        is_alias = False
        target = model
        if model in self.config.aliases:
            is_alias = True
            target = self.config.aliases[model]

        pid = self._by_model.get(target)
        if pid is None:
            # allow alias to point at another alias once
            if is_alias and target in self.config.aliases:
                target2 = self.config.aliases[target]
                pid = self._by_model.get(target2)
                if pid is not None:
                    target = target2
            if pid is None:
                raise ModelNotFoundError(raw)

        provider = self.config.providers[pid]
        return ResolvedRoute(
            provider_id=pid,
            provider=provider,
            model=target,
            requested_model=raw,
            is_alias=is_alias,
            upstream_type=provider.type,
        )

    def list_models(self) -> list[dict]:
        return self.config.model_catalog()

    def provider_ids(self) -> list[str]:
        return [pid for pid, p in self.config.providers.items() if p.enabled]
