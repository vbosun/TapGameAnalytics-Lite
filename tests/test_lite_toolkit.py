from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from tapgame_api.app import create_app
from tapgame_mcp.owner_service import OwnerAnalyticsService
from tapintel.owner_analytics import (
    OwnerAnalyticsSettings,
    build_markdown,
    build_portfolio_markdown,
    query_daily_metrics,
    query_portfolio_metrics,
    save_report,
    upsert_daily,
)
from tapintel.owner_store import open_owner_store
from tapintel.__main__ import main as cli_main


class LiteToolkitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp.name, 'lite.db').as_posix()}"
        self.store = open_owner_store(self.database_url)
        self.settings = OwnerAnalyticsSettings(7, 42, "示例游戏", "DC_XSRF_TOKEN=x")
        rows = [
            {
                "stat_date": date(2026, 9, 28),
                "impressions": 100,
                "store_clicks": 10,
                "store_page_views": 9,
                "store_conversions": 3,
                "store_click_rate": 10,
                "store_conversion_rate": 30,
                "active_devices": 8,
                "new_devices": 2,
                "mini_app_start_rate": 80,
                "estimated_ad_revenue": 1.25,
                "raw_payload": {"source": "test"},
            }
        ]
        upsert_daily(self.store.conn, self.settings, rows)
        markdown = build_markdown("示例游戏", date(2026, 9, 28), [rows[0]])
        save_report(self.store.conn, self.settings, date(2026, 9, 28), markdown)
        self.store.conn.commit()

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_sqlite_supports_metrics_and_portfolio(self) -> None:
        result = query_daily_metrics(
            self.store.conn, date(2026, 9, 28), date(2026, 9, 28), 7, 42
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["rows"][0]["impressions"], 100)
        portfolio = query_portfolio_metrics(
            self.store.conn, date(2026, 9, 28), date(2026, 9, 28)
        )
        self.assertEqual(portfolio["rows"][0]["app_name"], "示例游戏")

    def test_cli_owner_init_uses_sqlite_without_postgres(self) -> None:
        database_url = f"sqlite:///{Path(self.temp.name, 'cli.db').as_posix()}"
        with patch.dict("os.environ", {
            "TAP_GAME_DATABASE_URL": database_url,
            "TAP_GAME_OWNER_REPORT_DIR": str(Path(self.temp.name, "reports")),
        }, clear=False):
            rc = cli_main(["--env", str(Path(self.temp.name, "missing.env")), "owner-init"])
        self.assertEqual(rc, 0)
        self.assertTrue(Path(self.temp.name, "cli.db").is_file())

    def test_rest_api_returns_games_and_csv(self) -> None:
        service = OwnerAnalyticsService(self.database_url)
        client = TestClient(create_app(service))
        self.assertEqual(client.get("/api/v1/games").json()["count"], 1)
        response = client.get(
            "/api/v1/portfolio/metrics",
            params={"start_date": "2026-09-28", "end_date": "2026-09-28", "format": "csv"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("示例游戏", response.text)

    def test_rest_api_honors_optional_bearer_token(self) -> None:
        service = OwnerAnalyticsService(self.database_url)
        with patch.dict("os.environ", {"TAP_GAME_API_TOKEN": "secret"}):
            client = TestClient(create_app(service))
            self.assertEqual(client.get("/api/v1/games").status_code, 401)
            response = client.get(
                "/api/v1/games", headers={"Authorization": "Bearer secret"}
            )
            self.assertEqual(response.status_code, 200)

    def test_reports_support_portfolio_and_wrapper_template(self) -> None:
        portfolio = build_portfolio_markdown(date(2026, 9, 28), [{
            "app_name": "示例游戏", "impressions": 100, "store_page_views": 9,
            "active_devices": 8, "new_devices": 2, "estimated_ad_revenue": 1.25,
        }])
        self.assertIn("全部游戏", portfolio)
        template = Path(self.temp.name, "report.md")
        template.write_text("{app_name}\n{report_date}\n{generated_report}", encoding="utf-8")
        row = dict(query_daily_metrics(
            self.store.conn, date(2026, 9, 28), date(2026, 9, 28), 7, 42
        )["rows"][0])
        with patch.dict("os.environ", {"TAP_GAME_REPORT_TEMPLATE": str(template)}):
            report = build_markdown("示例游戏", date(2026, 9, 28), [row])
        self.assertTrue(report.startswith("示例游戏\n2026-09-28"))


if __name__ == "__main__":
    unittest.main()
