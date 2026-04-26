"""Helpers for formatting Server-Sent Events frames.

A well-formed SSE frame is three lines ending in a blank line::

    event: <event_type>
    data: <utf-8 payload, may not contain newlines>

The payload is newline-sanitised: SSE treats ``\\n`` as a field separator,
so any newline inside a JSON blob becomes an extra ``data:`` line. We
follow the standard wire format and split on newlines accordingly.

See: https://html.spec.whatwg.org/multipage/server-sent-events.html
"""
from __future__ import annotations

import json
from typing import Any


def format_sse(event_type: str, data: Any) -> str:
    """Return a single SSE frame.

    Parameters
    ----------
    event_type:
        Value for the ``event:`` field. Must not contain ``\\n``.
    data:
        Either a pre-serialised string, or a JSON-serialisable object
        (dict / list / primitive). Objects are dumped with ``default=str``
        so datetimes and other non-trivial types survive.

    Returns
    -------
    str
        ``event: <type>\\ndata: <line1>\\n[data: <line2>\\n...]\\n\\n``
        — always terminates in one blank line per the spec.
    """
    if event_type is None or "\n" in event_type:
        raise ValueError("event_type must be a single line")

    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, default=str, separators=(",", ":"))

    # Multi-line payloads must emit one data: line per source line so
    # the browser reassembles them verbatim.
    data_lines = "\n".join(f"data: {line}" for line in payload.split("\n"))
    return f"event: {event_type}\n{data_lines}\n\n"


def format_keepalive() -> str:
    """Standard heartbeat frame (``event: keepalive`` + epoch seconds)."""
    import time

    return format_sse("keepalive", str(int(time.time())))
