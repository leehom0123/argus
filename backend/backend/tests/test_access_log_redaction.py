"""Unit test for the uvicorn access-log ``?token=`` redactor.

Phase-3 post-review M4. The app installs a :class:`logging.Filter` on
the ``uvicorn.access`` logger that rewrites ``token=<value>`` query
params in each record's ``args`` to ``token=REDACTED`` before the log
line is formatted. This test drives the filter directly with
hand-crafted LogRecord-like objects so it doesn't depend on spinning
up a real uvicorn server.
"""
from __future__ import annotations

import logging

import pytest

from backend.app import _TOKEN_QUERY_PAT, _TokenRedactFilter


def _record(args) -> logging.LogRecord:
    """Build a minimal LogRecord that mimics uvicorn.access format.

    ``logging.LogRecord.__init__`` treats a single-element tuple whose
    element is a Mapping as a formatting dict and unwraps it; we want
    to test the dict-args path explicitly so we set ``record.args``
    after construction to avoid the auto-unwrap.
    """
    rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(),
        exc_info=None,
    )
    rec.args = args
    return rec


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "GET /api/events/stream?token=em_live_secrethex HTTP/1.1",
            "GET /api/events/stream?token=REDACTED HTTP/1.1",
        ),
        (
            "GET /api/events/stream?token=em_view_anotherkey HTTP/1.1",
            "GET /api/events/stream?token=REDACTED HTTP/1.1",
        ),
        (
            # JWT with dots and underscores, plus another query param
            "GET /api/events/stream?batch_id=b1&token=eyJ0eXAi.abc-DEF_xyz.signature HTTP/1.1",
            "GET /api/events/stream?batch_id=b1&token=REDACTED HTTP/1.1",
        ),
        (
            # token= appears multiple times — both should be redacted
            "GET /foo?token=aaa&other=1&token=bbb HTTP/1.1",
            "GET /foo?token=REDACTED&other=1&token=REDACTED HTTP/1.1",
        ),
    ],
)
def test_regex_redacts_known_token_shapes(raw: str, expected: str) -> None:
    assert _TOKEN_QUERY_PAT.sub("token=REDACTED", raw) == expected


def test_filter_rewrites_request_line_in_record_args() -> None:
    """uvicorn's access format puts the request line at ``record.args[2]``
    (``method``, ``path``, ``http_version``) — after the filter runs no
    ``token=...`` substring survives."""
    f = _TokenRedactFilter()
    original_args = (
        "127.0.0.1",
        "GET",
        "/api/events/stream?batch_id=b1&token=em_live_sensitive",
        "1.1",
        200,
    )
    rec = _record(original_args)
    assert f.filter(rec) is True
    # The rewritten tuple must not contain the raw token anywhere.
    assert all("em_live_sensitive" not in str(a) for a in rec.args)
    # And must preserve the 'REDACTED' placeholder.
    assert any("token=REDACTED" in str(a) for a in rec.args)


def test_filter_leaves_args_with_no_token_alone() -> None:
    """When no ``token=`` substring is present the filter must not
    introduce a REDACTED placeholder. Args get stringified because we
    use ``str(a)`` inside the filter — that's fine, logging formats
    stringify anyway — so we compare on string form."""
    f = _TokenRedactFilter()
    original = ("1.2.3.4", "GET", "/api/batches", "1.1", 200)
    rec = _record(original)
    assert f.filter(rec) is True
    assert tuple(str(a) for a in rec.args) == tuple(str(a) for a in original)
    assert all("REDACTED" not in str(a) for a in rec.args)


def test_filter_handles_dict_args() -> None:
    """Some logging formats use dict args rather than tuples; redactor
    should cover that shape too without crashing."""
    f = _TokenRedactFilter()
    rec = _record({"request_line": "GET /?token=em_live_abc HTTP/1.1"})
    f.filter(rec)
    assert "token=REDACTED" in rec.args["request_line"]
    assert "em_live_abc" not in rec.args["request_line"]


def test_filter_handles_empty_args() -> None:
    f = _TokenRedactFilter()
    rec = _record(None)
    # No args → nothing to rewrite; filter must still return True so the
    # record isn't dropped.
    assert f.filter(rec) is True


def test_install_is_idempotent() -> None:
    """Repeated ``create_app()`` calls in the test suite must not stack
    the same filter twice on the uvicorn.access logger."""
    from backend.app import _install_access_log_redaction

    logger = logging.getLogger("uvicorn.access")
    # Wipe any prior state — other tests may or may not have run first.
    logger.filters = [f for f in logger.filters if not isinstance(f, _TokenRedactFilter)]
    _install_access_log_redaction()
    _install_access_log_redaction()
    installed = [f for f in logger.filters if isinstance(f, _TokenRedactFilter)]
    assert len(installed) == 1
