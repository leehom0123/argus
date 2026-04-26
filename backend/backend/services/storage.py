"""Filesystem-backed artifact storage.

Keeps metadata in the ``artifact`` table and the actual bytes on disk.
Layout under ``settings.artifact_storage_dir``::

    <batch_id>/<job_id>/<artifact_id>_<safe_filename>

The ``<artifact_id>`` prefix avoids collisions when the same job uploads
two files with identical original names. ``secure_filename`` scrubs
path separators, control chars and leading dots so a compromised
reporter cannot escape the storage root via ``../`` or absolute paths.

Size caps (configurable via env):
  * per-file:   50 MB — rejected at 413 before any bytes hit disk
  * per-job:    500 MB — cumulative, checked via ``SUM(size_bytes)``

Both numbers are deliberately generous for research workloads (loss
plots + small CSVs + JSON metrics) but bounded so a runaway uploader
can't fill the server disk. Increase via
``ARGUS_ARTIFACT_MAX_FILE_MB`` / ``ARGUS_ARTIFACT_MAX_JOB_MB``.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


# ``_SAFE_CHARS`` — keep alphanumerics, dots, underscores, dashes.
# Anything else collapses to ``_`` so the resulting name is safe to
# embed in a filesystem path on Linux / macOS / Windows without extra
# escaping. Leading dots are stripped to prevent hidden-file surprises.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def secure_filename(name: str) -> str:
    """Sanitise an uploaded filename for use on disk.

    Rejects path traversal (``..``), strips directory separators, and
    collapses unsafe characters to ``_``. Empty results fall back to
    ``"upload.bin"`` so we always have *something* on disk.
    """
    if not name:
        return "upload.bin"
    # Reduce to the basename even if a caller passed "a/b/c.png".
    name = os.path.basename(name.replace("\\", "/"))
    # Strip control characters + collapse unsafe runs.
    name = _SAFE_CHARS.sub("_", name)
    # Leading dots → hidden file. Strip them.
    name = name.lstrip(".")
    # Avoid the literal ``..`` after collapsing.
    if name in ("", "_", "..", "."):
        return "upload.bin"
    # Cap at 120 chars so the full storage_path stays well under the
    # 255-char PATH_MAX most filesystems enforce per-component.
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        if stem and ext and len(ext) <= 8:
            name = stem[: 120 - len(ext) - 1] + "." + ext
        else:
            name = name[:120]
    return name


class ArtifactStore:
    """Write + read raw artifact bytes under a single root directory.

    The backend creates exactly one store per process
    (:func:`get_store`); all API endpoints share it. Tests get a fresh
    store per run via a ``tmp_path`` fixture, bypassing the global
    singleton by constructing the class directly.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Dedicated tmp dir UNDER the store root so streaming uploads can
        # `os.replace(tmp, dest)` intra-filesystem. Putting the temp file
        # in `$TMPDIR` (default of `tempfile.mkstemp`) breaks in
        # production when `/tmp` is tmpfs and the store root is on a
        # mounted volume — `os.replace` raises ``OSError: [Errno 18]
        # Invalid cross-device link``. See v0.1.4 hotfix.
        self._tmp_dir = self.root / "_tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Temp file helper
    # ------------------------------------------------------------------
    def make_temp_path(
        self, *, prefix: str = "argus-art-", suffix: str = ".part"
    ) -> tuple[int, str]:
        """Create a temp file inside the store root. Returns ``(fd, path)``.

        Caller is responsible for closing the fd and removing the file
        on failure. Created under ``self._tmp_dir`` (a subdirectory of
        ``self.root``) so a subsequent ``os.replace`` to a destination
        elsewhere in ``self.root`` is guaranteed intra-filesystem.
        Re-creates the tmp dir on demand in case ``clear()`` (or an
        external janitor) removed it after init.
        """
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        return tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=str(self._tmp_dir)
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def save(
        self,
        *,
        artifact_id: int,
        batch_id: str,
        job_id: str,
        filename: str,
        data: bytes,
    ) -> str:
        """Persist ``data`` and return the relative storage path.

        The caller is responsible for having already inserted the
        artifact row to allocate ``artifact_id``. A failed ``save``
        leaves no partial file: we write to a ``.tmp`` sibling then
        ``os.replace`` atomically, and on any exception the tmp file is
        cleaned up before re-raising.
        """
        safe_batch = secure_filename(batch_id)
        safe_job = secure_filename(job_id)
        safe_name = secure_filename(filename)
        rel = f"{safe_batch}/{safe_job}/{artifact_id}_{safe_name}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        return rel

    def save_from_path(
        self,
        *,
        artifact_id: int,
        batch_id: str,
        job_id: str,
        filename: str,
        source_path: Path | str,
    ) -> str:
        """Move an already-on-disk temp file into the store atomically.

        Mirrors :meth:`save` but skips the in-memory copy — used by the
        streamed upload path so a 50 MB upload never has to live in RAM.
        ``source_path`` is consumed (replaced into ``dest``); the caller
        must not unlink it after this returns.
        """
        safe_batch = secure_filename(batch_id)
        safe_job = secure_filename(job_id)
        safe_name = secure_filename(filename)
        rel = f"{safe_batch}/{safe_job}/{artifact_id}_{safe_name}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(source_path), dest)
        return rel

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def open_path(self, storage_path: str) -> Path:
        """Resolve a relative storage path under the store root.

        Raises ``FileNotFoundError`` if the file doesn't exist.
        Additionally guards against path traversal: the resolved path
        MUST stay within ``self.root``; otherwise we raise
        ``ValueError`` and the caller returns 404.
        """
        candidate = (self.root / storage_path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as e:
            raise ValueError(
                f"resolved path {candidate} escapes artifact root"
            ) from e
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        return candidate

    def delete(self, storage_path: str) -> None:
        """Remove the file at ``storage_path`` if it exists.

        Never raises on missing files — a crashed upload may leave the
        DB row around with no file; the delete endpoint still succeeds.
        Also removes empty parent directories so a batch's storage
        folder disappears once the last artifact is gone.
        """
        try:
            target = self.open_path(storage_path)
        except (FileNotFoundError, ValueError):
            return
        try:
            target.unlink()
        except OSError as exc:
            log.warning("failed to delete artifact %s: %s", storage_path, exc)
            return
        # Walk upwards, cleaning empty dirs until we hit the root.
        parent = target.parent
        while parent != self.root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def clear(self) -> None:
        """Delete every file under the store root (test-only)."""
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Module-level singleton + test hook
# ---------------------------------------------------------------------------


_store: ArtifactStore | None = None


def _default_root() -> Path:
    """Resolve the artifact root from settings or env, with a sane default."""
    env_override = os.environ.get("ARGUS_ARTIFACT_DIR", "").strip()
    if env_override:
        return Path(env_override)
    # Fall back to a subdirectory of the backend data dir so a dev
    # checkout survives a server restart without extra plumbing.
    from backend.config import DATA_DIR  # local import: avoid cycles
    return DATA_DIR / "artifacts"


def get_store() -> ArtifactStore:
    """Return the process-wide :class:`ArtifactStore` singleton."""
    global _store
    if _store is None:
        _store = ArtifactStore(_default_root())
    return _store


def reset_store_for_tests(root: Path | str | None = None) -> ArtifactStore:
    """Reinstall a fresh store (test-only hook).

    Tests pass a ``tmp_path`` so they never share state. Returns the
    new store so callers can assert on it directly.
    """
    global _store
    if root is None:
        root = _default_root()
    _store = ArtifactStore(root)
    return _store


# ---------------------------------------------------------------------------
# Size caps (module-level helpers so the API can import without the store)
# ---------------------------------------------------------------------------


def max_file_bytes() -> int:
    """Per-file size cap, in bytes. Env: ``ARGUS_ARTIFACT_MAX_FILE_MB``."""
    mb = int(os.environ.get("ARGUS_ARTIFACT_MAX_FILE_MB", "50") or "50")
    return mb * 1024 * 1024


def max_job_bytes() -> int:
    """Per-job cumulative cap, in bytes. Env: ``ARGUS_ARTIFACT_MAX_JOB_MB``."""
    mb = int(os.environ.get("ARGUS_ARTIFACT_MAX_JOB_MB", "500") or "500")
    return mb * 1024 * 1024
