# 任务详情

Job 详情页是 项目 → 批次 → job 层级里的最末叶子。从
[任务矩阵](job-matrix.md) 任意单元格、[任务列表](jobs-list.md)
任意一行、或批次详情页 *Jobs* 标签都能打开。

## 遥测条

遥测条是首屏第一眼看到的。五个等宽单元，job 在跑时每隔几秒
刷新：

| 单元 | 来源 | 说明 |
| --- | --- | --- |
| **状态** | `job.status` | 五色 pill（#125）。`failed` 时悬停看失败原因。 |
| **已用** | `now - job_start.timestamp`（结束后 `job_done - job_start`） | 跑着时是实时秒表。 |
| **GPU util** | 最新 `resource_snapshot.gpu_util_pct` | 数值下面有 sparkline，覆盖最近 30 分钟。 |
| **GPU mem peak** | `max(resource_snapshot.gpu_mem_mb)` | 按 job 重置。 |
| **最近 loss** | 最近一次 `job_epoch` 的 `train_loss` 和 `val_loss` | 双行 —— train 在上 val 在下。 |

单元只读。点任意单元就把对应详细区滚动到视野（状态 → 状态面板，
已用 → 时间线，GPU util → 资源页，loss → 损失曲线）。

### 布局

遥测条在 `lg+` 屏上是 2 列网格（左半状态+已用，右半三个资源
单元），到 `md` 及以下退化成一行可横向滚动。手机用户也能看到
全部五个值（不必横滚）—— 用紧凑文本格式（`12m`、`73%`、
`8.2 GB` 等）。

## 嵌入日志尾

遥测条下面是一个 30 行的 `log_line` 尾，短轮询刷新。日志框带：

- **暂停 / 继续** 按钮，看的时候冻结自动滚屏。
- **过滤** 输入框，隐藏不含子串的行（不区分大小写）。
- **完整日志** 链接，在新标签页打开 SSE 流式日志端点。

它替代了原来的 *Logs* tab，省一次点击。长跑训练的 job，嵌入式
日志让这页变成一个一屏监控视图。

## Tab

日志嵌入下面三个 tab 覆盖更深的细节：

1. **Metrics** —— `job_done.data.metrics` 的完整表。点任意数值
   列头可以画图。
2. **Resources** —— GPU / CPU / RAM / 磁盘曲线，来自
   `resource_snapshot` 合并。跟遥测条 sparkline 是同一份数据。
3. **Artifacts** —— `j.upload(...)` 上传的图和报告。单击预览，
   双击下载。

原 *Logs* tab 没了 —— 嵌入式尾已经覆盖。原 *Telemetry* tab 也
没了 —— 遥测条覆盖。两个最常看的面板各省一次点击。

## 动作条

页底一排四个按钮：

| 按钮 | 何时可用 | 效果 |
| --- | --- | --- |
| **Stop** | `running` / `stalled` | 发 `stop_requested`，reporter 大约 10 秒内拉到。 |
| **Rerun** | `done` / `failed` / `cancelled` | 起一个新的批次行，`source_batch_id` 指向原批次；主机上的 `argus-agent` 拉到任务后起原命令。详见 [Argus Agent](../ops/argus-agent.md)。 |
| **Share** | 总是可用 | 打开分享菜单（公开链接、项目成员）。详见 [分享](sharing.md)。 |
| **Copy command** | 总是可用 | 把记录的 `env_snapshot.command` 复制到剪贴板。主机上没有 agent 时手动起命令很有用。 |

Stop 跟 Rerun 互斥：跑着的 job 只有 Stop 可用；跑完的 job 只有
Rerun 可用。Share 和 Copy 永远在线。

### 状态徽章动画

遥测条第一格的状态 pill 使用统一的五色调色板：

| 状态 | 颜色 | 动画 |
| --- | --- | --- |
| running | 绿 | 跳动点 |
| stalled | 琥珀 | 静态 |
| done | 灰绿 | 静态 |
| failed | 红 | 静态 |
| cancelled | 灰 | 静态 |

    批次列表、job 列表、watchdog 抽屉）含义一致。工单和外部博客
    里的旧截图仍然显示蓝色 running pill —— 那是历史，不是 bug。

## 键盘快捷键

| 键 | 动作 |
| --- | --- |
| `s` | 聚焦 Stop / Rerun（按状态可用的那个） |
| `r` | 聚焦 Rerun（终态时的别名） |
| `l` | 聚焦日志过滤输入框 |
| `j` / `k` | 同批次内下一个 / 上一个 job |
| `b` | 跳回父批次详情 |

文本输入框聚焦时快捷键禁用，所以在日志过滤里打字不会误触发。

## 相关阅读

- [任务矩阵](job-matrix.md) —— 透视视图，从这里打开单个 job 详情。
- [任务列表](jobs-list.md) —— 全局拍平列表。
- [Argus Agent](../ops/argus-agent.md) —— Rerun 按钮真的能在主机
  上起进程的关键。
