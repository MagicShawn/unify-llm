# Performance

Product-readiness concurrency measurements for Unify LLM on this machine.
All runs use an **ephemeral port** (default `8799`) and **temp SQLite DBs**.
Production `:8787` and `config.yaml` are never touched.

## How to reproduce

```powershell
# Python with project deps (fastapi, uvicorn, httpx, pydantic, PyYAML)
$env:MIMO_PYTHON   # or: python
$env:UNIFY_STATS_DB = "$env:TEMP\unify_bench_stats.db"
$env:UNIFY_USERS_DB = "$env:TEMP\unify_bench_users.db"

# Full matrix: open auth, user-key, queue limits, points
python scripts/bench_gateway.py --port 8799 --requests 48 --concurrency 1,8,32,64

# Legacy single-scenario mock bench (still works)
python scripts/bench.py mock --port 8799 --requests 40 --concurrency 1,8
```

`scripts/bench_gateway.py` defaults to **separate processes** for the dummy
upstream and the gateway (more realistic than sharing one event loop with the
load generator). Pass `--in-process` for the older same-loop mode.

## Environment

| Item | Value |
|------|--------|
| OS | Windows 10/11 (win32) |
| Python | 3.12.13 (MiMo bundled runtime) |
| Upstream | in-process/subprocess dummy OpenAI-compatible server, 0 ms delay (queue scenario: 50 ms) |
| DBs | temp SQLite (`UNIFY_STATS_DB`, `UNIFY_USERS_DB`) |
| Mode | single-process uvicorn (`127.0.0.1`) |

## Results (post-fix, subprocess dummy + gateway)

Date: 2026-02 (this machine). Requests/level = 48 (64 at c=64).

| Scenario | Conc | OK/Total | Err% | RPS | p50 ms | p95 ms | p99 ms | Max ms | Peak Active | Peak Queued |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| open (no auth) | 1 | 48/48 | 0.0% | 60.0 | 14.5 | 18.4 | 22.3 | 22.3 | 1 | 0 |
| open | 8 | 48/48 | 0.0% | 94.4 | 79.8 | 90.6 | 94.6 | 94.6 | 6 | 0 |
| open | 32 | 48/48 | 0.0% | 94.6 | 259.4 | 345.7 | 352.4 | 352.4 | 16 | 0 |
| open | 64 | 64/64 | 0.0% | 89.0 | 492.1 | 568.3 | 569.6 | 570.7 | 16 | 0 |
| user-key auth | 1 | 48/48 | 0.0% | 51.9 | 16.8 | 27.6 | 48.3 | 48.3 | 1 | 0 |
| user-key | 8 | 48/48 | 0.0% | 92.0 | 80.4 | 96.7 | 99.2 | 99.2 | 4 | 0 |
| user-key | 32 | 48/48 | 0.0% | 72.0 | 402.4 | 548.2 | 571.1 | 571.1 | 16 | 0 |
| user-key | 64 | 64/64 | 0.0% | 84.5 | 519.0 | 605.4 | 605.7 | 606.9 | 16 | 0 |
| queue maxc=2 q=8 (50 ms upstream) | 1 | 48/48 | 0.0% | 12.8 | 76.9 | 80.0 | 90.1 | 90.1 | 1 | 0 |
| queue | 8 | 48/48 | 0.0% | 21.8 | 93.6 | 1491.0 | 1794.7 | 1794.7 | 2 | 6 |
| queue | 32 | 12/48 | 75.0% | 15.0 | 515.2 | 750.3 | 750.4 | 750.4 | 2 | 8 |
| queue | 64 | 10/64 | 84.4% | 11.9 | 534.2 | 784.9 | 784.9 | 784.9 | 2 | 8 |
| user-key + points | 1 | 48/48 | 0.0% | 49.8 | 18.7 | 22.8 | 32.8 | 32.8 | 1 | 0 |
| user-key + points | 8 | 48/48 | 0.0% | 86.0 | 85.0 | 105.8 | 107.1 | 107.1 | 4 | 0 |
| user-key + points | 32 | 48/48 | 0.0% | 79.9 | 300.3 | 428.4 | 438.0 | 438.0 | 16 | 0 |
| user-key + points | 64 | 64/64 | 0.0% | 73.9 | 598.0 | 710.5 | 712.3 | 713.1 | 16 | 0 |

### Interpretation

- **Proxy overhead (c=1, 0 ms mock upstream):** ~15–17 ms p50, ~50–60 RPS.
  Absolute RPS is bounded by localhost + single-worker uvicorn + mock upstream,
  not by a LAN multi-user LLM workload (real upstreams are typically 100 ms+).
- **User-key auth** costs roughly 5–15% vs open mode (SQLite key lookup on `/v1/*`).
- **Points charging** adds another small step (pre-check + post-success deduct).
- **Queue (`max_concurrent=2`, `max_queue=8`)** behaves as specified:
  - Peak active caps at 2; peak queued caps at 8 (sampled via `/api/limits`).
  - Overflow is rejected with HTTP 429 `queue_full` (and `Retry-After`).
  - At c=8 with 50 ms upstream, expected wait ≈ queue_depth × service time;
    p95 ~1.5–1.8 s is consistent with that admission policy.
- **No 5xx / transport errors** on the unlimited scenarios through c=64.

### Baseline (before request-path SQLite fixes, same-process mock)

For comparison only — shared event loop with the load generator:

| Scenario | Conc | RPS | p50 ms |
|---|---:|---:|---:|
| open | 1 | 37.6 | 23.7 |
| open | 8 | 62.0 | 119.3 |
| open | 32 | 64.8 | 365.0 |
| open | 64 | 65.8 | 707.7 |

## Fixes applied for concurrency

1. **`unify_llm/monitor.py` — debounce stats SQLite writes**
   - Previously `Monitor.end()` called `save_totals()` (WAL commit) on every
     completed request while holding the monitor lock, blocking the event loop.
   - Now persists at most every `persist_interval_seconds` (default 2 s);
     `flush()` forces a write on shutdown (`AppState.shutdown`).

2. **`unify_llm/users.py` — throttle `touch_last_used`**
   - Auth middleware updated `last_used_at` (UPDATE + commit) on every `/v1/*`
     user-key request.
   - Now at most once per key per 60 s.

3. **`unify_llm/app.py` — cheap `/api/limits`**
   - Returns limiter + monitor in-flight counts without building the full
     `/api/status` history payload. Used by the bench sampler and useful for ops.

## Notes / caveats

- Numbers are **localhost mock** measurements. They characterize gateway
  admission, auth, points, and queue behaviour — not vendor API throughput.
- Peak Active under free concurrency is limited by the single-process event
  loop and the bench client; for real LAN load, prefer setting
  `limits.max_concurrent` / `max_queue` and watching `/api/limits`.
- Do not point the bench at production `:8787`. Always use an ephemeral port.
