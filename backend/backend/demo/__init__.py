"""Built-in demo-project fixtures.

Exports :func:`seed_demo` so ``backend.app.lifespan`` can call it at
startup and ``backend.api.admin`` can expose a force-regenerate
endpoint. The heavy lifting lives in :mod:`backend.demo.seed`.
"""
from __future__ import annotations

from backend.demo.seed import DEMO_PROJECT, DEMO_BATCH_ID, DEMO_HOST, seed_demo

__all__ = ["seed_demo", "DEMO_PROJECT", "DEMO_BATCH_ID", "DEMO_HOST"]
