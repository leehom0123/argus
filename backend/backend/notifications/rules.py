"""Notification rule parser and evaluator.

Rules are declared in YAML as::

    rules:
      - when: event_type == "job_failed"
        push: [feishu]
      - when: event_type == "batch_done" and data.n_failed > 0
        push: [feishu]
      - when: event_type == "resource_snapshot" and data.gpu_util_pct < 10
        push: [feishu]

Only three expression shapes are supported; we parse rather than ``eval`` for
safety:

* ``event_type == "X"``
* ``event_type == "X" and data.<field> <op> <value>``  (op: ``==``, ``!=``,
  ``<``, ``<=``, ``>``, ``>=``)
* ``data.<field> <op> <value>`` standalone

Unsupported expressions raise :class:`ValueError` at load time.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Expression parser
# ---------------------------------------------------------------------------


_EVENT_TYPE_RE = re.compile(
    r'^\s*event_type\s*==\s*"([^"]+)"\s*$'
)
_DATA_CMP_RE = re.compile(
    r'^\s*data\.([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$'
)


def _parse_literal(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if raw.lower() == "null" or raw.lower() == "none":
        return None
    try:
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"cannot parse literal: {raw!r}") from exc


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is None:
        return False
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    raise ValueError(f"unsupported operator: {op}")


def _build_predicate(expr: str) -> Callable[[dict[str, Any]], bool]:
    """Compile a rule expression into a predicate function."""
    parts = re.split(r"\s+and\s+", expr.strip(), maxsplit=1)

    preds: list[Callable[[dict[str, Any]], bool]] = []
    for part in parts:
        m = _EVENT_TYPE_RE.match(part)
        if m:
            wanted = m.group(1)
            preds.append(lambda ev, w=wanted: ev.get("event_type") == w)
            continue
        m = _DATA_CMP_RE.match(part)
        if m:
            field, op, raw_val = m.group(1), m.group(2), m.group(3)
            value = _parse_literal(raw_val)
            preds.append(
                lambda ev, f=field, o=op, v=value: _compare(
                    o, (ev.get("data") or {}).get(f), v
                )
            )
            continue
        raise ValueError(f"unsupported rule expression: {part!r}")

    def run(event: dict[str, Any]) -> bool:
        return all(p(event) for p in preds)

    return run


# ---------------------------------------------------------------------------
# Rule container
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    """Parsed notification rule."""

    expression: str
    push: list[str]
    predicate: Callable[[dict[str, Any]], bool]

    def matches(self, event: dict[str, Any]) -> bool:
        try:
            return self.predicate(event)
        except Exception as exc:  # noqa: BLE001 - rule must not crash ingest
            log.warning("rule %r raised %s", self.expression, exc)
            return False


def load_rules(path: str | Path) -> list[Rule]:
    """Load and compile rules from a YAML file. Missing file → empty list."""
    p = Path(path)
    if not p.exists():
        log.info("notifications config %s not found, no rules active", p)
        return []
    with p.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    raw_rules = doc.get("rules") or []
    rules: list[Rule] = []
    for idx, item in enumerate(raw_rules):
        expr = item.get("when")
        push = item.get("push") or []
        if not expr or not isinstance(push, list):
            log.warning("skipping malformed rule at index %d: %r", idx, item)
            continue
        try:
            pred = _build_predicate(expr)
        except ValueError as exc:
            log.warning("skipping rule %d (%r): %s", idx, expr, exc)
            continue
        rules.append(Rule(expression=expr, push=list(push), predicate=pred))
    log.info("loaded %d notification rules from %s", len(rules), p)
    return rules


def evaluate(event: dict[str, Any], rules: list[Rule]) -> list[str]:
    """Return channel names whose rules match the event (deduplicated)."""
    channels: list[str] = []
    for rule in rules:
        if rule.matches(event):
            for ch in rule.push:
                if ch not in channels:
                    channels.append(ch)
    return channels
