# js-proxy

Minimal Node.js reverse proxy with a global concurrency cap. Independent of the main `unify_llm` gateway — no npm dependencies, Node 18+ only.

## What it does

- Accepts local HTTP requests and forwards them to a single upstream.
- Caps concurrent in-flight proxied requests (default **3**, override via config or env).
- Returns `429` + `Retry-After` when at capacity.
- Exposes `GET /proxy/status` for live active/max counts (bypasses the limiter).

## Config

Edit `config.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8788,
  "upstream": "http://127.0.0.1:8787",
  "maxConcurrent": 3
}
```

Environment overrides (win over `config.json`):

| Variable | Meaning |
|----------|---------|
| `PROXY_HOST` | Bind address |
| `PROXY_PORT` | Listen port |
| `PROXY_UPSTREAM` | Upstream base URL |
| `PROXY_MAX_CONCURRENT` | Max in-flight requests (1+) |

## Run

```powershell
cd js-proxy
node proxy.js
# or
npm start
```

Point clients at `http://127.0.0.1:8788` instead of the upstream directly.

## Example

```powershell
# status
curl http://127.0.0.1:8788/proxy/status

# if upstream is the main gateway on 8787
curl http://127.0.0.1:8788/v1/models
```

## Notes

- Path and query are appended to the upstream path. `upstream: "http://host:8787"` + `/v1/chat/completions` → `http://host:8787/v1/chat/completions`.
- Streaming (SSE) is piped through as-is; the slot is released when the upstream response ends.
- Does not rewrite auth or bodies — pure transport forward + concurrency gate.
