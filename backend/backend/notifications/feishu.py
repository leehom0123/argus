"""Feishu (Lark) webhook notifier."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.notifications.base import BaseNotifier

log = logging.getLogger(__name__)


class FeishuNotifier(BaseNotifier):
    """POST a simple text card to a Feishu custom-bot webhook."""

    name = "feishu"

    def __init__(self, webhook_url: str, timeout: float = 5.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    async def send(self, title: str, body: str, level: str = "info") -> None:
        text = f"[{level.upper()}] {title}\n{body}" if title else body
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - best-effort notification
            log.warning("feishu notification failed: %s", exc)
