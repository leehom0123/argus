"""Keras integration for Argus.

Drop-in :class:`keras.callbacks.Callback` that wraps :class:`argus.Reporter`
and emits standard Argus events from your existing ``model.fit()`` flow
without any boilerplate::

    from argus.integrations.keras import ArgusCallback

    cb = ArgusCallback(
        project="my-proj",
        job_id="mnist_cnn",
        argus_url="http://argus.example.com",
        token="em_live_xxx",
    )
    model.fit(x, y, epochs=10, callbacks=[cb])

The callback drives one ``Reporter`` and one ``JobContext`` for the lifetime
of ``model.fit()``. Hook coverage:

* ``on_train_begin``  -> ``Reporter.__enter__`` + ``JobContext.__enter__``
* ``on_epoch_end``    -> ``JobContext.epoch`` (``job_epoch`` from ``logs``)
* ``on_train_end``    -> ``JobContext.__exit__`` + ``Reporter.__exit__``

Keras has no dedicated ``on_exception`` hook; if ``model.fit()`` raises,
the callback's destructor will best-effort emit ``job_failed`` /
``batch_failed`` so the run isn't left "in progress" on the dashboard.
For deterministic failure reporting, wrap ``fit()`` in ``try/except`` and
call :meth:`ArgusCallback.report_failure` from the ``except`` branch.

Keras compatibility: works with both Keras 3 (``import keras``) and the
TF-bundled Keras 2 (``tensorflow.keras``). The import is lazy — users
without either installed are unaffected.

Optional dependency: install with ``pip install argus-reporter[keras]``
to pull in ``keras>=2.10``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..context import JobContext, Reporter

logger = logging.getLogger("argus.integrations.keras")


def _resolve_keras_callback_base():
    """Lazily import ``Callback`` from ``keras`` (Keras 3) or ``tensorflow.keras``.

    Tries the stand-alone Keras package first (Keras 3 / multi-backend), then
    falls back to the TF-bundled Keras 2 namespace. Returns the ``Callback``
    class. Raises ``ImportError`` with a helpful install hint if neither is
    importable.
    """
    try:
        from keras.callbacks import Callback  # type: ignore
        return Callback
    except Exception:
        pass
    try:
        from tensorflow.keras.callbacks import Callback  # type: ignore
        return Callback
    except Exception as exc:  # pragma: no cover - tested via mocking
        raise ImportError(
            "argus.integrations.keras requires keras (or tensorflow with "
            "tf.keras). Install with: pip install 'argus-reporter[keras]'"
        ) from exc


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort tensor/number -> python float; ``None`` on failure."""
    if value is None:
        return None
    # Tensor / EagerTensor / numpy scalar all expose ``.item`` or ``__float__``.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return float(item())
        except Exception:
            pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_metric(logs: Dict[str, Any], *names: str) -> Optional[float]:
    """Pick the first key in ``names`` whose value coerces to float."""
    if not isinstance(logs, dict):
        return None
    for name in names:
        if name in logs:
            v = _coerce_float(logs[name])
            if v is not None:
                return v
    return None


_Base = None  # populated lazily on first instantiation


def _ensure_base() -> type:
    global _Base
    if _Base is None:
        _Base = _resolve_keras_callback_base()
    return _Base


class ArgusCallback:  # actual base injected via __new__ trick
    """Keras callback that auto-emits Argus events.

    Parameters
    ----------
    project:
        Argus ``source_project`` (logical group / repo name).
    job_id:
        Identifier for this run, e.g. ``"mnist_cnn"``.
    argus_url:
        Argus server base URL. Falls back to env ``ARGUS_URL``.
    token:
        Bearer token. Falls back to env ``ARGUS_TOKEN``.
    model, dataset:
        Optional metadata propagated into ``job_start``.
    experiment_type:
        Forwarded to ``Reporter`` (default ``"keras"``).
    batch_prefix:
        Prefix for the auto-generated batch id (default ``"keras"``).
    train_loss_keys, val_loss_keys:
        Ordered tuples of metric names to look up in the per-epoch ``logs``
        dict. The first key whose value coerces to float is reported.
    final_metric_keys:
        Metrics to stash via :meth:`JobContext.metrics` at clean
        ``on_train_end``. Each value is read from the latest epoch's logs.
    heartbeat, stop_polling, resource_snapshot:
        Forwarded to :class:`Reporter`. Default ``False`` to keep
        Keras runs lightweight; users can opt back in.
    auto_upload_dirs:
        Optional list of directories whose images/PDFs are uploaded as
        batch artifacts on clean exit.
    """

    def __new__(cls, *args, **kwargs):  # noqa: D401
        # Late-bind the Keras Callback base into our MRO so that
        # ``isinstance(cb, keras.callbacks.Callback)`` is true and Keras's
        # callback dispatcher accepts the instance, while we still fail
        # with a clear ImportError when neither Keras nor tf.keras is
        # available.
        if cls is ArgusCallback:
            base = _ensure_base()
            real_cls = type(cls.__name__, (cls, base), {})
            return object.__new__(real_cls)
        return object.__new__(cls)

    def __init__(
        self,
        *,
        project: str,
        job_id: str,
        argus_url: Optional[str] = None,
        token: Optional[str] = None,
        model: Optional[str] = None,
        dataset: Optional[str] = None,
        experiment_type: str = "keras",
        batch_prefix: str = "keras",
        train_loss_keys: tuple = ("loss",),
        val_loss_keys: tuple = ("val_loss",),
        final_metric_keys: tuple = (),
        heartbeat: Any = False,
        stop_polling: Any = False,
        resource_snapshot: Any = False,
        auto_upload_dirs: Optional[list] = None,
        # Test seam: inject a pre-built Reporter (skips URL resolution).
        _reporter: Optional[Reporter] = None,
    ) -> None:
        # Don't call base __init__ — Keras Callback's __init__ is no-op
        # but pinned to a specific signature; we sidestep it to keep our
        # constructor a clean kw-only API.
        self._project = project
        self._job_id = job_id
        self._model_name = model
        self._dataset = dataset
        self._argus_url = argus_url
        self._token = token
        self._experiment_type = experiment_type
        self._batch_prefix = batch_prefix
        self._train_loss_keys = tuple(train_loss_keys)
        self._val_loss_keys = tuple(val_loss_keys)
        self._final_metric_keys = tuple(final_metric_keys)
        self._heartbeat = heartbeat
        self._stop_polling = stop_polling
        self._resource_snapshot = resource_snapshot
        self._auto_upload_dirs = list(auto_upload_dirs) if auto_upload_dirs else None
        self._injected_reporter = _reporter

        self._reporter: Optional[Reporter] = None
        self._job: Optional[JobContext] = None
        self._last_logs: Dict[str, Any] = {}
        self._closed: bool = False

    # ------------------------------------------------------------------ #
    # Keras hooks
    # ------------------------------------------------------------------ #

    def on_train_begin(self, logs: Optional[Dict[str, Any]] = None) -> None:
        # ``self.params`` is set by Keras before this hook fires and contains
        # ``{'epochs': N, ...}`` for both Keras 2 and Keras 3.
        params = getattr(self, "params", None) or {}
        try:
            n_total = int(params.get("epochs") or 0)
        except Exception:
            n_total = 0

        if self._injected_reporter is not None:
            self._reporter = self._injected_reporter
        else:
            self._reporter = Reporter(
                batch_prefix=self._batch_prefix,
                experiment_type=self._experiment_type,
                source_project=self._project,
                n_total=n_total,
                heartbeat=self._heartbeat,
                stop_polling=self._stop_polling,
                resource_snapshot=self._resource_snapshot,
                monitor_url=self._argus_url,
                token=self._token,
                auto_upload_dirs=self._auto_upload_dirs,
            )
        self._reporter.__enter__()
        self._job = self._reporter.job(
            self._job_id, model=self._model_name, dataset=self._dataset
        )
        self._job.__enter__()
        self._closed = False

    def on_epoch_end(
        self,
        epoch: int,
        logs: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._job is None:
            return
        logs = dict(logs or {})
        self._last_logs = logs
        train_loss = _extract_metric(logs, *self._train_loss_keys)
        val_loss = _extract_metric(logs, *self._val_loss_keys)
        lr = _coerce_float(logs.get("lr") or logs.get("learning_rate"))
        # If lr wasn't surfaced via logs (Keras 2 default), peek at the
        # optimizer attached via ``self.model``.
        if lr is None:
            mdl = getattr(self, "model", None)
            opt = getattr(mdl, "optimizer", None) if mdl is not None else None
            lr_attr = getattr(opt, "learning_rate", None) if opt is not None else None
            if lr_attr is None:
                lr_attr = getattr(opt, "lr", None) if opt is not None else None
            lr = _coerce_float(lr_attr)
        # Forward any extra numeric metrics so the platform sees them too.
        extra: Dict[str, float] = {}
        for k, v in logs.items():
            if k in {"loss", "val_loss", "lr", "learning_rate"}:
                continue
            f = _coerce_float(v)
            if f is not None:
                extra[k] = f
        self._job.epoch(
            int(epoch),
            train_loss=train_loss,
            val_loss=val_loss,
            lr=lr,
            **extra,
        )

    def on_train_end(self, logs: Optional[Dict[str, Any]] = None) -> None:
        # Stash final metrics from the last epoch's logs.
        if self._job is not None and self._final_metric_keys:
            payload: Dict[str, float] = {}
            source = dict(logs or {}) or self._last_logs
            for k in self._final_metric_keys:
                v = _coerce_float(source.get(k))
                if v is not None:
                    payload[k] = v
            if payload:
                self._job.metrics(payload)
        self._close(exc_type=None, exc=None, tb=None)

    # ------------------------------------------------------------------ #
    # Public failure-report helper (Keras has no on_exception hook)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Safety: keep tokens out of logs / pickles
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(project={self._project!r}, "
            f"job_id={self._job_id!r}, token='<redacted>')"
        )

    def __reduce__(self):
        """Block pickling — tokens shouldn't survive serialization."""
        raise TypeError(
            f"{type(self).__name__} is not pickleable (would expose token). "
            f"Recreate the callback after deserialization."
        )

    def report_failure(self, exception: BaseException) -> None:
        """Emit ``job_failed`` + ``batch_failed`` and tear down.

        Call from a user ``try/except`` around ``model.fit()`` to ensure
        the dashboard reflects the failure deterministically. Safe to
        call after a clean ``on_train_end`` (no-op).
        """
        if self._closed:
            return
        self._close(
            exc_type=type(exception),
            exc=exception,
            tb=exception.__traceback__,
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _close(self, *, exc_type, exc, tb) -> None:
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            try:
                self._job.__exit__(exc_type, exc, tb)
            except Exception:  # pragma: no cover
                logger.debug("JobContext exit failed", exc_info=True)
            self._job = None
        if self._reporter is not None:
            try:
                self._reporter.__exit__(exc_type, exc, tb)
            except Exception:  # pragma: no cover
                logger.debug("Reporter exit failed", exc_info=True)
            self._reporter = None

    def __del__(self):  # pragma: no cover - best-effort cleanup
        # If user's fit() raised and they didn't call report_failure(),
        # at least mark the run failed instead of leaving it hanging.
        try:
            if not self._closed and self._job is not None:
                exc = RuntimeError(
                    "Keras training did not reach on_train_end "
                    "(likely raised); call ArgusCallback.report_failure "
                    "from your except branch for accurate diagnostics."
                )
                self._close(exc_type=type(exc), exc=exc, tb=None)
        except Exception:
            pass


__all__ = ["ArgusCallback"]
