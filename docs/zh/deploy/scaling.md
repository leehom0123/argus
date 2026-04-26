> 🌐 [English](./scaling.md) · **中文**

# 后端扩容

Argus 默认以单个 uvicorn worker 运行，对于 SQLite 上
每天几千条事件的小团队来说够用了。随着采集量上升，可以在**不做 DB
迁移**的前提下拉三根杠杆：**uvicorn worker 数**、**DB 连接池**、
**Reporter SDK 批量上报**（已经默认打开，见 §6）。

本页记录 `deploy/entrypoint.sh` + `deploy/docker-compose.yml` 里给出
的默认值，这些默认值背后的进程级单例机制，以及什么时候该继续扩。

## 1. Uvicorn workers

容器 entrypoint 默认以 `--workers 4` 启动 uvicorn。每个 worker 都是
独立的 OS 进程，通过 `SO_REUSEPORT` 共享同一个监听端口，请求在它们
之间轮询——不需要外置的负载均衡器。

按部署环境覆盖：

```bash
# deploy/.env
ARGUS_WORKERS=2      # 小 VM
ARGUS_WORKERS=8      # 8 核主机 + Postgres
```

| workers   | DB backend | 说明                                                |
|-----------|------------|-----------------------------------------------------|
| 1         | SQLite     | 开发环境。SQLite 是单写入 DB。                      |
| 1–2       | SQLite     | 并发 reporter ≤ 5 的情况；受同一约束。              |
| 4         | Postgres   | 默认。4 核机器上约 25 RPS。                         |
| `N_CORES` | Postgres   | 采集密集型场景；搭配 §2 的池子计算。                |

健康检查、CORS、SPA fallback 和事件入库都是无状态的，线性扩展。
不能线性扩的那几块——见 §3。

## 2. DB 连接池

每个 worker 各开一份 SQLAlchemy 池子。compose 里暴露两个旋钮：

```yaml
ARGUS_DB_POOL_SIZE: 10        # 每 worker 稳态常驻连接
ARGUS_DB_POOL_MAX_OVERFLOW: 15 # 每 worker 突发额度
```

DB 端的峰值连接数 = `workers × (pool_size + max_overflow)`。按默认值
即 `4 × 25 = 100`，刚好等于 PostgreSQL 默认的 `max_connections=100`。

如果你在后端前面接了 transaction-pooling 模式的 `pgbouncer`，把两个
值都改成 `5`——pgbouncer 会替你复用真实的后端连接。

两个环境变量都不设的话，落回 `backend.db._pool_defaults_for(dialect)`：

- **SQLite**：`pool_size=1`，无 overflow（单写入约束）。
- **PostgreSQL**：`pool_size=20`，`max_overflow=30`。

## 3. 进程级单例

有些后台循环**不能**每个 worker 都跑一遍。我们用 `fcntl.flock` 配合
`/tmp/em-<name>.lock` 文件来抢锁——第一个启动成功的 worker 赢，其余
worker 在启动阶段直接跳过这一块。进程退出（包括 SIGKILL）时内核自动
释放 flock，所以崩溃的 worker 不会永久霸占锁，运维无需手动介入。

| 循环                    | 单例？  | 原因                                                   |
|-------------------------|--------:|--------------------------------------------------------|
| JWT 黑名单清理          | 否      | 每进程的内存状态，每个 worker 都得跑。                 |
| 看门狗规则              | 是      | 扫 `event` / `batch` 全表，并行会相互赛跑。            |
| SQLite 备份             | 是      | 每次写一份文件。4 个 worker → 每小时 4 份重复文件。    |
| 数据保留清扫            | 是      | `DELETE ... WHERE timestamp < cutoff`，并发会死锁。    |

数据保留循环是本次新增的。以前清扫只能走管理端 `POST
/api/admin/retention/sweep` 手动触发；现在每
`ARGUS_RETENTION_SWEEP_MINUTES` 分钟（默认 60）自动跑一次。
设成 `0` 可以关掉内建循环，改用外部 cron 驱动。

## 4. SSE 怎么办？

`/api/events/stream` 这个端点由客户端首次连上的那个 worker 提供。
SSE 是长连接，单 worker 能挂几千个客户端。在 4 核机器 + 4 worker
的配置下，我们实测到大约 6k 并发连接后 Python GIL 成为瓶颈。

前端 follow-up（Team FE）：前端会把大部分实时面板迁移到 10s 的
HTTP 轮询，SSE 只保留给通知铃铛和正在运行的 batch 时间线。这之后
扩展曲线的形状会从 `workers × 开连接数` 变成
`workers × 缓存命中率`。后端这边不需要配合改动。

## 5. 什么时候再往上扩

按当前默认配置，4 核 Postgres 主机能稳吃 ~50 RPS，突发 ~200 RPS。
再往上，按顺序：

1. **上 pgbouncer**（transaction pooling）。连接 churn 降 ~10×，
   可以放心拉高 `ARGUS_WORKERS` 而不撞 `max_connections`。
2. **采集服务和看板服务分家**。两个 compose 服务用同一个镜像，
   nginx 配两套 upstream：一套专调 `/api/events/*`，一套专调
   读密集的看板查询。
3. **Postgres 只读副本**。让看板服务指到副本，需要给
   `backend.db` 打个小 patch 按请求选 engine，当前仓库未随附。

## 6. Reporter SDK 批量上报（说明）

`argus` Python SDK 已经自带自动批量：

- 单事件 → `POST /api/events`。
- 队列里堆到 ≥ 20 条 → 批量 `POST /api/events/batch`（每次最多 500 条）。
- 重启后 spill 的 JSONL 文件 → 批量重放。

开箱即用，不需要配置。**不要**再在 SDK 外面自己包一层 debounce——
这样会破坏 SDK 的 spill / replay 崩溃保护。

## 相关

- [PostgreSQL 部署](./postgres.zh-CN.md)——何时以及如何切换 DB backend。
- `backend/backend/retention.py`——清扫循环到底删什么。
- `backend/backend/app.py::_try_singleton_lock`——fcntl 辅助函数。
