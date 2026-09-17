from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import PricingConfig


def estimate_cost_usd(
    pricing: PricingConfig | None,
    *,
    model: str = "",
    requested_model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> float:
    """Estimate USD cost from token counts using pricing defaults + model overrides.

    Returns 0.0 when no rates are configured or tokens are unknown.
    Tries the resolved model id first, then the client-requested id (aliases).
    """
    if pricing is None:
        return 0.0
    pt = max(int(prompt_tokens or 0), 0)
    ct = max(int(completion_tokens or 0), 0)
    if pt == 0 and ct == 0:
        return 0.0
    input_rate, output_rate = pricing.rate_for(model, requested_model)
    if input_rate <= 0.0 and output_rate <= 0.0:
        return 0.0
    return (pt * float(input_rate) + ct * float(output_rate)) / 1_000_000.0


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


class LogBuffer:
    """Tiny in-memory ring buffer for live dashboard logs (no files, no I/O)."""

    def __init__(self, size: int = 200):
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = 0

    def add(self, level: str, msg: str, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "t": time.time(),
                "level": level,
                "msg": msg,
            }
            if fields:
                item.update(fields)
            self._items.append(item)

    def tail(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


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
        if len(self._buf) > 512_000:
            self._buf = self._buf[-64_000:]

        # Process only complete lines; keep the incomplete tail for the next chunk.
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith("data:"):
                self._handle_data_line(line[5:].strip())
            elif line.startswith("event:"):
                continue

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
    user_agent: str = ""
    app: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    user_id: str = ""
    username: str = ""

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
    user_agent: str = ""
    app: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    user_id: str = ""
    username: str = ""

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
    cost_usd: float = 0.0
    last_error: str | None = None
    last_error_at: float | None = None
    last_health: dict[str, Any] | None = None
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
            "cost_usd": self.cost_usd,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_health": dict(self.last_health) if self.last_health else None,
            "in_flight": [r.to_dict(now) for r in self.in_flight.values()],
            "recent": [r.to_dict() for r in list(self.recent)[-25:]],
        }


class Monitor:
    """Process-wide concurrency, latency series, token totals, request history."""

    def __init__(
        self,
        history_size: int = 500,
        bucket_seconds: float = 5.0,
        series_buckets: int = 60,
        pricing: PricingConfig | None = None,
        store: Any | None = None,
        persist_interval_seconds: float = 2.0,
    ):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self._global_recent: deque[CompletedRequest] = deque(maxlen=history_size)
        self._providers: dict[str, ProviderStats] = {}
        self._global_active = 0
        self._global_total = 0
        self._global_errors = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0
        self._bucket_seconds = bucket_seconds
        self._series_buckets = series_buckets
        self._pricing = pricing
        self.logs = LogBuffer(size=200)
        self._store = store
        # Debounce SQLite totals writes. Persisting on every request blocks the
        # event loop under concurrency (WAL commit + lock held in end()).
        self._persist_interval = max(0.0, float(persist_interval_seconds or 0.0))
        self._last_persist = time.monotonic()
        self._dirty_totals = False
        # Watchdog: force-end in-flight rows older than this (client abort leaks).
        # 0 disables the sweep.
        self.stale_request_seconds = 900.0
        if store is not None:
            self._load_persisted()

    def _load_persisted(self) -> None:
        if self._store is None:
            return
        try:
            t = self._store.load_totals()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._global_total = int(t.get("requests") or 0)
            self._global_errors = int(t.get("errors") or 0)
            self._prompt_tokens = int(t.get("prompt_tokens") or 0)
            self._completion_tokens = int(t.get("completion_tokens") or 0)
            self._cost_usd = float(t.get("cost_usd") or 0.0)

    def _persist_locked(self, force: bool = False) -> None:
        if self._store is None:
            return
        now = time.monotonic()
        if not force and self._persist_interval > 0:
            if (now - self._last_persist) < self._persist_interval:
                self._dirty_totals = True
                return
        try:
            self._store.save_totals(
                {
                    "requests": self._global_total,
                    "errors": self._global_errors,
                    "prompt_tokens": self._prompt_tokens,
                    "completion_tokens": self._completion_tokens,
                    "cost_usd": self._cost_usd,
                }
            )
            self._last_persist = now
            self._dirty_totals = False
        except Exception:  # noqa: BLE001
            self._dirty_totals = True

    def flush(self) -> None:
        """Force any dirty lifetime totals to the stats store (shutdown / admin)."""
        with self._lock:
            if self._dirty_totals or self._store is not None:
                self._persist_locked(force=True)

    def clear_stats(self) -> None:
        """Zero lifetime totals (tokens/cost/errors) and per-provider counters."""
        with self._lock:
            self._global_total = 0
            self._global_errors = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._cost_usd = 0.0
            self._global_recent.clear()
            for s in self._providers.values():
                s.total = 0
                s.errors = 0
                s.prompt_tokens = 0
                s.completion_tokens = 0
                s.cost_usd = 0.0
                s.recent.clear()
                s.last_error = None
                s.last_error_at = None
            self._persist_locked(force=True)

    def clear_logs(self) -> None:
        self.logs.clear()

    def log(self, level: str, msg: str, **fields: Any) -> None:
        self.logs.add(level, msg, **fields)

    def set_pricing(self, pricing: PricingConfig | None) -> None:
        """Replace pricing rates used for cost estimates on subsequent requests."""
        self._pricing = pricing

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
        user_agent: str = "",
        app: str = "",
        headers: dict[str, str] | None = None,
        user_id: str = "",
        username: str = "",
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
            user_agent=user_agent or "",
            app=app or "",
            headers=dict(headers or {}),
            user_id=str(user_id or ""),
            username=str(username or ""),
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
        src = app or (user_agent[:48] if user_agent else client or "?")
        who = f" user={username}" if username else ""
        self.logs.add(
            "info",
            f"→ {path} {protocol} model={requested_model} via {provider_id} from {src}{who}",
            rid=rid,
            provider=provider_id,
            model=model,
            protocol=protocol,
            path=path,
            client=client,
            user_agent=user_agent,
            app=app,
            headers=headers or {},
            user_id=user_id,
            username=username,
        )
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
        status: str | None = None,
    ) -> None:
        """Complete an in-flight request and release its concurrency slot.

        ``status`` overrides the derived label (``ok`` / ``error``). Pass
        ``"cancelled"`` for client aborts so they do not inflate error
        counters. Idempotent: ending an already-completed request is a no-op
        (active must not be double-decremented when cleanup paths race).
        """
        now = time.time()
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                self._global_active = max(0, self._global_active - 1)
                return
            rec = stats.in_flight.pop(request_id, None)
            if rec is None:
                # Already ended — keep totals/active stable.
                return
            start = started_at if started_at is not None else rec.started_at
            latency_ms = int((now - start) * 1000)
            model = rec.model
            requested = rec.requested_model
            protocol = rec.protocol
            path = rec.path
            client = rec.client
            user_agent = rec.user_agent
            app = rec.app
            headers = rec.headers
            user_id = rec.user_id
            username = rec.username
            if status is None:
                status = "error" if error or http_status >= 400 else "ok"
            prompt_tokens = int(prompt_tokens or 0)
            completion_tokens = int(completion_tokens or 0)
            # Estimate outside the rec branch so stream + non-stream share one path.
            cost = estimate_cost_usd(
                self._pricing,
                model=model,
                requested_model=requested,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
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
                user_agent=user_agent,
                app=app,
                headers=headers,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                user_id=user_id,
                username=username,
            )
            stats.active = max(0, stats.active - 1)
            stats.total += 1
            stats.prompt_tokens += completed.prompt_tokens
            stats.completion_tokens += completed.completion_tokens
            stats.cost_usd += cost
            self._prompt_tokens += completed.prompt_tokens
            self._completion_tokens += completed.completion_tokens
            self._cost_usd += cost
            if status == "error":
                stats.errors += 1
                stats.last_error = error or f"HTTP {http_status}"
                stats.last_error_at = now
                self._global_errors += 1
            stats.recent.append(completed)
            self._global_recent.append(completed)
            self._global_active = max(0, self._global_active - 1)
            self._persist_locked()
        if status == "cancelled":
            level = "warn"
        elif status == "error":
            level = "error"
        else:
            level = "info"
        src = app or (user_agent[:48] if user_agent else client or "?")
        who = f" user={username}" if username else ""
        self.logs.add(
            level,
            (
                f"← {path} {http_status} {latency_ms}ms model={model or requested} "
                f"from {src}{who} tok={prompt_tokens}+{completion_tokens}"
                + (f" err={error}" if error else "")
            ),
            rid=request_id,
            provider=provider_id,
            model=model,
            status=status,
            http_status=http_status,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error,
            client=client,
            user_agent=user_agent,
            app=app,
            headers=headers,
            user_id=user_id,
            username=username,
        )

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
                    "cost_usd": 0.0,
                    "latencies": [],
                },
            )
            item["total"] += 1
            item["prompt_tokens"] += r.prompt_tokens
            item["completion_tokens"] += r.completion_tokens
            item["cost_usd"] += r.estimated_cost_usd
            if r.status == "ok":
                item["latencies"].append(r.latency_ms)
            elif r.status == "error":
                item["errors"] += 1
            # cancelled: neither latency sample nor error
        for item in out.values():
            lats = sorted(item.pop("latencies") or [])
            item["p50"] = round(_percentile(lats, 0.5), 1) if lats else None
            item["p95"] = round(_percentile(lats, 0.95), 1) if lats else None
            item["cost_usd"] = round(float(item.get("cost_usd") or 0.0), 12)
        return out

    def status(self) -> dict[str, Any]:
        # Self-heal: drop phantom in-flight rows (e.g. abort that never finalized).
        if self.stale_request_seconds > 0:
            self.sweep_stale()
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
                    "cost_usd": self._cost_usd,
                    "persisted": self._store is not None,
                },
                "latency": self._latency_series_locked(),
                "models": models,
                "providers": [p.snapshot() for p in self._providers.values()],
                "logs": self.logs.tail(80),
            }

    def active_count(self) -> int:
        """Cheap in-flight count for /api/limits (no history/enrichment)."""
        if self.stale_request_seconds > 0:
            self.sweep_stale()
        with self._lock:
            return self._global_active

    def sweep_stale(self, max_age_seconds: float | None = None) -> list[str]:
        """Force-end in-flight requests older than ``max_age_seconds``.

        Safety net when a client abort never finalized the stream generator
        (monitor.end skipped). Returns request ids that were swept.
        """
        limit = self.stale_request_seconds if max_age_seconds is None else float(max_age_seconds)
        if limit <= 0:
            return []
        cutoff = time.time() - limit
        victims: list[tuple[str, str, float]] = []
        with self._lock:
            for pid, stats in self._providers.items():
                for rid, rec in stats.in_flight.items():
                    if rec.started_at <= cutoff:
                        victims.append((pid, rid, rec.started_at))
        swept: list[str] = []
        for pid, rid, started in victims:
            self.end(
                rid,
                provider_id=pid,
                http_status=499,
                error=f"swept stale in-flight (> {int(limit)}s)",
                started_at=started,
                status="cancelled",
            )
            swept.append(rid)
        return swept

    def lifetime_totals(self) -> dict[str, Any]:
        """Cheap lifetime counter snapshot for usage-window aggregation."""
        with self._lock:
            return {
                "requests": self._global_total,
                "errors": self._global_errors,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._prompt_tokens + self._completion_tokens,
                "cost_usd": self._cost_usd,
                "persisted": self._store is not None,
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
                "cost_usd": stats.cost_usd,
                "last_error": stats.last_error,
                "last_error_at": stats.last_error_at,
                "last_health": dict(stats.last_health) if stats.last_health else None,
            }

    def set_health(self, provider_id: str, health: dict[str, Any]) -> None:
        """Store the latest lightweight probe result for a provider.

        Accepts {ok, status_code, latency_ms} (optional error class name).
        Never store raw response bodies or API keys.
        """
        record: dict[str, Any] = {
            "ok": bool(health.get("ok")),
            "status_code": int(health.get("status_code") or 0),
            "latency_ms": int(health.get("latency_ms") or 0),
            "checked_at": time.time(),
        }
        err = health.get("error")
        if isinstance(err, str) and err:
            record["error"] = err[:80]
        with self._lock:
            stats = self._providers.get(provider_id)
            if stats is None:
                return
            stats.last_health = record

    def history(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            items = list(self._global_recent)[-limit:]
            return {"count": len(items), "items": [r.to_dict() for r in items]}
