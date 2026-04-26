# 让你的训练出现在 Leaderboard 上

Argus 自己不算指标。Leaderboard 直接把训练代码塞进 `job_done`
事件里 `data.metrics` 字段的内容拿出来用。**你看到的列，是该项目下所有
job 历史上报过的所有 key 取并集得来的。** 这是有意为之 —— Argus
不绑定任务类型，所以指标的命名属于训练侧的约定，不属于 Argus 的 schema。

## 一个 job 至少要发哪几条事件

| 步骤 | 事件 | 何时 |
|---|---|---|
| 1 | `batch_start` | 一次 benchmark / 单次跑开始时 |
| 2 | `job_start`   | 每个 (model x dataset x seed) job 开始时 |
| 3 | `job_epoch` × N | 每个 epoch —— 用来画 loss 曲线，**不进 leaderboard** |
| 4 | `job_done`    | test() 收尾时 —— **决定 leaderboard 行的就是它** |
| 5 | `batch_done`  | 所有 job 收尾后 |

只有第 4 步影响 leaderboard。其余四步分别驱动状态徽章、loss 曲线、ETA。

## `job_done.data.metrics` 里能放什么

随你放。Argus 把这个 dict 原样存到 `Job.metrics`（JSON 列），
leaderboard 接口在查询时把每个 key 读出来，作为 UI 表格列和 CSV
导出的一列。**加一个 key，多一列**。

平台不会校验指标名 —— `MSE`、`accuracy`、`bleu`、`dice_score`、
`wall_clock_seconds`，啥都行。下面截图里出现的那一串列名，是时序
预测和推理基准这两条工作流自己的约定，**不是** Argus 的硬性要求。

## 时序预测工作流采用的命名约定

如果你想直接复用时序排行版界面，不另外写视图，按下面的名字命名指标，
对应的值就会落在 UI 期望的那一列：

**质量指标**：`MSE`、`MAE`、`RMSE`、`R2`、`PCC`、`sMAPE`、
`MAPE`、`MASE`、`RAE`、`MSPE`

**吞吐 / 延时**：`Latency_P50`、`Latency_P95`、`Latency_P99`、
`Inference_Throughput`、`Inference_Time_Per_Sample`、
`Total_Inference_Time`、`Samples_Per_Second`

**算力开销**：`GPU_Memory`、`GPU_Memory_Peak`、
`GPU_Utilization`、`CPU_Memory`、`CPU_Utilization`、
`Total_Train_Time`、`Avg_Epoch_Time`、`Avg_Batch_Time`、
`Total_Batches`

**模型元信息**：`Model_Params`、`Model_Size`、`seed`

这些名字没有任何魔法。它们只是我们参考用的那套时序训练器恰好写出来
的 key。换你自己的就好，会和上面这些并排在 UI 里出现。

## 完整可跑的示例

`client/examples/leaderboard_full_demo.py` 是一段不到 80 行的演示，
完整发了上面五种事件。在任意 Argus 实例上跑：

```bash
export ARGUS_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_xxxxxxxxxxxxx
python client/examples/leaderboard_full_demo.py
```

然后刷新 `leaderboard-demo` 项目的 leaderboard 标签页，就能看到这个
demo job。

如果想看清楚事件长什么样、又不想真的 POST 出去，加 `--dry-run`：

```bash
python client/examples/leaderboard_full_demo.py --dry-run
```

## 常见错误

- 忘了发 `job_done`：jobs 列表里能看到这条记录，但 leaderboard 上
  永远不会出现。Leaderboard 只排状态为 `done` 的 job，状态由
  `job_done` 设置。
- 把指标塞到了 `job_epoch` 里而不是 `job_done`：epoch 指标进 loss
  曲线，不进 leaderboard。最终的 test 指标必须在 `job_done` 里给。
- 单位不固定。`Latency_P50` 习惯上是秒不是毫秒；`GPU_Memory`
  习惯上是 MB。UI 默认按这套单位画 sparkline，所以同一个 key
  请固定一种单位。

## 自定义指标和排序

只要某个 key 在任何一个 job 的 `job_done.metrics` 里出现过，它就
可以成为排序列。在 leaderboard 上点列头即可排序，平台侧无需做任何
schema 扩展。

Leaderboard 接口默认按 `MSE` 排序；矩阵视图允许在
[项目和批次](../user-guide/projects-batches.md) 的下拉里切换当前用
哪个指标。非时序项目就挑你实际上报的某个指标当排序列即可 —— 行的
内容是一样的，只是顺序变了。
