# 备份与保留策略

两件经常被混在一起、其实该分开看的事：**备份** 防数据丢失，
**保留策略** 控制磁盘上保留多长的历史。

## 保留策略

每天 02:30（服务器时间）跑一次清理任务，会删：

- 超过 `ARGUS_RETAIN_EVENTS_DAYS`（默认 90 天）的原始 `event` 行
- 超过 `ARGUS_RETAIN_LOGS_DAYS`（默认 30 天）的 `log_line` 行
- 超过 `ARGUS_RETAIN_SOFT_DELETED_DAYS`（默认 30 天）的软删除行
  —— 这些会变成硬删除

聚合表 —— 批次摘要、任务摘要、资源分钟桶汇总 —— *不会* 被清。
所以原始事件被清掉以后，面板上仍然能看到批次指标，只是不再有
per-epoch 粒度。

例外：

- 星标批次和置顶批次的原始事件永远保留。
- 当一个软删除行的父批次被星标时，它不会被硬删除。

也可以手动跑：

```bash
docker compose exec monitor python -m backend.scripts.retention_run
```

加 `--dry-run` 做一次干跑，只打日志说会删多少行。

## 备份

### SQLite

`.backup` 命令支持热备，对正在运行的 DB 也安全：

```bash
docker compose exec monitor sqlite3 /app/data/monitor.db \
  ".backup '/app/data/backup/monitor-$(date +%F).db'"
```

宿主机上挂 cron：

```cron
30 2 * * * docker compose exec -T monitor sqlite3 /app/data/monitor.db ".backup '/app/data/backup/monitor-$(date +\%F).db'"
```

按你顺手的工具（`logrotate`、`restic` 等）轮换：保留 7 个日备
+ 4 个周备 + 12 个月备。

恢复就是 `cp` —— 停容器、覆盖文件、再起。

### PostgreSQL

每天 `pg_dump`：

```bash
docker compose exec db pg_dump -U monitor monitor > monitor-$(date +%F).sql
```

生产环境则建议用 `pg_basebackup` / `wal-g` 做基础备份 + WAL 归档。
平台本身没有什么特殊要求 —— 就是个标准的 Postgres 消费者。

## Artifacts

上传的 artifacts（PNG / PDF / JSON）放在 `data/artifacts/`，按
普通二进制存储对待：

- SQLite 部署：跟 DB 文件一起做快照。
- Postgres 部署：用 rsync / restic 同步到对象存储。DB 里只存文件
  路径，所以 *只恢复 DB 不恢复 artifact 文件* 的话，元数据能看，
  但下载文件会 404。

## 灾难恢复演练

每个季度做一次：把最近一份备份恢复到一台用完即抛的机器上，跑：

1. `docker compose up -d --build`
2. 登录、打开最近的批次，确认损失曲线和 artifact 都能正常加载。
3. 用 `curl` 发一条合成事件，确认迁移版本与当前代码兼容。

只要这三步任意一步挂了，那份备份就还不算真正的备份。
