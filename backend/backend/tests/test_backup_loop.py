"""Tests for the SQLite backup cron + /api/admin/backup-status endpoint.

Team A / roadmap #34. The production loop runs on a wall-clock timer;
these tests invoke ``_perform_sqlite_backup`` synchronously so we can
assert on file-retention semantics without waiting for the scheduler.
The admin endpoint is hit via the standard ``client`` fixture.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest


def _make_source_db(tmp_path: Path) -> Path:
    src = tmp_path / "monitor.db"
    con = sqlite3.connect(str(src))
    try:
        con.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
        con.execute("INSERT INTO probe (note) VALUES ('hi')")
        con.commit()
    finally:
        con.close()
    return src


def test_perform_backup_creates_file(tmp_path):
    from backend.app import _perform_sqlite_backup

    src = _make_source_db(tmp_path)
    backup_dir = tmp_path / "backups"
    out = _perform_sqlite_backup(src, backup_dir, keep_last_n=7)
    assert out is not None
    assert out.is_file()
    assert out.parent == backup_dir
    assert out.name.startswith("monitor-")
    assert out.suffix == ".db"

    # Must be a real SQLite copy (row count == 1).
    con = sqlite3.connect(str(out))
    try:
        n = con.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_perform_backup_prunes_to_keep_n(tmp_path):
    from backend.app import _perform_sqlite_backup

    src = _make_source_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(5):
        p = backup_dir / f"monitor-2026010{i}-0000.db"
        p.write_bytes(b"placeholder")
        # Stagger mtimes so sorting is deterministic.
        t = time.time() - (10 - i) * 60
        os.utime(p, (t, t))

    out = _perform_sqlite_backup(src, backup_dir, keep_last_n=3)
    assert out is not None
    files = sorted(backup_dir.glob("monitor-*.db"))
    assert len(files) == 3  # keep_last_n enforced
    # Our freshly-written backup must survive the prune.
    assert out in files


def test_perform_backup_handles_missing_source(tmp_path):
    from backend.app import _perform_sqlite_backup

    src = tmp_path / "nonexistent.db"
    backup_dir = tmp_path / "backups"
    assert _perform_sqlite_backup(src, backup_dir, keep_last_n=3) is None
    # No backup dir should be created for a no-op.
    assert not backup_dir.exists()


def test_sqlite_path_from_url_parses_correctly():
    from backend.app import _sqlite_path_from_url

    p = _sqlite_path_from_url("sqlite+aiosqlite:///tmp/monitor.db")
    assert p == Path("tmp/monitor.db")

    assert _sqlite_path_from_url("sqlite+aiosqlite:///:memory:") is None
    assert _sqlite_path_from_url("postgresql://user@host/db") is None


@pytest.mark.asyncio
async def test_backup_status_endpoint_shape(client):
    """Admin endpoint returns the documented JSON shape."""
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/backup-status",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "enabled",
        "interval_h",
        "keep_last_n",
        "last_backup_at",
        "backup_age_h",
        "recent_files",
    ):
        assert key in body, f"missing {key}"
    # In the test env the loop is disabled → no files yet.
    assert body["recent_files"] == []
    assert body["last_backup_at"] is None
    assert body["backup_age_h"] is None


@pytest.mark.asyncio
async def test_backup_status_rejects_non_admin(client):
    """A fresh non-admin user must get 403."""
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "joe",
            "email": "joe@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201, r.text

    client.headers.pop("Authorization", None)
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "joe", "password": "password123"},
    )
    assert login.status_code == 200, login.text
    jwt_joe = login.json()["access_token"]

    r = await client.get(
        "/api/admin/backup-status",
        headers={"Authorization": f"Bearer {jwt_joe}"},
    )
    assert r.status_code == 403, r.text
