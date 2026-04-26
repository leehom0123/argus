# Admin settings

Argus has DB-backed runtime configuration. An admin can change GitHub OAuth
credentials, SMTP, retention caps, demo project, and feature flags
**without redeploying** — saved values live in the `system_config` table
and are read on each request.

The corresponding `ARGUS_*` env vars act only as **seed defaults** for the
first boot. Read precedence:

```
DB row > ARGUS_* env > built-in default
```

## Open the panel

Sign in as an admin (the first registered account is auto-admin), then go to
**Settings → Admin**. Non-admins cannot see the section.

The panels (one per concern) are:

| Panel | Path on disk | What it edits |
|---|---|---|
| OAuth (GitHub) | `frontend/src/pages/settings/admin/OAuthGithub.vue` | Enabled flag, client id, client secret |
| SMTP | `frontend/src/pages/settings/admin/Smtp.vue` | Host, port, user, password, From:, TLS |
| Retention | `frontend/src/pages/settings/admin/Retention.vue` | Days per data type |
| Demo project | `frontend/src/pages/settings/admin/DemoProject.vue` | Synthetic project visibility toggle |
| Feature flags | `frontend/src/pages/settings/admin/FeatureFlags.vue` | UI feature switches |
| Security | `frontend/src/pages/settings/admin/Security.vue` | JWT rotation, anomalous-login alert |

## OAuth (GitHub)

Configure under *Settings → Admin → OAuth (GitHub)*:

* **Enabled** — master switch; when off, the GitHub button on Login is hidden.
* **Client ID** — public.
* **Client secret** — encrypted at rest.
* The callback URL the user has to register with github.com is
  `${ARGUS_BASE_URL}/api/oauth/github/callback`.

Environment fallback: `ARGUS_GITHUB_OAUTH_ENABLED`,
`ARGUS_GITHUB_CLIENT_ID`, `ARGUS_GITHUB_CLIENT_SECRET`. The DB row wins.

## SMTP

Configure under *Settings → Admin → SMTP*:

| Field | Notes |
|---|---|
| Host / Port | Default port 587 (STARTTLS) |
| User / Password | Encrypted at rest |
| From address | "From:" header on outgoing mail |
| Use TLS | STARTTLS toggle |

If SMTP is left blank, the email worker prints what it would have sent and
moves on — fine for dev. Verify-email and password-reset flows go through
the same delivery.

Environment fallback: the seven `ARGUS_SMTP_*` vars in
[Configuration](configuration.md).

## Retention

| Field | Default |
|---|---|
| Snapshots | 7 days |
| Log lines | 14 days |
| Job epochs | 30 days |
| Other events | 90 days |
| Demo data | 1 day |
| Sweep interval | 60 minutes |

Summary rows (one per batch and one per job) are **never purged**.

## Demo project

When on, a synthetic project + batch + job is created on first boot so the
dashboard isn't empty. Demo data falls under the *demo data* retention day
cap (intentionally short).

## Feature flags

A small set of UI feature switches managed via the admin panel; new flags
are added in code as needed.

## Security panel

* **JWT rotation** — `GET /api/admin/jwt/status` shows the active key and
  any verify-only keys; `POST /api/admin/jwt/rotate` issues a new key.
  Verification accepts any non-expired key, so live sessions ride through
  a rotation without forced logout.
* **Anomalous-login alerting** — toggle for the email sent when a login
  happens from a new (IP, user-agent) pair (`ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED`).
* **Audit log** — `GET /api/admin/audit-log` records every admin-settings
  change (actor, target, before/after with secrets masked, timestamp).

## Encryption details

Secret fields (OAuth client secret, SMTP password) are wrapped with
**Fernet** (AES-128-CBC + HMAC-SHA256) at rest in the `system_config` table.
The Fernet key derives from:

1. `ARGUS_CONFIG_KEY` (preferred — set this so JWT rotation does not
   touch encrypted config).
2. otherwise `ARGUS_JWT_SECRET` (fallback for old deployments — legacy).

If `ARGUS_CONFIG_KEY` is unset and any encrypted rows exist, a one-shot
warning is logged at startup. See `backend/backend/services/secrets.py` for
the full key-derivation contract.

## See also

* [Configuration](configuration.md) — bootstrap env vars (seed values).
* [Argus Agent](argus-agent.md) — agent register / heartbeat / poll endpoints.
* [Backups & retention](retention.md) — what the sweeper actually purges.
