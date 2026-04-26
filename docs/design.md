> 🌐 **English** · [中文](./design.zh-CN.md)

# Argus — System Design Document v1

> This document answers "how"; the requirements document answers "what". Where the two conflict the requirements document takes precedence, but code implementation follows this document.

## 1. Overall Architecture

```
┌────────────────────────┐   HTTPS POST   ┌──────────────────────────────────┐
│ Experiment side        │ ─────────────►│ nginx (TLS / :443)                │
│ (reporter client)      │                │   reverse proxy                  │
│                        │                └───────────┬──────────────────────┘
│ - scripts/monitor.yaml │                            │ :8000
│ - argus-reporter  │                            ▼
│ - background thread    │                ┌──────────────────────────────────┐
│   + queue              │                │ FastAPI process (uvicorn)        │
│ - local spill jsonl    │                │                                  │
└────────────────────────┘                │ ┌───────────────┐  ┌──────────┐ │
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
                                           │ Browser (Vue SPA)                 │
                                           │   /login /batches /settings ...   │
                                           │   - JWT stored in localStorage    │
                                           │   - EventSource (SSE) for live    │
                                           │   - axios interceptor auto-auth   │
                                           └───────────────────────────────────┘
```

## 2. Technology Stack

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
- **Email**: `aiosmtplib` (async SMTP) + `jinja2` (email templates)
- **Rate limit**: `slowapi` or custom in-memory bucket
- **HTTP client (for Feishu/webhooks)**: `httpx.AsyncClient`
- **Logging**: stdlib logging + structured JSON via `python-json-logger`
- **Testing**: pytest + pytest-asyncio + httpx.AsyncClient

### 2.2 Frontend
- **Framework**: Vue 3 + TypeScript + Composition API
- **Build**: Vite 5
- **UI Library**: Ant Design Vue 4.x + `@ant-design/icons-vue`
- **State**: Pinia
- **Router**: Vue Router 4
- **HTTP**: axios (with interceptor to auto-attach JWT)
- **Charts**: Apache ECharts via vue-echarts
- **SSE**: native `EventSource` API
- **Date**: dayjs
- **Auto-import AntD**: unplugin-vue-components

### 2.3 Client (reporter)
- **Framework**: pure Python 3.10+ stdlib
- **HTTP**: `requests` (keep-alive Session)
- **Concurrency**: `threading.Thread` + `queue.Queue`
- **Packaging**: `hatchling`
- **Test**: pytest + pytest-httpserver

## 3. Directory Structure

```
argus/
├── README.md
├── .gitignore
├── schemas/
│   └── event_v1.json                  # event contract (adds event_id field, in this iteration)
├── docs/
│   ├── requirements.md                # requirements
│   ├── design.md                      # this file
│   ├── architecture.md                # original data model notes (written)
│   └── api.md                         # OpenAPI auto-generated; this is the human-written notes
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/                    # Alembic migrations
│   │   └── versions/
│   │       ├── 001_initial.py         # batch/job/event/resource
│   │       └── 002_auth_sharing.py    # user/token/share/public_share/...
│   ├── backend/
│   │   ├── __init__.py
│   │   ├── app.py                     # FastAPI factory + lifespan
│   │   ├── config.py                  # settings (Pydantic BaseSettings, reads env)
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
│   │   │   ├── event_ingest.py        # write event / update batch/job
│   │   │   ├── visibility.py          # visibility query helper
│   │   │   ├── notifications.py       # rule engine + dispatch
│   │   │   ├── email.py               # render template + send
│   │   │   └── audit.py               # write audit log
│   │   ├── api/
│   │   │   ├── __init__.py            # assemble APIRouter
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
│   │       ├── ids.py                 # slug / token generation
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
│   │   ├── reporter.py                # main class
│   │   ├── schema.py                  # event construction + client-side validation
│   │   ├── queue.py                   # background thread + Queue + retry
│   │   └── spill.py                   # local spill jsonl
│   ├── tests/
│   │   └── ...
│   └── examples/
│       ├── simple.py
│       └── callback_style.py
└── deploy/
    ├── Dockerfile                     # single image: backend + pre-built frontend
    ├── docker-compose.yml
    ├── nginx.conf.example
    └── systemd/
        └── argus.service
```

## 4. Key Sequence Diagrams (narrative)

### 4.1 User Registration + Email Verification
```
Browser                 backend                  SMTP
  │ POST /api/auth/register
  ├──────────────────────►│
  │                        ├─ check username/email unique
  │                        ├─ argon2 hash password
  │                        ├─ INSERT user (email_verified=false)
  │                        ├─ if first user → is_admin=true
  │                        ├─ generate email_verification token
  │                        ├─ render verify email template
  │                        ├────────────────────►│ send email
  │ 200 {user_id, require_verify}                 │
  │◄──────────────────────┤                       │
  │                                                │
  (user clicks link in inbox)                      │
  │ GET /verify-email?token=X                      │
  │─ frontend routing                              │
  │ POST /api/auth/verify-email {token}           │
  ├──────────────────────►│                       │
  │                        ├─ look up email_verification
  │                        ├─ UPDATE user SET email_verified=true
  │                        ├─ DELETE consumed token
  │ 200 {success}                                  │
  │◄──────────────────────┤
```

### 4.2 Experiment-side POST Event (with idempotency)
```
reporter                 backend                 Feishu
  │ generate batch_id=X                           │
  │ background worker pops event from queue        │
  │ POST /api/events + Authorization: Bearer em_live_YYY
  ├──────────────────────►│
  │                        ├─ look up api_token WHERE token_hash=SHA256(YYY)
  │                        │   → user_id, scope='reporter'
  │                        ├─ rate limit check (per token)
  │                        ├─ Pydantic validation
  │                        ├─ SELECT event WHERE event_id=<client-uuid>
  │                        │   → if exists, return existing db_id (idempotent)
  │                        ├─ INSERT event
  │                        ├─ update batch/job based on event_type
  │                        │   - on batch creation set owner_id = user_id
  │                        ├─ asyncio.create_task(dispatch_notifications)
  │ 200 {accepted, event_id, db_id}                │
  │◄──────────────────────┤                       │
  │                        (background)            │
  │                        ├─ match rules          │
  │                        ├────────────────────►│
```

### 4.3 SSE Live Subscription
```
Browser                    backend
  │ GET /api/events/stream?batch_id=X
  │   Authorization: Bearer <JWT>
  ├─────────────────────────►│
  │                           ├─ verify JWT → user_id
  │                           ├─ check visibility(user, batch)
  │                           │   if not visible → 403
  │                           ├─ return Content-Type: text/event-stream
  │ <─── event: job_epoch (keep-alive) ───┤
  │ <─── event: job_done ──────────────────┤
  │ (connection held until browser closes or times out)
  │
  Backend push mechanism:
    EventIngestService publishes to asyncio.Queue per-subscriber after each write
    SSE handler pulls events from Queue, serialises to SSE format, and pushes to browser
```

### 4.4 Share + Visibility Filtering
```
Browser B                  backend
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

## 5. Key Algorithms / Logic

### 5.1 Visibility Query

Encapsulated in `services/visibility.py`:

```python
class VisibilityResolver:
    async def visible_batch_ids(self, user_id: int, scope: str = 'all') -> SQL_clause:
        """
        scope:
          'mine'   → owner_id = user_id
          'shared' → batches that other users shared with me (batch_share + project_share)
          'public' → batches that have a public_share (public link exists)
          'all'    → mine ∪ shared (default; admin sees everything via this path)
        """
    
    async def can_edit(self, user_id, batch_id) -> bool: ...
    async def can_view(self, user_id, batch_id) -> bool: ...
```

Admin users bypass filtering (`visible_batch_ids` returns no-filter when `is_admin=True`).

### 5.2 Token Verification Middleware

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
        # API token branch
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
        # JWT branch
        try:
            payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception:
            raise HTTPException(401)
        user = await db.get(User, payload["user_id"])
        if not user or not user.is_active:
            raise HTTPException(401)
        return user

async def require_reporter_token(user: User = Depends(get_current_user)) -> User:
    """Only allows users authenticated with em_live_ tokens (scope=reporter). Used by POST /api/events."""
    # ...

async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user
```

### 5.3 Event Processing + Idempotency

```python
# services/event_ingest.py
class EventIngestService:
    async def ingest(self, event: EventIn, user: User, db: AsyncSession) -> dict:
        # 1. Idempotency check
        if event.event_id:
            existing = await db.scalar(
                select(Event.id).where(Event.event_id == event.event_id)
            )
            if existing:
                return {"accepted": True, "event_id": event.event_id, 
                        "db_id": existing, "deduplicated": True}
        
        # 2. Write event row
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
        await db.flush()  # get row.id
        
        # 3. Apply side effects based on event_type
        await self._apply_side_effects(event, user, db)
        
        await db.commit()
        
        # 4. Fire notifications + SSE broadcast (both fire-and-forget)
        asyncio.create_task(self._dispatch_notifications(event))
        asyncio.create_task(self._sse_broadcast(event))
        
        return {"accepted": True, "event_id": event.event_id, "db_id": row.id}
    
    async def _apply_side_effects(self, event, user, db):
        if event.event_type == 'batch_start':
            # create batch if it doesn't exist; owner = user
            await self._ensure_batch(event, user, db, creating=True)
        elif event.event_type in ('job_start', 'job_epoch', 'job_done', 'job_failed'):
            # ensure batch + job exist (auto-stub)
            await self._ensure_batch(event, user, db)
            await self._ensure_job(event, db)
            if event.event_type == 'job_done':
                await self._update_job_metrics(event, db)
                await self._recompute_batch_counters(event.batch_id, db)
            # ...
```

### 5.4 SSE Broadcast

Simple per-process in-memory pub/sub:

```python
# api/events_stream.py
class SSEHub:
    """In-process SSE subscription manager. Swap for Redis pub/sub for multi-process scaling."""
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
                except asyncio.QueueFull: pass  # drop frame

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

### 5.5 Rate Limiting

Per-token bucket, in-memory:

```python
# utils/ratelimit.py
class TokenBucket:
    """Leaky bucket per key. 60 tokens, 10/s refill = 600/min."""
    def __init__(self, capacity=60, refill_per_sec=10): ...
    async def try_consume(self, key: str, cost: int = 1) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
```

Applied in the `POST /api/events` middleware; key is the token_hash prefix. Returns 429 with `Retry-After` header on throttle.

## 6. Schema Evolution

Current v1.0. The `event_id` field is now required (client must generate a UUID). Update `schemas/event_v1.json`:

```jsonc
{
  "required": ["schema_version", "event_type", "timestamp", "batch_id", "source", "event_id"],
  "properties": {
    "event_id": {
      "type": "string",
      "format": "uuid",
      "description": "Client-generated UUID for idempotency"
    },
    // ... rest unchanged
  }
}
```

The backend accepts events without `event_id` for backward compatibility, but logs a warning; rejected responses include a `deprecated` flag.

## 7. Database Migration

Two-phase Alembic migration:

**001_initial**: retain existing Batch/Job/Event/ResourceSnapshot (already implemented by backend agent)

**002_auth_sharing**:
- Create new tables: user / api_token / email_verification / batch_share / project_share / public_share / audit_log / feature_flag
- `ALTER TABLE batch ADD COLUMN owner_id` + `is_deleted`
- `ALTER TABLE event ADD COLUMN event_id` + unique index
- Data backfill: for existing batches, `owner_id = NULL` (admin handles separately)

Upgrade: `alembic upgrade head`. Downgrade `alembic downgrade -1` drops only the new tables from 002.

## 8. Error Handling Standards

| Scenario | HTTP code | Body |
|---|---|---|
| Unauthenticated access to protected resource | 401 | `{"detail": "Authentication required"}` + `WWW-Authenticate: Bearer` |
| Authenticated but insufficient permission | 403 | `{"detail": "Forbidden", "required": "admin"}` |
| Resource not found | 404 | `{"detail": "Batch not found"}` |
| Invalid client input | 422 | Pydantic ValidationError JSON |
| Unsupported schema version | 415 | `{"detail": "Unsupported schema_version", "supported": ["1.0"]}` |
| Rate limit exceeded | 429 | `{"detail": "Rate limit exceeded"}` + `Retry-After: 5` |
| Server error | 500 | `{"detail": "Internal error", "trace_id": "<uuid>"}` + log |

Client reporter behaviour on 4xx:
- 401/403 → log.error (credential problem, no retry)
- 422/415 → log.error (schema problem, no retry)
- 404 → log.warning (batch may have been deleted, drop)
- 429 → read Retry-After and back off
- 5xx / network → exponential backoff 3× → spill

## 9. Logging / Audit

- **App log**: stdlib logging, JSON format, INFO+WARN+ERROR levels. Fields: timestamp / level / logger / trace_id / user_id / message
- **Audit log**: `audit_log` table. Trigger points:
  - User: register / login / password_change / email_verify
  - Token: create / revoke
  - Share: add / remove / public_share_create
  - Batch: delete (owner)
  - Admin: user_ban / feature_flag_change

## 10. Deployment

### 10.1 Docker Single Image

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

### 10.3 Backup

- SQLite: hourly `sqlite3 monitor.db ".backup /backup/monitor-$(date +%Y%m%d-%H).db"`
- Simple cron + rsync to backup directory

## 11. Self-Monitoring (meta-monitoring)

- `/health` returns 200 (basic liveness)
- `/metrics` Prometheus format (phase 2)
- FastAPI error logs → systemd journal
- Disk space alert: email notification to admin when `disk_free_mb < 5 GB`

## 12. Development Workflow

### 12.1 Local Development

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

# Terminal 3: client (for testing)
cd client
pip install -e ".[dev]"
pytest

# Open browser at http://localhost:5173
# Register first user → becomes admin
# Settings → Tokens → generate em_live_xxx
# Write to DeepTS-Flow-Wheat/scripts/monitor.yaml
# Run a smoke benchmark to see live data
```

### 12.2 Test Matrix

| Layer | Tool | Coverage |
|---|---|---|
| Backend unit | pytest + httpx.AsyncClient | service layer + auth + visibility |
| Backend integration | pytest + in-memory SQLite | full API end-to-end |
| Frontend unit | vitest | stores + utilities |
| Frontend e2e | Playwright | login → register → post event → view result |
| Client | pytest + pytest-httpserver | queue + retry + spill |
| Cross-repo integration | handwritten shell script | start monitor → DeepTS-Flow real POST → assert data appears |

### 12.3 CI (phase 2)

- GitHub Actions (if moved to GitHub) / GitLab CI
- Run backend/frontend/client tests and build Docker image on every push

## 13. Team Collaboration

- **product-manager**: reads requirements + design → breaks into tasks → assigns → collects results → reports to user
- **backend-eng**: full backend stack, iterates features as directed by PM
- **frontend-eng**: full frontend stack
- **client-eng**: maintains reporter library
- **qa-eng**: writes cross-repo integration tests, load tests, security audits, signs off on releases

Communication protocol:
- PM assigns tasks to eng via SendMessage
- Eng reports back to PM with results + risks via SendMessage
- QA performs black-box testing after eng finishes, reports to PM via SendMessage
- PM consolidates and reports to user via SendMessage

All members share documents under `<repo_root>/docs/` as the single source of truth. All code lives in the corresponding subdirectory under `<repo_root>/`.
