# Unify LLM

Local multi-provider LLM gateway. One fixed port routes OpenAI-compatible and Anthropic clients to DeepSeek, Kimi, GLM, and other upstreams, with concurrency monitoring and a web dashboard.

## Features

- Deploy locally and integrate multiple vendor Base URLs and API keys.
- Switch models by name (or aliases) from a single endpoint.
- Expose both protocol surfaces:
  - OpenAI: `POST /v1/chat/completions`, `GET /v1/models`
  - Anthropic: `POST /v1/messages`
- Route by model id; convert request and response bodies across protocols.
- Stream SSE end to end; recover token usage from stream chunks when available.
- Configure timeouts, retries, and an optional fallback model.
- Watch live concurrency, in-flight requests, latency (rolling p50/p95), and token totals on the dashboard.

## Requirements

| Item | Value |
|------|--------|
| Python | 3.10 or later |
| OS | Windows, Linux, or macOS |
| Network | Reach the upstream APIs you configure |

Install dependencies:

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
```

## Quick start

1. Copy the sample config and edit providers and keys:

   ```bash
   cp config.example.yaml config.yaml
   ```

   Prefer environment variables for secrets:

   ```powershell
   $env:DEEPSEEK_API_KEY = "..."
   $env:KIMI_API_KEY = "..."
   $env:GLM_API_KEY = "..."
   ```

2. Start the gateway:

   ```bash
   python main.py
   ```

   Default listen address: `http://127.0.0.1:8787`.

3. Open the dashboard:

   [http://127.0.0.1:8787/dashboard](http://127.0.0.1:8787/dashboard)

## Client setup

| Protocol | Base URL |
|----------|----------|
| OpenAI-compatible | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |

The gateway does not authenticate local clients by default. For LAN access, set `UNIFY_GATEWAY_KEY` (see [LAN access](#lan-access)); clients then send that key as `Authorization: Bearer` or `x-api-key`. SDKs still require a non-empty API key string when auth is off; pass a placeholder such as `local`.

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="local")
resp = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "hello"}],
)
print(resp.choices[0].message.content)
```

### Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8787", api_key="local")
msg = client.messages.create(
    model="deepseek-flash",
    max_tokens=256,
    messages=[{"role": "user", "content": "hello"}],
)
print(msg.content[0].text)
```

### curl

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}]}'
```

## Configuration

Primary file: `config.yaml` (gitignored). Template: `config.example.yaml`.

```yaml
server:
  host: "127.0.0.1"
  port: 8787

defaults:
  timeout_seconds: 120
  max_retries: 2
  fallback_model: null

providers:
  deepseek:
    type: openai                 # openai | anthropic
    base_url: "https://api.deepseek.com"
    api_key: "${DEEPSEEK_API_KEY}"
    enabled: true
    models:
      - deepseek-flash
      - deepseek-v4-pro

aliases:
  fast: deepseek-flash
```

| Field | Notes |
|-------|--------|
| `providers.*.type` | `openai` for OpenAI-compatible chat; `anthropic` for Messages API |
| `providers.*.base_url` | Vendor base URL (see comments in `config.example.yaml`) |
| `providers.*.api_key` | Supports `${ENV_VAR}` expansion |
| `providers.*.models` | Model ids this upstream owns |
| `aliases` | Friendly name → real model id |
| `defaults.fallback_model` | Optional model used after hard upstream failure |

List the same model id under only one enabled provider. The first match wins.

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness |
| GET | `/dashboard` | Web UI |
| GET | `/api/info` | Service name, version, host, auth_required |
| GET | `/api/status` | Concurrency, latency series, tokens, models |
| GET | `/api/history` | Recent completed requests |
| GET | `/api/config` | Redacted config view |
| GET | `/api/providers` | Providers (redacted) + monitor totals |
| PATCH | `/api/providers/{id}` | Toggle `enabled` (YAML write-back + in-memory) |
| POST | `/api/providers/{id}/test` | Upstream ping (`status_code`, `latency_ms`) |
| POST | `/api/admin/reload` | Hot-reload config from startup path |
| GET | `/v1/models` | Combined model catalog |
| POST | `/v1/chat/completions` | OpenAI chat (supports `stream`) |
| POST | `/v1/messages` | Anthropic messages (supports `stream`) |
| GET | `/docs` | Interactive OpenAPI docs |

## Monitoring

The dashboard shows:

- Active, total, and error counts
- Rolling p50 / p95 latency lines
- Prompt and completion token totals
- Per-model and per-provider breakdowns
- In-flight requests and recent history

Status JSON is available at `/api/status` for scripts or other tools.

Token totals for streaming requests are recovered from SSE `usage` fields when the upstream emits them. OpenAI-compatible streams request `stream_options.include_usage`. If a vendor omits usage, those counters stay at zero for that request.

## Develop and test

```bash
# Unit-style smoke test (no real API keys)
python scripts/smoke.py

# Proxy overhead bench against a local dummy upstream
python scripts/bench.py mock --requests 200 --concurrency 1,10,30

# Live bench (uses real quota; keep counts small)
python scripts/bench.py live --model deepseek-flash --protocol openai \
  --concurrency 1,4,8 --requests 8 --max-tokens 8
```

## Security

- Default bind address is `127.0.0.1` only.
- Do not expose port `8787` to the public internet without adding authentication.
- Keep API keys in environment variables or a gitignored `config.yaml`.
- `/api/config` redacts API keys; avoid sharing base URLs and model maps with untrusted parties.

## LAN access

Bind to all interfaces so other machines on the LAN can use the gateway:

```bash
python main.py --host 0.0.0.0
# or set server.host: "0.0.0.0" in config.yaml
```

Set a shared gateway key (recommended when bound to LAN):

```powershell
$env:UNIFY_GATEWAY_KEY = "change-me-long-random"
```

Or in `config.yaml`:

```yaml
auth:
  api_key: "${UNIFY_GATEWAY_KEY}"
```

When a key is set, `/v1/*` and `/api/*` require `Authorization: Bearer <key>` or `x-api-key: <key>`. `/healthz` and `/dashboard` stay open.

Other machines point at:

| Protocol | Base URL |
|----------|----------|
| OpenAI-compatible | `http://<host-ip>:8787/v1` |
| Anthropic Messages | `http://<host-ip>:8787` |
| Health / info | `http://<host-ip>:8787/healthz`, `/api/info` |

Allow inbound TCP 8787 in the host firewall for the LAN subnet only. Do not port-forward 8787 to the internet.

## Project layout

```
unify_llm/
  app.py              # FastAPI app and proxy routes
  config.py           # YAML load and validation
  registry.py         # Model → provider routing
  monitor.py          # Concurrency, tokens, latency series
  convert.py          # OpenAI ↔ Anthropic conversion
  adapters/           # Upstream HTTP adapters
  static/dashboard.html
scripts/
  smoke.py
  bench.py
main.py
config.example.yaml
DESIGN.md
DEPLOYMENT.md
```

## Documentation

| Document | Purpose |
|----------|---------|
| [README.md](./README.md) | Overview and quick start |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | Install, ops, systemd, troubleshooting |
| [DESIGN.md](./DESIGN.md) | Architecture and design decisions |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | How to contribute |
| [docs/style-guide.md](./docs/style-guide.md) | Docs and commit style (Google-aligned) |

## License

Add a license file before publishing this repository publicly.
