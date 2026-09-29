from __future__ import annotations

import csv
import io
import os
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from tapgame_mcp.owner_service import OwnerAnalyticsService


def _authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    token = os.environ.get("TAP_GAME_API_TOKEN", "").strip()
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, supplied):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def create_app(service: OwnerAnalyticsService | None = None) -> FastAPI:
    service = service or OwnerAnalyticsService()
    app = FastAPI(
        title="TapGame Owner Analytics API",
        version="1.0.0",
        description="Local, read-only API for data the configured developer account can access.",
    )

    @app.exception_handler(ValueError)
    def invalid_request(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/v1/games", dependencies=[Depends(_authorize)])
    def games() -> dict:
        return service.list_owner_games()

    @app.get("/api/v1/games/{app_id}/metrics", dependencies=[Depends(_authorize)])
    def game_metrics(
        app_id: int,
        developer_id: int = Query(...),
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        return service.query_owner_game_metrics(start_date, end_date, developer_id, app_id)

    @app.get("/api/v1/games/{app_id}/reports", dependencies=[Depends(_authorize)])
    def game_report(
        app_id: int, developer_id: int = Query(...), report_date: str | None = None
    ) -> dict:
        return service.get_owner_game_daily_report(report_date, developer_id, app_id)

    @app.get("/api/v1/portfolio/metrics", dependencies=[Depends(_authorize)])
    def portfolio(
        start_date: str | None = None,
        end_date: str | None = None,
        developer_id: int | None = None,
        format: str = Query("json", pattern="^(json|csv)$"),
    ):
        result = service.query_owner_portfolio_metrics(start_date, end_date, developer_id)
        if format == "json":
            return result
        fieldnames = [
            "developer_id", "app_id", "app_name", "stat_date", "impressions",
            "store_clicks", "store_page_views", "store_conversions",
            "store_click_rate", "store_conversion_rate", "active_devices",
            "new_devices", "mini_app_start_rate", "estimated_ad_revenue", "collected_at",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.get("rows", []))
        return PlainTextResponse(
            "\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8"
        )

    @app.get("/api/v1/collection/status", dependencies=[Depends(_authorize)])
    def collection_status() -> dict:
        return service.get_owner_collection_status()

    return app


app = create_app()
