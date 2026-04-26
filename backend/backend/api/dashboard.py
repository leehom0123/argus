"""``GET /api/dashboard`` — single-payload home page aggregate.

Delegates to :class:`DashboardService` for the heavy lifting so the
router stays a thin adapter. The service already applies visibility
filters (mine + shared; admins get everything when ``scope='all'``).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import User
from backend.schemas.dashboard import DashboardOut
from backend.services.dashboard import DashboardService
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOut)
async def get_dashboard(
    scope: Literal["mine", "shared", "all"] = Query(
        default="all",
        description=(
            "Visibility filter: 'mine' = I own, 'shared' = shared to "
            "me, 'all' = mine ∪ shared (admins see everything)."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardOut:
    """Return the home page payload.

    One round-trip fills every panel in the §16.2 layout — counters,
    project cards, activity feed, host cards, notifications.
    """
    # Key by user id + scope. Per-user keying keeps the cache safe for
    # visibility (``mine`` / ``shared`` already depend on user). Admins
    # see a superset via ``scope=all`` which is again tied to their id.
    key = f"dashboard:u{user.id}:{scope}"

    async def _load() -> DashboardOut:
        svc = DashboardService()
        payload = await svc.home(user, db, scope=scope)
        return DashboardOut.model_validate(payload)

    return await _response_cache.get_or_compute(key, _load)
