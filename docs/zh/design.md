> 🌐 [English](./design.md) · **中文**

# Argus — 系统设计文档 v1

> 本文档回答"怎么做"；需求文档回答"做什么"。有冲突以需求文档为准，但代码实现以本文档为准。

## 1. 总体架构

```
┌────────────────────────┐   HTTPS POST   ┌──────────────────────────────────┐
│ 实验端 (reporter 客户端)  │ ─────────────►│ nginx (TLS / :443)                │
│                        │                │   reverse proxy                  │
│ - scripts/monitor.yaml │                └───────────┬──────────────────────┘
│ - argus-reporter  │                            │ :8000
│ - 后台线程 + queue       │                            ▼
│ - 本地 spill jsonl      │                ┌──────────────────────────────────┐
└────────────────────────┘                │ FastAPI 进程 (uvicorn)           │
                                           │                                  │
                                           │ ┌───────────────┐  ┌──────────┐ │
                                           │ │ Auth layer    │  │ Rate     │ │
                                           │ │ - JWT middle  │  │ limiter  │ │
                                           │ │ - Token lookup│  │ (slowapi)│ │
                                           │ └──────┬────────┘  └──────────┘ │
                                           │        │                        │
                                           │ ┌──────▼────────────────────┐   │
                                           │ │ API routers               │   │
                                           │ │ auth | tokens | events |  │   │
                                           │ │ batches | jobs | shares | │   │
                                           │ │ public | admin | sse      │   │
                                           │ └──────┬────────────────────┘   │
                                           │        │                        │
                                           │ ┌──────▼────────────────────┐   │
                                           │ │ Service layer             │   │
                                           │ │ - EventIngestService      │   │
                                           │ │ - VisibilityResolver      │   │
                                           │ │ - NotificationEngine      │   │
                                           │ │ - EmailService            │   │
                                           │ └──────┬────────────────────┘   │
                                           │        │                        │
                                           │ ┌──────▼──────┐  ┌───────────┐  │
                                           │ │ SQLAlchemy  │  │ bg tasks  │  │
                                           │ │ 2.0 async   │  │(asyncio)  │  │
                                           │ └──────┬──────┘  └─────┬─────┘  │
                                           └────────┼────────────────┼───────┘
                                                    ▼                ▼
                                           ┌──────────────┐   ┌──────────────┐
                                           │ SQLite       │   │ Feishu /     │
                                           │ monitor.db   │   │ WhatsApp /   │
                                           │ (WAL mode)   │   │ SMTP / ...   │
                                           └──────────────┘   └──────────────┘
                                                    ▲
                                                    │
                                           ┌────────┼──────────────────────────┐
                                           │ 浏览器 (Vue SPA)                   │
                                           │   /login /batches /settings ...   │
                                           │   - localStorage 存 JWT           │
                                           │   - EventSource (SSE) for live    │
                                           │   - axios interceptor auto-auth   │
                                           └───────────────────────────────────┘
```

## 2. 技术栈

### 2.1 Backend
- **Framework**: FastAPI 0.115+
- **ASGI server**: uvicorn 0.32+
- **ORM**: SQLAlchemy 2.0 async + aiosqlite
- **Migrations**: Alembic
- **Validation**: Pydantic v2
- **Auth**:
  - Password: `argon2-cffi` (argon2id)
  - JWT: `pyjwt`
  - API token: `secrets.token_urlsafe` + SHA-256 hashing
- **Email**: `aiosmtplib`（异步 SMTP）+ `jinja2`（邮件模板）
- **Rate limit**: `slowapi` 或自写 in-memory bucket
- **HTTP client (for Feishu/webhooks)**: `httpx.AsyncClient`
- **Logging**: stdlib logging + structured JSON via `python-json-logger`
- **Testing**: pytest + pytest-asyncio + httpx.AsyncClient

### 2.2 Frontend
- **Framework**: Vue 3 + TypeScript + Composition API
- **Build**: Vite 5
- **UI Library**: Ant Design Vue 4.x + `@ant-design/icons-vue`
- **State**: Pinia
- **Router**: Vue Router 4
- **HTTP**: axios（带 interceptor 自动 attach JWT）
- **Charts**: Apache ECharts via vue-echarts
- **SSE**: 原生 `EventSource` API
- **Date**: dayjs
- **Auto-import AntD**: unplugin-vue-components

### 2.3 Client (reporter)
- **Framework**: 纯 Python 3.10+ stdlib
- **HTTP**: `requests` (keep-alive Session)
- **Concurrency**: `threading.Thread` + `queue.Queue`
- **Packaging**: `hatchling`
- **Test**: pytest + pytest-httpserver

## 3. 目录结构

```
argus/
├── README.md
├── .gitignore
├── schemas/
│   └── event_v1.json                  # 事件契约（加 event_id 字段，已在本次迭代）
├── docs/
│   ├── requirements.md                # 需求
│   ├── design.md                      # 本文件
│   ├── architecture.md                # 原始数据模型笔记（已写）
│   └── api.md                         # OpenAPI 自动生成，这里是人写的说明
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/                    # Alembic 迁移
│   │   └── versions/
│   │       ├── 001_initial.py         # batch/job/event/resource
│   │       └── 002_auth_sharing.py    # user/token/share/public_share/...
│   ├── backend/
│   │   ├── __init__.py
│   │   ├── app.py                     # FastAPI 工厂 + lifespan
│   │   ├── config.py                  # settings (Pydantic BaseSettings，读 env)
│   │   ├── db.py                      # async engine + session
│   │   ├── models.py                  # SQLAlchemy ORM
│   │   ├── schemas/                   # Pydantic DTOs
│   │   │   ├── auth.py
│   │   │   ├── events.py
│   │   │   ├── batches.py
│   │   │   ├── shares.py
│   │   │   └── ...
│   │   ├── deps.py                    # Depends: get_db, get_current_user, require_admin
│   │   ├── auth/
│   │   │   ├── password.py            # argon2 wrappers
│   │   │   ├── jwt.py                 # sign / verify / refresh
│   │   │   ├── tokens.py              # API token gen / hash / lookup
│   │   │   └── providers/
│   │   │       ├── base.py            # AuthProvider ABC
│   │   │       └── local.py           # username+password
│   │   ├── services/
│   │   │   ├── event_ingest.py        # 写 event / 更新 batch/job
│   │   │   ├── visibility.py          # visibility query helper
│   │   │   ├── notifications.py       # 规则引擎 + dispatch
│   │   │   ├── email.py               # 渲染模板 + 发送
│   │   │   └── audit.py               # 写审计日志
│   │   ├── api/
│   │   │   ├── __init__.py            # 组装 APIRouter
│   │   │   ├── auth.py                # /api/auth/*
│   │   │   ├── tokens.py              # /api/tokens
│   │   │   ├── events.py              # /api/events (+ /batch)
│   │   │   ├── events_stream.py       # /api/events/stream (SSE)
│   │   │   ├── batches.py
│   │   │   ├── jobs.py
│   │   │   ├── resources.py
│   │   │   ├── shares.py              # batch + project share
│   │   │   ├── public.py              # /api/public/{slug}
│   │   │   └── admin.py
│   │   ├── notifications/
│   │   │   ├── base.py                # BaseNotifier
│   │   │   ├── feishu.py
│   │   │   ├── rules.py               # YAML → rule matcher
│   │   │   └── config.yaml.example
│   │   ├── emails/
│   │   │   ├── templates/
│   │   │   │   ├── verify.html
│   │   │   │   ├── reset_password.html
│   │   │   └── __init__.py
│   │   └── utils/
│   │       ├── ids.py                 # slug / token 生成
│   │       └── ratelimit.py
│   └── tests/
│       ├── conftest.py
│       ├── test_auth.py
│       ├── test_tokens.py
│       ├── test_events.py
│       ├── test_events_idempotency.py
│       ├── test_visibility.py
│       ├── test_shares.py
│       ├── test_public.py
│       ├── test_admin.py
│       └── test_notifications.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.ts
│       ├── App.vue
│       ├── router/
│       ├── store/
│       │   ├── auth.ts                # JWT + current user
│       │   ├── batches.ts
│       │   └── ui.ts
│       ├── api/
│       │   ├── client.ts              # axios + interceptor
│       │   ├── auth.ts
│       │   ├── events.ts
│       │   ├── batches.ts
│       │   ├── shares.ts
│       │   └── ...
│       ├── types.ts
│       ├── pages/
│       │   ├── Login.vue
│       │   ├── Register.vue
│       │   ├── VerifyEmail.vue
│       │   ├── ResetPassword.vue
│       │   ├── BatchList.vue          # tabs: mine / shared / all
│       │   ├── BatchDetail.vue        # + ShareDialog
│       │   ├── JobDetail.vue
│       │   ├── HostList.vue
│       │   ├── HostDetail.vue
│       │   ├── PublicBatch.vue        # /public/:slug
│       │   ├── settings/
│       │   │   ├── Profile.vue
│       │   │   ├── Tokens.vue
│       │   │   └── Shares.vue
│       │   └── admin/
│       │       ├── Users.vue
│       │       ├── FeatureFlags.vue
│       │       └── AuditLog.vue
│       └── components/
│           ├── AppLayout.vue
│           ├── StatusTag.vue
│           ├── ProgressInline.vue
│           ├── LossChart.vue
│           ├── ResourceChart.vue
│           ├── JobMatrix.vue
│           ├── ShareDialog.vue
│           └── LiveLogPanel.vue
├── client/
│   ├── pyproject.toml
│   ├── README.md
│   ├── argus/
│   │   ├── __init__.py
│   │   ├── reporter.py                # 主类
│   │   ├── schema.py                  # 事件构造 + 客户端校验
│   │   ├── queue.py                   # 后台线程 + Queue + retry
│   │   └── spill.py                   # 本地 spill jsonl
│   ├── tests/
│   │   └── ...
│   └── examples/
│       ├── simple.py
│       └── callback_style.py
└── deploy/
    ├── Dockerfile                     # 单镜像：backend + 已 build 的 frontend
    ├── docker-compose.yml
    ├── nginx.conf.example
    └── systemd/
        └── argus.service
```

## 4. 关键序列图（文字描述）

### 4.1 用户注册 + 邮件验证
```
浏览器                  backend                  SMTP
  │ POST /api/auth/register
  ├──────────────────────►│
  │                        ├─ 检查 username/email 唯一
  │                        ├─ argon2 哈希密码
  │                        ├─ INSERT user (email_verified=false)
  │                        ├─ 如果是首个用户 → is_admin=true
  │                        ├─ 生成 email_verification token
  │                        ├─ 渲染 verify 邮件模板
  │                        ├────────────────────►│ send email
  │ 200 {user_id, require_verify}                 │
  │◄──────────────────────┤                       │
  │                                                │
  (用户去邮箱点链接)                                  │
  │ GET /verify-email?token=X                      │
  │─ 前端路由                                        │
  │ POST /api/auth/verify-email {token}           │
  ├──────────────────────►│                       │
  │                        ├─ 查 email_verification
  │                        ├─ UPDATE user SET email_verified=true
  │                        ├─ DELETE consumed token
  │ 200 {success}                                  │
  │◄──────────────────────┤
```

### 4.2 实验端 POST 事件（带幂等）
```
reporter                 backend                 Feishu
  │ batch_id=X 生成                                 │
  │ 后台 worker 从 queue pop event                   │
  │ POST /api/events + Authorization: Bearer em_live_YYY
  ├──────────────────────►│
  │                        ├─ 查 api_token WHERE token_hash=SHA256(YYY)
  │                        │   → user_id, scope='reporter'
  │                        ├─ 速率限制检查 (per token)
  │                        ├─ Pydantic 校验
  │                        ├─ SELECT event WHERE event_id=<client-uuid>
  │                        │   → 若存在，返回已有 db_id（幂等）
  │                        ├─ INSERT event
  │                        ├─ 根据 event_type 更新 batch/job
  │                        │   - batch 新建时设 owner_id = user_id
  │                        ├─ asyncio.create_task(dispatch_notifications)
  │ 200 {accepted, event_id, db_id}                │
  │◄──────────────────────┤                       │
  │                        (background)            │
  │                        ├─ 匹配规则               │
  │                        ├────────────────────►│
```

### 4.3 SSE live 订阅
```
浏览器                     backend
  │ GET /api/events/stream?batch_id=X
  │   Authorization: Bearer <JWT>
  ├─────────────────────────►│
  │                           ├─ verify JWT → user_id
  │                           ├─ check visibility(user, batch) 
  │                           │   若不可见 → 403
  │                           ├─ 返回 Content-Type: text/event-stream
  │ <─── event: job_epoch (keep-alive) ───┤
  │ <─── event: job_done ──────────────────┤
  │ (连接保持，直到浏览器关闭或超时)
  │
  后端 push 机制：
    EventIngestService 里每次写入后，publish 到 asyncio.Queue per-subscriber
    SSE handler 从 Queue 拿事件序列化为 SSE event 发到浏览器
```

### 4.4 Share + 可见性过滤
```
浏览器 B                   backend
  │ GET /api/batches?scope=shared
  ├─────────────────────────►│
  │                           ├─ JWT → user_id=B
  │                           ├─ VisibilityResolver.query(user=B, scope='shared')
  │                           │   = batch WHERE id IN (
  │                           │       SELECT batch_id FROM batch_share WHERE grantee_id=B
  │                           │     UNION
  │                           │       SELECT b.id FROM batch b, project_share p
  │                           │       WHERE b.owner_id=p.owner_id
  │                           │         AND b.project=p.project
  │                           │         AND p.grantee_id=B)
  │ 200 [{batch}, ...]                              │
  │◄─────────────────────────┤
```

## 5. 关键算法 / 逻辑

### 5.1 Visibility 查询

统一封装在 `services/visibility.py`：

```python
class VisibilityResolver:
    async def visible_batch_ids(self, user_id: int, scope: str = 'all') -> SQL_clause:
        """
        scope:
          'mine'   → owner_id = user_id
          'shared' → 其它用户 share 给我的（batch_share + project_share）
          'public' → 有 public_share 的（公开链接存在）
          'all'    → mine ∪ shared（默认；admin 可见全部用此作底层）
        """
    
    async def can_edit(self, user_id, batch_id) -> bool: ...
    async def can_view(self, user_id, batch_id) -> bool: ...
```

Admin 用户跳过过滤（`is_admin=True` 的 `visible_batch_ids` 返回 no-filter）。

### 5.2 Token 验证中间件

```python
# deps.py
async def get_current_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401)
    token = authorization[7:]
    
    if token.startswith("em_live_") or token.startswith("em_view_"):
        # API token 分支
        token_hash = sha256(token).hexdigest()
        row = await db.execute(
            select(ApiToken).where(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked == False,
                or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now())
            )
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(401, "Invalid or expired API token")
        await db.execute(update(ApiToken).where(...).values(last_used=now()))
        return row.user
    else:
        # JWT 分支
        try:
            payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception:
            raise HTTPException(401)
        user = await db.get(User, payload["user_id"])
        if not user or not user.is_active:
            raise HTTPException(401)
        return user

async def require_reporter_token(user: User = Depends(get_current_user)) -> User:
    """仅允许 em_live_ token（scope=reporter）的用户调用。POST /api/events 用。"""
    # ...

async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user
```

### 5.3 事件处理 + 幂等

```python
# services/event_ingest.py
class EventIngestService:
    async def ingest(self, event: EventIn, user: User, db: AsyncSession) -> dict:
        # 1. 幂等检查
        if event.event_id:
            existing = await db.scalar(
                select(Event.id).where(Event.event_id == event.event_id)
            )
            if existing:
                return {"accepted": True, "event_id": event.event_id, 
                        "db_id": existing, "deduplicated": True}
        
        # 2. 写 event 行
        row = Event(
            event_id=event.event_id,
            batch_id=event.batch_id,
            job_id=event.job_id,
            event_type=event.event_type,
            timestamp=event.timestamp,
            schema_version=event.schema_version,
            data_json=json.dumps(event.data.model_dump()),
        )
        db.add(row)
        await db.flush()  # 拿 row.id
        
        # 3. 根据 event_type 更新派生表
        await self._apply_side_effects(event, user, db)
        
        await db.commit()
        
        # 4. 触发通知 + SSE 广播（都是 fire-and-forget）
        asyncio.create_task(self._dispatch_notifications(event))
        asyncio.create_task(self._sse_broadcast(event))
        
        return {"accepted": True, "event_id": event.event_id, "db_id": row.id}
    
    async def _apply_side_effects(self, event, user, db):
        if event.event_type == 'batch_start':
            # 若 batch 不存在则创建，owner = user
            await self._ensure_batch(event, user, db, creating=True)
        elif event.event_type in ('job_start', 'job_epoch', 'job_done', 'job_failed'):
            # 确保 batch + job 存在（自动 stub）
            await self._ensure_batch(event, user, db)
            await self._ensure_job(event, db)
            if event.event_type == 'job_done':
                await self._update_job_metrics(event, db)
                await self._recompute_batch_counters(event.batch_id, db)
            # ...
```

### 5.4 SSE 广播

简单的 per-process in-memory pub/sub：

```python
# api/events_stream.py
class SSEHub:
    """进程内的 SSE 订阅管理。多进程扩展需换 Redis pub/sub。"""
    def __init__(self):
        self._subs: dict[int, asyncio.Queue] = {}
        self._next_id = 0
    
    def subscribe(self, filter: dict) -> tuple[int, asyncio.Queue]:
        q = asyncio.Queue(maxsize=100)
        sid = self._next_id; self._next_id += 1
        self._subs[sid] = (q, filter)
        return sid, q
    
    def unsubscribe(self, sid: int):
        self._subs.pop(sid, None)
    
    async def publish(self, event: EventIn):
        for sid, (q, filt) in list(self._subs.items()):
            if self._match(event, filt):
                try: q.put_nowait(event)
                except asyncio.QueueFull: pass  # 掉帧

hub = SSEHub()

@router.get("/api/events/stream")
async def stream(
    batch_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # visibility check
    if not await VisibilityResolver().can_view(user.id, batch_id):
        raise HTTPException(403)
    
    sid, q = hub.subscribe({"batch_id": batch_id})
    
    async def event_gen():
        try:
            yield f"event: hello\ndata: subscribed\n\n"
            while True:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"event: {event.event_type}\n"
                yield f"data: {json.dumps(event.model_dump())}\n\n"
        except asyncio.TimeoutError:
            yield f"event: keepalive\ndata: {int(time.time())}\n\n"
        finally:
            hub.unsubscribe(sid)
    
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

### 5.5 Rate limiting

per-token bucket，in-memory：

```python
# utils/ratelimit.py
class TokenBucket:
    """Leaky bucket per key. 60 tokens, 10/s refill = 600/min."""
    def __init__(self, capacity=60, refill_per_sec=10): ...
    async def try_consume(self, key: str, cost: int = 1) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
```

用在 POST /api/events 中间件里：key 是 token_hash 前缀。429 时返回 `Retry-After`。

## 6. Schema 演进

当前 v1.0。加 `event_id` 字段为 required（客户端必须生成 UUID）。更新 `schemas/event_v1.json`：

```jsonc
{
  "required": ["schema_version", "event_type", "timestamp", "batch_id", "source", "event_id"],
  "properties": {
    "event_id": {
      "type": "string",
      "format": "uuid",
      "description": "Client-generated UUID for idempotency"
    },
    // ... 其它保持不变
  }
}
```

backend 接受没有 event_id 的旧事件（向后兼容），但会警告；rejected 响应里标 `deprecated`。

## 7. 数据迁移

两阶段 Alembic migration：

**001_initial**：沿用现有 Batch/Job/Event/ResourceSnapshot（backend agent 已实现）

**002_auth_sharing**：
- 新建 user / api_token / email_verification / batch_share / project_share / public_share / audit_log / feature_flag
- `ALTER TABLE batch ADD COLUMN owner_id` + `is_deleted`
- `ALTER TABLE event ADD COLUMN event_id` + unique index
- 数据回填：对现有 batch，`owner_id = NULL`（admin 单独处理）

升级流程：`alembic upgrade head`。降级 `alembic downgrade -1` 只丢 002 的新表。

## 8. 错误处理规范

| 场景 | HTTP code | Body |
|---|---|---|
| 未登录访问受保护 | 401 | `{"detail": "Authentication required"}` + `WWW-Authenticate: Bearer` |
| 登录但无权限 | 403 | `{"detail": "Forbidden", "required": "admin"}` |
| 资源不存在 | 404 | `{"detail": "Batch not found"}` |
| 客户端输入错 | 422 | Pydantic ValidationError JSON |
| Schema 版本不支持 | 415 | `{"detail": "Unsupported schema_version", "supported": ["1.0"]}` |
| 速率超限 | 429 | `{"detail": "Rate limit exceeded"}` + `Retry-After: 5` |
| 服务端异常 | 500 | `{"detail": "Internal error", "trace_id": "<uuid>"}` + 日志 |

客户端 reporter 对 4xx 行为：
- 401/403 → log.error（凭据问题，不重试）
- 422/415 → log.error（schema 问题，不重试）
- 404 → log.warning（可能 batch 被删，drop）
- 429 → 读 Retry-After 退避
- 5xx / 网络 → 指数退避 3 次 → spill

## 9. 日志 / 审计

- **App log**：stdlib logging，JSON format，INFO+WARN+ERROR 级别。字段：timestamp / level / logger / trace_id / user_id / message
- **Audit log**：`audit_log` 表。写入触发点：
  - 用户：register / login / password_change / email_verify
  - Token：create / revoke
  - Share：add / remove / public_share_create
  - Batch：delete (owner)
  - Admin：user_ban / feature_flag_change

## 10. 部署

### 10.1 Docker 单镜像

```dockerfile
FROM node:20 AS frontend-build
WORKDIR /app
COPY frontend/ .
RUN npm ci && npm run build

FROM python:3.11-slim
WORKDIR /app
COPY backend/pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY backend/ .
COPY --from=frontend-build /app/dist ./frontend_dist
COPY schemas/ ./schemas/
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 10.2 docker-compose

```yaml
services:
  monitor:
    build: .
    ports: ["8000:8000"]
    environment:
      ARGUS_JWT_SECRET: ${ARGUS_JWT_SECRET}
      ARGUS_DB_URL: sqlite+aiosqlite:///data/monitor.db
      ARGUS_SMTP_HOST: smtp.gmail.com
      ARGUS_SMTP_PORT: 587
      ARGUS_SMTP_USER: ${SMTP_USER}
      ARGUS_SMTP_PASS: ${SMTP_PASS}
      ARGUS_SMTP_FROM: "noreply@monitor.local"
      ARGUS_BASE_URL: https://monitor.example.com
      ARGUS_FEISHU_WEBHOOK: ${FEISHU_WEBHOOK}
    volumes:
      - ./data:/app/data
    restart: unless-stopped
  
  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./certs:/etc/certs
    depends_on: [monitor]
```

### 10.3 备份

- SQLite：每小时 `sqlite3 monitor.db ".backup /backup/monitor-$(date +%Y%m%d-%H).db"`
- 简单 cron + rsync 到备份目录

## 11. 监控自身（meta-monitoring）

- `/health` 返回 200（基本 liveness）
- `/metrics` Prometheus 格式（phase 2）
- FastAPI 错误日志 → systemd journal
- 磁盘空间告警：`disk_free_mb < 5 GB` 时邮件通知 admin

## 12. 开发工作流

### 12.1 本地开发

```bash
# Terminal 1: backend
cd backend
pip install -e ".[dev]"
export ARGUS_JWT_SECRET=dev-secret-not-for-prod
alembic upgrade head
uvicorn backend.app:app --reload --port 8000

# Terminal 2: frontend
cd frontend
npm install
npm run dev  # :5173

# Terminal 3: client (如果要测)
cd client
pip install -e ".[dev]"
pytest

# 浏览器打开 http://localhost:5173
# 注册首用户 → admin
# Settings → Tokens → 生成 em_live_xxx
# 写入 DeepTS-Flow-Wheat/scripts/monitor.yaml
# 跑一个 smoke benchmark 看实时数据
```

### 12.2 测试矩阵

| 层 | 工具 | 覆盖 |
|---|---|---|
| Backend unit | pytest + httpx.AsyncClient | service 层 + auth + visibility |
| Backend integration | pytest + in-memory SQLite | 完整 API 端到端 |
| Frontend unit | vitest | stores + utilities |
| Frontend e2e | Playwright | 登录 → 注册 → post event → 看结果 |
| Client | pytest + pytest-httpserver | 队列 + 重试 + spill |
| 跨仓库集成 | 手写 shell 脚本 | 启动 monitor → DeepTS-Flow 真 POST → 断言数据出现 |

### 12.3 CI (phase 2)

- GitHub Actions（若移到 GitHub）/GitLab CI
- 每次 push run backend/frontend/client 测试；build Docker 镜像

## 13. 团队协作

- **product-manager**: 读需求 + 设计 → 拆任务 → 派发 → 收集结果 → 向用户汇报
- **backend-eng**: backend 全栈，按 PM 指派做功能迭代
- **frontend-eng**: frontend 全栈
- **client-eng**: reporter 库维护
- **qa-eng**: 写跨仓库集成测试、压测、安全审计、签发 release

沟通协议：
- PM 用 SendMessage 派任务给 eng
- eng 完成后 SendMessage 回 PM 报告 + 指出风险
- QA 在 eng 完成后做黑盒测试，SendMessage 给 PM 报告结果
- PM 整合后 SendMessage 给用户

所有人共享 `<repo_root>/docs/` 下的文档作为真源。代码全部在 `<repo_root>/` 下对应子目录。
