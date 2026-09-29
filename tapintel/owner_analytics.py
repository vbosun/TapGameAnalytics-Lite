"""Authenticated TapTap developer-console analytics collection and reporting.

The developer console endpoints are private UI endpoints rather than a published API.
Keep the endpoint allowlist narrow, retain raw per-day payloads for audit, and fail loudly
when TapTap changes a response shape instead of silently writing zeroes.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    class Jsonb:  # Minimal SQLite adapter; real psycopg Jsonb is used with PostgreSQL.
        def __init__(self, obj: Any):
            self.obj = obj


DEFAULT_BASE_URL = "https://developer.taptap.cn"
ALLOWED_ENDPOINTS = {
    "developers": "/api/developer/v1/list",
    "apps": "/api/app/v2/list",
    "position": "/api/dashboard/v2/stats-by-day/position/cn",
    "page_views": "/api/dashboard/v3/stats-by-day/pv/cn",
    "devices": "/api/mini-app/v1/stats-by-day/device",
    "launcher": "/api/dashboard/v2/stats-by-day/launcher-button",
    "ad_revenue": "/api/mini-app/v1/ad/payout-report-data",
}
METRIC_ENDPOINTS = {"position", "page_views", "devices", "launcher", "ad_revenue"}
ALLOWED_ICON_HOSTS = {"img.tapimg.com", "img2.tapimg.com", "assets.tapimg.com"}
# TapTap 图片 CDN 会按地域/机房返回不同子域（本机实测拿到的是 img-tc.tapimg.com），
# 逐个枚举必然漏；改为允许 tapimg.com 整个域下的子域，仍锁死在官方图片域名内。
ICON_HOST_SUFFIX = ".tapimg.com"
MAX_ICON_BYTES = 2 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DAILY_REQUEST_COUNTS: dict[tuple[date, str], int] = {}


def _icon_host_allowed(hostname: str | None) -> bool:
    """游戏图标域名白名单：显式列表 + tapimg.com 全部子域。"""
    host = (hostname or "").lower().rstrip(".")
    return bool(host) and (host in ALLOWED_ICON_HOSTS or host.endswith(ICON_HOST_SUFFIX))


class OwnerAnalyticsError(RuntimeError):
    """A developer analytics request or payload failed validation."""


class AuthenticationExpired(OwnerAnalyticsError):
    """The configured TapTap developer-console session is no longer valid."""


@dataclass(frozen=True)
class OwnerAnalyticsSettings:
    developer_id: int
    app_id: int
    app_name: str
    cookie: str
    base_url: str = DEFAULT_BASE_URL
    request_delay: float = 1.0
    timeout: float = 30.0
    cookie_file: str = ""
    refresh_command: str = ""
    refresh_timeout: float = 120.0
    max_retries: int = 3
    max_requests_per_day: int = 1000
    backoff_max: float = 30.0
    icon_url: str = ""
    icon_color: str = ""

    @classmethod
    def from_env(cls, require_cookie: bool = True) -> "OwnerAnalyticsSettings":
        settings = cls.from_env_all(require_cookie=require_cookie)
        if len(settings) != 1:
            raise OwnerAnalyticsError("配置了多个自有游戏，请使用 from_env_all")
        return settings[0]

    @classmethod
    def from_env_all(cls, require_cookie: bool = True) -> list["OwnerAnalyticsSettings"]:
        cookie = ""
        cookie_file = os.environ.get("TAP_GAME_DEVELOPER_COOKIE_FILE", "").strip()
        if require_cookie:
            cookie = os.environ.get("TAP_GAME_DEVELOPER_COOKIE", "").strip()
            if cookie and cookie_file:
                raise OwnerAnalyticsError(
                    "TAP_GAME_DEVELOPER_COOKIE 与 TAP_GAME_DEVELOPER_COOKIE_FILE 只能配置一个"
                )
            if cookie_file:
                cookie = _read_cookie_file(cookie_file)
            if not cookie:
                raise OwnerAnalyticsError(
                    "缺少 TapTap 登录态；请配置 TAP_GAME_DEVELOPER_COOKIE_FILE（推荐）"
                )
            if "\n" in cookie or "\r" in cookie:
                raise OwnerAnalyticsError("Cookie 文件必须只包含一行 Cookie 请求头值")
        games_json = os.environ.get("TAP_GAME_OWNER_GAMES", "").strip()
        games_file = os.environ.get("TAP_GAME_OWNER_GAMES_FILE", "").strip()
        if games_json and games_file:
            raise OwnerAnalyticsError("TAP_GAME_OWNER_GAMES 与 TAP_GAME_OWNER_GAMES_FILE 只能配置一个")
        if games_file:
            path = Path(games_file).expanduser()
            if not path.is_file():
                raise OwnerAnalyticsError(f"自有游戏配置文件不存在：{path}")
            games_json = path.read_text(encoding="utf-8")
        if games_json:
            try:
                games = json.loads(games_json)
            except json.JSONDecodeError as exc:
                raise OwnerAnalyticsError(f"自有游戏 JSON 配置无效：{exc}") from exc
            if not isinstance(games, list) or not games:
                raise OwnerAnalyticsError("自有游戏配置必须是非空 JSON 数组")
        elif os.environ.get("TAP_GAME_OWNER_DEVELOPER_ID") or os.environ.get("TAP_GAME_OWNER_APP_ID"):
            games = [{
                "developer_id": os.environ.get("TAP_GAME_OWNER_DEVELOPER_ID"),
                "app_id": os.environ.get("TAP_GAME_OWNER_APP_ID"),
                "app_name": os.environ.get("TAP_GAME_OWNER_APP_NAME", ""),
            }]
        else:
            games = []
        base_url = os.environ.get("TAP_GAME_DEVELOPER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "developer.taptap.cn":
            raise OwnerAnalyticsError("开发者数据采集仅允许访问 https://developer.taptap.cn")
        result = []
        seen = set()
        for item in games:
            if not isinstance(item, dict):
                raise OwnerAnalyticsError("自有游戏配置的每一项必须是对象")
            try:
                developer_id = int(item["developer_id"])
                app_id = int(item["app_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise OwnerAnalyticsError("developer_id 和 app_id 必须是整数") from exc
            identity = (developer_id, app_id)
            if identity in seen:
                raise OwnerAnalyticsError(f"自有游戏配置重复：{developer_id}/{app_id}")
            seen.add(identity)
            result.append(cls(
                developer_id=developer_id,
                app_id=app_id,
                app_name=str(item.get("app_name") or app_id).strip(),
                cookie=cookie,
                base_url=base_url,
                request_delay=float(os.environ.get("TAP_GAME_OWNER_REQUEST_DELAY", "1.0")),
                timeout=float(os.environ.get("TAP_GAME_OWNER_TIMEOUT", "30")),
                cookie_file=cookie_file,
                refresh_command=os.environ.get("TAP_GAME_COOKIE_REFRESH_COMMAND", "").strip(),
                refresh_timeout=float(os.environ.get("TAP_GAME_COOKIE_REFRESH_TIMEOUT", "120")),
                max_retries=max(0, int(os.environ.get("TAP_GAME_OWNER_MAX_RETRIES", "3"))),
                max_requests_per_day=max(
                    1, int(os.environ.get("TAP_GAME_OWNER_MAX_REQUESTS_PER_DAY", "1000"))
                ),
                backoff_max=max(1.0, float(os.environ.get("TAP_GAME_OWNER_BACKOFF_MAX", "30"))),
            ))
        if not result:
            result.append(cls(
                developer_id=0,
                app_id=0,
                app_name="",
                cookie=cookie,
                base_url=base_url,
                request_delay=float(os.environ.get("TAP_GAME_OWNER_REQUEST_DELAY", "1.0")),
                timeout=float(os.environ.get("TAP_GAME_OWNER_TIMEOUT", "30")),
                cookie_file=cookie_file,
                refresh_command=os.environ.get("TAP_GAME_COOKIE_REFRESH_COMMAND", "").strip(),
                refresh_timeout=float(os.environ.get("TAP_GAME_COOKIE_REFRESH_TIMEOUT", "120")),
                max_retries=max(0, int(os.environ.get("TAP_GAME_OWNER_MAX_RETRIES", "3"))),
                max_requests_per_day=max(
                    1, int(os.environ.get("TAP_GAME_OWNER_MAX_REQUESTS_PER_DAY", "1000"))
                ),
                backoff_max=max(1.0, float(os.environ.get("TAP_GAME_OWNER_BACKOFF_MAX", "30"))),
            ))
        return result


def _read_cookie_file(cookie_file: str) -> str:
    path = Path(cookie_file).expanduser()
    if not path.is_file():
        raise OwnerAnalyticsError(f"TapTap Cookie 文件不存在：{path}")
    cookie = path.read_text(encoding="utf-8").strip()
    if not cookie or "\n" in cookie or "\r" in cookie:
        raise OwnerAnalyticsError("Cookie 文件必须只包含一行非空 Cookie 请求头值")
    return cookie


def _write_cookie_file(cookie_file: str, cookie: str) -> None:
    """Atomically persist a renewed Cookie header with owner-only permissions."""
    path = Path(cookie_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(cookie + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class TapDeveloperClient:
    def __init__(self, settings: OwnerAnalyticsSettings):
        self.settings = settings
        self.requests_total = 0
        self.requests_ok = 0
        self.refresh_attempted = False
        self._set_cookie(
            _read_cookie_file(settings.cookie_file) if settings.cookie_file else settings.cookie
        )

    def _set_cookie(self, cookie: str) -> None:
        cookies = SimpleCookie()
        cookies.load(cookie)
        token = cookies.get("DC_XSRF_TOKEN")
        if token is None or not token.value:
            raise OwnerAnalyticsError("TapTap Cookie 缺少 DC_XSRF_TOKEN，请复制完整 Cookie 请求头值")
        self.xsrf_token = token.value
        self.cookie = cookie

    def _refresh_cookie(self) -> bool:
        if self.refresh_attempted or not self.settings.refresh_command:
            return False
        if not self.settings.cookie_file:
            raise OwnerAnalyticsError("自动刷新要求配置 TAP_GAME_DEVELOPER_COOKIE_FILE")
        self.refresh_attempted = True
        try:
            subprocess.run(
                shlex.split(self.settings.refresh_command),
                check=True,
                timeout=self.settings.refresh_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuthenticationExpired(f"Cookie 刷新命令失败：{exc}") from exc
        self._set_cookie(_read_cookie_file(self.settings.cookie_file))
        return True

    def _merge_response_cookies(self, headers: Any) -> None:
        """Apply response Set-Cookie values and persist sliding-session renewal."""
        get_all = getattr(headers, "get_all", None)
        values = get_all("Set-Cookie") if callable(get_all) else []
        if not values:
            return
        current = SimpleCookie()
        current.load(self.cookie)
        changed = False
        for value in values:
            incoming = SimpleCookie()
            incoming.load(value)
            for key, morsel in incoming.items():
                if morsel["max-age"] == "0":
                    if key in current:
                        del current[key]
                        changed = True
                    continue
                if key not in current or current[key].value != morsel.value:
                    current[key] = morsel.value
                    changed = True
        if not changed:
            return
        cookie = "; ".join(
            morsel.OutputString().split(";", 1)[0] for morsel in current.values()
        )
        self._set_cookie(cookie)
        if self.settings.cookie_file:
            _write_cookie_file(self.settings.cookie_file, cookie)

    def _get(self, endpoint_key: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            endpoint = ALLOWED_ENDPOINTS[endpoint_key]
        except KeyError as exc:
            raise OwnerAnalyticsError(f"未批准的开发者接口：{endpoint_key}") from exc
        url = urljoin(f"{self.settings.base_url}/", endpoint.lstrip("/"))
        url = f"{url}?{urlencode(params)}"
        max_attempts = self.settings.max_retries + 1
        for attempt in range(max_attempts):
            budget_key = (date.today(), self.settings.base_url)
            requests_today = _DAILY_REQUEST_COUNTS.get(budget_key, 0)
            if requests_today >= self.settings.max_requests_per_day:
                raise OwnerAnalyticsError(
                    f"已达到每日请求上限 {self.settings.max_requests_per_day}，停止采集"
                )
            request = Request(url, headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": self.cookie,
                "Referer": (
                    f"{self.settings.base_url}/v3/{self.settings.developer_id}/app/"
                    f"{self.settings.app_id}/store/version-release/data"
                ),
                "User-Agent": USER_AGENT,
                "X-DC-XSRF-TOKEN": self.xsrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "x-region": "zh",
            })
            self.requests_total += 1
            _DAILY_REQUEST_COUNTS[budget_key] = requests_today + 1
            try:
                with urlopen(request, timeout=self.settings.timeout) as response:  # noqa: S310
                    body = response.read()
                    self._merge_response_cookies(getattr(response, "headers", None))
                    payload = json.loads(body.decode("utf-8"))
            except HTTPError as exc:
                if exc.code in (401, 403):
                    if not self.refresh_attempted and self._refresh_cookie():
                        continue
                    raise AuthenticationExpired(
                        "TapTap 登录态已失效或无权读取该游戏，请更新 Cookie 文件"
                    ) from exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt + 1 < max_attempts:
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = min(self.settings.backoff_max, 2 ** attempt + random.random())
                    time.sleep(min(self.settings.backoff_max, max(0.0, wait)))
                    continue
                raise OwnerAnalyticsError(f"TapTap 接口 HTTP {exc.code}：{endpoint_key}") from exc
            except json.JSONDecodeError as exc:
                if not self.refresh_attempted and self._refresh_cookie():
                    continue
                raise AuthenticationExpired(
                    f"TapTap 返回了非 JSON 登录页，登录态可能已失效：{endpoint_key}"
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt + 1 < max_attempts:
                    time.sleep(min(self.settings.backoff_max, 2 ** attempt + random.random()))
                    continue
                raise OwnerAnalyticsError(f"TapTap 接口请求失败：{endpoint_key}: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("success") is not True:
                error_text = json.dumps(payload, ensure_ascii=False).lower()
                auth_failure = any(marker in error_text for marker in (
                    "unauth", "login", "401", "403", "未登录", "登录失效", "认证失败",
                ))
                if auth_failure and not self.refresh_attempted and self._refresh_cookie():
                    continue
                raise OwnerAnalyticsError(f"TapTap 接口返回失败：{endpoint_key}")
            break
        self.requests_ok += 1
        return payload

    def _get_list(self, endpoint_key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._get(endpoint_key, params)
        return self._payload_rows(endpoint_key, payload)

    @staticmethod
    def _payload_rows(endpoint_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        rows = data if isinstance(data, list) else None
        if isinstance(data, dict):
            for key in ("list", "developers", "apps", "items"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise OwnerAnalyticsError(f"TapTap 接口结构已变化：{endpoint_key} 缺少列表数据")
        return rows

    def discover_games(self) -> list[OwnerAnalyticsSettings]:
        """Discover every developer and game visible to the authenticated account."""
        developers = self._get_list("developers", {})
        games: list[OwnerAnalyticsSettings] = []
        seen = set()
        for developer in developers:
            developer_id = developer.get("developer_id") or developer.get("developerId") or developer.get("id")
            try:
                developer_id = int(developer_id)
            except (TypeError, ValueError) as exc:
                raise OwnerAnalyticsError("开发者列表缺少 developer_id") from exc
            page = 1
            received = 0
            while True:
                if self.settings.request_delay > 0:
                    time.sleep(self.settings.request_delay)
                payload = self._get("apps", {
                    "developer_id": developer_id,
                    "page": page,
                    "pagesize": 100,
                })
                rows = self._payload_rows("apps", payload)
                data = payload.get("data")
                total = data.get("total") if isinstance(data, dict) else None
                received += len(rows)
                for item in rows:
                    raw_id = item.get("app_id") or item.get("appId") or item.get("id")
                    try:
                        app_id = int(raw_id)
                    except (TypeError, ValueError) as exc:
                        raise OwnerAnalyticsError("游戏列表缺少 app_id") from exc
                    identity = (developer_id, app_id)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    name = (
                        item.get("app_name") or item.get("appName") or
                        item.get("name") or item.get("title") or str(app_id)
                    )
                    icon = item.get("icon")
                    icon_url = icon.get("url", "") if isinstance(icon, dict) else icon or ""
                    icon_color = icon.get("color", "") if isinstance(icon, dict) else ""
                    if str(icon_url).startswith("//"):
                        icon_url = f"https:{icon_url}"
                    games.append(replace(
                        self.settings,
                        developer_id=developer_id,
                        app_id=app_id,
                        app_name=str(name).strip() or str(app_id),
                        cookie=self.cookie,
                        icon_url=str(icon_url).strip(),
                        icon_color=str(icon_color).strip(),
                    ))
                if not rows or (total is not None and received >= int(total)):
                    break
                if total is None and len(rows) < 100:
                    break
                page += 1
                if page > 100:
                    raise OwnerAnalyticsError("游戏列表分页超过安全上限")
        if not games:
            raise OwnerAnalyticsError("当前登录账号下没有可读取的游戏")
        return games

    def download_icon(self, icon_url: str) -> tuple[bytes, str, str]:
        """Download one small TapTap-hosted game icon without sending account cookies."""
        parsed = urlparse(icon_url)
        if parsed.scheme != "https" or not _icon_host_allowed(parsed.hostname):
            raise OwnerAnalyticsError(f"不允许的游戏图标地址：{parsed.hostname or icon_url}")
        request = Request(icon_url, headers={"Accept": "image/*", "User-Agent": USER_AGENT})
        self.requests_total += 1
        try:
            with urlopen(request, timeout=self.settings.timeout) as response:  # noqa: S310
                final_url = urlparse(response.geturl())
                if final_url.scheme != "https" or not _icon_host_allowed(final_url.hostname):
                    raise OwnerAnalyticsError("游戏图标重定向到未批准的资源域名")
                mime_type = response.headers.get_content_type()
                if not mime_type.startswith("image/"):
                    raise OwnerAnalyticsError(f"游戏图标返回了非图片类型：{mime_type}")
                content = response.read(MAX_ICON_BYTES + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise OwnerAnalyticsError(f"游戏图标下载失败：{exc}") from exc
        if not content or len(content) > MAX_ICON_BYTES:
            raise OwnerAnalyticsError("游戏图标为空或超过 2 MiB 限制")
        self.requests_ok += 1
        return content, mime_type, hashlib.sha256(content).hexdigest()

    def collect(self, start_date: date, end_date: date) -> dict[str, dict[str, Any]]:
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")
        common = {
            "developer_id": self.settings.developer_id,
            "app_id": self.settings.app_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        requests = {
            "position": common,
            "page_views": common,
            "devices": common,
            "launcher": common,
            "ad_revenue": {
                "app_id": self.settings.app_id,
                "developer_id": self.settings.developer_id,
                "start_time": start_date.isoformat(),
                "end_time": end_date.isoformat(),
            },
        }
        payloads: dict[str, dict[str, Any]] = {}
        for index, (key, params) in enumerate(requests.items()):
            if index and self.settings.request_delay > 0:
                time.sleep(self.settings.request_delay)
            payload = self._get(key, params)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise OwnerAnalyticsError(f"TapTap 接口结构已变化：{key} 缺少 data.list")
            payloads[key] = payload
        return payloads


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
        if not value or value == "-":
            return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _decimal(value)
    return int(number) if number is not None else None


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["data"]["list"]


def normalize_daily(payloads: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the five console responses into one audited row per calendar day."""
    missing = METRIC_ENDPOINTS - set(payloads)
    if missing:
        raise OwnerAnalyticsError(f"缺少接口结果：{', '.join(sorted(missing))}")
    by_date: dict[str, dict[str, Any]] = {}

    def record(day: Any) -> dict[str, Any]:
        if not isinstance(day, str):
            raise OwnerAnalyticsError("TapTap 日数据缺少 date")
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise OwnerAnalyticsError(f"TapTap 返回非法日期：{day}") from exc
        return by_date.setdefault(day, {"stat_date": day, "raw_payload": {}})

    # The unfiltered position endpoint can return one aggregate row per platform.
    # Sum counts first; percentages are derived from the summed numerator/denominator.
    for row in _rows(payloads["position"]):
        target = record(row.get("date"))
        for field, output in (
            ("impression_cnt", "impressions"),
            ("click_cnt", "store_clicks"),
            ("convert_detail_cnt", "store_conversions"),
        ):
            value = _int(row.get(field))
            if value is not None:
                target[output] = target.get(output, 0) + value
        target["raw_payload"].setdefault("position", []).append(row)

    for row in _rows(payloads["page_views"]):
        target = record(row.get("date"))
        target["store_page_views"] = _int(row.get("pv_from_total"))
        target["raw_payload"]["page_views"] = row

    for row in _rows(payloads["devices"]):
        target = record(row.get("date"))
        target["active_devices"] = _int(row.get("active_device_num"))
        target["new_devices"] = _int(row.get("new_active_device_num"))
        target["raw_payload"]["devices"] = row

    for row in _rows(payloads["launcher"]):
        target = record(row.get("date"))
        target["mini_app_start_rate"] = _decimal(row.get("mini_app_start_rate"))
        target["raw_payload"]["launcher"] = row

    for row in _rows(payloads["ad_revenue"]):
        target = record(row.get("date"))
        revenue = row.get("revenue", row.get("estimated_income", row.get("income")))
        target["estimated_ad_revenue"] = _decimal(revenue)
        target["raw_payload"]["ad_revenue"] = row

    for target in by_date.values():
        impressions = target.get("impressions")
        clicks = target.get("store_clicks")
        conversions = target.get("store_conversions")
        target["store_click_rate"] = (
            Decimal(clicks) * 100 / Decimal(impressions) if impressions and clicks is not None else None
        )
        target["store_conversion_rate"] = (
            Decimal(conversions) * 100 / Decimal(clicks) if clicks and conversions is not None else None
        )
    return [by_date[key] for key in sorted(by_date)]


def sync_owner_games(
    conn,
    settings_list: Iterable[OwnerAnalyticsSettings],
    disable_missing: bool = False,
) -> int:
    """Persist discovered games so integrations can see games even before metrics succeed."""
    games = list(settings_list)
    if disable_missing:
        conn.execute("UPDATE owner_game SET enabled=FALSE,updated_at=CURRENT_TIMESTAMP")
    for settings in games:
        conn.execute(
            """INSERT INTO owner_game(
                   developer_id,app_id,app_name,enabled,icon_url,icon_color,updated_at)
               VALUES(%s,%s,%s,TRUE,%s,%s,CURRENT_TIMESTAMP)
               ON CONFLICT(developer_id,app_id) DO UPDATE SET
               app_name=excluded.app_name,enabled=TRUE,
               icon_url=COALESCE(NULLIF(excluded.icon_url,''),owner_game.icon_url),
               icon_color=COALESCE(NULLIF(excluded.icon_color,''),owner_game.icon_color),
               updated_at=CURRENT_TIMESTAMP""",
            (
                settings.developer_id,
                settings.app_id,
                settings.app_name,
                settings.icon_url or None,
                settings.icon_color or None,
            ),
        )
    return len(games)


def sync_owner_game_icons(
    conn,
    settings_list: Iterable[OwnerAnalyticsSettings],
    client: TapDeveloperClient,
) -> dict[str, Any]:
    """Download changed/missing icons and keep failures non-fatal for metric collection."""
    updated = 0
    skipped = 0
    failures = []
    for settings in settings_list:
        if not settings.icon_url:
            skipped += 1
            continue
        existing = conn.execute(
            """SELECT icon_url,octet_length(icon_data) AS icon_size
               FROM owner_game WHERE developer_id=%s AND app_id=%s""",
            (settings.developer_id, settings.app_id),
        ).fetchone()
        if existing and existing["icon_url"] == settings.icon_url and existing["icon_size"]:
            skipped += 1
            continue
        try:
            content, mime_type, digest = client.download_icon(settings.icon_url)
            conn.execute(
                """UPDATE owner_game SET icon_url=%s,icon_color=%s,icon_mime_type=%s,
                          icon_data=%s,icon_sha256=%s,icon_collected_at=CURRENT_TIMESTAMP,
                          updated_at=CURRENT_TIMESTAMP
                   WHERE developer_id=%s AND app_id=%s""",
                (
                    settings.icon_url,
                    settings.icon_color or None,
                    mime_type,
                    content,
                    digest,
                    settings.developer_id,
                    settings.app_id,
                ),
            )
            updated += 1
        except OwnerAnalyticsError as exc:
            failures.append({
                "developer_id": settings.developer_id,
                "app_id": settings.app_id,
                "error": str(exc),
            })
    return {"updated": updated, "skipped": skipped, "failures": failures}


def upsert_daily(conn, settings: OwnerAnalyticsSettings, rows: Iterable[dict[str, Any]]) -> int:
    sync_owner_games(conn, [settings])
    statement = """INSERT INTO owner_metric_daily(
        developer_id,app_id,stat_date,impressions,store_clicks,store_page_views,
        store_conversions,store_click_rate,store_conversion_rate,active_devices,
        new_devices,mini_app_start_rate,estimated_ad_revenue,raw_payload,collected_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
        ON CONFLICT(developer_id,app_id,stat_date) DO UPDATE SET
        impressions=excluded.impressions,store_clicks=excluded.store_clicks,
        store_page_views=excluded.store_page_views,store_conversions=excluded.store_conversions,
        store_click_rate=excluded.store_click_rate,
        store_conversion_rate=excluded.store_conversion_rate,
        active_devices=excluded.active_devices,new_devices=excluded.new_devices,
        mini_app_start_rate=excluded.mini_app_start_rate,
        estimated_ad_revenue=excluded.estimated_ad_revenue,
        raw_payload=excluded.raw_payload,collected_at=CURRENT_TIMESTAMP"""
    count = 0
    with conn.cursor() as cursor:
        for row in rows:
            cursor.execute(
                statement,
                (
                    settings.developer_id,
                    settings.app_id,
                    row["stat_date"],
                    row.get("impressions"),
                    row.get("store_clicks"),
                    row.get("store_page_views"),
                    row.get("store_conversions"),
                    row.get("store_click_rate"),
                    row.get("store_conversion_rate"),
                    row.get("active_devices"),
                    row.get("new_devices"),
                    row.get("mini_app_start_rate"),
                    row.get("estimated_ad_revenue"),
                    (
                        json.dumps(row.get("raw_payload", {}), ensure_ascii=False)
                        if getattr(conn, "dialect", "postgres") == "sqlite"
                        else Jsonb(row.get("raw_payload", {}))
                    ),
                ),
            )
            count += 1
    return count


REPORT_METRICS = (
    ("impressions", "曝光", "次", False),
    ("store_page_views", "商店页浏览", "次", False),
    ("active_devices", "启动用户", "人", False),
    ("new_devices", "新增用户", "人", False),
    ("estimated_ad_revenue", "广告预估收入", "元", True),
)


def _number(value: Any) -> float | None:
    number = _decimal(value)
    return float(number) if number is not None else None


def _fmt(value: Any, money: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "待回填"
    if money:
        return f"¥{number:,.2f}"
    return f"{number:,.0f}"


def _pct(value: Any) -> str:
    number = _number(value)
    return "待回填" if number is None else f"{number:.2f}%"


def _change(current: Any, previous: Any) -> str:
    now, before = _number(current), _number(previous)
    if now is None or before is None or before == 0:
        return "—"
    change = (now - before) / abs(before) * 100
    return f"{change:+.1f}%"


def build_markdown(app_name: str, report_date: date, rows: list[dict[str, Any]]) -> str:
    indexed = {str(row["stat_date"]): row for row in rows}
    current = indexed.get(report_date.isoformat())
    if current is None:
        raise OwnerAnalyticsError(f"数据库中没有 {report_date.isoformat()} 的经营数据")
    previous = indexed.get((report_date - timedelta(days=1)).isoformat(), {})
    baseline = [
        row for day, row in indexed.items()
        if report_date - timedelta(days=7) <= date.fromisoformat(day) < report_date
    ]

    lines = [
        f"# {app_name} · TapTap 每日报告",
        "",
        f"> 数据日期：{report_date.isoformat()}；报告默认使用完整自然日，广告收入为平台预估值。",
        "",
        "## 核心数据",
        "",
        "| 指标 | 当日 | 较前日 | 近 7 日均值 |",
        "|---|---:|---:|---:|",
    ]
    alerts: list[str] = []
    for key, label, unit, money in REPORT_METRICS:
        values = [_number(row.get(key)) for row in baseline]
        known = [value for value in values if value is not None]
        average = sum(known) / len(known) if known else None
        current_value = _number(current.get(key))
        current_text = _fmt(current.get(key), money)
        average_text = _fmt(average, money)
        lines.append(
            f"| {label} | {current_text}{'' if money or current_text == '待回填' else unit} "
            f"| {_change(current.get(key), previous.get(key))} | "
            f"{average_text}{'' if money or average_text == '待回填' else unit} |"
        )
        if current_value is not None and average not in (None, 0):
            deviation = (current_value - average) / abs(average) * 100
            if abs(deviation) >= 30:
                direction = "高于" if deviation > 0 else "低于"
                alerts.append(f"{label}{direction}近 7 日均值 {abs(deviation):.1f}%")

    lines += [
        "",
        "## 转化漏斗",
        "",
        f"曝光 {_fmt(current.get('impressions'))} → 点击 {_fmt(current.get('store_clicks'))} "
        f"→ 商店页转化 {_fmt(current.get('store_conversions'))}",
        "",
        f"- 商店页点击率：{_pct(current.get('store_click_rate'))}",
        f"- 商店页转化率：{_pct(current.get('store_conversion_rate'))}",
        f"- 小游戏启动率：{_pct(current.get('mini_app_start_rate'))}",
        "",
        "## 提醒",
        "",
    ]
    if current.get("estimated_ad_revenue") is None:
        alerts.append("广告收入尚未出数；后续日采会自动回补最近 14 天")
    if not alerts:
        alerts.append("核心指标未出现超过近 7 日均值 ±30% 的波动")
    lines.extend(f"- {alert}" for alert in alerts)
    lines += [
        "",
        "_口径：曝光、点击、商店页转化、小游戏设备数据与广告预估收入均来自 TapTap 开发者后台。_",
        "",
    ]
    generated = "\n".join(lines)
    template_path = os.environ.get("TAP_GAME_REPORT_TEMPLATE", "").strip()
    if not template_path:
        return generated
    path = Path(template_path).expanduser()
    if not path.is_file():
        raise OwnerAnalyticsError(f"日报模板不存在：{path}")
    try:
        return path.read_text(encoding="utf-8").format(
            app_name=app_name,
            report_date=report_date.isoformat(),
            generated_report=generated,
        )
    except KeyError as exc:
        raise OwnerAnalyticsError(f"日报模板包含未知占位符：{exc}") from exc


def build_portfolio_markdown(report_date: date, rows: list[dict[str, Any]]) -> str:
    """Build one compact report covering every collected game for the day."""
    lines = [
        "# 全部游戏 · TapTap 每日经营汇总",
        "",
        f"> 数据日期：{report_date.isoformat()}；广告收入为平台预估值。",
        "",
        "| 游戏 | 曝光 | 商店页浏览 | 启动用户 | 新增用户 | 广告预估收入 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['app_name']} | {_fmt(row.get('impressions'))} | "
            f"{_fmt(row.get('store_page_views'))} | {_fmt(row.get('active_devices'))} | "
            f"{_fmt(row.get('new_devices'))} | {_fmt(row.get('estimated_ad_revenue'), True)} |"
        )
    if not rows:
        lines.append("| 暂无数据 | — | — | — | — | — |")
    lines += ["", f"共 {len(rows)} 款游戏。", ""]
    return "\n".join(lines)


def load_report_rows(conn, developer_id: int, app_id: int, report_date: date) -> list[dict[str, Any]]:
    start = report_date - timedelta(days=7)
    return [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM owner_metric_daily
               WHERE developer_id=%s AND app_id=%s AND stat_date BETWEEN %s AND %s
               ORDER BY stat_date""",
            (developer_id, app_id, start, report_date),
        )
    ]


def save_report(conn, settings: OwnerAnalyticsSettings, report_date: date, markdown: str) -> None:
    conn.execute(
        """INSERT INTO owner_daily_report(developer_id,app_id,report_date,markdown,generated_at)
           VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP)
           ON CONFLICT(developer_id,app_id,report_date) DO UPDATE SET
           markdown=excluded.markdown,generated_at=CURRENT_TIMESTAMP""",
        (settings.developer_id, settings.app_id, report_date, markdown),
    )


def collect_and_report(
    conn,
    settings: OwnerAnalyticsSettings,
    report_date: date,
    backfill_days: int = 14,
    client: TapDeveloperClient | None = None,
) -> tuple[dict[str, Any], TapDeveloperClient]:
    """Run one auditable owner-data collection and persist its daily report."""
    if not 1 <= int(backfill_days) <= 90:
        raise ValueError("backfill_days 必须在 1 到 90 之间")
    client = client or TapDeveloperClient(settings)
    start_date = report_date - timedelta(days=int(backfill_days) - 1)
    payloads = client.collect(start_date, report_date)
    rows = normalize_daily(payloads)
    count = upsert_daily(conn, settings, rows)
    report_rows = load_report_rows(conn, settings.developer_id, settings.app_id, report_date)
    markdown = build_markdown(settings.app_name, report_date, report_rows)
    save_report(conn, settings, report_date, markdown)
    return {
        "start_date": start_date.isoformat(),
        "end_date": report_date.isoformat(),
        "days_upserted": count,
        "markdown": markdown,
    }, client


def resolve_owner_game(
    conn, developer_id: int | None = None, app_id: int | None = None
) -> dict[str, Any] | None:
    if (developer_id is None) != (app_id is None):
        raise ValueError("developer_id 与 app_id 必须同时提供或同时省略")
    if developer_id is not None:
        row = conn.execute(
            """SELECT developer_id,app_id,app_name,enabled,icon_url,icon_color,
                      icon_mime_type,icon_sha256,octet_length(icon_data) AS icon_size,
                      icon_collected_at
               FROM owner_game
               WHERE developer_id=%s AND app_id=%s""",
            (int(developer_id), int(app_id)),
        ).fetchone()
        return dict(row) if row else None
    rows = conn.execute(
        """SELECT developer_id,app_id,app_name,enabled,icon_url,icon_color,
                  icon_mime_type,icon_sha256,octet_length(icon_data) AS icon_size,
                  icon_collected_at
           FROM owner_game
           WHERE enabled=TRUE ORDER BY updated_at DESC,app_id LIMIT 2"""
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("存在多个启用的自有游戏，请明确指定 developer_id 与 app_id")
    return dict(rows[0]) if rows else None


def query_daily_metrics(
    conn,
    start_date: date,
    end_date: date,
    developer_id: int | None = None,
    app_id: int | None = None,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date 不能早于 start_date")
    if (end_date - start_date).days > 366:
        raise ValueError("单次最多查询 367 个自然日")
    game = resolve_owner_game(conn, developer_id, app_id)
    if game is None:
        return {"found": False, "game": None, "rows": []}
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT stat_date,impressions,store_clicks,store_page_views,
                      store_conversions,store_click_rate,store_conversion_rate,
                      active_devices,new_devices,mini_app_start_rate,
                      estimated_ad_revenue,collected_at
               FROM owner_metric_daily
               WHERE developer_id=%s AND app_id=%s AND stat_date BETWEEN %s AND %s
               ORDER BY stat_date""",
            (game["developer_id"], game["app_id"], start_date, end_date),
        )
    ]
    return {"found": True, "game": game, "rows": rows}


def query_saved_report(
    conn,
    report_date: date | None = None,
    developer_id: int | None = None,
    app_id: int | None = None,
) -> dict[str, Any]:
    game = resolve_owner_game(conn, developer_id, app_id)
    if game is None:
        return {"found": False, "game": None, "report": None}
    statement = """SELECT report_date,markdown,generated_at FROM owner_daily_report
                   WHERE developer_id=%s AND app_id=%s"""
    params: list[Any] = [game["developer_id"], game["app_id"]]
    if report_date is not None:
        statement += " AND report_date=%s"
        params.append(report_date)
    statement += " ORDER BY report_date DESC LIMIT 1"
    row = conn.execute(statement, params).fetchone()
    return {"found": row is not None, "game": game, "report": dict(row) if row else None}


def list_owner_games(conn) -> list[dict[str, Any]]:
    """List configured/collected games with their available data window."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT g.developer_id,g.app_id,g.app_name,g.enabled,
                      g.icon_url,g.icon_color,g.icon_mime_type,g.icon_sha256,
                      octet_length(g.icon_data) AS icon_size,g.icon_collected_at,
                      metrics.date_from,metrics.date_to,metrics.metric_days,
                      metrics.last_collected_at
               FROM owner_game g
               LEFT JOIN (
                   SELECT developer_id,app_id,min(stat_date) AS date_from,
                          max(stat_date) AS date_to,count(*) AS metric_days,
                          max(collected_at) AS last_collected_at
                   FROM owner_metric_daily GROUP BY developer_id,app_id
               ) metrics
                 ON metrics.developer_id=g.developer_id AND metrics.app_id=g.app_id
               ORDER BY g.developer_id,g.app_name,g.app_id"""
        )
    ]


def query_portfolio_metrics(
    conn,
    start_date: date,
    end_date: date,
    developer_id: int | None = None,
) -> dict[str, Any]:
    """Return a flat, integration-friendly daily dataset for every owned game."""
    if end_date < start_date:
        raise ValueError("end_date 不能早于 start_date")
    if (end_date - start_date).days > 366:
        raise ValueError("单次最多查询 367 个自然日")
    statement = """SELECT g.developer_id,g.app_id,g.app_name,g.icon_url,
                           g.icon_sha256,m.stat_date,
                           m.impressions,m.store_clicks,m.store_page_views,
                           m.store_conversions,m.store_click_rate,m.store_conversion_rate,
                           m.active_devices,m.new_devices,m.mini_app_start_rate,
                           m.estimated_ad_revenue,m.collected_at
                    FROM owner_metric_daily m
                    JOIN owner_game g USING(developer_id,app_id)
                    WHERE m.stat_date BETWEEN %s AND %s"""
    params: list[Any] = [start_date, end_date]
    if developer_id is not None:
        statement += " AND g.developer_id=%s"
        params.append(int(developer_id))
    statement += " ORDER BY m.stat_date,g.developer_id,g.app_id"
    rows = [dict(row) for row in conn.execute(statement, params)]
    return {
        "schema_version": 1,
        "range": {"start_date": start_date, "end_date": end_date},
        "games": list_owner_games(conn),
        "rows": rows,
    }
