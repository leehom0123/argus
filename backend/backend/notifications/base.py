"""Abstract base class for notification channels."""
from __future__ import annotations

import abc


class BaseNotifier(abc.ABC):
    """A channel that can dispatch a short message (title + body)."""

    name: str = "base"

    @abc.abstractmethod
    async def send(self, title: str, body: str, level: str = "info") -> None:
        """Deliver one notification.

        Implementations should log but not raise on transient failure; the
        ingest path treats notification dispatch as best-effort.
        """
        raise NotImplementedError
