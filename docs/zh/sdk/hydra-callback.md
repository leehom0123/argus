# Hydra 回调

如果你的实验是用 [Hydra](https://hydra.cc/) 跑的，把 `argus-reporter`
接成一个 Hydra callback，每次运行自动开一个批次，业务脚本里完全不用
写 SDK 代码。

`argus-reporter` 现在内置了一等公民的适配器：
[`argus.integrations.hydra.ArgusCallback`](../sdk/integrations.md#hydra)。

## 安装

```bash
pip install 'argus-reporter[hydra]'
```

## 接入

```yaml
# configs/config.yaml
defaults:
  - _self_

experiment_name: dam_forecast

hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: deepts-flow
      experiment_type: forecast
```

`main.py` 完全不动，鉴权 env 设一次：

```bash
export ARGUS_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_...
```

仅此而已 —— 之后每次 `python main.py experiment=...` 都会向面板发一个
批次。

## 单跑 vs Multirun

适配器自动识别 Hydra 模式：

* `python main.py` → 发出 **1 批次 + 1 job**。
  * `on_run_start` 开 `Reporter`。
  * `on_job_start` / `on_job_end` 开关那唯一一个 `JobContext`
    （`job_id` 默认是 `HydraConfig.job.num`，即 `"0"`）。
  * `on_run_end` 关 `Reporter`。
* `python main.py -m experiment=a,b,c` → 发出 **1 批次 + N jobs**。
  * `on_multirun_start` 开 `Reporter`。
  * 每个 Hydra trial 触发一对 `on_job_start` / `on_job_end`，每个 trial
    一个 `JobContext`，`job_id` 从 `HydraConfig.job.num` 拿。
  * `on_multirun_end` 关 `Reporter`。

`task_function`（`@hydra.main` 包的函数）抛异常时，Hydra 会把
`JobReturn.status` 置为 `FAILED`，适配器把它传给 `JobContext.__exit__`，
面板上就会看到 `job_failed`。

## 自定义 job_id

默认 `job_id` 是 `str(HydraConfig.job.num)`。覆盖方式：

```yaml
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: deepts-flow
      job_id_template: "{experiment_name}-{job_num}"   # "dam_forecast-0"
      # 或者：job_id_key: experiment_name              # 直接用 cfg.experiment_name
```

## 在训练代码里

callback 已经设过全局 batch id，所以你不用持有 `Reporter` 引用，也能用
模块级 `emit`：

```python
from argus import emit

emit("log_line", line="loaded 32 batches", level="info")
```

这在某个模型文件深处加 per-step 诊断时很方便，不必把 reporter 句柄一路
传过去。

## 注意事项

- Hydra callback 在父进程里跑。如果你的 sweep 用 Submitit / Ray 起
  worker，每个 worker 需要自己的 Reporter —— 父进程的 callback 只开了
  那个伞批次。
- Callback 不会处理 `on_run_start` 之前抛出的异常（比如配置无效）。
  Hydra 仍会把这些写到磁盘日志，只是面板上看不到。

## 自定义 callback（fallback）

如果内置适配器满足不了需求（比如自定义失败路由、一次 run 多批次、对接
Submitit），可以把下面的 ~30 行骨架抄进自己项目里改。内置适配器就是
这个 pattern，加上 `__new__` shim、env 兜底、multirun 模式识别、安全
加固。

```python
# my_project/callbacks/monitor.py
from hydra.experimental.callback import Callback
from argus import Reporter, set_batch_id


class MonitorCallback(Callback):
    """整次 run 用一个 Reporter 包起来的 Hydra 回调。"""

    def __init__(self, project: str, experiment_type: str = "forecast"):
        self.project = project
        self.experiment_type = experiment_type
        self._reporter = None
        self._job = None

    def on_run_start(self, config, **kwargs):
        self._reporter = Reporter(
            batch_prefix=config.experiment_name,
            experiment_type=self.experiment_type,
            source_project=self.project,
        ).__enter__()
        set_batch_id(self._reporter.batch_id)

    def on_job_start(self, config, **kwargs):
        from hydra.core.hydra_config import HydraConfig
        job_id = str(HydraConfig.get().job.num)
        self._job = self._reporter.job(job_id).__enter__()

    def on_job_end(self, config, job_return=None, **kwargs):
        if self._job is not None:
            self._job.__exit__(None, None, None)
            self._job = None

    def on_run_end(self, config, **kwargs):
        if self._reporter is not None:
            self._reporter.__exit__(None, None, None)
            self._reporter = None
```
