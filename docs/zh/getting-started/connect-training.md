# 接入训练任务

平台跑起来、token 拿到手以后，SDK 把事件上报这件事压缩成了两个
`with` 块。

## 安装

```bash
pip install argus-reporter
```

SDK 兼容 Python 3.10+，不带任何深度学习依赖。`requests` 和 `psutil`
会一起装；`pynvml` 是可选的，装了之后会自动上报 GPU 利用率。

## 配置

SDK 读两个环境变量：

```bash
export ARGUS_URL=http://localhost:8000
export ARGUS_TOKEN=em_live_xxxxxxxxxxxxxxxx
```

如果 `ARGUS_URL` 没设，`Reporter` 会进 no-op 模式 —— 训练脚本照常
跑，但不发任何事件。这样的好处是：把埋点留在共享代码里、偶尔在没装
面板的机器上跑也不会出问题。

## 最小例子

```python
from argus import Reporter

with Reporter("my-experiment",
              experiment_type="forecast",
              source_project="demo",
              n_total=1) as r:
    with r.job("job-1", model="DLinear", dataset="ETTh1") as j:
        for epoch in range(50):
            train_loss, val_loss = train_one_epoch()  # 你的训练代码
            j.epoch(epoch, train_loss=train_loss, val_loss=val_loss)
            if j.stopped:
                break
        j.metrics({"MSE": 0.21, "MAE": 0.34})
```

它做了什么：

- `Reporter.__enter__` 发出 `batch_start`，并启动三个守护线程
  （心跳、停止信号轮询、GPU/CPU 快照）。
- `r.job(...)` 打开 `JobContext`，发出带 `model` / `dataset` 标签的
  `job_start` / `job_done`。
- 每次 `j.epoch(...)` 发一条 `job_epoch` —— 这就是面板上实时损失
  曲线的来源。
- `j.metrics({...})` 暂存最终指标；离开内层 `with` 时随 `job_done`
  一起发出。
- 如果代码抛异常，`Reporter.__exit__` 会发 `batch_failed`（而不是
  `batch_done`），里面带异常类型和消息。

## Sweep / 任务矩阵模式

```python
combos = [
    ("DLinear",  "ETTh1"),
    ("PatchTST", "ETTh1"),
    ("DLinear",  "ETTh2"),
]

with Reporter("sweep-2026-04",
              experiment_type="forecast",
              source_project="ts-bench",
              n_total=len(combos)) as r:
    for i, (model, dataset) in enumerate(combos):
        with r.job(f"j{i}", model=model, dataset=dataset) as j:
            metrics = train(model, dataset)
            j.metrics(metrics)
```

任务矩阵视图按 `model × dataset` 把任务铺开成网格，每个格子里显示
最佳 / 最差指标。详见 [任务矩阵](../user-guide/job-matrix.md)。

## 自动跑起来的部分

进入 `Reporter` 块后，SDK 同时跑三个守护线程。每个都可以在构造
`Reporter` 时关掉或重设间隔：

| 守护线程 | 默认间隔 | 作用 |
| --- | --- | --- |
| 心跳 | 300 秒 | 长耗时分析（SHAP、画图）不会触发卡死检测 |
| 停止信号轮询 | 10 秒 | 在 UI 上点 *Stop*，`j.stopped` 翻成 `True`，可以干净退出 |
| 资源快照 | 30 秒 | GPU / CPU / RAM / 磁盘读数喂给主机面板 |

按需关掉：

```python
Reporter("quick-debug", heartbeat=False, resource_snapshot=False)
```

## 上传图

在 job 上下文里：

```python
j.upload("outputs/run-42/visualizations", glob="**/*.png")
```

目录里的 PNG / JPG / PDF / SVG 文件会 POST 到
`/api/jobs/<job-id>/artifacts`，在 job 详情页能直接看到。

## PyTorch Lightning

```python
from argus.integrations.lightning import ArgusCallback
import pytorch_lightning as pl  # 或：import lightning as L

trainer = pl.Trainer(callbacks=[ArgusCallback(
    project="my-paper",
    job_id="bert-base",
    model="bert-base-uncased",            # 可选，写到 job_start
    dataset="glue/sst2",                  # 可选，写到 job_start
    argus_url="http://localhost:8000",    # 也可只用 ARGUS_URL
    token="em_live_…",                    # 也可只用 ARGUS_TOKEN

    # 告诉适配器 trainer.callback_metrics 里哪些 key 当 train_loss / val_loss。
    # 第一个能取到 finite float 的 key 胜出。下面是默认值：
    train_loss_keys=("train_loss_epoch", "train_loss", "loss"),
    val_loss_keys=("val_loss_epoch", "val_loss"),

    # 末轮汇总指标 —— 在 on_train_end 时一次性读取，传给
    # JobContext.metrics。LightningModule 里 self.log() 出来的 key 都行。
    final_metric_keys=("val_mse", "val_rmse", "val_mae",
                       "val_r2", "val_pcc"),

    # 三个守护线程在 Lightning 适配器里默认 OFF（裸 Reporter 是 True）。
    heartbeat=False, stop_polling=False, resource_snapshot=False,
)])
trainer.fit(model, datamodule)
```

构造参数（全部 keyword-only，源：`client/argus/integrations/lightning.py`）：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `project` | 必填 | `Reporter` 的 `source_project` |
| `job_id` | 必填 | 这次 run 的标识 |
| `model`、`dataset` | None | 写到 `job_start` |
| `argus_url`、`token` | env | 退回 `ARGUS_URL` / `ARGUS_TOKEN` |
| `experiment_type` | `"lightning"` | 转给 `Reporter` |
| `batch_prefix` | `"lightning"` | 自动生成 batch id 的前缀 |
| `train_loss_keys` | `("train_loss_epoch", "train_loss", "loss")` | 命中第一个 |
| `val_loss_keys` | `("val_loss_epoch", "val_loss")` | 命中第一个 |
| `final_metric_keys` | `()` | 在 `on_train_end` 读取，传给 `JobContext.metrics` |
| `heartbeat`、`stop_polling`、`resource_snapshot` | `False` | 想要时手动开 |
| `auto_upload_dirs` | None | 干净退出时上传目录里的图/PDF |

钩子覆盖：

| Lightning 钩子 | Argus 事件 |
|---|---|
| `on_train_start` | `Reporter.__enter__` + `JobContext.__enter__`（`batch_start` + `job_start`） |
| `on_train_epoch_end` | `JobContext.epoch`（`train_loss` + `lr`） |
| `on_validation_epoch_end` | `JobContext.epoch`（`val_loss`） |
| `on_train_end` | 干净退出；末轮指标通过 `JobContext.metrics` 落 |
| `on_exception` | 失败退出（`job_failed` + `batch_failed`） |

两个 epoch 钩子之间做了去重：Lightning 2.x 在同一个 epoch 内
`on_validation_epoch_end` 早于 `on_train_epoch_end`，适配器保证每个
epoch 只发一条 `job_epoch`。同时支持 `pytorch_lightning` 与
`lightning.pytorch` 命名空间，覆盖 Lightning 1.9+ 与 2.x。

## Keras

```python
from argus.integrations.keras import ArgusCallback

cb = ArgusCallback(
    project="my-paper",
    job_id="mnist_cnn",
    model="cnn-32-32-64",
    dataset="mnist",
    argus_url="http://localhost:8000",
    token="em_live_…",
    train_loss_keys=("loss",),                       # 默认
    val_loss_keys=("val_loss",),                     # 默认
    final_metric_keys=("accuracy", "val_accuracy"),  # 从最后一轮 logs 读
)
model.fit(x, y, epochs=10, callbacks=[cb])
```

构造参数与 Lightning 一致；区别只在 `experiment_type` / `batch_prefix`
默认值（`"keras"`）以及 `train_loss_keys` / `val_loss_keys` 是从 Keras 在
`on_epoch_end` 里传的 `logs` dict 里取的。

钩子覆盖：`on_train_begin` 同时打开 Reporter 与 JobContext；
`on_epoch_end` 发一条 `job_epoch`；`on_train_end` 末轮指标落库后干净
关闭两层上下文。Keras 没有 `on_exception` —— 析构里尽力发失败事件，
也可以在 `try/except` 里手动调 `cb.report_failure(exc)` 做确定性上报。

支持 Keras 3（`import keras`）与 TF 自带的 Keras 2。

## Hydra 一行接入

Hydra 驱动的项目最简洁的写法是在 `configs/config.yaml` 里加一段：

```yaml
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: my-paper
      experiment_type: forecast
```

`main.py` 不用改。设好 `ARGUS_URL` / `ARGUS_TOKEN`，每次
`python main.py …`（或 `-m` sweep）都会发出一个 batch、N 个 job。
详见 [Hydra 回调](../sdk/hydra-callback.md)。

## 崩溃续跑

跑很久的 sweep 中途挂了再启动时，用 `derive_batch_id` 让续跑落回
原来的同一个 Batch 行：

```python
from argus import Reporter, derive_batch_id

batch_id = derive_batch_id("my-paper", "dam_forecast")  # 走 git rev-parse HEAD
with Reporter(source_project="my-paper",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:
    ...
```

详见 [批次身份与续跑](../sdk/resume.md)。

## 接下来

- [Reporter API](../sdk/reporter.md) —— 构造函数完整参考。
- [Hydra 回调](../sdk/hydra-callback.md) —— 如果你用 Hydra 配置。
- [事件 Schema](../sdk/event-schema.md) —— 想了解线上传输细节。
- [批次身份与续跑](../sdk/resume.md) —— `derive_batch_id` + `resume_from`。
