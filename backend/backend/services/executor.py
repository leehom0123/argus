"""Executor service — unified rerun / stop primitives (#103 v0.1.5 slice).

The Executor owns the lifecycle transitions previously scattered across
:mod:`backend.api.batches`. API routes shrink to thin wrappers; this
service is the single seam where:

* the source-batch state machine is enforced,
* idempotency + dedupe live (re-clicking Rerun within 60 s collapses to
  one ``agent_command`` row instead of N),
* the typed lifecycle Event row is written,
* an :class:`backend.models.AgentCommand` is enqueued for the host
  agent to pick up.

The companion API surface lives in :mod:`backend.api.agents`. The host
agent (``argus-agent`` in :mod:`sibyl.executor.agent`) polls
``GET /api/agents/{id}/jobs`` for pending commands targeting its host
and acks via ``POST /api/agents/{id}/jobs/{cmd_id}/ack``.

Pause / resume are explicitly **out of scope** for v0.1.5 — they land
as additional :meth:`Executor.request_pause` / ``request_resume``
methods in the v0.1.5 follow-up. Auto-retry policy (#104) is v0.1.6.
"""
from __future__ import annotations

import json
import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AgentCommand, AgentHost, Batch, Event

log = logging.getLogger(__name__)


# Source batches must be in one of these terminal states before we'll
# allow a rerun. Mirrors the design doc Section 5 idempotency table —
# ``running`` / ``stopping`` are explicitly excluded so a user can't
# rerun a batch that's still in flight (they should stop it first).
_RERUN_ALLOWED_SOURCE_STATES: frozenset[str] = frozenset(
    {"done", "failed", "cancelled", "stopped"}
)

# Stop is a cooperative signal — only meaningful on running batches.
# Already-terminal states 200-OK no-op so re-clicks don't 4xx.
_STOP_LIVE_STATES: frozenset[str] = frozenset(
    {"running", "stalled", "requested", "pending"}
)
_STOP_NOOP_STATES: frozenset[str] = frozenset(
    {"stopping", "cancelled", "stopped", "done", "failed"}
)


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(ts: str) -> datetime:
    """Parse the ISO 8601 strings we write everywhere (``...Z`` form)."""
    cleaned = ts.rstrip("Z")
    if cleaned.endswith("+00:00"):
        cleaned = cleaned[:-6]
    return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RerunResult:
    """Outcome of :meth:`Executor.request_rerun`.

    ``deduped`` flips True when an existing pending command on the same
    source batch was reused instead of minting a new row. The API layer
    surfaces this back to the client so the UI can distinguish "your
    click took effect" from "we're still waiting on the previous one".
    """

    new_batch_id: str
    new_batch_name: str | None
    source_batch_id: str
    command: AgentCommand | None
    deduped: bool


@dataclass(frozen=True)
class StopResult:
    """Outcome of :meth:`Executor.request_stop`.

    ``noop`` is True when the batch was already in a terminal / stopping
    state — the API layer returns 200 with the existing status untouched.
    """

    batch_id: str
    status: str
    noop: bool
    command: AgentCommand | None


class ExecutorError(Exception):
    """Base class for Executor-raised failures.

    The ``status_code`` field lets API wrappers map the error to an HTTP
    response without re-introspecting the exception type.
    """

    status_code: int = 500

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvalidSourceState(ExecutorError):
    """Raised when the source batch is in a state that forbids the action.

    e.g. trying to rerun a still-running batch. Maps to HTTP 409 so the
    frontend can show a "wait for it to finish first" toast instead of
    a generic 500.
    """

    status_code = 409


class BatchNotFound(ExecutorError):
    """Source batch missing or soft-deleted."""

    status_code = 404


class Executor:
    """Lifecycle service for batch start / stop / rerun primitives.

    Stateless — every method takes an :class:`AsyncSession` so the
    surrounding request transaction stays in control of commit / rollback.
    The service ``add()``s ORM rows but does NOT call ``commit()``;
    the API wrapper is responsible for the final flush. This keeps
    the audit log + rerun event + agent command write atomic with
    the response generation.
    """

    def __init__(self) -> None:
        # Reserved for future config (token TTL, dedupe window
        # overrides). No state today.
        pass

    # ------------------------------------------------------------------
    # Rerun
    # ------------------------------------------------------------------

    async def request_rerun(
        self,
        session: AsyncSession,
        source_batch_id: str,
        user_id: int,
        username: str | None,
        overrides: dict[str, Any] | None = None,
        custom_name: str | None = None,
    ) -> RerunResult:
        """Mint a new Batch row cloned from *source_batch_id*.

        Idempotent on pending rerun commands: if there's already a
        pending ``rerun`` ``AgentCommand`` for the same source batch
        (any age — until an agent acks or :meth:`request_stop` cancels
        it), return *that* new batch id rather than minting a second
        one. The deduplication is keyed on the source batch (not the
        rerun's new id) so rapid double-clicks AND late retries on a
        stuck command both collapse cleanly. Once the agent acks
        (status flips out of ``'pending'``) a fresh rerun mints a new
        row, which is the intended behaviour for "I want another go
        after the previous one ran."

        The new Batch row is added but not committed — the caller owns
        the txn. Status starts at ``'requested'``; the agent flips it to
        ``'running'`` after ``ack`` arrives (handled in the agents API).

        Raises:
            BatchNotFound: source batch missing / soft-deleted.
            InvalidSourceState: source still running (must stop first).
        """
        source = await session.get(Batch, source_batch_id)
        if source is None or source.is_deleted:
            raise BatchNotFound(f"batch {source_batch_id!r} not found")

        # Strict source-state check. Architect's design doc Section 5
        # idempotency table: rerun allowed on terminal states only.
        # ``cancelled`` accepted because the user explicitly killed the
        # previous run and now wants a fresh attempt.
        if source.status not in _RERUN_ALLOWED_SOURCE_STATES:
            # ``None`` happens for stub batches in tests — accept those
            # too so the existing test_batches_rerun.py keeps passing
            # without rewriting fixtures (the legacy rerun route had no
            # state guard at all).
            if source.status is not None:
                raise InvalidSourceState(
                    f"cannot rerun batch in status {source.status!r}; "
                    f"expected one of {sorted(_RERUN_ALLOWED_SOURCE_STATES)}"
                )

        overrides = overrides or {}

        # Idempotency: look for a recent pending rerun command on this
        # source. The match key is ``payload.source_batch_id`` because
        # an AgentCommand's ``batch_id`` is the new (child) batch — we
        # need to dedupe at the parent level.
        deduped = await self._find_recent_rerun(session, source_batch_id)
        if deduped is not None:
            log.info(
                "rerun deduped: source=%s reusing new_batch=%s cmd=%s",
                source_batch_id,
                deduped.batch_id,
                deduped.id,
            )
            # Look up the existing child batch so the response is shaped
            # identically to a fresh rerun.
            child = await session.get(Batch, deduped.batch_id)
            return RerunResult(
                new_batch_id=deduped.batch_id,
                new_batch_name=child.name if child is not None else None,
                source_batch_id=source_batch_id,
                command=deduped,
                deduped=True,
            )

        now = _utcnow_iso()
        new_id = f"rerun-{_uuid_mod.uuid4().hex[:12]}"
        new_name = custom_name or f"{source.name or source.id} (rerun)"

        new_batch = Batch(
            id=new_id,
            project=source.project,
            host=source.host,
            user=source.user,
            owner_id=user_id,
            status="requested",
            n_total=source.n_total,
            n_done=0,
            n_failed=0,
            start_time=now,
            end_time=None,
            command=source.command,
            name=new_name,
            source_batch_id=source_batch_id,
            env_snapshot_json=source.env_snapshot_json,
        )
        session.add(new_batch)

        session.add(
            Event(
                batch_id=new_id,
                job_id=None,
                event_type="rerun_requested",
                timestamp=now,
                schema_version="1.1",
                data=json.dumps(
                    {
                        "source_batch_id": source_batch_id,
                        "overrides": overrides,
                        "requested_by": username,
                        "requested_at": now,
                    },
                    ensure_ascii=False,
                ),
                event_id=str(_uuid_mod.uuid4()),
            )
        )

        cmd = await self._enqueue_rerun_command(
            session,
            new_batch_id=new_id,
            source_batch=source,
            overrides=overrides,
            requested_by=username,
            now=now,
        )

        # TODO(v0.2 polish): emit SSE event for real-time UI update
        # (architect design Section 5; deferred from #103 v0.1.5 slice)

        return RerunResult(
            new_batch_id=new_id,
            new_batch_name=new_name,
            source_batch_id=source_batch_id,
            command=cmd,
            deduped=False,
        )

    async def _find_recent_rerun(
        self, session: AsyncSession, source_batch_id: str
    ) -> AgentCommand | None:
        """Return the most-recent pending rerun command for this source.

        Lookup is keyed on ``(source_batch_id, kind='rerun',
        status='pending')`` with **no time window**. The earlier
        60-second cutoff opened a race: if the agent stayed offline for
        more than a minute, a second click would mint a duplicate
        command and the user would end up with two identical reruns
        the moment the agent came back online. Bounding the lookup by
        ``status='pending'`` already gives the correct lifecycle: as
        soon as the agent acks the command (status flips to
        ``started`` / ``failed`` / ...) or :meth:`request_stop`
        cancels it, the next rerun mints a fresh row.

        Backed by :data:`backend.models.idx_agent_command_batch_kind_status`,
        so the query stays index-scan even with thousands of historical
        rows.
        """
        rows = (
            await session.execute(
                select(AgentCommand)
                .where(AgentCommand.kind == "rerun")
                .where(AgentCommand.status == "pending")
                .order_by(AgentCommand.created_at.desc())
            )
        ).scalars().all()
        for cmd in rows:
            if not cmd.payload_json:
                continue
            try:
                payload = json.loads(cmd.payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("source_batch_id") == source_batch_id:
                return cmd
        return None

    async def _enqueue_rerun_command(
        self,
        session: AsyncSession,
        *,
        new_batch_id: str,
        source_batch: Batch,
        overrides: dict[str, Any],
        requested_by: str | None,
        now: str,
    ) -> AgentCommand | None:
        """Stage an :class:`AgentCommand` row for the host agent.

        Returns ``None`` when no agent is registered for the source
        host yet. The new batch row is still created — the operator
        sees ``status='requested'`` and can either start an agent or
        run the command manually (the documented escape hatch in the
        design doc Section 2).
        """
        host = await self._resolve_host(session, source_batch.host)
        if host is None:
            log.info(
                "rerun: no agent for host=%r; new_batch=%s sits in 'requested' "
                "until an agent registers or operator runs the command manually",
                source_batch.host,
                new_batch_id,
            )
            return None

        # ``cwd`` and ``env`` come out of the source batch's
        # env_snapshot. Stale paths surface as a Popen failure on the
        # agent side, which acks ``status='failed'`` — the design doc
        # explicitly opts NOT to repair stale env (Section 8).
        env_snapshot: dict[str, Any] = {}
        if source_batch.env_snapshot_json:
            try:
                env_snapshot = json.loads(source_batch.env_snapshot_json)
            except (TypeError, json.JSONDecodeError):
                env_snapshot = {}

        payload = {
            "source_batch_id": source_batch.id,
            "command": source_batch.command,
            "cwd": env_snapshot.get("cwd"),
            "env": env_snapshot.get("env"),
            "host": source_batch.host,
            "overrides": overrides,
            "requested_by": requested_by,
        }

        cmd = AgentCommand(
            id=str(_uuid_mod.uuid4()),
            host_id=host.id,
            batch_id=new_batch_id,
            kind="rerun",
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            created_at=now,
        )
        session.add(cmd)
        return cmd

    async def _resolve_host(
        self, session: AsyncSession, hostname: str | None
    ) -> AgentHost | None:
        """Find the most-recently-seen agent for *hostname*."""
        if not hostname:
            return None
        rows = (
            await session.execute(
                select(AgentHost)
                .where(AgentHost.hostname == hostname)
                .order_by(AgentHost.last_seen_at.desc().nullslast())
            )
        ).scalars().all()
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def request_stop(
        self,
        session: AsyncSession,
        batch_id: str,
        user_id: int,
        username: str | None,
    ) -> StopResult:
        """Cooperative stop signal — flips status + writes Event row.

        Idempotent on already-stopping / terminal states (returns
        ``noop=True``). Mirrors the pre-Executor behaviour in
        :func:`backend.api.batches.stop_batch` so existing reporters
        and tests don't need a re-validate sweep.
        """
        batch = await session.get(Batch, batch_id)
        if batch is None or batch.is_deleted:
            raise BatchNotFound(f"batch {batch_id!r} not found")

        # Already stopping / terminal → noop. Caller still gets 200.
        if batch.status in _STOP_NOOP_STATES:
            return StopResult(
                batch_id=batch_id,
                status=batch.status or "stopping",
                noop=True,
                command=None,
            )

        # If the batch has never seen a status transition (None), allow
        # the stop through — same forgiving behaviour as the legacy
        # endpoint.
        if (
            batch.status is not None
            and batch.status not in _STOP_LIVE_STATES
        ):
            raise InvalidSourceState(
                f"cannot stop batch in status {batch.status!r}"
            )

        batch.status = "stopping"
        now = _utcnow_iso()

        session.add(
            Event(
                batch_id=batch_id,
                job_id=None,
                event_type="batch_stop_requested",
                timestamp=now,
                schema_version="1.1",
                data=json.dumps(
                    {"requested_by": username, "requested_at": now}
                ),
                event_id=str(_uuid_mod.uuid4()),
            )
        )

        # Cancel any in-flight rerun command targeting this batch. If
        # the user stopped a rerun before the agent picked it up the
        # agent must NOT spawn it after the fact — otherwise stop +
        # poll would race and we'd see a "stopped" batch transition to
        # "running" minutes later. ``status='pending'`` filter is
        # critical: commands already ``started`` are owned by the
        # agent and get torn down via the separate ``kind='stop'``
        # command queued below.
        await session.execute(
            update(AgentCommand)
            .where(AgentCommand.batch_id == batch_id)
            .where(AgentCommand.kind == "rerun")
            .where(AgentCommand.status == "pending")
            .values(status="cancelled", ack_at=now)
        )

        # Best-effort agent enqueue — same fall-through as rerun.
        host = await self._resolve_host(session, batch.host)
        cmd: AgentCommand | None = None
        if host is not None:
            cmd = AgentCommand(
                id=str(_uuid_mod.uuid4()),
                host_id=host.id,
                batch_id=batch_id,
                kind="stop",
                payload_json=json.dumps(
                    {"requested_by": username}, ensure_ascii=False
                ),
                status="pending",
                created_at=now,
            )
            session.add(cmd)

        # TODO(v0.2 polish): emit SSE event for real-time UI update
        # (architect design Section 5; deferred from #103 v0.1.5 slice)

        return StopResult(
            batch_id=batch_id,
            status="stopping",
            noop=False,
            command=cmd,
        )


# Single process-wide instance — keeps the call sites short. The
# Executor itself is stateless so there's no benefit to per-request
# instantiation.
_executor: Executor | None = None


def get_executor() -> Executor:
    """Return the process-wide :class:`Executor` singleton."""
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor
