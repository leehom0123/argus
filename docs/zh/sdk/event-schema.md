# 事件 Schema

所有上报到平台的事件都遵循 **schema v1.1**，定义在
[`schemas/event_v1.json`](https://github.com/leehom0123/argus/blob/main/schemas/event_v1.json)。
SDK 会替你拼好这些事件，但下面把线上格式列出来，方便你从非 Python
客户端集成。

## 信封（Envelope）

每条事件外层结构相同：

```json
{
  "event_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "schema_version": "1.1",
  "event_type": "batch_start",
  "timestamp": "2026-04-25T09:23:06Z",
  "batch_id": "sweep-2026-04-abcd1234",
  "job_id": null,
  "source": {
    "project": "ts-bench",
    "host": "gpu-01",
    "user": "alice",
    "commit": "a14d303",
    "command": "python main.py experiment=dam"
  },
  "data": { "...": "类型相关的 payload" }
}
```

字段：

- **`event_id`** *（uuid，必填）* —— 客户端生成。后端用它做重试
  去重（网络抖动、spill 重放）。重发同一个 `event_id` 会得到
  200 + `deduplicated: true`，不会插第二行。
- **`schema_version`** —— 必须是 `"1.1"`。v1.0 已不再接受。
- **`event_type`** —— `data` 的判别字段。见下表。
- **`timestamp`** —— ISO 8601 UTC，结尾要带 `Z`。
- **`batch_id`** —— 字符串。每条事件都得带。
- **`job_id`** —— `job_*` 与 `log_line` 必填；其它情况下为 `null`。
- **`source`** —— 至少要 `project`。其它字段可选，但强烈建议都填，
  方便过滤和排错。
- **`data`** —— payload，按 `event_type` 分别约束。

## 事件类型

| 类型 | SDK 何时发出 | 必需的 `data` 字段 |
| --- | --- | --- |
| `batch_start` | `Reporter.__enter__` | `experiment_type`、`n_total` |
| `batch_done` | `Reporter.__exit__`（无异常） | `n_done`、`n_failed`、`total_elapsed_s` |
| `batch_failed` | `Reporter.__exit__`（带异常） | `reason`、`total_elapsed_s` |
| `job_start` | `JobContext.__enter__` | `job_id`、可选 `model`、`dataset` |
| `job_epoch` | `j.epoch(...)` | `epoch`（int）；可选 `train_loss`、`val_loss`、`lr`、`batch_time_ms` |
| `job_done` | `JobContext.__exit__`（干净退出） | 可选 `metrics`（dict）、`elapsed_s`、`train_epochs` |
| `job_failed` | `JobContext.__exit__`（异常） | `reason`、`elapsed_s` |
| `resource_snapshot` | `_resource_loop` 守护线程 | 可选 `gpu_util_pct`、`gpu_mem_mb`、`cpu_util_pct`、`ram_mb`、`disk_free_mb` 等 |
| `log_line` | `j.log(...)` 与心跳线程 | `line`、`level` |

## 接口

```
POST /api/events           # 单条
POST /api/events/batch     # 数组，每次 ≤ 500 条
```

两个都要 `Authorization: Bearer em_live_<...>`。
单条响应：

```json
{ "db_id": 17, "deduplicated": false }
```

批量响应：

```json
{ "results": [{ "db_id": 17, "deduplicated": false }, ...] }
```

## HTTP 语义

| 状态码 | 含义 | 客户端行为 |
| --- | --- | --- |
| 200 | 已接受 | 完成 |
| 401 / 403 | token 错 | 丢事件、记 error |
| 404 | endpoint 缺失 | 丢事件、记 warning |
| 415 | `schema_version` 不对 | 丢事件、记 error |
| 422 | 信封结构不对 | 丢事件、记 error |
| 429 | 限速 | 看 `Retry-After`（封顶 60 秒），重试 |
| 5xx / 网络错 | 临时性 | 指数退避（100 ms → 300 ms → 1 s）；3 次失败后 spill 到磁盘 |

SDK 的 spill 文件在 `~/.argus-reporter/*.jsonl`，按 mtime
顺序在下次进程启动时自动重放。

## 用非 Python 客户端验证

schema 文件在仓库的 `schemas/event_v1.json`，任何符合 JSON Schema
draft-07 的校验器都能用。把 `event_id` 校验成 UUID，把
`schema_version` 写成 `"1.1"`，就能上车。
