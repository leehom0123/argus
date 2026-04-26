# Release process for argus-reporter

## Prerequisites

- PyPI project `argus-reporter` must exist (create at https://pypi.org/manage/projects/).
- PyPI Trusted Publishing is the supported path; see the in-file note in
  `.github/workflows/release-sdk.yml` for the publisher fields. Long-lived
  `PYPI_API_TOKEN` secrets still work but are not recommended.

## Steps

1. **Bump the version** in `client/pyproject.toml` (and `__version__` in
   `client/argus/__init__.py` — they must agree):
   ```toml
   version = "0.X.Y"
   ```

2. **Rebuild the in-tree wheel** (keeps `client/dist/` and downstream `tools/wheels/` current):
   ```bash
   cd client
   python -m build --sdist --wheel
   # copy new wheel to DeepTS-Flow-Wheat/tools/wheels/ if you maintain that fallback
   ```

3. **Commit the changes**:
   ```bash
   git add client/pyproject.toml client/argus/__init__.py \
           client/dist/ client/README.md client/README.zh-CN.md
   git commit -m "chore(client): bump argus-reporter to 0.X.Y"
   ```

4. **Tag the release** — the workflow triggers only on tags matching `reporter-v*`:
   ```bash
   git tag reporter-v0.X.Y
   git push origin reporter-v0.X.Y
   ```

5. **Watch the workflow** at `.github/workflows/release-sdk.yml`:
   - Builds sdist + wheel inside `client/`
   - Uploads `client/dist/*` as a GitHub Actions artifact (for audit)
   - Publishes to PyPI via `pypa/gh-action-pypi-publish`

6. **Verify** the release appeared at https://pypi.org/project/argus-reporter/.

## Version scheme

`0.MINOR.PATCH` — no semver guarantees yet (alpha stage). Increment `PATCH` for bug fixes /
dependency updates, `MINOR` for new event types or API additions. Current released line is
`0.3.x` (context-manager API + built-in heartbeat / stop-poll / resource snapshot daemons).

## Trusted publishing (recommended)

Instead of a long-lived API token you can use PyPI's OIDC trusted publishing:

1. On PyPI: project settings → Publishing → Add a new publisher → GitHub Actions
   - Owner: `leehom0123`, repo: `argus`, workflow: `release-sdk.yml`,
     environment: `pypi` (optional but recommended).
2. Remove the `password:` line from the `pypa/gh-action-pypi-publish` step in the workflow and
   add `with: { attestations: true }` instead.
3. Delete the `PYPI_API_TOKEN` secret from the repo — it is no longer needed.
