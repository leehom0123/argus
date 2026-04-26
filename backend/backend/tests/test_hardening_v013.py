"""Backend hardening sprint v0.1.3 — security/safety regression tests.

Each test pins one of the five fixes covered by the sprint:

* SMTP test endpoint — per-admin 10/hour rate limit + optional host
  allowlist via ``ARGUS_SMTP_HOST_ALLOWLIST``.
* Bulk-delete endpoints — pydantic ``Field(max_length=500)`` returns
  422 above the cap; the 500-id boundary still succeeds with 200.
* Artifact upload — streamed chunked read with byte counter rejects
  oversize bodies via 413 without buffering the whole payload.
* Artifact download — response carries
  ``Content-Disposition: attachment`` so browsers save instead of
  rendering inline (blocks reflected-XSS via uploaded HTML/SVG).
* Token mint cap — 51st active token returns 409 ``token.mint_cap_exceeded``.
* Rerun overrides — body whose JSON serialises to >64 KB returns 422
  via the field validator on ``RerunIn.overrides``.
"""
from __future__ import annotations

import io
import json
import uuid

import pytest

from backend.tests._dashboard_helpers import seed_completed_batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batch_start_event(batch_id: str, project: str = "proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-25T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_total_jobs": 1, "command": "run.py"},
    }


def _batch_done_event(batch_id: str, project: str = "proj") -> dict:
    """Companion to ``_batch_start_event`` — flips the batch to terminal.

    The v0.1.3 delete-guard (``test_delete_guards``) refuses to soft-delete
    a batch whose status is still ``running`` / ``pending`` / ``stopping``,
    so any hardening test that wants to exercise a *successful* delete
    path must first pair its start event with a ``batch_done`` so the
    parent row lands in a terminal state.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-25T10:05:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 30},
    }


async def _admin_jwt(client) -> str:
    # The conftest ``client`` registers ``tester`` as the first user, who
    # auto-promotes to admin. That JWT is stashed on the client.
    return getattr(client, "_test_default_jwt")


# ---------------------------------------------------------------------------
# Fix 1 — SMTP test endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_test_rate_limited_after_10_calls(client, monkeypatch):
    """The 11th SMTP test within an hour returns 429 with Retry-After."""
    from backend.api import email_admin

    async def fake(**kwargs):
        return True, "sent"

    monkeypatch.setattr(email_admin, "_smtp_test_impl", fake)
    payload = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "u",
        "password": "hunter2",
        "use_tls": True,
        "from_addr": "noreply@example.com",
    }
    # First 10 calls drain the bucket (capacity=10).
    for i in range(10):
        r = await client.post("/api/admin/email/smtp/test", json=payload)
        assert r.status_code == 200, f"call {i + 1}: {r.text}"
    # 11th call hits the cap.
    r = await client.post("/api/admin/email/smtp/test", json=payload)
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_smtp_test_host_allowlist_blocks_unknown(client, monkeypatch):
    """Unknown host returns 403 + i18n message when allowlist is configured."""
    from backend.api import email_admin

    monkeypatch.setenv("ARGUS_SMTP_HOST_ALLOWLIST", "mail.example.com")

    async def fake(**kwargs):  # should not be called
        raise AssertionError("transport must not run for blocked host")

    monkeypatch.setattr(email_admin, "_smtp_test_impl", fake)
    r = await client.post(
        "/api/admin/email/smtp/test",
        json={
            "host": "evil.com",
            "port": 587,
            "username": "u",
            "password": "x",
            "use_tls": True,
            "from_addr": "noreply@example.com",
        },
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert "evil.com" in (body.get("detail") or "")


@pytest.mark.asyncio
async def test_smtp_test_host_allowlist_permits_listed(client, monkeypatch):
    """Allowlisted hosts pass the gate and reach the transport."""
    from backend.api import email_admin

    monkeypatch.setenv("ARGUS_SMTP_HOST_ALLOWLIST", "mail.example.com,backup.example.com")

    async def fake(**kwargs):
        return True, "sent"

    monkeypatch.setattr(email_admin, "_smtp_test_impl", fake)
    r = await client.post(
        "/api/admin/email/smtp/test",
        json={
            "host": "MAIL.example.com",  # case-insensitive
            "port": 587,
            "username": "u",
            "password": "x",
            "use_tls": True,
            "from_addr": "noreply@example.com",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Fix 2 — Bulk-delete cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_delete_batches_501_ids_returns_422(client):
    """501 ids in a single bulk-delete is rejected by pydantic with 422."""
    jwt = await _admin_jwt(client)
    ids = [f"b-{i}" for i in range(501)]
    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": ids},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_bulk_delete_batches_500_ids_boundary(client):
    """500 ids is the upper bound and still succeeds with 200."""
    jwt = await _admin_jwt(client)
    # Only one of the 500 ids exists; the rest go into ``skipped``. The
    # test verifies that the handler ran (200) rather than the cap (422).
    real_id = "bulk-cap-real"
    await client.post("/api/events", json=_batch_start_event(real_id))
    # Flip the parent batch to ``done`` so the v0.1.3 delete-guard
    # (which routes running batches into ``skipped``) doesn't mask the
    # 500-id boundary contract this test is pinning.
    await client.post("/api/events", json=_batch_done_event(real_id))
    ids = [real_id] + [f"missing-{i}" for i in range(499)]
    assert len(ids) == 500
    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": ids},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert real_id in body["deleted"]
    assert len(body["skipped"]) == 499


# ---------------------------------------------------------------------------
# Fix 3 — Artifact upload streamed + Content-Disposition on download
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_artifact_root(tmp_path, monkeypatch):
    """Per-test artifact store root; mirrors test_artifacts.py fixture."""
    from backend.services import storage as storage_mod

    root = tmp_path / "artifacts"
    monkeypatch.setenv("ARGUS_ARTIFACT_DIR", str(root))
    storage_mod.reset_store_for_tests(root)
    yield root
    storage_mod.reset_store_for_tests(None)


@pytest.mark.asyncio
async def test_artifact_upload_oversize_returns_413_streamed(
    client, monkeypatch, _tmp_artifact_root
):
    """A body larger than the per-file cap is rejected with 413.

    With the streamed implementation the response should arrive without
    the entire body ever being held in process memory; we verify the
    contract surface (status code + on-disk leftovers) since the
    streaming detail is internal.
    """
    monkeypatch.setenv("ARGUS_ARTIFACT_MAX_FILE_MB", "1")  # 1 MB cap
    await seed_completed_batch(client, batch_id="b-stream-1")

    # 2 MB body — well past the 1 MB cap; chunked detector should abort
    # before reading all of it.
    big = b"y" * (2 * 1024 * 1024)
    files = {"file": ("big.bin", io.BytesIO(big), "application/octet-stream")}
    r = await client.post(
        "/api/jobs/b-stream-1-job-0/artifacts", files=files
    )
    assert r.status_code == 413, r.text

    # No leftover ``.part`` temp files in the artifact root.
    leftovers = list(_tmp_artifact_root.rglob("*.part"))
    assert leftovers == [], leftovers


@pytest.mark.asyncio
async def test_artifact_download_has_content_disposition_attachment(
    client, _tmp_artifact_root
):
    """Download response must carry Content-Disposition: attachment."""
    await seed_completed_batch(client, batch_id="b-cd-1")
    payload = b"download-me"
    files = {"file": ("plot.png", io.BytesIO(payload), "image/png")}
    r = await client.post("/api/jobs/b-cd-1-job-0/artifacts", files=files)
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    r = await client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd.lower(), cd
    assert "plot.png" in cd


# ---------------------------------------------------------------------------
# Fix 4 — Token mint cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_mint_cap_blocks_51st_token(client):
    """The 51st active token returns 409 token.mint_cap_exceeded."""
    jwt = await _admin_jwt(client)
    # The conftest fixture already minted ``default-test-token`` (1).
    # Mint another 49 to bring active total to 50.
    for i in range(49):
        r = await client.post(
            "/api/tokens",
            json={"name": f"t-{i}", "scope": "reporter"},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert r.status_code == 201, f"i={i}: {r.text}"
    # 51st mint must hit the cap.
    r = await client.post(
        "/api/tokens",
        json={"name": "one-too-many", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 409, r.text
    detail = r.json().get("detail") or ""
    assert "50" in detail or "token" in detail.lower()


@pytest.mark.asyncio
async def test_token_mint_cap_revoke_frees_slot(client):
    """Revoking a token frees up a slot under the cap."""
    jwt = await _admin_jwt(client)
    minted_ids: list[int] = []
    for i in range(49):
        r = await client.post(
            "/api/tokens",
            json={"name": f"slot-{i}", "scope": "reporter"},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        assert r.status_code == 201, r.text
        minted_ids.append(r.json()["id"])
    # 51st mint is over the cap.
    r = await client.post(
        "/api/tokens",
        json={"name": "blocked", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 409, r.text

    # Revoke one and retry — should now be 201.
    rv = await client.delete(
        f"/api/tokens/{minted_ids[0]}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert rv.status_code == 200, rv.text

    r = await client.post(
        "/api/tokens",
        json={"name": "after-revoke", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Fix 5 — Rerun overrides body cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_overrides_too_large_returns_422(client):
    """An overrides payload that serialises to >64 KB is rejected with 422."""
    jwt = await _admin_jwt(client)
    # Seed the source batch.
    r = await client.post("/api/events", json=_batch_start_event("rerun-cap-1"))
    assert r.status_code == 200

    # Build a payload that easily exceeds 64 KB once JSON-encoded:
    # 4096 keys × ~24-byte values ≈ 100 KB.
    big = {f"k{i}": "x" * 24 for i in range(4096)}
    serialised_size = len(json.dumps(big).encode("utf-8"))
    assert serialised_size > 64 * 1024, serialised_size

    r = await client.post(
        "/api/batches/rerun-cap-1/rerun",
        json={"overrides": big},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 422, r.text
