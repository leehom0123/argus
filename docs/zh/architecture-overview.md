# 架构

从高层视角走一遍系统形状。模块级设计文档参见仓库 `docs/` 下的旧文件
[`architecture.md`](https://github.com/leehom0123/argus/blob/main/docs/architecture.md)
和 `design.md`。

## 系统图

```text
   +-----------------------+        +----------------------+
   | 训练脚本               |        | 训练脚本               |
   | (gpu-01)              |        | (gpu-02)              |
   |                       |        |                       |
   |  with Reporter(...)   |        |  with Reporter(...)   |
   |    j.epoch(...)       |        |    j.epoch(...)       |
   +----------+------------+        +-----------+----------+
              |                                 |
              |  POST /api/events (JSON, v1.1)  |
              |  Authorization: Bearer em_live_…|
              v                                 v
        +-----------------------------------------------+
        |  FastAPI 后端（uvicorn，1 进程）                 |
        |   - 鉴权（JWT 给用户，em_live_ 给 SDK）          |
        |   - 按 token 限速                              |
        |   - schema_version=1.1，event_id 幂等           |
        |   - 落库 + 对 batch / job 做副作用              |
        |   - 通知派发（asyncio.create_task）             |
        +-----------------------+-----------------------+
                                |
                       SQLAlchemy 2.x async
                                |
                                v
                +-----------------------------+
                | SQLite（默认）              |
                |   monitor.db                |
                | -- 或 --                    |
                | PostgreSQL                  |
                +-----------------------------+
                                ^
                                |
                          短轮询 GET
                                |
                +-----------------------------+
                | Vue 3 + ant-design-vue UI   |
                |   /api/dashboard/*          |
                |   /api/batches/*            |
                |   /api/jobs/*               |
                |   /api/hosts/*              |
                +-----------------------------+
                       浏览器（en / zh）
```

## 三层

### 1. SDK（Python）

`client/argus/` 是 `requests` 之上的薄层。每个公共
方法都把字典塞进一个有界队列。一个守护 worker 把队列里的事件
POST 到 `/api/events`（单条）或 `/api/events/batch`（待发条数 ≥ 20
时）。失败会指数退避，3 次以后 spill 到
`~/.argus-reporter/*.jsonl`，下次启动重放。

`Reporter` 打开期间还有三个守护线程：

- **心跳**（每 5 分钟一条 `log_line`），让长跑的 inference / SHAP
  回调不会触发卡死检测。
- **停止信号轮询**（每 10 秒 `GET
  /api/batches/<id>/stop-requested`），让用户点击的 *Stop* 能传到
  训练代码。
- **资源快照**（每 30 秒），通过 `pynvml` + `psutil` 收集 GPU /
  CPU / RAM / 磁盘读数。

### 2. 后端（FastAPI + SQLAlchemy）

`backend/backend/api/` 一个资源一个文件：`events.py`、`batches.py`、
`jobs.py` 等等。唯一的写入路径是 `POST /api/events`：

1. 鉴权 token（`em_live_` 前缀）。
2. 按 token 限速。
3. 校验信封形状（`EventIn` Pydantic 模型）。
4. 强制 `schema_version == "1.1"`。
5. 幂等：`event_id` 已存在则短路返回 200 + 原始 `db_id`。
6. 校验 `data` payload 的类型化模型。
7. 落原始 event 行。
8. 对 `batch` / `job` / `resource_snapshot` 摘要表做副作用。
9. commit。
10. 通过 `asyncio.create_task` 派发匹配的通知规则，让 HTTP 响应
    尽快返回。

读路径基本都是聚合查询 —— 面板从不把原始事件流给浏览器。

### 3. 前端（Vue 3）

`frontend/src/` 是标准的 Vue 3 + Vite 应用：

- `pages/` —— 每个路由一个组件（`Dashboard.vue`、`BatchList.vue`、
  `BatchDetail.vue`、`JobDetail.vue` 等）
- `api/` —— 对每个后端接口的类型化 `fetch` 封装
- `store/` —— Pinia store：鉴权、locale、用户偏好
- `i18n/` —— 英文与简体中文的 locale 文件

构建产物由 FastAPI 的 `StaticFiles` 挂载，所以一个容器同源服务
API 与 UI —— 生产环境完全没有 CORS 问题。

## 为什么是这种形状

- **一个进程，一个镜像** —— 本地开发的研究员可以一句
  `docker compose up` 把整套栈拉起来。不需要外置 Redis、不需要
  Kafka。
- **幂等上报** —— `event_id` + spill 文件意味着 SDK 不会因为
  网络抖动而静默丢事件。
- **以聚合为先的读路径** —— 即使原始事件被保留期清掉，面板仍然
  靠摘要表正常工作。
- **自托管友好** —— Apache-2.0、零遥测、不挂任何 SaaS 钩子。
