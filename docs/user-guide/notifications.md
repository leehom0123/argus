# Notifications

Notifications fire when a batch transitions to *done*, a job transitions to
*failed*, or — if subscribed — once a day as a digest. Delivery is run by an
in-process worker that watches fresh DB rows; no external queue required.

## Channels

| Channel | When it's used |
|---|---|
| **Email (SMTP)** | Configured at *Admin → SMTP* (or via the seven `ARGUS_SMTP_*` env vars) |
| **Feishu webhook** | Set `ARGUS_FEISHU_WEBHOOK` in the env; the watchdog registers a `FeishuNotifier` channel into `app.state.notification_channels` |

If no channel is configured, the worker logs the message it would have sent
and moves on — fine for dev.

## Recipient resolution

For each event the dispatcher resolves recipients in this order:

1. **Project recipients** — added via *Project → Settings → Recipients*
   (#116). A list of users; events route to all of them whether or not
   they own the batch.
2. **Batch owner**, if not already in the project recipient list.
3. Each candidate user's per-user preferences (`Notifications.vue`) are
   then applied — if they have *email on batch done* off, no email is
   sent to them even if they are a recipient.

This is how teams have all senior collaborators copied without spamming
people who do not want this kind of pager.

## Per-user preferences

In **Settings → Notifications**:

| Setting | Effect |
|---|---|
| Email on batch done | One mail per batch transition to *done* |
| Email on job failed | One mail per job transition to *failed* |
| Daily digest | Roll-up of yesterday's activity |

Each email also carries a one-click unsubscribe link that flips the
matching preference off.

## Templates

HTML emails are rendered with Jinja2 from
`backend/backend/emails/templates/`. SMTP MIME type is
`multipart/alternative` (HTML + plain-text).

## Verify-email & password reset

Two transactional flows share the same SMTP path:

* **Verify email** on registration / email change — link valid
  `ARGUS_EMAIL_VERIFY_TTL_HOURS` (default 24) for register, or
  `ARGUS_EMAIL_CHANGE_TTL_HOURS` (default 168) for change.
* **Reset password** — link valid `ARGUS_PASSWORD_RESET_TTL_MINUTES`
  (default 15), single-use.

Both flows have a per-address resend rate limit.

## Feishu webhook

Set `ARGUS_FEISHU_WEBHOOK` to a Feishu *bot* webhook URL. The dispatcher
posts a card per batch-done / job-failed. There is no per-user / per-channel
routing for Feishu — the webhook receives every routed notification.

## Anomalous-login alerting

When `ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED=true` (default), an
informational email is sent on a successful login from a new
`(remote_ip, user_agent)` pair not seen in the last 30 days.

## Debugging

* Tail backend logs for entries from `notifications.watchdog`.
* The watchdog runs as a background task started from `app.py`.

## See also

* [Admin settings](../ops/admin-settings.md) — configure SMTP / Feishu.
* [Profile & settings](profile-settings.md) — per-user toggles.
