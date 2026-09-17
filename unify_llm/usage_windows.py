"""Time-window usage aggregation (lifetime / last 24h / last 7d).

Aggregates completed-request history dicts produced by ``Monitor.history`` /
``CompletedRequest.to_dict()``. Does not touch monitor begin/end/active logic.

Retention note: monitor history is an in-memory deque (default 500). Window
metrics derived from history are capped by that buffer; lifetime gateway totals
persisted in the stats store remain accurate and can be supplied as an override.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

WINDOW_SECONDS = {
    "last_24h": 24 * 3600,
    "last_7d": 7 * 24 * 3600,
}

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
    """Sum point *deductions* (negative deltas) in lifetime / 24h / 7d.

    Grants are ignored so windows show Points spent, matching usage UI copy.
    """
    now = time.time() if now is None else float(now)
    out = {
        "lifetime": {"points": 0.0},
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
        if age <= WINDOW_SECONDS["last_24h"]:
            out["last_24h"]["points"] += spent
        if age <= WINDOW_SECONDS["last_7d"]:
            out["last_7d"]["points"] += spent
    for bucket in out.values():
        bucket["points"] = round(bucket["points"], 6)
    return out


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
) -> dict[str, Any]:
    """Aggregate history items into lifetime + last_24h + last_7d windows.

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
    """
    now = time.time() if now is None else float(now)
    uid_filter = None if user_id is None else str(user_id)

    windows = {
        "lifetime": empty_window(),
        "last_24h": empty_window(),
        "last_7d": empty_window(),
    }
    scanned = 0
    for item in items:
        if uid_filter is not None and str(item.get("user_id") or "") != uid_filter:
            continue
        scanned += 1
        _add(windows["lifetime"], item)
        finished = float(item.get("finished_at") or 0.0)
        age = now - finished if finished > 0 else None
        if age is not None and age < 0:
            age = 0.0
        if age is not None and age <= WINDOW_SECONDS["last_24h"]:
            _add(windows["last_24h"], item)
        if age is not None and age <= WINDOW_SECONDS["last_7d"]:
            _add(windows["last_7d"], item)

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

    if points_log_rows is not None:
        by_points = aggregate_points_log(points_log_rows, now=now)
        windows["lifetime"]["points"] = by_points["lifetime"]["points"]
        windows["last_24h"]["points"] = by_points["last_24h"]["points"]
        windows["last_7d"]["points"] = by_points["last_7d"]["points"]
    else:
        for key in windows:
            windows[key]["points"] = estimate_points(
                windows[key],
                points_per_1k_prompt=points_per_1k_prompt,
                points_per_1k_completion=points_per_1k_completion,
            )

    for key in windows:
        _finalize(windows[key])

    return {
        "ok": True,
        "windows": windows,
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
        "points_source": "log" if points_log_rows is not None else "rate_estimate",
        "as_of": now,
    }
