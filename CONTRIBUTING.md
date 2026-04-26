# Contributing to Argus

Thank you for contributing. This guide covers dev setup, code style, commit conventions, and the PR process.

---

## Table of contents

1. [Project layout](#project-layout)
2. [Dev environment setup](#dev-environment-setup)
3. [Code style](#code-style)
4. [Commit message convention](#commit-message-convention)
5. [Running tests](#running-tests)
6. [Opening a PR](#opening-a-pr)
7. [Branching strategy](#branching-strategy)
8. [Licensing & DCO](#licensing--dco)

---

## Project layout

```
argus/
├── backend/      FastAPI service, SQLAlchemy models, Alembic migrations, pytest suite
├── frontend/     Vue 3 + Vite + ant-design-vue UI; Pinia stores; i18n in src/locales
├── client/       Python reporter library that experiments import to stream events
├── deploy/       docker-compose, k8s manifests, deployment helper scripts
├── docs/         user-facing documentation and architecture notes
├── schemas/      JSON / OpenAPI schemas shared between backend and client
└── scripts/      maintenance and tooling scripts (e.g. i18n_lint.py)
```

---

## Dev environment setup

### Prerequisites

- Node 20+ and pnpm 9+
- Python 3.11+ with a virtual env
- PostgreSQL (or SQLite for local dev)

### Steps

```bash
# 1. Clone
git clone https://github.com/leehom0123/argus.git
cd argus

# 2. Install backend (editable, with dev extras: pytest / black / ruff / mypy)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
cd backend && pip install -e ".[dev]" && cd ..

# 3. Install frontend dependencies
cd frontend && pnpm install && cd ..

# 4. Set up the database (Alembic migrations)
cp .env.example .env               # edit DB connection string
alembic upgrade head

# 5. Start dev servers
pnpm --dir frontend dev            # Vite frontend (localhost:5173)
uvicorn backend.main:app --reload  # FastAPI backend (localhost:8000)
```

---

## Code style

| Layer | Tooling |
|-------|---------|
| Python (backend, client) | `black .` (formatter, 88-col) + `ruff check .` (linter, replaces isort/flake8) |
| TypeScript / Vue (frontend) | `prettier --write .` + `vue-tsc --noEmit`; Vue 3 **composition API** with `<script setup lang="ts">` |
| CSS | Prettier handles |

**Backend conventions**: `black` formats and `ruff` lints (import order, unused
imports, common bug patterns). Type-annotate all public functions, prefer
async/await over callbacks. 88-column lines.

**Frontend conventions**: Vue 3 composition API (no Options API in new code), Pinia
stores for cross-component state, `ant-design-vue` v4 components, i18n keys in both
`en-US.ts` and `zh-CN.ts` (parity enforced by `python scripts/i18n_lint.py`).

Run all linters before committing:
```bash
black --check .
ruff check .
pnpm --dir frontend typecheck
python scripts/i18n_lint.py
```

---

## Commit message convention

We follow [Conventional Commits](https://www.conventionalcommits.org/).

```
<type>(<scope>): <subject>
```

**Types**: `feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `ci`

**Scopes** (examples): `backend` / `frontend` / `db` / `api` / `ui` / `ci`

**Examples**:
```
feat(ui): add bilingual metric chart export
fix(backend): race condition in experiment status update
docs(api): update OpenAPI description for /runs endpoint
refactor(db): migrate Alembic revision to UUID primary keys
test(backend): add unit tests for event aggregation
chore(ci): upgrade Node to v20 in GitHub Actions
```

Rules:
- Subject line 50 chars max, no trailing period
- Body wrapped at 72 chars
- Use imperative mood: "add" not "added"

Install the commit template so your editor pre-fills the format:
```bash
git config commit.template .gitmessage
```

---

## Running tests

The backend test suite runs against **both SQLite and PostgreSQL** — CI runs
both in a matrix. Locally you can run either (or both):

```bash
# Backend unit tests — default (SQLite in-memory/file)
pytest backend/tests/

# Backend against PostgreSQL (docker-compose brings up a throwaway DB)
docker compose -f deploy/docker-compose.test.yml up -d postgres
DATABASE_URL=postgresql+asyncpg://em:em@localhost:5433/em_test pytest backend/tests/
docker compose -f deploy/docker-compose.test.yml down

# Coverage report
pytest backend/tests/ --cov=backend --cov-report=html

# Frontend type check
pnpm --dir frontend typecheck

# Frontend unit tests (vitest)
pnpm --dir frontend test:unit

# i18n parity (en-US <-> zh-CN), run from repo root
python scripts/i18n_lint.py
```

If your change only touches the frontend you can skip the PostgreSQL lane
locally — CI still runs it.

---

## Opening a PR

1. Fork and create a feature branch (see [Branching strategy](#branching-strategy))
2. Make changes, commit using the convention above
3. Push and open a PR against `main`
4. Fill in the PR template — summary, type, test plan

### PR checklist

Before requesting review, please confirm:

- [ ] `pytest backend/tests/` passes locally
- [ ] `pnpm --dir frontend test:unit` passes locally (when frontend changed)
- [ ] `black --check .` and `ruff check .` are clean
- [ ] i18n parity check passes — `python scripts/i18n_lint.py`
- [ ] Docs updated when public API, CLI flags, or env vars changed
- [ ] CI is green (lint + unit tests + i18n)

For large changes, open a draft PR early to discuss the approach before investing more work. PRs are **squash-merged** to keep `main` history linear.

---

## Branching strategy

- `main` — always deployable, protected, squash-merge only
- `feat/<short-name>` — new features
- `fix/<short-name>` — bug fixes
- `chore/<short-name>` — maintenance, deps, refactors that aren't user-facing

Delete the branch after merge. Long-lived branches (release branches, etc.) are not used pre-v0.1.

---

## Licensing & DCO

`Argus` is licensed under the **Apache License, Version 2.0** (see
[`LICENSE`](./LICENSE) at the repo root). By opening a pull request, you agree
that:

1. **Your contribution is licensed under Apache-2.0.** We do not require a
   separate Contributor License Agreement (CLA). The act of opening a PR is
   taken as consent for your changes to ship under the project's existing
   license, including the patent grant in Apache-2.0 §3.
2. **You have the right to submit the contribution.** Either the work is
   your own, or you have permission to contribute it under Apache-2.0. We
   do **not** require a DCO sign-off (no `git commit -s` requirement); the
   Apache-2.0 inbound=outbound license terms apply by virtue of opening
   the PR.
3. **Third-party code included in a PR must be compatible** with Apache-2.0
   (MIT, BSD-2/3, ISC, Apache-2.0). If you are unsure, call it out in the PR
   description and flag the license so reviewers can check compatibility.

A DCO sign-off is **not required**. Sign-offs (`git commit -s`) are accepted
but never enforced; opening a PR is sufficient consent under Apache-2.0.

New files with significant original authorship should carry the standard
Apache-2.0 header or be implicitly covered by the repo-level `LICENSE` —
please mirror the style of nearby files.

---
---

# 贡献指南（中文）

感谢你的贡献。本指南涵盖开发环境搭建、代码风格、提交规范和 PR 流程。

---

## 目录

1. [开发环境搭建](#开发环境搭建)
2. [代码风格](#代码风格)
3. [提交信息规范](#提交信息规范)
4. [运行测试](#运行测试)
5. [提交 PR](#提交-pr)
6. [许可与 DCO](#许可与-dco)

---

## 开发环境搭建

### 前置条件

- Node 20+ 和 pnpm 9+
- Python 3.11+（建议使用虚拟环境）
- PostgreSQL（本地开发可用 SQLite）

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/leehom0123/argus.git
cd argus

# 2. 安装前端依赖
pnpm install

# 3. 安装后端依赖
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# 4. 初始化数据库（Alembic 迁移）
cp .env.example .env               # 编辑数据库连接字符串
alembic upgrade head

# 5. 启动开发服务
pnpm dev                           # Vite 前端（localhost:5173）
uvicorn backend.main:app --reload  # FastAPI 后端（localhost:8000）
```

---

## 代码风格

| 层级 | 工具 |
|------|------|
| Python（后端） | `ruff check .` + `ruff format .`（Black 兼容，替代 isort） |
| TypeScript / Vue（前端） | `prettier --write .` + `vue-tsc --noEmit`；Vue 3 **组合式 API**，统一使用 `<script setup lang="ts">` |
| CSS | Prettier 处理 |

**后端约定**：ruff 自动排序 import、所有公共函数加类型注解、优先用
async/await、Black 风格 88 列换行。

**前端约定**：新增代码全部使用组合式 API（不再引入 Options API）；跨组件状态
放 Pinia；UI 组件使用 `ant-design-vue` 4.x；i18n 新键须同时加入 `en-US.ts` 和
`zh-CN.ts`（`pnpm test:i18n` 强制校验一致性）。

提交前执行所有 lint 检查：
```bash
ruff check . && ruff format --check .
pnpm --dir frontend typecheck
pnpm --dir frontend test:i18n
```

---

## 提交信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)。

```
<类型>(<范围>): <简要描述>
```

**类型**：`feat` 新功能 / `fix` 修复 / `docs` 文档 / `refactor` 重构 / `test` 测试 / `chore` 杂项 / `ci` 持续集成

**范围示例**：`backend` / `frontend` / `db` / `api` / `ui` / `ci`

**示例**：
```
feat(ui): 新增双语指标图表导出
fix(backend): 修复实验状态更新竞态条件
docs(api): 更新 /runs 端点的 OpenAPI 描述
```

规则：
- 主题行不超过 50 字符，不加句号
- 正文每行不超过 72 字符
- 使用祈使句：写 "新增" 而非 "新增了"

安装提交模板：
```bash
git config commit.template .gitmessage
```

---

## 运行测试

后端测试矩阵会同时跑 **SQLite 和 PostgreSQL** 两套数据库 — CI 里两条 lane 都会执行。
本地可以二选一，或两套都跑：

```bash
# 后端单元测试 — 默认 SQLite（内存/文件）
pytest backend/

# 后端跑 PostgreSQL（docker-compose 启动临时库）
docker compose -f deploy/docker-compose.test.yml up -d postgres
DATABASE_URL=postgresql+asyncpg://em:em@localhost:5433/em_test pytest backend/
docker compose -f deploy/docker-compose.test.yml down

# 覆盖率报告
pytest backend/ --cov=backend --cov-report=html

# 前端类型检查
pnpm --dir frontend typecheck

# 前端 i18n 一致性（en-US ↔ zh-CN）
pnpm --dir frontend test:i18n

# 前端单元测试（vitest）
pnpm --dir frontend vitest run
```

只改前端的 PR 可以本地跳过 PostgreSQL lane — CI 会兜底。

---

## 提交 PR

1. Fork 并创建功能分支：`git checkout -b feat/my-feature`
2. 按规范提交代码
3. Push 后向 `main` 提交 PR
4. 填写 PR 模板：概述、变更类型、测试计划
5. 确保 CI 通过（lint + 测试）
6. 请求代码审查，并响应反馈意见

大型变更建议先以草稿 PR 形式讨论方案，再深入开发。

---

## 许可与 DCO

`Argus` 采用 **Apache License 2.0** 协议（见仓库根目录的
[`LICENSE`](./LICENSE)）。提交 PR 即视为你同意以下条款：

1. **你的贡献以 Apache-2.0 许可发布。** 我们不要求另签 CLA，开 PR 这一动作
   本身就是你同意把改动按照仓库现有许可（包括 Apache-2.0 §3 的专利授权）发布
   给项目。
2. **你有权提交该贡献。** 参考
   [Developer Certificate of Origin (DCO) 1.1](https://developercertificate.org/)
   — 代码要么是你本人原创，要么你已获得在 Apache-2.0 下贡献的授权。
3. **PR 中夹带的第三方代码必须与 Apache-2.0 兼容**（MIT / BSD-2/3 / ISC /
   Apache-2.0）。不确定时请在 PR 描述中显式标注来源与许可，便于审核。

可选：`git commit -s` 会自动加上 `Signed-off-by:` 尾部，作为 DCO 同意的证据 —
不强制，但推荐。

有明显原创性的新文件建议带上标准 Apache-2.0 头注释，或由仓库根 `LICENSE`
隐式覆盖 — 参考相邻文件的风格即可。
