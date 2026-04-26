"""Bilingual message catalog.

Keys are dot-namespaced strings matching the source HTTPException detail strings
collected from backend/api/*.py and backend/deps.py.  The catalog has two top-
level locales: "en-US" and "zh-CN".

Interpolation uses str.format(**fmt) — placeholders are {name} style.
"""
from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------
    # English (canonical / fallback)
    # ------------------------------------------------------------------
    "en-US": {
        # ---- auth ----
        "auth.token.invalid": "Invalid or expired token",
        "auth.token.used": "Token already used",
        "auth.token.malformed_expiry": "Token has malformed expiry",
        "auth.token.expired": "Token expired",
        "auth.token.user_missing": "Invalid token (user missing)",
        "auth.token.refresh_failed": "Could not refresh token",
        "auth.token.api_invalid": "Invalid or expired API token",
        "auth.token.owner_inactive": "Token owner is not active",
        "auth.credentials.bad": "Invalid username / email or password",
        "auth.credentials.required": "Authentication required",
        "auth.locked": "Account temporarily locked; try again in {minutes} minutes",
        "auth.verify.expired": "Verification link expired",
        "auth.register.duplicate": "Username or email already in use",
        "auth.register.disabled": "Registration is currently disabled",
        "auth.oauth.not_found": "Not Found",
        "auth.oauth.state_invalid": "Invalid or expired OAuth state",
        "auth.oauth.code_missing": "Missing authorization code",
        "auth.oauth.exchange_failed": "OAuth provider exchange failed",
        "auth.oauth.user_fetch_failed": "Could not fetch OAuth user profile",
        "auth.oauth.no_verified_email": "No verified email returned by GitHub",
        "auth.oauth.unlink_needs_password": (
            "You must set a login password before unlinking GitHub — "
            "otherwise you would lock yourself out of the account."
        ),
        "auth.oauth.github_already_linked_to_other": (
            "This GitHub account is already linked to a different user."
        ),
        "auth.oauth.not_linked": "No GitHub account is linked to this user.",
        "auth.oauth.link_success": "GitHub account linked successfully.",
        "auth.oauth.unlink_success": "GitHub account unlinked successfully.",
        "auth.oauth.password_already_set": (
            "A password is already set for this account; use the "
            "change-password flow instead."
        ),

        # ---- change-password (Settings > Password) ----
        "auth.password.wrong_current": "Current password is incorrect",
        "auth.password.same": "New password must differ from the current password",
        "auth.password.rate_limited": "Too many password-change attempts; try again later",
        "auth.password.changed_subject": "Your Argus password was changed",
        "auth.password.changed_body": (
            "Your password was changed from IP {ip} at {user_agent}. "
            "If this was not you, contact support immediately and reset "
            "your password via the Forgot-password flow."
        ),

        # ---- change-email (Settings > Profile) ----
        "auth.email.same": "New email is the same as the current one",
        "auth.email.in_use": "Email is already in use",
        "auth.email.rate_limited": "Too many email-change attempts; try again later",
        "auth.email.change_subject": "Confirm your new email — Argus",
        "auth.email.change_body": (
            "Click this link to confirm the change. If you didn't request "
            "this, ignore this email."
        ),

        # ---- resend-verification (Settings > Profile, #108) ----
        "auth.verify.already_verified": "Your email is already verified",
        "auth.verify.rate_limited": (
            "Please wait before requesting another verification email"
        ),
        "auth.verify.resent": "Verification email sent — check your inbox",

        # ---- admin ----
        "admin.user.not_found": "User not found",
        "admin.user.self_ban": "Cannot ban yourself",
        "admin.flag.invalid_key": "Flag key {key!r} must be [a-z0-9_] or a known default",

        # ---- batch ----
        "batch.not_found": "batch not found",
        "batch.delete_blocked_running": (
            "Cannot delete a running batch. Stop it first."
        ),

        # ---- job ----
        "job.not_found": "job not found",
        "job.start.missing_job_id": "job_start requires job_id",
        "job.epoch.missing_job_id": "job_epoch requires job_id",
        "job.done.missing_job_id": "job_done requires job_id",
        "job.failed.missing_job_id": "job_failed requires job_id",
        "job.delete_blocked_running": (
            "Cannot delete a running job. Stop it first."
        ),

        # ---- host ----
        "host.delete_blocked_active": (
            "Host has reported within the last 10 minutes; "
            "delete refused."
        ),

        # ---- event ----
        "event.schema.unsupported": "Unsupported schema_version",
        "event.schema.event_id_required": (
            "event_id is required on schema_version '1.1'. "
            "Generate a UUID client-side for each event."
        ),
        "event.rate_limit": "Rate limit exceeded",

        # ---- token (API tokens) ----
        "token.not_found": "Token not found",
        "token.scope.invalid": "scope must be 'reporter' or 'viewer'",
        "token.scope.reporter_required": "Reporter scope requires a personal API token",
        "token.scope.viewer_cannot_post": (
            "This token has 'viewer' scope; POST requires 'reporter' (em_live_) tokens."
        ),
        "token.session_required": "This action requires an interactive login session",

        # ---- share ----
        "share.not_found": "Share not found",
        "share.batch.owner_only": "Only the batch owner may manage shares",
        "share.batch.self": "Cannot share a batch with yourself",
        "share.batch.owner_has_access": "Owner already has access; cannot share with self",
        "share.project.self": "Cannot share a project with yourself",
        "share.public.owner_only": "Only the batch owner may manage public shares",
        "share.public.slug_exhausted": "Could not allocate a unique slug",
        "share.user.not_found": "User {username!r} not found",
        "share.user.deactivated": "Cannot share with a deactivated user",

        # ---- project ----
        "project.not_found": "project not found",
        "project.public.unpublished_404": "Project is not published",
        "project.public.publish_conflict": "Project is already published",

        # ---- compare ----
        "compare.too_few": "Compare needs at least 2 batch ids (comma-separated).",
        "compare.too_many": "Compare supports at most {max} batches; got {count}.",
        "compare.batch.not_found": "batch not found: {batch_id}",

        # ---- pins ----
        "pin.batch.not_found": "batch not found",
        "pin.limit_reached": "Pin limit is {limit}; unpin one first.",

        # ---- stars ----
        "star.invalid_target_type": "target_type must be 'project' or 'batch'",

        # ---- studies (v0.2 hyperopt-ui) ----
        "study.invalid_sort": "sort must be one of: value, trial_id, start_time",
        "study.invalid_order": "order must be 'asc' or 'desc'",
        "study.trial_not_found": "Trial not found in this study",

        # ---- SSE / events_stream ----
        "sse.auth.required": "Authentication required (Authorization header or ?token=)",
        "sse.auth.invalid": "Invalid or expired token",
        "sse.batch.no_access": "You do not have access to this batch",
        "sse.admin_required": (
            "Non-admin subscribers must supply a batch_id filter. "
            "Project / host / firehose subscriptions require admin."
        ),
        "sse.rate_limit": "Too many requests; slow down.",

        # ---- admin / deps ----
        "admin.privileges_required": "Admin privileges required",
        "admin.email_verify_required": "Email verification required for this action",

        # ---- public (owner view) ----
        "public.batch.not_found": "batch not found",
        "public.share.not_found": "Public share not found",
        "public.job.not_found": "job not found",
        "public.share.expired": "Public share has expired",

        # ---- guardrails (Team A) ----
        "guardrails.batch.diverged.title": "Batch {batch_id}: training diverged",
        "guardrails.batch.diverged.body": (
            "val_loss grew by {ratio:.1f}× over {window} epochs or became "
            "NaN / Inf. The batch has been flagged as divergent — check "
            "your learning rate, data pipeline, or recent config changes."
        ),
        "guardrails.job.idle.title": "Job {job_id} looks idle",
        "guardrails.job.idle.body": (
            "GPU utilisation has stayed below 5% for more than "
            "{minutes} minutes. The job was not killed; open it to "
            "confirm whether it is still useful."
        ),

        # ---- anomalous login email ----
        "auth.anomalous_login.subject": "New sign-in from an unfamiliar location",
        "auth.anomalous_login.intro": (
            "We spotted a new sign-in to your Argus account. "
            "If this was you, no action is needed."
        ),
        "auth.anomalous_login.ip": "IP address",
        "auth.anomalous_login.user_agent": "Device / browser",
        "auth.anomalous_login.when": "When",
        "auth.anomalous_login.not_you": (
            "If you do not recognise this sign-in, change your password "
            "immediately and revoke any API tokens you no longer use."
        ),

        # ---- admin.backup (status endpoint) ----
        "admin.backup.disabled": "SQLite backup loop is disabled",

        # ---- events (guardrails new types) ----
        "events.batch_diverged.description": "Training diverged",
        "events.job_idle_flagged.description": "Job flagged as idle",
        # ---- bulk operations (v0.1.3 hardening) ----
        "bulk.delete_max_n_exceeded": (
            "Max 500 items per bulk operation. Submit in batches."
        ),

        # ---- token mint cap (v0.1.3 hardening) ----
        "token.mint_cap_exceeded": (
            "You have 50 active tokens. Revoke some before minting more."
        ),

        # ---- SMTP test (v0.1.3 hardening) ----
        "smtp.test.rate_limited": (
            "Too many SMTP test requests; try again later."
        ),
        "smtp.test.host_not_allowed": (
            "SMTP host {host!r} is not in the configured allowlist."
        ),

        # ---- email (Team Email) ----
        "email.template.not_found": "Email template not found",
        "email.template.not_resettable": "Only system templates can be reset to factory defaults",
        "email.template.render_failed": "Template render failed: {error}",
        "email.smtp.host_required": "SMTP host is required",
        "email.subscription.unknown_event": "Unknown event_type values: {events}",
    },

    # ------------------------------------------------------------------
    # Chinese Simplified (zh-CN)
    # ------------------------------------------------------------------
    "zh-CN": {
        # ---- auth ----
        "auth.token.invalid": "令牌无效或已过期",
        "auth.token.used": "令牌已被使用",
        "auth.token.malformed_expiry": "令牌的过期时间格式有误",
        "auth.token.expired": "令牌已过期",
        "auth.token.user_missing": "令牌无效（对应用户不存在）",
        "auth.token.refresh_failed": "无法刷新令牌",
        "auth.token.api_invalid": "API 令牌无效或已过期",
        "auth.token.owner_inactive": "令牌所属用户已被停用",
        "auth.credentials.bad": "邮箱或密码不正确",
        "auth.credentials.required": "需要身份验证",
        "auth.locked": "账号已锁定，请 {minutes} 分钟后再试",
        "auth.verify.expired": "验证链接已过期",
        "auth.register.duplicate": "用户名或邮箱已被使用",
        "auth.register.disabled": "注册功能当前已关闭",
        "auth.oauth.not_found": "未找到",
        "auth.oauth.state_invalid": "OAuth state 无效或已过期",
        "auth.oauth.code_missing": "缺少授权码",
        "auth.oauth.exchange_failed": "OAuth 提供方换取令牌失败",
        "auth.oauth.user_fetch_failed": "无法获取 OAuth 用户信息",
        "auth.oauth.no_verified_email": "GitHub 未返回已验证的邮箱",
        "auth.oauth.unlink_needs_password": "解绑 GitHub 之前必须先设置登录密码，否则将无法再登录账户。",
        "auth.oauth.github_already_linked_to_other": "该 GitHub 账户已绑定到另一位用户。",
        "auth.oauth.not_linked": "当前账户未绑定 GitHub。",
        "auth.oauth.link_success": "GitHub 账户绑定成功。",
        "auth.oauth.unlink_success": "已成功解绑 GitHub 账户。",
        "auth.oauth.password_already_set": "当前账户已设置密码，请通过修改密码功能更新。",

        # ---- change-password (Settings > Password) ----
        "auth.password.wrong_current": "当前密码不正确",
        "auth.password.same": "新密码不能与当前密码相同",
        "auth.password.rate_limited": "修改密码尝试过于频繁，请稍后再试",
        "auth.password.changed_subject": "您的 Argus 密码已被修改",
        "auth.password.changed_body": (
            "您的账户密码已从 IP {ip}（设备：{user_agent}）修改成功。"
            "如果这不是您本人操作，请立即联系支持人员，并使用“忘记密码”流程重置密码。"
        ),

        # ---- change-email (Settings > Profile) ----
        "auth.email.same": "新邮箱与当前邮箱相同",
        "auth.email.in_use": "该邮箱已被使用",
        "auth.email.rate_limited": "修改邮箱尝试过于频繁，请稍后再试",
        "auth.email.change_subject": "确认新邮箱 — Argus",
        "auth.email.change_body": "点击此链接确认变更。若并非您本人操作，请忽略本邮件。",

        # ---- resend-verification (Settings > Profile, #108) ----
        "auth.verify.already_verified": "您的邮箱已完成验证",
        "auth.verify.rate_limited": "请稍后再请求发送验证邮件",
        "auth.verify.resent": "验证邮件已发送，请查收收件箱",

        # ---- admin ----
        "admin.user.not_found": "未找到该用户",
        "admin.user.self_ban": "不能封禁自己",
        "admin.flag.invalid_key": "功能开关键名 {key!r} 必须符合 [a-z0-9_] 或为已知默认项",

        # ---- batch ----
        "batch.not_found": "未找到该批次",
        "batch.delete_blocked_running": "无法删除运行中的批次，请先停止。",

        # ---- job ----
        "job.not_found": "未找到该任务",
        "job.start.missing_job_id": "job_start 事件必须提供 job_id",
        "job.epoch.missing_job_id": "job_epoch 事件必须提供 job_id",
        "job.done.missing_job_id": "job_done 事件必须提供 job_id",
        "job.failed.missing_job_id": "job_failed 事件必须提供 job_id",
        "job.delete_blocked_running": "无法删除运行中的任务，请先停止。",

        # ---- host ----
        "host.delete_blocked_active": (
            "主机最近 10 分钟内仍在上报，拒绝删除。"
        ),

        # ---- event ----
        "event.schema.unsupported": "不支持的 schema_version",
        "event.schema.event_id_required": (
            "schema_version 为 '1.1' 时必须提供 event_id，请在客户端生成 UUID。"
        ),
        "event.rate_limit": "请求过于频繁，已超出速率限制",

        # ---- token (API tokens) ----
        "token.not_found": "未找到该令牌",
        "token.scope.invalid": "scope 必须为 'reporter' 或 'viewer'",
        "token.scope.reporter_required": "Reporter 权限需要使用个人 API 令牌",
        "token.scope.viewer_cannot_post": (
            "当前令牌权限为 'viewer'；POST 请求需要 'reporter'（em_live_）令牌。"
        ),
        "token.session_required": "此操作需要通过交互式登录会话执行",

        # ---- share ----
        "share.not_found": "未找到该共享记录",
        "share.batch.owner_only": "只有批次所有者才能管理共享",
        "share.batch.self": "不能将批次共享给自己",
        "share.batch.owner_has_access": "所有者已有访问权限，无法再次共享给自己",
        "share.project.self": "不能将项目共享给自己",
        "share.public.owner_only": "只有批次所有者才能管理公开共享链接",
        "share.public.slug_exhausted": "无法分配唯一的公开链接标识",
        "share.user.not_found": "未找到用户 {username!r}",
        "share.user.deactivated": "无法共享给已停用的用户",

        # ---- project ----
        "project.not_found": "未找到该项目",
        "project.public.unpublished_404": "项目未公开发布",
        "project.public.publish_conflict": "项目已处于公开状态",

        # ---- compare ----
        "compare.too_few": "对比至少需要 2 个批次 ID（逗号分隔）。",
        "compare.too_many": "对比最多支持 {max} 个批次，当前提供了 {count} 个。",
        "compare.batch.not_found": "未找到批次：{batch_id}",

        # ---- pins ----
        "pin.batch.not_found": "未找到该批次",
        "pin.limit_reached": "固定数量已达上限 {limit}，请先取消固定一个。",

        # ---- stars ----
        "star.invalid_target_type": "target_type 必须为 'project' 或 'batch'",

        # ---- studies (v0.2 hyperopt-ui) ----
        "study.invalid_sort": "sort 仅支持: value、trial_id、start_time",
        "study.invalid_order": "order 仅支持 'asc' 或 'desc'",
        "study.trial_not_found": "该 Study 中未找到对应 Trial",

        # ---- SSE / events_stream ----
        "sse.auth.required": "需要身份验证（请提供 Authorization 请求头或 ?token= 参数）",
        "sse.auth.invalid": "令牌无效或已过期",
        "sse.batch.no_access": "您无权访问该批次",
        "sse.admin_required": (
            "非管理员订阅者必须提供 batch_id 过滤参数；"
            "项目 / 主机 / 全流订阅需要管理员权限。"
        ),
        "sse.rate_limit": "请求过于频繁，请稍后再试。",

        # ---- admin / deps ----
        "admin.privileges_required": "需要管理员权限",
        "admin.email_verify_required": "执行此操作需要先完成邮箱验证",

        # ---- public (owner view) ----
        "public.batch.not_found": "未找到该批次",
        "public.share.not_found": "未找到公开共享记录",
        "public.job.not_found": "未找到该任务",
        "public.share.expired": "公开链接已过期",

        # ---- guardrails (Team A) ----
        "guardrails.batch.diverged.title": "批次 {batch_id}：训练已发散",
        "guardrails.batch.diverged.body": (
            "val_loss 在 {window} 个 epoch 内增长了 {ratio:.1f} 倍，"
            "或出现 NaN / Inf。批次已被标记为 divergent，请检查学习率、"
            "数据管道或最近的配置变更。"
        ),
        "guardrails.job.idle.title": "任务 {job_id} 疑似闲置",
        "guardrails.job.idle.body": (
            "GPU 利用率已连续 {minutes} 分钟低于 5%。任务未被终止，"
            "请打开任务详情确认是否仍需继续运行。"
        ),

        # ---- anomalous login email ----
        "auth.anomalous_login.subject": "检测到来自陌生位置的新登录",
        "auth.anomalous_login.intro": (
            "我们检测到您的 Argus 账户在一个新位置登录。"
            "如果是您本人操作，可忽略此邮件。"
        ),
        "auth.anomalous_login.ip": "IP 地址",
        "auth.anomalous_login.user_agent": "设备 / 浏览器",
        "auth.anomalous_login.when": "时间",
        "auth.anomalous_login.not_you": (
            "如果这次登录不是您本人操作，请立即修改密码，"
            "并撤销不再使用的 API 令牌。"
        ),

        # ---- admin.backup (status endpoint) ----
        "admin.backup.disabled": "SQLite 备份循环已禁用",

        # ---- events (guardrails new types) ----
        "events.batch_diverged.description": "训练已发散",
        "events.job_idle_flagged.description": "任务已标记为闲置",
        # ---- bulk operations (v0.1.3 hardening) ----
        "bulk.delete_max_n_exceeded": "单次批量操作最多 500 项，请分批提交。",

        # ---- token mint cap (v0.1.3 hardening) ----
        "token.mint_cap_exceeded": "您已有 50 个活动 token，请先撤销部分后再创建。",

        # ---- SMTP test (v0.1.3 hardening) ----
        "smtp.test.rate_limited": "SMTP 测试请求过于频繁，请稍后再试。",
        "smtp.test.host_not_allowed": "SMTP 主机 {host!r} 不在已配置的白名单中。",

        # ---- email (Team Email) ----
        "email.template.not_found": "邮件模板不存在",
        "email.template.not_resettable": "仅系统模板可重置为出厂默认",
        "email.template.render_failed": "模板渲染失败：{error}",
        "email.smtp.host_required": "必须提供 SMTP 主机",
        "email.subscription.unknown_event": "未知的事件类型：{events}",
    },
}

_FALLBACK_LOCALE = "en-US"


def tr(locale: str, key: str, **fmt: object) -> str:
    """Return the translated string for *key* in *locale*.

    Resolution order:
    1. ``MESSAGES[locale][key]``
    2. ``MESSAGES["en-US"][key]``  (fallback locale)
    3. ``key`` itself             (missing-key sentinel)

    After resolving the raw string, ``str.format(**fmt)`` is applied when
    *fmt* is non-empty.  All errors are swallowed — this helper must never
    raise in a hot path.
    """
    locale_dict = MESSAGES.get(locale) or MESSAGES.get(_FALLBACK_LOCALE, {})
    raw = locale_dict.get(key)
    if raw is None:
        raw = MESSAGES.get(_FALLBACK_LOCALE, {}).get(key, key)
    if not fmt:
        return raw
    try:
        return raw.format(**fmt)
    except (KeyError, IndexError):
        return raw
