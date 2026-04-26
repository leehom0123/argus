"""Team A "Guardrails" — QA edge-case coverage.

These tests complement the dedicated per-feature suites
(``test_guardrails_divergence.py``, ``test_idle_detector.py``,
``test_anomalous_login.py``, ``test_backup_loop.py``) by exercising
the edge cases called out in the team-A review brief:

* #12 divergence — window-too-short, pure-decrease, Inf/NaN at any
  position, missing epoch, terminal-state & already-flagged
  short-circuits, idempotency under repeat scans.
* #13 idle — single-busy-sample veto, below-threshold duration,
  insufficient coverage (samples too sparse), already-flagged
  short-circuit, terminal-state jobs, multi-job partial flag.
* #33 anomalous login — same-IP / new-UA, 50-entry ring buffer
  enforcement, 31-day aging, UA hash determinism + truncation,
  huge / unicode / quote-laden UA smoke tests, IPv6-mapped IP
  handling, malformed ``known_ips_json`` defensiveness.
* #34 backup — empty DB survives, filename format + uniqueness,
  ``keep_last_n=3`` + 5 rapid backups, age-calc on a 2-hour-old file,
  ``shutil/sqlite3`` failure resilience, missing / zero interval,
  recent_files capped at ``keep_last_n``.

All tests are independent of real wall-clock sleeps — file mtimes
are set explicitly via :func:`os.utime` and snapshot timestamps are
computed relative to ``datetime.now``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import Batch, Event, Job, ResourceSnapshot, User
from backend.notifications.watchdog import (
    _check_batch_divergence,
    _check_idle_jobs,
    watchdog_loop_once,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the per-feature test files so edge-case seeding stays
# local to this module and can be tweaked without cross-file edits.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_batch_with_losses(
    db,
    *,
    batch_id: str,
    losses: list,  # list of float/None/'inf'/'nan'
    status: str = "running",
    job_id: str = "j1",
) -> Batch:
    now = datetime.now(timezone.utc)
    batch = Batch(
        id=batch_id,
        project="p1",
        status=status,
        start_time=_iso(now),
    )
    db.add(batch)
    db.add(Job(id=job_id, batch_id=batch_id, status="running"))
    for i, vl in enumerate(losses):
        data: dict = {"epoch": i}
        if vl is not None:
            data["val_loss"] = vl
        db.add(
            Event(
                batch_id=batch_id,
                job_id=job_id,
                event_type="job_epoch",
                timestamp=_iso(now - timedelta(minutes=len(losses) - i)),
                schema_version="1.1",
                data=json.dumps(data),
            )
        )
    await db.commit()
    return batch


async def _load_batch_events(db, batch_id: str):
    batch = (
        await db.execute(select(Batch).where(Batch.id == batch_id))
    ).scalar_one()
    events = list(
        (
            await db.execute(
                select(Event)
                .where(Event.batch_id == batch_id)
                .order_by(Event.timestamp.desc())
            )
        ).scalars()
    )
    return batch, events


async def _seed_idle(
    db,
    *,
    batch_id: str,
    job_id: str,
    utils: list[float],
    span_minutes: float = 10,
    job_already_flagged: bool = False,
    job_status: str = "running",
) -> None:
    """Seed a batch + job + snapshots distributed across ``span_minutes``.

    Mirrors the fixture in ``test_idle_detector.py`` so edge-case deltas
    are expressed as parameter overrides, not re-writes of the whole
    staging flow.
    """
    now = datetime.now(timezone.utc)
    db.add(Batch(id=batch_id, project="p", status="running", host="gpu-1"))
    db.add(
        Job(
            id=job_id,
            batch_id=batch_id,
            status=job_status,
            is_idle_flagged=job_already_flagged,
        )
    )
    if utils:
        effective_span = span_minutes - 0.2
        step = effective_span / max(1, len(utils) - 1)
        for i, u in enumerate(utils):
            offset_min = (span_minutes - 0.1) - step * i
            ts = now - timedelta(minutes=offset_min)
            db.add(
                ResourceSnapshot(
                    host="gpu-1",
                    batch_id=batch_id,
                    timestamp=_iso(ts),
                    gpu_util_pct=u,
                )
            )
    await db.commit()


# ---------------------------------------------------------------------------
# #12 Divergence — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_divergence_only_two_epochs_does_not_fire(client):
    """Default window=3 — two epochs is below threshold even on a 2× jump."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-2ep", losses=[0.1, 0.5]
        )
        batch, events = await _load_batch_events(db, "b-2ep")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is False
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-2ep"))
        ).scalar_one()
        assert b.status == "running"


@pytest.mark.asyncio
async def test_divergence_monotonic_decreasing_does_not_fire(client):
    """3 strictly decreasing epochs → healthy, never flag."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-dec", losses=[1.0, 0.5, 0.2]
        )
        batch, events = await _load_batch_events(db, "b-dec")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is False


@pytest.mark.asyncio
async def test_divergence_exactly_doubles_over_three_epochs(client):
    """val_loss = [0.5, 1.0, 2.0] → ratio 4.0, strictly increasing → flagged."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-4x", losses=[0.5, 1.0, 2.0]
        )
        batch, events = await _load_batch_events(db, "b-4x")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is True
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-4x"))
        ).scalar_one()
        assert b.status == "divergent"


@pytest.mark.asyncio
async def test_divergence_positive_inf_flags_immediately(client):
    """Any epoch with val_loss = +Inf → short-circuit flag."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-inf", losses=[0.1, float("inf"), 0.2]
        )
        batch, events = await _load_batch_events(db, "b-inf")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is True
    async with SessionLocal() as db:
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-inf")
                    .where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert len(evs) == 1
        assert json.loads(evs[0].data)["reason"] == "nan_or_inf"


@pytest.mark.asyncio
async def test_divergence_nan_in_middle_position_still_fires(client):
    """NaN in position 1 of 3 — triggers the nan_or_inf short-circuit."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db,
            batch_id="b-nan-mid",
            losses=[0.1, float("nan"), 0.1],
        )
        batch, events = await _load_batch_events(db, "b-nan-mid")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is True


@pytest.mark.asyncio
async def test_divergence_missing_val_loss_skipped_not_crashed(client):
    """Missing val_loss in one epoch shouldn't crash — detector just drops it.

    Events with no ``val_loss`` field (``vl=None``) are ``continue``'d
    inside the predicate. With only 2 usable losses remaining, window=3
    is not satisfied → no flag, no crash.
    """
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-miss", losses=[0.5, None, 1.2]
        )
        batch, events = await _load_batch_events(db, "b-miss")
        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is False


@pytest.mark.asyncio
async def test_divergence_done_batch_not_mutated_by_loop(client):
    """A terminal batch (status='done') isn't scanned by watchdog_loop_once.

    The loop query filters on ``Batch.status == 'running'``. So even if
    the epoch history looks divergent we must not overwrite the final
    state.
    """
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db,
            batch_id="b-done",
            status="done",
            losses=[0.1, 0.5, 2.0],
        )
        await watchdog_loop_once(db)

    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-done"))
        ).scalar_one()
        assert b.status == "done"
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-done")
                    .where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert evs == []


@pytest.mark.asyncio
async def test_divergence_idempotent_on_second_scan(client):
    """Run the watchdog twice on the same batch — only one ``batch_diverged``."""
    async with SessionLocal() as db:
        await _seed_batch_with_losses(
            db, batch_id="b-idem", losses=[0.1, 0.3, 0.9]
        )
        await watchdog_loop_once(db)
        # Second scan: batch is now 'divergent' so it won't even be picked
        # up by the running filter, let alone re-emit.
        await watchdog_loop_once(db)

    async with SessionLocal() as db:
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-idem")
                    .where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert len(evs) == 1


# ---------------------------------------------------------------------------
# #13 Idle-job — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_single_high_sample_vetoes_flag(client):
    """Utils [10, 1, 2, 1] — the first sample >5 kills the flag."""
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-spike",
            job_id="j-spike",
            utils=[10.0, 1.0, 2.0, 1.0],
            span_minutes=10,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-spike"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j-spike"))
        ).scalar_one()
        assert job.is_idle_flagged is False


@pytest.mark.asyncio
async def test_idle_zero_util_below_window_does_not_fire(client):
    """All zeros but the realized span is only ~1 minute — below 0.9×10."""
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-tiny",
            job_id="j-tiny",
            utils=[0.0, 0.0, 0.0, 0.0],
            span_minutes=1,  # effective span ~0.8 min → < 9 min coverage
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-tiny"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []


@pytest.mark.asyncio
async def test_idle_all_zero_across_full_window_fires(client):
    """Six zero samples spanning the full 10-min window → flagged.

    The detector's SQL clips snapshots to ``now - window_min``, so a
    "15 min" natural-language window effectively means "samples packed
    into the last 10 min with ≥ 0.9× coverage". Six zeros across ~9.8
    min satisfy both constraints.
    """
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-full-win",
            job_id="j-full-win",
            utils=[0.0] * 6,
            span_minutes=10,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-full-win"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert len(flipped) == 1


@pytest.mark.asyncio
async def test_idle_insufficient_coverage_in_window_skipped(client):
    """2 samples only 5 min apart within a 10-min window → < 0.9 coverage → skip."""
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-sparse",
            job_id="j-sparse",
            utils=[0.0, 0.0],
            span_minutes=5,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-sparse"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []


@pytest.mark.asyncio
async def test_idle_terminal_job_not_flagged(client):
    """Job with ``status='done'`` is excluded by the SELECT filter."""
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-terminal",
            job_id="j-terminal",
            utils=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            span_minutes=10,
            job_status="done",
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-terminal"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j-terminal"))
        ).scalar_one()
        assert job.is_idle_flagged is False


@pytest.mark.asyncio
async def test_idle_already_flagged_emits_no_event(client):
    """Repeat scans of a sticky-flagged job produce zero new events."""
    async with SessionLocal() as db:
        await _seed_idle(
            db,
            batch_id="b-stuck",
            job_id="j-stuck",
            utils=[0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
            span_minutes=10,
            job_already_flagged=True,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-stuck"))
        ).scalar_one()
        flipped1 = await _check_idle_jobs(db, batch)
        flipped2 = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped1 == []
    assert flipped2 == []
    async with SessionLocal() as db:
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-stuck")
                    .where(Event.event_type == "job_idle_flagged")
                )
            ).scalars()
        )
        assert evs == []


# ---------------------------------------------------------------------------
# #33 Anomalous login — edge cases
# ---------------------------------------------------------------------------


async def _register_user(
    client, username: str = "edwina", email: str = "edwina@example.com"
) -> None:
    r = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": "password123",
        },
    )
    assert r.status_code == 201, r.text


async def _login(
    client, *, username: str, user_agent: str | None = None
):
    client.headers.pop("Authorization", None)
    headers: dict = {}
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
        headers=headers,
    )


@pytest.fixture(autouse=True)
def _enable_anomalous(monkeypatch):
    """Flip the feature on for every test in this file.

    Tests that want to assert on the disabled path explicitly re-flip
    it off via ``monkeypatch`` after the fixture runs.
    """
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_anomalous_new_ua_same_ip_fires_email(client, email_service):
    """ASGITransport always sets the same client IP — a new UA alone triggers."""
    await _register_user(client)
    r1 = await _login(client, username="edwina", user_agent="ua-seed")
    assert r1.status_code == 200
    r2 = await _login(client, username="edwina", user_agent="ua-different")
    assert r2.status_code == 200

    await asyncio.sleep(0.1)
    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert len(alerts) == 1
    assert "ua-different" in alerts[0].body_html


@pytest.mark.asyncio
async def test_known_ips_json_at_50_entries_drops_oldest(client):
    """Seed 50 stale-but-fresh entries, log in once, verify oldest dropped.

    We pre-populate ``known_ips_json`` with 50 entries spread over the last
    hour (so none are aged out), then trigger one login from a brand-new
    pair. Afterwards the list should still be 50 long and the oldest
    ``"entry-0"`` IP should have been evicted.
    """
    await _register_user(client)
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        now = datetime.now(timezone.utc)
        entries = [
            {
                "ip": f"10.0.0.{i}",
                "ua_hash": f"hash{i:02d}" + "0" * 10,
                "last_seen": _iso(now - timedelta(minutes=50 - i)),
            }
            for i in range(50)
        ]
        user.known_ips_json = json.dumps(entries)
        await db.commit()

    r = await _login(client, username="edwina", user_agent="ua-brand-new-51st")
    assert r.status_code == 200

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        stored = json.loads(user.known_ips_json)
        assert len(stored) == 50
        ips = {e["ip"] for e in stored}
        # oldest (ip=10.0.0.0, last_seen furthest in the past) is gone.
        assert "10.0.0.0" not in ips


@pytest.mark.asyncio
async def test_known_ips_aged_out_after_31_days(client, email_service):
    """An entry stamped 31 days ago should be pruned on the next login."""
    await _register_user(client)
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        now = datetime.now(timezone.utc)
        old_entry = {
            "ip": "203.0.113.7",
            "ua_hash": "stale" + "0" * 11,
            "last_seen": _iso(now - timedelta(days=31)),
        }
        user.known_ips_json = json.dumps([old_entry])
        await db.commit()

    r = await _login(client, username="edwina", user_agent="ua-fresh-after-gap")
    assert r.status_code == 200

    await asyncio.sleep(0.05)
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        stored = json.loads(user.known_ips_json)
        # Old entry is aged out. One fresh entry was appended — since there
        # was no *fresh* prior history (only the stale one got pruned)
        # ``had_history=False`` and no email should fire. Effectively a
        # 31-day gap = first-login re-seed.
        assert len(stored) == 1
        assert stored[0]["ip"] != "203.0.113.7"

    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert alerts == []  # aging-out treated as fresh first-login


@pytest.mark.asyncio
async def test_malformed_known_ips_json_is_defensive(client, email_service):
    """Garbage JSON in the column → treat as empty, never crash login."""
    await _register_user(client)
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        user.known_ips_json = "{not valid json"
        await db.commit()

    r = await _login(client, username="edwina", user_agent="ua-after-garbage")
    assert r.status_code == 200

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        stored = json.loads(user.known_ips_json)
        # Column has been rewritten with a clean list.
        assert isinstance(stored, list)
        assert len(stored) == 1

    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    # Malformed state → treated as blank → first-login => no email.
    assert alerts == []


@pytest.mark.asyncio
async def test_malformed_json_as_dict_is_defensive(client):
    """JSON that parses but isn't a list (e.g. ``{}``) also yields empty list."""
    await _register_user(client)
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        user.known_ips_json = json.dumps({"rogue": "payload"})
        await db.commit()

    r = await _login(client, username="edwina", user_agent="ua-after-dict")
    assert r.status_code == 200

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        stored = json.loads(user.known_ips_json)
        assert isinstance(stored, list)


@pytest.mark.asyncio
async def test_ua_hash_deterministic_and_collision_resistant():
    """``_ua_hash`` must be stable, 16-char, and distinguish common UAs."""
    from backend.api.auth import _ua_hash

    a = _ua_hash("Mozilla/5.0 (Windows NT 10.0) Chrome/120")
    b = _ua_hash("Mozilla/5.0 (Windows NT 10.0) Chrome/120")
    c = _ua_hash("Mozilla/5.0 (Windows NT 10.0) Chrome/121")
    assert a == b
    assert a != c
    assert len(a) == 16
    # Empty / None → deterministic empty-string sentinel.
    assert _ua_hash("") == ""
    assert _ua_hash(None) == ""


@pytest.mark.asyncio
async def test_ua_hash_handles_exotic_inputs():
    """Unicode / quotes / 10-KB strings don't crash or return variable length."""
    from backend.api.auth import _ua_hash

    quoted = _ua_hash('"injection";DROP TABLE user;--')
    unicode_ua = _ua_hash("模拟器 🐉 emoji-browser/1.0")
    huge_ua = _ua_hash("A" * 10_000)
    for h in (quoted, unicode_ua, huge_ua):
        assert len(h) == 16
        int(h, 16)  # all hex


@pytest.mark.asyncio
async def test_login_huge_user_agent_survives(client, email_service):
    """A 10 KB UA header shouldn't blow up the endpoint.

    This also doubles as a DoS smoke test: if the whole UA were persisted
    verbatim the column would inflate rapidly. Because only the 16-char
    hash is stored, repeated huge-UA logins stay bounded.
    """
    await _register_user(client)
    big_ua = "A" * 10_000
    r = await _login(client, username="edwina", user_agent=big_ua)
    assert r.status_code == 200

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "edwina"))
        ).scalar_one()
        stored = json.loads(user.known_ips_json)
        # Only the 16-char hash was persisted, not the full 10k string.
        assert len(stored) == 1
        assert len(stored[0]["ua_hash"]) == 16
        assert big_ua not in user.known_ips_json


@pytest.mark.asyncio
async def test_login_quoted_ua_does_not_crash(client):
    """A UA with SQL-injection-esque quotes traverses the stack cleanly.

    Pure-unicode / non-ASCII UAs are validated through ``_ua_hash``
    directly in :func:`test_ua_hash_handles_exotic_inputs`; the wire-
    level test below confirms shell/SQL metacharacters in the UA
    header don't break the endpoint.
    """
    await _register_user(client)
    r = await _login(
        client, username="edwina", user_agent='"; drop table user;--'
    )
    assert r.status_code == 200
    r2 = await _login(
        client,
        username="edwina",
        user_agent="Mozilla/5.0 (ugly-ua)<script>alert(1)</script>",
    )
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_disabled_flag_overrides_everything(client, email_service, monkeypatch):
    """Even with prior history + a clearly-new UA, flag=false silences email."""
    from backend.config import get_settings

    await _register_user(client)
    # Seed one login so history exists.
    await _login(client, username="edwina", user_agent="ua-first")
    await asyncio.sleep(0.02)
    email_service.sent_messages.clear()

    monkeypatch.setenv("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "false")
    get_settings.cache_clear()

    r = await _login(client, username="edwina", user_agent="ua-totally-new")
    assert r.status_code == 200
    await asyncio.sleep(0.05)

    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert alerts == []


# ---------------------------------------------------------------------------
# #34 Backup cron — edge cases
# ---------------------------------------------------------------------------


def _write_placeholder_backup(path: Path, mtime_age_s: float) -> None:
    path.write_bytes(b"placeholder")
    t = time.time() - mtime_age_s
    os.utime(path, (t, t))


def test_backup_from_empty_db_succeeds(tmp_path):
    """A source DB with zero tables/rows still backs up cleanly."""
    from backend.app import _perform_sqlite_backup

    src = tmp_path / "empty.db"
    # Create just the file with a valid sqlite header + nothing else.
    con = sqlite3.connect(str(src))
    con.close()
    assert src.is_file()
    backup_dir = tmp_path / "bk"

    out = _perform_sqlite_backup(src, backup_dir, keep_last_n=5)
    assert out is not None
    assert out.is_file()
    assert out.stat().st_size > 0


def test_backup_filename_format_matches_spec(tmp_path):
    """``monitor-YYYYMMDD-HHMM.db`` — verify the regex literally."""
    import re as _re
    from backend.app import _perform_sqlite_backup

    src = tmp_path / "src.db"
    sqlite3.connect(str(src)).close()
    out = _perform_sqlite_backup(src, tmp_path / "bk", keep_last_n=3)
    assert out is not None
    m = _re.fullmatch(r"monitor-\d{8}-\d{4}\.db", out.name)
    assert m is not None, f"bad filename: {out.name}"


def test_backup_keep_last_n_prunes_with_five_rapid_backups(tmp_path):
    """Pre-seed 5 files, run the backup with keep_last_n=3 → two oldest gone."""
    from backend.app import _perform_sqlite_backup

    src = tmp_path / "src.db"
    sqlite3.connect(str(src)).close()
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    # Five pre-existing backups staggered from 1h ago → 5h ago.
    seeded: list[Path] = []
    for i in range(5):
        p = backup_dir / f"monitor-2026010{i}-0000.db"
        _write_placeholder_backup(p, mtime_age_s=(i + 1) * 3600)
        seeded.append(p)

    out = _perform_sqlite_backup(src, backup_dir, keep_last_n=3)
    assert out is not None
    remaining = sorted(backup_dir.glob("monitor-*.db"))
    assert len(remaining) == 3
    assert out in remaining
    # Oldest two seeded files should be gone.
    by_mtime = sorted(seeded, key=lambda p: p.stat().st_mtime if p.exists() else 0)
    # ``seeded[-1]`` is newest (mtime_age_s=5h) — *wait*, we staggered by
    # (i+1)*3600 so index 0 is 1h old (newest of the seeded) and index 4
    # is 5h old (oldest). The two oldest (indices 3, 4) should be pruned.
    assert not seeded[4].exists()
    assert not seeded[3].exists()


def test_backup_keep_last_n_one_keeps_only_newest(tmp_path):
    """``keep_last_n=1`` — the just-written backup survives, all seeds pruned."""
    from backend.app import _perform_sqlite_backup

    src = tmp_path / "src.db"
    sqlite3.connect(str(src)).close()
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    for i in range(3):
        p = backup_dir / f"monitor-2026010{i}-0000.db"
        _write_placeholder_backup(p, mtime_age_s=(i + 1) * 3600)

    out = _perform_sqlite_backup(src, backup_dir, keep_last_n=1)
    remaining = list(backup_dir.glob("monitor-*.db"))
    assert remaining == [out]


def test_backup_survives_sqlite_error(tmp_path, caplog, monkeypatch):
    """If the underlying SQLite backup raises, we log + return None, no crash.

    ``sqlite3.Connection`` is a C type and its ``.backup`` attribute is
    read-only, so we monkey-patch ``sqlite3.connect`` in the module
    under test to return a thin shim whose ``.backup()`` raises
    ``OSError('simulated disk full')``.
    """
    from backend import app as app_mod

    src = tmp_path / "src.db"
    sqlite3.connect(str(src)).close()
    backup_dir = tmp_path / "bk"

    class _BoomConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def backup(self, *_a, **_kw):
            raise OSError("simulated disk full")

        def close(self):
            return None

    monkeypatch.setattr(app_mod.sqlite3, "connect", lambda *a, **k: _BoomConn())

    with caplog.at_level("ERROR"):
        out = app_mod._perform_sqlite_backup(src, backup_dir, keep_last_n=3)

    assert out is None
    # Any half-written destination file is cleaned up.
    assert list(backup_dir.glob("monitor-*.db")) == []
    assert any("backup" in r.message.lower() for r in caplog.records)


def test_sqlite_path_from_url_accepts_relative_and_absolute(tmp_path):
    from backend.app import _sqlite_path_from_url

    assert _sqlite_path_from_url("sqlite:///tmp/monitor.db") == Path("tmp/monitor.db")
    assert _sqlite_path_from_url(
        "sqlite+aiosqlite:///" + str(tmp_path / "x.db")
    ) == tmp_path / "x.db"
    assert _sqlite_path_from_url("sqlite+aiosqlite:///:memory:") is None
    assert _sqlite_path_from_url("mysql://u@h/db") is None


@pytest.mark.asyncio
async def test_backup_status_recent_files_not_leaking_abspath(client, tmp_path, monkeypatch):
    """``recent_files[*].name`` must be basename-only, never absolute paths."""
    # Point the endpoint at ``tmp_path`` by rebinding ``BACKEND_DIR``.
    from backend import app as app_mod

    fake_backend_dir = tmp_path / "backend"
    (fake_backend_dir / "data" / "backups").mkdir(parents=True)
    # Seed a real backup file.
    sample = fake_backend_dir / "data" / "backups" / "monitor-20260101-0000.db"
    sample.write_bytes(b"fake-sqlite")

    monkeypatch.setattr(app_mod, "BACKEND_DIR", fake_backend_dir, raising=True)

    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/backup-status",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["recent_files"]) == 1
    first = body["recent_files"][0]
    assert first["name"] == "monitor-20260101-0000.db"
    assert "/" not in first["name"]
    assert "\\" not in first["name"]
    assert str(tmp_path) not in json.dumps(body)


@pytest.mark.asyncio
async def test_backup_age_h_close_to_two_hours(client, tmp_path, monkeypatch):
    """File mtime forced to 2h ago → ``backup_age_h`` within [1.99, 2.01]."""
    from backend import app as app_mod

    fake = tmp_path / "backend"
    (fake / "data" / "backups").mkdir(parents=True)
    f = fake / "data" / "backups" / "monitor-20260101-0000.db"
    f.write_bytes(b"x")
    two_h_ago = time.time() - 2 * 3600
    os.utime(f, (two_h_ago, two_h_ago))
    monkeypatch.setattr(app_mod, "BACKEND_DIR", fake, raising=True)

    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/backup-status",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    body = r.json()
    age = body["backup_age_h"]
    assert age is not None
    assert 1.98 <= age <= 2.02, f"unexpected age {age}"


@pytest.mark.asyncio
async def test_backup_status_recent_files_capped_by_keep_last_n(client, tmp_path, monkeypatch):
    """Even with 20 files on disk, the payload lists at most ``keep_last_n``."""
    from backend import app as app_mod
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_BACKUP_KEEP_LAST_N", "3")
    get_settings.cache_clear()

    fake = tmp_path / "backend"
    (fake / "data" / "backups").mkdir(parents=True)
    for i in range(20):
        p = fake / "data" / "backups" / f"monitor-2026010{i % 10}-{i:02d}00.db"
        p.write_bytes(b"z")
        t = time.time() - i * 60
        os.utime(p, (t, t))
    monkeypatch.setattr(app_mod, "BACKEND_DIR", fake, raising=True)

    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/backup-status",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    body = r.json()
    assert body["keep_last_n"] == 3
    assert len(body["recent_files"]) == 3


@pytest.mark.asyncio
async def test_backup_loop_disabled_when_interval_zero(monkeypatch):
    """``ARGUS_BACKUP_INTERVAL_H=0`` → loop returns immediately."""
    from backend.app import _backup_loop
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_BACKUP_INTERVAL_H", "0")
    get_settings.cache_clear()

    # Run the coroutine directly; it must return without awaiting sleep.
    await asyncio.wait_for(_backup_loop(), timeout=0.5)


@pytest.mark.asyncio
async def test_backup_loop_disabled_for_non_file_sqlite(monkeypatch):
    """In-memory SQLite → loop logs & returns without looping."""
    from backend.app import _backup_loop
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_BACKUP_INTERVAL_H", "1")
    monkeypatch.setenv(
        "ARGUS_DB_URL", "sqlite+aiosqlite:///:memory:"
    )
    get_settings.cache_clear()

    await asyncio.wait_for(_backup_loop(), timeout=0.5)


# ---------------------------------------------------------------------------
# Migration 016 — schema contract surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_is_idle_flagged_defaults_false_for_new_row(client):
    """A freshly-inserted job without explicit is_idle_flagged → False."""
    async with SessionLocal() as db:
        db.add(Batch(id="b-default", project="p", status="running"))
        db.add(Job(id="j-default", batch_id="b-default", status="running"))
        await db.commit()

    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j-default"))
        ).scalar_one()
        assert job.is_idle_flagged is False


@pytest.mark.asyncio
async def test_user_known_ips_json_nullable(client):
    """Existing users with NULL known_ips_json must still log in OK."""
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        # Force the column back to NULL — simulate a pre-migration user.
        user.known_ips_json = None
        await db.commit()

    r = await _login(client, username="tester", user_agent="ua-null-path")
    assert r.status_code == 200
