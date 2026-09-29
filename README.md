# TapGame Analytics Lite

一个在自己设备上运行的 TapTap 自有游戏经营数据工具。它会自动发现当前开发者账号能够读取的全部游戏，按日保存曝光、商店页转化、启动、新增设备和广告预估收入，并生成 Markdown 日报。

本仓库只有轻量版，不包含热门榜单、第三方游戏、评论、竞品研究、市场分析、Milvus、嵌入模型或向量检索代码。

> 非官方第三方项目，与 TapTap 不存在隶属、合作或授权关系。开发者后台接口不是公开稳定 API；请只访问本人有权读取的数据，并自行遵守平台协议。

## 能力

- Docker Compose 一键部署。
- 自动发现当前账号下全部可读取游戏，无需逐个配置游戏 ID。
- 每日采集曝光、点击、商店页浏览与转化、启动、新增设备和广告预估收入。
- 下载游戏图标并保存至数据库；图片请求不会携带开发者 Cookie。
- 默认 SQLite，安装可选依赖后可切换 PostgreSQL。
- 生成单游戏和全部游戏 Markdown 日报，广告收入延迟出数时自动回补最近 14 天。
- REST API、CLI、JSON/CSV 日期范围导出。
- 随包提供只读 MCP 服务，但 Docker 默认不启动。
- Cookie 本地保存、服务端滑动续期、失效检测、外部刷新命令和失败 Webhook。
- 串行请求、默认 1 秒间隔、429/5xx 指数退避、`Retry-After` 和每日请求上限。

## Docker Compose 部署

需要 Docker 和 Docker Compose。Linux 上执行：

```bash
git clone https://github.com/vbosun/TapGameAnalytics-Lite.git
cd TapGameAnalytics-Lite
cp .env.example .env
mkdir -p secrets
nano secrets/developer.cookie
chmod 600 secrets/developer.cookie
docker compose up -d --build
```

默认使用 DaoCloud 的 Python/PostgreSQL 镜像和阿里云 PyPI 镜像，适合中国大陆网络。可在 `.env` 中通过 `TAP_GAME_PYTHON_IMAGE`、`TAP_GAME_POSTGRES_IMAGE`、`TAP_GAME_PIP_INDEX_URL` 换成官方源、其他镜像或企业私有仓库；不需要修改 Dockerfile。API、调度器和可选 MCP 共用同一个本地镜像，不会重复保存三份应用镜像。

`nano` 中粘贴一行完整 Cookie 后，按 `Ctrl+O`、回车保存，再按 `Ctrl+X` 退出。Cookie 必须包含 `DC_XSRF_TOKEN`。

容器使用 UID `10001`。如果容器无法读写 Cookie 文件，可执行：

```bash
sudo chown 10001:10001 secrets/developer.cookie
chmod 600 secrets/developer.cookie
docker compose restart scheduler
```

默认启动：

- REST API：`http://127.0.0.1:8780`
- OpenAPI 文档：`http://127.0.0.1:8780/docs`
- 每日调度器：北京时间每天 09:00 运行
- SQLite：Docker volume 内的 `/data/tapgame.db`
- 日报：Docker volume 内的 `/data/reports`

先检查服务，再手动执行一次采集：

```bash
docker compose ps
docker compose exec scheduler python -m tapintel owner-doctor --online
docker compose exec scheduler python -m tapintel owner-daily
```

## 如何取得 Cookie

1. 在自己的电脑浏览器登录 [TapTap 开发者中心](https://developer.taptap.cn/)。
2. 打开浏览器开发者工具的 Network/网络面板。
3. 刷新开发者后台的数据页，选择发往 `developer.taptap.cn` 的请求。
4. 在 Request Headers/请求头中复制完整 `Cookie` 值。
5. 将值作为单独一行保存到 `secrets/developer.cookie`。

不要把 Cookie 发给项目维护者，不要粘贴进 Issue、截图、日志或 Git 提交。工具会合并响应中的 `Set-Cookie` 并原子写回本地文件，以维持服务端滑动会话；遇到硬失效、风控或二次验证时仍需重新登录并更新文件。

也可以从另一个本地文件安全导入：

```bash
python -m tapintel owner-cookie --from-file /安全路径/new-cookie.txt
```

如有自建的可信登录态更新程序，可在 `.env` 配置 `TAP_GAME_COOKIE_REFRESH_COMMAND`。本项目不会保存账号密码，也不会尝试绕过验证码或二次验证。

## REST API

接口均为只读：

```text
GET /healthz
GET /api/v1/games
GET /api/v1/games/{app_id}/metrics?developer_id=...&start_date=...&end_date=...
GET /api/v1/games/{app_id}/reports?developer_id=...&report_date=...
GET /api/v1/portfolio/metrics?start_date=...&end_date=...&format=json|csv
GET /api/v1/collection/status
```

API 默认只监听 `127.0.0.1`。如果要开放至局域网或公网，至少应在 `.env` 配置足够长的 `TAP_GAME_API_TOKEN`，调用时发送 `Authorization: Bearer <token>`，并使用防火墙和 TLS 反向代理。

## MCP 服务

MCP 随包提供，但 Compose 默认不开启：

```bash
docker compose --profile mcp up -d mcp
```

默认地址为 `http://127.0.0.1:8765/mcp`，提供五个只读工具：

- `list_owner_games`
- `query_owner_game_metrics`
- `get_owner_game_daily_report`
- `query_owner_portfolio_metrics`
- `get_owner_collection_status`

stdio 模式：

```bash
python -m tapgame_mcp --transport stdio
```

## CLI 查询和导出

```bash
# 初始化数据库
python -m tapintel owner-init

# 验证 Cookie 和在线登录态
python -m tapintel owner-doctor --online

# 采集全部可读取游戏
python -m tapintel owner-daily

# 查询单款游戏最近数据
python -m tapintel owner-metrics --developer-id 123456 --app-id 789012 --json

# 按日期范围导出全部游戏
python -m tapintel owner-export \
  --start 2026-09-01 --end 2026-09-30 \
  --output out/september.csv

# 读取已生成日报
python -m tapintel owner-report \
  --developer-id 123456 --app-id 789012 --report-date 2026-09-28
```

日期范围导出支持 JSON 和带 UTF-8 BOM 的 CSV，方便其他应用继续整合生成日志。

## 本机 Python 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
python -m tapintel owner-init
```

Windows PowerShell 激活命令为：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 可选 PostgreSQL

本机 Python 安装 PostgreSQL 驱动：

```bash
pip install ".[postgres]"
```

然后把 `TAP_GAME_DATABASE_URL` 改成 PostgreSQL URL。首次连接会在配置的 schema 中自动创建轻量版所需表。

使用 Compose 内置 PostgreSQL：

```dotenv
POSTGRES_DB="tapgame"
POSTGRES_USER="tapgame"
POSTGRES_PASSWORD="请替换为强密码"
TAP_GAME_COMPOSE_DATABASE_URL="postgresql://tapgame:请替换为强密码@postgres:5432/tapgame"
```

```bash
docker compose --profile postgres up -d postgres api scheduler
```

## 数据与安全边界

- Cookie、数据库、图标和日报默认只保存在部署者自己的设备上。
- 项目不会把 Cookie 或经营数据上传给维护者。
- 只允许访问代码中列出的 TapTap 开发者后台路径和图片域名。
- 不附带账号、Cookie、历史数据，也不提供批量抓取他人数据的能力。
- Webhook 只发送简短失败提示，不包含 Cookie 或完整经营数据。
- 删除本地数据库、报告目录和 Cookie 文件即可删除工具保存的数据。

更完整的边界见 [PRIVACY.md](PRIVACY.md)、[SECURITY.md](SECURITY.md) 和 [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md)。

## 测试

```bash
pip install ".[dev]"
python -m unittest discover -s tests -v
```

测试覆盖 Cookie 续期与失效处理、自动发现、图标安全下载、SQLite、日报、REST API、CSV 和 CLI。上线前仍应使用本人账号执行一次 `owner-doctor --online` 和人工冒烟验证。

## License

[MIT](LICENSE)
