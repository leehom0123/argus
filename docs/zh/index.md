# Argus 实验监测平台

> 自托管的机器学习实验监测平台 —— 批次、任务、GPU/CPU 资源、实时面板。
> 采用 Apache-2.0 许可证。
>
> *SDK 在 PyPI 上以 `argus-reporter` 发布。*

## 它解决什么问题

Argus 是一个面向多用户的网页面板，用来跟踪跨多台机器的
长时间机器学习实验。一个轻量的 Python SDK（`argus-reporter`）
从训练脚本中推送事件 —— 批次开始、每个 epoch 的损失、GPU 利用率、
产物文件等等 —— 后端用 FastAPI 接收，并把这些数据实时推送到 Vue 3
面板上。

它针对的场景非常具体：研究员在一台远程工作站上启动一个 32 组合的
sweep 跑一晚上，第二天早上想知道哪些任务收敛了、哪些崩溃了、GPU
用量曲线长什么样 —— 而不必每次都从零写一套日志胶水。

## 为什么再造一个轮子

主流工具（Weights & Biases、MLflow、Aim、Comet）都能用，但每一个
至少在以下某一点上让人不舒服：

- **不依赖付费层即可自托管** —— 单镜像 Docker，默认 SQLite，
  按需切到 PostgreSQL。
- **双语界面** —— 英文 / 简体中文，按用户切换。
- **为 sweep 而生，而不是为单跑** —— 一等公民的
  *batch → jobs → epochs* 层次，配套任务矩阵视图。
- **sweep 维度的通知** —— 一个 sweep 跑完发一次飞书 / 邮件，而不是
  每个 epoch 都轰你一下。
- **Apache-2.0 + 极小依赖树** —— 没有厂商锁定，方便审计和嵌入。

## 一眼看懂

| 组件 | 技术栈 | 位置 |
| --- | --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.x async | `backend/` |
| 前端 | Vue 3 + Vite + ant-design-vue | `frontend/` |
| SDK | 纯 Python，不依赖 PyTorch | `client/argus/` |
| 数据库 | SQLite（默认）或 PostgreSQL | `data/argus.db` / `ARGUS_DB_URL` |
| 部署 | 单镜像 Docker | `deploy/docker-compose.yml` |
| Schema | JSON Schema v1.1 | `schemas/event_v1.json` |

## 下一步去哪

- 第一次用？从 [安装](getting-started/installation.md) 开始 —— Docker
  路径大约 1 分钟搞定。
- 已经跑起来了？直接看
  [接入训练任务](getting-started/connect-training.md)。
- 上生产环境？看 [Docker 部署](ops/docker.md) 与
  [数据库（SQLite vs Postgres）](ops/database.md)。
- 想搞清楚内部结构？读
  [架构概览](architecture-overview.md)。
