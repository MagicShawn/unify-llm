from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


def extract_usage(payload: Any) -> tuple[int, int]:
    """Return (prompt/input_tokens, completion/output_tokens) from OpenAI or Anthropic body."""
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return _usage_pair(usage)


def _usage_pair(usage: dict[str, Any]) -> tuple[int, int]:
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    if "input_tokens" in usage or "output_tokens" in usage:
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return 0, 0


class StreamUsageSniffer:
    """Parse SSE bytes in-flight and recover token usage without breaking passthrough."""

    def __init__(self, protocol: str):
        self.protocol = protocol  # client-facing: openai | anthropic
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._buf = ""

    def feed(self, chunk: bytes) -> None:
        try:
            self._buf += chunk.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return
        # keep buffer bounded
        if len(self._buf) > 512_000:
            self._buf = self._buf[-64_000:]

        while True:
            # SSE events separated by blank line; also handle line-wise data:
            idx = self._buf.find("\n\n")
            if idx < 0:
                # try line-based for data: ...\n
                break
            block, self._buf = self._buf[:idx], self._buf[idx + 2 :]
            self._handle_block(block)

        # also drain any complete data lines left without trailing blank yet
        lines = self._buf.split("\n")
        keep = []
        for line in lines:
            if line.startswith("data:"):
                self._handle_data_line(line[5:].strip())
            else:
                keep.append(line)
        # only keep trailing incomplete structure
        self._buf = "\n".join(keep[-5:])

    def _handle_block(self, block: str) -> None:
        for line in block.splitlines():
            if line.startswith("data:"):
                self._handle_data_line(line[5:].strip())

    def _handle_data_line(self, payload: str) -> None:
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        self._absorb(obj)

    def _absorb(self, obj: dict[str, Any]) -> None:
        if self.protocol == "anthropic":
            etype = obj.get("type")
            if etype == "message_start":
                msg = obj.get("message") or {}
                u = msg.get("usage") or {}
                pt, ct = _usage_pair(u)
                self.prompt_tokens = max(self.prompt_tokens, pt)
                self.completion_tokens = max(self.completion_tokens, ct)
            elif etype == "message_delta":
                u = obj.get("usage") or {}
                # message_delta may only carry output_tokens
                if "output_tokens" in u:
                    self.completion_tokens = max(self.completion_tokens, int(u.get("output_tokens") or 0))
                if "input_tokens" in u:
                    self.prompt_tokens = max(self.prompt_tokens, int(u.get("input_tokens") or 0))
                pt, ct = _usage_pair(u)
                if pt or ct:
                    self.prompt_tokens = max(self.prompt_tokens, pt)
                    self.completion_tokens = max(self.completion_tokens, ct)
            return

        # openai chat.completion.chunk
        u = obj.get("usage")
        if isinstance(u, dict):
            pt, ct = _usage_pair(u)
            if pt or ct:
                self.prompt_tokens = max(self.prompt_tokens, pt)
                self.completion_tokens = max(self.completion_tokens, ct)
        # some providers put usage on the choice
        for ch in obj.get("choices") or []:
            if isinstance(ch, dict) and isinstance(ch.get("usage"), dict):
                pt, ct = _usage_pair(ch["usage"])
                if pt or ct:
                    self.prompt_tokens = max(self.prompt_tokens, pt)
                    self.completion_tokens = max(self.completion_tokens, ct)

    def usage(self) -> tuple[int, int]:
        return self.prompt_tokens, self.completion_tokens


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 1:
        return float(sorted_vals[-1])
    idx = int(round(p * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


@dataclass
class InFlightRequest:
    id: str
    provider_id: str
    model: str
    requested_model: str
    protocol: str
    path: str
    started_at: float
    client: str = ""

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        d = asdict(self)
        d["elapsed_ms"] = int((now - self.started_at) * 1000)
        return d


@dataclass
class CompletedRequest:
    id: str
    provider_id: str
    model: str
    requested_model: str
    protocol: str
    path: str
    status: str
    http_status: int
    latency_ms: int
    error: str | None
    finished_at: float
    client: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderStats:
    id: str
    type: str
    base_url: str
    enabled: bool
    models: list[str]
    active: int = 0
    total: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    in_flight: dict[str, InFlightRequest] = field(default_factory=dict)
    recent: deque[CompletedRequest] = field(default_factory=lambda: deque(maxlen=80))

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "id": self.id,
            "type": self.type,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "models": self.models,
            "active": self.active,
            "total": self.total,
            "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "in_flight": [r.to_dict(now) for r in self.in_flight.values()],
            "recent": [r.to_dict() for r in list(self.recent)[-25:]],
        }


class Monitor:
    """Process-wide concurrency, latency series, token totals, request history."""

    def __init__(self, history_size: int = 500, bucket_seconds: float = 5.0, series_buckets: int = 60):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self._global_recent: deque[CompletedRequest] = deque(maxlen=history_size)
        self._providers: dict[str, ProviderStats] = {}
        self._global_active = 0
        self._global_total = 0
        self._global_errors = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._bucket_seconds = bucket_seconds
        self._series_buckets = series_buckets

    def register_provider(
        self,
        provider_id: str,
        *,
        type: str,
        base_url: str,
        enabled: bool,
        models: list[str],
    ) -> None:
        """Register or refresh provider metadata. Preserves counters if id already exists."""
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                self._providers[provider_id] = ProviderStats(
                    id=provider_id,
                    type=type,
                    base_url=base_url,
                    enabled=enabled,
                    models=models,
                )
                return
            stats.type = type
            stats.base_url = base_url
            stats.enabled = enabled
            stats.models = models

    def begin(
        self,
        *,
        provider_id: str,
        model: str,
        requested_model: str,
        protocol: str,
        path: str,
        client: str = "",
    ) -> str:
        rid = uuid.uuid4().hex[:12]
        rec = InFlightRequest(
            id=rid,
            provider_id=provider_id,
            model=model,
            requested_model=requested_model,
            protocol=protocol,
            path=path,
            started_at=time.time(),
            client=client,
        )
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                stats = ProviderStats(
                    id=provider_id,
                    type="unknown",
                    base_url="",
                    enabled=True,
                    models=[],
                )
                self._providers[provider_id] = stats
            stats.active += 1
            stats.in_flight[rid] = rec
            self._global_active += 1
            self._global_total += 1
        return rid

    def end(
        self,
        request_id: str,
        *,
        provider_id: str,
        http_status: int,
        error: str | None = None,
        started_at: float | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        now = time.time()
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                self._global_active = max(0, self._global_active - 1)
                return
            rec = stats.in_flight.pop(request_id, None)
            latency_ms = 0
            if rec is not None:
                start = started_at if started_at is not None else rec.started_at
                latency_ms = int((now - start) * 1000)
                model = rec.model
                requested = rec.requested_model
                protocol = rec.protocol
                path = rec.path
                client = rec.client
            else:
                model = requested = protocol = path = client = ""
            status = "error" if error or http_status >= 400 else "ok"
            completed = CompletedRequest(
                id=request_id,
                provider_id=provider_id,
                model=model,
                requested_model=requested,
                protocol=protocol,
                path=path,
                status=status,
                http_status=http_status,
                latency_ms=latency_ms,
                error=error,
                finished_at=now,
                client=client,
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
            )
            stats.active = max(0, stats.active - 1)
            stats.total += 1
            stats.prompt_tokens += completed.prompt_tokens
            stats.completion_tokens += completed.completion_tokens
            self._prompt_tokens += completed.prompt_tokens
            self._completion_tokens += completed.completion_tokens
            if status == "error":
                stats.errors += 1
                stats.last_error = error or f"HTTP {http_status}"
                stats.last_error_at = now
                self._global_errors += 1
            stats.recent.append(completed)
            self._global_recent.append(completed)
            self._global_active = max(0, self._global_active - 1)

    def _latency_series_locked(self) -> dict[str, Any]:
        """Last-N successful requests → continuous rolling p50/p95 series (no sparse nulls)."""
        oks = [r for r in self._global_recent if r.status == "ok"]
        oks.sort(key=lambda r: r.finished_at)
        window = 10  # rolling window size in requests
        points: list[dict[str, Any]] = []
        series: list[dict[str, Any]] = []
        for i, r in enumerate(oks):
            lat = float(r.latency_ms)
            points.append(
                {
                    "t": r.finished_at,
                    "ms": lat,
                    "model": r.model or r.requested_model,
                    "provider": r.provider_id,
                }
            )
            lo = max(0, i - window + 1)
            vals = sorted(float(x.latency_ms) for x in oks[lo : i + 1])
            series.append(
                {
                    "t": r.finished_at,
                    "ms": lat,
                    "p50": round(_percentile(vals, 0.50), 1),
                    "p95": round(_percentile(vals, 0.95), 1),
                    "avg": round(sum(vals) / len(vals), 1),
                    "count": len(vals),
                }
            )
        # keep chart payload small
        max_pts = max(self._series_buckets, 20)
        if len(points) > max_pts:
            points = points[-max_pts:]
            series = series[-max_pts:]
        return {
            "mode": "rolling",
            "window": window,
            "bucket_seconds": self._bucket_seconds,
            "points": points,
            "series": series,
        }

    def _model_breakdown_locked(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self._global_recent:
            key = r.model or r.requested_model or "unknown"
            item = out.setdefault(
                key,
                {
                    "model": key,
                    "provider": r.provider_id,
                    "total": 0,
                    "errors": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "latencies": [],
                },
            )
            item["total"] += 1
            item["prompt_tokens"] += r.prompt_tokens
            item["completion_tokens"] += r.completion_tokens
            if r.status == "ok":
                item["latencies"].append(r.latency_ms)
            else:
                item["errors"] += 1
        for item in out.values():
            lats = sorted(item.pop("latencies") or [])
            item["p50"] = round(_percentile(lats, 0.5), 1) if lats else None
            item["p95"] = round(_percentile(lats, 0.95), 1) if lats else None
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            models = list(self._model_breakdown_locked().values())
            models.sort(key=lambda m: m["total"], reverse=True)
            return {
                "ok": True,
                "version": __import__("unify_llm").__version__,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "totals": {
                    "active": self._global_active,
                    "requests": self._global_total,
                    "errors": self._global_errors,
                    "prompt_tokens": self._prompt_tokens,
                    "completion_tokens": self._completion_tokens,
                    "total_tokens": self._prompt_tokens + self._completion_tokens,
                },
                "latency": self._latency_series_locked(),
                "models": models,
                "providers": [p.snapshot() for p in self._providers.values()],
            }

    def provider_totals(self, provider_id: str) -> dict[str, Any] | None:
        """Counter snapshot for one provider, or None if unknown."""
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                return None
            return {
                "active": stats.active,
                "total": stats.total,
                "errors": stats.errors,
                "prompt_tokens": stats.prompt_tokens,
                "completion_tokens": stats.completion_tokens,
                "last_error": stats.last_error,
                "last_error_at": stats.last_error_at,
            }

    def history(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            items = list(self._global_recent)[-limit:]
            return {"count": len(items), "items": [r.to_dict() for r in items]}
