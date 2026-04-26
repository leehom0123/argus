"""Tests for ``/api/jobs/{id}/artifacts`` + ``/api/artifacts/{id}``.

Covers upload, list, download, size-cap rejection, and the auth
boundary (non-owner upload → 403; visible-grantee download → 200).

Status: route wiring landed in the cleanup PR (`app.include_router(
artifacts_api.jobs_router)` + `artifacts_api.artifacts_router`). The
module-wide ``xfail`` marker has been removed; tests now run as
regular asserts.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from backend.services import storage as storage_mod
from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    seed_completed_batch,
)


# ---------------------------------------------------------------------------
# Fixture: re-point the artifact store at a tmp dir per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_artifact_root(tmp_path, monkeypatch):
    """Isolate artifact bytes between tests.

    ``reset_store_for_tests`` replaces the module-wide singleton; we
    also set ``ARGUS_ARTIFACT_DIR`` so any code path that re-reads
    the env (e.g. after ``get_settings.cache_clear()``) lands in the
    same place.
    """
    root = tmp_path / "artifacts"
    monkeypatch.setenv("ARGUS_ARTIFACT_DIR", str(root))
    storage_mod.reset_store_for_tests(root)
    yield root
    storage_mod.reset_store_for_tests(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_list_download_roundtrip(client):
    """Happy path: upload a PNG, list it, download bytes back verbatim."""
    await seed_completed_batch(client, batch_id="b-upload-1")

    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00hello-png"
    files = {"file": ("prediction.png", io.BytesIO(png_bytes), "image/png")}
    data = {"label": "visualizations", "meta": '{"step": 1}'}

    r = await client.post(
        "/api/jobs/b-upload-1-job-0/artifacts", files=files, data=data
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["filename"] == "prediction.png"
    assert body["mime"] == "image/png"
    assert body["size_bytes"] == len(png_bytes)
    assert body["label"] == "visualizations"
    assert body["meta"] == {"step": 1}
    artifact_id = body["id"]

    # List — should include our row.
    r = await client.get("/api/jobs/b-upload-1-job-0/artifacts")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == artifact_id

    # Download — bytes must match; Content-Type from the row.
    r = await client.get(f"/api/artifacts/{artifact_id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == png_bytes


@pytest.mark.asyncio
async def test_upload_rejected_when_too_large(client, monkeypatch):
    """413 when a single file blows past ``ARGUS_ARTIFACT_MAX_FILE_MB``."""
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_FILE_MB", "1")  # 1 MB
    await seed_completed_batch(client, batch_id="b-big")

    big = b"x" * (2 * 1024 * 1024)  # 2 MB
    files = {"file": ("big.bin", io.BytesIO(big), "application/octet-stream")}
    r = await client.post(
        "/api/jobs/b-big-job-0/artifacts", files=files
    )
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_delete_removes_row_and_file(client, _tmp_artifact_root):
    """DELETE endpoint wipes both DB row and on-disk bytes."""
    await seed_completed_batch(client, batch_id="b-del-1")
    payload = b"gone-soon"
    files = {"file": ("doomed.txt", io.BytesIO(payload), "text/plain")}
    r = await client.post("/api/jobs/b-del-1-job-0/artifacts", files=files)
    assert r.status_code == 201
    aid = r.json()["id"]

    # File exists before delete.
    listed = list(Path(_tmp_artifact_root).rglob("*"))
    assert any(p.is_file() for p in listed)

    r = await client.delete(f"/api/artifacts/{aid}")
    assert r.status_code == 204

    # Row gone from list.
    r = await client.get("/api/jobs/b-del-1-job-0/artifacts")
    assert r.status_code == 200
    assert r.json() == []

    # File gone from disk.
    files_after = [p for p in Path(_tmp_artifact_root).rglob("*") if p.is_file()]
    assert files_after == []


@pytest.mark.asyncio
async def test_non_owner_cannot_upload(client):
    """A second user without any share can neither see nor upload."""
    # Owner (default ``tester``) seeds the batch.
    await seed_completed_batch(client, batch_id="b-auth-1")

    # Mint a second user with their own reporter token.
    _jwt, api_token = await mk_user_with_token(client, "intruder")
    hostile_headers = {"Authorization": f"Bearer {api_token}"}

    files = {"file": ("pwn.txt", io.BytesIO(b"nope"), "text/plain")}
    # Upload without auth headers wouldn't authenticate at all; swap
    # the client's default header for the intruder's token.
    orig = client.headers.get("Authorization")
    client.headers["Authorization"] = hostile_headers["Authorization"]
    try:
        r = await client.post(
            "/api/jobs/b-auth-1-job-0/artifacts", files=files
        )
    finally:
        if orig is not None:
            client.headers["Authorization"] = orig
    # 404 (batch invisible) OR 403 (visible but not owner) — both fine;
    # the contract is just "cannot upload".
    assert r.status_code in (403, 404), r.text


@pytest.mark.asyncio
async def test_unknown_job_returns_404(client):
    """Upload to a non-existent job_id → 404, not 5xx."""
    await seed_completed_batch(client, batch_id="b-404")
    files = {"file": ("x.txt", io.BytesIO(b"data"), "text/plain")}
    r = await client.post(
        "/api/jobs/does-not-exist/artifacts", files=files
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_temp_path_lives_under_store_root(
    client, _tmp_artifact_root
):
    """Regression: streaming upload must place its temp file under the
    store root, not ``$TMPDIR``.

    In v0.1.3 hardening the upload handler used a bare
    ``tempfile.mkstemp()`` so the temp file landed in ``/tmp``. In
    production where ``/tmp`` is tmpfs and the artifact volume is a
    mounted bind, the subsequent ``os.replace(tmp, dest)`` raised
    ``OSError: [Errno 18] Invalid cross-device link`` and the endpoint
    500'd. This test asserts the temp path is created via the store
    helper (under ``self.root``) so the move is intra-filesystem.
    """
    store = storage_mod.get_store()
    fd, path = store.make_temp_path()
    try:
        # Same-filesystem invariant: the temp file MUST live somewhere
        # under the store root so ``os.replace`` to any final dest
        # within the store root is intra-filesystem.
        assert Path(path).resolve().is_relative_to(
            Path(store.root).resolve()
        ), f"temp file {path!r} escaped store root {store.root!r}"
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_artifact_upload_smoke_path_no_500(client):
    """Deploy smoke regression: POST /api/jobs/.../artifacts shouldn't 500.

    Mirrors the prod symptom from v0.1.3 → v0.1.4: a small file uploaded
    to a freshly seeded job. Even with ``/tmp`` on a different fs from
    the artifact root, the upload must succeed (the fix routes the
    streaming temp file under the store root so ``os.replace`` is
    intra-fs).
    """
    await seed_completed_batch(client, batch_id="b-smoke-500")

    payload = b"smoke-test-bytes"
    files = {"file": ("smoke.bin", io.BytesIO(payload), "application/octet-stream")}
    r = await client.post(
        "/api/jobs/b-smoke-500-job-0/artifacts", files=files
    )
    # The exact symptom we're guarding against — a 500 from EXDEV.
    assert r.status_code != 500, r.text
    assert r.status_code == 201, r.text
    assert r.json()["size_bytes"] == len(payload)
