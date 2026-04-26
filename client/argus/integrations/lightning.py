"""PyTorch Lightning integration for Argus.

Drop-in :class:`pytorch_lightning.Callback` that wraps :class:`argus.Reporter`
and emits standard Argus events from your existing Trainer flow without any
boilerplate::

    import pytorch_lightning as pl
    from argus.integrations.lightning import ArgusCallback

    trainer = pl.Trainer(callbacks=[ArgusCallback(
        project="my-proj",
        job_id="etth1_dlinear",
        argus_url="http://argus.example.com",
        token="em_live_xxx",
    )])
    trainer.fit(model, datamodule)

The callback drives one ``Reporter`` and one ``JobContext`` for the lifetime
of ``trainer.fit()``. Hook coverage:

* ``on_train_start``        -> ``Reporter.__enter__`` + ``JobContext.__enter__`` (``batch_start`` + ``job_start``)
* ``on_train_epoch_end``    -> ``JobContext.epoch`` (``job_epoch`` with ``train_loss``, ``lr``)
* ``on_validation_epoch_end`` -> ``JobContext.epoch`` (``job_epoch`` with ``val_loss``)
* ``on_train_end``          -> ``JobContext.__exit__`` (clean) + ``Reporter.__exit__`` (``job_done`` + ``batch_done``)
* ``on_exception``          -> ``JobContext.__exit__`` (failure) + ``Reporter.__exit__`` (``job_failed`` + ``batch_failed``)

Lightning version compatibility: works on Lightning 1.9+ and 2.x. The hook
names listed above are stable across both major versions.

Optional dependency: install with ``pip install argus-reporter[lightning]``
to pull in ``pytorch-lightning>=1.9``. The import here is lazy so users
without Lightning installed are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..context import JobContext, Reporter

logger = logging.getLogger("argus.integrations.lightning")


def _resolve_lightning_callback_base():
    """Lazily import ``Callback`` from ``pytorch_lightning`` or ``lightning.pytorch``.

    Lightning 2.0 reorganized the namespace into ``lightning.pytorch`` while
    keeping the ``pytorch_lightning`` shim, so we try both in order.
    Returns the ``Callback`` class. Raises ``ImportError`` with a helpful
    install hint if neither is importable.
    """
    try:
        from pytorch_lightning import Callback  # type: ignore
        return Callback
    except Exception:
        pass
    try:
        from lightning.pytorch import Callback  # type: ignore
        return Callback
    except Exception as exc:  # pragma: no cover - tested via mocking
        raise ImportError(
            "argus.integrations.lightning requires pytorch-lightning. "
            "Install with: pip install 'argus-reporter[lightning]'"
        ) from exc


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort tensor/number -> python float; ``None`` on failure."""
    if value is None:
        return None
    # Tensor (1-element). We avoid importing torch at module load time.
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


def _extract_metric(metrics: Dict[str, Any], *names: str) -> Optional[float]:
    """Pick the first key in ``names`` whose value coerces to float."""
    if not isinstance(metrics, dict):
        return None
    for name in names:
        if name in metrics:
            v = _coerce_float(metrics[name])
            if v is not None:
                return v
    return None


def _extract_lr(trainer: Any) -> Optional[float]:
    """Read first param-group lr from ``trainer.optimizers``."""
    opts = getattr(trainer, "optimizers", None) or []
    for opt in opts:
        groups = getattr(opt, "param_groups", None) or []
        for g in groups:
            lr = g.get("lr") if isinstance(g, dict) else None
            v = _coerce_float(lr)
            if v is not None:
                return v
    return None


_Base = None  # populated lazily on first instantiation


def _ensure_base() -> type:
    global _Base
    if _Base is None:
        _Base = _resolve_lightning_callback_base()
    return _Base


class ArgusCallback:  # actual base injected via __init_subclass__-like trick
    """PyTorch Lightning callback that auto-emits Argus events.

    Parameters
    ----------
    project:
        Argus ``source_project`` (logical group / repo name).
    job_id:
        Identifier for this run, e.g. ``"etth1_dlinear"``.
    argus_url:
        Argus server base URL. Falls back to env ``ARGUS_URL``.
    token:
        Bearer token. Falls back to env ``ARGUS_TOKEN``.
    model, dataset:
        Optional metadata propagated into ``job_start``.
    experiment_type:
        Forwarded to ``Reporter`` (default ``"lightning"``).
    batch_prefix:
        Prefix for the auto-generated batch id (default ``"lightning"``).
    train_loss_keys, val_loss_keys:
        Ordered tuples of metric names to look up in
        ``trainer.callback_metrics``. The first key whose value coerces
        to float is reported.
    final_metric_keys:
        Metrics to stash via :meth:`JobContext.metrics` at clean
        ``on_train_end``. Each value is read from
        ``trainer.callback_metrics`` and coerced to float.
    heartbeat, stop_polling, resource_snapshot:
        Forwarded to :class:`Reporter`. Default ``False`` to keep
        Lightning runs lightweight; users can opt back in.
    auto_upload_dirs:
        Optional list of directories whose images/PDFs are uploaded as
        batch artifacts on clean exit.
    """

    def __new__(cls, *args, **kwargs):  # noqa: D401
        # Late-bind the Lightning Callback base into our MRO so that
        # ``isinstance(cb, pytorch_lightning.Callback)`` is true and
        # Lightning's callback connector accepts the instance, while we
        # still fail with a clear ImportError when Lightning is missing.
        if cls is ArgusCallback:
            base = _ensure_base()
            # Build a one-time subclass with the real base injected.
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
        experiment_type: str = "lightning",
        batch_prefix: str = "lightning",
        train_loss_keys: tuple = ("train_loss_epoch", "train_loss", "loss"),
        val_loss_keys: tuple = ("val_loss_epoch", "val_loss"),
        final_metric_keys: tuple = (),
        heartbeat: Any = False,
        stop_polling: Any = False,
        resource_snapshot: Any = False,
        auto_upload_dirs: Optional[list] = None,
        # Test seam: inject a pre-built Reporter (skips URL resolution).
        _reporter: Optional[Reporter] = None,
    ) -> None:
        # Avoid calling base __init__ — Lightning's Callback has none.
        self._project = project
        self._job_id = job_id
        self._model = model
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
        # Track the last epoch index for which we've already emitted a
        # ``job_epoch`` event. PL 2.x fires ``on_validation_epoch_end``
        # BEFORE ``on_train_epoch_end`` within the same epoch, so a
        # one-sided guard would still let both hooks emit. Both hooks
        # consult this counter before emitting and bump it after.
        self._last_emitted_epoch: int = -1

    # ------------------------------------------------------------------ #
    # Lightning hooks
    # ------------------------------------------------------------------ #

    def on_train_start(self, trainer: Any, pl_module: Any) -> None:  # noqa: D401
        try:
            n_total = int(getattr(trainer, "max_epochs", 0) or 0)
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
            self._job_id, model=self._model, dataset=self._dataset
        )
        self._job.__enter__()

    def on_train_epoch_end(self, trainer: Any, pl_module: Any) -> None:
        if self._job is None:
            return
        epoch = int(getattr(trainer, "current_epoch", 0) or 0)
        # Symmetric dedup: if validation already emitted for this epoch
        # (PL 2.x fires on_validation_epoch_end BEFORE on_train_epoch_end),
        # skip — we'd double-count otherwise.
        if epoch <= self._last_emitted_epoch:
            return
        metrics = dict(getattr(trainer, "callback_metrics", {}) or {})
        train_loss = _extract_metric(metrics, *self._train_loss_keys)
        val_loss = _extract_metric(metrics, *self._val_loss_keys)
        lr = _extract_lr(trainer)
        self._job.epoch(
            epoch, train_loss=train_loss, val_loss=val_loss, lr=lr
        )
        self._last_emitted_epoch = epoch

    def on_validation_epoch_end(self, trainer: Any, pl_module: Any) -> None:
        if self._job is None:
            return
        # Skip Lightning's pre-train sanity validation (current_epoch=0
        # but global_step still 0).
        if getattr(trainer, "sanity_checking", False):
            return
        epoch = int(getattr(trainer, "current_epoch", 0) or 0)
        # Symmetric dedup: if we've already emitted for this epoch
        # (either via the train hook earlier in the epoch, or via a
        # prior validation pass), skip.
        if epoch <= self._last_emitted_epoch:
            return
        metrics = dict(getattr(trainer, "callback_metrics", {}) or {})
        val_loss = _extract_metric(metrics, *self._val_loss_keys)
        if val_loss is None:
            return
        # Also surface train_loss/lr if Lightning has already populated
        # them (PL 2.x val-before-train order means callback_metrics
        # often carries the previous epoch's train_loss; emit it so the
        # platform sees a complete row in either ordering).
        train_loss = _extract_metric(metrics, *self._train_loss_keys)
        self._job.epoch(
            epoch,
            train_loss=train_loss,
            val_loss=val_loss,
            lr=_extract_lr(trainer),
        )
        self._last_emitted_epoch = epoch

    def on_train_end(self, trainer: Any, pl_module: Any) -> None:
        # Stash final metrics from the trainer's callback_metrics.
        if self._job is not None and self._final_metric_keys:
            metrics = dict(getattr(trainer, "callback_metrics", {}) or {})
            payload: Dict[str, float] = {}
            for k in self._final_metric_keys:
                v = _coerce_float(metrics.get(k))
                if v is not None:
                    payload[k] = v
            if payload:
                self._job.metrics(payload)
        self._close(exc_type=None, exc=None, tb=None)

    def on_exception(self, trainer: Any, pl_module: Any, exception: BaseException) -> None:
        self._close(
            exc_type=type(exception), exc=exception, tb=exception.__traceback__,
        )

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

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _close(self, *, exc_type, exc, tb) -> None:
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


__all__ = ["ArgusCallback"]
