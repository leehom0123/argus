# Profile and settings

The **Settings** area collects per-user controls. The frontend pages live
under `frontend/src/pages/settings/`:

| Page | What it does |
|---|---|
| `Notifications.vue` | Per-user email preferences |
| `Password.vue` | Change password |
| `About.vue` | Build info |
| `admin/*` | Admin-only panels (see [Admin settings](../ops/admin-settings.md)) |

## Account

* **Email** — change requires verification of the new address (link valid
  `ARGUS_EMAIL_CHANGE_TTL_HOURS`, default 7 days; resend rate-limited).
* **Password** — argon2id; minimum length `ARGUS_PASSWORD_MIN_LENGTH`
  (default 10 characters).
* **Locale** — English / 简体中文 (also switchable per session via the
  language button).

Email-change verifies the new address before switching. Until verified the
old address still receives notifications.

## Notifications (per-user)

`Notifications.vue` toggles:

* **Email on batch done**
* **Email on job failed**
* **Daily digest**

These ride alongside per-project notification recipients (set on the
project's *Settings → Recipients* page) — see [Notifications](notifications.md)
for resolution rules.

Unsubscribe links in every email map to a one-click endpoint that flips the
matching preference off.

## Tokens

Manage `em_live_…` SDK tokens in the Tokens page.

* **Create** mints a new token; the value is shown **once**.
* **Revoke** invalidates immediately; in-flight events from that token
  start receiving 401.
* Tokens are stored hashed.

## GitHub linking

Optional. Click **Link GitHub** to start the OAuth flow at
`/api/oauth/github/link/start` — requires the admin to have configured a
GitHub OAuth app. Once linked, sign in with GitHub.

## Admin tab (admins only)

The **Admin** section is visible only to admin accounts and edits DB-backed
runtime config. See [Admin settings](../ops/admin-settings.md).

## See also

* [Notifications](notifications.md)
* [Sharing](sharing.md)
