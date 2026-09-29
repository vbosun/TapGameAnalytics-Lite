from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .owner_service import OwnerAnalyticsService


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _csv_env(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in os.environ.get(name, default).split(",") if part.strip()]


def create_owner_server(
    service: OwnerAnalyticsService | None = None,
    host: str | None = None,
    port: int | None = None,
) -> FastMCP:
    """Create the read-only owner analytics MCP server."""
    service = service or OwnerAnalyticsService()
    host = host or os.environ.get("TAP_GAME_MCP_HOST", "127.0.0.1")
    port = int(port or os.environ.get("TAP_GAME_MCP_PORT", "8765"))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv_env(
            "TAP_GAME_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*"
        ),
        allowed_origins=_csv_env("TAP_GAME_MCP_ALLOWED_ORIGINS"),
    )
    mcp = FastMCP(
        "TapGame Owner Analytics",
        instructions=(
            "只读查询当前用户自行配置的 TapTap 开发者账号经营数据。"
            "广告收入字段是平台预估值；空值表示尚未出数，不等于零。"
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def list_owner_games() -> dict[str, Any]:
        """列出当前账号已采集的全部自有游戏及覆盖区间。"""
        return service.list_owner_games()

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def query_owner_game_metrics(
        start_date: str | None = None,
        end_date: str | None = None,
        developer_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        """按日期查询单款自有游戏经营指标。"""
        return service.query_owner_game_metrics(start_date, end_date, developer_id, app_id)

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def get_owner_game_daily_report(
        report_date: str | None = None,
        developer_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        """读取已生成的 Markdown 经营日报。"""
        return service.get_owner_game_daily_report(report_date, developer_id, app_id)

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def query_owner_portfolio_metrics(
        start_date: str | None = None,
        end_date: str | None = None,
        developer_id: int | None = None,
    ) -> dict[str, Any]:
        """按日期查询全部自有游戏，供其他应用整合日志。"""
        return service.query_owner_portfolio_metrics(start_date, end_date, developer_id)

    @mcp.tool(annotations=READ_ONLY, structured_output=True)
    def get_owner_collection_status() -> dict[str, Any]:
        """读取最新采集、失败情况和凭据维护提示。"""
        return service.get_owner_collection_status()

    return mcp
