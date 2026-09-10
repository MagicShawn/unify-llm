from __future__ import annotations

"""Load / latency bench for Central Proxy.

Modes:
  mock  — local dummy upstream (measures proxy overhead, safe high concurrency)
  live  — real upstream via proxy (uses API quota; keep N modest)

Examples:
  python scripts/bench.py mock --concurrency 1,5,10,20,50 --requests 200
  python scripts/bench.py live --model deepseek-flash --concurrency 1,3,5,10 --requests 15
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Dummy OpenAI-compatible upstream (for mock mode) — async, same loop
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
    try:
        # read headers
        data = await reader.readuntil(b"\r\n\r\n")
        headers = data.decode("latin-1", errors="replace").lower()
        length = 0
        for line in headers.split("\r\n"):
            if line.startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    length = 0
        if length:
            await reader.readexactly(length)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000.0)
        header = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(_RESP_BODY)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(header + _RESP_BODY)
        await writer.drain()
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
            "wall_s": round(self.wall_s, 2),
            "rps": round(self.ok / self.wall_s, 2) if self.wall_s > 0 else 0,
            "lat_ms": {
                "avg": round(statistics.fmean(lat), 1) if lat else 0,
                "p50": pct(0.50),
                "p90": pct(0.90),
                "p95": pct(0.95),
                "p99": pct(0.99),
                "min": round(lat[0], 1) if lat else 0,
                "max": round(lat[-1], 1) if lat else 0,
            },
        }


def fmt_report(s: dict[str, Any]) -> str:
    lat = s["lat_ms"]
    return (
        f"{s['label']:>18}  c={s['concurrency']:<3}  "
        f"ok={s['ok']}/{s['total']}  err={s['err']}  "
        f"rps={s['rps']:<7}  "
        f"p50={lat['p50']:<7} p95={lat['p95']:<7} p99={lat['p99']:<7} "
        f"avg={lat['avg']:<7} max={lat['max']}"
    )


# ---------------------------------------------------------------------------
# Runner
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
            return Result(False, dt, r.status_code, r.text[:200])
        # light parse to ensure body is usable
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
    protocol: str,
    max_tokens: int,
    timeout: float,
) -> Report:
    url = base.rstrip("/") + path
    if protocol == "openai":
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}
    else:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {"Content-Type": "application/json"}

    sem = asyncio.Semaphore(c)
    results: list[Result] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:

        async def worker() -> None:
            async with sem:
                results.append(await _one(client, url, payload, headers))

        t0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(n)])
        wall = time.perf_counter() - t0

    ok_lats = [r.latency_ms for r in results if r.ok]
    return Report(
        label=label,
        concurrency=c,
        total=n,
        ok=sum(1 for r in results if r.ok),
        err=sum(1 for r in results if not r.ok),
        wall_s=wall,
        latencies_ms=ok_lats,
    )


def parse_conc(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


async def main_async(args: argparse.Namespace) -> None:
    conc_levels = parse_conc(args.concurrency)
    server = None
    base = args.base.rstrip("/")

    if args.mode == "mock":
        server, port = await start_async_dummy(delay_ms=args.mock_delay_ms)
        # temporary config with dummy provider only
        from central_proxy.app import create_app
        from central_proxy.config import AppConfig, ProviderConfig

        cfg = AppConfig(
            providers={
                "bench": ProviderConfig(
                    type="openai",
                    base_url=f"http://127.0.0.1:{port}/v1",
                    api_key="bench",
                    models=["bench-model"],
                    timeout_seconds=60,
                    max_retries=0,
                )
            }
        )
        import uvicorn

        config = uvicorn.Config(create_app(config=cfg), host="127.0.0.1", port=args.port, log_level="warning")
        server_uvicorn = uvicorn.Server(config)
        task = asyncio.create_task(server_uvicorn.serve())
        # wait until started
        for _ in range(80):
            if server_uvicorn.started:
                break
            await asyncio.sleep(0.05)
        base = f"http://127.0.0.1:{args.port}"
        model = "bench-model"
        protocol = "openai"
        path = "/v1/chat/completions"
        label = f"mock:{model}"
    else:
        model = args.model
        protocol = args.protocol
        path = "/v1/chat/completions" if protocol == "openai" else "/v1/messages"
        label = f"live:{model}"
        task = None
        server_uvicorn = None

    print(f"mode={args.mode} base={base} model={model} protocol={protocol}")
    print(f"requests/level={args.requests} concurrency={conc_levels} max_tokens={args.max_tokens}")
    print("-" * 100)

    reports: list[dict[str, Any]] = []
    try:
        for c in conc_levels:
            n = max(args.requests, c) if args.mode == "mock" else max(args.requests, c)
            # live mode: scale down total with high concurrency to limit quota
            if args.mode == "live":
                n = min(n, max(c * 2, c))
            rep = await run_wave(
                label=label,
                base=base,
                path=path,
                model=model,
                n=n,
                c=c,
                protocol=protocol,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            s = rep.summary()
            reports.append(s)
            print(fmt_report(s))
            # status snapshot
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    st = (await client.get(base + "/api/status")).json()
                act = st.get("totals", {}).get("active")
                print(f"{'':>18}  after wave: active={act} total={st.get('totals',{}).get('requests')} err={st.get('totals',{}).get('errors')}")
            except Exception:
                pass
            await asyncio.sleep(0.3)
    finally:
        if server_uvicorn is not None:
            server_uvicorn.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    print("-" * 100)
    if reports:
        best = max(reports, key=lambda x: x["rps"])
        print(f"best_rps={best['rps']} at concurrency={best['concurrency']}")
        # latency budget hint
        for s in reports:
            p95 = s["lat_ms"]["p95"]
            if p95 and p95 > args.slo_p95:
                print(f"NOTE: {s['label']} c={s['concurrency']} p95={p95}ms exceeds SLO {args.slo_p95}ms")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Central Proxy bench")
    p.add_argument("mode", choices=["mock", "live"], help="mock=local dummy, live=real proxy")
    p.add_argument("--base", default="http://127.0.0.1:8787", help="proxy base url (live)")
    p.add_argument("--port", type=int, default=8799, help="ephemeral proxy port (mock)")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--protocol", choices=["openai", "anthropic"], default="openai")
    p.add_argument("--requests", type=int, default=200, help="total requests per concurrency level")
    p.add_argument("--concurrency", default="1,5,10,20,50")
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=1)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--mock-delay-ms", dest="mock_delay_ms", type=int, default=0)
    p.add_argument("--slo-p95", dest="slo_p95", type=float, default=2000.0)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    asyncio.run(main_async(args))
