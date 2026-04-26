# Code Review — `feat/team-pwd` (Change Password)

**Reviewer**: independent code-review pass (read-only)
**Scope**: `feat/team-pwd` vs `main` — 3 commits (`5a0c27c`, `2e71dca`, `d060e1b`)
**Files touched**: 13 (+907 / -1)

---

## TL;DR — **REQUEST_CHANGES**

One **blocking** security regression, two **RED** issues, plus a handful of nits.
Once the blocker (API-token bypass) and the revoke-self-on-apitoken edge case are fixed,
this is ready to land. The implementation is otherwise thoughtful: atomic audit-in-commit,
per-user bucket, argon2 reuse, and the "keep caller JWT alive" UX choice is defensible.

**Security rating**: **MEDIUM-HIGH risk as-merged** → **LOW risk after fixes**.

---

## BLOCKING — API-token bypass (the self-flagged HIGH PRIORITY concern is REAL)

**Verdict: RED — must fix before merge.**

`backend/backend/api/auth.py:718-722`:

```python
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),    # <-- wrong dep
    ...
)
```

Grounded evidence:

- `backend/backend/deps.py:74-127` — `get_current_user` accepts **both** JWT *and*
  `em_live_*` / `em_view_*` API tokens. Docstring explicitly says so.
- `backend/backend/deps.py:234-248` — `require_web_session` exists and its docstring
  calls out this exact use case: *"Used for ops that should never be driven by an
  opaque long-lived token: **password change**, token revoke, share management, etc."*
- **So the feature author wrote the correct guard dep, placed it one module over,
  and then forgot to wire it into the one route its docstring names.**

**Impact**: a holder of a reporter (`em_live_*`) or viewer (`em_view_*`) API token
can POST `/api/auth/change-password` and (provided they know the current password,
which they shouldn't — but a reporter token is often shipped in CI env vars that
someone with ops access can grab) change the account password. Reporter tokens are
supposed to be scoped to ingest — elevation to full account takeover is a scope
escalation.

**Fix** (one line, `backend/backend/api/auth.py:722`):

```python
-    user: User = Depends(get_current_user),
+    user: User = Depends(require_web_session),
```

…and add a corresponding test (see **Concern 8**).

---

## Concerns (one-by-one)

### 1. API-token bypass — **RED** (blocking, see above)

### 2. Session-revocation race — **YELLOW**

Two sub-issues in `auth.py:748-761`:

**2a. `jti is None` revokes the caller's own session (depends on fix #1).**
`request.state._auth_jwt_jti` is only set in the JWT branch of `get_current_user`
(`deps.py:152-153`). If the caller authed via API token, `current_jti = None`,
and the condition `if current_jti is not None and row.jti == current_jti` is `False`
for every row → every live session gets revoked, **including any JWT the same user
has open in a browser**. Denial-of-service against the user's own logged-in tabs.
**This is moot if blocker #1 is fixed** (API-token callers can't reach the handler
at all); keeping it listed because if the wrong-dep regressions ever reappears,
this amplifies the blast radius.

**2b. Concurrent-login race (genuine but low-severity).**
Between `SELECT ... WHERE revoked_at IS NULL` and the `UPDATE` loop, a fresh
`/api/auth/login` can insert a new `active_sessions` row. That row's `jti`
was not in the SELECT result set, so it escapes revocation. An attacker with
the stolen password (and network access to `/login`) has a millisecond-wide
window to mint a JWT that survives the rotation.

**Mitigation**: acceptable as-is given the narrow window, but a defensive
single `UPDATE ... WHERE user_id = :uid AND jti != :current_jti AND revoked_at IS NULL`
issued *after* the password is hashed (so logins with the old password during
the window also fail) would close the gap. File a follow-up issue; don't block
on it.

### 3. Rate-limit semantics — **YELLOW**

- `backend/backend/utils/ratelimit.py:162`: `_CHANGE_PASSWORD_BUCKET` is an
  in-process singleton. Container restart / redeploy = bucket resets. The concern
  doc called this out; confirmed. Document it explicitly in the docstring, file
  a follow-up to migrate to Redis when `TODO: multiprocess backend` is done. For
  the current single-worker uvicorn deployment this is fine.
- Bucket key = `user_id` (not IP + user_id). Legitimate user who typos their
  password 5× from home then 1× from a café is locked out for ~1 hour. **Accept**
  — the other direction (IP-keyed) lets an attacker bypass by rotating IPs, which
  is strictly worse. Flag in the error message so the user knows to wait.
- Email fires on **every** successful change. A user burning through 5/hour via
  legit password rotations gets up to 5 emails — fine, that's actually the point
  of the notification.

### 4. Email-fail-silent — **YELLOW**

`auth.py:832-843`: SMTP failure is caught, logged, and swallowed — the password
is already persisted via `db.commit()` above. Attacker pattern: change password +
tie up SMTP with junk → victim gets no notification.

**Recommendation**: keep fail-silent (blocking on SMTP during a password change
is worse UX than the marginal security gain), but **queue a retry** (simple
APScheduler / background task — the project already uses one) so a transient
SMTP hiccup doesn't permanently swallow the notice. Follow-up issue, not a
blocker.

Also: the email body (`email.py:247-252`) contains IP + user_agent, which is the
right metadata, but does **not** include a "if this wasn't you, click here to
lock your account" CTA. Consider adding one in a follow-up.

### 5. Frontend UX — **GREEN with one NIT**

- `Password.vue:76` — redirects to `/` (Dashboard), not `/login`. This is
  **correct** because the caller's own JWT is preserved server-side. The concern
  doc's worry about "unnecessary logout" doesn't apply. GREEN.
- Three inputs (current / new / confirm) are wired, `autocomplete` attrs set
  correctly (`current-password` / `new-password`), confirm-match error clears
  reactively via `computed`. GREEN.
- i18n: both `zh-CN.ts:759-776` and `en-US.ts:761` contain the full
  `page_settings_password` block — 16 keys, all covered. GREEN.
- **NIT** (`Password.vue:132-136`): the `:help=` ternary has both branches
  returning `hint_min_length` — the else branch should be an empty string,
  otherwise the hint text is always visible even when the input is valid. This
  is cosmetic.

### 6. Password policy — **GREEN**

- Minimum length is **10** (not 8 as the concern doc claims — verify against
  `schemas/auth.py:18-33`, `_validate_password_strength`). 10 + must-contain
  letter-and-digit is reasonable for research-users UX; slightly below the
  modern NIST 12+ recommendation but consistent with the rest of the codebase
  (register / reset both use the same validator).
- No haveibeenpwned check — acceptable for MVP, file follow-up.

### 7. Audit log — **GREEN**

- `auth.py:802-814` writes `password_changed` in-transaction with the hash
  update → atomic. Metadata is `{revoked_other_sessions: int, kept_current_jti: bool}`
  — no password, no hash. GREEN.
- `auth.py:742-750` writes `password_change_rate_limited` on 429. Good.
- `auth.py:770-777` writes `password_change_failed` with `reason: wrong_current`
  on 401. Good.
- Missing: IP + user_agent on the `password_changed` row. The `log` method
  takes `ip=ip` already (passed), but `user_agent` isn't passed through.
  Follow-up nit.

### 8. Test coverage gaps — **YELLOW**

`backend/backend/tests/test_password_change.py` has 7 tests (the concern doc
says 8 — count mismatch, but immaterial). Coverage:

- Happy path + other-session revocation ✓
- Wrong current → 401 ✓
- Same-as-current → 400 ✓
- Too-short new → 422 ✓
- Unauth'd → 401 ✓
- Cross-user isolation (bob's JWT untouched) ✓
- Rate limit 6th attempt → 429 ✓
- Email sent ✓

**Missing**:
- **API-token rejection test** — directly tied to blocker #1. Add:
  ```python
  async def test_change_password_rejects_api_token(client, api_token_factory):
      token = await api_token_factory(scope="reporter")
      r = await client.post("/api/auth/change-password",
          json={"current_password": "...", "new_password": "..."},
          headers={"Authorization": f"Bearer {token}"})
      assert r.status_code == 403  # require_web_session
  ```
- No test for `jti is None` path (belt-and-braces even after fix #1 lands).
- No test for "revoke_other_sessions" count being correctly reported in the
  audit log metadata.

---

## Summary verdict matrix

| # | Concern               | Verdict | Blocking? |
|---|-----------------------|---------|-----------|
| 1 | API-token bypass      | RED     | YES       |
| 2 | Session revoke race   | YELLOW  | No        |
| 3 | Rate-limit semantics  | YELLOW  | No        |
| 4 | Email fail-silent     | YELLOW  | No        |
| 5 | Frontend UX           | GREEN   | No        |
| 6 | Password policy       | GREEN   | No        |
| 7 | Audit log             | GREEN   | No        |
| 8 | Test coverage gaps    | YELLOW  | Partial (add API-token test alongside fix #1) |

## Required before merge

1. `backend/backend/api/auth.py:722` — swap `get_current_user` → `require_web_session`.
2. `backend/backend/tests/test_password_change.py` — add API-token-rejection test.

## Recommended (follow-up tickets)

- Close the concurrent-login race with an atomic `UPDATE` (concern 2b).
- Queue password-changed email retries on SMTP failure (concern 4).
- Add `user_agent` field to the `password_changed` audit row (concern 7).
- Document in-process bucket reset-on-restart + migrate to Redis when multiworker
  support lands (concern 3).
- Clean up `Password.vue:132-136` help-ternary (concern 5 nit).
