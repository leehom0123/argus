"""Notification plumbing (rule engine + channels)."""

from backend.notifications.base import BaseNotifier
from backend.notifications.feishu import FeishuNotifier
from backend.notifications.rules import Rule, evaluate, load_rules

__all__ = [
    "BaseNotifier",
    "FeishuNotifier",
    "Rule",
    "evaluate",
    "load_rules",
]
