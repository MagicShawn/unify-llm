# Unify LLM — Design

Local LLM gateway: one fixed port, multi-provider routing, dual protocol (OpenAI + Anthropic), concurrency monitoring, Web dashboard.

## Goals

1. Deploy locally; integrate multiple vendor APIs / base URLs.
2. Unified access: clients hit one port; switch models by name; proxy routes upstream.
3. Monitor concurrency per provider and what each in-flight request is doing.
4. First version extras: timeout, retry, optional fallback; OpenAI + Anthropic native paths; SSE streaming.

## Non-goals (v1)

- Auth / rate limiting for local clients
- Billing / token cost accounting
- Multi-node cluster mode
- Full OpenAI tool-calling rewrite across protocol gaps (pass-through when same protocol; best-effort convert otherwise)

## Style / stack

| Choice | Value |
|--------|--------|
| Runtime | Python 3.10+ |
| HTTP | FastAPI + Uvicorn |
| Upstream client | httpx (async, streaming) |
| Config | YAML + `${ENV}` expansion |
| Dashboard | Single HTML page, polls JSON status |
| Default port | `8787` |

## Directory layout

```
unify_llm/
├── DESIGN.md
├── README.md
├── requirements.txt
├── config.example.yaml
├── config.yaml                 # local, gitignored
├── main.py                     # uvicorn entry
├── unify_llm/
│   ├── __init__.py
│   ├── config.py               # load / validate YAML
│   ├── registry.py             # provider + model routing
│   ├── monitor.py              # concurrency / in-flight / history
│   ├── errors.py
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py             # UpstreamAdapter protocol
│   │   ├── openai_compat.py    # OpenAI-compatible upstream
│   │   └── anthropic.py        # Anthropic Messages upstream
│   ├── convert.py              # OpenAI ↔ Anthropic request/response
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── openai_api.py       # /v1/chat/completions, /v1/models
│   │   ├── anthropic_api.py    # /v1/messages
│   │   ├── status_api.py       # /api/status, /api/history
│   │   └── health.py           # /healthz
│   └── static/
│       └── dashboard.html
└── scripts/
    └── smoke.py                # local smoke checks (no real keys needed for health)
```

## Architecture

```mermaid
flowchart LR
  ClientA[OpenAI client] -->|POST /v1/chat/completions| GW[unify_llm :8787]
  ClientB[Anthropic client] -->|POST /v1/messages| GW
  Browser[Browser] -->|GET /dashboard| GW
  GW --> Router[Registry / model route]
  Router --> OA[OpenAI adapter]
  Router --> AN[Anthropic adapter]
  OA --> Up1[OpenAI / DeepSeek / Ollama ...]
  AN --> Up2[Anthropic API]
  GW --> Mon[Monitor]
  Mon --> Dash[/api/status + Dashboard]
```

Request path:

1. Client calls gateway with `model` (real name or alias).
2. `Registry.resolve(model)` → provider id + upstream base URL + adapter kind.
3. Monitor opens an in-flight record (provider, model, protocol, start time).
4. Adapter sends upstream (timeout + retries). Streams are forwarded incrementally.
5. Monitor closes record → completed log entry (latency, status, error).
6. Dashboard polls `/api/status` every 2s.

## Config schema

```yaml
server:
  host: "127.0.0.1"
  port: 8787
  dashboard: true

defaults:
  timeout_seconds: 120
  connect_timeout_seconds: 10
  max_retries: 2
  retry_backoff_seconds: 0.8
  # optional global fallback model name if primary fails hard
  fallback_model: null

providers:
  openai:
    type: openai                 # openai | anthropic
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    enabled: true
    timeout_seconds: null        # override defaults
    max_retries: null
    models:
      - gpt-4o
      - gpt-4o-mini

  deepseek:
    type: openai
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    models:
      - deepseek-chat
      - deepseek-reasoner

  anthropic:
    type: anthropic
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - claude-sonnet-4-20250514
      - claude-3-5-haiku-20241022

  ollama:
    type: openai
    base_url: "http://127.0.0.1:11434/v1"
    api_key: "ollama"
    models:
      - llama3.2

# Friendly names → real model id
aliases:
  smart: claude-sonnet-4-20250514
  fast: gpt-4o-mini
```

Env expansion: any string `${VAR}` is replaced from `os.environ` at load time. Missing key leaves empty string and is treated as invalid if provider enabled.

## Routing rules

- Exact model id wins over alias.
- First enabled provider that lists the model owns it (duplicate model ids: warn, first wins).
- `GET /v1/models` merges all enabled providers + aliases as OpenAI model objects.
- Cross-protocol: if client uses OpenAI path but model’s upstream is `anthropic`, request is converted; same for reverse.

## Protocol surface (exposed)

| Method | Path | Notes |
|--------|------|-------|
| POST | `/v1/chat/completions` | OpenAI chat; `stream: true` supported |
| GET | `/v1/models` | Combined catalog |
| POST | `/v1/messages` | Anthropic Messages; `stream: true` supported |
| GET | `/healthz` | Liveness |
| GET | `/api/status` | Providers, concurrency, in-flight |
| GET | `/api/history` | Recent completed requests |
| GET | `/dashboard` | Web UI |

## Concurrency model

Per provider:

- `active` — currently open upstream calls
- `total` / `errors` — cumulative this process
- `in_flight[]` — id, model, protocol, started_at, elapsed
- `recent[]` — ring buffer (default 50): id, model, status, latency_ms, error

Global: active count, uptime, last request summary.

## Retry policy

- Retry only on connect errors, timeouts, and HTTP `408/429/5xx`.
- Exponential-ish backoff: `backoff * attempt`.
- Never retry after a stream has already emitted body bytes to the client.
- Optional `fallback_model` after retries exhausted (same protocol path).

## Streaming

- OpenAI upstream → OpenAI client: raw SSE passthrough.
- Anthropic upstream → Anthropic client: raw SSE passthrough.
- Cross-protocol: convert non-stream JSON fully; stream conversion is best-effort text-delta only in v1.

## Security notes

- Default bind `127.0.0.1` only.
- API keys stay in config/env; never returned by `/api/status` (keys redacted).
- Dashboard is local-first; do not expose port publicly without auth (out of v1 scope).

## Extension points

- New vendor = new entry under `providers` with `type: openai` if OpenAI-compatible.
- True new protocol = new adapter under `adapters/` + convert rules.
- Auth later: FastAPI dependency on gateway routes.

## Success criteria

1. `python main.py` starts; `/healthz` 200.
2. With real keys in `config.yaml`, OpenAI client pointed at `http://127.0.0.1:8787/v1` can chat.
3. Anthropic client pointed at `http://127.0.0.1:8787` can call `/v1/messages`.
4. Dashboard shows live active counts and in-flight rows.
5. Killing network / bad key surfaces a clear error in history and dashboard.
