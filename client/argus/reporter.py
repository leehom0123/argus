"""ExperimentReporter: fire-and-forget client for Argus (schema v1.1).

Threading model:
- Every public method builds a dict and pushes it onto a bounded queue.
- A single daemon worker thread drains the queue and POSTs to the backend.
    * Single event  -> POST {url}/api/events
    * Burst / queued batch / spill replay -> POST {url}/api/events/batch
- POST failures:
    * 2xx: accept, done.
    * 401 / 403: log error ("Invalid credentials"), drop event.
    * 404: log warning, drop event.
    * 415 / 422: log error ("Schema mismatch"), drop event.
    * 429: honor Retry-After header, then retry.
    * 5xx / network errors: exponential backoff (100 ms -> 300 ms -> 1 s),
      and if still failing, append to a JSONL spill file for later replay.
- On startup the worker scans ~/.argus-reporter/*.jsonl (ordered by
  mtime) and replays via the batch endpoint; successful files are removed.

Public methods never raise. All exceptions are caught and logged via the
`argus` logger — training is sacred.
"""
from __future__ import annotations

import atexit
import logging
import mimetypes
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from . import __version__
from .schema import EventSource, build_event, drop_none, validate_event
from .spill import SpillStore, iter_existing_spill_files

logger = logging.getLogger("argus")

_RETRY_BACKOFFS_S = (0.1, 0.3, 1.0)
_DISABLE_ENV = "ARGUS_DISABLE"
# When the queue has this many items pending, the worker flushes them as
# a single /api/events/batch call instead of one POST per event. Also used
# during spill replay.
_BATCH_FLUSH_THRESHOLD = 20
# Upper bound matching backend policy (see requirements.md §6.1).
_BATCH_MAX_EVENTS = 500
# Fallback 429 sleep if no Retry-After header is sent.
_DEFAULT_429_BACKOFF_S = 30.0
# Cap on Retry-After so a misconfigured server can't pin the worker forever.
_MAX_RETRY_AFTER_S = 60.0


def _is_disabled() -> bool:
    return os.getenv(_DISABLE_ENV, "").strip() in {"1", "true", "TRUE", "yes", "on"}


def _short_git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            return sha or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class ExperimentReporter:
    """Thread-safe, fire-and-forget event emitter (schema v1.1)."""

    def __init__(
        self,
        url: str,
        project: str,
        auth_token: Optional[str] = None,
        host: Optional[str] = None,
        user: Optional[str] = None,
        commit: Optional[str] = None,
        timeout: float = 10.0,
        queue_size: int = 1000,
        spill_path: Optional[str] = None,
        batch_id: Optional[str] = None,
    ):
        self.disabled = _is_disabled()
        self.url = url.rstrip("/")
        self.endpoint = f"{self.url}/api/events"
        self.batch_endpoint = f"{self.url}/api/events/batch"
        self.timeout = timeout

        self.source = EventSource(
            project=project,
            host=host or socket.gethostname(),
            user=user or os.getenv("USER") or os.getenv("USERNAME"),
            commit=commit if commit is not None else _short_git_sha(),
        )

        self.current_batch_id: Optional[str] = batch_id
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=queue_size)
        self._spill = SpillStore(spill_path) if spill_path else SpillStore()
        self._session = requests.Session()
        if auth_token:
            self._session.headers["Authorization"] = f"Bearer {auth_token}"
        else:
            # Not a hard error — backend may be open during local dev — but
            # emit a structured warning so it's visible in run logs.
            logger.error(
                "ExperimentReporter: auth_token not provided; requests will be"
                " unauthenticated and likely rejected by a real backend."
            )
        self._session.headers["Content-Type"] = "application/json"
        self._session.headers["User-Agent"] = (
            f"argus-reporter/{__version__} (+schema=1.1)"
        )

        self._stop_evt = threading.Event()
        self._hard_stop = threading.Event()
        self._drops = 0

        if self.disabled:
            logger.info("argus disabled via %s", _DISABLE_ENV)
            self._worker = None
        else:
            self._worker = threading.Thread(
                target=self._worker_loop, name="exp-reporter", daemon=True
            )
            self._worker.start()
            atexit.register(self._atexit_close)

    # ------------------------------------------------------------------
    # Public API — thin wrappers that build a payload dict + enqueue.
    # Extra kwargs go into `data` for forward compatibility.
    # ------------------------------------------------------------------
    def batch_start(
        self,
        experiment_type: str,
        n_total: int,
        command: Optional[str] = None,
        batch_id: Optional[str] = None,
        **extra: Any,
    ) -> str:
        bid = batch_id or self.current_batch_id or self._new_batch_id()
        self.current_batch_id = bid
        if command and not self.source.command:
            self.source.command = command
        self._emit(
            "batch_start", bid,
            drop_none({"experiment_type": experiment_type, "n_total_jobs": n_total,
                       "command": command, **extra}),
        )
        return bid

    def batch_done(self, n_done: int, n_failed: int = 0,
                   total_elapsed_s: Optional[float] = None, **extra: Any) -> None:
        bid = self._require_batch("batch_done")
        if bid is None:
            return
        self._emit("batch_done", bid,
                   drop_none({"n_done": n_done, "n_failed": n_failed,
                              "total_elapsed_s": total_elapsed_s, **extra}))

    def batch_failed(self, reason: Optional[str] = None, **extra: Any) -> None:
        bid = self._require_batch("batch_failed")
        if bid is None:
            return
        self._emit("batch_failed", bid, drop_none({"reason": reason, **extra}))

    def job_start(self, job_id: str, model: Optional[str] = None,
                  dataset: Optional[str] = None, **extra: Any) -> None:
        bid = self._require_batch("job_start")
        if bid is None:
            return
        self._emit("job_start", bid,
                   drop_none({"model": model, "dataset": dataset, **extra}),
                   job_id=job_id)

    def job_epoch(self, job_id: str, epoch: int,
                  train_loss: Optional[float] = None, val_loss: Optional[float] = None,
                  lr: Optional[float] = None, **extra: Any) -> None:
        bid = self._require_batch("job_epoch")
        if bid is None:
            return
        self._emit("job_epoch", bid,
                   drop_none({"epoch": epoch, "train_loss": train_loss,
                              "val_loss": val_loss, "lr": lr, **extra}),
                   job_id=job_id)

    def job_done(self, job_id: str, metrics: Optional[Dict[str, Any]] = None,
                 elapsed_s: Optional[float] = None, train_epochs: Optional[int] = None,
                 resources: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
        bid = self._require_batch("job_done")
        if bid is None:
            return
        self._emit("job_done", bid,
                   drop_none({"status": "DONE", "metrics": metrics,
                              "elapsed_s": elapsed_s, "train_epochs": train_epochs,
                              "resources": resources, **extra}),
                   job_id=job_id)

    def job_failed(self, job_id: str, reason: Optional[str] = None,
                   elapsed_s: Optional[float] = None, **extra: Any) -> None:
        bid = self._require_batch("job_failed")
        if bid is None:
            return
        self._emit("job_failed", bid,
                   drop_none({"status": "FAILED", "reason": reason,
                              "elapsed_s": elapsed_s, **extra}),
                   job_id=job_id)

    def resource_snapshot(self, **extra: Any) -> None:
        bid = self._require_batch("resource_snapshot")
        if bid is None:
            return
        self._emit("resource_snapshot", bid, drop_none(extra))

    def log_line(self, job_id: str, line: str, level: str = "info", **extra: Any) -> None:
        bid = self._require_batch("log_line")
        if bid is None:
            return
        self._emit("log_line", bid,
                   drop_none({"line": line, "level": level, **extra}),
                   job_id=job_id)

    # ------------------------------------------------------------------
    # Artifact upload (synchronous multipart POST)
    # ------------------------------------------------------------------
    def job_artifact(
        self,
        job_id: str,
        path: Union[str, "Path"],
        *,
        label: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Upload a file as a job artifact to the monitor backend.

        Synchronous POST (not queued): artifacts are rare enough that
        the extra worker-thread complexity isn't worth it, and callers
        usually emit them from ``on_test_end`` where a blocking call is
        fine. Never raises — every failure is logged at DEBUG level and
        swallowed so a flaky backend can't crash training.

        Parameters
        ----------
        job_id:
            The job id the artifact belongs to. Must match an existing
            job the reporter's auth token has access to.
        path:
            Local file to upload. ``str`` or :class:`pathlib.Path`.
        label:
            Optional grouping label (e.g. ``"visualizations"``). The
            frontend uses this to bucket artifacts in the UI.
        meta:
            Optional dict of arbitrary JSON-serialisable metadata that
            will be stored on the artifact row.
        """
        if self.disabled:
            return
        try:
            p = Path(path)
            if not p.is_file():
                logger.debug("job_artifact: %s is not a file; skipping", p)
                return
            mime, _ = mimetypes.guess_type(p.name)
            if mime is None:
                mime = "application/octet-stream"
            data: Dict[str, Any] = {}
            if label is not None:
                data["label"] = label
            if meta is not None:
                try:
                    import json as _json  # local import keeps top small
                    data["meta"] = _json.dumps(meta)
                except (TypeError, ValueError):
                    logger.debug("job_artifact: meta not JSON-serialisable; dropping meta")
            url = f"{self.url}/api/jobs/{job_id}/artifacts"
            # requests.Session stores Content-Type: application/json as
            # a default — we must override it on this upload so the
            # multipart boundary isn't clobbered. Passing a per-call
            # dict doesn't merge with session headers the way we want;
            # the cleanest fix is popping the session default for this
            # request via an explicit header override of ``None``.
            headers = {"Content-Type": None}  # type: ignore[dict-item]
            with p.open("rb") as fh:
                files = {"file": (p.name, fh, mime)}
                resp = self._session.post(
                    url,
                    data=data,
                    files=files,
                    headers=headers,  # type: ignore[arg-type]
                    timeout=self.timeout,
                )
            if 200 <= resp.status_code < 300:
                logger.debug(
                    "job_artifact: uploaded %s (%d bytes) -> %s",
                    p.name, p.stat().st_size, resp.status_code,
                )
                return
            logger.debug(
                "job_artifact: upload %s returned HTTP %d: %s",
                p.name, resp.status_code, (resp.text or "")[:200],
            )
        except Exception:  # noqa: BLE001 - training is sacred
            logger.debug("job_artifact: upload failed", exc_info=True)

    # ------------------------------------------------------------------
    # Context manager + shutdown
    # ------------------------------------------------------------------
    def __enter__(self) -> "ExperimentReporter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> None:
        """Drain the queue and stop the worker. Best-effort, never raises."""
        if self.disabled or self._worker is None:
            return
        if not self._worker.is_alive() and self._stop_evt.is_set():
            return
        try:
            self._q.put_nowait(None)  # sentinel
        except queue.Full:
            self._stop_evt.set()
        self._stop_evt.set()
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            logger.warning(
                "argus close: worker did not exit within %.1fs; "
                "setting hard stop (remaining events will be spilled)", timeout,
            )
            # Force-spill any remaining queued events so the worker doesn't
            # keep POSTing after close returns (important in tests + Ctrl+C).
            self._hard_stop.set()
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                try:
                    self._spill.append(item)
                except Exception:
                    logger.exception("close: failed to spill on hard stop")
            self._worker.join(timeout=1.0)
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass

    def _atexit_close(self) -> None:
        try:
            self.close(timeout=2.0)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _new_batch_id() -> str:
        return f"batch-{uuid.uuid4().hex[:12]}"

    def _require_batch(self, event_type: str) -> Optional[str]:
        if self.current_batch_id:
            return self.current_batch_id
        logger.warning("%s called before batch_start; event dropped", event_type)
        return None

    def _emit(self, event_type: str, batch_id: str, data: Dict[str, Any],
              job_id: Optional[str] = None) -> None:
        if self.disabled:
            return
        try:
            event = build_event(event_type, batch_id, self.source, data, job_id=job_id)
        except Exception:
            logger.exception("_emit: failed to build event; dropping")
            return
        if not validate_event(event):
            return
        self._enqueue(event)

    def _enqueue(self, event: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
            return
        except queue.Full:
            pass
        # Drop-oldest: evict head, then retry put.
        try:
            dropped = self._q.get_nowait()
            self._drops += 1
            if dropped is not None:
                logger.warning(
                    "argus queue full; dropped oldest %s event",
                    dropped.get("event_type", "?"),
                )
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self._drops += 1
            logger.warning("argus queue full; dropping new event")

    def _worker_loop(self) -> None:
        try:
            self._replay_spill()
        except Exception:
            logger.exception("worker: replay_spill crashed, continuing")

        while True:
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                if self._stop_evt.is_set():
                    self._drain_remaining()
                    return
                continue
            if item is None:  # sentinel
                self._drain_remaining()
                return
            # Opportunistic batching: if the queue already has more pending
            # work, drain up to the threshold and ship it as a single batch
            # POST. This shrinks tail latency when callers burst events.
            extras: List[Dict[str, Any]] = []
            if self._q.qsize() >= _BATCH_FLUSH_THRESHOLD - 1:
                while len(extras) + 1 < _BATCH_MAX_EVENTS:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        # Sentinel seen mid-batch: put it back so outer loop
                        # exits cleanly after we flush.
                        self._stop_evt.set()
                        break
                    extras.append(nxt)
            try:
                if extras:
                    self._post_batch_with_retries([item] + extras)
                else:
                    self._post_with_retries(item)
            except Exception:
                logger.exception("worker: unexpected crash handling event")
            finally:
                try:
                    self._q.task_done()
                except ValueError:  # pragma: no cover
                    pass
                for _ in extras:
                    try:
                        self._q.task_done()
                    except ValueError:  # pragma: no cover
                        pass

    def _drain_remaining(self) -> None:
        # Collect everything still sitting in the queue and ship it as one
        # batch if there's more than one. Single leftover -> single POST.
        leftovers: List[Dict[str, Any]] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            leftovers.append(item)
        if not leftovers:
            return
        try:
            if self._hard_stop.is_set():
                for ev in leftovers:
                    self._spill.append(ev)
            elif len(leftovers) == 1:
                self._post_with_retries(leftovers[0])
            else:
                # chunk to batch size
                for i in range(0, len(leftovers), _BATCH_MAX_EVENTS):
                    self._post_batch_with_retries(leftovers[i:i + _BATCH_MAX_EVENTS])
        except Exception:
            logger.exception("worker: crash draining remaining events")
        finally:
            for _ in leftovers:
                try:
                    self._q.task_done()
                except ValueError:  # pragma: no cover
                    pass

    def _replay_spill(self) -> None:
        """On startup, replay every pre-existing spill file in mtime order.

        Each file is shipped via POST /api/events/batch. A successful send
        removes the file; a failed send keeps it for the next run.
        Both our own spill_path and any sibling spill files under
        ~/.argus-reporter/ are considered.
        """
        seen: set = set()

        # First drain our own file (covers the test-fixture case where a
        # custom spill_path is under tmp_path).
        if self._spill.exists():
            events = list(self._spill.drain())
            if events:
                logger.info(
                    "argus: replaying %d events from %s",
                    len(events), self._spill.path,
                )
                ok = self._post_batch_with_retries(events, spill_on_fail=False)
                if not ok:
                    # Put them back for a future run.
                    for ev in events:
                        self._spill.append(ev)
            seen.add(str(self._spill.path))

        # Then any sibling spill files in the default directory.
        for path in iter_existing_spill_files():
            if str(path) in seen:
                continue
            try:
                other = SpillStore(path)
                events = list(other.drain())
            except Exception:
                logger.exception("replay: failed to read %s", path)
                continue
            if not events:
                continue
            logger.info(
                "argus: replaying %d events from %s",
                len(events), path,
            )
            ok = self._post_batch_with_retries(events, spill_on_fail=False)
            if not ok:
                # Keep them on disk for the next run.
                for ev in events:
                    other.append(ev)

    # -- transport ------------------------------------------------------

    @staticmethod
    def _parse_retry_after(resp: "requests.Response") -> float:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return _DEFAULT_429_BACKOFF_S
        try:
            return max(0.0, min(_MAX_RETRY_AFTER_S, float(raw)))
        except (TypeError, ValueError):
            return _DEFAULT_429_BACKOFF_S

    def _classify_status(self, status: int, resp: "requests.Response") -> str:
        """Return one of: 'ok', 'drop', 'retry_5xx', 'retry_429'."""
        if 200 <= status < 300:
            return "ok"
        if status in (401, 403):
            logger.error(
                "argus: invalid credentials (HTTP %d); dropping event",
                status,
            )
            return "drop"
        if status in (415, 422):
            logger.error(
                "argus: schema mismatch (HTTP %d); dropping event. body=%s",
                status, (resp.text or "")[:200],
            )
            return "drop"
        if status == 404:
            logger.warning(
                "argus: endpoint returned 404; dropping event"
            )
            return "drop"
        if status == 429:
            return "retry_429"
        if 500 <= status < 600:
            return "retry_5xx"
        # Unknown 4xx: conservative drop.
        logger.warning(
            "argus: unexpected HTTP %d; dropping event. body=%s",
            status, (resp.text or "")[:200],
        )
        return "drop"

    def _post_with_retries(self, event: Dict[str, Any]) -> None:
        """Single-event POST with retry + spill-on-final-failure."""
        for attempt, backoff in enumerate(_RETRY_BACKOFFS_S + (None,)):
            if self._hard_stop.is_set():
                break
            try:
                resp = self._session.post(
                    self.endpoint, json=event, timeout=self.timeout
                )
                verdict = self._classify_status(resp.status_code, resp)
                if verdict == "ok":
                    return
                if verdict == "drop":
                    return
                if verdict == "retry_429":
                    wait_s = self._parse_retry_after(resp)
                    logger.warning(
                        "argus: rate-limited, sleeping %.2fs", wait_s,
                    )
                    if self._hard_stop.wait(wait_s):
                        break
                    continue  # do not consume a backoff slot
                # retry_5xx: fall through to backoff
                logger.debug(
                    "post attempt %d: HTTP %d %s", attempt + 1,
                    resp.status_code, (resp.text or "")[:200],
                )
            except requests.RequestException as e:
                logger.debug("post attempt %d failed: %s", attempt + 1, e)
            except Exception:
                logger.exception("post attempt %d crashed", attempt + 1)
            if backoff is None:
                break
            # wait() returns True if hard_stop gets set, letting us abort early.
            if self._hard_stop.wait(backoff):
                break
        # Retries exhausted (or hard-stop) — spill.
        self._spill.append(event)
        logger.warning(
            "argus: event %s spilled to %s after retries",
            event.get("event_type", "?"), self._spill.path,
        )

    def _post_batch_with_retries(
        self, events: List[Dict[str, Any]], spill_on_fail: bool = True
    ) -> bool:
        """Batch POST with retry. Returns True on success, False if given up.

        When `spill_on_fail=True` (default), unsent events are spilled.
        `spill_on_fail=False` is used by the replay path so we don't nuke
        the file in a single go.
        """
        if not events:
            return True
        body = {"events": events}
        for attempt, backoff in enumerate(_RETRY_BACKOFFS_S + (None,)):
            if self._hard_stop.is_set():
                break
            try:
                resp = self._session.post(
                    self.batch_endpoint, json=body, timeout=self.timeout
                )
                verdict = self._classify_status(resp.status_code, resp)
                if verdict == "ok":
                    return True
                if verdict == "drop":
                    # schema / auth / 404: no point in retrying or spilling
                    return False
                if verdict == "retry_429":
                    wait_s = self._parse_retry_after(resp)
                    logger.warning(
                        "argus: batch rate-limited, sleeping %.2fs",
                        wait_s,
                    )
                    if self._hard_stop.wait(wait_s):
                        break
                    continue
                logger.debug(
                    "batch post attempt %d: HTTP %d %s", attempt + 1,
                    resp.status_code, (resp.text or "")[:200],
                )
            except requests.RequestException as e:
                logger.debug("batch post attempt %d failed: %s", attempt + 1, e)
            except Exception:
                logger.exception("batch post attempt %d crashed", attempt + 1)
            if backoff is None:
                break
            if self._hard_stop.wait(backoff):
                break
        # Retries exhausted
        if spill_on_fail:
            for ev in events:
                self._spill.append(ev)
            logger.warning(
                "argus: %d batched events spilled to %s",
                len(events), self._spill.path,
            )
        return False
