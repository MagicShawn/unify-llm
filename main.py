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
    parser.add_argument("--host", default=None, help="Override server.host")
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
    app = create_app(config=config)

    print(f"Unify LLM listening on http://{host}:{port}")
    print(f"  Dashboard:  http://{host}:{port}/dashboard")
    print(f"  OpenAI:     http://{host}:{port}/v1")
    print(f"  Anthropic:  http://{host}:{port}/v1/messages")
    print(f"  Health:     http://{host}:{port}/healthz")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
