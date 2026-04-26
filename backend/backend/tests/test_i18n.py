"""Tests for the bilingual message catalog and get_locale dependency.

Covers:
  - tr() returns correct English strings
  - tr() returns correct Chinese strings
  - tr() falls back to key string on missing key
  - tr() falls back to en-US on unknown locale
  - tr() applies str.format() interpolation
  - get_locale() parses quality-weighted Accept-Language
  - get_locale() defaults to en-US when header is absent
  - get_locale() falls back to en-US on unknown language tag
"""
from __future__ import annotations

import pytest

from backend.i18n import tr
from backend.deps_locale import get_locale, _parse_accept_language


# ---------------------------------------------------------------------------
# tr() — message catalog
# ---------------------------------------------------------------------------


def test_tr_en_returns_english() -> None:
    result = tr("en-US", "auth.token.invalid")
    assert result == "Invalid or expired token"


def test_tr_zh_returns_chinese() -> None:
    result = tr("zh-CN", "auth.token.invalid")
    assert result == "令牌无效或已过期"


def test_tr_missing_key_returns_key_as_string() -> None:
    """When a key does not exist in any locale, the key itself is returned."""
    missing = "this.key.does.not.exist"
    result = tr("en-US", missing)
    assert result == missing


def test_tr_missing_key_returns_key_zh() -> None:
    """Same fallback behaviour for zh-CN locale."""
    missing = "totally.unknown.key"
    result = tr("zh-CN", missing)
    assert result == missing


def test_tr_missing_locale_falls_back_to_en() -> None:
    """An unrecognised locale gracefully falls back to the en-US string."""
    result = tr("fr-FR", "auth.token.invalid")
    assert result == "Invalid or expired token"


def test_tr_formats_interpolation_zh() -> None:
    """Interpolation placeholders are filled correctly for zh-CN."""
    result = tr("zh-CN", "auth.locked", minutes=5)
    assert result == "账号已锁定，请 5 分钟后再试"


def test_tr_formats_interpolation_en() -> None:
    """Interpolation placeholders are filled correctly for en-US."""
    result = tr("en-US", "auth.locked", minutes=10)
    assert result == "Account temporarily locked; try again in 10 minutes"


def test_tr_interpolation_missing_placeholder_does_not_raise() -> None:
    """If the format template has a placeholder but none are passed, return raw."""
    raw = tr("en-US", "auth.locked")  # no minutes= kwarg
    # Should not raise; returns the raw (un-formatted) string
    assert "{minutes}" in raw


def test_tr_credentials_bad_en() -> None:
    assert tr("en-US", "auth.credentials.bad") == "Invalid username / email or password"


def test_tr_credentials_bad_zh() -> None:
    assert tr("zh-CN", "auth.credentials.bad") == "邮箱或密码不正确"


def test_tr_all_en_keys_are_strings() -> None:
    """Sanity: every en-US value is a non-empty string."""
    from backend.i18n.messages import MESSAGES

    for key, value in MESSAGES["en-US"].items():
        assert isinstance(value, str) and value, f"en-US key {key!r} is empty"


def test_tr_all_zh_keys_are_strings() -> None:
    """Sanity: every zh-CN value is a non-empty string."""
    from backend.i18n.messages import MESSAGES

    for key, value in MESSAGES["zh-CN"].items():
        assert isinstance(value, str) and value, f"zh-CN key {key!r} is empty"


def test_tr_zh_has_same_keys_as_en() -> None:
    """zh-CN should cover all keys defined in en-US (parity check)."""
    from backend.i18n.messages import MESSAGES

    en_keys = set(MESSAGES["en-US"].keys())
    zh_keys = set(MESSAGES["zh-CN"].keys())
    missing = en_keys - zh_keys
    assert not missing, f"zh-CN is missing keys: {sorted(missing)}"


# ---------------------------------------------------------------------------
# get_locale() — Accept-Language parsing
# ---------------------------------------------------------------------------


def test_get_locale_parses_quality_weights() -> None:
    """zh-CN with higher quality than en should resolve to zh-CN."""
    result = _parse_accept_language("zh-CN, en;q=0.8")
    assert result == "zh-CN"


def test_get_locale_en_higher_than_zh() -> None:
    """en with higher quality than zh should resolve to en-US."""
    result = _parse_accept_language("en-US, zh-CN;q=0.5")
    assert result == "en-US"


def test_get_locale_defaults_to_en() -> None:
    """Missing header returns en-US."""
    result = get_locale(accept_language=None)
    assert result == "en-US"


def test_get_locale_unknown_tag_falls_back() -> None:
    """An unknown language tag (fr-FR) falls back to en-US."""
    result = _parse_accept_language("fr-FR")
    assert result == "en-US"


def test_get_locale_unknown_with_en_secondary() -> None:
    """Unknown primary tag + en secondary should return en-US."""
    result = _parse_accept_language("fr-FR;q=1.0, en;q=0.7")
    assert result == "en-US"


def test_get_locale_zh_bare_tag() -> None:
    """Bare 'zh' tag (without region subtag) should map to zh-CN."""
    result = _parse_accept_language("zh")
    assert result == "zh-CN"


def test_get_locale_empty_string_falls_back() -> None:
    """Empty Accept-Language string should fall back to en-US."""
    result = get_locale(accept_language="")
    assert result == "en-US"


def test_get_locale_wildcard_falls_back() -> None:
    """Wildcard '*' tag should fall back to en-US."""
    result = get_locale(accept_language="*")
    assert result == "en-US"
