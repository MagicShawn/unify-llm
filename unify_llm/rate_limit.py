from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

# Cap how many per-client buckets we keep so a noisy LAN cannot grow memory forever.
_MAX_TRACKED_CLIENTS = 1024
_BUCKET_IDLE_SECONDS = 120.0


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single admission check."""

    allowed: bool
    retry_after: int  # seconds; 0 when allowed
    reason: str | None  # "requests_per_minute" | "max_concurrent" | None
    remaining: int | None  # tokens left in the client bucket, if RPM is on
    active: int


class RateLimiter:
    """Optional gateway limits: per-client token bucket RPM + global concurrency.

    0 disables the corresponding check. Thread-safe; safe for Starlette middleware.
    Clock is injectable for tests (monotonic preferred).
    """

    def __init__(
        self,
        requests_per_minute: int = 0,
        max_concurrent: int = 0,
        max_queue: int = 0,
        queue_timeout_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ):
        self._lock = threading.Lock()
        self._clock: Callable[[], float] = clock or time.monotonic
        self.requests_per_minute = self._norm(requests_per_minute)
        self.max_concurrent = self._norm(max_concurrent)
        self.max_queue = self._norm(max_queue)
        self.queue_timeout_seconds = max(0.0, float(queue_timeout_seconds or 0.0))
        # client -> (tokens, last_updated)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._active = 0
        self._queued = 0

    @staticmethod
    def _norm(value: int | None) -> int:
        if value is None:
            return 0
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, iv)

    @property
    def enabled(self) -> bool:
        return (
            self.requests_per_minute > 0
            or self.max_concurrent > 0
            or self.max_queue > 0
        )

    @property
    def queued(self) -> int:
        with self._lock:
            return self._queued

    def update_config(
        self,
        requests_per_minute: int | None,
        max_concurrent: int | None,
        max_queue: int | None = None,
        queue_timeout_seconds: float | None = None,
    ) -> None:
        """Apply new limits (e.g. after config reload). Does not clear active count."""
        with self._lock:
            self.requests_per_minute = self._norm(requests_per_minute)
            self.max_concurrent = self._norm(max_concurrent)
            if max_queue is not None:
                self.max_queue = self._norm(max_queue)
            if queue_timeout_seconds is not None:
                self.queue_timeout_seconds = max(0.0, float(queue_timeout_seconds or 0.0))
            if self.requests_per_minute <= 0:
                self._buckets.clear()

    def _refill_locked(self, client: str, now: float) -> float:
        capacity = float(self.requests_per_minute)
        entry = self._buckets.get(client)
        if entry is None:
            tokens = capacity
        else:
            tokens, updated = entry
            elapsed = max(0.0, now - updated)
            tokens = min(capacity, tokens + elapsed * (capacity / 60.0))
        self._buckets[client] = (tokens, now)
        self._prune_locked(now)
        return tokens

    def _prune_locked(self, now: float) -> None:
        if len(self._buckets) <= _MAX_TRACKED_CLIENTS:
            return
        cutoff = now - _BUCKET_IDLE_SECONDS
        stale = [k for k, (_, updated) in self._buckets.items() if updated < cutoff]
        for k in stale:
            del self._buckets[k]
        # Still too many? Drop arbitrary extras (oldest updated first).
        if len(self._buckets) > _MAX_TRACKED_CLIENTS:
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(self._buckets) - _MAX_TRACKED_CLIENTS]:
                del self._buckets[k]

    def check(self, client: str) -> RateLimitDecision:
        """Admit one request (no queue). On success concurrency slot held until :meth:`release`."""
        client = client or "unknown"
        now = self._clock()
        with self._lock:
            if self.max_concurrent > 0 and self._active >= self.max_concurrent:
                return RateLimitDecision(
                    allowed=False,
                    retry_after=1,
                    reason="max_concurrent",
                    remaining=self._remaining_locked(now, client),
                    active=self._active,
                )

            remaining: int | None = None
            if self.requests_per_minute > 0:
                tokens = self._refill_locked(client, now)
                if tokens < 1.0:
                    need = 1.0 - tokens
                    retry = max(1, int(math.ceil(need * 60.0 / self.requests_per_minute)))
                    return RateLimitDecision(
                        allowed=False,
                        retry_after=retry,
                        reason="requests_per_minute",
                        remaining=0,
                        active=self._active,
                    )
                tokens -= 1.0
                self._buckets[client] = (tokens, now)
                remaining = int(math.floor(tokens))

            self._active += 1
            return RateLimitDecision(
                allowed=True,
                retry_after=0,
                reason=None,
                remaining=remaining,
                active=self._active,
            )

    def try_reserve_concurrency(self) -> bool:
        """Take one concurrency slot if under cap (or cap disabled)."""
        with self._lock:
            if self.max_concurrent <= 0 or self._active < self.max_concurrent:
                self._active += 1
                return True
            return False

    def enter_queue(self) -> bool:
        """Register a waiter. False if queue is full."""
        with self._lock:
            if self.max_queue > 0 and self._queued >= self.max_queue:
                return False
            self._queued += 1
            return True

    def leave_queue(self) -> None:
        with self._lock:
            if self._queued > 0:
                self._queued -= 1

    def check_rpm_only(self, client: str) -> RateLimitDecision:
        """Apply RPM (if on) without touching concurrency. Used after a queue slot opens."""
        client = client or "unknown"
        now = self._clock()
        with self._lock:
            remaining: int | None = None
            if self.requests_per_minute > 0:
                tokens = self._refill_locked(client, now)
                if tokens < 1.0:
                    need = 1.0 - tokens
                    retry = max(1, int(math.ceil(need * 60.0 / self.requests_per_minute)))
                    return RateLimitDecision(
                        allowed=False,
                        retry_after=retry,
                        reason="requests_per_minute",
                        remaining=0,
                        active=self._active,
                    )
                tokens -= 1.0
                self._buckets[client] = (tokens, now)
                remaining = int(math.floor(tokens))
            return RateLimitDecision(
                allowed=True,
                retry_after=0,
                reason=None,
                remaining=remaining,
                active=self._active,
            )

    def release(self) -> None:
        """Release a concurrency slot previously taken by a successful :meth:`check`."""
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def _remaining_locked(self, now: float, client: str) -> int | None:
        if self.requests_per_minute <= 0:
            return None
        capacity = float(self.requests_per_minute)
        entry = self._buckets.get(client)
        if entry is None:
            return self.requests_per_minute
        tokens, updated = entry
        elapsed = max(0.0, now - updated)
        tokens = min(capacity, tokens + elapsed * (capacity / 60.0))
        return int(math.floor(tokens))

    def status(self) -> dict[str, Any]:
        """Public snapshot for /api/status — no secrets."""
        with self._lock:
            now = self._clock()
            remaining: int | None = None
            if self.requests_per_minute > 0:
                if self._buckets:
                    remaining = min(
                        self._remaining_locked(now, c) or 0 for c in self._buckets
                    )
                else:
                    remaining = self.requests_per_minute
            return {
                "enabled": self.enabled,
                "requests_per_minute": self.requests_per_minute,
                "max_concurrent": self.max_concurrent,
                "max_queue": self.max_queue,
                "queue_timeout_seconds": self.queue_timeout_seconds,
                "active": self._active,
                "queued": self._queued,
                "remaining": remaining,
                "tracked_clients": len(self._buckets),
            }
