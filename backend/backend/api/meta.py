"""Meta / bootstrap endpoints (roadmap #30 — empty-state hints).

``GET /api/meta/hints`` returns a dict of i18n keys → human-readable
hint text. The frontend renders one hint per empty-state surface (no
batches, no hosts, no projects, ...). Locale is resolved from the
``Accept-Language`` header via :func:`backend.deps_locale.get_locale`.

The hint catalog is deliberately kept self-contained in this module
rather than folded into ``backend/i18n/messages.py`` because:

  1. The messages catalog is scoped to HTTPException detail strings —
     all keys there share the property of being surfaced on a 4xx /
     5xx response. Hint strings are display copy and have a different
     lifecycle (A/B tested, tone-polished) from error messages.

  2. Frontend callers of this endpoint get a *stable* JSON shape even
     if the error catalog grows keys that shouldn't leak into the UI.

Adding a hint = edit the locale maps below. No migration, no schema
change — just ship the backend.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from backend.deps_locale import SupportedLocale, get_locale

router = APIRouter(prefix="/api/meta", tags=["meta"])


# ---------------------------------------------------------------------------
# Hint catalog.
#
# Keys are chosen to match the frontend's EmptyState component's "variant"
# prop so the client can write ``hints[`empty_${variant}`]`` directly.
# ---------------------------------------------------------------------------


HINTS: dict[SupportedLocale, dict[str, str]] = {
    "en-US": {
        "empty_hosts": (
            "No hosts yet — add ARGUS_URL to your reporter config"
            " so runs start showing up here."
        ),
        "empty_batches": (
            "No batches yet — run main.py or scripts/forecast/run_benchmark.py"
            " to create one."
        ),
        "empty_jobs": (
            "This batch has no jobs yet — they'll appear as the sweep starts."
        ),
        "empty_projects": (
            "No projects yet — the first time a reporter posts a batch, its"
            " project name will show up here."
        ),
        "empty_notifications": (
            "No notifications — we'll ping you when a watchdog rule fires."
        ),
        "empty_pins": (
            "No pinned batches — pin up to 4 batches to compare them side"
            " by side in one view."
        ),
        "empty_shared": "No batches have been shared with you yet.",
        "empty_stars": (
            "No starred projects or batches yet — click the star icon to add one."
        ),
        "empty_search": "No results — try a shorter query or broader filter.",
        "empty_events": (
            "No events yet — the reporter streams them once training begins."
        ),
        "empty_artifacts": (
            "No artifacts yet — checkpoints, plots and predictions show up here"
            " after a job finishes."
        ),
    },
    "zh-CN": {
        "empty_hosts": "暂无主机 —— 请在 reporter 配置中设置 ARGUS_URL，运行后会自动上报。",
        "empty_batches": "暂无批次 —— 运行 main.py 或 scripts/forecast/run_benchmark.py 创建第一个。",
        "empty_jobs": "该批次还没有任务 —— 任务会在 sweep 启动后陆续出现。",
        "empty_projects": "暂无项目 —— 首次上报批次时，项目名会自动出现在这里。",
        "empty_notifications": "暂无通知 —— 看门狗规则触发时我们会提醒你。",
        "empty_pins": "暂无固定批次 —— 最多可固定 4 个批次并排对比。",
        "empty_shared": "还没有人向你共享批次。",
        "empty_stars": "暂无收藏 —— 点击星标图标即可收藏项目或批次。",
        "empty_search": "无结果 —— 试试更短的关键词或放宽筛选条件。",
        "empty_events": "暂无事件 —— 训练开始后 reporter 会实时推送。",
        "empty_artifacts": "暂无产物 —— 任务完成后会展示 checkpoint、图表和预测结果。",
    },
}


class MetaHintsOut(BaseModel):
    """Response body for ``GET /api/meta/hints``."""

    model_config = ConfigDict(extra="forbid")

    locale: SupportedLocale
    hints: dict[str, str]


@router.get("/hints", response_model=MetaHintsOut)
async def get_hints(
    locale: SupportedLocale = Depends(get_locale),
) -> MetaHintsOut:
    """Return the empty-state hint copy for the caller's locale.

    Falls back to ``en-US`` when the ``Accept-Language`` header selects
    an unsupported locale (the ``get_locale`` dependency already
    normalises this, so the lookup below is essentially O(1)).
    """
    catalog = HINTS.get(locale) or HINTS["en-US"]
    # Defensive copy so callers can't mutate the shared catalog.
    return MetaHintsOut(locale=locale, hints=dict(catalog))
