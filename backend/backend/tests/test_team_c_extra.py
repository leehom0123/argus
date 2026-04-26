"""Team C QA — extra edge-case coverage for #31 Sessions + #19 Compare N>4.

These tests complement ``test_sessions.py`` + ``test_compare_n_gt_4.py`` by
probing areas the original test authors didn't hit: concurrent revokes,
user-agent length DoS surface, expired row filtering, cross-user 404
semantics, dedupe contract in compare, and the exact cap wording.

The goal is independent verification, not re-implementation; failures here
should be reported back, not silently patched.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.schemas.compare import MAX_COMPARE_BATCHES
from backend.tests._dashboard_helpers import seed_completed_batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(
    client, username: str, *, user_agent: str | None = None
) -> str:
    """Register + login; return JWT."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    headers = {"user-agent": user_agent} if user_agent else None
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
        headers=headers,
    )
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"]


# ===========================================================================
# #31 Sessions — edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_revoke_takes_effect_same_cycle(client):
    """Revoking a JWT blocks its next request without a delay.

    Exercises two logins for the same user: we revoke one, and the very
    next authed call from that JWT must 401. This is the "within one
    cycle" guarantee the design claims.
    """
    jwt_a = await _register_and_login(client, "alice", user_agent="pytest/a")
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/b"},
    )
    jwt_b = lr2.json()["access_token"]

    # Find A's jti from B's sessions view.
    r = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    rows = r.json()
    a_jti = next(s for s in rows if s["user_agent"] == "pytest/a")["jti"]

    # Revoke A via B's auth.
    rv = await client.post(
        f"/api/auth/sessions/{a_jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert rv.status_code == 200

    # Immediate next call with A's JWT must 401 (no cache-warmup delay).
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt_a}"}
    )
    assert r.status_code == 401

    # B still works.
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_revoke_other_users_jti_returns_404_not_403(client):
    """Cross-user revoke must 404 (not 403) to avoid existence leakage."""
    await _register_and_login(client, "alice")
    bob_jwt = await _register_and_login(client, "bob")

    # Login as alice separately to generate alice's jti.
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    alice_jwt = lr.json()["access_token"]
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    alice_jti = r.json()[0]["jti"]

    # Bob tries to revoke alice's jti.
    rv = await client.post(
        f"/api/auth/sessions/{alice_jti}/revoke",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert rv.status_code == 404
    # Critical: not 403. A 403 would leak "this jti exists but belongs
    # to someone else" to the attacker.
    assert rv.status_code != 403


@pytest.mark.asyncio
async def test_revoked_session_filtered_from_list(client):
    """After revoke, GET /sessions must not list the revoked row.

    The backend chose filter-out semantics (revoked_at IS NULL WHERE).
    Locks that choice in so a future schema change doesn't flip the
    convention without updating the UI.
    """
    jwt_a = await _register_and_login(client, "alice", user_agent="pytest/a")
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/b"},
    )
    jwt_b = lr2.json()["access_token"]

    r = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    rows = r.json()
    assert len(rows) == 2
    a_jti = next(s for s in rows if s["user_agent"] == "pytest/a")["jti"]

    await client.post(
        f"/api/auth/sessions/{a_jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )

    r2 = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    rows2 = r2.json()
    assert len(rows2) == 1
    assert rows2[0]["jti"] != a_jti
    # Also: no row has revoked_at populated (we filter them out).
    assert all("revoked_at" not in r or not r.get("revoked_at") for r in rows2)


@pytest.mark.asyncio
async def test_last_seen_monotonic_across_requests(client):
    """Two calls 1s+ apart → last_seen_at strictly increases."""
    jwt = await _register_and_login(client, "alice")
    r1 = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt}"}
    )
    first = r1.json()[0]["last_seen_at"]
    time.sleep(1.05)
    await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    r2 = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt}"}
    )
    second = r2.json()[0]["last_seen_at"]
    assert second >= first  # monotonic
    assert second != first  # actually changed


@pytest.mark.asyncio
async def test_double_revoke_is_idempotent(client):
    """Revoking the same JTI twice must not crash; second call returns 200
    or 404 (depending on whether BE considers revoked rows still "visible")."""
    jwt_a = await _register_and_login(client, "alice", user_agent="pytest/a")
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/b"},
    )
    jwt_b = lr2.json()["access_token"]

    r = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    a_jti = next(s for s in r.json() if s["user_agent"] == "pytest/a")["jti"]

    rv1 = await client.post(
        f"/api/auth/sessions/{a_jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert rv1.status_code == 200
    rv2 = await client.post(
        f"/api/auth/sessions/{a_jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    # Either 200 (already revoked, no-op) or 404 (row considered gone) is
    # acceptable. A 500 would be a bug.
    assert rv2.status_code in (200, 404), rv2.text


@pytest.mark.asyncio
async def test_parallel_double_revoke_no_crash(client):
    """Two concurrent revokes of same JTI → neither returns 500."""
    jwt_a = await _register_and_login(client, "alice", user_agent="pytest/a")
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/b"},
    )
    jwt_b = lr2.json()["access_token"]

    r = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    a_jti = next(s for s in r.json() if s["user_agent"] == "pytest/a")["jti"]

    async def _do_revoke():
        return await client.post(
            f"/api/auth/sessions/{a_jti}/revoke",
            headers={"Authorization": f"Bearer {jwt_b}"},
        )

    r1, r2 = await asyncio.gather(_do_revoke(), _do_revoke())
    assert r1.status_code < 500, r1.text
    assert r2.status_code < 500, r2.text


@pytest.mark.asyncio
async def test_sessions_unauth_returns_401(unauthed_client):
    r = await unauthed_client.get("/api/auth/sessions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revoke_unauth_returns_401(unauthed_client):
    r = await unauthed_client.post("/api/auth/sessions/some-jti/revoke")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_long_user_agent_header_truncated_to_512(client):
    """A massive user-agent header is truncated to 512 B at persistence time.

    ``record_active_session`` caps the UA to 512 bytes before insert to
    bound the active_sessions row size against oversized-header DoS.
    Upstream stack may additionally refuse very large headers, but the
    application layer enforces its own ceiling independent of that.
    """
    long_ua = "X" * 10_000
    await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": long_ua},
    )
    assert lr.status_code == 200
    jwt = lr.json()["access_token"]
    r = await client.get(
        "/api/auth/sessions", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200
    row = r.json()[0]
    assert row["user_agent"] == "X" * 512


# ===========================================================================
# #19 Compare N>4 — edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_compare_one_over_cap_message_mentions_32(client):
    """33 batch ids → 400 with a message mentioning the cap number."""
    ids = ",".join(f"b-{i}" for i in range(MAX_COMPARE_BATCHES + 1))
    r = await client.get(f"/api/compare?batches={ids}")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "32" in detail, f"expected cap number in error, got: {detail!r}"


@pytest.mark.asyncio
async def test_compare_exact_cap_returns_all_32(client):
    """Exactly 32 batches → 200 with 32 columns."""
    from backend.utils.ratelimit import reset_default_bucket_for_tests

    ids = [f"b-{i}" for i in range(MAX_COMPARE_BATCHES)]
    for n, bid in enumerate(ids):
        if n and n % 10 == 0:
            reset_default_bucket_for_tests()
        await seed_completed_batch(client, batch_id=bid)
    r = await client.get("/api/compare?batches=" + ",".join(ids))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == MAX_COMPARE_BATCHES


@pytest.mark.asyncio
async def test_compare_empty_query_rejects(client):
    """batches= (empty) → validation error."""
    r = await client.get("/api/compare?batches=")
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_compare_single_id_rejects(client):
    """1 batch → 400 'too few'."""
    await seed_completed_batch(client, batch_id="only-one")
    r = await client.get("/api/compare?batches=only-one")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_compare_duplicate_ids_deduped(client):
    """Passing the same id twice → deduped before the min-count check.

    The parser collapses duplicates preserving order. That means
    ``batches=b-1,b-1`` has effective length 1 → 400 ("too few"). Lock
    this in so a future rewrite doesn't silently accept duplicates.
    """
    await seed_completed_batch(client, batch_id="b-dup")
    r = await client.get("/api/compare?batches=b-dup,b-dup")
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_compare_duplicate_then_unique_dedupes(client):
    """``b-1,b-2,b-1`` → 2 columns, not 3."""
    await seed_completed_batch(client, batch_id="b-1")
    await seed_completed_batch(client, batch_id="b-2")
    r = await client.get("/api/compare?batches=b-1,b-2,b-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == 2
    assert [c["batch_id"] for c in body["batches"]] == ["b-1", "b-2"]


@pytest.mark.asyncio
async def test_compare_mixed_projects_resolves(client):
    """Batches spanning multiple projects in the same compare call work."""
    await seed_completed_batch(client, batch_id="p1-b", project="p1")
    await seed_completed_batch(client, batch_id="p2-b", project="p2")
    r = await client.get("/api/compare?batches=p1-b,p2-b")
    assert r.status_code == 200, r.text
    body = r.json()
    projects = {c["project"] for c in body["batches"]}
    assert projects == {"p1", "p2"}


@pytest.mark.asyncio
async def test_compare_unknown_id_returns_404(client):
    """Visibility layer: an id the caller can't see → 404 (not 403)."""
    await seed_completed_batch(client, batch_id="real-b")
    r = await client.get("/api/compare?batches=real-b,ghost-b")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_compare_32_batches_perf(client):
    """32 batches × 1 job each: response < 3s on test DB.

    Conservative bound; we're not stress-testing, just catching a
    quadratic-blow-up regression.
    """
    from backend.utils.ratelimit import reset_default_bucket_for_tests

    ids = [f"perf-{i}" for i in range(MAX_COMPARE_BATCHES)]
    for n, bid in enumerate(ids):
        if n and n % 10 == 0:
            reset_default_bucket_for_tests()
        await seed_completed_batch(client, batch_id=bid)

    t0 = time.time()
    r = await client.get("/api/compare?batches=" + ",".join(ids))
    elapsed = time.time() - t0
    assert r.status_code == 200
    assert elapsed < 3.0, f"compare with 32 batches took {elapsed:.2f}s"
