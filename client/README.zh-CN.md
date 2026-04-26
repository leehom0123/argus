> 🌐 **中文** · [English](./README.md)

# argus-reporter

[Argus](../) 服务的 fire-and-forget Python 客户端。
遵循**事件 schema v1.1**（新增客户端生成的 `event_id` 以支持幂等性）。

训练运行通过 HTTP 向中央监控服务推送生命周期事件（`batch_start`、`job_epoch`、`job_done`……）。
客户端的设计原则是：**永不向训练代码抛出异常，永不阻塞训练循环**。若后端不可达，事件将被
溢出（spill）到 JSONL 文件，并在下次运行时重放——由于每个事件携带 UUID `event_id`，
即使部分事件已经到达后端，重放也是安全的（后端按 id 去重）。

## 安装

三种安装方式任选其一：

```bash
# 1) 从 PyPI 安装（联网机器推荐）
pip install argus-reporter

# 2) 仓库内预构建 wheel（离线 / 无 PyPI 访问时使用）
pip install client/dist/argus-0.1.2-py3-none-any.whl

# 3) 源码开发模式
pip install -e "client/[dev]"
```

`client/dist/*.whl`（以及下游项目如 `DeepTS-Flow-Wheat/tools/wheels/` 中的
备份 wheel）是**离线兜底方案**，用于无法访问 PyPI 的服务器。联网机器优先用
PyPI 路径——补丁版本自动跟进，无需手动管理 wheel 文件。

运行时仅依赖 `requests>=2.31`，无其他依赖。

## 快速上手

```python
from argus import ExperimentReporter

rep = ExperimentReporter(
    url="http://monitor.local:8000",
    project="DeepTS-Flow-Wheat",
)
batch_id = rep.batch_start(
    experiment_type="forecast",
    n_total=120,
    command="scripts/forecast/run_benchmark.py --epochs 50",
)

rep.job_start(job_id="etth1_transformer", model="transformer", dataset="etth1")
for epoch in range(50):
    rep.job_epoch(
        job_id="etth1_transformer",
        epoch=epoch,
        train_loss=train_loss,
        val_loss=val_loss,
        lr=lr,
    )
rep.job_done(
    job_id="etth1_transformer",
    # metrics 字段完全由你定义：dict 原样进 leaderboard，每个 key 自动
    # 变成一列。`MSE` / `MAE` 是时序预测的约定，换成你任务对应的指标
    # 即可。详见 ../docs/zh/how-to/report-metrics-for-leaderboard.md
    metrics={"MSE": 0.44, "MAE": 0.42},
    elapsed_s=elapsed,
    train_epochs=50,
)

rep.batch_done(n_done=120, n_failed=0)
rep.close()
```

或以上下文管理器方式使用：

```python
with ExperimentReporter(url=URL, project=PROJECT) as rep:
    rep.batch_start(experiment_type="forecast", n_total=1)
    ...
# __exit__ 时自动排空队列
```

## 集成模式

### 1. 包装批次驱动器（集中式上报器）

在编排器中创建一个上报器实例，调用一次 `batch_start`，并在每个子进程训练完成时发出
`job_start` / `job_done`。训练脚本本身无需修改。参见
[`examples/benchmark_wrapper.py`](examples/benchmark_wrapper.py) 以及
DeepTS-Flow-Wheat 中 `scripts/forecast/run_benchmark.py` 的使用模式。

### 2. 作为训练回调

若训练框架支持回调，可将上报器接入 `on_train_begin` / `on_epoch_end` / `on_train_end`。
参见 [`examples/callback_style.py`](examples/callback_style.py)。

### 3. 混合模式

编排器发出批次级事件；每个训练子进程创建自己的上报器实例，共享 `batch_id` 并发出
任务级事件。`batch_id` 传递是唯一需要协调的内容。

## 故障模式与保证

| 情况 | 行为 |
|------|------|
| 后端不可达 / DNS 失败 | POST 重试 3 次（100 ms → 300 ms → 1 s），后追加至溢出文件。调用方不感知失败。 |
| 后端返回 5xx | 同上，走重试→溢出路径。 |
| 请求超时（默认 10 s） | 同上。 |
| 后端返回 429 | 按 `Retry-After` 休眠（上限 60 s），然后重试。不计入 3 次重试预算。 |
| 后端返回 401 / 403 | 记录 `error("Invalid credentials")`，丢弃事件，不重试。 |
| 后端返回 415 / 422 | 记录 `error("Schema mismatch")`，丢弃事件，不重试。 |
| 后端返回 404 | 记录 `warning`，丢弃事件。 |
| 队列满（默认 1000 个事件） | 丢弃最旧的事件，记录警告。 |
| 无效事件（缺少 `batch_id` / `event_id` 或未知 `event_type`） | 入队时丢弃并记录警告，不发送。 |
| 进程异常退出 | 守护线程通过 `atexit` 尝试排空队列。未排空的事件留在队列中——只有已溢出的事件才被持久化。 |
| 下次进程启动 | Worker 按 mtime 顺序扫描 `~/.argus-reporter/*.jsonl`，通过 `POST /api/events/batch` 逐文件重放。成功后删除文件。 |

**训练运行是不可侵犯的。** 所有上报器代码路径均包裹在 `try` / `except` 中——异常通过
`logging.getLogger("argus")` 记录，永不向上抛出。

## 配置参数

`ExperimentReporter(...)` 构造函数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `url` | （必填） | 监控服务的基础 URL，自动追加 `/api/events`。 |
| `project` | （必填） | 逻辑项目名（例如 `"DeepTS-Flow-Wheat"`）。 |
| `host` | `socket.gethostname()` | 上报主机名。 |
| `user` | `$USER` / `$USERNAME` | 上报用户名。 |
| `commit` | `git rev-parse --short HEAD` | 代码的 Git SHA，自动检测；不在 git 仓库中时为 `None`。 |
| `auth_token` | `None` | 提供时以 `Authorization: Bearer <token>` 发送。 |
| `timeout` | `10.0` | 每次 HTTP 请求的超时时间（秒）。 |
| `queue_size` | `1000` | 有界内部队列，满时采用丢弃最旧策略。 |
| `spill_path` | `~/.argus-reporter/spill-<pid>-<ts>.jsonl` | 不可投递事件的回退 JSONL 文件。 |
| `batch_id` | 首次 `batch_start()` 时自动生成 UUID | 可覆盖，用于多个上报器实例共享同一批次（例如编排器 + 训练子进程）。 |

## 环境变量

| 变量 | 效果 |
|------|------|
| `ARGUS_DISABLE=1` | 所有方法变为空操作，不产生网络流量、不写溢出文件、不启动 worker 线程。适用于不运行 Argus 服务的研究人员。也接受 `true`、`yes`、`on`。 |

## 事件 Schema（v1.1）

所有事件符合 [`schemas/event_v1.json`](../schemas/event_v1.json)。
本版本中 `schema_version` 固定为 `"1.1"`。每个事件还携带客户端生成的 UUID `event_id`，
后端用于对重试或重放的 POST 进行去重。

支持的 `event_type` 值：

- `batch_start`、`batch_done`、`batch_failed` — 扫描生命周期
- `job_start`、`job_epoch`、`job_done`、`job_failed` — 单次训练运行
- `resource_snapshot` — 主机级指标（GPU / 内存 / 磁盘）
- `log_line` — 可选的日志行转发

传入任何方法的额外关键字参数会合并到事件的 `data` 字段，从而保持向前兼容：
无需发布新客户端版本即可添加 `run_dir="..."` 或 `config_digest="..."` 等字段。

## 传输机制

- 单事件调用：`POST {url}/api/events`，附带 `Authorization: Bearer <token>`。
- 突发或溢出重放：`POST {url}/api/events/batch`，请求体为
  `{"events": [...]}（每批最多 500 个事件）`。
- 两个端点均接受和响应 `application/json`。
- 重试策略：5xx 及网络错误重试 3 次，指数退避（100 ms、300 ms、1 s）；429 等待
  `Retry-After`（上限 60 s）；其他 4xx（除 429 外）丢弃事件并记录结构化日志。

## 幂等性（v1.1 新增）

每个事件在构建时被赋予 UUID4 `event_id`。后端的 `POST /api/events[/batch]`
处理器按此 id 去重：若同一事件发送两次（因超时后重试，或溢出事件在原 POST 已悄然成功
后存活下来），后端返回 200 并附上原始 `db_id`，**不会**创建重复行。这意味着客户端的
"至少一次"投递模型与"恰好一次"存储语义完美结合。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

测试使用 `pytest-httpserver` 搭建本地模拟后端，并使用 `jsonschema` 对每个发出的事件
进行 `schemas/event_v1.json` 验证。

## 许可证

MIT（参见项目根目录）。
