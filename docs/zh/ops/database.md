# 数据库（SQLite vs Postgres）

平台支持两种数据库后端。SQLite 是默认值，覆盖小型实验室的全部
场景。一旦你有多个上报主机或几个 GB 的事件历史，就该上
PostgreSQL。

## 默认：SQLite

```text
ARGUS_DB_URL=sqlite+aiosqlite:////app/data/monitor.db
```

优点：

- 运维成本为零 —— 一个文件、一个卷挂载就够了。
- 跟后端跑在同一个容器里。
- 备份就是 `cp monitor.db monitor.db.YYYYMMDD`。

缺点：

- 单写者模型。多个 reporter 并发上报没问题（后端用一个 async session
  串起来），但极大的瞬时洪峰会排队。
- 文件级锁不支持跨主机。**别** 把数据库放 NFS 上。

什么时候该升 Postgres：

- 事件历史超过约 10 GB；或
- 几百个并发 reporter；或
- 需要 PITR / 副本；或
- 计划跑多个平台副本。

## Postgres

设置：

```text
ARGUS_DB_URL=postgresql+asyncpg://user:password@db.example.com:5432/monitor
```

在 compose 里加上 Postgres 服务：

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: monitor
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}
      POSTGRES_DB: monitor
    volumes:
      - ./pgdata:/var/lib/postgresql/data
    restart: unless-stopped

  monitor:
    depends_on: [db]
    environment:
      ARGUS_DB_URL: postgresql+asyncpg://monitor:${POSTGRES_PASSWORD}@db:5432/monitor
```

然后在容器里一次性跑迁移（幂等）：

```bash
docker compose run --rm monitor alembic upgrade head
```

## 迁移

schema 用 Alembic 管，文件在 `backend/migrations/`。迁移是单向的：

- 容器入口每次启动都跑 `alembic upgrade head`。
- 新迁移走 PR 合入；多 head 合并需要写一个 merge 迁移
  （参见 `migrations/versions/*_merge_*.py`）。

开发时新建迁移：

```bash
docker compose exec monitor alembic revision --autogenerate -m "add foo"
```

提交前手工审一遍生成文件 —— autogenerate 不错，但识别索引重命名
之类的不一定准。

## SQLite → Postgres 迁移

目前没有原地工具。受支持的流程是：

1. 先把平台停掉。
2. 用 `pgloader` 跑一遍 SQLite 文件：
   ```bash
   pgloader sqlite:///path/to/monitor.db postgresql://user:pw@host/monitor
   ```
3. 在新 Postgres 上跑 `alembic upgrade head`，对齐索引。
4. 改 `ARGUS_DB_URL` 指到 Postgres，重启。

迁移是单向的，先在副本上演练。
