from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from tapintel.environment import configure_stdio, load_env


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="TapGame 自有游戏只读 REST API")
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    load_env(args.env)
    uvicorn.run(
        "tapgame_api.app:app",
        host=args.host or os.environ.get("TAP_GAME_API_HOST", "127.0.0.1"),
        port=args.port or int(os.environ.get("TAP_GAME_API_PORT", "8780")),
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
