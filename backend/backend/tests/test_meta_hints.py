"""Roadmap #30 — backend-provided empty-state hints.

``GET /api/meta/hints`` returns ``{locale, hints: dict[str, str]}``.
Locale is driven off ``Accept-Language`` (same parsing as the
HTTPException message catalog). The keys match what the frontend's
EmptyState component expects (``empty_hosts``, ``empty_batches``, ...).

Covered:
  1. No header → en-US catalog, containing every expected key
  2. zh-CN header → Chinese catalog with the same key set
  3. Unsupported locale falls back to en-US
  4. No auth header is required (endpoint is public UI bootstrap)
  5. Response obeys schema (extra="forbid" on MetaHintsOut)
"""
from __future__ import annotations

import pytest


_REQUIRED_KEYS = {
    "empty_hosts",
    "empty_batches",
    "empty_jobs",
    "empty_projects",
    "empty_notifications",
    "empty_pins",
    "empty_shared",
    "empty_stars",
    "empty_search",
}


@pytest.mark.asyncio
async def test_meta_hints_default_en(unauthed_client):
    """No Accept-Language → en-US catalog, unauthed is fine."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "en-US"
    hints = body["hints"]
    # Every required empty-state key must be present.
    missing = _REQUIRED_KEYS - set(hints.keys())
    assert not missing, f"Missing hint keys: {missing}"
    # Sanity: an English hint mentions ARGUS_URL for the hosts key.
    assert "ARGUS_URL" in hints["empty_hosts"]


@pytest.mark.asyncio
async def test_meta_hints_zh_cn(unauthed_client):
    """Accept-Language: zh-CN → zh-CN catalog."""
    r = await unauthed_client.get(
        "/api/meta/hints",
        headers={"Accept-Language": "zh-CN, en;q=0.8"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "zh-CN"
    hints = body["hints"]
    missing = _REQUIRED_KEYS - set(hints.keys())
    assert not missing, f"Missing hint keys in zh-CN: {missing}"
    # A Chinese hint must contain CJK characters.
    assert any("一" <= ch <= "鿿" for ch in hints["empty_hosts"])
    # Key to English override: still references ARGUS_URL by name
    assert "ARGUS_URL" in hints["empty_hosts"]


@pytest.mark.asyncio
async def test_meta_hints_unsupported_locale_falls_back_to_en(unauthed_client):
    """``Accept-Language: fr-FR`` still returns en-US (get_locale fallback)."""
    r = await unauthed_client.get(
        "/api/meta/hints",
        headers={"Accept-Language": "fr-FR"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "en-US"
    # Expect the English phrasing
    assert "No hosts" in body["hints"]["empty_hosts"]


@pytest.mark.asyncio
async def test_meta_hints_shape_is_stable(unauthed_client):
    """Response has exactly {locale, hints} and nothing else."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"locale", "hints"}
    assert isinstance(body["hints"], dict)
    # Every hint value is a non-empty string.
    for k, v in body["hints"].items():
        assert isinstance(v, str) and v.strip(), (
            f"Hint {k!r} has empty or non-string value: {v!r}"
        )


@pytest.mark.asyncio
async def test_meta_hints_en_and_zh_share_key_set(unauthed_client):
    """The two locales expose identical key sets so the frontend never sees a gap."""
    r_en = await unauthed_client.get("/api/meta/hints")
    r_zh = await unauthed_client.get(
        "/api/meta/hints", headers={"Accept-Language": "zh-CN"},
    )
    en_keys = set(r_en.json()["hints"].keys())
    zh_keys = set(r_zh.json()["hints"].keys())
    assert en_keys == zh_keys, (
        f"en-only: {en_keys - zh_keys}; zh-only: {zh_keys - en_keys}"
    )
