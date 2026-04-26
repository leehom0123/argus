# 仪表盘

首页是日常总览。它是只读的，每隔几秒短轮询刷新一次 —— 不依赖
WebSocket，所以在严苛的公司代理后面也能用。

## 顶部小卡片

- **Running batches** —— 状态为 `running` 的批次数。点进去就是按
  状态过滤好的批次列表。
- **GPU hours (24 h)** —— 所有主机上 `gpu_util_pct × 时间` 的累计，
  来自 `resource_snapshot` 事件。
- **Active hosts** —— 最近一小时内至少发过一次快照的主机，配最新的
  GPU 与 RAM 百分比。
- **Recent failures** —— 最近 5 条 `batch_failed` / `job_failed`。

每张卡片点击后会展开成详细表格。

## 活跃批次表

中间这段列出每个运行中的批次：

| 列 | 来源 |
| --- | --- |
| Batch | `batch_id`（可点击） |
| Project | `source.project` |
| Type | `data.experiment_type`（如 `forecast`） |
| Progress | `n_done + n_failed` 比 `n_total` |
| Started | `batch_start.timestamp` |
| Owner | 持有 reporter token 的账号 |

进度条会随着 `job_done` 事件不断推进。

## 实时损失图块

只要有任何任务在跑，仪表盘右侧会固定一个小型损失曲线预览。它选的
是最近活跃的那个 job，画 `train_loss` 和 `val_loss` 随 `epoch`
的变化。点这个图块跳到完整的
[任务详情页](projects-batches.md)。

## 过滤与时间范围

顶部条上有：

- **时间范围** —— 最近 1 小时 / 24 小时 / 7 天 / 自定义。影响所有
  图块和主机面板，但不影响批次列表。
- **项目过滤** —— 给只盯一条流水线的用户的快速下拉。
- **Mine / All** —— 管理员默认看全部；切到 Mine 只看自己 token
  发的。

## 自定义

- **置顶批次** —— 行末的星号图标会把这条批次永久置顶到列表上方，
  跨 session 保留。
- **主题** —— 顶部条的明暗切换；按用户保存选择。
- **语言** —— 英文 / 简体中文，同一个工具条上切。文档站的语言
  也跟着切。

## 空状态

刚装好、还没有任何事件时，会显示一个引导面板，指向
*Settings → Tokens* 和 *Getting started → First run*。第一条
`batch_start` 进来后，引导面板就消失了。
