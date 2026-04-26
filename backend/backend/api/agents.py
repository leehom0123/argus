"""``/api/agents`` — host-agent lifecycle (#103 v0.1.5 slice).

A host running ``argus-agent`` (the Sibyl-side daemon) authenticates with
a JWT once at startup, registers via :endpoint:`POST /api/agents/register`,
and then polls :endpoint:`GET /api/agents/{id}/jobs` on its agent token.

The poll endpoint returns pending :class:`backend.models.AgentCommand`
rows targeted at this host. The agent spawns the requested subprocess
and acks via :endpoint:`POST /api/agents/{id}/jobs/{cmd_id}/ack`. Stop
commands are picked up the same way.

The agent reports liveness via
:endpoint:`POST /api/agents/{id}/heartbeat` so the dashboard can show
"agent online: yes/no" next to a host. We don't drop or expire stale
agents in v0.1.5 — that's an admin housekeeping job for v0.1.6.

Authentication model
--------------------
* ``POST /api/agents/register`` — JWT (the human installs the agent and
  posts their own JWT to claim it). Returns the freshly-minted
  ``agent_token`` exactly once; subsequent polls authenticate with it.
* ``GET /jobs`` / ``POST /jobs/{cmd_id}/ack`` / ``POST /heartbeat`` —
  agent token (validated against ``agent_host.agent_token_hash``).

The agent token is a 32-byte URL-safe random string with the
``ag_live_`` prefix to keep it visually distinct from the reporter
``em_live_`` token. We never reuse the ApiToken table for agents
because the auth pipeline already gates ``em_*`` to user resolution;
mixing in agent-scope tokens there would require touching every
:class:`backend.deps.get_current_user` call site.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import AgentCommand, AgentHost, Batch, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


# Default poll interval suggested to fresh agents. Aligns with the
# existing reporter ``stop-requested`` poll (currently 5 s) but is set
# longer here because rerun events are rare compared to per-batch stop
# polls. The agent honours the value from the register response.
_DEFAULT_POLL_INTERVAL_S = 10

# Agent tokens use a different prefix from reporter tokens so they're
# easy to tell apart in logs / DB rows. ``32`` byte body → ~43 char
# base64 string + 8-char prefix → ~51 char token. Plenty of entropy.
_AGENT_TOKEN_PREFIX = "ag_live_"
_AGENT_TOKEN_BYTES = 32


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _hash_agent_token(token: str) -> str:
    """SHA-256 hex digest — same convention as :func:`backend.auth.tokens.hash_token`."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_agent_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256_hash)`` for a freshly minted token."""
    plaintext = _AGENT_TOKEN_PREFIX + secrets.token_urlsafe(_AGENT_TOKEN_BYTES)
    return plaintext, _hash_agent_token(plaintext)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AgentRegisterIn(BaseModel):
    """Request body for ``POST /api/agents/register``."""

    model_config = {"extra": "forbid"}

    hostname: str
    version: str | None = None
    capabilities: list[str] = []


class AgentRegisterOut(BaseModel):
    """Response body — the only place plaintext ``agent_token`` ever appears."""

    model_config = {"extra": "forbid"}

    agent_id: str
    agent_token: str
    poll_interval_s: int
    server_time_utc: str


class AgentJobOut(BaseModel):
    """A pending :class:`AgentCommand` row, projected for the agent's poll loop."""

    model_config = {"extra": "forbid"}

    id: str
    kind: str
    batch_id: str
    payload: dict[str, Any]
    created_at: str


class AgentJobsListOut(BaseModel):
    """Wrapper around the poll response so we can extend with pagination."""

    model_config = {"extra": "forbid"}

    jobs: list[AgentJobOut]


class AgentAckIn(BaseModel):
    """Request body for ``POST /api/agents/{id}/jobs/{cmd_id}/ack``."""

    model_config = {"extra": "forbid"}

    # ``started`` flips the new batch from ``requested`` to ``running``;
    # ``failed`` keeps the batch in ``requested`` and writes the error.
    status: str  # 'started' | 'failed'
    pid: int | None = None
    error: str | None = None


class AgentAckOut(BaseModel):
    """Echo of the ack write so agents can verify it landed."""

    model_config = {"extra": "forbid"}

    cmd_id: str
    status: str


class HeartbeatIn(BaseModel):
    """Optional heartbeat payload — extensible for future telemetry."""

    model_config = {"extra": "forbid"}

    note: str | None = None


# ---------------------------------------------------------------------------
# Auth helper — agent token branch
# ---------------------------------------------------------------------------


async def _resolve_agent_from_token(
    db: AsyncSession,
    authorization: str | None,
) -> AgentHost:
    """Map an ``Authorization: Bearer ag_live_…`` header to its agent.

    Raises 401 on missing / unknown tokens. We do NOT bump
    ``last_seen_at`` here — the heartbeat / poll endpoints touch it
    explicitly so a misbehaving agent that only acks but never polls
    still updates the timestamp.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="agent token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token.startswith(_AGENT_TOKEN_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent token prefix",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_hash = _hash_agent_token(token)
    row = (
        await db.execute(
            select(AgentHost).where(AgentHost.agent_token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="agent token unknown",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return row


async def _require_agent_match(
    agent_id: str,
    authorization: Annotated[str | None, Header()],
    db: AsyncSession,
) -> AgentHost:
    """Resolve the agent from the token *and* verify its ID in the path.

    Prevents one agent's token from acking another's commands. We could
    drop the path-param entirely and trust the token, but keeping it
    matches the architect's contract sketch in the design doc and makes
    log lines easier to read.
    """
    agent = await _resolve_agent_from_token(db, authorization)
    if agent.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent token does not match path agent_id",
        )
    return agent


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=AgentRegisterOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_agent(
    payload: AgentRegisterIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRegisterOut:
    """Register a fresh ``argus-agent`` daemon and mint its token.

    Idempotent on ``hostname`` for the same user — a re-register just
    rotates the token (plaintext returned again). This is intentional:
    if an agent restart loses its token from disk it should be able to
    bootstrap without admin intervention. The previous token's hash is
    overwritten in place so the old plaintext is no longer accepted.
    """
    now = _utcnow_iso()

    existing = (
        await db.execute(
            select(AgentHost)
            .where(AgentHost.hostname == payload.hostname)
            .where(AgentHost.owner_id == user.id)
        )
    ).scalar_one_or_none()

    plaintext, token_hash = _generate_agent_token()
    capabilities = json.dumps(payload.capabilities, ensure_ascii=False)

    if existing is not None:
        existing.agent_token_hash = token_hash
        existing.capabilities_json = capabilities
        existing.version = payload.version
        existing.last_seen_at = now
        agent_id = existing.id
        log.info(
            "agent re-registered: id=%s hostname=%s user=%d",
            agent_id, payload.hostname, user.id,
        )
    else:
        agent_id = f"agent-{_uuid_mod.uuid4().hex[:12]}"
        db.add(
            AgentHost(
                id=agent_id,
                hostname=payload.hostname,
                agent_token_hash=token_hash,
                capabilities_json=capabilities,
                version=payload.version,
                registered_at=now,
                last_seen_at=now,
                owner_id=user.id,
            )
        )
        log.info(
            "agent registered: id=%s hostname=%s user=%d caps=%s",
            agent_id, payload.hostname, user.id, payload.capabilities,
        )

    await db.commit()

    return AgentRegisterOut(
        agent_id=agent_id,
        agent_token=plaintext,
        poll_interval_s=_DEFAULT_POLL_INTERVAL_S,
        server_time_utc=now,
    )


@router.get(
    "/{agent_id}/jobs",
    response_model=AgentJobsListOut,
)
async def poll_jobs(
    agent_id: str,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AgentJobsListOut:
    """Return pending commands for *agent_id*.

    Bumps ``last_seen_at`` so the dashboard can detect a silent agent.
    Pending rows are NOT atomically transitioned to ``in_flight`` here
    because we want re-polls (e.g. agent restart) to re-deliver the
    same row until it's explicitly acked. Idempotency on the agent
    side is enforced by ``cmd_id`` — the agent tracks "I already
    spawned this one" in its local state.
    """
    agent = await _require_agent_match(agent_id, authorization, db)
    now = _utcnow_iso()
    agent.last_seen_at = now

    rows = (
        await db.execute(
            select(AgentCommand)
            .where(AgentCommand.host_id == agent.id)
            .where(AgentCommand.status == "pending")
            .order_by(AgentCommand.created_at.asc())
        )
    ).scalars().all()

    out: list[AgentJobOut] = []
    for r in rows:
        try:
            payload = json.loads(r.payload_json) if r.payload_json else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        out.append(
            AgentJobOut(
                id=r.id,
                kind=r.kind,
                batch_id=r.batch_id,
                payload=payload,
                created_at=r.created_at,
            )
        )
    await db.commit()
    return AgentJobsListOut(jobs=out)


@router.post(
    "/{agent_id}/jobs/{cmd_id}/ack",
    response_model=AgentAckOut,
)
async def ack_job(
    agent_id: str,
    cmd_id: str,
    payload: AgentAckIn,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AgentAckOut:
    """Acknowledge that the agent acted on a command.

    On ``status='started'``:
        * cmd row → ``status='started'``, records pid
        * for ``kind='rerun'``: child Batch flips ``requested → running``
          so the frontend's "did the agent pick it up" poll succeeds
    On ``status='failed'``:
        * cmd row → ``status='failed'``, records error
        * Batch.status stays ``requested`` so the user can re-try

    Called once per command. A second ack for the same cmd_id is a 409.
    """
    agent = await _require_agent_match(agent_id, authorization, db)

    cmd = await db.get(AgentCommand, cmd_id)
    if cmd is None or cmd.host_id != agent.id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.status not in ("pending",):
        raise HTTPException(
            status_code=409,
            detail=f"cmd already in status {cmd.status!r}",
        )
    if payload.status not in ("started", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"invalid ack status {payload.status!r}",
        )

    now = _utcnow_iso()
    cmd.status = payload.status
    cmd.ack_at = now
    cmd.pid = payload.pid
    cmd.error = payload.error
    agent.last_seen_at = now

    if payload.status == "started":
        target = await db.get(Batch, cmd.batch_id)
        if target is not None and not target.is_deleted:
            if cmd.kind == "rerun" and target.status == "requested":
                target.status = "running"
            # For ``stop`` commands the batch stays in ``stopping`` —
            # the reporter will eventually flip it to ``cancelled`` /
            # ``stopped`` when the subprocess exits.

    await db.commit()
    return AgentAckOut(cmd_id=cmd_id, status=cmd.status)


@router.post("/{agent_id}/heartbeat", status_code=204)
async def heartbeat(
    agent_id: str,
    payload: HeartbeatIn | None = None,  # noqa: ARG001 - reserved for future use
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Liveness ping. Bumps ``last_seen_at`` and returns 204."""
    agent = await _require_agent_match(agent_id, authorization, db)
    agent.last_seen_at = _utcnow_iso()
    await db.commit()
