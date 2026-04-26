# Sharing

There are two kinds of sharing: **per-batch share links** (token URLs that
unlock one batch for anyone) and **public projects** (visibility flag that
makes every batch in a project read-only public).

## Per-batch share links

From a batch detail page, **Share → Create link** gives you a URL of the
form:

```
https://argus.example.com/share/<token>
```

The token is a long URL-safe random string. Stored hashed; the actual value
is shown only once.

### What viewers see

Anyone with the link, no login required, sees:

* The batch detail page (header, JobMatrix, loss curves, log tail)
* Resource snapshots and per-job histories
* The recorded command and env snapshot

What they do **not** see:

* Other batches in the same project
* The action bar (Stop / Rerun / Share / Delete are hidden)
* Any other project's data

### Revoke

The same **Share** menu lists tokens with a revoke icon. Revoke makes the
link immediately 404.

## Public projects

For a fully public project (e.g. an open-source benchmark suite), set
**Visibility = Public** on **Project settings**. Every batch in that
project becomes accessible at the normal URLs without a login — no token
needed.

This is a heavier hammer; prefer per-batch share links for one-off sharing.

## Backend routers

* `shares` API — per-batch tokens (`backend/backend/api/shares.py`)
* `public` API — owner-public + public-projects routes
  (`backend/backend/api/public.py`)

## See also

* [Projects & batches](projects-batches.md) — visibility settings.
* [Profile & settings](profile-settings.md) — account controls.
