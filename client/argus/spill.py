"""Append-only JSONL spill store used when the backend is unreachable.

Design goals:
- Never lose an event silently due to a transient network failure.
- Fail safe in the face of concurrent writers in the same process (thread
  lock) and in the odd case of two processes sharing the same path
  (fcntl advisory lock where available). Two Reporters in one process
  get independent files by default (pid+timestamp).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("argus")

try:
    import fcntl  # POSIX only
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - windows fallback
    _HAS_FCNTL = False


def default_spill_root() -> Path:
    root = Path(os.path.expanduser("~")) / ".argus-reporter"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_spill_path() -> Path:
    return default_spill_root() / f"spill-{os.getpid()}-{int(time.time())}.jsonl"


def iter_existing_spill_files():
    """Yield any pre-existing spill files under the default directory,
    ordered by mtime (oldest first). Used on reporter startup to replay
    events from crashed previous runs. Missing dir is tolerated.

    `.draining` sidecars (created mid-drain) are skipped — they'll be
    cleaned up by whoever is actively draining.
    """
    root = Path(os.path.expanduser("~")) / ".argus-reporter"
    if not root.exists():
        return
    try:
        files = [p for p in root.glob("spill-*.jsonl") if p.is_file()]
    except OSError:
        return
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
    for p in files:
        yield p


class SpillStore:
    """Append-only JSONL; one file per Reporter instance."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_spill_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # --- writes -----------------------------------------------------------
    def append(self, event: Dict[str, Any]) -> None:
        """Append one event. Never raises; logs on failure."""
        try:
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except Exception:
            logger.exception("spill: cannot serialize event; dropping")
            return
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    if _HAS_FCNTL:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        except OSError:
                            pass
                    f.write(line)
                    f.flush()
                    if _HAS_FCNTL:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
            except Exception:
                logger.exception("spill: append failed for %s", self.path)

    # --- reads ------------------------------------------------------------
    def drain(self) -> Iterator[Dict[str, Any]]:
        """Atomically rename + read lines. After drain, file is gone.

        If rename fails (e.g. file missing), yields nothing. Malformed
        JSON lines are logged and skipped, never raised.
        """
        with self._lock:
            if not self.path.exists():
                return
            tmp = self.path.with_suffix(self.path.suffix + ".draining")
            try:
                self.path.rename(tmp)
            except FileNotFoundError:
                return
            except OSError:
                logger.exception("spill: rename for drain failed")
                return
        try:
            with open(tmp, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("spill: malformed JSON at line %d, skipping", i + 1)
        finally:
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass

    def exists(self) -> bool:
        return self.path.exists()
