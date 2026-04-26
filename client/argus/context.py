"""High-level context-manager API for Argus.

Two new public classes wrap the lower-level :class:`ExperimentReporter`
and bring the four "advanced" patterns from the DeepTS-Flow scripts/
inside the SDK so users get them automatically:

* a heartbeat daemon that emits a ``heartbeat`` event every N seconds so
  long analysis callbacks (SHAP, attention export, ...) don't trip the
  monitor's stalled-batch detector,
* a stop-signal poller that hits
  ``GET /api/batches/<id>/stop-requested`` and flips a flag so the
  training loop can exit cleanly,
* a per-process resource snapshotter (GPU / CPU / RAM / disk) that
  emits ``resource_snapshot`` events every N seconds,
* an artifact uploader that POSTs visualization files (PNG / PDF / ...)
  to ``/api/batches/<id>/artifacts`` (and to ``/api/jobs/<id>/artifacts``
  when called from a :class:`JobContext`).

Quickstart::

    from argus import Reporter

    with Reporter("my-run", experiment_type="forecast",
                  source_project="deepts", n_total=3) as r:
        for job_id, model in [("j1", "transformer"), ("j2", "patchtst")]:
            with r.job(job_id, model=model, dataset="etth1") as j:
                if j.stopped:
                    break
                for ep in range(50):
                    j.epoch(ep, train_loss=..., val_loss=...)
                j.metrics({"MSE": 0.21, "MAE": 0.34})
                j.upload("outputs/.../visualizations")

The ``with`` blocks auto-emit ``batch_start`` / ``batch_done`` (or
``batch_failed`` on exception) and the parallel ``job_*`` events. Daemon
threads start on ``__enter__`` and stop on ``__exit__``; the SDK never
leaks threads.

Backwards compatibility: the previous :class:`ExperimentReporter` class
remains exported. ``Reporter`` is purely additive.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

try:  # optional read of monitor.yaml fallback
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is optional
    yaml = None  # type: ignore

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from .reporter import ExperimentReporter

logger = logging.getLogger("argus")

_DEFAULT_HEARTBEAT_S = 300.0
_DEFAULT_STOP_POLL_S = 10.0
_DEFAULT_RESOURCE_S = 30.0
_ARTIFACT_EXTS = {".png", ".jpg", ".jpeg", ".pdf", ".svg"}


# ---------------------------------------------------------------------------
# Module-level helpers (additive)
# ---------------------------------------------------------------------------

_GLOBAL_BATCH_ID: Optional[str] = None
_GLOBAL_REPORTER: Optional[ExperimentReporter] = None
_GLOBAL_LOCK = threading.Lock()


def new_batch_id(prefix: str = "batch") -> str:
    """Return a fresh ``<prefix>-<12 hex>`` batch id."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def set_batch_id(batch_id: str) -> None:
    """Set the process-wide current batch id (used by :func:`emit`)."""
    global _GLOBAL_BATCH_ID
    _GLOBAL_BATCH_ID = batch_id
    if _GLOBAL_REPORTER is not None:
        _GLOBAL_REPORTER.current_batch_id = batch_id


def get_batch_id() -> Optional[str]:
    """Return the process-wide current batch id, or ``None``."""
    return _GLOBAL_BATCH_ID


def sub_env(template: str, **extra: Any) -> str:
    """Tiny ``${VAR}`` / ``$VAR`` substitution helper.

    ``${ARGUS_URL}`` -> ``os.environ['ARGUS_URL']``. Missing keys
    are left as the literal placeholder. Used by configs that want to
    interpolate env-var values without pulling in OmegaConf.
    """
    if not isinstance(template, str):
        return template
    out: List[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "$" and i + 1 < n:
            if template[i + 1] == "{":
                end = template.find("}", i + 2)
                if end == -1:
                    out.append(template[i:])
                    break
                key = template[i + 2:end]
                out.append(str(extra.get(key, os.environ.get(key, f"${{{key}}}"))))
                i = end + 1
                continue
            j = i + 1
            while j < n and (template[j].isalnum() or template[j] == "_"):
                j += 1
            if j > i + 1:
                key = template[i + 1:j]
                out.append(str(extra.get(key, os.environ.get(key, f"${key}"))))
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def emit(event: str, **fields: Any) -> None:
    """Module-level escape hatch — direct emit on the active reporter.

    No-op when no :class:`Reporter` is active. Safe to call from any
    thread; never raises.
    """
    rep = _GLOBAL_REPORTER
    if rep is None:
        return
    try:
        rep._emit(  # noqa: SLF001 - intentional internal hook
            event,
            fields.pop("batch_id", None) or rep.current_batch_id or "",
            {k: v for k, v in fields.items() if k not in {"job_id"}},
            job_id=fields.get("job_id"),
        )
    except Exception:  # pragma: no cover
        logger.debug("emit escape-hatch failed", exc_info=True)


# ---------------------------------------------------------------------------
# Resource snapshot collection (in-SDK reimpl, no DeepTS dep)
# ---------------------------------------------------------------------------

_PROC_CACHE: Dict[int, Any] = {}


def _get_cached_process():  # pragma: no cover - exercised indirectly
    import psutil
    pid = os.getpid()
    proc = _PROC_CACHE.get(pid)
    if proc is None:
        proc = psutil.Process(pid)
        proc.cpu_percent(interval=None)
        _PROC_CACHE[pid] = proc
    return proc


def _collect_snapshot() -> Dict[str, Any]:
    """Best-effort GPU/CPU/RAM/disk snapshot. Empty dict on total failure.

    Mirrors :mod:`scripts.common.resource_snapshot.collect_resource_snapshot`
    but with no DeepTS-Flow imports — the SDK is standalone.
    """
    snap: Dict[str, Any] = {}
    try:
        snap["host"] = socket.gethostname()
    except Exception:
        pass
    try:
        snap["pid"] = os.getpid()
    except Exception:
        pass

    try:
        import psutil
        snap["cpu_util_pct"] = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        snap["ram_mb"] = int(vm.used // (1024 * 1024))
        snap["ram_total_mb"] = int(vm.total // (1024 * 1024))
    except Exception:
        pass

    try:
        import psutil  # noqa: F401  # already imported above when present
        proc = _get_cached_process()
        snap["proc_cpu_pct"] = float(proc.cpu_percent(interval=None))
        snap["proc_ram_mb"] = int(proc.memory_info().rss // (1024 * 1024))
    except Exception:
        pass

    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        try:
            snap["gpu_temp_c"] = int(
                pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:
            pass
        snap["gpu_util_pct"] = float(util.gpu)
        snap["gpu_mem_mb"] = int(mem.used // (1024 * 1024))
        snap["gpu_mem_total_mb"] = int(mem.total // (1024 * 1024))

        try:
            my_pid = os.getpid()
            procs: Optional[list] = None
            for fn in (
                "nvmlDeviceGetComputeRunningProcesses_v3",
                "nvmlDeviceGetComputeRunningProcesses_v2",
                "nvmlDeviceGetComputeRunningProcesses",
            ):
                getter = getattr(pynvml, fn, None)
                if getter is not None:
                    try:
                        procs = getter(h)
                        break
                    except Exception:
                        continue
            if procs is not None:
                hit = False
                for p in procs:
                    if p.pid == my_pid:
                        snap["proc_gpu_mem_mb"] = int(
                            getattr(p, "usedGpuMemory", 0) // (1024 * 1024)
                        )
                        hit = True
                        break
                if not hit:
                    snap["proc_gpu_mem_mb"] = 0
        except Exception:
            pass
    except Exception:
        pass

    try:
        import shutil
        usage = shutil.disk_usage(os.environ.get("PROJECT_ROOT", "/"))
        snap["disk_free_mb"] = int(usage.free // (1024 * 1024))
        snap["disk_total_mb"] = int(usage.total // (1024 * 1024))
    except Exception:
        pass

    return snap


# ---------------------------------------------------------------------------
# URL / token resolution
# ---------------------------------------------------------------------------


def _resolve_monitor_url(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    env = os.environ.get("ARGUS_URL")
    if env:
        return env
    cfg_path = Path("configs/monitor.yaml")
    if cfg_path.is_file() and yaml is not None:
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            url = data.get("url") or data.get("monitor_url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        except Exception:
            pass
    return None


def _resolve_token(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    env = os.environ.get("ARGUS_TOKEN")
    if env:
        return env
    return None


# ---------------------------------------------------------------------------
# Reporter — top-level batch context manager
# ---------------------------------------------------------------------------

class Reporter:
    """Context manager for one experiment batch.

    On ``__enter__`` it emits ``batch_start`` and starts the heartbeat,
    stop-poll, and resource-snapshot daemon threads. On ``__exit__`` it
    emits ``batch_done`` (or ``batch_failed`` if an exception bubbled
    up), stops the daemons, and drains the underlying event queue.

    Parameters
    ----------
    batch_prefix:
        Prefix for the auto-generated batch id (``"<prefix>-<12 hex>"``).
    experiment_type, source_project, command, n_total:
        Forwarded into the ``batch_start`` event.
    heartbeat, stop_polling, resource_snapshot:
        Daemon-thread toggles. ``True`` enables with the default
        interval (300 / 10 / 30 s); ``False`` disables; a numeric value
        overrides the interval (in seconds).
    monitor_url:
        Falls back to env ``ARGUS_URL`` or ``configs/monitor.yaml``.
    token:
        Falls back to env ``ARGUS_TOKEN``.
    auto_upload_dirs:
        Optional list of directories whose matching files (by extension
        in ``{.png,.jpg,.pdf,.svg}``) are uploaded as batch artifacts on
        clean exit. Default ``None`` = no auto upload.
    """

    def __init__(
        self,
        batch_prefix: str = "batch",
        *,
        experiment_type: Optional[str] = None,
        source_project: Optional[str] = None,
        command: Optional[str] = None,
        n_total: Optional[int] = None,
        heartbeat: Union[bool, float] = True,
        stop_polling: Union[bool, float] = True,
        resource_snapshot: Union[bool, float] = True,
        monitor_url: Optional[str] = None,
        token: Optional[str] = None,
        auto_upload_dirs: Optional[Iterable[Union[str, Path]]] = None,
        batch_id: Optional[str] = None,
        resume_from: Optional[str] = None,
        # Test / power-user knobs:
        _reporter: Optional[ExperimentReporter] = None,
    ) -> None:
        # Three-way precedence on the batch id:
        #   1. ``batch_id=...``        — explicit caller-supplied id.
        #   2. ``resume_from=...``     — alias for batch_id, carries the
        #      "resume" intent for the docs / log messages.
        #   3. fall back to a fresh ``<prefix>-<12 hex>`` UUID.
        # Idempotent re-init on the backend (see backend/api/events.py
        # _handle_batch_start) means a resumed run lands on the same
        # Batch row as the original — events append rather than fork.
        explicit = batch_id or resume_from
        if explicit:
            self._batch_id = str(explicit)
        else:
            self._batch_id = new_batch_id(batch_prefix)
        self._experiment_type = experiment_type or "experiment"
        self._source_project = source_project or "default"
        self._command = command
        self._n_total = int(n_total) if n_total is not None else 0
        self._auto_upload_dirs = [Path(p) for p in (auto_upload_dirs or [])]

        self._heartbeat_s = self._coerce_interval(heartbeat, _DEFAULT_HEARTBEAT_S)
        self._stop_poll_s = self._coerce_interval(stop_polling, _DEFAULT_STOP_POLL_S)
        self._resource_s = self._coerce_interval(resource_snapshot, _DEFAULT_RESOURCE_S)

        self._url = _resolve_monitor_url(monitor_url)
        self._token = _resolve_token(token)

        self._stop_evt = threading.Event()
        self._stopped_remote = threading.Event()
        self._threads: List[threading.Thread] = []
        self._t0: float = 0.0
        self._n_done = 0
        self._n_failed = 0
        self._active_job_id: Optional[str] = None
        self._entered = False

        # Underlying low-level reporter (handles queue, retries, spill).
        if _reporter is not None:
            self._rep = _reporter
            # Force the batch id we generated.
            self._rep.current_batch_id = self._batch_id
            self._owns_rep = False
        elif self._url:
            self._rep = ExperimentReporter(
                url=self._url,
                project=self._source_project,
                auth_token=self._token,
                batch_id=self._batch_id,
            )
            self._owns_rep = True
        else:
            # No URL configured — operate in pure no-op mode but keep the
            # public API working so user code doesn't have to branch.
            self._rep = None  # type: ignore[assignment]
            self._owns_rep = False
            logger.warning(
                "Reporter: ARGUS_URL not configured; events will be no-ops"
            )

    # -- public properties ---------------------------------------------------

    @property
    def batch_id(self) -> str:
        return self._batch_id

    @property
    def stopped(self) -> bool:
        """True once the platform's stop button has fired (or local cancel)."""
        return self._stopped_remote.is_set()

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "Reporter":
        if self._entered:
            return self
        self._entered = True
        self._t0 = time.time()
        global _GLOBAL_REPORTER, _GLOBAL_BATCH_ID
        with _GLOBAL_LOCK:
            _GLOBAL_REPORTER = self._rep
            _GLOBAL_BATCH_ID = self._batch_id

        if self._rep is not None:
            try:
                self._rep.batch_start(
                    experiment_type=self._experiment_type,
                    n_total=self._n_total,
                    command=self._command,
                    batch_id=self._batch_id,
                )
            except Exception:  # pragma: no cover
                logger.debug("batch_start emit failed", exc_info=True)

        # Daemons start regardless of whether we have a URL — they all
        # internally check and silently no-op when transport isn't wired.
        self._start_daemons()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._entered:
            return
        # Stop daemons first so they don't keep emitting after exit.
        self._stop_evt.set()
        for t in self._threads:
            try:
                t.join(timeout=2.0)
            except Exception:  # pragma: no cover
                pass
        self._threads.clear()

        elapsed = time.time() - self._t0
        if self._rep is not None:
            try:
                if exc is None:
                    # Auto-upload artifacts on clean exit.
                    if self._auto_upload_dirs:
                        try:
                            self._auto_upload()
                        except Exception:  # pragma: no cover
                            logger.debug("auto_upload failed", exc_info=True)
                    self._rep.batch_done(
                        n_done=self._n_done,
                        n_failed=self._n_failed,
                        total_elapsed_s=elapsed,
                    )
                else:
                    self._rep.batch_failed(
                        reason=f"{exc_type.__name__}: {exc}",
                        total_elapsed_s=elapsed,
                    )
            except Exception:  # pragma: no cover
                logger.debug("batch_done/batch_failed emit failed", exc_info=True)

        # Drain + close the underlying reporter (only if we own it).
        if self._rep is not None and self._owns_rep:
            try:
                self._rep.close(timeout=3.0)
            except Exception:  # pragma: no cover
                logger.debug("reporter close failed", exc_info=True)

        global _GLOBAL_REPORTER, _GLOBAL_BATCH_ID
        with _GLOBAL_LOCK:
            if _GLOBAL_REPORTER is self._rep:
                _GLOBAL_REPORTER = None
            if _GLOBAL_BATCH_ID == self._batch_id:
                _GLOBAL_BATCH_ID = None

    # -- public methods ------------------------------------------------------

    def job(
        self,
        job_id: str,
        *,
        model: Optional[str] = None,
        dataset: Optional[str] = None,
    ) -> "JobContext":
        """Return a :class:`JobContext` to use as ``with r.job(...) as j:``."""
        return JobContext(self, job_id, model=model, dataset=dataset)

    def emit(self, event: str, **fields: Any) -> None:
        """Direct emit — escape hatch for unusual events."""
        if self._rep is None:
            return
        job_id = fields.pop("job_id", None) or self._active_job_id
        try:
            self._rep._emit(  # noqa: SLF001
                event, self._batch_id, fields, job_id=job_id,
            )
        except Exception:  # pragma: no cover
            logger.debug("Reporter.emit failed", exc_info=True)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _coerce_interval(flag: Union[bool, float], default: float) -> float:
        if flag is False:
            return 0.0
        if flag is True:
            return default
        try:
            v = float(flag)
        except (TypeError, ValueError):
            return default
        return max(0.0, v)

    def _start_daemons(self) -> None:
        if self._heartbeat_s > 0:
            t = threading.Thread(
                target=self._heartbeat_loop,
                name=f"reporter-heartbeat[{self._batch_id}]",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        if self._stop_poll_s > 0 and self._url:
            t = threading.Thread(
                target=self._stop_poll_loop,
                name=f"reporter-stop-poll[{self._batch_id}]",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        if self._resource_s > 0:
            t = threading.Thread(
                target=self._resource_loop,
                name=f"reporter-resource[{self._batch_id}]",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _heartbeat_loop(self) -> None:
        while not self._stop_evt.is_set():
            if self._rep is not None:
                try:
                    job_id = self._active_job_id
                    self._rep._emit(  # noqa: SLF001
                        "log_line",  # heartbeat is not in EVENT_TYPES;
                        # backend treats heartbeat-as-log_line gracefully.
                        # We intentionally pick log_line so validate_event
                        # accepts it on schema v1.1.
                        self._batch_id,
                        {
                            "line": "heartbeat",
                            "level": "debug",
                            "ts_unix": time.time(),
                        },
                        job_id=job_id or self._batch_id,
                    )
                except Exception:  # pragma: no cover
                    logger.debug("heartbeat emit failed", exc_info=True)
            self._stop_evt.wait(timeout=self._heartbeat_s)

    def _stop_poll_loop(self) -> None:
        if requests is None or not self._url:
            return
        url = f"{self._url.rstrip('/')}/api/batches/{self._batch_id}/stop-requested"
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        while not self._stop_evt.is_set():
            try:
                r = requests.get(url, headers=headers, timeout=5)
                if r.ok:
                    try:
                        payload = r.json()
                    except Exception:
                        payload = {}
                    if payload.get("stop_requested"):
                        logger.info(
                            "Reporter: stop signal received for batch %s",
                            self._batch_id,
                        )
                        self._stopped_remote.set()
                        return
            except Exception as exc:
                logger.debug("stop poll iteration failed: %s", exc)
            self._stop_evt.wait(timeout=self._stop_poll_s)

    def _resource_loop(self) -> None:
        while not self._stop_evt.is_set():
            if self._rep is not None:
                try:
                    snap = _collect_snapshot()
                    if snap:
                        self._rep.resource_snapshot(**snap)
                except Exception:  # pragma: no cover
                    logger.debug("resource_snapshot emit failed", exc_info=True)
            self._stop_evt.wait(timeout=self._resource_s)

    def _record_job_outcome(self, ok: bool) -> None:
        if ok:
            self._n_done += 1
        else:
            self._n_failed += 1

    def _auto_upload(self) -> int:
        n = 0
        for d in self._auto_upload_dirs:
            n += _upload_paths_for(self, d, glob="**/*", scope_id=self._batch_id,
                                   scope="batches")
        return n


# ---------------------------------------------------------------------------
# JobContext — per-job context manager
# ---------------------------------------------------------------------------

class JobContext:
    """Context manager for a single job inside a :class:`Reporter` batch."""

    def __init__(
        self,
        parent: Reporter,
        job_id: str,
        *,
        model: Optional[str] = None,
        dataset: Optional[str] = None,
    ) -> None:
        self._parent = parent
        self._job_id = job_id
        self._model = model
        self._dataset = dataset
        self._t0: float = 0.0
        self._final_metrics: Dict[str, Any] = {}
        self._epochs_seen = 0
        self._entered = False

    # -- public -------------------------------------------------------------

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def stopped(self) -> bool:
        """Inherits from parent :class:`Reporter`."""
        return self._parent.stopped

    def __enter__(self) -> "JobContext":
        self._entered = True
        self._t0 = time.time()
        self._parent._active_job_id = self._job_id  # noqa: SLF001
        if self._parent._rep is not None:  # noqa: SLF001
            try:
                self._parent._rep.job_start(  # noqa: SLF001
                    job_id=self._job_id,
                    model=self._model,
                    dataset=self._dataset,
                )
            except Exception:  # pragma: no cover
                logger.debug("job_start emit failed", exc_info=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._entered:
            return
        elapsed = time.time() - self._t0
        rep = self._parent._rep  # noqa: SLF001
        if rep is not None:
            try:
                if exc is None:
                    rep.job_done(
                        job_id=self._job_id,
                        metrics=self._final_metrics or None,
                        elapsed_s=elapsed,
                        train_epochs=self._epochs_seen or None,
                    )
                else:
                    rep.job_failed(
                        job_id=self._job_id,
                        reason=f"{exc_type.__name__}: {exc}",
                        elapsed_s=elapsed,
                    )
            except Exception:  # pragma: no cover
                logger.debug("job_done/job_failed emit failed", exc_info=True)
        self._parent._record_job_outcome(exc is None)  # noqa: SLF001
        if self._parent._active_job_id == self._job_id:  # noqa: SLF001
            self._parent._active_job_id = None  # noqa: SLF001

    def epoch(
        self,
        epoch: int,
        *,
        train_loss: Optional[float] = None,
        val_loss: Optional[float] = None,
        lr: Optional[float] = None,
        batch_time_ms: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Emit one ``job_epoch`` event."""
        self._epochs_seen = max(self._epochs_seen, int(epoch) + 1)
        rep = self._parent._rep  # noqa: SLF001
        if rep is None:
            return
        try:
            rep.job_epoch(
                job_id=self._job_id,
                epoch=int(epoch),
                train_loss=train_loss,
                val_loss=val_loss,
                lr=lr,
                batch_time_ms=batch_time_ms,
                **extra,
            )
        except Exception:  # pragma: no cover
            logger.debug("epoch emit failed", exc_info=True)

    def metrics(self, m: Dict[str, float]) -> None:
        """Stash final metrics, surfaced when the job context exits."""
        if not isinstance(m, dict):
            return
        self._final_metrics.update(m)

    def log(self, message: str, level: str = "INFO") -> None:
        """Emit a ``log_line`` event."""
        rep = self._parent._rep  # noqa: SLF001
        if rep is None:
            return
        try:
            rep.log_line(job_id=self._job_id, line=str(message), level=str(level).lower())
        except Exception:  # pragma: no cover
            logger.debug("log emit failed", exc_info=True)

    def upload(self, path: Union[str, Path], *, glob: str = "**/*.png") -> int:
        """Upload artifacts from a file or directory.

        For a directory, ``glob`` selects which files to upload; default
        ``**/*.png``. Files outside ``{.png,.jpg,.pdf,.svg}`` are skipped.
        Auto-skips when the monitor isn't reachable. Returns the number
        of files uploaded.
        """
        return _upload_paths_for(
            self._parent, Path(path), glob=glob,
            scope_id=self._job_id, scope="jobs",
        )


# ---------------------------------------------------------------------------
# Artifact upload helper (shared by Reporter.auto_upload + JobContext.upload)
# ---------------------------------------------------------------------------

def _upload_paths_for(
    reporter: Reporter,
    path: Path,
    *,
    glob: str,
    scope_id: str,
    scope: str,
) -> int:
    if reporter._rep is None or not reporter._url:  # noqa: SLF001
        return 0
    if requests is None:
        return 0
    if not path.exists():
        logger.debug("upload: %s does not exist", path)
        return 0

    targets: List[Path] = []
    if path.is_file():
        targets.append(path)
    else:
        for p in sorted(path.glob(glob)):
            if p.is_file():
                targets.append(p)

    url = f"{reporter._url.rstrip('/')}/api/{scope}/{scope_id}/artifacts"  # noqa: SLF001
    headers: Dict[str, str] = {}
    if reporter._token:  # noqa: SLF001
        headers["Authorization"] = f"Bearer {reporter._token}"  # noqa: SLF001

    n_ok = 0
    for p in targets:
        if p.suffix.lower() not in _ARTIFACT_EXTS:
            continue
        try:
            with p.open("rb") as fh:
                r = requests.post(
                    url,
                    headers=headers,
                    files={"file": (p.name, fh)},
                    data={"kind": "image"},
                    timeout=30,
                )
            if r.ok:
                n_ok += 1
            else:
                logger.debug(
                    "upload %s -> HTTP %s %s",
                    p.name, r.status_code, (r.text or "")[:200],
                )
        except Exception as exc:
            logger.debug("upload %s failed: %s", p, exc)
    return n_ok


__all__ = [
    "Reporter",
    "JobContext",
    "emit",
    "new_batch_id",
    "set_batch_id",
    "get_batch_id",
    "sub_env",
]
