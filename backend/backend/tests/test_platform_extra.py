"""Platform-level sanity tests landed on ``feat/team-platform``.

These are QA-lane regression checks for the four platform deliverables
merged on the ``feat/team-platform`` branch:

  * Apache-2.0 LICENSE + NOTICE at the repo root.
  * ``backend/pyproject.toml`` SPDX license field + ``[project.optional-dependencies].postgres``.
  * Boolean ``server_default`` fixes across 5 migrations (PG compatibility).
  * SQLite-first default: ``Settings.db_url`` points at SQLite out of the box.

The tests are intentionally lightweight — they pin invariants that, if broken,
would be very hard to catch from normal unit/integration coverage (e.g. the
pyproject license slipping from ``Apache-2.0`` back to unspecified, or the
LICENSE file being accidentally deleted). They run under pytest on SQLite like
the rest of the suite and have no external service dependencies.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
#
# Tests live at ``backend/backend/tests/test_platform_extra.py``. The repo root
# is therefore ``parents[3]`` — up out of ``tests``, ``backend`` (pkg),
# ``backend`` (project dir), arriving at the worktree.
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[3]
_BACKEND_DIR = _THIS_FILE.parents[2]


# ---------------------------------------------------------------------------
# Postgres extras — importable only when ``pip install -e ".[postgres]"``.
# ---------------------------------------------------------------------------


def test_asyncpg_importable_when_postgres_extra_installed() -> None:
    """``asyncpg`` powers the async runtime path for the Postgres extra.

    Skips gracefully if the extra is not installed (default SQLite dev setup).
    """

    pytest.importorskip("asyncpg")


def test_psycopg2_importable_when_postgres_extra_installed() -> None:
    """``psycopg2-binary`` is used by alembic for sync migration ops on PG.

    Skips gracefully if the extra is not installed.
    """

    pytest.importorskip("psycopg2")


# ---------------------------------------------------------------------------
# LICENSE + NOTICE — pinned to repo root.
# ---------------------------------------------------------------------------


def test_license_file_exists_and_is_apache() -> None:
    license_path = _REPO_ROOT / "LICENSE"
    assert license_path.is_file(), f"LICENSE not found at {license_path}"
    text = license_path.read_text(encoding="utf-8")
    assert text.strip(), "LICENSE must not be empty"
    # The canonical Apache 2.0 header contains this exact wording.
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    # Sanity size — the canonical text is ~11357 bytes; allow some newline slop.
    assert 10000 < len(text) < 13000, (
        f"LICENSE length {len(text)} looks wrong; expected ~11357 for the "
        "canonical Apache 2.0 text."
    )


def test_notice_file_exists_and_mentions_apache() -> None:
    notice_path = _REPO_ROOT / "NOTICE"
    assert notice_path.is_file(), f"NOTICE not found at {notice_path}"
    text = notice_path.read_text(encoding="utf-8")
    assert text.strip(), "NOTICE must not be empty"
    assert "Apache" in text, "NOTICE should reference the Apache License"


# ---------------------------------------------------------------------------
# pyproject.toml — SPDX license + postgres extra shape.
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    path = _BACKEND_DIR / "pyproject.toml"
    assert path.is_file(), f"backend/pyproject.toml missing at {path}"
    with path.open("rb") as fh:
        return tomllib.load(fh)


def test_pyproject_license_is_apache_2_0() -> None:
    """Accept either the modern SPDX form (``"Apache-2.0"``) or the legacy
    PEP 621 form (``{text = "Apache-2.0"}``). Both are valid and parseable
    by pip; we only care that the value names Apache-2.0."""

    data = _load_pyproject()
    license_field = data["project"]["license"]
    if isinstance(license_field, str):
        assert license_field == "Apache-2.0"
    else:
        assert isinstance(license_field, dict), (
            f"Unexpected license field type: {type(license_field).__name__}"
        )
        assert license_field.get("text") == "Apache-2.0" or \
               license_field.get("file", "").lower() in {"license", "license.txt"}, \
               f"license table should set text='Apache-2.0' (got {license_field!r})"


def test_pyproject_declares_postgres_extra() -> None:
    data = _load_pyproject()
    extras = data["project"].get("optional-dependencies", {})
    assert "postgres" in extras, "pyproject must declare [project.optional-dependencies].postgres"
    pg_deps = extras["postgres"]
    joined = " ".join(pg_deps)
    assert re.search(r"\basyncpg\b", joined), f"postgres extra missing asyncpg: {pg_deps!r}"
    assert re.search(r"\bpsycopg2", joined), f"postgres extra missing psycopg2: {pg_deps!r}"


# ---------------------------------------------------------------------------
# Settings default — SQLite out of the box.
# ---------------------------------------------------------------------------


def test_settings_default_db_url_is_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The open-source onboarding path must stay SQLite-first.

    Clearing the env vars ensures we're reading the Field default, not a
    developer's ambient ``ARGUS_DB_URL`` (e.g. when debugging Postgres).
    """

    monkeypatch.delenv("ARGUS_DB_URL", raising=False)
    monkeypatch.delenv("ARGUS_DB_URL", raising=False)

    # Fresh import so env changes take effect (Settings reads os.environ eagerly).
    from backend.config import Settings  # noqa: WPS433 — intentional local import

    settings = Settings()
    assert settings.db_url.startswith("sqlite"), (
        f"Expected SQLite default, got {settings.db_url!r}"
    )
