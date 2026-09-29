from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from tapintel.owner_analytics import (
    list_owner_games,
    query_daily_metrics,
    query_portfolio_metrics,
    query_saved_report,
)
from tapintel.owner_store import default_database_url, open_owner_store


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class OwnerAnalyticsService:
    """Database-neutral read service used by the lite REST and MCP processes."""

    def __init__(self, database_url: str | None = None, schema: str | None = None) -> None:
        self.database_url = database_url or os.environ.get(
            "TAP_GAME_DATABASE_URL", default_database_url()
        )
        self.schema = schema or os.environ.get("TAP_GAME_POSTGRES_SCHEMA", "tap_game")

    def _store(self):
        return open_owner_store(self.database_url, self.schema)

    def list_owner_games(self) -> dict[str, Any]:
        store = self._store()
        try:
            games = list_owner_games(store.conn)
        finally:
            store.close()
        return _json_safe({"games": games, "count": len(games)})

    def query_owner_game_metrics(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        developer_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
        store = self._store()
        try:
            result = query_daily_metrics(store.conn, start, end, developer_id, app_id)
        finally:
            store.close()
        result["notice"] = "广告收入为平台预估值；空值表示尚未出数，不等于 0。"
        return _json_safe(result)

    def get_owner_game_daily_report(
        self,
        report_date: str | None = None,
        developer_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        parsed = date.fromisoformat(report_date) if report_date else None
        store = self._store()
        try:
            result = query_saved_report(store.conn, parsed, developer_id, app_id)
        finally:
            store.close()
        return _json_safe(result)

    def query_owner_portfolio_metrics(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        developer_id: int | None = None,
    ) -> dict[str, Any]:
        store = self._store()
        try:
            row = store.conn.execute("SELECT max(stat_date) AS value FROM owner_metric_daily").fetchone()
            latest = row["value"] if row else None
            if not latest:
                return _json_safe({
                    "schema_version": 1,
                    "range": None,
                    "games": list_owner_games(store.conn),
                    "rows": [],
                })
            end = date.fromisoformat(end_date or str(latest))
            start = date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
            result = query_portfolio_metrics(store.conn, start, end, developer_id)
        finally:
            store.close()
        result["notice"] = "广告收入为平台预估值；空值表示尚未出数，不等于 0。"
        return _json_safe(result)

    def get_owner_collection_status(self) -> dict[str, Any]:
        store = self._store()
        try:
            latest = store.conn.execute(
                """SELECT started_at,finished_at,req_total,req_ok,req_failed,note
                   FROM collect_run WHERE target='owner_daily'
                   ORDER BY run_id DESC LIMIT 1"""
            ).fetchone()
            games = list_owner_games(store.conn)
        finally:
            store.close()
        return _json_safe({
            "latest_collection": dict(latest) if latest else None,
            "games": games,
            "game_count": len(games),
            "credential_notice": (
                "认证失败时更新本机 Cookie 文件；服务不会上传 Cookie 或经营数据。"
            ),
        })
