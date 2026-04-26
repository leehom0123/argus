> 🌐 **中文** · [English](./architecture.md)

# 架构说明

## 数据模型（三层层级结构）

```
batch   — 一次扫描/批次运行调用（例如 `run_benchmark.py --epochs 50 ...`）
  └── job    — 批次内的单个模型×数据集×随机种子组合
        └── event — 生命周期事件（start、epoch、done、failed……）

resource_snapshot — 独立的、按主机定期采集的 GPU/CPU/内存时间序列
```

## 事件 Schema

规范定义：`schemas/event_v1.json`（JSON Schema draft-07）。

所有事件包含 `schema_version`、`event_type`、`timestamp`、`batch_id`、`source`。`data` 载荷因 `event_type` 而异。

## SQLite 表（后端负责维护）

```sql
-- 一次扫描/批次运行的任务组
CREATE TABLE batch (
    id               TEXT PRIMARY KEY,
    experiment_type  TEXT,                -- 'forecast' | 'gene_expr' | ...
    project          TEXT NOT NULL,       -- 例如 'DeepTS-Flow-Wheat'
    user             TEXT,
    host             TEXT,
    command          TEXT,
    n_total          INTEGER,
    n_done           INTEGER DEFAULT 0,
    n_failed         INTEGER DEFAULT 0,
    status           TEXT,                -- 'running' | 'done' | 'failed'
    start_time       TEXT,                -- ISO 8601
    end_time         TEXT,
    extra            TEXT                 -- JSON
);

-- 单次运行
CREATE TABLE job (
    id               TEXT,                -- 批次内唯一
    batch_id         TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    model            TEXT,
    dataset          TEXT,
    status           TEXT,                -- 'running' | 'done' | 'failed'
    start_time       TEXT,
    end_time         TEXT,
    elapsed_s        INTEGER,
    metrics          TEXT,                -- job_done 携带的 JSON
    extra            TEXT,                -- JSON
    PRIMARY KEY (batch_id, id)
);

-- 所有原始事件；用于审计和轮次级时序
CREATE TABLE event (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL,
    job_id           TEXT,
    event_type       TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    data             TEXT                 -- JSON
);
CREATE INDEX idx_event_batch_job  ON event(batch_id, job_id);
CREATE INDEX idx_event_timestamp  ON event(timestamp);

-- 主机资源时序，与任何批次无关
CREATE TABLE resource_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    host             TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    gpu_util_pct     REAL,
    gpu_mem_mb       REAL,
    gpu_mem_total_mb REAL,
    gpu_temp_c       REAL,
    cpu_util_pct     REAL,
    ram_mb           REAL,
    ram_total_mb     REAL,
    disk_free_mb     REAL,
    extra            TEXT
);
CREATE INDEX idx_resource_host_ts ON resource_snapshot(host, timestamp);
```

## 一致性规则

- `batch_start` 必须在任何携带相同 `batch_id` 的 `job_*` 事件之前到达。
  - 若后端收到 `job_*` 事件但 `batch_id` 未知，则自动创建一条状态为 `status='running'` 的占位 batch 行。
- `job_start` 应在 `job_epoch` / `job_done` 之前到达。
  - 同样采用自动占位策略。
- `job_done` 和 `job_failed` 具有幂等性：最后写入的值生效。
- 计数字段（`batch.n_done`、`batch.n_failed`）为派生值——优先通过 SQL 重新计算，而不依赖运行时累加。

## 通知规则（后端）

规则存放于 `backend/config/notifications.yaml`。引擎对每个接收到的事件逐一评估规则，匹配的规则将通知入队并推送至配置的渠道。

```yaml
rules:
  - when: event_type == "job_failed"
    push: [feishu, whatsapp]
  - when: event_type == "batch_done" and data.n_failed > 0
    push: [feishu]
  - when: event_type == "resource_snapshot" and data.gpu_util_pct < 10
    push: [feishu]
```

## API 端点（v1）

```
POST   /api/events                    # 接入，fire-and-forget 安全
GET    /api/batches                   # 列表，支持 ?user=, ?project=, ?status=, ?since=, ?limit=
GET    /api/batches/{batch_id}        # 详情及汇总统计
GET    /api/batches/{batch_id}/jobs   # 批次内的任务列表
GET    /api/jobs/{batch_id}/{job_id}  # 单个任务详情及最近事件
GET    /api/jobs/{batch_id}/{job_id}/epochs   # 损失/指标时序
GET    /api/resources                 # 支持 ?host=, ?since=, ?limit=
GET    /api/resources/hosts           # 已发现的不同主机列表

GET    /api/events/stream             # SSE 实时更新流（Phase 2）
```

## 部署

目标：单台服务器（监控机或 VPS），对外暴露 :8000 端口。

- `backend/` 通过 uvicorn + systemd 运行
- `frontend/dist/` 作为静态文件由同一 FastAPI 实例服务
- SQLite 数据库位于 `backend/data/monitor.db`
- 可选 nginx 反向代理以支持 TLS / 子域名
