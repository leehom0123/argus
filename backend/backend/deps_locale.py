"""Locale dependency for Accept-Language header parsing.

Provides a FastAPI dependency ``get_locale`` that parses the first
quality-weighted language tag from the ``Accept-Language`` header and
maps it to one of the supported locales.

Supported locales
-----------------
* ``en-US``  (default / fallback)
* ``zh-CN``

Mapping rules
-------------
* Any tag starting with ``zh`` (e.g. ``zh``, ``zh-CN``, ``zh-TW``,
  ``zh-Hans``) maps to ``zh-CN``.
* Any unknown tag falls back to ``en-US``.
* A missing header defaults to ``en-US``.

Example
-------
``Accept-Language: zh-CN, en;q=0.8``  →  ``"zh-CN"``
``Accept-Language: fr-FR, en;q=0.9``  →  ``"en-US"``
``Accept-Language:`` (absent)          →  ``"en-US"``

PR-12 note: once the User model gains a ``preferred_locale`` column the
dependency can be extended to prefer the DB value for authenticated
callers. The header-only path is intentionally kept separate here.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import Header

# Only these values are ever returned from get_locale.
SupportedLocale = Literal["en-US", "zh-CN"]

_DEFAULT_LOCALE: SupportedLocale = "en-US"

# Matches a single language tag optionally followed by a quality weight.
# E.g.: "zh-CN", "en;q=0.8", "zh"
_TAG_RE = re.compile(
    r"([a-zA-Z]{1,8}(?:-[a-zA-Z0-9]{1,8})*)"  # language tag
    r"(?:\s*;\s*q\s*=\s*([0-9](?:\.[0-9]{0,3})?))?",  # optional ;q=N
    re.ASCII,
)


def _parse_accept_language(header: str) -> SupportedLocale:
    """Parse *header* and return the highest-quality supported locale."""
    # Build a list of (quality, tag) pairs, highest quality first.
    candidates: list[tuple[float, str]] = []
    for part in header.split(","):
        part = part.strip()
        m = _TAG_RE.match(part)
        if not m:
            continue
        tag = m.group(1)
        q_str = m.group(2)
        quality = float(q_str) if q_str is not None else 1.0
        candidates.append((quality, tag))

    # Sort descending by quality; stable sort preserves declaration order on ties.
    candidates.sort(key=lambda x: x[0], reverse=True)

    for _, tag in candidates:
        tag_lower = tag.lower()
        if tag_lower.startswith("zh"):
            return "zh-CN"
        if tag_lower.startswith("en"):
            return "en-US"

    return _DEFAULT_LOCALE


def get_locale(
    accept_language: str | None = Header(default=None),
) -> SupportedLocale:
    """FastAPI dependency: parse ``Accept-Language`` → :data:`SupportedLocale`.

    Returns ``"en-US"`` when the header is absent or no supported tag is
    found.  Never raises.
    """
    if not accept_language:
        return _DEFAULT_LOCALE
    try:
        return _parse_accept_language(accept_language)
    except Exception:  # noqa: BLE001
        return _DEFAULT_LOCALE
