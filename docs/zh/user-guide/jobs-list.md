# 任务列表

全局 **`/jobs`** 页提供跨 sweep 的扁平、可过滤 job 列表。

## 什么时候用

- *"过去 24 小时所有项目里有哪些 job 失败了？"*
- *"`gpu-01` 上当前在跑的所有 job。"*
- *"过去一周所有打了 `dataset=ETTh1` 标的 job。"*

批次详情页仍然是想看某条特定 sweep 时的正解。`/jobs` 页是给
那些拍平列表更顺手的横切查询用的。

## 过滤器

顶部条有六个过滤器，全部按 AND 组合：

| 过滤器 | 来源 | 说明 |
| --- | --- | --- |
| 状态 | `job.status` | `running` / `done` / `failed` / `cancelled` / `stalled` 之一。五色调色板（#125）也在这里显示。 |
| 项目 | `source.project` | 单选。下拉里只有你能看到的项目。 |
| 主机 | `source.host` | 单选。 |
| 批次 | `batch_id` 子串 | 不用离开列表就能限定到某条 sweep。 |
| 标签 | `model` / `dataset` / 自由标签子串 | 多个值用逗号分隔，含义是 OR。 |
| Since | 时间简写（见下） | 以 *现在* 为锚点。 |

### `since` 简写

URL 接收 `since=` 查询参数表示时间窗：

| Token | 含义 |
| --- | --- |
| `30m` | 最近 30 分钟 |
| `24h` | 最近 24 小时 |
| `7d` | 最近 7 天 |
| `30d` | 最近 30 天 |

例：`/jobs?since=24h&status=failed` —— 过去一天里所有失败的
job。

绝对范围也可以粘贴长格式
`?from=2026-04-25T00:00:00Z&to=…`；动日期选择器时 URL 会跟着
更新。

## 列

| 列 | 来源 |
| --- | --- |
| 状态 | 五色 pill（#125 把 running 从蓝改绿；见下面迁移注释） |
| Job | `job_id`，点进去到 job 详情 |
| Batch | `batch_id`，点进去到批次详情 |
| Project | `source.project` |
| Host | `source.host` |
| Started | `job_start.timestamp`，相对时间 |
| Duration | `job_done.timestamp - job_start.timestamp`；`running` 显示实时累计 |
| Latest loss | 最近一次 `job_epoch` 的 `train_loss` / `val_loss` |
| Tags | `model` / `dataset` chip，或自由标签 |

点列头排序，默认按 *Started 降序*。

## 权限

Jobs 页跟平台其他地方一样，遵守项目可见性：

- **管理员** 看所有项目的所有 job。
- **非管理员** 只看自己拥有或参与的项目里的 job。看不到的项目
  里的 job 在渲染前就被过滤掉 —— 不会有一行 "权限不足"。
- 通过 [分享](sharing.md) 公开的 job 在 `/jobs` 上对未登录用户
  **不可见**。这页必须登录才能进。

## 来自仪表盘的跳转

仪表盘的小卡片和活跃批次表现在会深链接到过滤好的 jobs
列表（#118）：

| 卡片 | 跳转目标 |
| --- | --- |
| **Running batches** 计数 | `/jobs?status=running` |
| **Recent failures** 卡片 | `/jobs?status=failed&since=24h` |
| **Active hosts → 主机名** | `/jobs?host=<name>&status=running` |
| **Recent activity → 点某行** | `/jobs?batch=<batch_id>` |

跳转是 *追加* 模式 —— 如果你已经有过滤条件，卡片会叠加上去
而不是覆盖。

## 空状态

当前过滤条件没有结果时，列表显示一句简短说明和一个 *清空过滤*
按钮。最常见的原因是忘了 `since` 第一次进来默认是 24h。

## 导出

顶部条的 *Export CSV* 按钮把当前过滤结果按所有可见列导出。
"把昨晚失败的 job 发给同事" 用这个按钮一次就完事，不必一个个
打开。

## 永久链接

`/jobs` 上的每个 URL 都是永久链接。把
`/jobs?status=failed&since=7d` 加书签，就有了一个一键查看
"上周所有失败" 的 triage 视图。
