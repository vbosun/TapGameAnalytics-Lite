from __future__ import annotations

import argparse
import os
from pathlib import Path

from tapintel.environment import configure_stdio, load_env

from .server import create_owner_server


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="TapGame 自有游戏经营数据 MCP 服务")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="streamable-http"
    )
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    load_env(args.env)
    host = args.host or os.environ.get("TAP_GAME_MCP_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("TAP_GAME_MCP_PORT", "8765"))
    create_owner_server(host=host, port=port).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
