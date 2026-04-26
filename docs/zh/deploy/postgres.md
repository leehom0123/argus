> 🌐 [English](./postgres.md) · **中文**

# 使用 PostgreSQL 部署

Argus 默认用 SQLite。SQLite 适合单用户开发环境和小团队。对于更大的部署，我们支持把 PostgreSQL 当作平替——同一套迁移、同一套 ORM，应用代码零改动。

## 什么时候换 PostgreSQL

满足任一条件建议切换：

- **同时有超过 5 个 reporter 在写** 同一个后端。SQLite 在文件层串行写入；ingest 压力大时后端日志里会出现 `database is locked` 警告。
- **事件超过 10 万条**（大约是几周密集 benchmark 的量）。SQLite 磁盘上没问题，但是 Dashboard 的复杂查询（32 个 batch 对比、跨项目 leaderboard）会变慢。
- **后端要横向多副本** 挂在负载均衡后面。SQLite 是文件；要跨进程 / 跨机器共享状态必须上真正的 DBMS。
- **需要零停机备份**。SQLite 的 `.backup` 循环虽然在线但会短暂阻塞写入；Postgres 有 `pg_dump` 和流复制。
- **合规 / 运维约束** — 某些环境禁止文件型数据库，必须用托管服务（RDS / Cloud SQL / Aiven / ...）。

如果以上都不是，**留在 SQLite 就好**。它就是为零配置默认而存在的。

## 启一个 PostgreSQL 容器

本地开发 / 评测用：

```bash
docker run --rm -d \
  --name em-postgres \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=changeme \
  -e POSTGRES_DB=argus \
  -v em-postgres-data:/var/lib/postgresql/data \
  postgres:16
```

生产环境建议用托管实例（RDS / Cloud SQL / Aiven / Supabase）或者专用机器上跑 `postgres:16` + systemd。PostgreSQL 14+ 都支持；CI 矩阵跑的是 16。

## 装后端 Postgres 驱动

后端默认只拉 SQLite 驱动。Postgres 驱动挂在可选依赖下：

```bash
cd backend
pip install -e ".[postgres]"
```

这会装：

- `asyncpg>=0.30` — SQLAlchemy 异步引擎用的 async 驱动
- `psycopg2-binary>=2.9` — Alembic DDL 迁移用的 sync 驱动

两个一起装，因为 Alembic（同步）和请求路径（异步）走两条不同的路连同一台服务器。

## 配置 `ARGUS_DB_URL`

用异步驱动指向你的 Postgres 实例：

```bash
export ARGUS_DB_URL='postgresql+asyncpg://em_user:password@pg-host:5432/argus'
```

然后跑迁移、起后端，和 SQLite 完全一样：

```bash
alembic upgrade head
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Alembic 在 DDL 路径上会自动回退到 `psycopg2` 同步驱动；async URL 是内部重写的。

## 把 SQLite 现有数据迁到 PostgreSQL

这是**单向**迁移；先把 SQLite 文件备份好。

下面只是思路，不要盲跑：

```bash
# 1. 把 SQLite 数据库 dump 成 SQL 文本
#    --data-only 很关键：schema 走 alembic 而不是 dump
sqlite3 backend/backend/data/monitor.db .dump > /tmp/em.sql

# 2. 通过 alembic 在 Postgres 上建 schema（而不是直接倒 dump）
ARGUS_DB_URL='postgresql+asyncpg://em_user:pw@pg:5432/em' alembic upgrade head

# 3. 把 SQLite 数据灌进 Postgres。你得手工改 dump：
#    - 去掉 SQLite-only 的 pragma
#    - 调整 BOOLEAN 字面量（0/1 → false/true）
#    - 按外键依赖重排 INSERT 顺序
psql postgresql://em_user:pw@pg:5432/em < /tmp/em_cleaned.sql
```

超过几千行事件就不建议 `dump | psql` 这种裸跑了，推荐用 pgloader 或者手写一个用 ORM 的 Python 脚本。写一次丢 `scripts/` 里留着。

## JSONB vs TEXT

好几个列存 JSON（`env_snapshot_json` / `metrics_json` / `cross_mark_weights_json` / ...）。目前都声明成 `Text`，这样 SQLite 和 PostgreSQL 一套 schema 通用。Postgres 上可以升级成 `JSONB` 跑索引键查询——这是 roadmap 跟踪的一项，切换时要带一次数据迁移。目前 `Text` 编码跨方言正确，升级时不需要改动。

## CI 覆盖

每次推送都会在 SQLite 和 PostgreSQL 16 矩阵上并行跑一遍后端测试套件（见 `.github/workflows/ci.yml`）。哪一边出问题 CI 都会在 PR 合并前挡下来。
