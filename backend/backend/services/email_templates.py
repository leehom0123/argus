"""Factory-default Jinja templates + seed helper for ``email_template``.

This module owns:

* :data:`SUPPORTED_EVENTS` — the fixed list of event_types the email
  subsystem recognises. Any dispatcher / admin UI calling into this
  module MUST use these keys; adding a new event requires extending
  :data:`EVENT_DEFAULTS` in the same commit.
* :data:`EVENT_DEFAULTS` — factory subject + HTML + text body tuples
  for every (event, locale) in the Cartesian product.
* :func:`seed_default_templates` — upsert the defaults into
  ``email_template`` on first startup (called from ``app.py`` lifespan,
  after ``init_db``).
* :func:`reset_template_to_default` — admin "reset to factory" helper
  used by ``/api/admin/email/templates/{id}/reset``.

Template variables
------------------

All render contexts pass the following top-level keys:

* ``batch``: dict with ``id``, ``name``, ``project``, ``status``,
  ``start_time``, ``end_time``, ``host``
* ``job``: dict with ``id``, ``model``, ``dataset``, ``status``,
  ``error``; only populated for ``job_*`` events
* ``shared_by``: dict with ``username``; only populated for
  ``share_granted``
* ``project``: project name string (for ``share_granted``)
* ``permission``: ``'viewer'`` | ``'editor'`` (for ``share_granted``)
* ``link``: dict with ``batch_url``, ``project_url``,
  ``unsubscribe_url`` (whichever is relevant for the event)

Jinja's :func:`~jinja2.select_autoescape` for ``html`` is applied by
the dispatcher, so interpolated values are HTML-escaped in
``body_html`` but NOT in ``body_text`` (where escaping is undesirable).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import EmailTemplate

log = logging.getLogger(__name__)


# Keep this list in sync with backend.services.notifications_dispatcher —
# any new event type needs both a hook call site and a default template.
SUPPORTED_EVENTS: list[str] = [
    "batch_done",
    "batch_failed",
    "batch_diverged",
    "job_failed",
    "job_idle_flagged",
    "share_granted",
]

SUPPORTED_LOCALES: list[str] = ["en-US", "zh-CN"]


@dataclass(frozen=True)
class TemplateDefault:
    """Factory-seeded template tuple (subject + html + text)."""

    subject: str
    body_html: str
    body_text: str


# ---------------------------------------------------------------------------
# Shared markup fragments
# ---------------------------------------------------------------------------

_FOOTER_EN = (
    '<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">'
    '<p style="color:#999;font-size:12px;">'
    'Argus &middot; '
    'Not your alert? '
    '<a href="{{ link.unsubscribe_url }}">Unsubscribe</a>'
    '</p>'
)

_FOOTER_ZH = (
    '<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">'
    '<p style="color:#999;font-size:12px;">'
    'Argus &middot; '
    '不是您期望收到的提醒？'
    '<a href="{{ link.unsubscribe_url }}">退订</a>'
    '</p>'
)


def _html(body: str, locale: str) -> str:
    footer = _FOOTER_EN if locale == "en-US" else _FOOTER_ZH
    return (
        '<!DOCTYPE html><html><body '
        'style="font-family:-apple-system,Segoe UI,sans-serif;'
        'max-width:560px;margin:32px auto;color:#222;">'
        + body
        + footer
        + '</body></html>'
    )


# ---------------------------------------------------------------------------
# Per-event factory defaults
# ---------------------------------------------------------------------------

EVENT_DEFAULTS: dict[tuple[str, str], TemplateDefault] = {
    # ---- batch_done -----------------------------------------------------
    ("batch_done", "en-US"): TemplateDefault(
        subject="[argus] batch {{ batch.name or batch.id }} completed",
        body_html=_html(
            "<h2>Batch completed</h2>"
            "<p>Your batch <b>{{ batch.name or batch.id }}</b> "
            "(project <code>{{ batch.project }}</code>) finished on "
            "<code>{{ batch.host }}</code>.</p>"
            "<ul>"
            "<li>Status: <b>{{ batch.status }}</b></li>"
            "<li>Started: {{ batch.start_time }}</li>"
            "<li>Finished: {{ batch.end_time }}</li>"
            "</ul>"
            '<p><a href="{{ link.batch_url }}">Open in dashboard</a></p>',
            "en-US",
        ),
        body_text=(
            "Batch {{ batch.name or batch.id }} completed.\n"
            "Project: {{ batch.project }}\n"
            "Host: {{ batch.host }}\n"
            "Status: {{ batch.status }}\n"
            "Started: {{ batch.start_time }}\n"
            "Finished: {{ batch.end_time }}\n"
            "Open: {{ link.batch_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("batch_done", "zh-CN"): TemplateDefault(
        subject="[argus] 批次 {{ batch.name or batch.id }} 已完成",
        body_html=_html(
            "<h2>批次完成</h2>"
            "<p>您的批次 <b>{{ batch.name or batch.id }}</b>"
            "（项目 <code>{{ batch.project }}</code>）已在 "
            "<code>{{ batch.host }}</code> 上完成。</p>"
            "<ul>"
            "<li>状态：<b>{{ batch.status }}</b></li>"
            "<li>开始：{{ batch.start_time }}</li>"
            "<li>结束：{{ batch.end_time }}</li>"
            "</ul>"
            '<p><a href="{{ link.batch_url }}">在仪表盘中查看</a></p>',
            "zh-CN",
        ),
        body_text=(
            "批次 {{ batch.name or batch.id }} 已完成。\n"
            "项目：{{ batch.project }}\n"
            "主机：{{ batch.host }}\n"
            "状态：{{ batch.status }}\n"
            "开始：{{ batch.start_time }}\n"
            "结束：{{ batch.end_time }}\n"
            "查看：{{ link.batch_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
    # ---- batch_failed --------------------------------------------------
    ("batch_failed", "en-US"): TemplateDefault(
        subject="[argus] batch {{ batch.name or batch.id }} FAILED",
        body_html=_html(
            "<h2 style=\"color:#c0392b;\">Batch failed</h2>"
            "<p>Batch <b>{{ batch.name or batch.id }}</b> "
            "(project <code>{{ batch.project }}</code>) failed on "
            "<code>{{ batch.host }}</code>.</p>"
            "<ul>"
            "<li>Started: {{ batch.start_time }}</li>"
            "<li>Finished: {{ batch.end_time }}</li>"
            "</ul>"
            '<p><a href="{{ link.batch_url }}">Investigate in dashboard</a></p>',
            "en-US",
        ),
        body_text=(
            "Batch {{ batch.name or batch.id }} FAILED.\n"
            "Project: {{ batch.project }}\n"
            "Host: {{ batch.host }}\n"
            "Started: {{ batch.start_time }}\n"
            "Finished: {{ batch.end_time }}\n"
            "Investigate: {{ link.batch_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("batch_failed", "zh-CN"): TemplateDefault(
        subject="[argus] 批次 {{ batch.name or batch.id }} 失败",
        body_html=_html(
            "<h2 style=\"color:#c0392b;\">批次失败</h2>"
            "<p>批次 <b>{{ batch.name or batch.id }}</b>"
            "（项目 <code>{{ batch.project }}</code>）在 "
            "<code>{{ batch.host }}</code> 上失败。</p>"
            "<ul>"
            "<li>开始：{{ batch.start_time }}</li>"
            "<li>结束：{{ batch.end_time }}</li>"
            "</ul>"
            '<p><a href="{{ link.batch_url }}">到仪表盘中排查</a></p>',
            "zh-CN",
        ),
        body_text=(
            "批次 {{ batch.name or batch.id }} 失败。\n"
            "项目：{{ batch.project }}\n"
            "主机：{{ batch.host }}\n"
            "开始：{{ batch.start_time }}\n"
            "结束：{{ batch.end_time }}\n"
            "排查：{{ link.batch_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
    # ---- batch_diverged ------------------------------------------------
    ("batch_diverged", "en-US"): TemplateDefault(
        subject="[argus] batch {{ batch.name or batch.id }} diverging",
        body_html=_html(
            "<h2 style=\"color:#d35400;\">Training diverged</h2>"
            "<p>The watchdog flagged batch "
            "<b>{{ batch.name or batch.id }}</b> "
            "(project <code>{{ batch.project }}</code>) as divergent. "
            "Val-loss has grown past the configured ratio or become "
            "NaN / Inf.</p>"
            '<p><a href="{{ link.batch_url }}">Open in dashboard</a></p>',
            "en-US",
        ),
        body_text=(
            "Batch {{ batch.name or batch.id }} is diverging.\n"
            "Project: {{ batch.project }}\n"
            "Host: {{ batch.host }}\n"
            "Open: {{ link.batch_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("batch_diverged", "zh-CN"): TemplateDefault(
        subject="[argus] 批次 {{ batch.name or batch.id }} 已发散",
        body_html=_html(
            "<h2 style=\"color:#d35400;\">训练发散</h2>"
            "<p>看门狗检测到批次 <b>{{ batch.name or batch.id }}</b>"
            "（项目 <code>{{ batch.project }}</code>）已发散：val_loss "
            "连续上涨至触发阈值，或已变为 NaN / Inf。</p>"
            '<p><a href="{{ link.batch_url }}">在仪表盘中查看</a></p>',
            "zh-CN",
        ),
        body_text=(
            "批次 {{ batch.name or batch.id }} 已发散。\n"
            "项目：{{ batch.project }}\n"
            "主机：{{ batch.host }}\n"
            "查看：{{ link.batch_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
    # ---- job_failed ----------------------------------------------------
    ("job_failed", "en-US"): TemplateDefault(
        subject="[argus] job {{ job.id }} failed",
        body_html=_html(
            "<h2 style=\"color:#c0392b;\">Job failed</h2>"
            "<p>Job <b>{{ job.id }}</b> "
            "({{ job.model }} on {{ job.dataset }}) "
            "inside batch <b>{{ batch.name or batch.id }}</b> failed.</p>"
            "{% if job.error %}"
            '<pre style="background:#f6f8fa;padding:12px;border-radius:4px;'
            'overflow:auto;">{{ job.error }}</pre>'
            "{% endif %}"
            '<p><a href="{{ link.batch_url }}">Open batch in dashboard</a></p>',
            "en-US",
        ),
        body_text=(
            "Job {{ job.id }} failed.\n"
            "Model: {{ job.model }}\n"
            "Dataset: {{ job.dataset }}\n"
            "Batch: {{ batch.name or batch.id }}\n"
            "Error: {{ job.error }}\n"
            "Open: {{ link.batch_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("job_failed", "zh-CN"): TemplateDefault(
        subject="[argus] 任务 {{ job.id }} 失败",
        body_html=_html(
            "<h2 style=\"color:#c0392b;\">任务失败</h2>"
            "<p>任务 <b>{{ job.id }}</b>"
            "（{{ job.model }} / {{ job.dataset }}），"
            "所属批次 <b>{{ batch.name or batch.id }}</b> 已失败。</p>"
            "{% if job.error %}"
            '<pre style="background:#f6f8fa;padding:12px;border-radius:4px;'
            'overflow:auto;">{{ job.error }}</pre>'
            "{% endif %}"
            '<p><a href="{{ link.batch_url }}">在仪表盘中查看</a></p>',
            "zh-CN",
        ),
        body_text=(
            "任务 {{ job.id }} 失败。\n"
            "模型：{{ job.model }}\n"
            "数据集：{{ job.dataset }}\n"
            "批次：{{ batch.name or batch.id }}\n"
            "错误：{{ job.error }}\n"
            "查看：{{ link.batch_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
    # ---- job_idle_flagged ---------------------------------------------
    ("job_idle_flagged", "en-US"): TemplateDefault(
        subject="[argus] job {{ job.id }} flagged as idle",
        body_html=_html(
            "<h2 style=\"color:#d35400;\">Job flagged as idle</h2>"
            "<p>Job <b>{{ job.id }}</b> inside batch "
            "<b>{{ batch.name or batch.id }}</b> on "
            "<code>{{ batch.host }}</code> has had GPU utilisation "
            "below 5% for the last window. The job is still running; "
            "this is advisory only.</p>"
            '<p><a href="{{ link.batch_url }}">Open batch</a></p>',
            "en-US",
        ),
        body_text=(
            "Job {{ job.id }} appears idle in batch "
            "{{ batch.name or batch.id }} on {{ batch.host }}.\n"
            "Open: {{ link.batch_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("job_idle_flagged", "zh-CN"): TemplateDefault(
        subject="[argus] 任务 {{ job.id }} 被标记为闲置",
        body_html=_html(
            "<h2 style=\"color:#d35400;\">任务被标记为闲置</h2>"
            "<p>批次 <b>{{ batch.name or batch.id }}</b> 内的任务 "
            "<b>{{ job.id }}</b>（主机 <code>{{ batch.host }}</code>）"
            "近期 GPU 利用率低于 5%。任务仍在运行，此提示仅供参考。</p>"
            '<p><a href="{{ link.batch_url }}">查看批次</a></p>',
            "zh-CN",
        ),
        body_text=(
            "任务 {{ job.id }} 在批次 {{ batch.name or batch.id }} 上被标记为闲置。\n"
            "查看：{{ link.batch_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
    # ---- share_granted -------------------------------------------------
    ("share_granted", "en-US"): TemplateDefault(
        subject="[argus] {{ shared_by.username }} shared {{ project }} with you",
        body_html=_html(
            "<h2>Project shared with you</h2>"
            "<p><b>{{ shared_by.username }}</b> shared the project "
            "<code>{{ project }}</code> with you ("
            "permission: <b>{{ permission }}</b>).</p>"
            '<p><a href="{{ link.project_url }}">Open project</a></p>',
            "en-US",
        ),
        body_text=(
            "{{ shared_by.username }} shared project {{ project }} with you "
            "(permission: {{ permission }}).\n"
            "Open: {{ link.project_url }}\n"
            "Unsubscribe: {{ link.unsubscribe_url }}\n"
        ),
    ),
    ("share_granted", "zh-CN"): TemplateDefault(
        subject="[argus] {{ shared_by.username }} 向您共享了项目 {{ project }}",
        body_html=_html(
            "<h2>已与您共享项目</h2>"
            "<p><b>{{ shared_by.username }}</b> 向您共享了项目 "
            "<code>{{ project }}</code>（权限："
            "<b>{{ permission }}</b>）。</p>"
            '<p><a href="{{ link.project_url }}">查看项目</a></p>',
            "zh-CN",
        ),
        body_text=(
            "{{ shared_by.username }} 向您共享了项目 {{ project }}"
            "（权限：{{ permission }}）。\n"
            "查看：{{ link.project_url }}\n"
            "退订：{{ link.unsubscribe_url }}\n"
        ),
    ),
}


# ---------------------------------------------------------------------------
# Integrity check
# ---------------------------------------------------------------------------


def _assert_defaults_complete() -> None:
    missing = [
        (ev, loc)
        for ev in SUPPORTED_EVENTS
        for loc in SUPPORTED_LOCALES
        if (ev, loc) not in EVENT_DEFAULTS
    ]
    if missing:
        raise RuntimeError(
            f"EVENT_DEFAULTS missing entries: {missing}"
        )


_assert_defaults_complete()


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Seed + reset helpers
# ---------------------------------------------------------------------------


async def seed_default_templates(db: AsyncSession) -> int:
    """Upsert every (event_type, locale) factory default.

    Rows are idempotent: if a row already exists for ``(event_type,
    locale)`` we leave its subject + body alone (so admin edits stick)
    but make sure ``is_system=True``.  Missing rows are inserted.

    Returns the number of rows inserted.  Callers must commit; we
    deliberately don't so the caller can participate in a larger unit
    of work (e.g. the lifespan seeder commits once after both the
    email templates and the demo seeder ran).
    """
    existing_map: dict[tuple[str, str], EmailTemplate] = {}
    rows: Iterable[EmailTemplate] = (
        await db.execute(select(EmailTemplate))
    ).scalars()
    for row in rows:
        existing_map[(row.event_type, row.locale)] = row

    inserted = 0
    now = _utcnow_iso()
    for (event_type, locale), default in EVENT_DEFAULTS.items():
        row = existing_map.get((event_type, locale))
        if row is None:
            db.add(
                EmailTemplate(
                    event_type=event_type,
                    locale=locale,
                    subject=default.subject,
                    body_html=default.body_html,
                    body_text=default.body_text,
                    is_system=True,
                    updated_at=now,
                )
            )
            inserted += 1
        else:
            # Keep any admin edits; just re-affirm the system flag so
            # the reset endpoint treats it as resettable.
            if not row.is_system:
                row.is_system = True

    if inserted:
        log.info("email_templates: seeded %d default rows", inserted)
    return inserted


def lookup_default(event_type: str, locale: str) -> TemplateDefault | None:
    """Return the factory default for (event_type, locale), if any."""
    return EVENT_DEFAULTS.get((event_type, locale))


async def reset_template_to_default(
    db: AsyncSession, template: EmailTemplate
) -> bool:
    """Restore *template* to its factory default.

    Returns True iff the template has a registered default and the row
    was updated.  Non-system or operator-authored templates return
    False so the admin endpoint can 400 appropriately.  Caller commits.
    """
    if not template.is_system:
        return False
    default = lookup_default(template.event_type, template.locale)
    if default is None:
        return False
    template.subject = default.subject
    template.body_html = default.body_html
    template.body_text = default.body_text
    template.updated_at = _utcnow_iso()
    return True
