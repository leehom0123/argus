"""``/api/stars`` — per-user favourites.

Star toggle is idempotent: re-POSTing the same ``(target_type, target_id)``
returns 200 OK with the existing row's timestamp preserved. DELETE on a
missing row returns 204 (no-op) rather than 404 — the UI shouldn't have
to juggle "is this already unstarred?" race conditions.

Stars are private: no visibility join, no cross-user lookup. A user can
star anything they like (including projects / batches they don't
currently see) — the star just becomes visible once the share lands.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, User, UserStar
from backend.schemas.stars import StarIn, StarOut
from backend.services.visibility import _demo_project_subquery

router = APIRouter(prefix="/api/stars", tags=["stars"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.get("", response_model=list[StarOut])
async def list_stars(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[StarOut]:
    """Return every row the caller has starred, newest first.

    Demo-leak filter (Unify reviewer nit): if the caller previously
    starred a project or batch that has since been re-tagged as a demo
    fixture, hide it from the list. Stars are private so we still keep
    the underlying row — the user can re-see it if the project flips
    back to non-demo — but the API response stays clean of demo
    bleed-through.

    Reuses :func:`backend.services.visibility._demo_project_subquery`
    so the demo flag remains the single source of truth.
    """
    rows = (
        await db.execute(
            select(UserStar)
            .where(UserStar.user_id == user.id)
            .order_by(UserStar.starred_at.desc())
        )
    ).scalars().all()
    if not rows:
        return []

    # Resolve every demo-flagged project once. Two filters apply:
    #   1. Direct project stars whose target_id IS a demo project.
    #   2. Batch stars whose batch.project IS a demo project.
    demo_projects = set(
        (
            await db.execute(_demo_project_subquery())
        ).scalars().all()
    )

    # Pre-resolve batch → project for any 'batch' targets so we can
    # filter (2) without N round-trips.
    batch_target_ids = [
        r.target_id for r in rows if r.target_type == "batch"
    ]
    batch_project_map: dict[str, str | None] = {}
    if batch_target_ids:
        batch_rows = (
            await db.execute(
                select(Batch.id, Batch.project).where(
                    Batch.id.in_(batch_target_ids)
                )
            )
        ).all()
        batch_project_map = {bid: proj for bid, proj in batch_rows}

    out: list[StarOut] = []
    for r in rows:
        if r.target_type == "project" and r.target_id in demo_projects:
            continue
        if r.target_type == "batch":
            project = batch_project_map.get(r.target_id)
            if project is not None and project in demo_projects:
                continue
        out.append(StarOut.model_validate(r))
    return out


@router.post("", response_model=StarOut)
async def add_star(
    payload: StarIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StarOut:
    """Star a project or a batch.

    Idempotent — re-posting the same target returns the existing row
    (200 OK, not 201). This keeps the UI's optimistic-update pattern
    simple.
    """
    existing = await db.get(
        UserStar, (user.id, payload.target_type, payload.target_id)
    )
    if existing is not None:
        return StarOut.model_validate(existing)

    row = UserStar(
        user_id=user.id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        starred_at=_utcnow_iso(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return StarOut.model_validate(row)


@router.delete(
    "/{target_type}/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_star(
    target_type: str,
    target_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> Response:
    """Un-star. No-op if the star doesn't exist."""
    if target_type not in {"project", "batch"}:
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "star.invalid_target_type"),
        )
    await db.execute(
        delete(UserStar)
        .where(UserStar.user_id == user.id)
        .where(UserStar.target_type == target_type)
        .where(UserStar.target_id == target_id)
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
