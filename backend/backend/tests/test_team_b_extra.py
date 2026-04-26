"""Team B (Observability) — QA edge-case coverage.

Independent verification of the four BE items that landed on
``feat/team-b-observability``:

  * #11  GET /api/stats/gpu-hours-by-user
  * #21  JobOut extras (avg_batch_time_ms / gpu_memory_peak_mb / n_params)
  * #18  env_snapshot.git_sha_short + git_remote_url
  * #30  GET /api/meta/hints

These tests live outside the original BE test files so they can be
reviewed / reverted in isolation if any turn out to encode an
assumption the BE engineer disagrees with. They focus on edge cases
the original tests didn't cover:

  * Validation + SQL-injection surface on #11
  * String / bool / null coercion on #21
  * Weird git_remote shapes on #18 (svn://, empty git_sha, extra .git)
  * Cross-locale consistency + auth surface on #30
  * Cross-endpoint shape parity (GET /api/jobs/* vs
    GET /api/batches/{id}/jobs)
  * Rough perf sanity on #11 (1000-job fixture < 500 ms)
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Seed helpers (scoped to this file; mirror the patterns used in
# test_gpu_hours_by_user.py / test_job_detail_extras.py).
# ---------------------------------------------------------------------------


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _tester_id(session) -> int:
    from backend.models import User
    tester = (
        await session.execute(select(User).where(User.username == "tester"))
    ).scalar_one()
    return tester.id


async def _seed_batch_and_job(
    session,
    batch_id: str,
    job_id: str,
    owner_id: int,
    *,
    elapsed_s: int = 3600,
    end_time: datetime | None = None,
    metrics: dict | str | None = None,
) -> None:
    """Create a Batch + Job pair. ``metrics`` may be a dict (will be
    JSON-encoded), a raw string (inserted verbatim — useful for
    malformed JSON scenarios), or ``None``."""
    from backend.models import Batch, Job

    if end_time is None:
        end_time = datetime.now(timezone.utc) - timedelta(hours=1)
    start_time = end_time - timedelta(seconds=elapsed_s)

    existing = await session.get(Batch, batch_id)
    if existing is None:
        session.add(Batch(
            id=batch_id, project="p", owner_id=owner_id, status="done",
            start_time=_iso(start_time), end_time=_iso(end_time),
            n_done=1, n_failed=0,
        ))
    if isinstance(metrics, dict):
        metrics_str: str | None = json.dumps(metrics)
    elif isinstance(metrics, str):
        metrics_str = metrics
    else:
        metrics_str = None
    session.add(Job(
        id=job_id, batch_id=batch_id, model="transformer", dataset="etth1",
        status="done",
        start_time=_iso(start_time), end_time=_iso(end_time),
        elapsed_s=elapsed_s, metrics=metrics_str,
    ))
    await session.commit()


def _env_event(batch_id: str, data: dict) -> dict:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "env_snapshot",
        "timestamp": "2026-04-24T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p", "host": "h", "user": "u"},
        "data": data,
    }


def _batch_start(batch_id: str, n_total: int = 1) -> dict:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "batch_start",
        "timestamp": "2026-04-24T09:55:00Z",
        "batch_id": batch_id,
        "source": {"project": "p", "host": "h", "user": "u"},
        "data": {"n_total_jobs": n_total},
    }


# ===========================================================================
# #11 — GET /api/stats/gpu-hours-by-user edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_gpu_hours_days_zero_is_422(client):
    """``days=0`` → 422 (pydantic ge=1)."""
    r = await client.get("/api/stats/gpu-hours-by-user?days=0")
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_gpu_hours_days_negative_is_422(client):
    """Negative lookback windows are not allowed."""
    r = await client.get("/api/stats/gpu-hours-by-user?days=-3")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_gpu_hours_days_over_cap_is_422(client):
    """Cap is 365; 400 should 422, not silently clamp."""
    r = await client.get("/api/stats/gpu-hours-by-user?days=400")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_gpu_hours_days_non_integer_is_422(client):
    """Non-int (string) days is rejected by pydantic before any SQL runs."""
    r = await client.get("/api/stats/gpu-hours-by-user?days=abc")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_gpu_hours_sql_injection_attempt_is_422(client):
    """Classic injection payload in ``days`` — rejected by validation,
    so it never reaches the SQL layer."""
    r = await client.get(
        "/api/stats/gpu-hours-by-user?days=1;DROP TABLE users;--"
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_gpu_hours_gpu_count_string_is_coerced_to_one(client):
    """``metrics['gpu_count'] = '2'`` (string) is NOT accepted — the
    extractor requires int/float. We treat string as "unspecified" and
    fall back to gpu_count=1 rather than crashing.
    """
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-gh-str", "j-gh-str", owner_id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=1),
            metrics={"gpu_count": "2"},  # string instead of int
        )
    r = await client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 200, r.text
    rows = r.json()
    # 3600s * gpu_count=1 (string rejected) / 3600 = 1.0 hour
    assert rows[0]["gpu_hours"] == pytest.approx(1.0, rel=1e-3), (
        f"Expected string gpu_count to fall back to 1, got {rows[0]}"
    )


@pytest.mark.asyncio
async def test_gpu_hours_malformed_metrics_json_does_not_crash(client):
    """A job whose metrics column holds non-JSON garbage still counts
    (with gpu_count=1). Should not 500."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-gh-badjson", "j-gh-badjson", owner_id,
            elapsed_s=7200,
            end_time=datetime.now(timezone.utc) - timedelta(hours=2),
            metrics="not{valid}json",  # raw string; won't parse
        )
    r = await client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows[0]["gpu_hours"] == pytest.approx(2.0, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_alias_keys_are_recognised(client):
    """``GPU_Count`` / ``n_gpus`` aliases should be treated the same
    as ``gpu_count`` (contract per docstring)."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-gh-alias1", "j-a1", owner_id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=1),
            metrics={"GPU_Count": 4},
        )
        await _seed_batch_and_job(
            session, "b-gh-alias2", "j-a2", owner_id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=2),
            metrics={"n_gpus": 2},
        )
    r = await client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    # 1h*4 + 1h*2 = 6 gpu-hours
    assert rows[0]["gpu_hours"] == pytest.approx(6.0, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_non_admin_security_isolation(client):
    """Two non-admin users: each should only see self, not the other."""
    # Register two extra users (tester is admin; we want both sides
    # non-admin so the visibility branch is what's exercised).
    for uname in ("alice", "carol"):
        await client.post("/api/auth/register", json={
            "username": uname, "email": f"{uname}@ex.com",
            "password": "password123",
        })

    async def _token(uname: str) -> str:
        login = await client.post("/api/auth/login", json={
            "username_or_email": uname, "password": "password123",
        })
        jwt = login.json()["access_token"]
        t = await client.post("/api/tokens",
            json={"name": f"{uname}-rep", "scope": "reporter"},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        return t.json()["token"]

    alice_tok = await _token("alice")
    carol_tok = await _token("carol")

    import backend.db as db_mod
    from backend.models import User
    async with db_mod.SessionLocal() as session:
        alice = (await session.execute(
            select(User).where(User.username == "alice")
        )).scalar_one()
        carol = (await session.execute(
            select(User).where(User.username == "carol")
        )).scalar_one()
        await _seed_batch_and_job(
            session, "b-sec-alice", "j-sec-a", alice.id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=1),
            metrics={"gpu_count": 1},
        )
        await _seed_batch_and_job(
            session, "b-sec-carol", "j-sec-c", carol.id,
            elapsed_s=1800,
            end_time=datetime.now(timezone.utc) - timedelta(hours=2),
            metrics={"gpu_count": 1},
        )

    # Alice sees only alice
    r_a = await client.get(
        "/api/stats/gpu-hours-by-user",
        headers={"Authorization": f"Bearer {alice_tok}"},
    )
    assert r_a.status_code == 200
    rows_a = r_a.json()
    assert len(rows_a) == 1 and rows_a[0]["username"] == "alice"

    # Carol sees only carol — critically NOT alice's 1.0 gpu_hours
    r_c = await client.get(
        "/api/stats/gpu-hours-by-user",
        headers={"Authorization": f"Bearer {carol_tok}"},
    )
    assert r_c.status_code == 200
    rows_c = r_c.json()
    assert len(rows_c) == 1 and rows_c[0]["username"] == "carol"
    assert rows_c[0]["gpu_hours"] == pytest.approx(0.5, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_perf_under_1000_jobs(client):
    """1000 jobs / 50 batches: endpoint should respond in well under 500 ms."""
    import backend.db as db_mod
    from backend.models import Batch, Job

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        now = datetime.now(timezone.utc)
        # 50 batches × 20 jobs = 1000 jobs
        for b in range(50):
            session.add(Batch(
                id=f"perf-b-{b}", project="p", owner_id=owner_id,
                status="done",
                start_time=_iso(now - timedelta(hours=b + 2)),
                end_time=_iso(now - timedelta(hours=b + 1)),
                n_done=20, n_failed=0,
            ))
            for j in range(20):
                session.add(Job(
                    id=f"perf-j-{b}-{j}", batch_id=f"perf-b-{b}",
                    model="transformer", dataset="etth1", status="done",
                    start_time=_iso(now - timedelta(hours=b + 2)),
                    end_time=_iso(now - timedelta(hours=b + 1)),
                    elapsed_s=600,
                    metrics=json.dumps({"gpu_count": 1}),
                ))
        await session.commit()

    t0 = time.perf_counter()
    r = await client.get("/api/stats/gpu-hours-by-user?days=365")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["job_count"] == 1000
    # 1000 × 600s × 1 GPU / 3600 = ~166.67 hours
    assert rows[0]["gpu_hours"] == pytest.approx(166.667, rel=1e-3)
    # Generous ceiling — in-memory SQLite plus in-Python bucketing
    # should be well inside 500 ms; flag if a future refactor reintroduces
    # an N+1 or regresses the join.
    assert elapsed_ms < 500.0, (
        f"gpu-hours-by-user took {elapsed_ms:.1f} ms for 1000 jobs"
    )


# ===========================================================================
# #21 — JobOut extras edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_job_extras_shape_matches_across_endpoints(client):
    """The single-job endpoint and the batch-scoped list must return
    identical field sets + values for the same job row."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-x-parity", "j-parity", owner_id,
            metrics={"Avg_Batch_Time": 0.25, "GPU_Memory": 4096.0,
                     "n_params": 100},
        )

    r_single = await client.get("/api/jobs/b-x-parity/j-parity")
    r_list = await client.get("/api/batches/b-x-parity/jobs")
    assert r_single.status_code == 200
    assert r_list.status_code == 200
    row_from_list = next(j for j in r_list.json() if j["id"] == "j-parity")
    # All three extras must match byte-for-byte between endpoints
    for key in ("avg_batch_time_ms", "gpu_memory_peak_mb", "n_params"):
        assert r_single.json()[key] == row_from_list[key], (
            f"{key} differs: {r_single.json()[key]!r} vs {row_from_list[key]!r}"
        )
    # And the full shape must include our three fields
    for key in ("avg_batch_time_ms", "gpu_memory_peak_mb", "n_params"):
        assert key in r_single.json()
        assert key in row_from_list


@pytest.mark.asyncio
async def test_job_extras_bool_not_coerced_to_int(client):
    """Python's ``bool`` is a subclass of ``int`` — the extractor should
    explicitly reject it for n_params (``True`` → None, not 1)."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-x-bool", "j-bool", owner_id,
            metrics={"n_params": True},
        )
    r = await client.get("/api/jobs/b-x-bool/j-bool")
    assert r.status_code == 200
    assert r.json()["n_params"] is None, (
        "bool(True) must not be silently coerced to int(1) for n_params"
    )


@pytest.mark.asyncio
async def test_job_extras_avg_batch_time_zero_is_zero_not_none(client):
    """``Avg_Batch_Time=0`` is a legitimate (if suspicious) value —
    must survive the coercion as ``0.0``, not collapse to None."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-x-zero", "j-zero", owner_id,
            metrics={"Avg_Batch_Time": 0, "GPU_Memory": 0, "n_params": 0},
        )
    r = await client.get("/api/jobs/b-x-zero/j-zero")
    assert r.status_code == 200
    body = r.json()
    assert body["avg_batch_time_ms"] == 0.0
    assert body["gpu_memory_peak_mb"] == 0.0
    assert body["n_params"] == 0


@pytest.mark.asyncio
async def test_job_extras_malformed_metrics_json_yields_nulls(client):
    """If Job.metrics holds a string that fails json.loads, all three
    extras come back as None and the response still serialises."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-x-bad", "j-bad", owner_id,
            metrics="this is not JSON",
        )
    r = await client.get("/api/jobs/b-x-bad/j-bad")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None
    assert body["avg_batch_time_ms"] is None
    assert body["gpu_memory_peak_mb"] is None
    assert body["n_params"] is None


@pytest.mark.asyncio
async def test_job_extras_non_dict_metrics_is_safe(client):
    """Metrics JSON that parses to a list rather than a dict should
    still serialise cleanly with all extras null.

    Regression guard for a 500 that surfaced when ``Job.metrics`` contained
    a top-level JSON array/scalar (not the expected object). Fixed by
    coercing non-dict ``json.loads`` results to ``None`` in ``_job_to_out``
    before constructing :class:`JobOut`.
    """
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-x-list", "j-list", owner_id,
            metrics="[1, 2, 3]",
        )
    r = await client.get("/api/jobs/b-x-list/j-list")
    # Expected (once fixed): 200 with metrics=None and extras=None.
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None
    assert body["avg_batch_time_ms"] is None
    assert body["gpu_memory_peak_mb"] is None
    assert body["n_params"] is None


# ===========================================================================
# #18 — git_sha_short / git_remote_url edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_git_remote_https_dotgit_stripped(client):
    """Trailing ``.git`` is stripped regardless of SSH vs HTTPS origin."""
    batch_id = f"b-git-dotgit-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "a1b2c3d4e5f6",
        "git_remote": "https://github.com/foo/bar.git",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["env_snapshot"]["git_remote_url"] == (
        "https://github.com/foo/bar"
    )


@pytest.mark.asyncio
async def test_git_remote_ssh_alt_form(client):
    """``git@github.com:foo/bar.git`` (no leading slash) is the standard
    clone URL — make sure it doesn't break."""
    batch_id = f"b-git-ssh-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "1122334455",
        "git_remote": "git@github.com:foo/bar.git",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["env_snapshot"]["git_remote_url"] == (
        "https://github.com/foo/bar"
    )


@pytest.mark.asyncio
async def test_git_remote_unrecognised_scheme_does_not_crash(client):
    """``svn://host/repo`` is not a Git remote we recognise — the
    endpoint must return a 200 (with some value, not a 500)."""
    batch_id = f"b-git-svn-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "deadbeef12345678",
        "git_remote": "svn://example.com/trunk",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200, r.text
    snap = r.json()["env_snapshot"]
    # The implementation's contract is "return as-is" for unknown schemes
    # (per docstring). Either behaviour — as-is or None — is acceptable
    # as long as it doesn't crash; we just assert it's *some* string or
    # None.
    assert snap["git_remote_url"] is None or isinstance(
        snap["git_remote_url"], str
    )
    assert snap["git_sha_short"] == "deadbeef"


@pytest.mark.asyncio
async def test_git_sha_empty_string_is_none_not_empty(client):
    """``git_sha: ""`` → ``git_sha_short: null`` (not ``""``).
    Frontend conditional renders on ``snap.git_sha_short`` — an empty
    string would incorrectly render an empty chip."""
    batch_id = f"b-git-empty-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "",
        "git_remote": "https://github.com/foo/bar",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["env_snapshot"]["git_sha_short"] is None


@pytest.mark.asyncio
async def test_git_sha_whitespace_only_is_none(client):
    """``git_sha: "   "`` (padding from the reporter) → null, not ``"   "[:8]``."""
    batch_id = f"b-git-ws-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "   ",
        "git_remote": "https://github.com/foo/bar",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["env_snapshot"]["git_sha_short"] is None


@pytest.mark.asyncio
async def test_git_remote_short_sha_still_truncated_at_8(client):
    """A git_sha shorter than 8 chars should be passed through, not padded."""
    batch_id = f"b-git-short-{uuid.uuid4().hex[:6]}"
    await client.post("/api/events", json=_batch_start(batch_id))
    await client.post("/api/events", json=_env_event(batch_id, {
        "git_sha": "abc",
        "git_remote": "https://github.com/foo/bar",
    }))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["env_snapshot"]["git_sha_short"] == "abc"


# ===========================================================================
# #30 — /api/meta/hints edge cases
# ===========================================================================


# The deployed catalog includes these 11 keys — all must be present
# in every locale, with non-empty string values.
_ALL_HINT_KEYS = {
    "empty_hosts",
    "empty_batches",
    "empty_jobs",
    "empty_projects",
    "empty_notifications",
    "empty_pins",
    "empty_shared",
    "empty_stars",
    "empty_search",
    "empty_events",
    "empty_artifacts",
}


@pytest.mark.asyncio
async def test_meta_hints_all_11_keys_en(unauthed_client):
    """The full 11-key catalog is present in en-US with non-empty values."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200
    hints = r.json()["hints"]
    assert set(hints.keys()) == _ALL_HINT_KEYS, (
        f"Missing keys: {_ALL_HINT_KEYS - set(hints.keys())}; "
        f"Extras: {set(hints.keys()) - _ALL_HINT_KEYS}"
    )
    for k, v in hints.items():
        assert isinstance(v, str) and v.strip(), (
            f"Key {k!r} has empty value {v!r}"
        )


@pytest.mark.asyncio
async def test_meta_hints_all_11_keys_zh(unauthed_client):
    """Same 11 keys in zh-CN, all non-empty."""
    r = await unauthed_client.get(
        "/api/meta/hints", headers={"Accept-Language": "zh-CN"},
    )
    assert r.status_code == 200
    hints = r.json()["hints"]
    assert set(hints.keys()) == _ALL_HINT_KEYS
    for k, v in hints.items():
        assert isinstance(v, str) and v.strip(), (
            f"Key {k!r} has empty value in zh-CN: {v!r}"
        )


@pytest.mark.asyncio
async def test_meta_hints_complex_accept_language_quality_values(unauthed_client):
    """``Accept-Language: zh-CN,en-US;q=0.9`` → zh-CN (highest-q wins)."""
    r = await unauthed_client.get(
        "/api/meta/hints",
        headers={"Accept-Language": "zh-CN,en-US;q=0.9"},
    )
    assert r.status_code == 200
    assert r.json()["locale"] == "zh-CN"


@pytest.mark.asyncio
async def test_meta_hints_french_falls_back_to_en(unauthed_client):
    """Unsupported locale fr-FR → en-US catalog (no 406, no 500)."""
    r = await unauthed_client.get(
        "/api/meta/hints", headers={"Accept-Language": "fr-FR"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "en-US"
    # Sanity: the content is the English catalog (look for an English
    # anchor phrase that never appears in zh-CN). The English text
    # contains the em-dash ``—`` which is non-ASCII, so we can't
    # ASCII-test the whole string — just assert the anchor.
    assert "No batches" in body["hints"]["empty_batches"], (
        "French fallback did not return the English catalog"
    )


@pytest.mark.asyncio
async def test_meta_hints_no_header_is_en(unauthed_client):
    """Absent Accept-Language header → en-US default."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200
    assert r.json()["locale"] == "en-US"


@pytest.mark.asyncio
async def test_meta_hints_is_public_no_auth_required(unauthed_client):
    """Meta hints must be reachable without any credential — it's
    bootstrap copy for the empty-state UI."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200, (
        "meta/hints should be public; got {r.status_code}"
    )


@pytest.mark.asyncio
async def test_meta_hints_en_and_zh_have_identical_key_sets(unauthed_client):
    """Per spec: both locales ship the same key set so the frontend
    never sees a missing key."""
    r_en = await unauthed_client.get("/api/meta/hints")
    r_zh = await unauthed_client.get(
        "/api/meta/hints", headers={"Accept-Language": "zh-CN"},
    )
    en_keys = set(r_en.json()["hints"])
    zh_keys = set(r_zh.json()["hints"])
    assert en_keys == zh_keys, (
        f"en-only: {en_keys - zh_keys}; zh-only: {zh_keys - en_keys}"
    )


@pytest.mark.asyncio
async def test_meta_hints_response_has_no_extra_fields(unauthed_client):
    """MetaHintsOut uses extra='forbid'; response must be exactly
    ``{locale, hints}``."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"locale", "hints"}


# ===========================================================================
# Backward-compat sanity: existing callers of JobOut still work
# ===========================================================================


@pytest.mark.asyncio
async def test_joboutm_backward_compat_legacy_fields_all_present(client):
    """All pre-#21 fields still present + populated on the response so
    any existing deserialiser that ignores unknown-but-optional new
    fields keeps working."""
    import backend.db as db_mod
    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_batch_and_job(
            session, "b-compat", "j-compat", owner_id,
            metrics={"MSE": 0.1},
        )
    r = await client.get("/api/jobs/b-compat/j-compat")
    assert r.status_code == 200
    body = r.json()
    # Original JobOut fields still present with sensible values
    for k in ("id", "batch_id", "model", "dataset", "status", "start_time",
              "end_time", "elapsed_s", "metrics"):
        assert k in body, f"Legacy field {k!r} missing from JobOut response"
    assert body["id"] == "j-compat"
    assert body["batch_id"] == "b-compat"
    assert body["metrics"] == {"MSE": 0.1}
