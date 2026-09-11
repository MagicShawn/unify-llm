# LAN multi-machine access

Share one Unify LLM host with other machines on a private network. Clients point at the host LAN IP instead of `127.0.0.1`.

For install and general ops, see [DEPLOYMENT.md](../DEPLOYMENT.md). This page covers LAN-only setup.

## Host: bind and firewall

1. Bind all interfaces:

   ```bash
   python main.py --host 0.0.0.0
   # or set server.host: "0.0.0.0" in config.yaml
   ```

2. Allow inbound TCP `8787` for the LAN subnet only. Do not port-forward to the internet.

   Windows (PowerShell, admin):

   ```powershell
   New-NetFirewallRule -DisplayName "Unify LLM LAN" -Direction Inbound `
     -Protocol TCP -LocalPort 8787 -RemoteAddress 192.168.0.0/16 -Action Allow
   ```

   macOS (if the local firewall is on): allow incoming connections for Python, or add a rule for port `8787` limited to your LAN.

   Linux (nftables/ufw example):

   ```bash
   sudo ufw allow from 192.168.0.0/16 to any port 8787 proto tcp
   ```

3. Confirm the gateway is up:

   ```bash
   curl http://127.0.0.1:8787/healthz
   ```

## Host: gateway key

When bound to `0.0.0.0`, set a shared key so other machines cannot spend your upstream quota anonymously:

```powershell
$env:UNIFY_GATEWAY_KEY = "change-me-long-random"
```

```bash
export UNIFY_GATEWAY_KEY="change-me-long-random"
```

Or in `config.yaml`:

```yaml
auth:
  api_key: "${UNIFY_GATEWAY_KEY}"
```

When a key is set, `/v1/*` and `/api/*` require `Authorization: Bearer <key>` or `x-api-key: <key>`. `/healthz` and `/dashboard` stay open.

## Discover host URLs

On the host machine:

```bash
python scripts/print_lan_urls.py
```

The script lists local IPv4 addresses, probes `127.0.0.1:8787/healthz`, and prints OpenAI / Anthropic env snippets with a placeholder key.

Other machines use:

| Protocol | Base URL |
|----------|----------|
| OpenAI-compatible | `http://<host-ip>:8787/v1` |
| Anthropic Messages | `http://<host-ip>:8787` |
| Health / info | `http://<host-ip>:8787/healthz`, `/api/info` |

## Client env vars

Replace `<host-ip>` and the placeholder with the real LAN IP and `UNIFY_GATEWAY_KEY`.

### Windows (PowerShell)

```powershell
$env:OPENAI_BASE_URL = "http://<host-ip>:8787/v1"
$env:OPENAI_API_KEY = "<UNIFY_GATEWAY_KEY>"
$env:ANTHROPIC_BASE_URL = "http://<host-ip>:8787"
$env:ANTHROPIC_API_KEY = "<UNIFY_GATEWAY_KEY>"
```

Persist for your user (new terminal required after `setx`):

```powershell
setx OPENAI_BASE_URL "http://<host-ip>:8787/v1"
setx OPENAI_API_KEY "<UNIFY_GATEWAY_KEY>"
setx ANTHROPIC_BASE_URL "http://<host-ip>:8787"
setx ANTHROPIC_API_KEY "<UNIFY_GATEWAY_KEY>"
```

### macOS / Linux

```bash
export OPENAI_BASE_URL="http://<host-ip>:8787/v1"
export OPENAI_API_KEY="<UNIFY_GATEWAY_KEY>"
export ANTHROPIC_BASE_URL="http://<host-ip>:8787"
export ANTHROPIC_API_KEY="<UNIFY_GATEWAY_KEY>"
```

Add the same lines to `~/.zshrc` or `~/.bashrc` to persist.

Note: OpenAI-compatible Base URLs include `/v1`. Anthropic-style Base URLs use the host root (no `/v1`). Most OpenAI and Anthropic SDKs send `api_key` as `Authorization: Bearer`, so the gateway key can be used as the client API key.

## IDE and agent tools

Tools such as Cursor, OpenCode, Continue, Cline, and similar OpenAI-compatible clients usually need:

| Setting | Value |
|---------|--------|
| OpenAI Base URL | `http://<host-ip>:8787/v1` |
| API key | gateway key, or any non-empty placeholder if the host has no auth |
| Model | a model id or alias from the host (`GET /v1/models`) |

Tips:

- Prefer environment variables (`OPENAI_BASE_URL`, `OPENAI_API_KEY`) when the tool reads them; otherwise paste the Base URL and key into the tool's provider settings.
- If the tool only accepts a host without path, try `http://<host-ip>:8787` and keep `/v1` in the model/API path if the tool supports it.
- Validate once with `curl` or the SDK sample in the README before blaming the IDE.
- Keep the dashboard private: it is open without auth; firewall the subnet tightly if that is a concern.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Client times out | Host bound to `127.0.0.1`, or firewall blocks 8787 | Start with `--host 0.0.0.0`; allow LAN subnet on 8787 |
| `401` from gateway | Missing or wrong gateway key | Send `Authorization: Bearer` or `x-api-key` with `UNIFY_GATEWAY_KEY` |
| `404` on chat | OpenAI Base URL missing `/v1` | Use `http://<host-ip>:8787/v1` for OpenAI-compatible clients |
| Works on host, not on client | Wrong IP, VPN, or AP isolation | Run `python scripts/print_lan_urls.py`; try the primary LAN IP; disable client VPN or AP isolation |
| SSL / URL errors in SDK | `https://` used for a plain HTTP LAN URL | Use `http://` for private LAN |
| `/healthz` OK but models empty | No enabled provider models | Check host `config.yaml` and `/v1/models` |
| Port already in use | Another process on 8787 | Change `--port` or stop the other process |

Quick checks from another machine:

```bash
curl -sS http://<host-ip>:8787/healthz
curl -sS http://<host-ip>:8787/api/info
curl -sS http://<host-ip>:8787/v1/models -H "Authorization: Bearer <UNIFY_GATEWAY_KEY>"
```

If `/api/info` is reachable but `/v1/models` returns `401`, auth is working and the client key is wrong or missing.

## Security notes

- Do not expose port `8787` to the public internet.
- Always set `UNIFY_GATEWAY_KEY` when bound to `0.0.0.0`.
- Restrict the firewall rule to the LAN / VPN subnet, not `Any`.
- Keep real keys only on the host (`config.yaml` / upstream env vars). Clients only need the gateway key.
