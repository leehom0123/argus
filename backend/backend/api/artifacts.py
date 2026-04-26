"""Job artifact upload + download endpoints (roadmap #8).

Routes:
  POST   /api/jobs/{job_id}/artifacts    multipart upload (owner-or-admin)
  GET    /api/jobs/{job_id}/artifacts    list artifacts for a job
  GET    /api/artifacts/{id}             download (Content-Type from row)
  DELETE /api/artifacts/{id}             remove (owner-or-admin)

The upload endpoint enforces two caps:

* per-file: 50 MB (``ARGUS_ARTIFACT_MAX_FILE_MB``) — returns 413 if
  the request body is larger. We read the uploaded file in one shot so
  we can check actual size on disk rather than trust Content-Length.
* per-job cumulative: 500 MB (``ARGUS_ARTIFACT_MAX_JOB_MB``) —
  computed from ``SUM(size_bytes) WHERE job_id = :job``. Also 413.

Because :class:`backend.models.Job` uses a composite ``(id, batch_id)``
primary key there isn't a clean way to look up by ``job_id`` alone, so
the upload resolves the batch via a separate query and we store
``batch_id`` on the artifact row for free downstream reads.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import get_current_user
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Artifact, Batch, Job, User
from backend.schemas.artifacts import ArtifactOut
from backend.services.storage import (
    get_store,
    max_file_bytes,
    max_job_bytes,
    secure_filename,
)
from backend.services.visibility import VisibilityResolver

# Streamed-upload chunk size. 256 KB strikes a balance between syscall
# overhead and the resolution at which we can abort an oversized upload.
_UPLOAD_CHUNK_BYTES = 256 * 1024

log = logging.getLogger(__name__)


jobs_router = APIRouter(prefix="/api/jobs", tags=["artifacts"])
artifacts_router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _row_to_out(row: Artifact) -> ArtifactOut:
    meta: dict[str, Any] | None = None
    if row.meta_json:
        try:
            parsed = json.loads(row.meta_json)
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError:
            meta = None
    return ArtifactOut(
        id=row.id,
        job_id=row.job_id,
        batch_id=row.batch_id,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        label=row.label,
        meta=meta,
        created_at=row.created_at,
    )


async def _resolve_job(
    job_id: str, session: AsyncSession, locale: SupportedLocale
) -> Job:
    """Return the first :class:`Job` row matching ``job_id``.

    Jobs have a composite PK so there could in theory be more than one
    row across batches. In practice reporter-generated ids are
    UUID-flavoured; we still pick the first match deterministically by
    ``batch_id`` so repeated uploads land in the same place.
    """
    stmt = select(Job).where(Job.id == job_id).order_by(Job.batch_id.asc())
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "job.not_found")
        )
    return row


async def _load_batch_or_404(
    batch_id: str, session: AsyncSession, locale: SupportedLocale
) -> Batch:
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(
            status_code=404, detail=tr(locale, "batch.not_found")
        )
    return batch


def _require_owner_or_admin(batch: Batch, user: User, locale: SupportedLocale) -> None:
    if user.is_admin:
        return
    if batch.owner_id == user.id:
        return
    # Non-owners cannot upload / delete even when they can view the
    # batch via a share grant — keeps write semantics simple.
    raise HTTPException(
        status_code=403, detail=tr(locale, "share.batch.owner_only")
    )


async def _ensure_visible(
    batch_id: str, user: User, session: AsyncSession, locale: SupportedLocale
) -> Batch:
    resolver = VisibilityResolver()
    if not await resolver.can_view_batch(user, batch_id, session):
        raise HTTPException(
            status_code=404, detail=tr(locale, "batch.not_found")
        )
    return await _load_batch_or_404(batch_id, session, locale)


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/artifacts
# ---------------------------------------------------------------------------


@jobs_router.post(
    "/{job_id}/artifacts",
    response_model=ArtifactOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_artifact(
    job_id: str,
    file: UploadFile = File(...),
    label: str | None = Form(default=None),
    meta: str | None = Form(default=None),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> ArtifactOut:
    """Accept a multipart file and attach it to ``job_id``.

    Returns the saved artifact row. Auth: owner-or-admin of the job's
    parent batch.
    """
    job = await _resolve_job(job_id, session, locale)
    batch = await _load_batch_or_404(job.batch_id, session, locale)
    _require_owner_or_admin(batch, current, locale)

    file_cap = max_file_bytes()
    job_cap = max_job_bytes()

    # Cumulative per-job baseline. Looked up before reading bytes so a
    # job already at its quota gets rejected with a single COUNT query.
    stmt_sum = (
        select(func.coalesce(func.sum(Artifact.size_bytes), 0))
        .where(Artifact.job_id == job_id)
    )
    current_total = int(
        (await session.execute(stmt_sum)).scalar_one() or 0
    )

    # Stream the upload through a temp file under the store root so a
    # 50 MB body never has to live in RAM. We read fixed-size chunks,
    # accumulate a byte counter, and abort the moment cumulative bytes
    # cross either cap. The temp file MUST live on the same filesystem
    # as the final destination so ``save_from_path`` can ``os.replace``
    # without hitting EXDEV — putting it in ``$TMPDIR`` (the default of
    # ``tempfile.mkstemp``) regresses to a 500 in production when
    # ``/tmp`` is tmpfs and the artifact volume is mounted elsewhere.
    store = get_store()
    tmp_fd, tmp_path = store.make_temp_path()
    size = 0
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > file_cap:
                    raise HTTPException(
                        status_code=413,
                        detail=tr(
                            locale,
                            "artifact.file_too_large",
                            size=size,
                            cap=file_cap,
                        ),
                    )
                if current_total + size > job_cap:
                    raise HTTPException(
                        status_code=413,
                        detail=tr(
                            locale,
                            "artifact.job_quota_exceeded",
                            used=current_total,
                            add=size,
                            cap=job_cap,
                        ),
                    )
                out.write(chunk)

        # Validate + normalise meta.
        meta_json: str | None = None
        if meta is not None and meta != "":
            try:
                parsed = json.loads(meta)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail=tr(locale, "artifact.meta_invalid"),
                )
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=400,
                    detail=tr(locale, "artifact.meta_invalid"),
                )
            meta_json = json.dumps(parsed)

        # Prefer the file extension (server-side guess) over the client-
        # supplied Content-Type — a malicious reporter could otherwise
        # tag a script as ``image/png`` and have the browser execute it.
        # Fall back to the client header, then to the generic
        # ``application/octet-stream``.
        original_name = file.filename or "upload.bin"
        guessed, _ = mimetypes.guess_type(original_name)
        mime = guessed or file.content_type or "application/octet-stream"

        # Allocate the row first so ``save_from_path`` can embed
        # ``artifact.id`` in the on-disk filename (prevents collisions
        # across same-named uploads in the same job).
        row = Artifact(
            job_id=job_id,
            batch_id=job.batch_id,
            filename=original_name,
            mime=mime,
            size_bytes=size,
            label=label,
            meta_json=meta_json,
            storage_path="",  # placeholder; updated after save
            created_at=_utcnow_iso(),
            created_by_user_id=current.id,
        )
        session.add(row)
        await session.flush()  # need the PK for the filename

        try:
            storage_path = store.save_from_path(
                artifact_id=row.id,
                batch_id=job.batch_id,
                job_id=job_id,
                filename=row.filename,
                source_path=tmp_path,
            )
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("artifact save failed: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=tr(locale, "artifact.save_failed"),
            ) from exc
        # save_from_path consumed tmp_path via os.replace; the finally
        # block below will see it gone and skip the unlink.
        tmp_path = None  # type: ignore[assignment]
        row.storage_path = storage_path
        await session.commit()
        await session.refresh(row)
        return _row_to_out(row)
    finally:
        if tmp_path is not None:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/artifacts
# ---------------------------------------------------------------------------


@jobs_router.get(
    "/{job_id}/artifacts", response_model=list[ArtifactOut]
)
async def list_artifacts(
    job_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> list[ArtifactOut]:
    """List artifacts for a job. Auth: any user who can view the batch."""
    job = await _resolve_job(job_id, session, locale)
    await _ensure_visible(job.batch_id, current, session, locale)
    stmt = (
        select(Artifact)
        .where(Artifact.job_id == job_id)
        .order_by(Artifact.created_at.asc(), Artifact.id.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_out(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/artifacts/{id}  (download)
# ---------------------------------------------------------------------------


@artifacts_router.get("/{artifact_id}")
async def download_artifact(
    artifact_id: int,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> FileResponse:
    """Stream the artifact bytes with ``Content-Type`` from the row."""
    row = await session.get(Artifact, artifact_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "artifact.not_found")
        )
    await _ensure_visible(row.batch_id, current, session, locale)
    store = get_store()
    try:
        path = store.open_path(row.storage_path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail=tr(locale, "artifact.not_found")
        )
    # Force ``Content-Disposition: attachment`` (v0.1.3 hardening) so the
    # browser saves the file instead of trying to render it inline — that
    # blocks reflected-XSS via uploaded ``.html`` / ``.svg`` artifacts.
    # ``filename`` is run through ``secure_filename`` once more to scrub
    # any path or control chars that slipped past upload validation.
    safe_name = secure_filename(row.filename or "download.bin")
    return FileResponse(
        str(path),
        media_type=row.mime,
        filename=safe_name,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )


# ---------------------------------------------------------------------------
# DELETE /api/artifacts/{id}
# ---------------------------------------------------------------------------


@artifacts_router.delete(
    "/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_artifact(
    artifact_id: int,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
):
    """Remove an artifact. Auth: owner-or-admin of the parent batch."""
    row = await session.get(Artifact, artifact_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "artifact.not_found")
        )
    batch = await _load_batch_or_404(row.batch_id, session, locale)
    _require_owner_or_admin(batch, current, locale)

    storage_path = row.storage_path
    await session.delete(row)
    await session.commit()

    get_store().delete(storage_path)
    return None
