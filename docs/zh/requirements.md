> 🌐 [English](./requirements.md) · **中文**

# Argus — 产品需求文档 (PRD) v1

> 本文档是 MVP 的唯一真源。所有后端 / 前端 / 客户端实现以此为准。
> 有疑问先改这里，再改代码。

## 1. 项目定位

一个独立的、面向 ML 研究的实验监控 WebUI + 事件接入服务。

- **服务对象**：研究组/个人，多用户 + 协作
- **接入对象**：任何实验平台（MVP 优先 DeepTS-Flow-Wheat，未来任意 Python 训练框架）
- **核心价值**：跑 benchmark 时不用盯 tmux，手机 / 浏览器都能看；做好的结果能一键分享给合作者看，无需分享服务器权限

## 2. 范围

### 2.1 In scope（MVP）

1. 用户注册 / 登录 / 登出（**开放注册**，无需审批）
2. **首个注册用户自动成为 admin**
3. Personal API token：用户在 UI 自助生成多个 token，用于实验端 POST events
4. 多用户数据隔离：每个 batch 默认只属主自己可见
5. 共享三种粒度：
   - **Project-level share**：把我的整个 project 共享给某 user（TA 自动看到我所有 batch）
   - **Batch-level share**：单个 batch 共享给一到多个 user
   - **Public link**：生成只读 URL，任何人点开不需登录可看（论文图复现用）
6. 权限两级：`viewer` / `editor`
7. Web UI：登录/注册页、Batch 列表（My / Shared / All）、Batch 详情（matrix + timeline）、Job 详情（loss 曲线+指标+日志）、Host 资源时序、Settings（Profile / Tokens / Shares）
8. Email 发送：注册验证 + 密码重置 + 可选通知（MVP 出注册验证够）
9. 通知规则引擎：服务端按规则推 Feishu / (可扩展) WhatsApp / Slack / ...
10. 通信协议：HTTP REST 上行、HTTP polling + SSE 下行（见 §6）
11. OAuth **扩展点**：backend `AuthProvider` 抽象接口，预留将来接 GitHub/Google；MVP 不实现

### 2.2 Out of scope（Phase 2 及以后）

- OAuth provider（GitHub/Google/内网 LDAP）实际接入
- Teams / Organizations（用户属于组织，组织级共享）
- Per-user 通知路由（每用户独立 Feishu webhook）
- 评论 / 标签 / @ 提醒
- 对比视图（batch vs batch 并排图表）
- CSV / LaTeX 导出
- Batch 重跑（需要反向调用实验平台）
- 数据保留自动清理 / 归档 UI
- 审计日志 UI（MVP 只写日志文件，不做 UI）
- 邀请码注册（需求选 "开放注册"，邀请码路径作为 admin 可切换的 feature flag 预留）

## 3. 用户角色

| 角色 | 权限 |
|---|---|
| **anonymous** | 仅能访问登录/注册/public-link 页；所有 API 401 除 `/api/auth/*` 和 `/api/public/*` |
| **user** | CRUD 自己的 batch/job/token/share；只读被共享给自己的 batch/project |
| **admin** | 除 user 权限外，可见所有用户 / 所有 batch；可 ban 用户；可切换全局 feature flag（如注册开关） |

首个注册用户自动 `is_admin=true`；之后的注册用户 `is_admin=false`。

## 4. 认证与授权

### 4.1 凭据类型

| 凭据 | 格式 | 用途 | 生命周期 |
|---|---|---|---|
| **Session JWT** | `Bearer <jwt>` header | Web UI（浏览器）| 24 小时，过期前 30 分钟可自动续签 |
| **Personal API Token** | `Bearer em_live_<20 随机字符>` | 实验端 reporter POST events | 长期，显式 revoke；默认 `expires_at` 可选（UI 生成时可选 30天 / 90天 / 永不）|
| **Viewer Token**（可选） | `Bearer em_view_<20 字符>` | 只读，只能 GET 自己数据；可共享给 "想给别人登我账号看的场景"（慎用）| 与 API Token 同 |
| **Public Share Slug** | URL `/public/<slug>` | 匿名访问指定 batch 只读 | 可选过期时间 |

Token 前缀约定：`em_live_` = ingest+read，`em_view_` = read-only。

### 4.2 存储

- 用户密码：**argon2id** 哈希（不用 bcrypt，新标准更强）
- Token：存 **SHA-256 哈希**；明文只在生成时一次性返回给用户（UI 要求用户复制保存）
- JWT 签名密钥：服务启动从环境变量 `ARGUS_JWT_SECRET` 读；建议至少 32 字节随机

### 4.3 登录失败保护

- 连续 5 次失败锁定账户 10 分钟（per username）
- API token 无效 → 401 + `WWW-Authenticate: Bearer`
- 速率限制：per-token 每分钟 600 次（~10 req/s），超限 429

### 4.4 密码策略

- 最少 10 字符 + 至少 1 字母 + 1 数字（研究用，不搞硬核复杂度）
- 注册时 UI 给强度提示
- 密码修改需要当前密码
- 密码重置：走 email 链接（token 15 min 有效）

### 4.5 Email

- SMTP 配置从 `monitor.yaml` 读（或环境变量）
- 事件：注册验证（必须验证邮箱才能登录）、密码重置、（Phase 2: 每日摘要、重要通知）
- 验证邮件 24h 有效；未验证账号 7 天后自动删除

## 5. 数据模型

### 5.1 新增表

```sql
-- 用户
CREATE TABLE user (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT UNIQUE NOT NULL,
    email              TEXT UNIQUE NOT NULL,
    password_hash      TEXT NOT NULL,        -- argon2id
    is_active          BOOLEAN DEFAULT 1,
    is_admin           BOOLEAN DEFAULT 0,
    email_verified     BOOLEAN DEFAULT 0,
    created_at         TEXT NOT NULL,
    last_login         TEXT,
    failed_login_count INTEGER DEFAULT 0,
    locked_until       TEXT                  -- NULL 或 ISO 时间
);

-- Personal API token
CREATE TABLE api_token (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                -- 用户起的标签："laptop-reporter"
    token_hash  TEXT NOT NULL UNIQUE,         -- SHA-256
    prefix      TEXT NOT NULL,                -- "em_live_" / "em_view_"
    display_hint TEXT NOT NULL,               -- 前 8 字符明文，用于 UI 识别（避免暴露完整 token）
    scope       TEXT NOT NULL,                -- 'reporter' | 'viewer'
    created_at  TEXT NOT NULL,
    last_used   TEXT,
    expires_at  TEXT,                         -- 可选
    revoked     BOOLEAN DEFAULT 0
);

-- 邮件验证 token（一次性）
CREATE TABLE email_verification (
    token       TEXT PRIMARY KEY,             -- 随机 URL-safe
    user_id     INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                -- 'verify' | 'reset_password'
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed    BOOLEAN DEFAULT 0
);

-- Batch 级共享
CREATE TABLE batch_share (
    batch_id    TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    grantee_id  INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,                -- 'viewer' | 'editor'
    created_at  TEXT NOT NULL,
    created_by  INTEGER REFERENCES user(id),
    PRIMARY KEY (batch_id, grantee_id)
);

-- Project 级共享（所有者把自己在某 project 下所有 batch 共享）
CREATE TABLE project_share (
    owner_id    INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    project     TEXT NOT NULL,                -- 'DeepTS-Flow-Wheat'
    grantee_id  INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (owner_id, project, grantee_id)
);

-- 公开链接
CREATE TABLE public_share (
    slug        TEXT PRIMARY KEY,             -- 20 字符 URL-safe 随机
    batch_id    TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    created_by  INTEGER REFERENCES user(id),
    expires_at  TEXT,
    view_count  INTEGER DEFAULT 0,
    last_viewed TEXT
);

-- 审计日志（MVP 只写入，UI phase 2）
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES user(id),
    action      TEXT NOT NULL,                -- 'token_create' | 'share_add' | 'batch_delete' ...
    target_type TEXT,                         -- 'batch' | 'token' | 'user' ...
    target_id   TEXT,
    metadata    TEXT,                         -- JSON
    timestamp   TEXT NOT NULL,
    ip_address  TEXT
);

-- feature flags（admin 可切）
CREATE TABLE feature_flag (
    key         TEXT PRIMARY KEY,             -- 'registration_open' | 'email_required' | 'invite_only' ...
    value       TEXT NOT NULL,                -- JSON
    updated_at  TEXT,
    updated_by  INTEGER REFERENCES user(id)
);
```

### 5.2 修改现有表

```sql
ALTER TABLE batch ADD COLUMN owner_id INTEGER REFERENCES user(id);
ALTER TABLE batch ADD COLUMN is_deleted BOOLEAN DEFAULT 0;  -- 软删，方便 7 天后硬删
CREATE INDEX idx_batch_owner ON batch(owner_id);
CREATE INDEX idx_batch_project_owner ON batch(project, owner_id);

-- event 表加 idempotency key（见 §6.3）
ALTER TABLE event ADD COLUMN event_id TEXT;   -- 客户端生成的 UUID
CREATE UNIQUE INDEX idx_event_id ON event(event_id) WHERE event_id IS NOT NULL;
```

## 6. 通信协议

### 6.1 上行：实验 → monitor

**HTTP REST + JSON + Bearer token**：

```
POST /api/events
  Authorization: Bearer em_live_<token>
  Content-Type: application/json
  Body: <single event>
  Response: 200 {accepted: true, event_id: "<client-uuid>"}

POST /api/events/batch
  Authorization: Bearer em_live_<token>
  Body: {events: [<event>, ...]}    # 最多 500 条
  Response: 200 {
    accepted: 498,
    rejected: 2,
    results: [
      {event_id: "uuid-1", status: "accepted", db_id: 1001},
      {event_id: "uuid-2", status: "rejected", error: "schema_validation"},
      ...
    ]
  }
```

- 常规每事件单 POST；spill 回放时走 batch
- 客户端用 `requests.Session` keep-alive，减少握手开销
- 正常响应 < 100ms；超时 connect=2s, read=5s, total=10s
- 5xx / 网络错误：指数退避 3 次；仍失败 → 本地 spill `~/.argus-reporter/spill-<pid>-<ts>.jsonl`

### 6.2 下行：monitor → 浏览器

- **列表页 / 历史数据**：HTTP polling（5s interval），客户端自行轮询
- **Live job detail / running batch**：SSE `GET /api/events/stream?batch_id=X&job_id=Y`
  - `text/event-stream`，自动重连
  - 服务端 push 匹配的新事件
  - 客户端 `new EventSource(...)`，无额外库

### 6.3 幂等性

每个事件带客户端生成的 UUID `event_id`（从 schema v1.0 开始强制）：

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "schema_version": "1.0",
  "event_type": "job_epoch",
  ...
}
```

- 服务端以 `event_id` 去重：重复 POST 返回 200 + 首次写入的 db_id，不重复入表
- spill 回放本质上是重试 → 幂等保证不重复记
- 需要更新 `schemas/event_v1.json`：加 `event_id` 字段（required）

### 6.4 速率限制

- Per-token：600 req/min（~10 req/s）
- 超限返回 `429 Too Many Requests` + `Retry-After: <seconds>` header
- 客户端遇 429：按 `Retry-After` 或默认 30s 退避

### 6.5 Schema 版本

- Body 必带 `schema_version`
- 服务端仅接受 `"1.0"`，其它返回 `415 Unsupported Media Type` + 支持版本列表
- 未来升级 v2.0 时服务端同时支持 v1.0 和 v2.0 一段过渡期

### 6.6 CORS / TLS

- CORS：开发期 allow `http://localhost:5173`；生产期 allow 服务自己的域名
- TLS：生产期必须 HTTPS；nginx + Let's Encrypt 反代；开发期 HTTP OK

## 7. API 端点清单（v1）

### 7.1 认证

```
POST   /api/auth/register              {username, email, password, invite_code?}
POST   /api/auth/login                 {username_or_email, password}  → {access_token, user}
POST   /api/auth/logout                (invalidate JWT — 用黑名单实现)
POST   /api/auth/verify-email          {token}
POST   /api/auth/request-password-reset  {email}
POST   /api/auth/reset-password        {token, new_password}
POST   /api/auth/refresh               (JWT 续签)
GET    /api/auth/me                    → {id, username, email, is_admin, email_verified}
```

### 7.2 Token 管理（需登录）

```
GET    /api/tokens                     → [{id, name, prefix, display_hint, scope, created_at, last_used, expires_at}]
POST   /api/tokens                     {name, scope, expires_at?}  → {id, token: "em_live_FULL_ONLY_SHOWN_ONCE", ...}
DELETE /api/tokens/{id}                (revoke)
```

### 7.3 事件接入（需 Personal API Token）

```
POST   /api/events                     (§6.1)
POST   /api/events/batch               (§6.1)
GET    /api/events/stream              (SSE，需登录，过滤可见 batch)
```

### 7.4 数据查询（需登录，按可见性过滤）

```
GET    /api/batches?scope=mine|shared|all&user=&project=&status=&since=&limit=&offset=
GET    /api/batches/{batch_id}
GET    /api/batches/{batch_id}/jobs
GET    /api/jobs/{batch_id}/{job_id}
GET    /api/jobs/{batch_id}/{job_id}/epochs
GET    /api/jobs/{batch_id}/{job_id}/logs?since=&limit=   (phase 2 — MVP 占位)
GET    /api/resources/hosts
GET    /api/resources?host=&since=&limit=
```

**可见性规则**（backend 中间件强制）：
```
visible(user, batch) =
    batch.owner_id == user.id
  OR (user.id, batch.id) ∈ batch_share
  OR (batch.owner_id, batch.project, user.id) ∈ project_share
  OR user.is_admin
```

### 7.5 共享管理（需登录）

```
GET    /api/batches/{batch_id}/shares         → [{grantee, permission}]
POST   /api/batches/{batch_id}/shares         {grantee_username, permission}
DELETE /api/batches/{batch_id}/shares/{grantee_id}

GET    /api/projects/shares                   → [{project, grantee, permission}]
POST   /api/projects/shares                   {project, grantee_username, permission}
DELETE /api/projects/shares/{project}/{grantee_id}

POST   /api/batches/{batch_id}/public-share   {expires_at?}  → {url, slug, expires_at}
DELETE /api/batches/{batch_id}/public-share/{slug}
GET    /api/public/{slug}                     (no auth) → {batch, jobs, metrics, ...}
GET    /api/public/{slug}/jobs/{job_id}/epochs
```

### 7.6 管理员

```
GET    /api/admin/users                       → list all
POST   /api/admin/users/{id}/ban              (set is_active=false)
POST   /api/admin/users/{id}/unban
GET    /api/admin/feature-flags
PUT    /api/admin/feature-flags/{key}         {value}
GET    /api/admin/audit-log?since=&limit=
```

## 8. Frontend 页面清单

### 8.1 匿名可访问

- `/login` — 用户名/邮箱 + 密码
- `/register` — 用户名 + 邮箱 + 密码 + 邀请码（启用时）
- `/verify-email?token=X` — email 中点链接进来，完成验证
- `/reset-password?token=X` — 同上
- `/public/<slug>` — 公开 batch 展示（只读，和 BatchDetail 视觉一致但无操作按钮）

### 8.2 需登录

- `/` → redirect `/batches?scope=mine`
- `/batches?scope=mine|shared|all` — 批次列表（标签页切换）
- `/batches/:batchId` — 批次详情（matrix + timeline + share 按钮）
- `/batches/:batchId/jobs/:jobId` — job 详情（loss 曲线 + metrics + log）
- `/hosts` + `/hosts/:host` — 资源监控
- `/settings/profile` — 改密码、邮箱
- `/settings/tokens` — token 管理
- `/settings/shares` — 被我共享出去的 / 共享给我的（overview）

### 8.3 admin-only

- `/admin/users`
- `/admin/feature-flags`
- `/admin/audit-log`

## 9. 通知系统

### 9.1 规则引擎

规则文件 `backend/config/notifications.yaml`（全局）+ phase 2 扩展到 per-user 覆盖。

```yaml
rules:
  - when: event_type == "job_failed"
    push: [feishu]
  - when: event_type == "batch_done" and data.n_failed > 0
    push: [feishu]
  - when: event_type == "resource_snapshot" and data.gpu_util_pct < 5
    push: [feishu]
    throttle: 1800    # 30 分钟内只推一次
```

### 9.2 Channel 接口

```python
class BaseNotifier(ABC):
    async def send(self, title: str, body: str, level: str = 'info',
                   context: dict = None) -> None: ...
```

MVP 实现：`FeishuNotifier`。

留扩展点：`WhatsAppCallMeBotNotifier`, `WhatsAppTwilioNotifier`, `SlackNotifier`, `TelegramNotifier`, `EmailNotifier`, `WebhookNotifier`（generic）。

## 10. 部署

### 10.1 目标架构

```
单机：
  - backend (uvicorn :8000) 后台服务
  - frontend dist 由 backend StaticFiles 挂载到 /
  - SQLite 数据库 backend/data/monitor.db
  - systemd 管进程
  - nginx 反代 + TLS (Let's Encrypt)
```

### 10.2 环境变量

```
ARGUS_JWT_SECRET              JWT 签名密钥（必需，>=32 字节）
ARGUS_DB_URL                  默认 sqlite:///data/monitor.db
ARGUS_SMTP_HOST/PORT/USER/PASS  邮件配置
ARGUS_SMTP_FROM               发件人
ARGUS_BASE_URL                https://monitor.example.com （用于 email 里构造链接）
ARGUS_FEISHU_WEBHOOK          默认全局 Feishu（phase 2 可改成 per-user）
ARGUS_LOG_LEVEL               info/debug
```

### 10.3 启动流程

```bash
cd backend
pip install -e ".[dev]"
alembic upgrade head              # 数据库迁移
uvicorn backend.app:app --host 0.0.0.0 --port 8000

cd ../frontend
npm install && npm run build      # dist/ 被 backend 服务

# systemd 单元见 deploy/systemd/
```

## 11. 安全要求

- 所有密码 argon2id
- 所有 token 存哈希
- JWT secret 32+ 字节
- HTTPS 生产必需
- SQL 参数化（ORM 默认）
- XSS：前端渲染用户输入走 Vue 自动转义
- CSRF：用 Bearer header 天然免疫（不用 cookie session）
- 速率限制：per-token + per-IP 双保险
- 登录失败锁定
- 审计日志写入关键操作

## 12. OAuth 扩展点（v1 预留）

`backend/auth/providers/base.py`：

```python
class AuthProvider(ABC):
    name: str   # 'local' | 'github' | 'google'
    @abstractmethod
    async def authenticate(self, credentials: dict) -> User | None: ...
    @abstractmethod
    async def get_redirect_url(self, state: str) -> str: ...       # OAuth 流用
    @abstractmethod
    async def handle_callback(self, code: str, state: str) -> User: ...
```

MVP 只实现 `LocalAuthProvider`（username + password）。

Phase 2 加 `GitHubAuthProvider` / `GoogleAuthProvider` 实现，注册流程扩展支持绑定多个 provider 到同一 user。

## 13. 关键约束回顾

- **实验平台零侵入**：整套改动仅在 DeepTS-Flow-Wheat 加 3 个新文件 + 2-3 行修改；本表监控是**完全可选**特性（`scripts/monitor.yaml` 不配置就当本仓库不存在）
- **Fire-and-forget**：reporter 所有网络调用失败永不抛异常影响训练
- **Schema 约束**：事件必须通过 `schemas/event_v1.json` 验证；schema 变更必须 bump version
- **可复用 contract**：同一个 monitor 服务可接入任意多个 Python 实验项目（不限 DeepTS-Flow）

## 14. 验收标准

MVP 完成时以下全部可演示：

1. ✓ 两个用户 A 和 B 分别注册 + 邮箱验证 + 登录
2. ✓ A 生成 API token，填入 DeepTS-Flow-Wheat 的 `scripts/monitor.yaml`
3. ✓ A 跑 benchmark，web 上实时看到 batch + job + 每 epoch 的 loss 曲线
4. ✓ A 把某个 batch 共享给 B（viewer 权限）
5. ✓ B 登录后能看到这个 batch（在 "Shared with me" tab）但看不到 A 其它的 batch
6. ✓ A 把 project `DeepTS-Flow-Wheat` 整个共享给 B
7. ✓ B 现在能看到 A 在这个 project 下**所有** batch
8. ✓ A 生成一个 public link，任何人不登录点链接能看到结果
9. ✓ A revoke API token，DeepTS-Flow-Wheat 下次 POST 得到 401
10. ✓ admin 能在 /admin/users 看到所有用户
11. ✓ job_failed 事件触发 Feishu 推送
12. ✓ reporter 客户端：断网 5 分钟期间事件 spill，服务器恢复后自动 replay，无丢失无重复

## 15. 开放讨论点

- Batch 编辑权限（editor）具体能做什么？建议：改 name / 加 tag / 手动标记失败 job / 删除整个 batch；不能改 events 历史数据
- Phase 2 per-user Feishu webhook 配置放哪？user table 里加字段？单独 `user_notification` 表？
- 未登录访问 public link 时要不要加"请求登录以全功能查看"轻量提示？
- Admin 切换 `registration_open=false` 时，已存在未完成注册邮件验证的账号怎么处理？

---

## 16. 信息架构 (IA) 与 Dashboard / Project 看板

> 本章补充 §8 的 Frontend 页面清单，作为 MVP 的完整 IA 规范。

### 16.1 三层结构

```
/                                    Dashboard (全局看板)
/projects                            项目列表
/projects/:project                   项目详情（卡片式，主视图）
/projects/:project/batches/:id       Batch 详情（已在 §8 定义）
/projects/:project/batches/:id/jobs/:jobId   Job 详情（已定义）
/hosts /hosts/:host                  主机看板
/compare                             对比池 side-by-side
```

### 16.2 Dashboard（首页 `/`）

**显示范围**：默认仅 "my owner + shared with me"（不含匿名 public，public 去 search 找）；admin 可切 "all"。

**顶部指标条**（6 个指标数卡）：
- Running batches（我可见范围内）
- Jobs running 当前
- Jobs done last 24h
- Jobs failed last 24h（点击跳筛选列表）
- Active hosts（最近 5 分钟有 resource_snapshot 上报）
- Avg GPU util 跨所有 active host

**主区布局**（3 列 12 栅格）：
- **左 8 格**：
  1. 项目卡片 grid（2-3 列响应式），按"活跃度 > 最后事件时间"排序，starred 置顶；filter `Mine / Shared / All`
  2. 活动流 Activity Feed：last 20 events（batch_start / batch_done / batch_failed / job_failed）
- **右 4 格**：
  3. 主机状态卡：每 active host 一张，含 GPU util 条、VRAM 条、CPU util 条、RAM 条、disk_free（<10GB 红告警）、该 host 上 running jobs 数
  4. 通知面板：未读 = recent failed / rate limit / token 即将过期 / 有人共享给我
  5. 快速入口按钮：新生成 token、新建共享（按权限显隐）

### 16.3 项目详情（`/projects/:project`）

**顶部三行**：
- 第 1 行：project 名 + [Share] [Public link] [Star ★] 三按钮 + owner + collaborators + created
- 第 2 行：聚合指标条（总 batch / 本周新 / 失败率 / 累计 GPU-hours / 历史最佳 metric）
- 第 3 行：tab 切换 `Active 🟢 | Recent | Leaderboard | Matrix | Resources | Collaborators`

**Active tab（核心 view）**：卡片流，每 batch 一卡：

```
┌──────────────────────────────────────────────────────────┐
│ 🟢 <batch_id>  user: X  host: Y                          │
│ 进度 ████████░░  68/120 (57%)                             │
│ Running 3/3 slots：                                       │
│   [model × dataset] epoch 14/50 val_loss 0.38 ↓          │
│   [model × dataset] epoch 6/50 val_loss 0.51 ↓           │
│   [model × dataset] epoch 9/50 val_loss 0.42 ↓           │
│ elapsed 12h 4m  |  ETA 5h 12m  (EMA-based)               │
│ GPU 94%  VRAM 45GB  |  disk 149GB free                   │
│ mini sparkline: 近 24h job 完成趋势                        │
│ ✗ 2 failed  ⚠ 1 stalled (no events 8min)                 │
│ [View Matrix] [View Jobs] [Share] [Pin ⚓]               │
└──────────────────────────────────────────────────────────┘
```

**Recent tab**：已完成 batch 卡片（含 best metric、matrix 缩略图、duplicate config 按钮）
**Leaderboard tab**：跨 batch 聚合 best metric per (model, dataset)；可导出 CSV（§16.7）
**Matrix tab**：models × datasets 热力图，颜色 = best metric
**Resources tab**：本项目 GPU-hours 趋势、时段热力、host 分布
**Collaborators tab**：owner 管理 share；被共享者只读

### 16.4 卡片交互

- **整卡可点**：primary action = 进详情（整卡点击）；卡内 3-4 个次要按钮（Share / Pin / Compare / View）独立可点击（stopPropagation）
- **状态即视觉**：🟢 running / ✅ done / ❌ failed / ⚪ pending / ⏸ stalled
- **Live 刷新**：只有 running 卡订阅 SSE；done 卡无刷新，节省资源
- **卡内 mini chart**：loss sparkline（30px 高）/ progress bar；hover 出完整 tooltip
- **响应式**：移动端单列；mini chart 保留

### 16.5 衍生字段定义

| 字段 | 定义 / 算法 |
|---|---|
| **ETA** | 最近 10 个已完成 job 的 `elapsed_s` 指数移动平均 × pending_count；α=0.3 |
| **is_stalled** | `status=running AND now - max(event.timestamp) > 300s`（5 分钟，admin 可通过 feature_flag `stalled_threshold_sec` 调整） |
| **GPU-hours** | `sum(jobs.elapsed_s) / 3600`（单卡假设 ×1，多卡扩展时按 resource_snapshot 里的 `gpu_count` 加权） |
| **completion_pct** | `(n_done + n_failed) / n_total * 100` |
| **best_metric** | `MIN(metrics.MSE)` across scope（project / batch），指标列可切换 |
| **is_running** | `job.status=running AND batch.status=running AND now - last_event < 5min` |

### 16.6 Star 收藏

**数据**：
```sql
CREATE TABLE user_star (
    user_id        INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    target_type    TEXT NOT NULL,           -- 'project' | 'batch'
    target_id      TEXT NOT NULL,           -- project name or batch_id
    starred_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, target_type, target_id)
);
```

**行为**：Dashboard 项目卡片按 starred 置顶；Batch 列表支持 star 过滤；setting 里可看"我收藏的"。

### 16.7 对比池 Compare Pin

**数据**：
```sql
CREATE TABLE user_pin (
    user_id        INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    batch_id       TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    pinned_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, batch_id)
);
```

**UI**：
- 每 batch 卡片有"Pin ⚓"按钮
- 顶部 nav 显示"已钉 N 个"徽标
- `/compare` 页面：side-by-side 展示 2-4 个 pinned batch，并列 loss 曲线、metrics 对比表、matrix 差异 heatmap
- 上限 4 个 batch（UI 空间约束）
- Pin 跨会话持久（存 DB 而非 localStorage）

### 16.8 导出 CSV

**端点**：
```
GET /api/batches/{batch_id}/export.csv        → batch leaderboard CSV
GET /api/projects/{project}/export.csv        → project leaderboard CSV（跨 batch 聚合）
GET /api/projects/{project}/export-raw.csv    → 每 (batch, model, dataset, metric) 明细
GET /api/compare/export.csv?batches=a,b,c     → 对比池导出
```

CSV 列至少含：batch_id, model, dataset, status, epochs, elapsed_s, MSE, MAE, RMSE, R2, PCC, MAPE, GPU_peak_mb, ...，前端点 "Export CSV" 按钮直接触发浏览器下载。

### 16.9 Public link 页面

**显示与登录用户同样信息**（不简化）：batch 详情 matrix / timeline / 每 job 的 loss 曲线 + metrics + SHAP 图。
只有 3 类操作被禁用：
- Share 按钮（不能在公开链接里再加共享）
- Pin 按钮（未登录无法 pin）
- Export CSV（要求登录，防止批量爬）

### 16.10 Project 识别

**完全自动**：`source.project` 字段值作为 project 标识，无显式"创建 project"流程。
- 首次出现即注册 project（无额外 DB 表；从 batch 表 GROUP BY project 动态生成列表）
- 大小写敏感（`DeepTS-Flow-Wheat` ≠ `deepts-flow-wheat`）
- rename：不支持；用户可通过 admin 提 issue 由 admin 做 `UPDATE batch SET project=... WHERE project=...`

### 16.11 卡片上"你能想到的"其它重要信息（已纳入）

MVP 里每个卡片和详情页必须展示：
1. model 参数量 / 文件大小（来自 job metrics）
2. 单 job loss mini sparkline（卡内 30px；详情页大图）
3. 当前 epoch / 总 epoch（`14/50`）
4. Batch 命令可一键复制（悬浮按钮）
5. Git commit hash（batch source 扩展字段；public 页也显示，论文可复现）
6. 环境指纹（Python / torch / CUDA 版本；source 扩展字段）
7. 运行时告警（GPU >85℃ / 磁盘 <10GB / OOM 标记）→ 卡片右上角红点 + 通知面板
8. Best-so-far 提示（`已超历史最好 3.2%`）→ batch active tab 顶部横条
9. Notification badge（nav 上未读计数）
10. 快速 filter chips：`Only mine / Only failed / Last 24h` 一键筛

## 17. IA 衍生 API 端点（补充 §7）

```
GET  /api/dashboard                          顶部指标 + projects + activity + hosts + notifications（一次性聚合，降前端 N+1）
GET  /api/projects                           项目列表（自动推断，去重 batch.project）
GET  /api/projects/{project}                 项目详情聚合
GET  /api/projects/{project}/active-batches  当前 running batch + 其 running jobs（SSE 订阅同一数据流）
GET  /api/projects/{project}/leaderboard    跨 batch 最好结果
GET  /api/projects/{project}/matrix         models × datasets 聚合
GET  /api/projects/{project}/resources      GPU-hours / 时段热力
POST /api/stars                             {target_type, target_id}
DELETE /api/stars/{target_type}/{target_id}
GET  /api/stars                             我收藏的
POST /api/pins                              {batch_id}
DELETE /api/pins/{batch_id}
GET  /api/pins                              我钉的
GET  /api/compare?batches=a,b,c             对比池数据
GET  /api/batches/{id}/health               {is_stalled, last_event_age_s, failure_count, warnings}
GET  /api/batches/{id}/eta                  基于 EMA 的预估
GET  /api/batches/{id}/export.csv
GET  /api/projects/{project}/export.csv
GET  /api/projects/{project}/export-raw.csv
GET  /api/compare/export.csv?batches=...
```

## 18. 更新后的验收标准（扩展 §14）

原 12 条基础上补充：

13. ✓ 登录用户首页 Dashboard 能看到"我的 running batches"数量、项目卡片、活动流、主机状态面板
14. ✓ 进入某项目详情，Active tab 用卡片列出当前 running 的 batch，含进度条、running jobs 列表、ETA、GPU/VRAM、stalled 警告
15. ✓ Star 某项目 → Dashboard 置顶显示
16. ✓ Pin 2-4 个 batch → `/compare` 显示 side-by-side 对比（loss 曲线 + metrics 表 + matrix 差异）
17. ✓ 任意 batch leaderboard / project leaderboard 可一键导出 CSV
18. ✓ Public link 页面展示完整信息（matrix / 每 job loss / metrics），但 Share / Pin / Export 按钮被禁用
19. ✓ job 5 分钟无事件且 status=running → UI 显示 "stalled" 橙色标记
20. ✓ ETA 基于最近 10 完成 job 的 EMA，跨 benchmark 阶段性变速时准确跟进
21. ✓ Dashboard 默认范围只含"我 + 共享给我"，admin 可切 "All" 看全局
22. ✓ GPU >85℃ 或磁盘 <10GB → host 卡片 + 通知面板同时告警
