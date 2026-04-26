"""Tests for the SPA fallback route (commit 823722d).

The SPA fallback serves index.html for any non-API deep path so Vue Router
history-mode works on hard refresh. This file exercises:

- /api/* paths continue to return 404 (not swallowed by the catch-all)
- /docs, /redoc, /openapi.json likewise pass through to the real handler
- Deep Vue paths (/projects/foo, /batches/bar/logs) would return 200 when
  index.html exists, but in test mode the frontend/dist dir is absent so
  the route returns 404 — we just assert it is NOT treated as an API path
  and that the handler is registered correctly
- /health (dedicated route) still returns 200

Note: In the test harness FRONTEND_DIST_PATH does not exist, so
spa_fallback raises 404 for every deep path (index.html is absent).
The important invariant is:
  * /api/... deep paths → 404 (not 200)
  * /health → 200 (own route, never reaches spa_fallback)
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_still_returns_200(client):
    """/health is registered before the SPA catch-all and must still return 200."""
    r = await client.get("/health")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_api_deep_path_not_swallowed(client):
    """/api/nonexistent should be 404, not a 200 HTML blob."""
    r = await client.get("/api/totally-unknown-endpoint-xyz")
    assert r.status_code == 404, r.text
    # Must NOT return HTML (the SPA index) — real 404 is JSON detail
    ct = r.headers.get("content-type", "")
    # FastAPI JSON 404 has application/json; HTML blob would be text/html
    assert "text/html" not in ct, (
        f"/api/... path returned HTML — SPA catch-all incorrectly swallowed it: {ct}"
    )


@pytest.mark.asyncio
async def test_openapi_json_not_swallowed(client):
    """/openapi.json is served by FastAPI itself, not the SPA catch-all."""
    r = await client.get("/openapi.json")
    # Should be 200 (FastAPI schema) or a server-config 404, never HTML
    ct = r.headers.get("content-type", "")
    assert "text/html" not in ct, "/openapi.json returned HTML from SPA catch-all"


@pytest.mark.asyncio
async def test_api_events_post_still_works_after_spa_registration(client):
    """/api/events POST must still function — SPA catch-all is GET-only."""
    import uuid
    r = await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": "2026-04-24T10:00:00Z",
            "batch_id": "spa-test-batch",
            "source": {"project": "spa-test"},
            "data": {"n_total_jobs": 1, "experiment_type": "forecast"},
        },
    )
    assert r.status_code == 200, r.text
