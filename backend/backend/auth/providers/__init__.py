"""Auth provider implementations.

The :class:`AuthProvider` ABC in :mod:`backend.auth.providers.base` is the
extension point for phase-2 OAuth (GitHub / Google / LDAP). MVP ships only
:class:`LocalAuthProvider` — username + password.
"""
from backend.auth.providers.base import AuthProvider
from backend.auth.providers.local import LocalAuthProvider

__all__ = ["AuthProvider", "LocalAuthProvider"]
