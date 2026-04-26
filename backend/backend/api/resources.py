"""Host resource timeseries endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import get_current_user
from backend.models import HostMeta, ResourceSnapshot, User
from backend.schemas import ResourceSnapshotOut

router = APIRouter(prefix="/api/resources", tags=["resources"])


async def _deleted_host_names(session: AsyncSession) -> set[str]:
    """Return the set of hosts soft-deleted via migration 021.

    Used by host-list / host-detail queries to filter retired hosts
    out of the UI without dropping the underlying snapshot history.
    """
    rows = (
        await session.execute(
            select(HostMeta.host).where(HostMeta.is_deleted.is_(True))
        )
    ).scalars().all()
    return set(rows)


@router.get("/hosts", response_model=list[str])
async def list_hosts(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Distinct hosts that have posted any resource snapshot.

    Resources are host-scoped, not user-scoped, so any authenticated
    user can see the host inventory. Per-host batch visibility is still
    enforced elsewhere.
    """
    stmt = select(ResourceSnapshot.host).distinct()
    rows = (await session.execute(stmt)).scalars().all()
    deleted = await _deleted_host_names(session)
    return sorted(h for h in rows if h and h not in deleted)


@router.get("", response_model=list[ResourceSnapshotOut])
async def list_snapshots(
    host: str | None = None,
    since: str | None = Query(
        default=None,
        description="ISO 8601 timestamp; include snapshots at or after this.",
    ),
    limit: int = Query(default=500, ge=1, le=5000),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ResourceSnapshotOut]:
    """Return recent resource snapshots, newest first."""
    stmt = select(ResourceSnapshot)
    if host is not None:
        stmt = stmt.where(ResourceSnapshot.host == host)
    if since is not None:
        stmt = stmt.where(ResourceSnapshot.timestamp >= since)
    stmt = stmt.order_by(ResourceSnapshot.timestamp.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [ResourceSnapshotOut.model_validate(r) for r in rows]
