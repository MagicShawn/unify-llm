from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Central Proxy — local LLM gateway")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument("--host", default=None, help="Override server.host")
    parser.add_argument("--port", type=int, default=None, help="Override server.port")
    args = parser.parse_args()

    from central_proxy.app import create_app
    from central_proxy.config import load_config

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
    app = create_app(config=config)

    print(f"Central Proxy listening on http://{host}:{port}")
    print(f"  Dashboard:  http://{host}:{port}/dashboard")
    print(f"  OpenAI:     http://{host}:{port}/v1")
    print(f"  Anthropic:  http://{host}:{port}/v1/messages")
    print(f"  Health:     http://{host}:{port}/healthz")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
