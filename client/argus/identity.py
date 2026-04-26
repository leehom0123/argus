"""Deterministic batch-id derivation for crash-safe resume.

The default batch identity flow in :class:`argus.Reporter` mints a fresh
``<prefix>-<12 hex>`` UUID on every ``__enter__``. That works fine for
greenfield runs but breaks the natural workflow when a multi-job batch
crashes mid-flight: re-running the same launcher mints a *new* batch id,
so the partial result rows on the backend are stranded under one id and
the freshly produced rows live under another.

:func:`derive_batch_id` solves that without explicit user bookkeeping by
hashing a stable triple — ``(project, experiment_name, git_sha)`` — into
a deterministic id. Re-running the same launcher (same git checkout,
same experiment) reproduces the same id; events from the resumed run
land in the same Batch row on the backend, which is already idempotent
on ``batch_id`` as of v0.1.x (see ``backend/api/events.py::_handle_batch_start``).

The helper is purely advisory: callers may always pass ``batch_id="..."``
explicitly, in which case derivation never runs.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("argus")

_DEFAULT_PREFIX = "bench"
_NO_GIT = "no-git"


def _read_git_sha() -> str:
    """Best-effort ``git rev-parse HEAD``; returns ``"no-git"`` on failure.

    Falling back to a constant string keeps the derived id stable across
    machines that don't have git installed (e.g. a stripped-down
    container) — the derivation contract is "same inputs → same id", and
    "no git" is treated as one valid input value.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _NO_GIT
    if out.returncode != 0:
        return _NO_GIT
    sha = (out.stdout or "").strip()
    return sha or _NO_GIT


def derive_batch_id(
    project: str,
    experiment_name: str,
    git_sha: Optional[str] = None,
    *,
    prefix: str = _DEFAULT_PREFIX,
) -> str:
    """Return ``f"{prefix}-{16-hex}"`` derived from project + experiment + git.

    Parameters
    ----------
    project:
        Project / namespace name; usually ``cfg.monitor.project``.
    experiment_name:
        The experiment-level identifier — for sibyl this is
        ``cfg.experiment_name`` (e.g. ``etth1_transformer``); for a
        multi-experiment sweep, the launcher's batch tag.
    git_sha:
        Caller-supplied commit hash. ``None`` (default) triggers a
        ``git rev-parse HEAD`` lookup; a string ``""`` is treated as
        "no git" too. Pass an explicit string in tests for determinism.
    prefix:
        Leading token of the returned id (default ``"bench"``).

    Returns
    -------
    str
        A reproducible id of the form ``"<prefix>-<16 hex>"``. Re-running
        the same command from the same checkout yields the same id, so
        the resumed events append to the existing Batch row instead of
        forking a new one.

    Notes
    -----
    16 hex chars = 64 bits of SHA-256. That's plenty for collision
    avoidance across a single project's experiment space; we'd need
    ~4 billion concurrent batches before a clash becomes likely.
    """
    if git_sha is None:
        git_sha = _read_git_sha()
    elif not git_sha:
        # Treat empty string the same as "no git" so callers can pass
        # ``os.environ.get("GIT_SHA")`` without branching.
        git_sha = _NO_GIT

    # ``|`` is reserved out of any reasonable project / experiment name,
    # so it gives an unambiguous join character. Use UTF-8 explicitly so
    # non-ASCII project names hash deterministically across platforms.
    raw = f"{project}|{experiment_name}|{git_sha}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{prefix}-{digest}"


__all__ = ["derive_batch_id"]
