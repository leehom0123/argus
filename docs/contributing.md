# Contributing

Thanks for considering a contribution. Argus is Apache-2.0; PRs and issues
are welcome. The full developer guide is in
[`CONTRIBUTING.md`](https://github.com/leehom0123/argus/blob/main/CONTRIBUTING.md)
at the repo root — this page is a quick orientation.

## Repo layout

```
argus/
├── backend/      FastAPI + async SQLAlchemy + Alembic     ← Python ≥3.10
├── frontend/     Vue 3 + TypeScript + Vite                ← Node ≥20, pnpm
├── client/       argus-reporter SDK                       ← Python ≥3.10
├── schemas/      event_v1.json (versioned wire contract)
├── deploy/       Dockerfile, compose, .env.example
└── docs/         MkDocs Material site (you are here)
```

## Local dev loops

### Backend

```bash
cd backend
pip install -e ".[dev]"
alembic upgrade head
uvicorn backend.app:app --reload
pytest                                # whole suite
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev                              # vite dev server, proxies to localhost:8000
pnpm test                             # vitest
pnpm build                            # production bundle
```

### SDK

```bash
cd client
pip install -e ".[dev]"
pytest
python -m build                       # build wheel for PyPI
```

### Docs

```bash
pip install -r requirements-docs.txt
mkdocs serve                          # local preview
mkdocs build --strict                 # CI uses --strict
```

## Style

* Python: ruff + black; run `ruff check . && ruff format .` before pushing.
* Vue / TS: ESLint + Prettier; `pnpm lint` before pushing.
* Bilingual docs: each English page under `docs/...` has a Chinese mirror
  under `docs/zh/...` with the same path. CI checks parity.

## Adding a new event field

A new optional field requires bumping the **minor** version of
`schemas/event_v1.json`:

1. Update `schemas/event_v1.json` with the new field as optional.
2. Update the SDK in `client/argus/schema.py` to mirror.
3. Add a backend handler in `backend/backend/api/events.py` if the field is
   stored or surfaced.

Wire-breaking changes require a major bump and a coordinated SDK release.

## Adding an Alembic migration

* Chain after the current head — never branch.
* Name files `NNN_short_description.py` matching the existing
  numbering convention.
* Test both `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
  on **both** SQLite and PostgreSQL before merging.

## Adding a new framework integration

Drop `client/argus/integrations/<framework>.py` exporting `ArgusCallback`.
Use the lazy-import pattern from `lightning.py` / `keras.py` so missing
optional deps do not break the SDK install. Add an extra in
`client/pyproject.toml`.

## CI

The GitHub Actions workflow runs:

* Backend pytest (SQLite + Postgres matrix).
* Frontend vitest.
* Client pytest.
* `mkdocs build --strict`.
* Bilingual parity check.

Green CI is required to merge.

## Reporting issues

* **Security**: see [`SECURITY.md`](https://github.com/leehom0123/argus/blob/main/SECURITY.md) — please do not open a public issue for vulnerabilities.
* **Bugs**: include the Argus version, the SDK version, and the smallest
  reproducer you can manage.
* **Features**: explain the use case first; we will figure out the shape together.

## Code of conduct

By participating you agree to the
[Code of Conduct](https://github.com/leehom0123/argus/blob/main/CODE_OF_CONDUCT.md).
