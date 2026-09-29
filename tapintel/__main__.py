from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .environment import configure_stdio, load_env
from .owner_analytics import OwnerAnalyticsError
from .owner_store import default_database_url, open_owner_store


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _report_date(value: str = "") -> date:
    return date.fromisoformat(value) if value else datetime.now(SHANGHAI).date() - timedelta(days=1)


def _identity(args) -> tuple[int | None, int | None]:
    if bool(args.developer_id) != bool(args.app_id):
        raise ValueError("--developer-id 与 --app-id 必须同时提供")
    return args.developer_id or None, args.app_id or None


def cmd_owner_init(args) -> None:
    store = open_owner_store(args.database_url, args.schema)
    store.close()
    report_dir = Path(
        os.environ.get("TAP_GAME_OWNER_REPORT_DIR", str(ROOT / "data" / "owner_reports"))
    ).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    print(f"数据库已就绪：{args.database_url}")
    print(f"日报目录已就绪：{report_dir}")


def cmd_owner_cookie(args) -> None:
    from .owner_analytics import _write_cookie_file

    source = Path(args.from_file).expanduser().resolve()
    target_value = os.environ.get("TAP_GAME_DEVELOPER_COOKIE_FILE", "").strip()
    if not target_value:
        raise OwnerAnalyticsError("请先配置 TAP_GAME_DEVELOPER_COOKIE_FILE")
    if not source.is_file():
        raise OwnerAnalyticsError(f"Cookie 来源文件不存在：{source}")
    _write_cookie_file(target_value, source.read_text(encoding="utf-8").strip())
    print(f"Cookie 已安全写入：{Path(target_value).expanduser().resolve()}")


def cmd_owner_doctor(args) -> None:
    from .owner_analytics import OwnerAnalyticsSettings, TapDeveloperClient

    store = open_owner_store(args.database_url, args.schema)
    store.conn.execute("SELECT 1").fetchone()
    store.close()
    settings = OwnerAnalyticsSettings.from_env_all(require_cookie=True)
    credential = (
        str(Path(settings[0].cookie_file).expanduser().resolve())
        if settings[0].cookie_file else "环境变量"
    )
    checks = [("数据库", "正常"), ("Cookie", f"正常（{credential}）")]
    if args.online:
        games = TapDeveloperClient(settings[0]).discover_games()
        checks.append(("在线登录态", f"正常，可读取 {len(games)} 款游戏"))
    else:
        checks.append(("在线登录态", "未检查；使用 --online 验证"))
    for name, result in checks:
        print(f"✓ {name}：{result}")


def cmd_owner_daily(args) -> None:
    from .owner_analytics import (
        OwnerAnalyticsSettings,
        TapDeveloperClient,
        build_portfolio_markdown,
        collect_and_report,
        query_portfolio_metrics,
        sync_owner_game_icons,
        sync_owner_games,
    )

    store = open_owner_store(args.database_url, args.schema)
    run_id = store.start_run("owner_daily")
    clients = []
    failures = []
    results = []
    started = time.time()
    try:
        settings_list = OwnerAnalyticsSettings.from_env_all(require_cookie=True)
        report_date = _report_date(args.report_date)
        discovered = len(settings_list) == 1 and settings_list[0].app_id == 0
        if discovered:
            discovery_client = TapDeveloperClient(settings_list[0])
            clients.append(discovery_client)
            settings_list = discovery_client.discover_games()
        sync_owner_games(store.conn, settings_list, disable_missing=discovered)
        icon_result = {"updated": 0, "skipped": len(settings_list), "failures": []}
        if discovered:
            icon_result = sync_owner_game_icons(store.conn, settings_list, discovery_client)
        store.conn.commit()
        output_dir = Path(
            args.output_dir
            or os.environ.get(
                "TAP_GAME_OWNER_REPORT_DIR", str(ROOT / "data" / "owner_reports")
            )
        ).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        for settings in settings_list:
            try:
                client = TapDeveloperClient(settings)
                clients.append(client)
                result, _ = collect_and_report(
                    store.conn, settings, report_date, args.backfill_days, client=client
                )
                store.conn.commit()
                output_path = output_dir / f"{settings.app_id}-{report_date.isoformat()}.md"
                output_path.write_text(result["markdown"], encoding="utf-8")
                results.append({
                    "developer_id": settings.developer_id,
                    "app_id": settings.app_id,
                    "app_name": settings.app_name,
                    "days_upserted": result["days_upserted"],
                    "output_path": str(output_path),
                })
            except Exception as exc:  # Continue collecting the other owned games.
                store.conn.rollback()
                failures.append({
                    "developer_id": settings.developer_id,
                    "app_id": settings.app_id,
                    "app_name": settings.app_name,
                    "error": repr(exc)[:500],
                })
        manifest = {
            "report_date": report_date.isoformat(),
            "generated_at": datetime.now(SHANGHAI).isoformat(),
            "succeeded": results,
            "failed": failures,
            "icons": icon_result,
        }
        manifest_path = output_dir / f"portfolio-{report_date.isoformat()}.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
        )
        portfolio = query_portfolio_metrics(store.conn, report_date, report_date)
        (output_dir / f"portfolio-{report_date.isoformat()}.md").write_text(
            build_portfolio_markdown(report_date, portfolio["rows"]), encoding="utf-8"
        )
        total = sum(client.requests_total for client in clients)
        ok = sum(client.requests_ok for client in clients)
        store.finish_run(
            run_id,
            total,
            ok,
            max(total - ok, len(failures)),
            json.dumps(manifest, ensure_ascii=False, default=str),
        )
    except Exception as exc:
        store.conn.rollback()
        total = sum(client.requests_total for client in clients)
        ok = sum(client.requests_ok for client in clients)
        store.finish_run(run_id, total, ok, max(total - ok, 1), repr(exc)[:1000])
        store.close()
        raise
    store.close()
    print(
        f"自有游戏经营采集：成功 {len(results)} 款 / 失败 {len(failures)} 款，"
        f"接口 {ok}/{total}，用时 {time.time() - started:.1f}s"
    )
    print(f"批次清单：{manifest_path}")
    if failures:
        raise OwnerAnalyticsError("部分游戏采集失败：" + "; ".join(
            f"{item['app_name']}({item['app_id']}): {item['error']}" for item in failures
        ))


def cmd_owner_metrics(args) -> None:
    from .owner_analytics import query_daily_metrics

    end = date.fromisoformat(args.end) if args.end else _report_date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=6)
    developer_id, app_id = _identity(args)
    store = open_owner_store(args.database_url, args.schema)
    result = query_daily_metrics(store.conn, start, end, developer_id, app_id)
    store.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return
    if not result["found"]:
        print("未找到自有游戏经营数据")
        return
    game = result["game"]
    print(f"{game['app_name']} ({game['app_id']})  {start} 至 {end}")
    print("日期        曝光      浏览    启动   新增   广告收入")
    print("─" * 58)
    for row in result["rows"]:
        revenue = row["estimated_ad_revenue"]
        revenue_text = "待回填" if revenue is None else f"¥{revenue:.2f}"
        print(
            f"{row['stat_date']}  {row['impressions'] or 0:>8,}  "
            f"{row['store_page_views'] or 0:>6,}  {row['active_devices'] or 0:>5,}  "
            f"{row['new_devices'] or 0:>5,}  {revenue_text:>9}"
        )


def cmd_owner_report(args) -> None:
    from .owner_analytics import query_saved_report

    developer_id, app_id = _identity(args)
    report_date = date.fromisoformat(args.report_date) if args.report_date else None
    store = open_owner_store(args.database_url, args.schema)
    result = query_saved_report(store.conn, report_date, developer_id, app_id)
    store.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    elif result["found"]:
        print(result["report"]["markdown"])
    else:
        print("未找到经营日报")


def cmd_owner_export(args) -> None:
    from .owner_analytics import query_portfolio_metrics

    end = date.fromisoformat(args.end) if args.end else _report_date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=29)
    store = open_owner_store(args.database_url, args.schema)
    result = query_portfolio_metrics(store.conn, start, end, args.developer_id or None)
    store.close()
    output_format = args.format
    if output_format == "auto":
        output_format = "csv" if args.output.lower().endswith(".csv") else "json"
    if output_format == "csv":
        fieldnames = [
            "developer_id", "app_id", "app_name", "stat_date", "impressions",
            "store_clicks", "store_page_views", "store_conversions",
            "store_click_rate", "store_conversion_rate", "active_devices",
            "new_devices", "mini_app_start_rate", "estimated_ad_revenue", "collected_at",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["rows"])
        payload = "\ufeff" + buffer.getvalue()
    else:
        payload = json.dumps(result, ensure_ascii=False, default=str, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        print(output)
    else:
        print(payload, end="" if payload.endswith("\n") else "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapgame-lite", description="TapTap 自有游戏经营数据工具")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="环境变量文件")
    parser.add_argument("--schema", default="", help="覆盖 TAP_GAME_POSTGRES_SCHEMA")
    sub = parser.add_subparsers(dest="cmd", required=True)

    daily = sub.add_parser("owner-daily", help="采集全部自有游戏并生成日报")
    daily.add_argument("--report-date", default="", help="报告日 YYYY-MM-DD，默认昨日")
    daily.add_argument("--backfill-days", type=int, default=14, help="每次回补天数（1–90）")
    daily.add_argument("--output-dir", default="", help="Markdown 日报目录")
    sub.add_parser("owner-init", help="初始化数据库和日报目录")
    doctor = sub.add_parser("owner-doctor", help="检查数据库、Cookie 和登录态")
    doctor.add_argument("--online", action="store_true", help="访问开发者后台验证登录态")
    cookie = sub.add_parser("owner-cookie", help="从本地文件安全导入 Cookie")
    cookie.add_argument("--from-file", required=True, help="包含一行完整 Cookie 的来源文件")

    metrics = sub.add_parser("owner-metrics", help="查询单款游戏的逐日指标")
    metrics.add_argument("--start", default="")
    metrics.add_argument("--end", default="")
    metrics.add_argument("--developer-id", type=int, default=0)
    metrics.add_argument("--app-id", type=int, default=0)
    metrics.add_argument("--json", action="store_true")
    report = sub.add_parser("owner-report", help="读取已生成的 Markdown 日报")
    report.add_argument("--report-date", default="")
    report.add_argument("--developer-id", type=int, default=0)
    report.add_argument("--app-id", type=int, default=0)
    report.add_argument("--json", action="store_true")
    export = sub.add_parser("owner-export", help="按日期范围导出全部游戏数据")
    export.add_argument("--start", default="")
    export.add_argument("--end", default="")
    export.add_argument("--developer-id", type=int, default=0)
    export.add_argument("--output", default="")
    export.add_argument("--format", choices=("auto", "json", "csv"), default="auto")
    return parser


def main(argv=None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(args.env)
    args.database_url = os.environ.get("TAP_GAME_DATABASE_URL", "").strip() or default_database_url()
    args.schema = args.schema or os.environ.get("TAP_GAME_POSTGRES_SCHEMA", "tap_game")
    handlers = {
        "owner-init": cmd_owner_init,
        "owner-cookie": cmd_owner_cookie,
        "owner-doctor": cmd_owner_doctor,
        "owner-daily": cmd_owner_daily,
        "owner-metrics": cmd_owner_metrics,
        "owner-report": cmd_owner_report,
        "owner-export": cmd_owner_export,
    }
    try:
        return handlers[args.cmd](args) or 0
    except (OwnerAnalyticsError, RuntimeError, ValueError) as exc:
        print(f"\n⛔ 自有游戏数据任务失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断（数据已提交的部分保留）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
