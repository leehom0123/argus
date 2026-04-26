"""Bilingual message catalog for HTTP error responses.

Public API
----------
tr(locale, key, **fmt) -> str
    Return the translated string, with str.format(**fmt) applied.
    Falls back en-US → raw key on any miss. Never raises.

Example
-------
>>> tr("zh-CN", "auth.locked", minutes=5)
'账号已锁定，请 5 分钟后再试'
>>> tr("en-US", "auth.locked", minutes=5)
'Account temporarily locked; try again in 5 minutes'
"""
from .messages import MESSAGES, tr

__all__ = ["MESSAGES", "tr"]
