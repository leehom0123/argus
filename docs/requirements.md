> 🌐 **English** · [中文](./requirements.zh-CN.md)

# Argus — Product Requirements Document (PRD) v1

> This document is the single source of truth for the MVP. All backend / frontend / client implementations follow it.
> When in doubt, update this document first, then update the code.

## 1. Product Positioning

Argus is a standalone experiment monitoring WebUI + event ingestion service for ML research.

- **Audience**: research groups and individuals; multi-user with collaboration
- **Integrations**: any experiment platform (MVP priority: DeepTS-Flow-Wheat; future: any Python training framework)
- **Core value**: no need to watch tmux while a benchmark runs — results are accessible from a phone or browser; finished results can be shared with collaborators with one click, without sharing server access

## 2. Scope

### 2.1 In scope (MVP)

1. User registration / login / logout (**open registration**, no approval required)
2. **First registered user automatically becomes admin**
3. Personal API tokens: users generate multiple tokens from the UI, used by experiment runners to POST events
4. Multi-user data isolation: each batch is visible only to its owner by default
5. Three sharing granularities:
   - **Project-level share**: share all of my batches in a project with another user (they automatically see all batches under that project)
   - **Batch-level share**: share a single batch with one or more users
   - **Public link**: generate a read-only URL accessible without login (for paper figure reproduction)
6. Two permission levels: `viewer` / `editor`
7. Web UI: login/register page, batch list (My / Shared / All), batch detail (matrix + timeline), job detail (loss curve + metrics + log), host resource timeseries, settings (Profile / Tokens / Shares)
8. Email delivery: registration verification + password reset + optional notifications (registration verification is sufficient for MVP)
9. Notification rule engine: server-side rules push to Feishu / (extensible) WhatsApp / Slack / ...
10. Communication protocol: HTTP REST uplink, HTTP polling + SSE downlink (see §6)
11. OAuth **extension point**: backend `AuthProvider` abstract interface, reserved for future GitHub/Google integration; not implemented in MVP

### 2.2 Out of scope (Phase 2 and later)

- Actual OAuth provider integration (GitHub / Google / internal LDAP)
- Teams / Organizations (users belong to an org, org-level sharing)
- Per-user notification routing (each user with their own Feishu webhook)
- Comments / tags / @-mentions
- Comparison view (side-by-side charts for batch vs batch)
- CSV / LaTeX export
- Batch re-run (requires reverse call into the experiment platform)
- Automatic data retention cleanup / archive UI
- Audit log UI (MVP writes to log only; no UI)
- Invite-code registration (requirements choose "open registration"; invite-code path is reserved as an admin-toggleable feature flag)

## 3. User Roles

| Role | Permissions |
|---|---|
| **anonymous** | Login/register/public-link pages only; all API returns 401 except `/api/auth/*` and `/api/public/*` |
| **user** | CRUD own batches/jobs/tokens/shares; read-only access to batches shared with them |
| **admin** | All user permissions, plus visibility of all users and all batches; can ban users; can toggle global feature flags (e.g. registration switch) |

The first registered user is automatically `is_admin=true`; all subsequent registrations are `is_admin=false`.

## 4. Authentication and Authorisation

### 4.1 Credential Types

| Credential | Format | Purpose | Lifetime |
|---|---|---|---|
| **Session JWT** | `Bearer <jwt>` header | Web UI (browser) | 24 hours; auto-renewed 30 minutes before expiry |
| **Personal API Token** | `Bearer em_live_<20 random chars>` | Experiment-side reporter to POST events | Long-lived; explicitly revoked; optional `expires_at` (UI offers 30 days / 90 days / never) |
| **Viewer Token** (optional) | `Bearer em_view_<20 chars>` | Read-only; can read own data only; suitable for "letting someone view your account without full access" (use with caution) | Same as API token |
| **Public Share Slug** | URL `/public/<slug>` | Anonymous read-only access to a specific batch | Optional expiry |

Token prefix convention: `em_live_` = ingest + read, `em_view_` = read-only.

### 4.2 Storage

- User passwords: **argon2id** hash (not bcrypt; argon2id is the modern standard)
- Tokens: store **SHA-256 hash only**; plaintext is returned to the user once at generation time (UI prompts user to copy and save it)
- JWT signing key: read from environment variable `ARGUS_JWT_SECRET` at startup; at least 32 bytes of randomness recommended

### 4.3 Login Failure Protection

- 5 consecutive failures lock the account for 10 minutes (per username)
- Invalid API token → 401 + `WWW-Authenticate: Bearer`
- Rate limit: 600 requests per minute per token (~10 req/s); excess returns 429

### 4.4 Password Policy

- Minimum 10 characters + at least 1 letter + 1 digit (research context; no heavy complexity requirements)
- UI shows strength indicator at registration
- Password change requires current password
- Password reset: email link (token valid for 15 minutes)

### 4.5 Email

- SMTP config read from `monitor.yaml` (or environment variables)
- Events: registration verification (email must be verified before login), password reset, (Phase 2: daily digest, important notifications)
- Verification email valid for 24 hours; unverified accounts automatically deleted after 7 days

## 5. Data Model

### 5.1 New Tables

```sql
-- Users
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
    locked_until       TEXT                  -- NULL or ISO timestamp
);

-- Personal API tokens
CREATE TABLE api_token (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                -- user-assigned label: "laptop-reporter"
    token_hash  TEXT NOT NULL UNIQUE,         -- SHA-256
    prefix      TEXT NOT NULL,                -- "em_live_" / "em_view_"
    display_hint TEXT NOT NULL,               -- first 8 chars in plaintext, for UI identification (avoids exposing full token)
    scope       TEXT NOT NULL,                -- 'reporter' | 'viewer'
    created_at  TEXT NOT NULL,
    last_used   TEXT,
    expires_at  TEXT,                         -- optional
    revoked     BOOLEAN DEFAULT 0
);

-- Email verification tokens (one-time use)
CREATE TABLE email_verification (
    token       TEXT PRIMARY KEY,             -- random URL-safe
    user_id     INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                -- 'verify' | 'reset_password'
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed    BOOLEAN DEFAULT 0
);

-- Batch-level sharing
CREATE TABLE batch_share (
    batch_id    TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    grantee_id  INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,                -- 'viewer' | 'editor'
    created_at  TEXT NOT NULL,
    created_by  INTEGER REFERENCES user(id),
    PRIMARY KEY (batch_id, grantee_id)
);

-- Project-level sharing (owner shares all their batches within a project)
CREATE TABLE project_share (
    owner_id    INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    project     TEXT NOT NULL,                -- 'DeepTS-Flow-Wheat'
    grantee_id  INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (owner_id, project, grantee_id)
);

-- Public links
CREATE TABLE public_share (
    slug        TEXT PRIMARY KEY,             -- 20-char URL-safe random
    batch_id    TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    created_by  INTEGER REFERENCES user(id),
    expires_at  TEXT,
    view_count  INTEGER DEFAULT 0,
    last_viewed TEXT
);

-- Audit log (MVP: write only; UI in phase 2)
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

-- Feature flags (admin-toggleable)
CREATE TABLE feature_flag (
    key         TEXT PRIMARY KEY,             -- 'registration_open' | 'email_required' | 'invite_only' ...
    value       TEXT NOT NULL,                -- JSON
    updated_at  TEXT,
    updated_by  INTEGER REFERENCES user(id)
);
```

### 5.2 Modifications to Existing Tables

```sql
ALTER TABLE batch ADD COLUMN owner_id INTEGER REFERENCES user(id);
ALTER TABLE batch ADD COLUMN is_deleted BOOLEAN DEFAULT 0;  -- soft delete; hard delete after 7 days
CREATE INDEX idx_batch_owner ON batch(owner_id);
CREATE INDEX idx_batch_project_owner ON batch(project, owner_id);

-- Add idempotency key to event table (see §6.3)
ALTER TABLE event ADD COLUMN event_id TEXT;   -- client-generated UUID
CREATE UNIQUE INDEX idx_event_id ON event(event_id) WHERE event_id IS NOT NULL;
```

## 6. Communication Protocol

### 6.1 Uplink: Experiment → Monitor

**HTTP REST + JSON + Bearer token**:

```
POST /api/events
  Authorization: Bearer em_live_<token>
  Content-Type: application/json
  Body: <single event>
  Response: 200 {accepted: true, event_id: "<client-uuid>"}

POST /api/events/batch
  Authorization: Bearer em_live_<token>
  Body: {events: [<event>, ...]}    # max 500 events
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

- Normal operation: one POST per event; spill replay uses batch endpoint
- Client uses `requests.Session` keep-alive to reduce handshake overhead
- Normal response < 100 ms; timeouts: connect=2 s, read=5 s, total=10 s
- 5xx / network errors: exponential backoff 3×; on continued failure → local spill `~/.argus-reporter/spill-<pid>-<ts>.jsonl`

### 6.2 Downlink: Monitor → Browser

- **List pages / historical data**: HTTP polling (5 s interval), client polls independently
- **Live job detail / running batch**: SSE `GET /api/events/stream?batch_id=X&job_id=Y`
  - `text/event-stream`, auto-reconnect
  - Server pushes matching new events
  - Client uses `new EventSource(...)`, no additional library

### 6.3 Idempotency

Each event carries a client-generated UUID `event_id` (required from schema v1.0 onward):

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "schema_version": "1.0",
  "event_type": "job_epoch",
  ...
}
```

- Server deduplicates by `event_id`: a duplicate POST returns 200 with the original db_id, without inserting a second row
- Spill replay is essentially a retry — idempotency guarantees no duplicate records
- `schemas/event_v1.json` must be updated to add `event_id` as a required field

### 6.4 Rate Limiting

- Per-token: 600 req/min (~10 req/s)
- Excess returns `429 Too Many Requests` + `Retry-After: <seconds>` header
- Client on 429: back off by `Retry-After` value or default 30 s

### 6.5 Schema Version

- Every request body must include `schema_version`
- Server accepts only `"1.0"`; any other value returns `415 Unsupported Media Type` with supported version list
- When v2.0 is introduced, both v1.0 and v2.0 will be accepted for a transition period

### 6.6 CORS / TLS

- CORS: allow `http://localhost:5173` in development; allow the service's own domain in production
- TLS: HTTPS required in production; nginx + Let's Encrypt reverse proxy; HTTP acceptable in development

## 7. API Endpoint List (v1)

### 7.1 Authentication

```
POST   /api/auth/register              {username, email, password, invite_code?}
POST   /api/auth/login                 {username_or_email, password}  → {access_token, user}
POST   /api/auth/logout                (invalidate JWT — blacklist implementation)
POST   /api/auth/verify-email          {token}
POST   /api/auth/request-password-reset  {email}
POST   /api/auth/reset-password        {token, new_password}
POST   /api/auth/refresh               (JWT renewal)
GET    /api/auth/me                    → {id, username, email, is_admin, email_verified}
```

### 7.2 Token Management (requires login)

```
GET    /api/tokens                     → [{id, name, prefix, display_hint, scope, created_at, last_used, expires_at}]
POST   /api/tokens                     {name, scope, expires_at?}  → {id, token: "em_live_FULL_ONLY_SHOWN_ONCE", ...}
DELETE /api/tokens/{id}                (revoke)
```

### 7.3 Event Ingestion (requires Personal API Token)

```
POST   /api/events                     (§6.1)
POST   /api/events/batch               (§6.1)
GET    /api/events/stream              (SSE; requires login; filtered by visible batches)
```

### 7.4 Data Query (requires login; filtered by visibility)

```
GET    /api/batches?scope=mine|shared|all&user=&project=&status=&since=&limit=&offset=
GET    /api/batches/{batch_id}
GET    /api/batches/{batch_id}/jobs
GET    /api/jobs/{batch_id}/{job_id}
GET    /api/jobs/{batch_id}/{job_id}/epochs
GET    /api/jobs/{batch_id}/{job_id}/logs?since=&limit=   (phase 2 — placeholder in MVP)
GET    /api/resources/hosts
GET    /api/resources?host=&since=&limit=
```

**Visibility rule** (enforced by backend middleware):
```
visible(user, batch) =
    batch.owner_id == user.id
  OR (user.id, batch.id) ∈ batch_share
  OR (batch.owner_id, batch.project, user.id) ∈ project_share
  OR user.is_admin
```

### 7.5 Share Management (requires login)

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

### 7.6 Admin

```
GET    /api/admin/users                       → list all
POST   /api/admin/users/{id}/ban              (set is_active=false)
POST   /api/admin/users/{id}/unban
GET    /api/admin/feature-flags
PUT    /api/admin/feature-flags/{key}         {value}
GET    /api/admin/audit-log?since=&limit=
```

## 8. Frontend Page List

### 8.1 Accessible Without Login

- `/login` — username/email + password
- `/register` — username + email + password + invite code (when enabled)
- `/verify-email?token=X` — email link click-through; completes verification
- `/reset-password?token=X` — same
- `/public/<slug>` — public batch display (read-only; visually consistent with BatchDetail but without action buttons)

### 8.2 Requires Login

- `/` → redirect `/batches?scope=mine`
- `/batches?scope=mine|shared|all` — batch list (tab-switched)
- `/batches/:batchId` — batch detail (matrix + timeline + share button)
- `/batches/:batchId/jobs/:jobId` — job detail (loss curve + metrics + log)
- `/hosts` + `/hosts/:host` — resource monitoring
- `/settings/profile` — change password, email
- `/settings/tokens` — token management
- `/settings/shares` — shares I have created / shares granted to me (overview)

### 8.3 Admin Only

- `/admin/users`
- `/admin/feature-flags`
- `/admin/audit-log`

## 9. Notification System

### 9.1 Rule Engine

Rule file at `backend/config/notifications.yaml` (global) + phase 2 extends to per-user overrides.

```yaml
rules:
  - when: event_type == "job_failed"
    push: [feishu]
  - when: event_type == "batch_done" and data.n_failed > 0
    push: [feishu]
  - when: event_type == "resource_snapshot" and data.gpu_util_pct < 5
    push: [feishu]
    throttle: 1800    # push at most once every 30 minutes
```

### 9.2 Channel Interface

```python
class BaseNotifier(ABC):
    async def send(self, title: str, body: str, level: str = 'info',
                   context: dict = None) -> None: ...
```

MVP implementation: `FeishuNotifier`.

Extension points: `WhatsAppCallMeBotNotifier`, `WhatsAppTwilioNotifier`, `SlackNotifier`, `TelegramNotifier`, `EmailNotifier`, `WebhookNotifier` (generic).

## 10. Deployment

### 10.1 Target Architecture

```
Single server:
  - backend (uvicorn :8000) as a background service
  - frontend dist mounted by backend StaticFiles at /
  - SQLite database at backend/data/monitor.db
  - systemd manages the process
  - nginx reverse proxy + TLS (Let's Encrypt)
```

### 10.2 Environment Variables

```
ARGUS_JWT_SECRET              JWT signing key (required, >=32 bytes)
ARGUS_DB_URL                  default sqlite:///data/monitor.db
ARGUS_SMTP_HOST/PORT/USER/PASS  email config
ARGUS_SMTP_FROM               sender address
ARGUS_BASE_URL                https://monitor.example.com (used to construct links in email)
ARGUS_FEISHU_WEBHOOK          default global Feishu (phase 2: per-user overrides)
ARGUS_LOG_LEVEL               info/debug
```

### 10.3 Startup Procedure

```bash
cd backend
pip install -e ".[dev]"
alembic upgrade head              # database migration
uvicorn backend.app:app --host 0.0.0.0 --port 8000

cd ../frontend
npm install && npm run build      # dist/ served by backend

# systemd unit: see deploy/systemd/
```

## 11. Security Requirements

- All passwords: argon2id
- All tokens: store hash only
- JWT secret: 32+ bytes
- HTTPS required in production
- SQL parameterisation (ORM default)
- XSS: frontend renders user input via Vue auto-escaping
- CSRF: Bearer header authentication is inherently CSRF-resistant (not cookie-based)
- Rate limiting: per-token + per-IP double protection
- Login failure lockout
- Audit log for all critical operations

## 12. OAuth Extension Point (reserved for v1)

`backend/auth/providers/base.py`:

```python
class AuthProvider(ABC):
    name: str   # 'local' | 'github' | 'google'
    @abstractmethod
    async def authenticate(self, credentials: dict) -> User | None: ...
    @abstractmethod
    async def get_redirect_url(self, state: str) -> str: ...       # for OAuth flow
    @abstractmethod
    async def handle_callback(self, code: str, state: str) -> User: ...
```

MVP implements `LocalAuthProvider` (username + password) only.

Phase 2 adds `GitHubAuthProvider` / `GoogleAuthProvider` and extends the registration flow to support binding multiple providers to the same user.

## 13. Key Constraints

- **Zero intrusion on the experiment platform**: the entire integration requires only 3 new files + 2–3 line changes in DeepTS-Flow-Wheat; monitoring is **entirely optional** (if `scripts/monitor.yaml` is not configured, this service is effectively invisible)
- **Fire-and-forget**: reporter network call failures never raise exceptions into training code
- **Schema constraint**: events must pass validation against `schemas/event_v1.json`; schema changes must bump the version
- **Reusable contract**: the same monitor service can accept events from any number of Python experiment projects (not limited to DeepTS-Flow)

## 14. Acceptance Criteria

MVP is complete when all of the following are demonstrable:

1. ✓ Two users A and B each register, verify email, and log in
2. ✓ A generates an API token and writes it into DeepTS-Flow-Wheat's `scripts/monitor.yaml`
3. ✓ A runs a benchmark; the web UI shows the batch + jobs + per-epoch loss curve in real time
4. ✓ A shares a batch with B (viewer permission)
5. ✓ B logs in and can see that batch (in "Shared with me" tab) but not A's other batches
6. ✓ A shares the entire project `DeepTS-Flow-Wheat` with B
7. ✓ B can now see **all** of A's batches under that project
8. ✓ A generates a public link; anyone can view the result without logging in
9. ✓ A revokes the API token; next POST from DeepTS-Flow-Wheat returns 401
10. ✓ Admin can view all users at /admin/users
11. ✓ A `job_failed` event triggers a Feishu push notification
12. ✓ Reporter client: events spill during 5 minutes of network outage, auto-replay after recovery, with no lost or duplicate records

## 15. Open Discussion Points

- What exactly can `editor` permission do with a batch? Suggestion: rename / add tags / manually mark a failed job / delete the entire batch; cannot modify historical event data
- Where should phase 2 per-user Feishu webhook config be stored? Add a field to the user table? A separate `user_notification` table?
- Should the public link page show a lightweight "log in for full features" prompt to anonymous visitors?
- When admin toggles `registration_open=false`, what should happen to accounts that have registered but not yet verified their email?

---

## 16. Information Architecture (IA) and Dashboard / Project Board

> This chapter supplements §8 with a complete IA specification for the MVP.

### 16.1 Three-Level Structure

```
/                                    Dashboard (global board)
/projects                            project list
/projects/:project                   project detail (card-based, main view)
/projects/:project/batches/:id       batch detail (defined in §8)
/projects/:project/batches/:id/jobs/:jobId   job detail (defined)
/hosts /hosts/:host                  host board
/compare                             compare pool side-by-side
```

### 16.2 Dashboard (home page `/`)

**Scope**: default is "my batches + shared with me" (excludes anonymous public; search for public batches separately); admin can switch to "all".

**Top metric strip** (6 stat cards):
- Running batches (within my visible scope)
- Jobs currently running
- Jobs completed in last 24 h
- Jobs failed in last 24 h (click to jump to filtered list)
- Active hosts (with a resource_snapshot in the last 5 minutes)
- Average GPU utilisation across all active hosts

**Main area layout** (3-column, 12-grid):
- **Left 8 columns**:
  1. Project card grid (2–3 responsive columns), sorted by "activity > last event time", starred items pinned first; filter: `Mine / Shared / All`
  2. Activity feed: last 20 events (batch_start / batch_done / batch_failed / job_failed)
- **Right 4 columns**:
  3. Host status cards: one per active host, with GPU util bar, VRAM bar, CPU util bar, RAM bar, disk_free (<10 GB = red alert), and count of running jobs on that host
  4. Notification panel: unread = recent failures / rate limit hits / token nearing expiry / new shares granted to me
  5. Quick-action buttons: generate new token, create new share (conditionally shown based on permissions)

### 16.3 Project Detail (`/projects/:project`)

**Header three rows**:
- Row 1: project name + [Share] [Public link] [Star ★] buttons + owner + collaborators + created date
- Row 2: aggregate metric strip (total batches / new this week / failure rate / cumulative GPU-hours / historical best metric)
- Row 3: tab switcher `Active 🟢 | Recent | Leaderboard | Matrix | Resources | Collaborators`

**Active tab (core view)**: card stream, one card per batch:

```
┌──────────────────────────────────────────────────────────┐
│ 🟢 <batch_id>  user: X  host: Y                          │
│ progress ████████░░  68/120 (57%)                        │
│ Running 3/3 slots:                                       │
│   [model × dataset] epoch 14/50 val_loss 0.38 ↓          │
│   [model × dataset] epoch 6/50 val_loss 0.51 ↓           │
│   [model × dataset] epoch 9/50 val_loss 0.42 ↓           │
│ elapsed 12h 4m  |  ETA 5h 12m  (EMA-based)               │
│ GPU 94%  VRAM 45GB  |  disk 149GB free                   │
│ mini sparkline: job completion trend (last 24 h)         │
│ ✗ 2 failed  ⚠ 1 stalled (no events 8 min)               │
│ [View Matrix] [View Jobs] [Share] [Pin ⚓]               │
└──────────────────────────────────────────────────────────┘
```

**Recent tab**: completed batch cards (with best metric, matrix thumbnail, duplicate-config button)
**Leaderboard tab**: best metric per (model, dataset) aggregated across batches; CSV export available (§16.7)
**Matrix tab**: models × datasets heat map, colour = best metric
**Resources tab**: GPU-hours trend for this project, time-of-day heat map, host distribution
**Collaborators tab**: owner manages shares; collaborators are read-only

### 16.4 Card Interactions

- **Full card is clickable**: primary action = go to detail (click anywhere on card); 3–4 secondary action buttons inside (Share / Pin / Compare / View) have independent click handling (stopPropagation)
- **Status is visual**: 🟢 running / ✅ done / ❌ failed / ⚪ pending / ⏸ stalled
- **Live refresh**: only running cards subscribe to SSE; done cards do not refresh, conserving resources
- **In-card mini chart**: loss sparkline (30 px height) / progress bar; hover shows full tooltip
- **Responsive**: single column on mobile; mini chart is preserved

### 16.5 Derived Field Definitions

| Field | Definition / Algorithm |
|---|---|
| **ETA** | Exponential moving average of `elapsed_s` across the last 10 completed jobs × pending_count; α=0.3 |
| **is_stalled** | `status=running AND now - max(event.timestamp) > 300 s` (5 minutes; admin can adjust via feature_flag `stalled_threshold_sec`) |
| **GPU-hours** | `sum(jobs.elapsed_s) / 3600` (assumes single GPU ×1; when `resource_snapshot.gpu_count` is available, weight accordingly) |
| **completion_pct** | `(n_done + n_failed) / n_total * 100` |
| **best_metric** | `MIN(metrics.MSE)` across scope (project / batch); metric column is switchable |
| **is_running** | `job.status=running AND batch.status=running AND now - last_event < 5 min` |

### 16.6 Star (Favourite)

**Schema**:
```sql
CREATE TABLE user_star (
    user_id        INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    target_type    TEXT NOT NULL,           -- 'project' | 'batch'
    target_id      TEXT NOT NULL,           -- project name or batch_id
    starred_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, target_type, target_id)
);
```

**Behaviour**: starred projects appear first on the Dashboard; the batch list supports star filtering; the settings page shows "my starred items".

### 16.7 Compare Pool — Pin

**Schema**:
```sql
CREATE TABLE user_pin (
    user_id        INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    batch_id       TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    pinned_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, batch_id)
);
```

**UI**:
- Each batch card has a "Pin ⚓" button
- Top nav shows "N pinned" badge
- `/compare` page: side-by-side display of 2–4 pinned batches with parallel loss curves, metrics comparison table, and matrix diff heat map
- Maximum 4 batches (UI space constraint)
- Pins are session-persistent (stored in DB, not localStorage)

### 16.8 CSV Export

**Endpoints**:
```
GET /api/batches/{batch_id}/export.csv        → batch leaderboard CSV
GET /api/projects/{project}/export.csv        → project leaderboard CSV (cross-batch aggregate)
GET /api/projects/{project}/export-raw.csv    → per-(batch, model, dataset, metric) detail
GET /api/compare/export.csv?batches=a,b,c     → compare pool export
```

CSV columns include at minimum: batch_id, model, dataset, status, epochs, elapsed_s, MSE, MAE, RMSE, R2, PCC, MAPE, GPU_peak_mb, ...; the frontend "Export CSV" button triggers a direct browser download.

### 16.9 Public Link Page

**Displays the same information as a logged-in user** (no simplification): batch detail matrix / timeline / per-job loss curve + metrics + SHAP plots.
Only 3 actions are disabled:
- Share button (cannot add further shares from a public link)
- Pin button (anonymous visitors cannot pin)
- Export CSV (requires login to prevent bulk scraping)

### 16.10 Project Identification

**Fully automatic**: the `source.project` field value serves as the project identifier; there is no explicit "create project" flow.
- A project is registered on first appearance (no additional DB table; project list is dynamically derived from `GROUP BY batch.project`)
- Case-sensitive (`DeepTS-Flow-Wheat` ≠ `deepts-flow-wheat`)
- Rename: not supported; admin can perform `UPDATE batch SET project=... WHERE project=...` on request

### 16.11 Additional Card Information (included in MVP)

Every card and detail page must display:
1. Model parameter count / file size (from job metrics)
2. Per-job loss mini sparkline (30 px on card; full chart on detail page)
3. Current epoch / total epochs (`14/50`)
4. Batch command with one-click copy (hover button)
5. Git commit hash (batch source extension field; displayed on public page for paper reproducibility)
6. Environment fingerprint (Python / torch / CUDA version; source extension field)
7. Runtime alerts (GPU >85°C / disk <10 GB / OOM marker) → red dot in card upper-right corner + notification panel
8. Best-so-far indicator (`Already 3.2% above historical best`) → banner at top of batch active tab
9. Notification badge (unread count on nav)
10. Quick filter chips: `Only mine / Only failed / Last 24 h` one-click filters

## 17. IA-Derived API Endpoints (supplement §7)

```
GET  /api/dashboard                          top metrics + projects + activity + hosts + notifications (single aggregated call, avoids frontend N+1)
GET  /api/projects                           project list (auto-inferred; deduplicated from batch.project)
GET  /api/projects/{project}                 project detail aggregate
GET  /api/projects/{project}/active-batches  currently running batches + their running jobs (SSE subscribes to same data stream)
GET  /api/projects/{project}/leaderboard    best result per combination across batches
GET  /api/projects/{project}/matrix         models × datasets aggregate
GET  /api/projects/{project}/resources      GPU-hours / time-of-day heat map
POST /api/stars                             {target_type, target_id}
DELETE /api/stars/{target_type}/{target_id}
GET  /api/stars                             my starred items
POST /api/pins                              {batch_id}
DELETE /api/pins/{batch_id}
GET  /api/pins                              my pinned items
GET  /api/compare?batches=a,b,c             compare pool data
GET  /api/batches/{id}/health               {is_stalled, last_event_age_s, failure_count, warnings}
GET  /api/batches/{id}/eta                  EMA-based estimate
GET  /api/batches/{id}/export.csv
GET  /api/projects/{project}/export.csv
GET  /api/projects/{project}/export-raw.csv
GET  /api/compare/export.csv?batches=...
```

## 18. Updated Acceptance Criteria (extends §14)

In addition to the original 12 criteria:

13. ✓ Logged-in user Dashboard shows count of "my running batches", project cards, activity feed, and host status panel
14. ✓ Project detail Active tab lists currently running batches as cards, each with progress bar, running jobs list, ETA, GPU/VRAM, and stalled warnings
15. ✓ Star a project → it appears pinned first on Dashboard
16. ✓ Pin 2–4 batches → `/compare` shows side-by-side comparison (loss curves + metrics table + matrix diff)
17. ✓ Any batch or project leaderboard can be exported to CSV with one click
18. ✓ Public link page shows full information (matrix / per-job loss / metrics), with Share / Pin / Export buttons disabled
19. ✓ A job with no events for 5 minutes and status=running → UI shows "stalled" orange indicator
20. ✓ ETA is based on EMA of the last 10 completed jobs and adapts accurately when benchmark phases change speed
21. ✓ Dashboard default scope includes only "mine + shared with me"; admin can switch to "All" for global view
22. ✓ GPU >85°C or disk <10 GB → simultaneous alert on host card + notification panel
