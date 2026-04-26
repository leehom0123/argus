# Reporter API

`argus` 包对外暴露两个公共类，加一组模块级辅助函数。
其它都是实现细节，可能在小版本之间发生变化。

## 快速上手

```python
from argus import Reporter

with Reporter("my-run",
              experiment_type="forecast",
              source_project="demo",
              n_total=2) as r:
    with r.job("j1", model="patchtst", dataset="etth1") as j:
        for ep in range(50):
            j.epoch(ep, train_loss=0.5, val_loss=0.6)
            if j.stopped:
                break
        j.metrics({"MSE": 0.21})
        j.upload("outputs/run/visualizations")
```

外层 `with` 块会发出 `batch_start` / `batch_done`（或异常时
`batch_failed`），并启动三个守护线程。内层 `with r.job(...)`
块发出对应的 `job_*` 事件。

## Reporter

::: argus.Reporter
    options:
      show_signature: true
      show_root_heading: true
      members:
        - batch_id
        - stopped
        - job
        - emit

## JobContext

::: argus.JobContext
    options:
      show_signature: true
      show_root_heading: true
      members:
        - job_id
        - stopped
        - epoch
        - metrics
        - log
        - upload

## 崩溃续跑

`Reporter` 接受两个新关键字参数：

- `batch_id="…"` —— 显式指定批次 id（覆盖 `batch_prefix` 自动生成）。
- `resume_from="…"` —— `batch_id` 的别名，意图是「续跑」。

配套有一个新的导出函数 `derive_batch_id(project, experiment_name, git_sha=None, *, prefix="bench")`：
对 `(project, experiment_name, git_sha)` 三元组做哈希，得到稳定的
`<prefix>-<16 hex>` 批次 id。同一份 checkout 重启同一个实验落到
**同一个** Batch 行上 —— 后端 `_handle_batch_start` 对
重复 `batch_start` 是幂等的。

```python
from argus import Reporter, derive_batch_id

batch_id = derive_batch_id("my-bench", "dam_forecast")
with Reporter(batch_prefix="bench",
              source_project="my-bench",
              n_total=120,
              batch_id=batch_id) as r:
    ...
```

完整流程见 [批次身份与续跑](resume.md)。

## 模块级辅助函数

::: argus.derive_batch_id
    options:
      show_signature: true

::: argus.new_batch_id
    options:
      show_signature: true

::: argus.set_batch_id
    options:
      show_signature: true

::: argus.get_batch_id
    options:
      show_signature: true

::: argus.emit
    options:
      show_signature: true

::: argus.sub_env
    options:
      show_signature: true

## 环境变量

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `ARGUS_URL` | 平台基地址，如 `http://localhost:8000` | 未设 → 进 no-op 模式 |
| `ARGUS_TOKEN` | reporter 范围 API token（`em_live_…`） | 未设 |
| `ARGUS_DISABLE` | 设为 `1` / `true` 时彻底关闭事件上报 | `0` |

no-op 模式下，SDK 静默吃掉所有 emit。这正是把埋点留在共享训练脚本
里需要的行为 —— 在没有面板的机器上不会崩。

## 安装额外项

| 包 | 内容 |
| --- | --- |
| `argus-reporter` | 基础 SDK |
| `argus-reporter[lightning]` | + PyTorch Lightning 回调 |
| `argus-reporter[keras]` | + Keras 回调 |
| `argus-reporter[hydra]` | + Hydra 回调 |
| `argus-reporter[all-integrations]` | 以上全部 |

## 错误处理

所有公共方法都不抛异常。网络错误以 `DEBUG` 级别记录后入队重试。
如果队列满了、本地 spill 文件也满了，事件会被丢弃 —— 训练第一，
SDK 永远不能阻塞用户代码。

## 线程安全

`Reporter` 和 `JobContext` 在用户多线程之间不是线程安全的。它们
启动的守护线程之间是。如果你需要从多个工作线程发事件，自己加锁，
或者用模块级的 `emit(...)` 辅助函数。
