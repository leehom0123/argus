# 批次身份与崩溃续跑

跑十数据集 × 十二模型这种几天的 GPU benchmark，最容易在某个周二
下午意外死机。`argus` SDK 让你**用同一个逻辑批次接着跑**，崩溃前的
部分结果就和崩溃后的部分结果合并到同一个 Batch 行，而不是分裂成两个。

本页讲三种使用模式、线上语义、以及让 resume 安全的两条后端保证。

## 三种使用模式

### 1. 自动派生（推荐默认）

```python
from argus import Reporter, derive_batch_id

batch_id = derive_batch_id(
    project="my-bench",
    experiment_name="dam_forecast",
    # git_sha=None → 调 `git rev-parse HEAD`；没装 git 时回退到 "no-git"。
)

with Reporter(batch_prefix="bench",
              source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:
    ...
```

`derive_batch_id` 把 `(project, experiment_name, git_sha)` 三元组哈希成
形如 `bench-<16 hex>` 的确定性 id。**同一个 checkout 重跑同一个实验得到
相同的 id**，崩溃后的 events 就落到同一个 Batch 行。

切换 git commit 后 id 自动改变 —— 这正是你想要的，不同 commit 本来就
应该是不同的实验。

### 2. 显式指定

```python
with Reporter(source_project="demo",
              n_total=4,
              batch_id="my-paper-table-1-bench") as r:
    ...
```

需要可读的 id、或一批活儿要拆给好几台机器跑（CI / 多节点）时用这种。

### 3. 崩溃后续跑

```python
with Reporter(source_project="demo",
              n_total=120,
              resume_from="bench-abcdef0123456789") as r:
    ...
```

`resume_from` 是 `batch_id` 的别名 —— 线上效果一模一样，只是这个名字
让"我在续跑"这个意图在代码里显得更明确。把崩了的那次 launcher 打的
id（或从后端 `/batches` 列表里捞回来的）传进来，后续 events 全部追加
到原批次。

如果同时传了 `batch_id` 和 `resume_from`，`batch_id` 优先。

## 端到端 崩溃 + 续跑 演示

```python
# 周二 09:00 —— 第一次跑。
batch_id = derive_batch_id("my-bench", "dam_forecast")
print(f"running batch {batch_id}")  # bench-abcdef0123456789

with Reporter(source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:
    for job in plan.jobs:
        with r.job(job.id, model=job.model, dataset=job.dataset) as j:
            train_one(j)
        # ↑ 跑完 47 个 job 时机器死机。
```

Launcher 给 `bench-abcdef0123456789` 发了 `batch_start`，跑完 47 个
job，然后挂了。后端那边 Batch 行保留 `status=running`（没收到
`batch_done`）；`_handle_batch_start` 侧效果处理（见
[`backend/api/events.py`](https://github.com/argus-ai/argus/blob/main/backend/backend/api/events.py)）
把原始 `start_time` 留住等续跑。

```python
# 周二 14:00 —— 重启续跑。
batch_id = derive_batch_id("my-bench", "dam_forecast")
# 同 checkout → 同 id（不用记下 id）。
with Reporter(source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              resume_from=batch_id) as r:
    for job in plan.remaining_jobs():  # 还没跑的那 73 个
        with r.job(job.id, model=job.model, dataset=job.dataset) as j:
            train_one(j)
```

第二次启动还是给同一个 id 发 `batch_start`。后端识别出已有行，刷新
可变字段（最新 command、最新 n_total），把 `status` 翻回 `running`，
**`start_time` 保留原值不动**。续跑产生的 job events 全部追加到原批次
—— 最后 `/batches/<id>/jobs` 看到的就是同一行底下的全部 120 个 job。

## 后端保证

Resume 合约依赖 `POST /api/events` 的三条幂等性：

1. **对已存在 batch_id 发 `batch_start` 是 OK 的，不会 409。**
   Handler 更新可变 metadata（command、n_total、project），返回
   200 + `accepted=true`。

2. **续跑时保留原始 `start_time`。** 后续的 `batch_start` 事件刷新
   状态但永远不会改起点时间戳 —— 历史 timing 不会因为续跑就改写。

3. **已经 `done` 的批次不会被翻回去。** 幂等重跑安全网：再启动一个
   已经完成的批次不会让它的 status 倒退回 `running`。真要重跑请用
   不同的 id。

对应的后端测试在
[`test_resume_appends_to_existing_batch.py`](https://github.com/argus-ai/argus/blob/main/backend/backend/tests/test_resume_appends_to_existing_batch.py)。

## Launcher 侧的输出目录约定

Argus 只管 metadata；磁盘怎么写归各 launcher 自己。推荐约定（sibyl
forecast pipeline 用的）是 `outputs/<task>/` 下两层子树：

```
outputs/forecast/<batch_id>/
├── <experiment_1>/
│   ├── checkpoints/
│   ├── leaderboard.csv
│   └── COMPLETED
├── <experiment_2>/
└── ...
```

用同一个 `batch_id` 续跑就直接复用同一个父目录，文件级 resume 机制
（如 `checkpoint_last.pt`）和磁盘上的 leaderboard 都能继续工作，不需要
任何迁移。

## API 参考

`derive_batch_id` 从包根直接导出：

::: argus.derive_batch_id
    options:
      show_signature: true
      show_root_heading: true

`Reporter` 的 `batch_id` / `resume_from` 关键字参数文档在
[`reporter.md`](reporter.md)。
