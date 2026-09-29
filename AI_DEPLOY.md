# AI 部署与调用说明

本文是交给 AI Agent 直接执行的部署契约。目标是在不接触明文凭据、不影响现有服务的前提下，把 TapGame Analytics Lite 部署到无桌面的 Linux 主机，并完成可验证的采集与查询。

## 1. 任务输入

AI 开始前只需要确认这些信息：

- 已获授权的 SSH 目标和可用登录方式。
- 安装目录；未指定时可使用用户目录下独立的 `tapgame-analytics-lite` 目录。
- Cookie 已由用户放置在目标主机的哪个文件，或由用户稍后直接写入 `secrets/developer.cookie`。
- API、MCP 是否需要从其他机器访问，以及期望端口。
- 是否启用 MCP；默认不启用。

不需要游戏 ID。程序会从当前登录账号自动发现全部可读取游戏。

不得要求用户在对话中发送 Cookie、密码、Token 或数据库连接密码。不得在终端命令、日志、最终报告或 Git 提交中输出这些值。

## 2. 安全默认值

信息不完整但不影响安全时，采用以下默认值：

| 项目 | 默认值 |
| --- | --- |
| 运行方式 | Linux + Docker Compose |
| 数据库 | SQLite Docker volume |
| API | `127.0.0.1:8780` |
| MCP | 关闭；启用后为 `127.0.0.1:8765/mcp` |
| 调度 | 每天北京时间 09:00 |
| 采集范围 | 当前账号下全部可读取游戏 |
| 网络策略 | 串行、请求间隔 1 秒、重试与指数退避、每日最多 1000 请求 |

只能在用户明确要求并配置访问控制后把 API/MCP 暴露到局域网或公网。对外提供 REST API 时设置长随机 `TAP_GAME_API_TOKEN`，并使用防火墙和 TLS 反向代理。

## 3. 只读预检

先在目标主机执行只读检查，识别系统、磁盘、端口和既有容器：

```bash
id
docker --version
docker compose version
df -h
ss -lntp | grep -E ':(8780|8765)\b' || true
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
```

检查安装目录是否已经存在。如果已存在且是本仓库，保留 `.env`、`secrets/` 和数据卷并使用 `git pull --ff-only`；如果不是本仓库，选择新目录。端口被占用时在 `.env` 修改宿主机端口，例如 `TAP_GAME_API_PORT=18780`、`TAP_GAME_MCP_PORT=18765`。

不得为了部署本项目停止或删除其他容器、进程、目录、数据库或 Docker volume。

## 4. 获取代码和配置

公开仓库首次安装：

```bash
git clone https://github.com/vbosun/TapGameAnalytics-Lite.git
cd TapGameAnalytics-Lite
cp .env.example .env
mkdir -p secrets
```

现有安装升级：

```bash
cd /实际安装目录
git status --short
git pull --ff-only
```

如果工作区有未知改动，先报告并保留，不要 reset、checkout 或覆盖。

根据预检结果编辑 `.env`。中国大陆默认使用 DaoCloud 容器镜像和阿里云 PyPI 镜像；如不可用，通过 `TAP_GAME_PYTHON_IMAGE`、`TAP_GAME_POSTGRES_IMAGE` 和 `TAP_GAME_PIP_INDEX_URL` 改用可达镜像，无需修改 Dockerfile。

## 5. Cookie 交接

推荐让用户在目标主机直接执行：

```bash
nano secrets/developer.cookie
chmod 600 secrets/developer.cookie
```

文件只包含一行完整 Cookie 请求头值，并且必须包含 `DC_XSRF_TOKEN`。AI 可以检查文件是否存在、权限和字节数，但不得读取或打印内容：

```bash
stat -c '%n %a %s bytes' secrets/developer.cookie
```

容器进程使用 UID `10001`。如果在线检查明确报权限错误，可调整所有者后重启调度器：

```bash
sudo chown 10001:10001 secrets/developer.cookie
chmod 600 secrets/developer.cookie
docker compose restart scheduler
```

服务会把响应中的 `Set-Cookie` 安全合并回本地文件，从而延长支持滑动续期的登录态；平台硬失效、风控或二次验证仍需要用户重新登录并替换该文件。

## 6. 启动

先校验配置，再构建和启动默认服务：

```bash
docker compose config -q
docker compose build
docker compose up -d api scheduler
```

只有用户要求 MCP 时才启动：

```bash
docker compose --profile mcp up -d mcp
```

如果使用 PostgreSQL，必须先由用户确定强密码和连接参数，再按 README 的 PostgreSQL 配置启动；不要猜测或把凭据写入仓库。

## 7. 验收

依次完成以下验证：

```bash
docker compose ps
curl --fail --silent http://127.0.0.1:${TAP_GAME_API_PORT:-8780}/healthz
docker compose exec scheduler python -m tapintel owner-doctor --online
docker compose exec scheduler python -m tapintel owner-daily
curl --fail --silent http://127.0.0.1:${TAP_GAME_API_PORT:-8780}/api/v1/collection/status
```

验收成功必须同时满足：

- `api` 健康，`scheduler` 正常运行。
- 在线检查确认 Cookie 和账号访问有效。
- 至少完成一次自动发现与采集，失败请求为零；若平台确实没有某类数据，应报告“无数据”而不是写成 0。
- REST 能读取采集状态。
- 启用 MCP 时，客户端能初始化并通过 `tools/list` 看到五个只读工具。
- 既有服务、端口、数据库和数据没有被修改。

最终报告只给出游戏数量、请求成功/失败数量、日期范围和接口状态，不输出 Cookie、完整响应、收入明细或其他敏感数据。

## 8. AI 使用 MCP

部署后的 AI 优先使用 MCP，地址为 `http://127.0.0.1:<MCP端口>/mcp`。推荐调用顺序：

1. `get_owner_collection_status`：确认最近采集时间和状态。
2. `list_owner_games`：取得可用游戏及标识，不要预先要求用户提供游戏 ID。
3. `query_owner_portfolio_metrics`：按日期范围读取全部游戏汇总。
4. `query_owner_game_metrics`：需要单款游戏明细时再调用。
5. `get_owner_game_daily_report`：读取已经生成的 Markdown 日报。

日期使用 `YYYY-MM-DD`。收入数据可能延迟出数；`null`/缺失表示尚无可确认数据，不能推断为 0。工具全部只读，不允许 AI 通过 MCP 修改 Cookie、数据库或采集设置。

MCP 未启用时，可使用 README 中的 REST API 或 CLI。REST 查询跨主机开放时必须携带 Bearer Token；AI 不得把 Token 写入提示词、源码或日志。

## 9. 常见故障

- `Cookie expired`、401、403：停止重复请求，通知用户在本机浏览器重新登录并直接更新服务器 Cookie 文件。
- `Permission denied`：只检查 `secrets/developer.cookie` 和数据卷权限，不要放宽为全员可读写。
- 端口冲突：修改 `.env` 的宿主机绑定端口，不停止占用端口的既有服务。
- Docker Hub/PyPI 不可达：用 `.env` 切换镜像，保留可复现的配置。
- 429/5xx：保留默认退避，不提高并发；等待后重试。
- 后台响应结构变化：保留失败证据的字段名和状态码，绝不输出秘密或完整响应，也不要猜测字段继续写库。

## 10. 升级与回退原则

升级前记录当前版本，并备份 Docker volume 或 PostgreSQL 数据库。使用 `git pull --ff-only`、`docker compose build`、`docker compose up -d` 升级，再重复验收流程。发生故障时保留现场并报告；除非用户明确授权且备份已验证，不执行删除 volume、数据库或 Cookie 的操作。

## 可直接交给 AI 的提示词

```text
请先完整阅读仓库根目录的 AGENTS.md、AGENT.md 和 AI_DEPLOY.md，然后通过我已授权的 SSH 登录目标 Linux 主机部署 TapGame Analytics Lite。先做只读预检，不停止或覆盖任何既有服务。默认使用 SQLite，启动 API 和每日调度器；只有我明确要求时才启用 MCP。不要让我在对话里发送 Cookie，也不要读取或打印 Cookie；由我直接在目标主机写入 secrets/developer.cookie。完成后按 AI_DEPLOY.md 验收，只汇报安装目录、绑定地址、端口、游戏数量、采集请求成功/失败数量和服务状态，不汇报任何秘密值或经营明细。
```
