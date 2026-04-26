"""Wire-format checks for ``backend.utils.sse.format_sse``.

The browser's ``EventSource`` rejects malformed frames silently, so we
lock in the expected bytes here rather than finding out during frontend
integration.
"""
from __future__ import annotations

import json

import pytest

from backend.utils.sse import format_keepalive, format_sse


def test_format_sse_dict_payload():
    frame = format_sse("job_epoch", {"epoch": 1, "loss": 0.5})
    assert frame.endswith("\n\n")
    lines = frame.rstrip().split("\n")
    assert lines[0] == "event: job_epoch"
    assert lines[1].startswith("data: ")
    payload = lines[1][len("data: "):]
    assert json.loads(payload) == {"epoch": 1, "loss": 0.5}


def test_format_sse_string_payload_passes_through():
    frame = format_sse("hello", "subscribed")
    assert frame == "event: hello\ndata: subscribed\n\n"


def test_format_sse_multiline_payload_emits_multiple_data_lines():
    """A payload containing '\\n' must split into multiple data: lines."""
    frame = format_sse("log_line", "line 1\nline 2\nline 3")
    lines = frame.rstrip().split("\n")
    assert lines == [
        "event: log_line",
        "data: line 1",
        "data: line 2",
        "data: line 3",
    ]


def test_format_sse_rejects_newline_in_event_type():
    with pytest.raises(ValueError):
        format_sse("bad\nevent", {})


def test_format_keepalive_uses_event_keepalive():
    frame = format_keepalive()
    assert frame.startswith("event: keepalive\n")
    assert frame.endswith("\n\n")
