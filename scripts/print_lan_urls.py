from __future__ import annotations

"""Print likely LAN base URLs for multi-machine Unify LLM clients.

Stdlib only (no psutil). Enumerates local IPv4 addresses, optionally probes
``127.0.0.1:8787/healthz``, and prints OpenAI / Anthropic env snippets with a
placeholder gateway key.
"""

import argparse
import socket
import urllib.error
import urllib.request
from typing import Iterable

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
PLACEHOLDER_KEY = "<UNIFY_GATEWAY_KEY>"
HEALTH_TIMEOUT_S = 1.5


def _parse_ipv4(addr: str) -> list[int] | None:
    if not addr or ":" in addr:
        return None
    parts = addr.split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return None
    if any(o < 0 or o > 255 for o in octets):
        return None
    return octets


def _is_usable_ipv4(addr: str) -> bool:
    """Skip loopback, link-local, and invalid addresses."""
    octets = _parse_ipv4(addr)
    if octets is None:
        return False
    if octets[0] == 127:
        return False
    if octets[0] == 169 and octets[1] == 254:
        return False
    return True


def _lan_rank(addr: str) -> int:
    """Lower rank = more likely a real home/office LAN address for other machines."""
    o = _parse_ipv4(addr)
    if o is None:
        return 100
    a, b = o[0], o[1]
    if a == 192 and b == 168:
        return 0
    if a == 10:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    # 198.18.0.0/15 — benchmark / VPN synthetic
    if a == 198 and b in (18, 19):
        return 50
    # 100.64.0.0/10 — CGNAT / some VPNs
    if a == 100 and 64 <= b <= 127:
        return 40
    return 10


def default_route_ipv4() -> str | None:
    """Best-effort primary LAN IP via a non-sending UDP connect trick."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Address is never contacted; it only selects a route.
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        return addr if _is_usable_ipv4(addr) else None
    except OSError:
        return None
    finally:
        s.close()


def hostname_ipv4s() -> list[str]:
    """IPv4 addresses associated with this host's name."""
    found: list[str] = []
    try:
        hostname = socket.gethostname()
    except OSError:
        return found

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []

    for info in infos:
        addr = info[4][0]
        if _is_usable_ipv4(addr) and addr not in found:
            found.append(addr)

    try:
        _, aliases, ips = socket.gethostbyname_ex(hostname)
        for ip in ips:
            if _is_usable_ipv4(ip) and ip not in found:
                found.append(ip)
        _ = aliases
    except OSError:
        pass

    return found


def collect_ipv4s() -> list[str]:
    """Ordered unique usable IPv4 addresses: best LAN candidates first."""
    candidates: list[str] = []
    primary = default_route_ipv4()
    if primary:
        candidates.append(primary)
    for ip in hostname_ipv4s():
        if ip not in candidates:
            candidates.append(ip)
    # Stable sort: preferred RFC1918 LAN ranges before VPN/synthetic/other.
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (_lan_rank(pair[1]), pair[0]))
    return [ip for _, ip in indexed]


def healthz_ok(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """Return (ok, detail) after probing http://host:port/healthz."""
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as resp:
            body = resp.read(256).decode("utf-8", errors="replace")
            ok = 200 <= getattr(resp, "status", 0) < 300
            return ok, f"HTTP {resp.status} {body.strip()[:80]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:  # noqa: BLE001 — any failure means "not ready"
        return False, f"{type(e).__name__}: {e}"


def base_urls(ip: str, port: int) -> dict[str, str]:
    return {
        "openai": f"http://{ip}:{port}/v1",
        "anthropic": f"http://{ip}:{port}",
        "healthz": f"http://{ip}:{port}/healthz",
        "info": f"http://{ip}:{port}/api/info",
        "dashboard": f"http://{ip}:{port}/dashboard",
    }


def env_snippets(ip: str, port: int, key: str) -> str:
    openai = f"http://{ip}:{port}/v1"
    anthropic = f"http://{ip}:{port}"
    return f"""\
# Windows PowerShell (current session)
$env:OPENAI_BASE_URL = "{openai}"
$env:OPENAI_API_KEY = "{key}"
$env:ANTHROPIC_BASE_URL = "{anthropic}"
$env:ANTHROPIC_API_KEY = "{key}"

# Windows CMD
set OPENAI_BASE_URL={openai}
set OPENAI_API_KEY={key}
set ANTHROPIC_BASE_URL={anthropic}
set ANTHROPIC_API_KEY={key}

# macOS / Linux
export OPENAI_BASE_URL="{openai}"
export OPENAI_API_KEY="{key}"
export ANTHROPIC_BASE_URL="{anthropic}"
export ANTHROPIC_API_KEY="{key}"
"""


def print_section(title: str, lines: Iterable[str]) -> None:
    print(title)
    for line in lines:
        print(f"  {line}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print likely LAN base URLs and client env snippets for Unify LLM."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Gateway port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--health-host",
        default=DEFAULT_HOST,
        help=f"Host used for the local healthz probe (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--key",
        default=PLACEHOLDER_KEY,
        help="API key string to show in snippets (default: placeholder)",
    )
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="Do not probe /healthz",
    )
    args = parser.parse_args()

    print_section(
        "Local IPv4 addresses (likely LAN clients use one of these):",
        collect_ipv4s() or ["(none found — check adapters / VPN)"],
    )

    if args.skip_health:
        print_section("Healthz:", ["skipped (--skip-health)"])
    else:
        ok, detail = healthz_ok(args.health_host, args.port)
        status = "UP" if ok else "DOWN"
        note = "" if ok else "  (start with: python main.py --host 0.0.0.0)"
        print_section(
            f"Healthz http://{args.health_host}:{args.port}/healthz — {status}:",
            [detail + note],
        )

    ips = collect_ipv4s()
    if not ips:
        print("No usable LAN IPv4; set OPENAI_BASE_URL manually once the host IP is known.")
        return

    # Show full URL set for the primary IP; remaining IPs get a compact line.
    primary = ips[0]
    urls = base_urls(primary, args.port)
    print_section(
        f"Base URLs for {primary} (share these with other machines):",
        [
            f"OpenAI-compatible:  {urls['openai']}",
            f"Anthropic Messages: {urls['anthropic']}",
            f"Health / info:      {urls['healthz']}  |  {urls['info']}",
            f"Dashboard:          {urls['dashboard']}",
        ],
    )

    if len(ips) > 1:
        print_section(
            "Other candidate IPs (replace in the URLs above if needed):",
            ips[1:],
        )

    print("Client env snippets (placeholder key — replace with UNIFY_GATEWAY_KEY):")
    print(env_snippets(primary, args.port, args.key))


if __name__ == "__main__":
    main()
