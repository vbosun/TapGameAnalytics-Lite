"""Small container-friendly scheduler for the lightweight owner analytics edition."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from tools._env import configure_stdio, load_env


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _notify(message: str) -> None:
    url = os.environ.get("TAP_GAME_ALERT_WEBHOOK", "").strip()
    if not url:
        return
    body = json.dumps({"text": message}, ensure_ascii=False).encode("utf-8")
    try:
        with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"告警 Webhook 发送失败：{exc}", file=sys.stderr)


def _run(env_path: Path) -> int:
    command = [sys.executable, "-m", "tapintel", "--env", str(env_path), "owner-daily"]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        _notify(f"TapGame 自有游戏日报采集失败，exit={result.returncode}；请检查 Cookie 和日志。")
    return result.returncode


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="轻量版自有游戏日报调度器")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    env_path = Path(args.env).resolve()
    load_env(env_path)
    if args.once:
        return _run(env_path)

    hour = max(0, min(23, int(os.environ.get("TAP_GAME_DAILY_HOUR", "9"))))
    state_path = Path(os.environ.get(
        "TAP_GAME_SCHEDULER_STATE", str(ROOT / "data" / "owner-scheduler.json")
    ))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = datetime.now(SHANGHAI)
        last_date = ""
        if state_path.exists():
            try:
                last_date = json.loads(state_path.read_text(encoding="utf-8")).get("last_date", "")
            except (OSError, json.JSONDecodeError):
                pass
        today = now.date().isoformat()
        if now.hour >= hour and last_date != today:
            rc = _run(env_path)
            if rc == 0:
                state_path.write_text(json.dumps({"last_date": today}), encoding="utf-8")
        time.sleep(max(30, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
