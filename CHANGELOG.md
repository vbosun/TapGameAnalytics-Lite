# Changelog

## 0.1.3 - 2026-09-29

- 将默认 Python 包源改为服务器实测可用的阿里云镜像。
- API、调度器和 MCP 共用同一个应用镜像，减少重复构建与磁盘占用。
- 移除容器构建时非必要的 pip 自升级步骤。
- 使用 BuildKit pip 缓存，减少源码更新后的重复依赖下载。

## 0.1.2 - 2026-09-29

- 中国大陆部署默认使用 DaoCloud 容器镜像和清华 PyPI 镜像。
- 容器镜像及 Python 包源均可通过 `.env` 覆盖，无需修改 Dockerfile。

## 0.1.1 - 2026-09-29

- 排除 Docker 构建上下文中的 `.env`、Cookie、数据库、报告、Git 历史和本地构建产物。

## 0.1.0 - 2026-09-29

- 首次公开轻量版。
- 自动发现账号下全部可读取游戏，并采集逐日经营指标和图标。
- 支持 SQLite、可选 PostgreSQL、Markdown 日报、REST API、CLI 和 JSON/CSV 导出。
- 随包提供只读 MCP 服务，Docker Compose 默认不启动。
- 支持 Cookie 滑动续期、外部刷新命令、登录失效检测、限速、退避和失败告警。
