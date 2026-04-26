> 🌐 **中文** · [English](./README.md)

# Argus 后端

基于 FastAPI 的服务，负责接收来自 ML 实验运行器的事件，并为 Vue 前端提供 JSON API。

## 安装

```bash
cd /path/to/argus/backend
pip install -e ".[dev]"
```

需要 Python 3.10+。

## 启动开发服务器

```bash
# 1. 首次运行（以及每次 schema 变更后）：手动执行 Alembic 迁移。
alembic upgrade head

# 2. 启动 API 服务。
uvicorn backend.app:app --reload --port 8000
```

服务提供以下路径：

- `/api/*` — 事件接入与查询端点（参见 `docs/architecture.md`）
- `/health` — 存活探针
- `/` — 已构建的前端（若 `../frontend/dist/` 存在）；否则仅提供 API

SQLite 文件默认路径为 `backend/data/argus.db`。可通过
`ARGUS_DB_URL=sqlite+aiosqlite:///...` 覆盖。

### 迁移由运维手动执行

进程启动时**不会**自动运行 Alembic。之前内嵌的 `command.upgrade` 调用在已运行的
asyncio 循环中为空操作，已被移除，而非引入线程池绕过方案。请在启动 uvicorn 之前手动
执行 `alembic upgrade head`；新安装时 `init_db()` 内部的 `Base.metadata.create_all`
会作为测试和一次性开发沙盒的便利手段自动建表。

### 生产环境：从访问日志中隐藏 `?token=`

浏览器的 `EventSource` 请求无法附加 `Authorization` 头，因此 SSE 流接受
`?token=<JWT>` 作为回退方式。应用在 `uvicorn.access` 日志记录器上安装了一个
`logging.Filter`，在日志行写出之前将所有 `token=...` 查询参数替换为
`token=REDACTED`。这是尽力而为的安全保护——推荐的生产部署方式仍是在 nginx（或类似反向
代理）处终止 TLS 并直接剥离该查询参数，例如：

```nginx
location /api/events/stream {
    # 从 $request 中移除 ?token=...，使其不进入访问日志
    set $scrub_args $args;
    if ($scrub_args ~ (.*)(^|&)token=[^&]*(.*)) {
        set $scrub_args $1$2$3;
    }
    access_log /var/log/nginx/em.log combined;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
}
```

## 运行测试

```bash
pytest -q
```

测试使用每个测试独立的内存 SQLite 数据库，可并行运行且互不干扰。

## 通知

规则存放于 `backend/config/notifications.yaml`（已加入 .gitignore；请从
`backend/notifications/config.yaml.example` 复制并修改）。Feishu Webhook URL 也可
通过 `ARGUS_FEISHU_WEBHOOK` 环境变量提供。

`when:` 字段仅支持受限 DSL（不使用 `eval`）：

```
event_type == "job_failed"
event_type == "batch_done" and data.n_failed > 0
data.gpu_util_pct < 10
```

## 事件契约

权威 schema：`../schemas/event_v1.json`。后端仅接受 `schema_version: "1.1"` 的事件；
其他值返回 **415 Unsupported Media Type**，响应体为
`{"detail": "Unsupported schema_version", "supported": ["1.1"]}`。v1.1 要求客户端生成
UUID `event_id` 以支持幂等重试（参见 `docs/requirements.md` §6.5 以及
`../client/argus/schema.py`）。
