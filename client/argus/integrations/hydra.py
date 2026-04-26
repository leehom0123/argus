"""Hydra integration for Argus.

Drop-in :class:`hydra.experimental.callback.Callback` that wraps
:class:`argus.Reporter` and emits standard Argus events from any
``@hydra.main`` entry point with no boilerplate. Wire it in via
``hydra.callbacks`` in your config::

    # configs/config.yaml
    hydra:
      callbacks:
        argus:
          _target_: argus.integrations.hydra.ArgusCallback
          project: deepts
          experiment_type: forecast

    # main.py — unchanged, no SDK code required.

The callback drives one ``Reporter`` (the *batch*) per ``@hydra.main``
invocation and one ``JobContext`` per Hydra job. Hook coverage:

* ``on_run_start``       -> ``Reporter.__enter__`` (single-run mode only)
* ``on_multirun_start``  -> ``Reporter.__enter__`` (multirun mode)
* ``on_job_start``       -> ``JobContext.__enter__`` (fires for *every* job in
  both modes; ``job_id`` defaults to ``HydraConfig.job.num`` cast to str)
* ``on_job_end``         -> ``JobContext.__exit__`` (clean or failure depending
  on ``job_return.return_value`` / ``status``)
* ``on_run_end``         -> ``Reporter.__exit__`` (single-run mode only)
* ``on_multirun_end``    -> ``Reporter.__exit__`` (multirun mode)

Single-run mode (``python main.py``) emits 1 batch with 1 job. Multirun
mode (``python main.py -m``) emits 1 batch with N jobs, one per Hydra
trial. The global ``batch_id`` is set on enter so module-level
:func:`argus.emit` works without a Reporter handle.

Hydra version compatibility: works on hydra-core 1.3.x and 1.4+. The
``hydra.experimental.callback.Callback`` base has been stable since the
callback feature shipped in 1.1; the hooks listed above are stable
across all callback-supporting versions.

Optional dependency: install with ``pip install argus-reporter[hydra]``
to pull in ``hydra-core>=1.3``. The import here is lazy — users without
Hydra installed are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..context import JobContext, Reporter, set_batch_id

logger = logging.getLogger("argus.integrations.hydra")


def _resolve_hydra_callback_base():
    """Lazily import :class:`hydra.experimental.callback.Callback`.

    Raises ``ImportError`` with a helpful install hint if hydra-core is
    not available.
    """
    try:
        from hydra.experimental.callback import Callback  # type: ignore
        return Callback
    except Exception as exc:  # pragma: no cover - tested via mocking
        raise ImportError(
            "argus.integrations.hydra requires hydra-core. "
            "Install with: pip install 'argus-reporter[hydra]'"
        ) from exc


def _resolve_job_num() -> Optional[int]:
    """Best-effort lookup of the running Hydra job number.

    Returns ``None`` if Hydra's runtime config isn't initialised yet
    (e.g. during early hooks of a custom test harness). The caller
    should fall back to a synthetic counter in that case.
    """
    try:
        from hydra.core.hydra_config import HydraConfig  # type: ignore
        return int(HydraConfig.get().job.num)
    except Exception:
        return None


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a Hydra ``DictConfig`` or any object/dict."""
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    val = getattr(config, key, None)
    if val is None:
        # OmegaConf DictConfig also supports __getitem__
        try:
            val = config[key]  # type: ignore[index]
        except Exception:
            val = None
    return val if val is not None else default


_Base = None  # populated lazily on first instantiation


def _ensure_base() -> type:
    global _Base
    if _Base is None:
        _Base = _resolve_hydra_callback_base()
    return _Base


class ArgusCallback:  # actual base injected via __new__ trick
    """Hydra callback that auto-emits Argus events.

    Parameters
    ----------
    project:
        Argus ``source_project`` (logical group / repo name).
    experiment_type:
        Forwarded to ``Reporter`` (default ``"hydra"``).
    argus_url:
        Argus server base URL. Falls back to env ``ARGUS_URL``.
    token:
        Bearer token. Falls back to env ``ARGUS_TOKEN``.
    batch_prefix:
        Prefix for the auto-generated batch id. Defaults to
        ``config.experiment_name`` if set, else ``project``.
    job_id_key:
        Name of the config field to use as ``job_id``. When unset,
        ``HydraConfig.job.num`` is used (str-cast).
    job_id_template:
        Optional ``str.format``-style template combined with
        ``HydraConfig.job.num`` and config fields, e.g.
        ``"{experiment_name}-{job_num}"``. Overrides ``job_id_key``.
    heartbeat, stop_polling, resource_snapshot:
        Forwarded to :class:`Reporter`. Default ``False`` to keep
        Hydra runs lightweight; users can opt back in.
    auto_upload_dirs:
        Optional list of directories whose images/PDFs are uploaded
        as batch artifacts on clean exit.
    """

    def __new__(cls, *args, **kwargs):  # noqa: D401
        # Late-bind the Hydra Callback base into our MRO so that
        # ``isinstance(cb, hydra.experimental.callback.Callback)`` is true
        # and Hydra's callback dispatcher accepts the instance, while we
        # still fail with a clear ImportError when hydra-core is missing.
        if cls is ArgusCallback:
            base = _ensure_base()
            real_cls = type(cls.__name__, (cls, base), {})
            return object.__new__(real_cls)
        return object.__new__(cls)

    def __init__(
        self,
        *,
        project: str,
        experiment_type: str = "hydra",
        argus_url: Optional[str] = None,
        token: Optional[str] = None,
        batch_prefix: Optional[str] = None,
        job_id_key: Optional[str] = None,
        job_id_template: Optional[str] = None,
        heartbeat: Any = False,
        stop_polling: Any = False,
        resource_snapshot: Any = False,
        auto_upload_dirs: Optional[list] = None,
        # Test seam: inject a pre-built Reporter (skips URL resolution).
        _reporter: Optional[Reporter] = None,
    ) -> None:
        # Avoid calling base __init__ — Hydra's Callback has no required ctor.
        self._project = project
        self._experiment_type = experiment_type
        self._argus_url = argus_url
        self._token = token
        self._batch_prefix = batch_prefix
        self._job_id_key = job_id_key
        self._job_id_template = job_id_template
        self._heartbeat = heartbeat
        self._stop_polling = stop_polling
        self._resource_snapshot = resource_snapshot
        self._auto_upload_dirs = list(auto_upload_dirs) if auto_upload_dirs else None
        self._injected_reporter = _reporter

        # Runtime state.
        self._reporter: Optional[Reporter] = None
        self._job: Optional[JobContext] = None
        self._is_multirun: bool = False
        # Synthetic counter when HydraConfig isn't available (e.g. tests).
        self._job_counter: int = 0

    # ------------------------------------------------------------------ #
    # Hydra hooks — single-run flow
    # ------------------------------------------------------------------ #

    def on_run_start(self, config: Any, **kwargs: Any) -> None:
        # Only enter the reporter here in single-run mode. Multirun
        # opens it in on_multirun_start before any job fires.
        if self._reporter is None:
            self._open_reporter(config)

    def on_run_end(self, config: Any, **kwargs: Any) -> None:
        if self._is_multirun:
            return
        self._close_reporter(exc_type=None, exc=None, tb=None)

    # ------------------------------------------------------------------ #
    # Hydra hooks — multirun flow
    # ------------------------------------------------------------------ #

    def on_multirun_start(self, config: Any, **kwargs: Any) -> None:
        self._is_multirun = True
        self._open_reporter(config)

    def on_multirun_end(self, config: Any, **kwargs: Any) -> None:
        self._close_reporter(exc_type=None, exc=None, tb=None)

    # ------------------------------------------------------------------ #
    # Hydra hooks — per-job flow (fires in BOTH single-run and multirun)
    # ------------------------------------------------------------------ #

    def on_job_start(self, config: Any, **kwargs: Any) -> None:
        if self._reporter is None:
            # Defensive: Hydra docs guarantee on_run_start /
            # on_multirun_start fires first, but custom test harnesses
            # may skip ahead. Open lazily so we never drop a job.
            self._open_reporter(config)
        if self._job is not None:
            # Should never happen — Hydra serialises jobs within a
            # process. Guard anyway: close the dangling job cleanly.
            try:
                self._job.__exit__(None, None, None)
            except Exception:  # pragma: no cover
                logger.debug("dangling JobContext exit failed", exc_info=True)
            self._job = None

        job_id = self._compute_job_id(config)
        model = _config_get(config, "model", None)
        if hasattr(model, "name"):  # DictConfig with model.name
            model = model.name  # type: ignore[assignment]
        elif isinstance(model, dict):
            model = model.get("name")
        else:
            model = None if not isinstance(model, str) else model
        dataset = _config_get(config, "dataset", None)
        if hasattr(dataset, "name"):
            dataset = dataset.name  # type: ignore[assignment]
        elif isinstance(dataset, dict):
            dataset = dataset.get("name")
        elif not isinstance(dataset, str):
            dataset = None

        self._job = self._reporter.job(job_id, model=model, dataset=dataset)
        self._job.__enter__()
        self._job_counter += 1

    def on_job_end(
        self,
        config: Any,
        job_return: Any = None,
        **kwargs: Any,
    ) -> None:
        if self._job is None:
            return
        # Hydra's JobReturn carries ``status`` (JobStatus.COMPLETED /
        # FAILED) and ``_return_value`` (which is the exception object
        # when status==FAILED). We propagate failures so JobContext
        # emits ``job_failed`` instead of ``job_done``.
        exc_type = exc = tb = None
        status = getattr(job_return, "status", None)
        status_name = getattr(status, "name", None) or str(status or "")
        if status_name.upper() == "FAILED":
            ret = getattr(job_return, "_return_value", None)
            if ret is None:
                ret = getattr(job_return, "return_value", None)
            if isinstance(ret, BaseException):
                exc = ret
                exc_type = type(ret)
                tb = ret.__traceback__
            else:
                exc = RuntimeError(f"Hydra job failed: {ret!r}")
                exc_type = RuntimeError
        try:
            self._job.__exit__(exc_type, exc, tb)
        except Exception:  # pragma: no cover
            logger.debug("JobContext exit failed", exc_info=True)
        self._job = None

    # ------------------------------------------------------------------ #
    # Safety: keep tokens out of logs / pickles
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(project={self._project!r}, "
            f"experiment_type={self._experiment_type!r}, "
            f"token='<redacted>')"
        )

    def __reduce__(self):
        """Block pickling — tokens shouldn't survive serialization."""
        raise TypeError(
            f"{type(self).__name__} is not pickleable (would expose token). "
            f"Recreate the callback after deserialization."
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _compute_job_id(self, config: Any) -> str:
        """Derive a stable job id, in priority order.

        1. ``job_id_template`` (str.format with all top-level config
           keys + ``job_num``).
        2. ``config[job_id_key]`` if ``job_id_key`` was passed.
        3. ``HydraConfig.job.num`` as str.
        4. Synthetic counter (only when Hydra runtime isn't available).
        """
        if self._job_id_template:
            num = _resolve_job_num()
            if num is None:
                num = self._job_counter
            ctx = {"job_num": num}
            try:
                # Add top-level config fields when possible. DictConfig
                # supports .items(); plain objects expose attributes via
                # __dict__; dicts expose .items() too.
                items = None
                if hasattr(config, "items") and callable(config.items):
                    items = list(config.items())
                elif hasattr(config, "__dict__"):
                    items = list(vars(config).items())
                elif hasattr(config, "keys"):
                    items = [(k, _config_get(config, k)) for k in config.keys()]  # type: ignore[attr-defined]
                if items:
                    for k, v in items:
                        if isinstance(v, (str, int, float, bool)):
                            ctx.setdefault(k, v)
            except Exception:
                pass
            try:
                return self._job_id_template.format(**ctx)
            except Exception:
                logger.debug(
                    "job_id_template format failed; falling back",
                    exc_info=True,
                )
        if self._job_id_key:
            v = _config_get(config, self._job_id_key)
            if v is not None:
                return str(v)
        num = _resolve_job_num()
        if num is None:
            num = self._job_counter
        return str(num)

    def _open_reporter(self, config: Any) -> None:
        if self._reporter is not None:
            return
        if self._injected_reporter is not None:
            self._reporter = self._injected_reporter
        else:
            prefix = (
                self._batch_prefix
                or _config_get(config, "experiment_name", None)
                or self._project
            )
            self._reporter = Reporter(
                batch_prefix=str(prefix),
                experiment_type=self._experiment_type,
                source_project=self._project,
                heartbeat=self._heartbeat,
                stop_polling=self._stop_polling,
                resource_snapshot=self._resource_snapshot,
                monitor_url=self._argus_url,
                token=self._token,
                auto_upload_dirs=self._auto_upload_dirs,
            )
        self._reporter.__enter__()
        # Surface batch id globally so module-level emit() works deep
        # inside user training code without a Reporter handle.
        try:
            set_batch_id(self._reporter.batch_id)
        except Exception:  # pragma: no cover
            logger.debug("set_batch_id failed", exc_info=True)

    def _close_reporter(self, *, exc_type, exc, tb) -> None:
        # Defensive: close any still-open job (shouldn't happen if
        # Hydra called on_job_end first, but tolerate misuse).
        if self._job is not None:
            try:
                self._job.__exit__(exc_type, exc, tb)
            except Exception:  # pragma: no cover
                logger.debug("late JobContext exit failed", exc_info=True)
            self._job = None
        if self._reporter is not None:
            try:
                self._reporter.__exit__(exc_type, exc, tb)
            except Exception:  # pragma: no cover
                logger.debug("Reporter exit failed", exc_info=True)
            self._reporter = None


__all__ = ["ArgusCallback"]
