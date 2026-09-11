from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Unify LLM — local multi-provider gateway")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override server.host (e.g. 0.0.0.0 for LAN access)",
    )
    parser.add_argument("--port", type=int, default=None, help="Override server.port")
    args = parser.parse_args()

    from unify_llm.app import create_app
    from unify_llm.config import load_config

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        example = Path("config.example.yaml")
        raise SystemExit(
            f"Config not found: {cfg_path}\n"
            f"Copy {example} to config.yaml and fill in providers/keys, then re-run."
        )

    config = load_config(cfg_path)
    host = args.host or config.server.host
    port = args.port or config.server.port
    if args.host:
        config.server.host = args.host
    app = create_app(config_path=cfg_path, config=config)
    auth_required = bool(config.gateway_api_key())
    lan = host in ("0.0.0.0", "::")

    print(f"Unify LLM listening on http://{host}:{port}")
    print(f"  Dashboard:  http://{host}:{port}/dashboard")
    print(f"  OpenAI:     http://{host}:{port}/v1")
    print(f"  Anthropic:  http://{host}:{port}/v1/messages")
    print(f"  Health:     http://{host}:{port}/healthz")
    print(f"  Info:       http://{host}:{port}/api/info")
    print(f"  Gateway auth: {'required' if auth_required else 'disabled'}")
    if lan:
        print("  LAN access enabled — set UNIFY_GATEWAY_KEY and firewall port 8787.")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
