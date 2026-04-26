"""Team Email — admin SMTP + template endpoints."""
from __future__ import annotations

import pytest

from backend.db import SessionLocal
from backend.services.email_templates import seed_default_templates


pytestmark = pytest.mark.asyncio


async def _seed(db_factory=SessionLocal):
    async with db_factory() as db:
        await seed_default_templates(db)
        await db.commit()


async def _jwt_headers(client):
    # conftest's ``client`` defaults to a reporter API token. Admin
    # endpoints require a real JWT session, so grab the stashed one.
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


async def test_get_smtp_config_masked_password(client):
    r = await client.get("/api/admin/email/smtp", headers=await _jwt_headers(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["smtp_password"] == "***"
    assert body["enabled"] is False


async def test_put_smtp_preserves_password_on_mask(client):
    # First PUT sets an explicit password.
    r = await client.put(
        "/api/admin/email/smtp",
        headers=await _jwt_headers(client),
        json={
            "enabled": True,
            "smtp_host": "smtp.example.org",
            "smtp_port": 587,
            "smtp_username": "robot",
            "smtp_password": "s3cret!",
            "smtp_from_address": "noreply@example.org",
            "smtp_from_name": "Monitor",
            "use_tls": True,
            "use_ssl": False,
        },
    )
    assert r.status_code == 200, r.text

    # Second PUT with masked password must NOT overwrite.
    r2 = await client.put(
        "/api/admin/email/smtp",
        headers=await _jwt_headers(client),
        json={
            "enabled": False,
            "smtp_host": "smtp.example.org",
            "smtp_port": 587,
            "smtp_username": "robot",
            "smtp_password": "***",  # sentinel → preserve
            "smtp_from_address": "noreply@example.org",
            "smtp_from_name": "Monitor",
            "use_tls": True,
            "use_ssl": False,
        },
    )
    assert r2.status_code == 200
    # Confirm by loading the row directly — response always masks.
    from backend.models import SmtpConfig
    async with SessionLocal() as db:
        row = await db.get(SmtpConfig, 1)
    assert row.smtp_password_encrypted == "s3cret!"
    assert row.enabled is False  # other fields still updated


async def test_smtp_requires_admin(client):
    # Strip JWT → reporter token in place; reporter is NOT admin when
    # the 'tester' user is first user in fresh DB, which is actually admin.
    # Create a non-admin user and verify 403.
    # Register a second user (not first => not admin)
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "mallory",
            "email": "mallory@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201, r.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "mallory", "password": "password123"},
    )
    assert login.status_code == 200
    bad_jwt = login.json()["access_token"]
    r = await client.get(
        "/api/admin/email/smtp",
        headers={"Authorization": f"Bearer {bad_jwt}"},
    )
    assert r.status_code == 403


async def test_list_templates(client):
    await _seed()
    r = await client.get(
        "/api/admin/email/templates",
        headers=await _jwt_headers(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 12


async def test_update_and_preview_and_reset_template(client):
    await _seed()
    # Pick the batch_done / en-US row
    r = await client.get(
        "/api/admin/email/templates",
        headers=await _jwt_headers(client),
    )
    rows = r.json()
    tpl = next(t for t in rows if t["event_type"] == "batch_done" and t["locale"] == "en-US")

    # update
    r2 = await client.put(
        f"/api/admin/email/templates/{tpl['id']}",
        headers=await _jwt_headers(client),
        json={
            "subject": "edited subject",
            "body_html": "<p>edited body {{ batch.id }}</p>",
            "body_text": "edited text {{ batch.id }}",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["subject"] == "edited subject"
    # event_type / locale intentionally not present in body; endpoint
    # keeps them read-only.
    assert r2.json()["event_type"] == "batch_done"
    assert r2.json()["locale"] == "en-US"

    # preview should render with sample context
    r3 = await client.post(
        f"/api/admin/email/templates/{tpl['id']}/preview",
        headers=await _jwt_headers(client),
    )
    assert r3.status_code == 200
    rendered = r3.json()
    assert "bench-sample" in rendered["body_html"]

    # reset
    r4 = await client.post(
        f"/api/admin/email/templates/{tpl['id']}/reset",
        headers=await _jwt_headers(client),
    )
    assert r4.status_code == 200
    assert r4.json()["subject"] != "edited subject"


async def test_template_404(client):
    await _seed()
    r = await client.get(
        "/api/admin/email/templates/999999",
        headers=await _jwt_headers(client),
    )
    assert r.status_code == 404
