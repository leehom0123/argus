"""Aggregation service for the Dashboard + Project views.

Centralises the SQL+Python work that powers the §16 information
architecture:

* ``GET /api/dashboard``                      — home page (counters,
  project grid, activity feed, host status, notifications)
* ``GET /api/projects``                       — project list
* ``GET /api/projects/{project}``             — project header
* ``GET /api/projects/{project}/active-batches``
* ``GET /api/projects/{project}/leaderboard``
* ``GET /api/projects/{project}/matrix``
* ``GET /api/projects/{project}/resources``

Every method applies visibility (``VisibilityResolver``) so a user only
ever sees batches they own or have been granted access to. Admins with
``scope='all'`` fall through to the no-filter path for triage views.

The methods deliberately do their own SQL instead of reusing the
``/api/batches`` router helpers because the aggregation paths need
GROUP BY / aggregate functions, not the per-row ORM shape.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Batch,
    BatchShare,
    Event,
    HostMeta,
    Job,
    ProjectMeta,
    ProjectShare,
    ResourceSnapshot,
    User,
    UserStar,
)
from backend.services.eta import ema_eta
from backend.services.feature_flags import get_flag
from backend.services.health import batch_health
from backend.services.visibility import VisibilityResolver

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_running(status: str | None) -> bool:
    """Treat any of running / pending / in_progress as "live"."""
    if not status:
        return False
    s = status.lower()
    return s in {"running", "in_progress", "pending"}


# Metrics where larger numbers are better. Keep this list short and
# explicit; defaulting to "lower is better" matches the convention used
# by every loss-style metric (MSE / MAE / RMSE / Huber / NLL).
_HIGHER_IS_BETTER = {
    "R2", "R²", "PCC", "SCC", "ACC", "ACCURACY", "F1", "AUC",
}


def _pick_top_models(
    candidates: list[dict[str, Any]],
    *,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Return the top-``k`` (model, dataset) pairs by best metric.

    Picks the metric with the most candidate rows (ties: prefer
    ``"MSE"``), sorts using direction inferred from the metric name,
    then de-duplicates by ``(model, dataset)`` so multiple seeds collapse
    to a single winner row.
    """
    if not candidates:
        return []
    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_metric[c["metric_name"]].append(c)
    if "MSE" in by_metric:
        chosen = "MSE"
    else:
        chosen = max(
            by_metric.keys(),
            key=lambda key: (len(by_metric[key]), -ord(key[0])),
        )
    pool = by_metric[chosen]
    higher_is_better = chosen.upper() in _HIGHER_IS_BETTER
    pool_sorted = sorted(
        pool,
        key=lambda c: c["metric_value"],
        reverse=higher_is_better,
    )
    seen: set[tuple[Any, Any]] = set()
    top: list[dict[str, Any]] = []
    for c in pool_sorted:
        key = (c["model"], c["dataset"])
        if key in seen:
            continue
        seen.add(key)
        top.append(c)
        if len(top) >= k:
            break
    return top


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DashboardService:
    """Stateless aggregator — constructs fresh queries for each call."""

    def __init__(self) -> None:
        self._resolver = VisibilityResolver()

    # ------------------------------------------------------------------
    # Visibility gate
    # ------------------------------------------------------------------

    async def _visible_batch_ids(
        self,
        user: User,
        db: AsyncSession,
        scope: str = "all",
    ) -> list[str]:
        """Return the list of batch ids a user can see.

        For admins under ``scope='all'`` we skip the visibility join
        entirely (the resolver already returns no-filter in that case).
        Callers still pass this list into ``IN (...)`` clauses so the
        downstream queries are identical between admin + non-admin.
        """
        stmt = await self._resolver.visible_batches_query(
            user, scope, db=db
        )
        stmt = stmt.with_only_columns(Batch.id)
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows)

    async def _can_view_project(
        self, user: User, project: str, db: AsyncSession
    ) -> bool:
        """True iff ``user`` can see any batch in ``project``.

        Used at the top of project-scoped endpoints to return 404 early
        when the project name doesn't exist in the user's visible set.
        Admins always pass (parallels :meth:`VisibilityResolver`) — but
        demo projects are invisible even to admins, matching the
        "logged-in users never see demo" rule established 2026-04-24.
        Admins that need to inspect a demo project directly do so via
        the ``/api/public/projects/<name>`` anonymous surface (no auth
        header).
        """
        # Demo short-circuit: demo projects are unreachable from the
        # authenticated surface entirely. Checked before the admin
        # branch because admin status must not bypass the demo filter.
        meta = await db.get(ProjectMeta, project)
        if meta is not None and meta.is_demo:
            return False
        # Soft-deleted projects (migration 021) are also hidden from
        # every surface. Admins re-create / undelete via the meta row
        # rather than navigating through a stale URL.
        if meta is not None and getattr(meta, "is_deleted", False):
            return False

        if user.is_admin:
            # Admin: project exists iff at least one batch has that name.
            exists = await db.execute(
                select(Batch.id)
                .where(Batch.project == project)
                .where(Batch.is_deleted.is_(False))
                .limit(1)
            )
            return exists.scalar_one_or_none() is not None

        stmt = await self._resolver.visible_batches_query(
            user, "all", db=db
        )
        stmt = stmt.where(Batch.project == project).limit(1)
        row = (await db.execute(stmt)).scalars().first()
        return row is not None

    # ------------------------------------------------------------------
    # Anonymous (public-demo) helpers
    # ------------------------------------------------------------------

    async def _anonymous_project_batch_ids(
        self, project: str, db: AsyncSession
    ) -> list[str]:
        """Return every non-deleted batch id under ``project``.

        Used by the public-demo endpoints: when a project is flagged
        ``is_public=True`` by an admin, anonymous visitors get the same
        aggregated stats that an admin would see — owner-visibility is
        intentionally not applied (the whole point of a public demo is
        to show every run).
        """
        rows = (
            await db.execute(
                select(Batch.id)
                .where(Batch.project == project)
                .where(Batch.is_deleted.is_(False))
            )
        ).scalars().all()
        return list(rows)

    async def _project_is_public(
        self, project: str, db: AsyncSession
    ) -> ProjectMeta | None:
        """Return the :class:`ProjectMeta` row iff the project is published.

        Returns ``None`` when the project has no meta row, when
        ``is_public=False``, or when the project has zero non-deleted
        batches. Callers map the last two cases to a 404 so the fact
        that a private project exists never leaks to anon visitors.
        """
        meta = await db.get(ProjectMeta, project)
        if meta is None or not meta.is_public:
            return None
        # Confirm the project actually has at least one batch so that an
        # orphaned meta row (from a renamed project, say) doesn't render
        # a broken /demo card.
        any_row = (
            await db.execute(
                select(Batch.id)
                .where(Batch.project == project)
                .where(Batch.is_deleted.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        if any_row is None:
            return None
        return meta

    # ------------------------------------------------------------------
    # Dashboard home
    # ------------------------------------------------------------------

    async def home(
        self,
        user: User,
        db: AsyncSession,
        scope: str = "all",
    ) -> dict[str, Any]:
        """Compose the ``GET /api/dashboard`` payload.

        The scope parameter controls *which* batches show up in the
        counters / project grid / activity feed. ``scope='all'`` is the
        default (my + shared). Admins can pass ``scope='all'`` to see
        everything because the resolver degrades to no-filter for them.
        """
        now = _utcnow()
        stalled_threshold = int(
            await get_flag(db, "stalled_threshold_sec", default=300)
        )

        visible_ids = await self._visible_batch_ids(user, db, scope)

        counters = await self._counters(
            user, db, visible_ids, now, stalled_threshold
        )
        projects = await self._project_cards(user, db, visible_ids, now)
        activity = await self._activity_feed(db, visible_ids, limit=20)
        hosts = await self._host_cards(db, visible_ids, now)
        notifications = await self._notifications(user, db)

        return {
            "scope": scope,
            "counters": counters,
            "projects": projects,
            "activity": activity,
            "hosts": hosts,
            "notifications": notifications,
            "generated_at": _iso(now),
        }

    async def _counters(
        self,
        user: User,
        db: AsyncSession,
        visible_ids: list[str],
        now: datetime,
        stalled_threshold_s: int,
    ) -> dict[str, Any]:
        if not visible_ids:
            # Zero-state: still need active_hosts / avg_gpu_util which
            # are host-scoped and not visibility-filtered.
            active_hosts, avg_util = await self._host_summary(db, now)
            return {
                "running_batches": 0,
                "jobs_running": 0,
                "jobs_done_24h": 0,
                "jobs_failed_24h": 0,
                "active_hosts": active_hosts,
                "avg_gpu_util": avg_util,
                "my_running": 0,
            }

        cutoff_24h = _iso(now - timedelta(hours=24))

        running_batches = (
            await db.execute(
                select(func.count(Batch.id))
                .where(Batch.id.in_(visible_ids))
                .where(func.lower(Batch.status) == "running")
            )
        ).scalar_one()

        jobs_running = (
            await db.execute(
                select(func.count(Job.id))
                .where(Job.batch_id.in_(visible_ids))
                .where(func.lower(Job.status) == "running")
            )
        ).scalar_one()

        jobs_done_24h = (
            await db.execute(
                select(func.count(Job.id))
                .where(Job.batch_id.in_(visible_ids))
                .where(func.lower(Job.status) == "done")
                .where(
                    or_(
                        Job.end_time.is_(None),
                        Job.end_time >= cutoff_24h,
                    )
                )
            )
        ).scalar_one()

        jobs_failed_24h = (
            await db.execute(
                select(func.count(Job.id))
                .where(Job.batch_id.in_(visible_ids))
                .where(func.lower(Job.status) == "failed")
                .where(
                    or_(
                        Job.end_time.is_(None),
                        Job.end_time >= cutoff_24h,
                    )
                )
            )
        ).scalar_one()

        my_running = (
            await db.execute(
                select(func.count(Batch.id))
                .where(Batch.owner_id == user.id)
                .where(Batch.is_deleted.is_(False))
                .where(func.lower(Batch.status) == "running")
            )
        ).scalar_one()

        active_hosts, avg_util = await self._host_summary(db, now)

        return {
            "running_batches": int(running_batches or 0),
            "jobs_running": int(jobs_running or 0),
            "jobs_done_24h": int(jobs_done_24h or 0),
            "jobs_failed_24h": int(jobs_failed_24h or 0),
            "active_hosts": active_hosts,
            "avg_gpu_util": avg_util,
            "my_running": int(my_running or 0),
        }

    async def _host_summary(
        self, db: AsyncSession, now: datetime
    ) -> tuple[int, float | None]:
        """Return (active_host_count, avg_gpu_util_pct_across_latest_snaps).

        ``active`` = snapshot in the last 5 minutes (§16.2). Demo-fixture
        hosts (hosts whose batches are all flagged ``is_demo=True``) are
        stripped so a freshly-seeded demo doesn't pollute the active-host
        count for authenticated users.

        Perf note (Team Perf): previously fetched each host's latest
        snapshot in a loop (N+1 on /api/dashboard). The loop is now a
        single ``WHERE host IN (...) AND timestamp IN (...)`` query
        plus Python-side dict lookup.
        """
        cutoff = _iso(now - timedelta(minutes=5))
        rows = (
            await db.execute(
                select(
                    ResourceSnapshot.host,
                    func.max(ResourceSnapshot.timestamp).label("ts"),
                )
                .where(ResourceSnapshot.timestamp >= cutoff)
                .group_by(ResourceSnapshot.host)
            )
        ).all()
        if not rows:
            return 0, None

        demo_hosts = await self._demo_host_names(db)
        rows = [(host, ts) for host, ts in rows if host not in demo_hosts]
        if not rows:
            return 0, None

        latest_map = await self._latest_snapshots_by_pair(db, rows)
        utils: list[float] = []
        for host, ts in rows:
            latest = latest_map.get((host, ts))
            if latest and latest.gpu_util_pct is not None:
                utils.append(float(latest.gpu_util_pct))

        avg = round(sum(utils) / len(utils), 2) if utils else None
        return len(rows), avg

    async def _latest_snapshots_by_pair(
        self,
        db: AsyncSession,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], "ResourceSnapshot"]:
        """Batch-fetch ResourceSnapshot rows for a set of (host, ts) pairs.

        Takes the output of the "latest timestamp per host" aggregate
        and resolves each pair to a full row in one IN query. Returns
        an empty dict when ``pairs`` is empty.

        Note: candidates may include extra rows when two hosts share the
        same timestamp — the post-fetch dict lookup is keyed on the
        full pair so the disambiguation happens on the Python side.
        """
        if not pairs:
            return {}
        hosts = [p[0] for p in pairs]
        timestamps = [p[1] for p in pairs]
        candidates = (
            await db.execute(
                select(ResourceSnapshot)
                .where(ResourceSnapshot.host.in_(hosts))
                .where(ResourceSnapshot.timestamp.in_(timestamps))
            )
        ).scalars().all()
        return {(c.host, c.timestamp): c for c in candidates}

    # ------------------------------------------------------------------
    # Project cards (dashboard grid)
    # ------------------------------------------------------------------

    async def _project_cards(
        self,
        user: User,
        db: AsyncSession,
        visible_ids: list[str],
        now: datetime,
    ) -> list[dict[str, Any]]:
        # Demo projects are scrubbed from every authenticated view
        # (2026-04-24 flip). ``visible_ids`` already excludes demo
        # batches at the :class:`VisibilityResolver` layer, so we do
        # not need to union-in demo batches here any more. We still
        # compute ``demo_names`` so the ``is_demo`` flag on the
        # returned card is authoritative; in practice every entry will
        # now have ``is_demo=False``.
        demo_names = await self._demo_project_names(db)
        deleted_names = await self._deleted_project_names(db)

        if not visible_ids:
            return []

        # Group visible batches by project. We pull a compact row per
        # batch and aggregate in Python because SQLite's subquery support
        # is mediocre for the "latest event per batch" case.
        batches = (
            await db.execute(
                select(
                    Batch.id,
                    Batch.project,
                    Batch.status,
                    Batch.n_total,
                    Batch.n_done,
                    Batch.n_failed,
                    Batch.start_time,
                )
                .where(Batch.id.in_(visible_ids))
            )
        ).all()

        by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in batches:
            by_project[row.project].append({
                "id": row.id,
                "status": row.status,
                "n_total": row.n_total,
                "n_done": row.n_done or 0,
                "n_failed": row.n_failed or 0,
                "start_time": row.start_time,
            })

        if not by_project:
            return []

        # Pull *all* visible done jobs once — used for top_models scoring,
        # gpu_hours per project, and 7-day batch volume sparkline data.
        # One query is cheaper than per-project subqueries.
        all_jobs = (
            await db.execute(
                select(
                    Job.batch_id,
                    Job.model,
                    Job.dataset,
                    Job.status,
                    Job.elapsed_s,
                    Job.metrics,
                )
                .where(Job.batch_id.in_(visible_ids))
            )
        ).all()

        # batch_id → project lookup for cross-referencing.
        batch_to_project: dict[str, str] = {
            row.id: row.project for row in batches
        }

        # gpu_hours, top_models per project — both derived from jobs.
        gpu_seconds_by_project: dict[str, int] = defaultdict(int)
        # candidates: project → list[(metric_name, metric_value, model, dataset)]
        metric_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for j in all_jobs:
            project = batch_to_project.get(j.batch_id)
            if project is None:
                continue
            if j.elapsed_s:
                gpu_seconds_by_project[project] += int(j.elapsed_s)
            if (j.status or "").lower() != "done":
                continue
            metrics = _safe_json(j.metrics)
            if not metrics:
                continue
            for mname, mval in metrics.items():
                if isinstance(mval, (int, float)):
                    metric_candidates[project].append({
                        "model": j.model,
                        "dataset": j.dataset,
                        "metric_name": mname,
                        "metric_value": float(mval),
                    })

        # 7-day batch-volume sparkline: count batches per UTC day for the
        # last 7 days (oldest → newest, len=7). start_time is an ISO-8601
        # string; we slice the YYYY-MM-DD prefix for a cheap day key.
        today = now.date()
        day_keys = [
            (today - timedelta(days=i)).isoformat()
            for i in range(6, -1, -1)
        ]
        volume_by_project: dict[str, list[int]] = {
            p: [0] * 7 for p in by_project
        }
        for project, items in by_project.items():
            counts = volume_by_project[project]
            for b in items:
                ts = b.get("start_time")
                if not ts or not isinstance(ts, str) or len(ts) < 10:
                    continue
                day = ts[:10]
                if day in day_keys:
                    counts[day_keys.index(day)] += 1

        # Jobs last_event_at per project: max(event.timestamp) over
        # batches in the project. One query for everyone is cheaper
        # than one-per-project.
        evt_rows = (
            await db.execute(
                select(Event.batch_id, func.max(Event.timestamp))
                .where(Event.batch_id.in_(visible_ids))
                .group_by(Event.batch_id)
            )
        ).all()
        last_event_by_batch: dict[str, str | None] = {
            bid: ts for bid, ts in evt_rows
        }

        # Starred project set for this user.
        starred = await self._user_stars(user, db, "project")

        # ETA sample lookup: hoist the per-project Job query out of the
        # loop. Previously ``_project_eta`` SELECT'd from Job once per
        # project (K projects → K queries). We now collect every running
        # batch id across all projects, run ONE IN-query for done jobs'
        # elapsed_s, and partition the rows in Python before calling
        # ``ema_eta`` per project.
        all_running_batch_ids: list[str] = []
        for items in by_project.values():
            for b in items:
                if _is_running(b["status"]):
                    all_running_batch_ids.append(b["id"])

        elapsed_by_batch: dict[str, list[int]] = defaultdict(list)
        if all_running_batch_ids:
            elapsed_rows = (
                await db.execute(
                    select(Job.batch_id, Job.elapsed_s, Job.end_time)
                    .where(Job.batch_id.in_(all_running_batch_ids))
                    .where(func.lower(Job.status) == "done")
                    .where(Job.elapsed_s.is_not(None))
                    .order_by(Job.end_time.desc().nullslast())
                )
            ).all()
            # Partition by batch_id, preserving the ORDER BY end_time DESC
            # ordering so the per-project sample is "most recent first".
            for bid, elapsed, _end in elapsed_rows:
                if elapsed is not None:
                    elapsed_by_batch[bid].append(int(elapsed))

        cards: list[dict[str, Any]] = []
        for project, items in by_project.items():
            is_demo = project in demo_names
            # Defence-in-depth: ``visible_ids`` already excludes demo
            # batches via the visibility resolver, so this branch
            # should be unreachable in practice. We keep it as a
            # belt-and-braces guard in case a caller bypasses the
            # resolver and hands in demo batch ids directly.
            if is_demo:
                continue
            # Soft-deleted projects (migration 021) — same skip.
            if project in deleted_names:
                continue
            running = sum(1 for b in items if _is_running(b["status"]))
            done = sum(b["n_done"] for b in items)
            failed = sum(b["n_failed"] for b in items)

            # last event across all batches in the project
            last_event_at: str | None = None
            for b in items:
                ts = last_event_by_batch.get(b["id"]) or b["start_time"]
                if ts and (last_event_at is None or ts > last_event_at):
                    last_event_at = ts

            eta_seconds = self._project_eta_from_samples(
                items, elapsed_by_batch
            )

            # failure_rate = failed / (done + failed). None when no
            # jobs have ended yet so the UI can hide the row gracefully.
            total_ended = done + failed
            failure_rate = (
                round(failed / total_ended, 4) if total_ended else None
            )
            gpu_hours = round(
                gpu_seconds_by_project.get(project, 0) / 3600.0, 3
            )

            # Top-3 models by best metric — see ``_pick_top_models`` for
            # the metric-selection + direction-inference rules.
            top_models = _pick_top_models(
                metric_candidates.get(project, [])
            )

            cards.append({
                "project": project,
                "running_batches": running,
                "jobs_done": done,
                "jobs_failed": failed,
                "failure_rate": failure_rate,
                "gpu_hours": gpu_hours,
                "top_models": top_models,
                "batch_volume_7d": volume_by_project.get(project, [0] * 7),
                "eta_seconds": eta_seconds,
                "last_event_at": last_event_at,
                "is_starred": project in starred,
                "is_demo": is_demo,
            })

        # Sort: starred first, then by "liveness" (running batches desc,
        # last_event_at desc) per §16.3.
        cards.sort(
            key=lambda c: (
                0 if c["is_starred"] else 1,
                -c["running_batches"],
                -(1 if c["last_event_at"] else 0),
                -(len(c["last_event_at"]) if c["last_event_at"] else 0),
            )
        )
        return cards

    async def _project_eta(
        self,
        batch_ids: list[str],
        batches: list[dict[str, Any]],
        db: AsyncSession,
    ) -> int | None:
        """Aggregate ETA across running batches in one project.

        Strategy: sum the per-batch EMA-ETAs. If no running batches,
        return None (rather than 0) so the UI can skip the field.

        Note: ``_project_cards`` no longer calls this helper — it uses
        :meth:`_project_eta_from_samples` to avoid the K-projects × 1
        query N+1. Kept for any out-of-loop callers that still want the
        single-project shape.
        """
        running_batch_ids = [
            b["id"] for b in batches if _is_running(b["status"])
        ]
        if not running_batch_ids:
            return None

        # pending = sum(n_total - n_done - n_failed) over running batches
        pending = 0
        for b in batches:
            if not _is_running(b["status"]):
                continue
            if b["n_total"] is None:
                continue
            remaining = b["n_total"] - b["n_done"] - b["n_failed"]
            pending += max(0, remaining)
        if pending <= 0:
            return None

        # Sample: the most recent ~10 done jobs across running batches.
        rows = (
            await db.execute(
                select(Job.elapsed_s)
                .where(Job.batch_id.in_(running_batch_ids))
                .where(func.lower(Job.status) == "done")
                .where(Job.elapsed_s.is_not(None))
                .order_by(Job.end_time.desc().nullslast())
                .limit(10)
            )
        ).all()
        elapsed = [r[0] for r in rows if r[0] is not None]
        return ema_eta(elapsed, pending)

    def _project_eta_from_samples(
        self,
        batches: list[dict[str, Any]],
        elapsed_by_batch: dict[str, list[int]],
    ) -> int | None:
        """Compute project ETA from a pre-fetched per-batch elapsed map.

        Companion to the IN-query hoist in :meth:`_project_cards`. Reads
        the (already partitioned + already-sorted-by-end_time-desc)
        ``elapsed_by_batch`` map instead of issuing its own SELECT, so
        the dashboard pays one query for the whole grid instead of one
        per project.
        """
        running_batch_ids = [
            b["id"] for b in batches if _is_running(b["status"])
        ]
        if not running_batch_ids:
            return None

        pending = 0
        for b in batches:
            if not _is_running(b["status"]):
                continue
            if b["n_total"] is None:
                continue
            remaining = b["n_total"] - b["n_done"] - b["n_failed"]
            pending += max(0, remaining)
        if pending <= 0:
            return None

        # Pull the most recent ~10 done jobs across this project's
        # running batches. The pre-fetched rows were already ordered by
        # end_time DESC, so concatenating per-batch slices preserves the
        # newest-first invariant ``ema_eta`` expects.
        elapsed: list[int] = []
        for bid in running_batch_ids:
            for v in elapsed_by_batch.get(bid, []):
                elapsed.append(v)
                if len(elapsed) >= 10:
                    break
            if len(elapsed) >= 10:
                break
        return ema_eta(elapsed, pending)

    async def _user_stars(
        self, user: User, db: AsyncSession, target_type: str
    ) -> set[str]:
        rows = (
            await db.execute(
                select(UserStar.target_id)
                .where(UserStar.user_id == user.id)
                .where(UserStar.target_type == target_type)
            )
        ).scalars().all()
        return set(rows)

    # ------------------------------------------------------------------
    # Activity feed + hosts + notifications
    # ------------------------------------------------------------------

    async def _activity_feed(
        self,
        db: AsyncSession,
        visible_ids: list[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not visible_ids:
            return []
        interesting = (
            "batch_start",
            "batch_done",
            "batch_failed",
            "job_failed",
        )
        rows = (
            await db.execute(
                select(
                    Event.event_type,
                    Event.batch_id,
                    Event.job_id,
                    Event.timestamp,
                    Event.data,
                )
                .where(Event.batch_id.in_(visible_ids))
                .where(Event.event_type.in_(interesting))
                .order_by(Event.timestamp.desc(), Event.id.desc())
                .limit(limit)
            )
        ).all()

        # Look up project names in one pass.
        batch_project = dict(
            (
                await db.execute(
                    select(Batch.id, Batch.project)
                    .where(Batch.id.in_([r.batch_id for r in rows]))
                )
            ).all()
        )

        out: list[dict[str, Any]] = []
        for r in rows:
            payload = _safe_json(r.data) or {}
            summary = _summarise_event(r.event_type, payload)
            out.append({
                "event_type": r.event_type,
                "batch_id": r.batch_id,
                "job_id": r.job_id,
                "project": batch_project.get(r.batch_id),
                "timestamp": r.timestamp,
                "summary": summary,
            })
        return out

    async def _host_cards(
        self,
        db: AsyncSession,
        visible_ids: list[str],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Per-host dashboard card (top-right panel).

        Perf note (Team Perf): the previous implementation did
        ``1 latest-snap query + 1 running-jobs query`` per host — i.e.
        ``2*N`` extra round-trips on /api/dashboard. The loop is now
        ``1 IN-query for latest snaps + 1 GROUP BY host for running
        job counts``, reducing the dashboard's host-card cost to a
        constant regardless of cluster size.
        """
        cutoff = _iso(now - timedelta(minutes=5))
        host_rows = (
            await db.execute(
                select(
                    ResourceSnapshot.host,
                    func.max(ResourceSnapshot.timestamp).label("ts"),
                )
                .where(ResourceSnapshot.timestamp >= cutoff)
                .group_by(ResourceSnapshot.host)
            )
        ).all()

        demo_hosts = await self._demo_host_names(db)
        deleted_hosts = await self._deleted_host_names(db)
        active_pairs = [
            (host, ts)
            for host, ts in host_rows
            if host not in demo_hosts and host not in deleted_hosts
        ]
        if not active_pairs:
            return []

        # Batch-resolve every (host, ts) in one query.
        snap_by_pair = await self._latest_snapshots_by_pair(db, active_pairs)

        # Batch-count running jobs grouped by host in one query.
        active_hosts = [h for h, _ in active_pairs]
        running_by_host: dict[str, int] = {h: 0 for h in active_hosts}
        running_q = (
            select(Batch.host, func.count(Job.id))
            .select_from(Job)
            .join(Batch, Batch.id == Job.batch_id)
            .where(Batch.host.in_(active_hosts))
            .where(func.lower(Job.status) == "running")
            .group_by(Batch.host)
        )
        if visible_ids:
            running_q = running_q.where(Job.batch_id.in_(visible_ids))
        for host, n in (await db.execute(running_q)).all():
            if host is not None:
                running_by_host[host] = int(n or 0)

        # v0.1.3 density: per-host top-5 running jobs (model × dataset
        # + owning user + pid). One query (Job ⨝ Batch ⨝ User), then
        # bucket by host in Python and slice each bucket to 5. Cheap
        # — there's only ever a handful of running jobs per cluster.
        top5_by_host: dict[str, list[dict[str, Any]]] = {
            h: [] for h in active_hosts
        }
        if active_hosts:
            top5_q = (
                select(
                    Batch.host,
                    Job.id,
                    Job.model,
                    Job.dataset,
                    Job.start_time,
                    User.username,
                    Job.extra,
                )
                .select_from(Job)
                .join(Batch, Batch.id == Job.batch_id)
                .join(User, User.id == Batch.owner_id, isouter=True)
                .where(Batch.host.in_(active_hosts))
                .where(func.lower(Job.status) == "running")
                .order_by(Job.start_time.asc().nullslast())
            )
            if visible_ids:
                top5_q = top5_q.where(Job.batch_id.in_(visible_ids))
            for row in (await db.execute(top5_q)).all():
                bucket = top5_by_host.get(row.host)
                if bucket is None or len(bucket) >= 5:
                    continue
                # PID lives in the Job.extra JSON blob if at all.
                extra = _safe_json(row.extra) or {}
                pid_val = extra.get("pid")
                pid_int = (
                    int(pid_val)
                    if isinstance(pid_val, (int, float))
                    else None
                )
                bucket.append({
                    "job_id": row.id,
                    "model": row.model,
                    "dataset": row.dataset,
                    "user": row.username,
                    "pid": pid_int,
                })

        cards: list[dict[str, Any]] = []
        for host, ts in active_pairs:
            snap = snap_by_pair.get((host, ts))
            if snap is None:
                continue

            warnings: list[str] = []
            if snap.gpu_temp_c is not None and snap.gpu_temp_c > 85:
                warnings.append(f"GPU {snap.gpu_temp_c:.0f}°C (hot)")
            if snap.disk_free_mb is not None and snap.disk_free_mb < 10_000:
                warnings.append(
                    f"disk only {snap.disk_free_mb/1024:.1f}GB free"
                )

            cards.append({
                "host": host,
                "last_seen": ts,
                "gpu_util_pct": snap.gpu_util_pct,
                "gpu_mem_mb": snap.gpu_mem_mb,
                "gpu_mem_total_mb": snap.gpu_mem_total_mb,
                "gpu_temp_c": snap.gpu_temp_c,
                "cpu_util_pct": snap.cpu_util_pct,
                "ram_mb": snap.ram_mb,
                "ram_total_mb": snap.ram_total_mb,
                "disk_free_mb": snap.disk_free_mb,
                "disk_total_mb": snap.disk_total_mb,
                "running_jobs": running_by_host.get(host, 0),
                "running_jobs_top5": top5_by_host.get(host, []),
                "warnings": warnings,
            })
        return cards

    async def _notifications(
        self, user: User, db: AsyncSession
    ) -> list[dict[str, Any]]:
        """Return a small digest of recent failure / share events.

        MVP implementation: last 5 job_failed / batch_failed events in
        the user's visible scope + a token-expiry warning if applicable.
        This hook exists so the frontend doesn't need a separate
        ``/api/notifications`` round-trip.
        """
        visible_ids = await self._visible_batch_ids(user, db, "all")
        out: list[dict[str, Any]] = []
        if visible_ids:
            rows = (
                await db.execute(
                    select(
                        Event.event_type,
                        Event.batch_id,
                        Event.timestamp,
                    )
                    .where(Event.batch_id.in_(visible_ids))
                    .where(
                        Event.event_type.in_(("job_failed", "batch_failed"))
                    )
                    .order_by(Event.timestamp.desc())
                    .limit(5)
                )
            ).all()
            for r in rows:
                out.append({
                    "kind": r.event_type,
                    "message": f"{r.event_type} in batch {r.batch_id}",
                    "timestamp": r.timestamp,
                    "target_type": "batch",
                    "target_id": r.batch_id,
                })
        return out

    # ------------------------------------------------------------------
    # Project list + detail
    # ------------------------------------------------------------------

    async def _demo_project_names(self, db: AsyncSession) -> set[str]:
        """Return the set of projects tagged ``is_demo=True``.

        Cheap single-row lookup in practice (there's exactly one
        demo project in the seeded fixture), but we return a set so
        callers that gain additional demo projects later don't need
        to change shape.
        """
        rows = (
            await db.execute(
                select(ProjectMeta.project).where(
                    ProjectMeta.is_demo.is_(True)
                )
            )
        ).scalars().all()
        return set(rows)

    async def _deleted_project_names(self, db: AsyncSession) -> set[str]:
        """Return the set of projects soft-deleted via migration 021.

        Projects don't have a first-class table — they're only
        ``Batch.project`` strings — so admin deletion flips
        ``ProjectMeta.is_deleted=True`` (and cascades to the batches).
        Read paths drop those projects from listings, the same way
        ``is_demo`` is filtered out.
        """
        rows = (
            await db.execute(
                select(ProjectMeta.project).where(
                    ProjectMeta.is_deleted.is_(True)
                )
            )
        ).scalars().all()
        return set(rows)

    async def _deleted_host_names(self, db: AsyncSession) -> set[str]:
        """Return hosts soft-deleted via :class:`HostMeta` (migration 021)."""
        rows = (
            await db.execute(
                select(HostMeta.host).where(HostMeta.is_deleted.is_(True))
            )
        ).scalars().all()
        return set(rows)

    async def _demo_host_names(self, db: AsyncSession) -> set[str]:
        """Return hosts that appear exclusively under demo projects.

        A host is classified as demo iff every non-deleted batch
        claiming it belongs to a ``ProjectMeta.is_demo=True`` project.
        Hosts that serve even a single real batch stay visible so a
        real server is never accidentally suppressed by a stray demo
        snapshot sharing its name.

        In the seeded fixture this set is ``{"demo-host-a100"}`` —
        that host name is reserved for demo use and never appears on
        real batches.
        """
        demo_projects = await self._demo_project_names(db)
        if not demo_projects:
            return set()
        # Hosts that ever host a demo batch.
        demo_host_rows = (
            await db.execute(
                select(Batch.host)
                .where(Batch.project.in_(demo_projects))
                .where(Batch.is_deleted.is_(False))
                .where(Batch.host.is_not(None))
                .distinct()
            )
        ).scalars().all()
        candidates = {h for h in demo_host_rows if h}
        if not candidates:
            return set()
        # Of those, drop any that also host a non-demo batch.
        non_demo_rows = (
            await db.execute(
                select(Batch.host)
                .where(Batch.host.in_(candidates))
                .where(Batch.project.notin_(demo_projects))
                .where(Batch.is_deleted.is_(False))
                .distinct()
            )
        ).scalars().all()
        overlap = {h for h in non_demo_rows if h}
        return candidates - overlap

    async def list_projects(
        self, user: User, db: AsyncSession
    ) -> list[dict[str, Any]]:
        """``GET /api/projects`` — one row per distinct visible project.

        Demo projects (``ProjectMeta.is_demo=True``) are filtered out
        unconditionally for authenticated callers (2026-04-24 flip).
        Anonymous visitors see them through ``/api/public/projects``
        instead. The legacy :attr:`User.hide_demo` preference is kept
        on the user row for backwards compatibility but is no longer
        consulted here — demo is simply never included.
        """
        visible_ids = await self._visible_batch_ids(user, db, "all")
        demo_names = await self._demo_project_names(db)
        deleted_names = await self._deleted_project_names(db)

        if not visible_ids:
            return []

        # Pull batch rows and aggregate in Python — avoids dialect
        # quirks around casting booleans to integers.
        batches = (
            await db.execute(
                select(
                    Batch.id,
                    Batch.project,
                    Batch.status,
                    Batch.n_done,
                    Batch.n_failed,
                    Batch.start_time,
                )
                .where(Batch.id.in_(visible_ids))
            )
        ).all()

        by_project: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "n_batches": 0,
                "running_batches": 0,
                "jobs_done": 0,
                "jobs_failed": 0,
                "batch_ids": [],
            }
        )
        for b in batches:
            agg = by_project[b.project]
            agg["n_batches"] += 1
            if _is_running(b.status):
                agg["running_batches"] += 1
            agg["jobs_done"] += b.n_done or 0
            agg["jobs_failed"] += b.n_failed or 0
            agg["batch_ids"].append(b.id)

        # last_event per project (single aggregated query)
        evt_rows = (
            await db.execute(
                select(Event.batch_id, func.max(Event.timestamp))
                .where(Event.batch_id.in_(visible_ids))
                .group_by(Event.batch_id)
            )
        ).all()
        last_by_batch = {bid: ts for bid, ts in evt_rows}

        # Density extension (v0.1.3): hoist top_models / failure_rate /
        # gpu_hours / batch_volume_7d into the list summary so the
        # ProjectCard can render the new info rows directly without
        # waiting on /api/projects/{p} for the header detail.
        all_jobs = (
            await db.execute(
                select(
                    Job.batch_id,
                    Job.model,
                    Job.dataset,
                    Job.status,
                    Job.elapsed_s,
                    Job.metrics,
                )
                .where(Job.batch_id.in_(visible_ids))
            )
        ).all()
        batch_to_project = {b.id: b.project for b in batches}
        gpu_seconds_by_project: dict[str, int] = defaultdict(int)
        metric_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for j in all_jobs:
            project = batch_to_project.get(j.batch_id)
            if project is None:
                continue
            if j.elapsed_s:
                gpu_seconds_by_project[project] += int(j.elapsed_s)
            if (j.status or "").lower() != "done":
                continue
            metrics = _safe_json(j.metrics)
            if not metrics:
                continue
            for mname, mval in metrics.items():
                if isinstance(mval, (int, float)):
                    metric_candidates[project].append({
                        "model": j.model,
                        "dataset": j.dataset,
                        "metric_name": mname,
                        "metric_value": float(mval),
                    })

        today = _utcnow().date()
        day_keys = [
            (today - timedelta(days=i)).isoformat()
            for i in range(6, -1, -1)
        ]
        volume_by_project: dict[str, list[int]] = {
            p: [0] * 7 for p in by_project
        }
        for b in batches:
            ts = b.start_time
            if not ts or not isinstance(ts, str) or len(ts) < 10:
                continue
            day = ts[:10]
            if day in day_keys and b.project in volume_by_project:
                volume_by_project[b.project][day_keys.index(day)] += 1

        starred = await self._user_stars(user, db, "project")

        out: list[dict[str, Any]] = []
        for project, agg in by_project.items():
            is_demo = project in demo_names
            # Belt-and-braces: visibility resolver already strips demo
            # batches so demo projects should not appear here; skip
            # defensively in case a caller supplied demo batch ids.
            if is_demo:
                continue
            # Soft-deleted projects (migration 021) — same treatment.
            if project in deleted_names:
                continue
            last_event_at: str | None = None
            for bid in agg["batch_ids"]:
                ts = last_by_batch.get(bid)
                if ts and (last_event_at is None or ts > last_event_at):
                    last_event_at = ts

            done = agg["jobs_done"]
            failed = agg["jobs_failed"]
            total_ended = done + failed
            failure_rate = (
                round(failed / total_ended, 4) if total_ended else None
            )
            gpu_hours = round(
                gpu_seconds_by_project.get(project, 0) / 3600.0, 3
            )
            top_models = _pick_top_models(metric_candidates.get(project, []))

            out.append({
                "project": project,
                "n_batches": agg["n_batches"],
                "running_batches": agg["running_batches"],
                "jobs_done": agg["jobs_done"],
                "jobs_failed": agg["jobs_failed"],
                "failure_rate": failure_rate,
                "gpu_hours": gpu_hours,
                "top_models": top_models,
                "batch_volume_7d": volume_by_project.get(project, [0] * 7),
                "last_event_at": last_event_at,
                "is_starred": project in starred,
                "is_demo": is_demo,
            })
        out.sort(
            key=lambda r: (
                0 if r["is_starred"] else 1,
                -r["running_batches"],
                r["project"],
            )
        )
        return out

    async def project_detail(
        self, user: User, project: str, db: AsyncSession
    ) -> dict[str, Any]:
        """Project header payload — throws 404-ish (None) if invisible.

        Caller (router) maps ``None`` to a 404 HTTP response.
        """
        if not await self._can_view_project(user, project, db):
            return None  # type: ignore[return-value]

        visible_ids = await self._visible_batch_ids(user, db, "all")
        rows = (
            await db.execute(
                select(
                    Batch.id,
                    Batch.status,
                    Batch.n_done,
                    Batch.n_failed,
                    Batch.start_time,
                    Batch.owner_id,
                )
                .where(Batch.id.in_(visible_ids))
                .where(Batch.project == project)
            )
        ).all()

        n_batches = len(rows)
        running = sum(1 for r in rows if _is_running(r.status))
        done = sum(r.n_done or 0 for r in rows)
        failed = sum(r.n_failed or 0 for r in rows)
        # failure_rate = failed / (done + failed)
        total_ended = done + failed
        failure_rate = round(failed / total_ended, 4) if total_ended else None

        # gpu_hours = sum(jobs.elapsed_s) / 3600 across project
        batch_ids_here = [r.id for r in rows]
        elapsed_sum = 0
        best_metric_val: float | None = None
        best_metric_name: str = "MSE"
        first_ts: str | None = None
        last_ts: str | None = None

        if batch_ids_here:
            elapsed_sum = int(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(Job.elapsed_s), 0))
                        .where(Job.batch_id.in_(batch_ids_here))
                    )
                ).scalar_one()
                or 0
            )

            # best MSE across all done jobs (MVP picks the first metric
            # that the recorder emits; frontend can switch via
            # /leaderboard?metric=...).
            metric_rows = (
                await db.execute(
                    select(Job.metrics)
                    .where(Job.batch_id.in_(batch_ids_here))
                    .where(func.lower(Job.status) == "done")
                )
            ).scalars().all()
            for raw in metric_rows:
                m = _safe_json(raw)
                if not m:
                    continue
                value = m.get(best_metric_name)
                if isinstance(value, (int, float)):
                    if best_metric_val is None or value < best_metric_val:
                        best_metric_val = float(value)

            # first / last event ts across the project
            ts_row = (
                await db.execute(
                    select(
                        func.min(Event.timestamp),
                        func.max(Event.timestamp),
                    )
                    .where(Event.batch_id.in_(batch_ids_here))
                )
            ).one()
            first_ts = ts_row[0]
            last_ts = ts_row[1]

        # owners: distinct users (by username) behind the project
        owners: list[str] = []
        if batch_ids_here:
            owner_rows = (
                await db.execute(
                    select(User.username)
                    .select_from(Batch)
                    .join(User, User.id == Batch.owner_id)
                    .where(Batch.id.in_(batch_ids_here))
                    .distinct()
                )
            ).scalars().all()
            owners = sorted(o for o in owner_rows if o)

        # batches_this_week: count batches whose start_time is within the
        # last 7 days.  start_time is stored as an ISO-8601 string so a
        # lexicographic comparison against the cutoff string is correct for
        # UTC-normalised values.
        cutoff_7d = _iso(_utcnow() - timedelta(days=7))
        batches_this_week = sum(
            1 for r in rows
            if r.start_time is not None and r.start_time >= cutoff_7d
        )

        starred = await self._user_stars(user, db, "project")
        return {
            "project": project,
            "n_batches": n_batches,
            "running_batches": running,
            "jobs_done": done,
            "jobs_failed": failed,
            "failure_rate": failure_rate,
            "gpu_hours": round(elapsed_sum / 3600.0, 3),
            "best_metric": (
                {"name": best_metric_name, "value": best_metric_val}
                if best_metric_val is not None
                else None
            ),
            "first_event_at": first_ts,
            "last_event_at": last_ts,
            "batches_this_week": batches_this_week,
            "is_starred": project in starred,
            "owners": owners,
        }

    async def public_project_detail(
        self, project: str, db: AsyncSession
    ) -> dict[str, Any] | None:
        """Header payload for the anonymous /demo/<project> landing page.

        Caller must have already confirmed ``ProjectMeta.is_public=True``
        (via :meth:`_project_is_public`). Returns ``None`` when the
        project has no batches so the router can 404 consistently.
        """
        meta = await self._project_is_public(project, db)
        if meta is None:
            return None

        batch_ids = await self._anonymous_project_batch_ids(project, db)
        if not batch_ids:
            return None

        rows = (
            await db.execute(
                select(
                    Batch.id,
                    Batch.status,
                    Batch.n_done,
                    Batch.n_failed,
                )
                .where(Batch.id.in_(batch_ids))
            )
        ).all()

        n_batches = len(rows)
        running = sum(1 for r in rows if _is_running(r.status))
        done = sum(r.n_done or 0 for r in rows)
        failed = sum(r.n_failed or 0 for r in rows)
        total_ended = done + failed
        failure_rate = (
            round(failed / total_ended, 4) if total_ended else None
        )

        elapsed_sum = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(Job.elapsed_s), 0))
                    .where(Job.batch_id.in_(batch_ids))
                )
            ).scalar_one()
            or 0
        )

        ts_row = (
            await db.execute(
                select(
                    func.min(Event.timestamp),
                    func.max(Event.timestamp),
                )
                .where(Event.batch_id.in_(batch_ids))
            )
        ).one()

        return {
            "project": project,
            "description": meta.public_description,
            "published_at": meta.published_at,
            "n_batches": n_batches,
            "running_batches": running,
            "jobs_done": done,
            "jobs_failed": failed,
            "failure_rate": failure_rate,
            "gpu_hours": round(elapsed_sum / 3600.0, 3),
            "first_event_at": ts_row[0],
            "last_event_at": ts_row[1],
        }

    async def public_project_list(
        self, db: AsyncSession
    ) -> list[dict[str, Any]]:
        """Return every currently-public project summary (for /demo index)."""
        metas = (
            await db.execute(
                select(ProjectMeta)
                .where(ProjectMeta.is_public.is_(True))
                .order_by(
                    ProjectMeta.published_at.desc().nullslast(),
                    ProjectMeta.project.asc(),
                )
            )
        ).scalars().all()
        out: list[dict[str, Any]] = []
        for meta in metas:
            rows = (
                await db.execute(
                    select(func.count(Batch.id))
                    .where(Batch.project == meta.project)
                    .where(Batch.is_deleted.is_(False))
                )
            ).scalar_one()
            n_batches = int(rows or 0)
            if n_batches == 0:
                # Orphaned meta row — don't leak it to anon visitors.
                continue
            out.append({
                "project": meta.project,
                "description": meta.public_description,
                "published_at": meta.published_at,
                "n_batches": n_batches,
            })
        return out

    async def public_project_batches(
        self, project: str, db: AsyncSession
    ) -> list[dict[str, Any]] | None:
        """Return batch-level metadata for the public detail page."""
        meta = await self._project_is_public(project, db)
        if meta is None:
            return None
        rows = (
            await db.execute(
                select(Batch)
                .where(Batch.project == project)
                .where(Batch.is_deleted.is_(False))
                .order_by(Batch.start_time.desc().nullslast(), Batch.id.asc())
            )
        ).scalars().all()
        return [
            {
                "batch_id": b.id,
                "project": b.project,
                "host": b.host,
                "status": b.status,
                "n_total": b.n_total,
                "n_done": b.n_done or 0,
                "n_failed": b.n_failed or 0,
                "start_time": b.start_time,
                "end_time": b.end_time,
            }
            for b in rows
        ]

    # ------------------------------------------------------------------
    # Active batches tab
    # ------------------------------------------------------------------

    async def project_active_batches(
        self,
        user: User | None,
        project: str,
        db: AsyncSession,
        anonymous: bool = False,
    ) -> list[dict[str, Any]] | None:
        if anonymous:
            visible_ids = await self._anonymous_project_batch_ids(project, db)
        else:
            if not await self._can_view_project(user, project, db):
                return None
            visible_ids = await self._visible_batch_ids(user, db, "all")

        stalled_threshold = int(
            await get_flag(db, "stalled_threshold_sec", default=300)
        )
        rows = (
            await db.execute(
                select(Batch)
                .where(Batch.id.in_(visible_ids))
                .where(Batch.project == project)
                .where(
                    or_(
                        func.lower(Batch.status) == "running",
                        Batch.status.is_(None),
                    )
                )
                .order_by(Batch.start_time.desc().nullslast())
            )
        ).scalars().all()

        out: list[dict[str, Any]] = []
        for batch in rows:
            if not _is_running(batch.status) and batch.status is not None:
                continue

            health = await batch_health(
                batch.id, db, stalled_threshold_s=stalled_threshold
            )
            eta = await self._batch_eta(batch, db)

            running_jobs_rows = (
                await db.execute(
                    select(Job)
                    .where(Job.batch_id == batch.id)
                    .where(func.lower(Job.status) == "running")
                    .order_by(Job.start_time.asc().nullslast())
                )
            ).scalars().all()

            running_jobs: list[dict[str, Any]] = []
            for j in running_jobs_rows:
                running_jobs.append({
                    "job_id": j.id,
                    "model": j.model,
                    "dataset": j.dataset,
                    "status": j.status,
                    "start_time": j.start_time,
                    "metrics": _safe_json(j.metrics),
                })

            n_total = batch.n_total
            completion_pct: float | None = None
            if n_total:
                completion_pct = round(
                    ((batch.n_done or 0) + (batch.n_failed or 0))
                    / n_total
                    * 100.0,
                    2,
                )

            out.append({
                "batch_id": batch.id,
                "project": batch.project,
                "owner_id": batch.owner_id,
                "host": batch.host,
                "status": batch.status,
                "n_total": n_total,
                "n_done": batch.n_done or 0,
                "n_failed": batch.n_failed or 0,
                "completion_pct": completion_pct,
                "start_time": batch.start_time,
                "last_event_at": (
                    None
                    if health["last_event_age_s"] is None
                    else _iso(
                        _utcnow() - timedelta(
                            seconds=health["last_event_age_s"]
                        )
                    )
                ),
                "eta_seconds": eta,
                "is_stalled": bool(health["is_stalled"]),
                "running_jobs": running_jobs,
                "warnings": health["warnings"],
            })
        return out

    async def _batch_eta(
        self, batch: Batch, db: AsyncSession
    ) -> int | None:
        if batch.n_total is None:
            return None
        pending = batch.n_total - (batch.n_done or 0) - (batch.n_failed or 0)
        if pending <= 0:
            return 0
        rows = (
            await db.execute(
                select(Job.elapsed_s)
                .where(Job.batch_id == batch.id)
                .where(func.lower(Job.status) == "done")
                .where(Job.elapsed_s.is_not(None))
                .order_by(Job.end_time.desc().nullslast())
                .limit(10)
            )
        ).all()
        elapsed = [r[0] for r in rows if r[0] is not None]
        return ema_eta(elapsed, pending)

    # ------------------------------------------------------------------
    # Leaderboard / matrix / resources
    # ------------------------------------------------------------------

    async def project_leaderboard(
        self,
        user: User | None,
        project: str,
        db: AsyncSession,
        metric: str = "MSE",
        anonymous: bool = False,
    ) -> list[dict[str, Any]] | None:
        if anonymous:
            visible_ids = await self._anonymous_project_batch_ids(project, db)
        else:
            if not await self._can_view_project(user, project, db):
                return None
            visible_ids = await self._visible_batch_ids(user, db, "all")
        rows = (
            await db.execute(
                select(Job, Batch.project)
                .select_from(Job)
                .join(Batch, Batch.id == Job.batch_id)
                .where(Batch.project == project)
                .where(Batch.id.in_(visible_ids))
            )
        ).all()

        # Group by (model, dataset); keep the best row by min-metric value.
        # Jobs that lack the requested metric still appear but rank last.
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for job, _ in rows:
            metrics_dict = _safe_json(job.metrics) or {}
            value = metrics_dict.get(metric)
            has_value = isinstance(value, (int, float))
            # Extract train_epochs from metrics (clients may embed it there).
            raw_epochs = (
                metrics_dict.get("train_epochs") or metrics_dict.get("epochs")
            )
            train_epochs: int | None = (
                int(raw_epochs) if isinstance(raw_epochs, (int, float)) else None
            )
            # Build a clean float-only metrics map (drop non-numeric entries).
            clean_metrics: dict[str, float] | None = {
                k: float(v)
                for k, v in metrics_dict.items()
                if isinstance(v, (int, float))
            } or None

            key = (job.model or "", job.dataset or "")
            candidate: dict[str, Any] = {
                "model": key[0],
                "dataset": key[1],
                "best_metric": float(value) if has_value else None,
                "metric_name": metric if has_value else None,
                "batch_id": job.batch_id,
                "job_id": job.id,
                "status": job.status,
                "train_epochs": train_epochs,
                "elapsed_s": job.elapsed_s,
                "metrics": clean_metrics,
                # Internal sort key: done rows with the metric come first.
                "_has_value": has_value,
            }
            prior = best.get(key)
            if prior is None:
                best[key] = candidate
            elif has_value and not prior["_has_value"]:
                # New row has the metric, prior didn't — prefer the new row.
                best[key] = candidate
            elif has_value and prior["_has_value"]:
                # Both have the metric — keep the lower value.
                if float(value) < prior["best_metric"]:
                    best[key] = candidate
            # else: prior has metric, new doesn't — keep prior

        # Strip internal sort key before returning.
        result = []
        for row in best.values():
            row.pop("_has_value", None)
            result.append(row)

        return sorted(result, key=lambda r: (r["model"], r["dataset"]))

    async def project_matrix(
        self,
        user: User | None,
        project: str,
        db: AsyncSession,
        metric: str = "MSE",
        anonymous: bool = False,
    ) -> dict[str, Any] | None:
        leaderboard = await self.project_leaderboard(
            user, project, db, metric=metric, anonymous=anonymous
        )
        if leaderboard is None:
            return None

        models = sorted({r["model"] for r in leaderboard if r["model"]})
        datasets = sorted(
            {r["dataset"] for r in leaderboard if r["dataset"]}
        )

        # rows × cols dense matrix; None where there's no value.
        # batch_index collects all batch_ids that contributed to each cell
        # (multiple batches can share a (model, dataset) slot).
        index: dict[str, dict[str, float | None]] = {
            r["model"]: {} for r in leaderboard
        }
        batch_index: dict[str, dict[str, list[str]]] = {}
        for r in leaderboard:
            index.setdefault(r["model"], {})[r["dataset"]] = r["best_metric"]
            batch_index.setdefault(r["model"], {}).setdefault(
                r["dataset"], []
            )
            if r.get("batch_id"):
                batch_index[r["model"]][r["dataset"]].append(r["batch_id"])

        values: list[list[float | None]] = []
        # batch_ids mirrors values; each cell is a list[str] (newest first, up to 3).
        batch_ids: list[list[list[str] | None]] = []
        for m in models:
            row: list[float | None] = []
            bid_row: list[list[str] | None] = []
            for d in datasets:
                row.append(index.get(m, {}).get(d))
                bids = batch_index.get(m, {}).get(d)
                bid_row.append(bids[:3] if bids else None)
            values.append(row)
            batch_ids.append(bid_row)

        return {
            "project": project,
            "metric": metric,
            "rows": models,
            "cols": datasets,
            "values": values,
            "batch_ids": batch_ids,
        }

    async def project_resources(
        self,
        user: User | None,
        project: str,
        db: AsyncSession,
        anonymous: bool = False,
    ) -> dict[str, Any] | None:
        if anonymous:
            visible_ids = await self._anonymous_project_batch_ids(project, db)
        else:
            if not await self._can_view_project(user, project, db):
                return None
            visible_ids = await self._visible_batch_ids(user, db, "all")
        batch_rows = (
            await db.execute(
                select(Batch.id, Batch.host)
                .where(Batch.id.in_(visible_ids))
                .where(Batch.project == project)
            )
        ).all()

        batch_ids_here = [r.id for r in batch_rows]
        if not batch_ids_here:
            return {
                "project": project,
                "gpu_hours": 0.0,
                "jobs_completed": 0,
                "avg_job_minutes": None,
                "hourly_heatmap": [[0] * 24 for _ in range(7)],
                "host_distribution": {},
            }

        host_distribution: dict[str, int] = defaultdict(int)
        for r in batch_rows:
            if r.host:
                host_distribution[r.host] += 1

        job_rows = (
            await db.execute(
                select(Job.elapsed_s, Job.end_time, Job.status)
                .where(Job.batch_id.in_(batch_ids_here))
            )
        ).all()

        total_elapsed = 0
        jobs_completed = 0
        heatmap: list[list[int]] = [[0] * 24 for _ in range(7)]
        for r in job_rows:
            if r.status and r.status.lower() == "done":
                jobs_completed += 1
                if r.elapsed_s:
                    total_elapsed += int(r.elapsed_s)
                dt = _parse_iso(r.end_time)
                if dt is not None:
                    # 0 = Monday per datetime.weekday()
                    heatmap[dt.weekday()][dt.hour] += 1

        avg_job_minutes: float | None = None
        if jobs_completed:
            avg_job_minutes = round(
                (total_elapsed / jobs_completed) / 60.0, 2
            )

        return {
            "project": project,
            "gpu_hours": round(total_elapsed / 3600.0, 3),
            "jobs_completed": jobs_completed,
            "avg_job_minutes": avg_job_minutes,
            "hourly_heatmap": heatmap,
            "host_distribution": dict(host_distribution),
        }

    # ------------------------------------------------------------------
    # Host resource timeseries (stacked by batch_id)
    # ------------------------------------------------------------------

    async def host_resource_timeseries(
        self,
        host: str,
        db: AsyncSession,
        metric: str = "gpu_mem_mb",
        since: str | None = None,
        bucket_seconds: int = 60,
    ) -> dict[str, Any] | None:
        """Aggregate ``ResourceSnapshot`` rows into time-buckets stacked by batch.

        Returns ``None`` when no snapshots exist for ``host`` in the
        requested window (caller maps this to 404).

        ``metric`` must be one of: ``gpu_mem_mb``, ``gpu_util_pct``,
        ``cpu_util_pct``, ``ram_mb``.

        Each bucket contains:
        * ``ts``       — bucket start (ISO-8601 / UTC)
        * ``total``    — host-level metric value (averaged within bucket)
        * ``by_batch`` — per-batch_id sum of the matching ``proc_*``
                         column, if present (PR-A columns).  Falls back
                         to empty dict when ``proc_*`` columns don't
                         exist yet (uses ``getattr`` for forward compat).

        ``host_total_capacity`` is extracted from the most recent
        snapshot in the window:
        * ``gpu_mem_mb``   → ``gpu_mem_total_mb``
        * ``ram_mb``       → ``ram_total_mb``
        * ``gpu_util_pct`` / ``cpu_util_pct`` → 100 (percentage max)
        """
        ALLOWED_METRICS = {"gpu_mem_mb", "gpu_util_pct", "cpu_util_pct", "ram_mb"}
        if metric not in ALLOWED_METRICS:
            metric = "gpu_mem_mb"

        # --- parse / default `since` -----------------------------------
        if since is None:
            cutoff = _utcnow() - timedelta(hours=1)
        else:
            # Support relative syntax like "now-2h" / "now-30m".
            rel = since.strip().lower()
            if rel.startswith("now-"):
                tail = rel[4:]
                import re as _re
                m = _re.fullmatch(r"(\d+)(h|m|s)", tail)
                if m:
                    val, unit = int(m.group(1)), m.group(2)
                    delta = {
                        "h": timedelta(hours=val),
                        "m": timedelta(minutes=val),
                        "s": timedelta(seconds=val),
                    }[unit]
                    cutoff = _utcnow() - delta
                else:
                    cutoff = _utcnow() - timedelta(hours=1)
            else:
                parsed = _parse_iso(since)
                cutoff = parsed if parsed is not None else (_utcnow() - timedelta(hours=1))

        since_iso = _iso(cutoff)

        # --- fetch snapshots for this host in the window ---------------
        rows = (
            await db.execute(
                select(ResourceSnapshot)
                .where(ResourceSnapshot.host == host)
                .where(ResourceSnapshot.timestamp >= since_iso)
                .order_by(ResourceSnapshot.timestamp.asc())
            )
        ).scalars().all()

        if not rows:
            # Check whether the host has ANY snapshots ever (for 404 vs empty).
            any_row = (
                await db.execute(
                    select(ResourceSnapshot.id)
                    .where(ResourceSnapshot.host == host)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if any_row is None:
                return None
            # Host exists but no data in the requested window — return empty.
            return {
                "host": host,
                "metric": metric,
                "buckets": [],
                "host_total_capacity": None,
            }

        # --- bucket aggregation ----------------------------------------
        # Map metric name → host-level column attribute + proc-level attr.
        HOST_COL: dict[str, str] = {
            "gpu_mem_mb": "gpu_mem_mb",
            "gpu_util_pct": "gpu_util_pct",
            "cpu_util_pct": "cpu_util_pct",
            "ram_mb": "ram_mb",
        }
        PROC_COL: dict[str, str] = {
            "gpu_mem_mb": "proc_gpu_mem_mb",
            "gpu_util_pct": "proc_cpu_pct",   # no per-proc GPU util; fallback
            "cpu_util_pct": "proc_cpu_pct",
            "ram_mb": "proc_ram_mb",
        }
        CAPACITY_COL: dict[str, str | None] = {
            "gpu_mem_mb": "gpu_mem_total_mb",
            "gpu_util_pct": None,   # percentage → capacity = 100
            "cpu_util_pct": None,
            "ram_mb": "ram_total_mb",
        }

        host_col = HOST_COL[metric]
        proc_col = PROC_COL[metric]
        cap_col = CAPACITY_COL[metric]

        # Determine host_total_capacity from most-recent snapshot.
        latest_snap = rows[-1]
        if cap_col is None:
            host_total_capacity: float | None = 100.0
        else:
            raw_cap = getattr(latest_snap, cap_col, None)
            host_total_capacity = float(raw_cap) if raw_cap is not None else None

        # Bucket rows by floor(epoch // bucket_seconds) * bucket_seconds.
        # Use ISO string parse → epoch arithmetic (avoids dialect-specific
        # time-bucket functions, works on both SQLite and Postgres).
        from collections import defaultdict

        # bucket_key → {host_vals: [float], by_batch: {batch_id: [float]}}
        BucketAcc = dict  # type alias for clarity
        buckets_acc: dict[int, BucketAcc] = defaultdict(
            lambda: {"host_vals": [], "by_batch": defaultdict(list)}
        )

        for row in rows:
            dt = _parse_iso(row.timestamp)
            if dt is None:
                continue
            epoch = int(dt.timestamp())
            bkey = (epoch // bucket_seconds) * bucket_seconds

            host_val = getattr(row, host_col, None)
            if host_val is not None:
                buckets_acc[bkey]["host_vals"].append(float(host_val))

            # Per-batch breakdown — proc_* columns added in PR-A (migration 008).
            # Use getattr with None default so the code works before PR-A lands.
            proc_val = getattr(row, proc_col, None)
            batch_id = getattr(row, "batch_id", None)
            if proc_val is not None and batch_id:
                buckets_acc[bkey]["by_batch"][batch_id].append(float(proc_val))

        # Build sorted bucket list.
        buckets = []
        for bkey in sorted(buckets_acc.keys()):
            acc = buckets_acc[bkey]
            host_vals = acc["host_vals"]
            total: float | None = (
                sum(host_vals) / len(host_vals) if host_vals else None
            )
            by_batch = {
                bid: sum(vals)
                for bid, vals in acc["by_batch"].items()
                if vals
            }
            bucket_ts = _iso(
                datetime.fromtimestamp(bkey, tz=timezone.utc)
            )
            buckets.append({
                "ts": bucket_ts,
                "total": total,
                "by_batch": by_batch,
            })

        return {
            "host": host,
            "metric": metric,
            "buckets": buckets,
            "host_total_capacity": host_total_capacity,
        }


# ---------------------------------------------------------------------------
# Event summary
# ---------------------------------------------------------------------------


def _summarise_event(event_type: str, data: dict[str, Any]) -> str:
    """Short human string for the activity feed."""
    if event_type == "batch_start":
        n = data.get("n_total_jobs")
        return f"batch started ({n} jobs)" if n else "batch started"
    if event_type == "batch_done":
        done = data.get("n_done")
        failed = data.get("n_failed")
        return f"batch done ({done} ok, {failed} failed)"
    if event_type == "batch_failed":
        # ``splitlines()`` returns [] for the empty string, so an empty
        # error message blows up [0]. Normalise via the truthy guard above
        # AND fall back to "" when the lines list is empty.
        _lines = (data.get("error") or "").splitlines()
        err = (_lines[0] if _lines else "")[:60]
        return f"batch failed: {err}" if err else "batch failed"
    if event_type == "job_failed":
        # ``splitlines()`` returns [] for the empty string, so an empty
        # error message blows up [0]. Normalise via the truthy guard above
        # AND fall back to "" when the lines list is empty.
        _lines = (data.get("error") or "").splitlines()
        err = (_lines[0] if _lines else "")[:60]
        return f"job failed: {err}" if err else "job failed"
    return event_type
