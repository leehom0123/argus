# Argus

> 自托管的 ML 实验监测平台 —— 跟踪 batch、job、GPU/CPU 资源，支持重跑、超参搜索可视化。
> 实时仪表盘，多用户，一个容器。

名字来自希腊神话中的阿耳戈斯·潘诺普忒斯（Argus Panoptes，Ἄργος Πανόπτης）——那个长有百只眼睛的巨人，职责是守望他人所不能及之处。用这个名字，因为这个工具的工作正是替你睁开每一只眼睛。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-MkDocs%20Material-brightgreen.svg)](https://leehom0123.github.io/argus/zh/)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg)](#)
[![Node](https://img.shields.io/badge/node-%E2%89%A520-green.svg)](#)

[English](README.md) · [简体中文](README.zh-CN.md) · [文档站](https://leehom0123.github.io/argus/zh/)

---

## 一个熟悉的故事

23:00。你在实验室的工作站上提交了一个 32 组合的 Optuna 扫描，然后去
吃晚饭。02:00 心里不踏实，爬起来 SSH 进去看，发现：

- 8 张 GPU 里有两张已经 0% 利用率挂了 4 个小时 —— dataloader 卡死了，
  训练循环却没崩；
- 一个 trial 在 50 个 epoch 里跑到第 47 个 `CUDA out of memory` 死了，
  唯一的现场是一份 600 MB 的 `stdout.log`，你根本不想读；
- 重新跑那个 trial 时，结果落在一个 *新* 的 batch 上，原来的扫描历史
  被劈成了两行；
- 导师消息：「哪个 trial 最好，能看下 loss 曲线吗？」；
- PI 在群里：「`gpu-03` 现在谁在用？」。

Argus 就是作者被这种夜晚刺激到多次以后做的实验面板：一款开源的
实验追踪工具，面向跨多台机器跑长训练的研究团队。在训练脚本里插入
两行 `Reporter` SDK 调用，上面的问题不用 SSH 就能回答。

## 痛点 → 功能映射

| 02:00 的痛点 | Argus 的应对 |
|---|---|
| GPU 已经 0% 几个小时了，训练循环却没察觉 | **空闲检测器** —— 每 30 秒一条 `resource_snapshot`，连续 `ARGUS_IDLE_JOB_THRESHOLD_MIN`（默认 10）分钟 GPU 利用率 < 5% 就标红 |
| 训练脚本死了，Argus 还显示 *running* | **卡死批次检测器** —— `ARGUS_STALL_TIMEOUT_MIN`（默认 15）分钟无新事件就翻为 *stalled* |
| 续跑变成一个新 batch，扫描历史断成两行 | **崩溃续跑** —— `derive_batch_id(project, experiment, git_sha)` 返回稳定 id；传入 `Reporter(resume_from=…)`，续跑的事件会接到原来的 Batch 行上 |
| 在 600 MB 的 stdout 里翻找 traceback | **Job 详情页的 `log_line` 尾**；完整日志由 SSE 流式推送 |
| 「32 组合扫描里哪个 trial 最好？」 | **JobMatrix** —— 整张 `model × dataset` 矩阵上唯一一个全局最佳（绿边 + 奖杯）和唯一一个全局最差（红边 + 警告），按主指标计算；可一键 CSV 导出做论文表 |
| 「把 trial #17 的 loss 曲线发给我看」但对方没账号 | **Batch 级分享链接** —— 不透明 slug、只读、可撤销；也可以把项目可见性设为 public |
| 每个训练脚本写 200 行 W&B / CSV 胶水 | 两个 `with` 块就够了：`with Reporter(...)` 开一个 *batch*，`with r.job(...)` 开一个 *job*，`job.epoch(...)` 推指标；异常时自动发 `job_failed` |
| Optuna 扫描需要自己专门的可视化 | **Studies 页** —— 散点图、平行坐标、参数重要性，由 Sibyl 的 monitor 回调在 `job_start` 上挂的 `optuna.{study_name, trial_number}` 标签触发 |
| 「重跑那个失败 trial，可我在家、机器在实验室」 | 在已结束 batch 上点 **Rerun**，给源主机的 `argus-agent` 发 `kind=rerun`；agent 用记录的 `env_snapshot.command` 调 `subprocess.Popen` 重新拉起 |
| 「把这次跑停了，我改了一下超参」 | **Stop** 按钮翻一个标志位，循环里 `if job.stopped: break`（10 秒轮询读到）；SDK 没在跑时由 agent 发 `SIGTERM` |
| 共同作者都想收通知，但又各有偏好 | **按用户偏好**（batch 完成发邮件、job 失败发邮件、每日摘要）**+ 项目级多收件人**：项目把事件路由到一个收件人列表，再按每个用户的偏好过滤 |
| 「这次跑用的命令行去哪了？」 | `env_snapshot`（git SHA、命令、cwd、主机名）随 `batch_start` 记录；UI 上一键 *Copy command* |
| Hydra 训练流，想要零侵入式监控 | `argus.integrations.hydra.ArgusCallback` —— 在 `hydra.callbacks` 里挂一次，之后每次 `python main.py …`（含 `-m` 扫描）自动发批次 |
| Lightning / Keras 训练流，同上 | `argus.integrations.{lightning,keras}.ArgusCallback` 开箱即用 |
| 「我要部署到不通外网的机房」 | 单个 Docker 镜像（默认 FastAPI + Vue 3 + SQLite）。无 SaaS、无遥测；可选 PostgreSQL 多机部署。 |

## 快速上手（60 秒）

```bash
git clone https://github.com/leehom0123/argus.git
cd argus/deploy
cp .env.example .env

# 生成 JWT 密钥（ARGUS_ENV=prod 时强制 ≥32 字节）
python3 -c "import secrets; print('ARGUS_JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env

docker compose up -d --build
# 打开 http://localhost:8000 —— 首位注册的用户自动成为管理员。
```

然后在训练脚本里：

```python
from argus import Reporter

with Reporter("my-run",
              experiment_type="forecast",
              source_project="my-paper",
              n_total=1,
              monitor_url="http://localhost:8000",
              token="em_live_…") as r:                # token 在「设置 → Tokens」里生成
    with r.job("run-1", model="patchtst", dataset="etth1") as job:
        for epoch in range(50):
            # JobContext.epoch 有 4 个命名 Optional[float]：
            #   train_loss、val_loss、lr、batch_time_ms
            # 之后是 **extra —— 任意其它 keyword 会一并写到事件 payload，
            # 所以每个 epoch 想推多少个指标都行。
            job.epoch(epoch,
                      train_loss=..., val_loss=..., lr=..., batch_time_ms=...,
                      val_mse=..., val_rmse=..., val_mae=...,
                      val_r2=..., val_pcc=...)
        # Run 的最终汇总指标 —— 任意 dict[str, float]，会出现在 JobMatrix
        # 和 CSV 导出里。想上排行榜的指标都丢这里。
        job.metrics({
            "MSE":  ..., "RMSE": ...,
            "MAE":  ..., "R2":   ...,
            "PCC":  ...,
        })
```

或者设环境变量、省掉显式参数：

```bash
export ARGUS_URL=http://localhost:8000          # 注意 SDK 用 ARGUS_URL
export ARGUS_TOKEN=em_live_…
```

刷新仪表盘即可看到实时数据。

> **指标完全由你自己决定。** Argus 不绑定指标 schema：你传给
> `job.metrics({...})` 的 dict 会原样变成项目 leaderboard 上的列。
> 上面例子里的 `MSE`、`final_val_loss` 等只是约定写法，不是 Argus
> 强制要求的字段。参见
> [How-to：让训练出现在 Leaderboard 上](docs/zh/how-to/report-metrics-for-leaderboard.md)。

## 目录结构

```
argus/
├── backend/    FastAPI + 异步 SQLAlchemy 2.0 + Alembic（Python ≥3.10）
├── frontend/   Vue 3 + TypeScript + Vite + Pinia + Ant Design Vue（精确锁 4.2.6）
├── client/     argus-reporter SDK（PyPI: argus-reporter）
├── schemas/    事件契约（event_v1.json，schema v1.1）
├── deploy/     Docker compose、Dockerfile、nginx 片段、.env 模板
└── docs/       MkDocs Material 文档站（英文 + 简体中文）
```

## 主要特性

| 模块 | 内容 |
|---|---|
| **追踪** | Batch → Job → Epoch 三层结构；通过 `event_id` 实现幂等接收；网络断开时落盘到 JSONL，回放 |
| **实时 UI** | 每个页面单条多路复用 SSE 连接（`GET /api/sse`）；ECharts loss 曲线；GPU/CPU 走势图 |
| **认证** | 邮箱 + 密码（argon2id）；GitHub OAuth（可选）；JWT 双密钥轮换 |
| **令牌** | SDK 用 `em_live_*`，Agent 用 `ag_live_*`，按用户绑定 |
| **执行器** | 后端提供 `/api/agents/*`。`argus-agent` 守护进程随 **Sibyl 包** 发布；通过 `subprocess.Popen` 重跑、SIGTERM 停止 |
| **Study** | 通过 Sibyl 的 monitor 回调在事件上挂 `optuna.{study_name, trial_number}` 标签触发 |
| **通知** | 按用户的邮件偏好 + 项目级多收件人路由；SMTP 投递 |
| **运行期配置** | GitHub OAuth、SMTP、保留策略、演示项目、特性开关在 UI 即改即生效 |
| **国际化** | 完整英文 + 简体中文 UI |
| **训练框架** | 一等公民支持 PyTorch Lightning、Keras、Hydra 回调（`argus.integrations.{lightning,keras,hydra}`） |
| **续跑** | `derive_batch_id(...)` + `Reporter(resume_from=...)`，同一实验跨重启落到同一 Batch |

## 文档导航

| | |
|---|---|
| 🚀 [快速开始](docs/zh/getting-started/installation.md) | 安装、首次运行、发送第一个事件 |
| 📖 [使用指南](docs/zh/user-guide/dashboard.md) | 仪表盘、batch、job、分享、通知 |
| 🐍 [SDK 参考](docs/zh/sdk/reporter.md) | `Reporter` API、Hydra 回调、批次身份与续跑、事件 schema |
| 🛠 [运维](docs/zh/ops/docker.md) | Docker、配置、管理员设置、Agent、数据库、保留策略 |
| 🏗 [架构概览](docs/zh/architecture-overview.md) | 各模块如何配合 |
| 📝 [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md) | |

## 许可

Apache-2.0，详见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。

学术使用请按 [`CITATION.cff`](CITATION.cff) 的方式引用。
