# Docker 部署

参考部署是基于 `deploy/Dockerfile` 多阶段构建的单镜像方案。第一阶段
用 Node 把前端打成静态资源；第二阶段用 Python 装 FastAPI 后端，
一并把前端文件挂出去，所以一个 8000 端口同时服务 API 与 UI。

## docker-compose

`deploy/` 下的文件：

- `Dockerfile` —— 多阶段（Node 20 → Python 3.11-slim）
- `docker-compose.yml` —— 单 `monitor` 服务，宿主机卷存 SQLite +
  artifacts
- `.env.example` —— 复制成 `.env` 改一下
- `entrypoint.sh` —— 先跑 Alembic 迁移，再起 `uvicorn`

```bash
cd deploy
cp .env.example .env  # 填 ARGUS_JWT_SECRET
docker compose up -d --build
```

第一次启动会跑 `alembic upgrade head` 来创建或迁移 SQLite schema。
迁移失败容器会干脆退出 —— 看 `docker compose logs monitor` 排查。

## 卷

| 宿主机路径 | 容器路径 | 内容 |
| --- | --- | --- |
| `./data/` | `/app/data/` | SQLite DB、上传的 artifacts |

跑 Postgres 部署时，丢掉 SQLite 卷，把 `ARGUS_DB_URL` 指到你的
Postgres —— 详见 [数据库](database.md)。

## 必填环境变量

`ARGUS_JWT_SECRET` 必须设，否则 Compose 会拒绝启动。生成一次以后
不要随便换，换了所有 session 都会失效：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

其它变量都有合理默认值；完整列表在
[配置项](configuration.md)。

## 反向代理

`deploy/nginx.snippet.conf` 里有一段可工作的 nginx 配置。把它丢到
某个 `server { ... }` 块里，指到容器 8000 端口，面板就能在你
nginx 暴露的域名上访问。

要注意的：

- **WebSocket / SSE** —— 平台用普通 HTTP 短轮询，不需要 upgrade
  规则。
- **Body 大小** —— artifact 上传可能几 MB，把
  `client_max_body_size` 调到 `20M` 或更高。
- **信任头** —— 设 `X-Forwarded-Proto`，让后端登录后能拼出正确的
  跳转 URL。

## 升级

```bash
cd deploy
git pull
docker compose pull   # 用发布镜像时
docker compose up -d --build
```

迁移是单向的；想把容器降级而不还原老 DB 备份，会失败。

## Healthcheck

镜像暴露 `GET /api/healthz`，DB 连接就绪后返回 `{"ok": true}`。
Compose 可以探活：

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/api/healthz"]
  interval: 30s
  timeout: 5s
  retries: 3
```

## 资源占用

一个典型的小型实验室部署（≤ 50 个并发 reporter，SQLite，
`monitor.db` < 1 GB）空闲时大概 256 MB 内存、CPU 远低于 0.1。
跑一个 32 任务 sweep 时，写入峰值大概 0.3 CPU 持续几秒。
