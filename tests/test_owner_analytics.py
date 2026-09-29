import unittest
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from tapintel.owner_analytics import (
    OwnerAnalyticsError,
    OwnerAnalyticsSettings,
    TapDeveloperClient,
    build_markdown,
    normalize_daily,
)


def payload(rows):
    return {"success": True, "data": {"list": rows}}


class OwnerAnalyticsTests(unittest.TestCase):
    def test_client_requires_console_xsrf_cookie(self):
        settings = OwnerAnalyticsSettings(1, 2, "test", "session=value")
        with self.assertRaises(OwnerAnalyticsError):
            TapDeveloperClient(settings)

    def test_default_configuration_discovers_games(self):
        env = {
            "TAP_GAME_DEVELOPER_COOKIE": "DC_XSRF_TOKEN=test; session=value",
            "TAP_GAME_OWNER_DEVELOPER_ID": "",
            "TAP_GAME_OWNER_APP_ID": "",
            "TAP_GAME_OWNER_GAMES": "",
            "TAP_GAME_OWNER_GAMES_FILE": "",
        }
        with patch.dict("os.environ", env, clear=False):
            settings = OwnerAnalyticsSettings.from_env_all()
        self.assertEqual(1, len(settings))
        self.assertEqual(0, settings[0].app_id)

    def test_discovers_every_accessible_game(self):
        client = TapDeveloperClient(OwnerAnalyticsSettings(
            0, 0, "", "DC_XSRF_TOKEN=test; session=value", request_delay=0
        ))
        responses = {
            "developers": [{"id": 10}, {"developer_id": 20}],
            ("apps", 10): [{
                "app_id": 101,
                "app_name": "甲",
                "icon": {"url": "//img.tapimg.com/market/images/a.png", "color": "0xff00ff"},
            }],
            ("apps", 20): [{"id": 202, "name": "乙"}],
        }

        client._get_list = lambda key, _params: responses[key]
        client._get = lambda key, params: {
            "success": True,
            "data": {
                "list": responses[(key, params["developer_id"])],
                "total": len(responses[(key, params["developer_id"])]),
            },
        }
        games = client.discover_games()
        self.assertEqual([(10, 101, "甲"), (20, 202, "乙")], [
            (game.developer_id, game.app_id, game.app_name) for game in games
        ])
        self.assertEqual("https://img.tapimg.com/market/images/a.png", games[0].icon_url)
        self.assertEqual("0xff00ff", games[0].icon_color)

    def test_download_icon_enforces_host_type_size_and_omits_cookie(self):
        class Headers:
            def get_content_type(self):
                return "image/png"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://img.tapimg.com/market/images/icon.png"

            def read(self, _limit):
                return b"png-image"

        client = TapDeveloperClient(OwnerAnalyticsSettings(
            0, 0, "", "DC_XSRF_TOKEN=test; session=value"
        ))
        with patch("tapintel.owner_analytics.urlopen", return_value=Response()) as request:
            content, mime_type, digest = client.download_icon(
                "https://img.tapimg.com/market/images/icon.png"
            )
        sent_request = request.call_args.args[0]
        self.assertIsNone(sent_request.get_header("Cookie"))
        self.assertEqual(b"png-image", content)
        self.assertEqual("image/png", mime_type)
        self.assertEqual(64, len(digest))
        with self.assertRaises(OwnerAnalyticsError):
            client.download_icon("https://example.com/icon.png")

    def test_auth_failure_runs_refresh_and_reloads_cookie_once(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"success": True, "data": []}).encode()

        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookie"
            cookie_path.write_text("DC_XSRF_TOKEN=old; session=old", encoding="utf-8")
            settings = OwnerAnalyticsSettings(
                0, 0, "", "DC_XSRF_TOKEN=old; session=old",
                cookie_file=str(cookie_path), refresh_command="refresh-cookie",
            )
            client = TapDeveloperClient(settings)

            def refresh(*_args, **_kwargs):
                cookie_path.write_text("DC_XSRF_TOKEN=new; session=new", encoding="utf-8")

            with patch("tapintel.owner_analytics.subprocess.run", side_effect=refresh) as run, patch(
                "tapintel.owner_analytics.urlopen",
                side_effect=[HTTPError("url", 401, "expired", {}, None), Response()],
            ):
                client._get("developers", {})
            self.assertEqual("new", client.xsrf_token)
            self.assertEqual(1, run.call_count)
            self.assertEqual(2, client.requests_total)

    def test_response_cookie_is_merged_and_persisted(self):
        class Headers:
            def get_all(self, name):
                return [
                    "DC_XSRF_TOKEN=renewed; Path=/; Secure",
                    "session=fresh; Path=/; HttpOnly",
                ] if name == "Set-Cookie" else []

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"success": True, "data": []}).encode()

        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookie"
            cookie_path.write_text("DC_XSRF_TOKEN=old; session=old", encoding="utf-8")
            client = TapDeveloperClient(OwnerAnalyticsSettings(
                0, 0, "", "DC_XSRF_TOKEN=old; session=old", cookie_file=str(cookie_path)
            ))
            with patch("tapintel.owner_analytics.urlopen", return_value=Response()):
                client._get("developers", {})
            persisted = cookie_path.read_text(encoding="utf-8")
            self.assertIn("DC_XSRF_TOKEN=renewed", persisted)
            self.assertIn("session=fresh", persisted)
            self.assertEqual("renewed", client.xsrf_token)

    def test_normalize_merges_platform_counts_and_keeps_missing_revenue(self):
        values = normalize_daily({
            "position": payload([
                {"date": "2026-09-27", "platform": "android", "impression_cnt": 100,
                 "click_cnt": 10, "convert_detail_cnt": 4},
                {"date": "2026-09-27", "platform": "ios", "impression_cnt": 50,
                 "click_cnt": 5, "convert_detail_cnt": 1},
            ]),
            "page_views": payload([{"date": "2026-09-27", "pv_from_total": 12}]),
            "devices": payload([{"date": "2026-09-27", "active_device_num": 8,
                                  "new_active_device_num": 6}]),
            "launcher": payload([{"date": "2026-09-27", "mini_app_start_rate": "97.20%"}]),
            "ad_revenue": payload([{"date": "2026-09-27", "revenue": ""}]),
        })
        self.assertEqual(1, len(values))
        row = values[0]
        self.assertEqual(150, row["impressions"])
        self.assertEqual(15, row["store_clicks"])
        self.assertEqual(5, row["store_conversions"])
        self.assertEqual(Decimal("10"), row["store_click_rate"])
        self.assertEqual(Decimal("97.20"), row["mini_app_start_rate"])
        self.assertIsNone(row["estimated_ad_revenue"])

    def test_report_calls_out_delayed_revenue(self):
        rows = [
            {"stat_date": date(2026, 9, 26), "impressions": 100, "store_page_views": 20,
             "active_devices": 10, "new_devices": 5, "estimated_ad_revenue": Decimal("3"),
             "store_clicks": 10, "store_conversions": 4, "store_click_rate": Decimal("10"),
             "store_conversion_rate": Decimal("40"), "mini_app_start_rate": Decimal("95")},
            {"stat_date": date(2026, 9, 27), "impressions": 120, "store_page_views": 22,
             "active_devices": 12, "new_devices": 6, "estimated_ad_revenue": None,
             "store_clicks": 12, "store_conversions": 3, "store_click_rate": Decimal("10"),
             "store_conversion_rate": Decimal("25"), "mini_app_start_rate": Decimal("96")},
        ]
        report = build_markdown("测试游戏", date(2026, 9, 27), rows)
        self.assertIn("测试游戏 · TapTap 每日报告", report)
        self.assertIn("广告收入尚未出数", report)
        self.assertIn("曝光 120", report)


if __name__ == "__main__":
    unittest.main()
