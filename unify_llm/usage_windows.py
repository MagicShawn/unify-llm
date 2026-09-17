"""Time-window usage aggregation (lifetime / last 1h / last 24h / last 7d).

Aggregates completed-request history dicts produced by ``Monitor.history`` /
``CompletedRequest.to_dict()``. Does not touch monitor begin/end/active logic.

Retention note: monitor history is an in-memory deque (default 500). Window
metrics and series curves derived from history are capped by that buffer
(best-effort); the 1h window fits well within the cap. Lifetime gateway totals
persisted in the stats store remain accurate and can be supplied as an override.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

WINDOW_SECONDS = {
    "last_1h": 3600,
    "last_24h": 24 * 3600,
    "last_7d": 7 * 24 * 3600,
}

# Right-aligned bins: last bucket ends at `now`.
# last_1h → 5-min bins × 12; last_24h → hourly × 24; last_7d → daily × 7.
SERIES_BINS: dict[str, tuple[float, int]] = {
    "last_1h": (300.0, 12),
    "last_24h": (3600.0, 24),
    "last_7d": (86400.0, 7),
}

SERIES_WINDOWS = tuple(SERIES_BINS.keys())

DEFAULT_POINTS_LIMIT = 200


def empty_window() -> dict[str, Any]:
    return {
        "requests": 0,
        "errors": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "points": 0.0,
    }


def empty_series_bucket(t: float = 0.0) -> dict[str, Any]:
    return {
        "t": float(t),
        "requests": 0,
        "errors": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "points": 0.0,
    }


def series_meta() -> dict[str, dict[str, Any]]:
    """Bin layout for each series window (for charts / clients)."""
    out: dict[str, dict[str, Any]] = {}
    for win, (bucket_seconds, buckets) in SERIES_BINS.items():
        out[win] = {
            "bucket_seconds": int(bucket_seconds),
            "buckets": buckets,
            "window_seconds": WINDOW_SECONDS[win],
        }
    return out


def _bucket_index(
    finished_at: float,
    *,
    now: float,
    window_seconds: float,
    bucket_seconds: float,
    num_buckets: int,
) -> int | None:
    """Map a timestamp into a right-aligned bucket index, or None if outside.

    Bucket i covers ``[window_start + i*bs, window_start + (i+1)*bs)``
    where ``window_start = now - window_seconds``. The final bucket also
    accepts timestamps at/after ``now`` (treated as age 0).
    """
    window_start = now - window_seconds
    rel = finished_at - window_start
    if rel < 0:
        # Allow exact lower edge (age == window_seconds) via ==0 after clamp.
        return None
    if rel >= window_seconds:
        # Age 0 (finished_at >= now) → last bucket.
        return num_buckets - 1
    idx = int(rel // bucket_seconds)
    if idx >= num_buckets:
        return num_buckets - 1
    return idx


def _add(window: dict[str, Any], item: Mapping[str, Any]) -> None:
    pt = int(item.get("prompt_tokens") or 0)
    ct = int(item.get("completion_tokens") or 0)
    window["requests"] += 1
    window["prompt_tokens"] += pt
    window["completion_tokens"] += ct
    window["total_tokens"] += pt + ct
    window["cost_usd"] += float(item.get("estimated_cost_usd") or 0.0)
    if item.get("status") != "ok":
        window["errors"] += 1


def _finalize(window: dict[str, Any]) -> dict[str, Any]:
    window["cost_usd"] = round(float(window.get("cost_usd") or 0.0), 12)
    window["points"] = round(float(window.get("points") or 0.0), 6)
    return window


def estimate_points(
    window: Mapping[str, Any],
    *,
    points_per_1k_prompt: float = 0.0,
    points_per_1k_completion: float = 0.0,
) -> float:
    """Rate-based points estimate from token counts (not actual deductions)."""
    rp = float(points_per_1k_prompt or 0.0)
    rc = float(points_per_1k_completion or 0.0)
    if rp <= 0.0 and rc <= 0.0:
        return 0.0
    return (
        (float(window.get("prompt_tokens") or 0) / 1000.0) * rp
        + (float(window.get("completion_tokens") or 0) / 1000.0) * rc
    )


def aggregate_points_log(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
) -> dict[str, dict[str, float]]:
    """Sum point *deductions* (negative deltas) in lifetime / 1h / 24h / 7d.

    Grants are ignored so windows show Points spent, matching usage UI copy.
    """
    now = time.time() if now is None else float(now)
    out = {
        "lifetime": {"points": 0.0},
        "last_1h": {"points": 0.0},
        "last_24h": {"points": 0.0},
        "last_7d": {"points": 0.0},
    }
    for row in rows:
        delta = float(row.get("delta") or 0.0)
        if delta >= 0:
            continue
        spent = abs(delta)
        created = float(row.get("created_at") or 0.0)
        out["lifetime"]["points"] += spent
        if created <= 0.0:
            continue
        age = now - created
        if age < 0:
            age = 0.0
        for key in ("last_1h", "last_24h", "last_7d"):
            if age <= WINDOW_SECONDS[key]:
                out[key]["points"] += spent
    for bucket in out.values():
        bucket["points"] = round(bucket["points"], 6)
    return out


def _empty_series_skeleton(now: float) -> dict[str, list[dict[str, Any]]]:
    series: dict[str, list[dict[str, Any]]] = {}
    for win, (bucket_seconds, num_buckets) in SERIES_BINS.items():
        window_seconds = WINDOW_SECONDS[win]
        window_start = now - window_seconds
        buckets = [
            empty_series_bucket(window_start + i * bucket_seconds)
            for i in range(num_buckets)
        ]
        series[win] = buckets
    return series


def _points_series_from_log(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: float,
) -> dict[str, list[float]]:
    """Per-bucket points spent from actual deductions (right-aligned bins)."""
    out: dict[str, list[float]] = {
        win: [0.0] * SERIES_BINS[win][1] for win in SERIES_BINS
    }
    for row in rows:
        delta = float(row.get("delta") or 0.0)
        if delta >= 0:
            continue
        created = float(row.get("created_at") or 0.0)
        if created <= 0.0:
            continue
        spent = abs(delta)
        for win, (bucket_seconds, num_buckets) in SERIES_BINS.items():
            idx = _bucket_index(
                created,
                now=now,
                window_seconds=WINDOW_SECONDS[win],
                bucket_seconds=bucket_seconds,
                num_buckets=num_buckets,
            )
            if idx is not None:
                out[win][idx] += spent
    return out


def aggregate_series(
    items: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    user_id: str | int | None = None,
    points_per_1k_prompt: float = 0.0,
    points_per_1k_completion: float = 0.0,
    points_log_rows: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build per-window request/token curves from completed-request history.

    Response shape::

        {
          "last_1h": [{t, requests, prompt_tokens, completion_tokens,
                       total_tokens, errors, cost_usd, points}, ...],
          "last_24h": [...24 hourly...],
          "last_7d": [...7 daily...],
        }

    Empty buckets are included so charts draw continuous series. Callers that
    need per-user privacy must pass ``user_id`` (or pre-filter items).
    """
    now = time.time() if now is None else float(now)
    uid_filter = None if user_id is None else str(user_id)
    series = _empty_series_skeleton(now)

    for item in items:
        if uid_filter is not None and str(item.get("user_id") or "") != uid_filter:
            continue
        finished = float(item.get("finished_at") or 0.0)
        if finished <= 0.0:
            continue
        for win, (bucket_seconds, num_buckets) in SERIES_BINS.items():
            idx = _bucket_index(
                finished,
                now=now,
                window_seconds=WINDOW_SECONDS[win],
                bucket_seconds=bucket_seconds,
                num_buckets=num_buckets,
            )
            if idx is None:
                continue
            _add(series[win][idx], item)

    # Points: prefer actual deductions when provided; else rate estimate.
    if points_log_rows is not None:
        log_series = _points_series_from_log(points_log_rows, now=now)
        for win in series:
            pts = log_series.get(win) or []
            for i, bucket in enumerate(series[win]):
                bucket["points"] = pts[i] if i < len(pts) else 0.0
    else:
        for win in series:
            for bucket in series[win]:
                bucket["points"] = estimate_points(
                    bucket,
                    points_per_1k_prompt=points_per_1k_prompt,
                    points_per_1k_completion=points_per_1k_completion,
                )

    for win in series:
        for bucket in series[win]:
            _finalize(bucket)
    return series


def aggregate_windows(
    items: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    user_id: str | int | None = None,
    points_per_1k_prompt: float = 0.0,
    points_per_1k_completion: float = 0.0,
    lifetime_totals: Mapping[str, Any] | None = None,
    points_log_rows: Iterable[Mapping[str, Any]] | None = None,
    history_cap: int | None = None,
    include_series: bool = True,
) -> dict[str, Any]:
    """Aggregate history items into lifetime + last_1h + last_24h + last_7d.

    Parameters
    ----------
    items:
        Completed request dicts (from Monitor history). Optionally filtered
        by ``user_id`` when provided.
    lifetime_totals:
        Optional authoritative lifetime counters (monitor/stats-store). When
        set, the lifetime bucket is replaced by these values (gateway-wide).
    points_log_rows:
        Optional actual Points movements. When provided, window ``points``
        come from the log instead of rate estimation.
    history_cap:
        Optional retention hint included in the payload (monitor deque size).
    include_series:
        When True (default), also emit time-bucketed ``series`` curves.
    """
    now = time.time() if now is None else float(now)
    uid_filter = None if user_id is None else str(user_id)

    windows = {
        "lifetime": empty_window(),
        "last_1h": empty_window(),
        "last_24h": empty_window(),
        "last_7d": empty_window(),
    }
    scanned_items: list[Mapping[str, Any]] = []
    scanned = 0
    for item in items:
        if uid_filter is not None and str(item.get("user_id") or "") != uid_filter:
            continue
        scanned += 1
        scanned_items.append(item)
        _add(windows["lifetime"], item)
        finished = float(item.get("finished_at") or 0.0)
        age = now - finished if finished > 0 else None
        if age is not None and age < 0:
            age = 0.0
        for key in ("last_1h", "last_24h", "last_7d"):
            if age is not None and age <= WINDOW_SECONDS[key]:
                _add(windows[key], item)

    if lifetime_totals:
        lt = windows["lifetime"]
        lt["requests"] = int(lifetime_totals.get("requests") or 0)
        lt["errors"] = int(lifetime_totals.get("errors") or 0)
        lt["prompt_tokens"] = int(lifetime_totals.get("prompt_tokens") or 0)
        lt["completion_tokens"] = int(lifetime_totals.get("completion_tokens") or 0)
        lt["total_tokens"] = int(lifetime_totals.get("total_tokens") or (
            lt["prompt_tokens"] + lt["completion_tokens"]
        ))
        lt["cost_usd"] = float(lifetime_totals.get("cost_usd") or 0.0)

    points_log_list: list[Mapping[str, Any]] | None = None
    if points_log_rows is not None:
        points_log_list = list(points_log_rows)
        by_points = aggregate_points_log(points_log_list, now=now)
        windows["lifetime"]["points"] = by_points["lifetime"]["points"]
        for key in ("last_1h", "last_24h", "last_7d"):
            windows[key]["points"] = by_points[key]["points"]
    else:
        for key in windows:
            windows[key]["points"] = estimate_points(
                windows[key],
                points_per_1k_prompt=points_per_1k_prompt,
                points_per_1k_completion=points_per_1k_completion,
            )

    for key in windows:
        _finalize(windows[key])

    series: dict[str, list[dict[str, Any]]] = {}
    if include_series:
        series = aggregate_series(
            scanned_items,
            now=now,
            points_per_1k_prompt=points_per_1k_prompt,
            points_per_1k_completion=points_per_1k_completion,
            points_log_rows=points_log_list,
        )

    return {
        "ok": True,
        "windows": windows,
        "series": series,
        "series_meta": series_meta(),
        "metrics": [
            "requests",
            "errors",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "points",
        ],
        "window_seconds": dict(WINDOW_SECONDS),
        "history_items_scanned": scanned,
        "history_cap": history_cap,
        "points_source": "log" if points_log_list is not None else "rate_estimate",
        "as_of": now,
        "series_note": (
            "History is an in-memory deque (best-effort); series buckets "
            "are derived from recent completed requests only."
        ),
    }
