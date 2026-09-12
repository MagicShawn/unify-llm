from __future__ import annotations

"""Product-readiness concurrency bench for Unify LLM.

Measures RPS / latency percentiles / error rate across concurrency levels,
plus queue behaviour under max_concurrent + max_queue.

Always uses an ephemeral port and temp SQLite DBs. Never touches production
on :8787 and never reads config.yaml.

Examples:
  python scripts/bench_gateway.py
  python scripts/bench_gateway.py --scenarios open,user,queue --port 8799
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Dummy OpenAI-compatible upstream
# ---------------------------------------------------------------------------

_RESP_BODY = json.dumps(
    {
        "id": "chatcmpl-bench",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
).encode()


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    delay_ms: int,
) -> None:
    """HTTP/1.1 dummy with keep-alive (closer to real upstreams than Connection: close)."""
    try:
        while True:
            try:
                data = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            headers = data.decode("latin-1", errors="replace").lower()
            length = 0
            close = False
            for line in headers.split("\r\n"):
                if line.startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = 0
                if line == "connection: close":
                    close = True
            if length:
                await reader.readexactly(length)
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)
            conn = b"close" if close else b"keep-alive"
            header = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(_RESP_BODY)).encode() + b"\r\n"
                b"Connection: " + conn + b"\r\n\r\n"
            )
            writer.write(header + _RESP_BODY)
            await writer.drain()
            if close:
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def start_async_dummy(delay_ms: int) -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader, writer):
        await _handle_client(reader, writer, delay_ms)

    server = await asyncio.start_server(handler, "127.0.0.1", 0, backlog=512)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class Result:
    ok: bool
    latency_ms: float
    status: int
    error: str | None = None


@dataclass
class Report:
    label: str
    concurrency: int
    total: int
    ok: int
    err: int
    wall_s: float
    latencies_ms: list[float] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    peak_active: int = 0
    peak_queued: int = 0
    sample_errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        n = len(lat)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            i = min(n - 1, max(0, int(round(p * (n - 1)))))
            return round(lat[i], 1)

        return {
            "label": self.label,
            "concurrency": self.concurrency,
            "total": self.total,
            "ok": self.ok,
            "err": self.err,
            "err_rate": round(self.err / self.total, 4) if self.total else 0.0,
            "wall_s": round(self.wall_s, 3),
            "rps": round(self.ok / self.wall_s, 2) if self.wall_s > 0 else 0,
            "lat_ms": {
                "avg": round(statistics.fmean(lat), 1) if lat else 0,
                "p50": pct(0.50),
                "p95": pct(0.95),
                "p99": pct(0.99),
                "min": round(lat[0], 1) if lat else 0,
                "max": round(lat[-1], 1) if lat else 0,
            },
            "status_counts": dict(sorted(self.status_counts.items())),
            "peak_active": self.peak_active,
            "peak_queued": self.peak_queued,
            "sample_errors": self.sample_errors[:5],
        }


def fmt_report(s: dict[str, Any]) -> str:
    lat = s["lat_ms"]
    return (
        f"{s['label']:>22}  c={s['concurrency']:<3}  "
        f"ok={s['ok']}/{s['total']}  err={s['err']} ({s['err_rate']:.1%})  "
        f"rps={s['rps']:<8}  "
        f"p50={lat['p50']:<8} p95={lat['p95']:<8} p99={lat['p99']:<8}  "
        f"max={lat['max']:<8} "
        f"peakA={s['peak_active']} peakQ={s['peak_queued']}"
    )


# ---------------------------------------------------------------------------
# Status sampler (peak active / queued via /api/status)
# ---------------------------------------------------------------------------


class StatusSampler:
    def __init__(self, base: str, interval: float = 0.05):
        self.base = base.rstrip("/")
        self.interval = interval
        self.peak_active = 0
        self.peak_queued = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop = asyncio.Event()
        self.peak_active = 0
        self.peak_queued = 0
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                while not self._stop.is_set():
                    try:
                        r = await client.get(self.base + "/api/limits")
                        if r.status_code == 200:
                            st = r.json()
                            act = int(st.get("effective_active") or 0)
                            que = int(st.get("queued") or 0)
                            self.peak_active = max(self.peak_active, act)
                            self.peak_queued = max(self.peak_queued, que)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Wave runner
# ---------------------------------------------------------------------------


async def _one(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> Result:
    t0 = time.perf_counter()
    try:
        r = await client.post(url, json=payload, headers=headers)
        dt = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return Result(False, dt, r.status_code, r.text[:180])
        data = r.json()
        if "choices" not in data and "content" not in data:
            return Result(False, dt, r.status_code, "unexpected body shape")
        return Result(True, dt, r.status_code, None)
    except Exception as e:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        return Result(False, dt, 0, f"{type(e).__name__}: {e}")


async def run_wave(
    *,
    label: str,
    base: str,
    path: str,
    model: str,
    n: int,
    c: int,
    headers: dict[str, str],
    timeout: float,
) -> Report:
    url = base.rstrip("/") + path
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    sem = asyncio.Semaphore(c)
    results: list[Result] = []
    # Cheap peak sampler via /api/limits (no history/enrichment).
    sampler = StatusSampler(base)
    sampler.start()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(max_connections=max(c * 2, 32), max_keepalive_connections=max(c, 8)),
    ) as client:

        async def worker() -> None:
            async with sem:
                results.append(await _one(client, url, payload, headers))

        t0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(n)])
        wall = time.perf_counter() - t0

    await sampler.stop()

    status_counts: dict[int, int] = {}
    sample_errors: list[str] = []
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
        if not r.ok and r.error and len(sample_errors) < 5:
            sample_errors.append(f"{r.status}:{r.error[:80]}")

    ok_lats = [r.latency_ms for r in results if r.ok]
    return Report(
        label=label,
        concurrency=c,
        total=n,
        ok=sum(1 for r in results if r.ok),
        err=sum(1 for r in results if not r.ok),
        wall_s=wall,
        latencies_ms=ok_lats,
        status_counts=status_counts,
        peak_active=sampler.peak_active,
        peak_queued=sampler.peak_queued,
        sample_errors=sample_errors,
    )


# ---------------------------------------------------------------------------
# Scenario scaffolding
# ---------------------------------------------------------------------------


def _fresh_db_paths(tmpdir: Path, tag: str) -> tuple[Path, Path]:
    stats = tmpdir / f"stats_{tag}.db"
    users = tmpdir / f"users_{tag}.db"
    for p in (stats, users, Path(str(stats) + "-wal"), Path(str(users) + "-wal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return stats, users


def _make_user_key(users_path: Path, *, points_balance: int | None = None) -> str:
    from unify_llm.users import UserStore

    store = UserStore(users_path)
    try:
        user = store.create_user(
            name="bench-user",
            email=f"bench-{time.time_ns()}@local.test",
            password="bench-pass-1234",
            role="user",
            status="active",
        )
        if points_balance is not None:
            store.set_points_balance(user["id"], int(points_balance))
        meta = store.create_api_key(user["id"], name="bench-key")
        return str(meta["raw_key"])
    finally:
        store.close()


@dataclass
class ScenarioSpec:
    name: str
    label: str
    limits: dict[str, Any]
    use_user_key: bool
    mock_delay_ms: int = 0
    points_prompt: float = 0.0
    points_completion: float = 0.0


SCENARIOS: dict[str, ScenarioSpec] = {
    "open": ScenarioSpec(
        name="open",
        label="open:no-auth",
        limits={"requests_per_minute": 0, "max_concurrent": 0, "max_queue": 0},
        use_user_key=False,
        mock_delay_ms=0,
    ),
    "user": ScenarioSpec(
        name="user",
        label="user-key:auth",
        limits={"requests_per_minute": 0, "max_concurrent": 0, "max_queue": 0},
        use_user_key=True,
        mock_delay_ms=0,
    ),
    "queue": ScenarioSpec(
        name="queue",
        label="queue:maxc=2,q=8",
        limits={
            "requests_per_minute": 0,
            "max_concurrent": 2,
            "max_queue": 8,
            "queue_timeout_seconds": 15.0,
        },
        use_user_key=False,
        mock_delay_ms=50,
    ),
    "points": ScenarioSpec(
        name="points",
        label="user-key+points",
        limits={
            "requests_per_minute": 0,
            "max_concurrent": 0,
            "max_queue": 0,
            "points_per_1k_prompt": 1.0,
            "points_per_1k_completion": 1.0,
        },
        use_user_key=True,
        mock_delay_ms=0,
    ),
}


async def run_scenario(
    spec: ScenarioSpec,
    *,
    port: int,
    conc_levels: list[int],
    requests: int,
    timeout: float,
    tmpdir: Path,
    slo_p95: float,
) -> list[dict[str, Any]]:
    from unify_llm.app import create_app
    from unify_llm.config import AppConfig, LimitsConfig, ProviderConfig

    stats_db, users_db = _fresh_db_paths(tmpdir, spec.name)
    # Point AppState at temp DBs before create_app opens them.
    os.environ["UNIFY_STATS_DB"] = str(stats_db)
    os.environ["UNIFY_USERS_DB"] = str(users_db)

    api_key = ""
    if spec.use_user_key:
        charging = bool(
            float(spec.limits.get("points_per_1k_prompt") or 0) > 0
            or float(spec.limits.get("points_per_1k_completion") or 0) > 0
        )
        api_key = _make_user_key(users_db, points_balance=10_000 if charging else None)

    server, up_port = await start_async_dummy(delay_ms=spec.mock_delay_ms)
    cfg = AppConfig(
        limits=LimitsConfig(**spec.limits),
        providers={
            "bench": ProviderConfig(
                type="openai",
                base_url=f"http://127.0.0.1:{up_port}/v1",
                api_key="bench",
                models=["bench-model"],
                timeout_seconds=60,
                max_retries=0,
            )
        },
        defaults={
            "health_interval_seconds": 0,
            "max_retries": 0,
        },
    )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    import uvicorn

    app = create_app(config=cfg, users_db=users_db)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server_uvicorn = uvicorn.Server(config)
    task = asyncio.create_task(server_uvicorn.serve())
    for _ in range(100):
        if server_uvicorn.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError(f"uvicorn failed to start on port {port}")

    base = f"http://127.0.0.1:{port}"
    model = "bench-model"
    path = "/v1/chat/completions"

    print(
        f"\n=== scenario={spec.name} delay={spec.mock_delay_ms}ms "
        f"limits={spec.limits} auth={'user-key' if api_key else 'open'} ==="
    )
    print(
        f"{'scenario':>22}  {'c':<4}  {'ok/total':<12}  {'err':<14}  "
        f"{'rps':<8}  {'p50':<8} {'p95':<8} {'p99':<8}  {'max':<8} peaks"
    )
    print("-" * 120)

    reports: list[dict[str, Any]] = []
    try:
        for c in conc_levels:
            n = max(requests, c)
            # Queue scenario: fire more than max_concurrent+max_queue to exercise 429s.
            if spec.name == "queue":
                n = max(requests, c, 24)
            rep = await run_wave(
                label=spec.label,
                base=base,
                path=path,
                model=model,
                n=n,
                c=c,
                headers=headers,
                timeout=timeout,
            )
            s = rep.summary()
            reports.append(s)
            print(fmt_report(s))
            await asyncio.sleep(0.25)
    finally:
        server_uvicorn.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        # Restore nothing — next scenario overwrites env.

    print("-" * 120)
    for s in reports:
        if s["lat_ms"]["p95"] and s["lat_ms"]["p95"] > slo_p95 and s["err"] == 0:
            print(
                f"NOTE: {s['label']} c={s['concurrency']} "
                f"p95={s['lat_ms']['p95']}ms exceeds SLO {slo_p95}ms"
            )
        if s["sample_errors"]:
            print(f"  errors[{s['label']} c={s['concurrency']}]: {s['sample_errors']}")
    return reports


def parse_conc(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


async def _wait_http_ready(base: str, path: str = "/healthz", timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(base.rstrip("/") + path)
                if r.status_code < 500:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError(f"server not ready: {base}{path}")


_DUMMY_SCRIPT = r'''
import asyncio, sys
delay_ms = int(sys.argv[1])
RESP = b'{"id":"chatcmpl-bench","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'

async def handle(reader, writer):
    try:
        while True:
            try:
                data = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            headers = data.decode("latin-1", "replace").lower()
            length = 0
            close = False
            for line in headers.split("\r\n"):
                if line.startswith("content-length:"):
                    try: length = int(line.split(":",1)[1].strip())
                    except ValueError: length = 0
                if line == "connection: close":
                    close = True
            if length:
                await reader.readexactly(length)
            if delay_ms:
                await asyncio.sleep(delay_ms/1000.0)
            conn = b"close" if close else b"keep-alive"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+str(len(RESP)).encode()+b"\r\nConnection: "+conn+b"\r\n\r\n"+RESP)
            await writer.drain()
            if close:
                break
    except Exception:
        pass
    finally:
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 0, backlog=512)
    port = server.sockets[0].getsockname()[1]
    print(port, flush=True)
    await server.serve_forever()

asyncio.run(main())
'''


def _start_dummy_process(delay_ms: int) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _DUMMY_SCRIPT, str(delay_ms)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    if not line.isdigit():
        proc.kill()
        raise RuntimeError(f"dummy upstream failed to start: {line!r}")
    return proc, int(line)


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def run_scenario(
    spec: ScenarioSpec,
    *,
    port: int,
    conc_levels: list[int],
    requests: int,
    timeout: float,
    tmpdir: Path,
    slo_p95: float,
    separate_processes: bool = True,
) -> list[dict[str, Any]]:
    from unify_llm.config import LimitsConfig

    stats_db, users_db = _fresh_db_paths(tmpdir, spec.name)
    api_key = ""
    if spec.use_user_key:
        charging = bool(
            float(spec.limits.get("points_per_1k_prompt") or 0) > 0
            or float(spec.limits.get("points_per_1k_completion") or 0) > 0
        )
        api_key = _make_user_key(users_db, points_balance=10_000 if charging else None)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    dummy_proc: subprocess.Popen | None = None
    gw_proc: subprocess.Popen | None = None
    inproc_dummy = None
    server_uvicorn = None
    task = None

    if separate_processes:
        dummy_proc, up_port = _start_dummy_process(spec.mock_delay_ms)
        env = os.environ.copy()
        env["UNIFY_STATS_DB"] = str(stats_db)
        env["UNIFY_USERS_DB"] = str(users_db)
        env["UNIFY_BENCH_UPSTREAM"] = f"http://127.0.0.1:{up_port}/v1"
        env["UNIFY_BENCH_PORT"] = str(port)
        env["UNIFY_BENCH_LIMITS"] = json.dumps(spec.limits)
        gw_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve-only"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            await _wait_http_ready(base, "/healthz", timeout=15.0)
        except Exception:
            _stop_proc(gw_proc)
            _stop_proc(dummy_proc)
            raise
    else:
        from unify_llm.app import create_app
        from unify_llm.config import AppConfig, ProviderConfig
        import uvicorn

        os.environ["UNIFY_STATS_DB"] = str(stats_db)
        os.environ["UNIFY_USERS_DB"] = str(users_db)
        inproc_dummy, up_port = await start_async_dummy(delay_ms=spec.mock_delay_ms)
        cfg = AppConfig(
            limits=LimitsConfig(**spec.limits),
            providers={
                "bench": ProviderConfig(
                    type="openai",
                    base_url=f"http://127.0.0.1:{up_port}/v1",
                    api_key="bench",
                    models=["bench-model"],
                    timeout_seconds=60,
                    max_retries=0,
                )
            },
            defaults={"health_interval_seconds": 0, "max_retries": 0},
        )
        app = create_app(config=cfg, users_db=users_db)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server_uvicorn = uvicorn.Server(config)
        task = asyncio.create_task(server_uvicorn.serve())
        for _ in range(100):
            if server_uvicorn.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(f"uvicorn failed to start on port {port}")
        base = f"http://127.0.0.1:{port}"

    model = "bench-model"
    path = "/v1/chat/completions"
    mode = "subprocess" if separate_processes else "in-process"

    print(
        f"\n=== scenario={spec.name} mode={mode} delay={spec.mock_delay_ms}ms "
        f"limits={spec.limits} auth={'user-key' if api_key else 'open'} ==="
    )
    print(
        f"{'scenario':>22}  {'c':<4}  {'ok/total':<12}  {'err':<14}  "
        f"{'rps':<8}  {'p50':<8} {'p95':<8} {'p99':<8}  {'max':<8} peaks"
    )
    print("-" * 120)

    reports: list[dict[str, Any]] = []
    try:
        for c in conc_levels:
            n = max(requests, c)
            if spec.name == "queue":
                n = max(requests, c, 24)
            rep = await run_wave(
                label=spec.label,
                base=base,
                path=path,
                model=model,
                n=n,
                c=c,
                headers=headers,
                timeout=timeout,
            )
            s = rep.summary()
            reports.append(s)
            print(fmt_report(s))
            await asyncio.sleep(0.25)
    finally:
        if server_uvicorn is not None:
            server_uvicorn.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        if inproc_dummy is not None:
            inproc_dummy.close()
            try:
                await inproc_dummy.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        _stop_proc(gw_proc)
        _stop_proc(dummy_proc)

    print("-" * 120)
    for s in reports:
        if s["lat_ms"]["p95"] and s["lat_ms"]["p95"] > slo_p95 and s["err"] == 0:
            print(
                f"NOTE: {s['label']} c={s['concurrency']} "
                f"p95={s['lat_ms']['p95']}ms exceeds SLO {slo_p95}ms"
            )
        if s["sample_errors"]:
            print(f"  errors[{s['label']} c={s['concurrency']}]: {s['sample_errors']}")
    return reports


def serve_from_env() -> None:
    """Start the gateway for bench subprocess mode. Config comes from env, not config.yaml."""
    from unify_llm.app import create_app
    from unify_llm.config import AppConfig, LimitsConfig, ProviderConfig

    upstream = os.environ.get("UNIFY_BENCH_UPSTREAM") or ""
    if not upstream:
        raise SystemExit("UNIFY_BENCH_UPSTREAM is required for --serve-only")
    port = int(os.environ.get("UNIFY_BENCH_PORT") or "8799")
    limits = json.loads(os.environ.get("UNIFY_BENCH_LIMITS") or "{}")
    users_db = os.environ.get("UNIFY_USERS_DB") or ""
    cfg = AppConfig(
        limits=LimitsConfig(**limits),
        providers={
            "bench": ProviderConfig(
                type="openai",
                base_url=upstream,
                api_key="bench",
                models=["bench-model"],
                timeout_seconds=60,
                max_retries=0,
            )
        },
        defaults={"health_interval_seconds": 0, "max_retries": 0},
    )
    import uvicorn

    app = create_app(config=cfg, users_db=users_db or None)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


async def main_async(args: argparse.Namespace) -> None:
    conc_levels = parse_conc(args.concurrency)
    names = [x.strip() for x in args.scenarios.split(",") if x.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"Unknown scenarios: {unknown}. Choose from {sorted(SCENARIOS)}")

    tmpdir = Path(tempfile.mkdtemp(prefix="unify_bench_"))
    print(f"Unify LLM concurrency bench  python={sys.version.split()[0]}")
    print(f"tmpdir={tmpdir}")
    print(f"port={args.port} (ephemeral; production :8787 is never used)")
    print(
        f"scenarios={names}  concurrency={conc_levels}  "
        f"requests/level={args.requests}  "
        f"processes={'separate' if args.separate_processes else 'in-process'}"
    )

    all_reports: list[dict[str, Any]] = []
    for name in names:
        spec = SCENARIOS[name]
        reports = await run_scenario(
            spec,
            port=args.port,
            conc_levels=conc_levels,
            requests=args.requests,
            timeout=args.timeout,
            tmpdir=tmpdir,
            slo_p95=args.slo_p95,
            separate_processes=args.separate_processes,
        )
        for s in reports:
            s["scenario"] = name
        all_reports.extend(reports)
        await asyncio.sleep(0.4)

    print("\n### Markdown\n")
    print("| Scenario | Conc | OK/Total | Err% | RPS | p50 ms | p95 ms | p99 ms | Max ms | Peak Active | Peak Queued |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in all_reports:
        lat = s["lat_ms"]
        print(
            f"| {s['scenario']} | {s['concurrency']} | {s['ok']}/{s['total']} | "
            f"{s['err_rate']:.1%} | {s['rps']} | {lat['p50']} | {lat['p95']} | "
            f"{lat['p99']} | {lat['max']} | {s['peak_active']} | {s['peak_queued']} |"
        )

    out = tmpdir / "results.json"
    out.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
    print(f"\nJSON: {out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unify LLM product-readiness concurrency bench")
    p.add_argument("--scenarios", default="open,user,queue,points", help="comma list")
    p.add_argument("--port", type=int, default=8799, help="ephemeral proxy port")
    p.add_argument("--requests", type=int, default=64, help="target requests per concurrency level")
    p.add_argument("--concurrency", default="1,8,32,64")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--slo-p95", dest="slo_p95", type=float, default=2000.0)
    p.add_argument(
        "--in-process",
        dest="separate_processes",
        action="store_false",
        help="run dummy+gateway on the bench event loop (shared; lower absolute RPS)",
    )
    p.add_argument(
        "--serve-only",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: gateway worker for subprocess mode
    )
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.serve_only:
        serve_from_env()
    else:
        asyncio.run(main_async(args))
