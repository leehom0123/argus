"""Abstract auth provider interface. See requirements §12."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import User


class AuthProvider(ABC):
    """Pluggable auth strategy.

    Every provider answers the question "given these credentials, is there a
    local :class:`User` we should treat as authenticated?". Implementations
    may create a new user on first contact (OAuth JIT-provision) or refuse
    login outright (local password check).
    """

    name: str  # 'local' | 'github' | 'google' | ...

    @abstractmethod
    async def authenticate(
        self,
        credentials: dict[str, Any],
        db: AsyncSession,
    ) -> User | None:
        """Return the matching user, or None if credentials are invalid.

        Implementations must *never* raise for invalid-credential cases —
        return None instead. Raises are reserved for true infrastructure
        failures (DB down, OAuth provider offline).
        """

    # --- OAuth-only hooks (local provider ignores these) -------------------

    async def get_redirect_url(self, state: str) -> str:  # pragma: no cover
        """Return the provider's OAuth authorize URL. Local returns ''."""
        return ""

    async def handle_callback(
        self,
        code: str,
        state: str,
        db: AsyncSession,
    ) -> User:  # pragma: no cover
        """Exchange ``code`` for a user. Local raises NotImplementedError."""
        raise NotImplementedError(
            f"{self.name} is not an OAuth provider"
        )
